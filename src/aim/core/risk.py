"""Semantic risk classification for artifact content.

Complements the hidden-Unicode scan (`content_guard`) by judging *what an artifact
instructs*. Two tiers cover two distinct threats:

- a cheap, always-on local encoder for injection/jailbreak payloads embedded in the
  text (default `protectai/deberta-v3-base-prompt-injection-v2`), and
- an optional judge that evaluates the artifact against the policy's explicit rule
  set (malicious-execution intent), driven through DSPy for structured output.

Everything heavy is imported lazily, so importing this module pulls no ML deps. The
whole subsystem is OFF by default: it activates only when the governing policy's
`[risk]` turns on `classifier` and/or `llm_judge`. Once active, the default mode is
`block` — a verdict at or above `block_threshold` fails the deploy; `--override-risk`
overrides it unless the policy sets `allow_override = false`.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Protocol

from aim.core import paths, policy

_SEVERITY_TO_LEVEL: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

# The encoder's max sequence length. Inputs longer than this are windowed (below)
# rather than truncated, so a payload anywhere in the artifact is screened.
_MODEL_MAX_TOKENS = 512
# Tokens shared between adjacent windows, so an injection straddling a window
# boundary still lands intact inside at least one window.
_WINDOW_OVERLAP = 64


def _split_reason(reason: str) -> tuple[str, str]:
    """Split a `rule_id: evidence` reason string into its parts.

    Args:
        reason: A reason string, ideally formatted "rule_id: evidence".

    Returns:
        A (rule, evidence) tuple; ("", reason) when no separator is present.
    """
    rule, sep, evidence = reason.partition(": ")
    return (rule, evidence) if sep else ("", rule)


class RiskBlockedError(ValueError):
    """An artifact's risk verdict meets the policy's block threshold.

    Carries the structured verdict (source, level, threshold, per-rule
    violations) so the CLI can render each violation instead of one flat line.
    """

    def __init__(
        self,
        message: str,
        *,
        source: str = "",
        level: str = "",
        threshold: str = "",
        violations: list[tuple[str, str]] | None = None,
        override_hint: str = "",
    ) -> None:
        """Initialize the error with its message and structured verdict fields."""
        super().__init__(message)
        self.source = source
        self.level = level
        self.threshold = threshold
        self.violations = violations or []
        self.override_hint = override_hint


class RiskDependencyError(RuntimeError):
    """A risk backend needs an optional dependency that is not installed."""


class RiskLevel(IntEnum):
    """Ordered severity of a risk verdict, comparable against thresholds."""

    LOW = 0
    MEDIUM = 1
    HIGH = 2

    @classmethod
    def from_severity(cls, severity: str) -> RiskLevel:
        """Map a policy severity string to a level, defaulting unknowns to HIGH.

        Args:
            severity: A severity label such as "low", "medium", or "high".

        Returns:
            The matching RiskLevel; HIGH when the label is unrecognized.
        """
        return cls(_SEVERITY_TO_LEVEL.get(severity, 2))


@dataclass(frozen=True)
class RiskVerdict:
    """A classifier's outcome: a level plus the reasons and originating backend."""

    level: RiskLevel
    reasons: list[str] = field(default_factory=list)
    source: str = ""


@dataclass(frozen=True)
class RiskConfig:
    """Resolved risk settings for a deploy, derived from the governing policy."""

    mode: str  # warn | block
    classifier: bool  # run the local ONNX screen
    llm_judge: bool  # run the DSPy judge (both on -> screen then escalate to judge)
    model_id: str
    block_threshold: RiskLevel
    escalate_threshold: RiskLevel
    judge: str | None
    allow_override: bool
    rules: list[policy.RiskRule]

    @property
    def active(self) -> bool:
        """Risk scanning runs iff a classifier or the judge is enabled."""
        return self.classifier or self.llm_judge


def config_from_policy(pol: policy.Policy) -> RiskConfig:
    """Build a RiskConfig from a resolved policy's `[risk]` section.

    Args:
        pol: The governing policy whose risk settings and active rules apply.

    Returns:
        The RiskConfig used to drive classification and enforcement.
    """
    r = pol.risk
    return RiskConfig(
        mode=r.mode,
        classifier=r.classifier,
        llm_judge=r.llm_judge,
        model_id=r.model_id,
        block_threshold=RiskLevel.from_severity(r.block_threshold),
        escalate_threshold=RiskLevel.from_severity(r.escalate_threshold),
        judge=r.judge,
        allow_override=r.allow_override,
        rules=policy.active_rules(pol),
    )


class RiskClassifier(Protocol):
    """Protocol for a risk backend that classifies artifact text into a verdict."""

    def classify(self, text: str, *, source: str | None = None) -> RiskVerdict:
        """Classify `text` and return its risk verdict."""
        ...


class NullClassifier:
    """No-op classifier that always returns a LOW verdict (risk disabled)."""

    def classify(self, text: str, *, source: str | None = None) -> RiskVerdict:
        """Return a LOW verdict regardless of input."""
        return RiskVerdict(RiskLevel.LOW, [], "null")


def risk_model_dir() -> Path:
    """Return the cache directory for local risk models under the user cache.

    Kept under platformdirs' user cache so aim never downloads into the cwd or a
    stray HF default.
    """
    return paths.user_cache_dir() / "risk-model"


def _injection_label_index(config_path: Path) -> int:
    """Read which output index means 'injection/unsafe' from the model config.

    Defaults to 1 (ProtectAI convention: 0=SAFE, 1=INJECTION).

    Args:
        config_path: Path to the model's `config.json`.

    Returns:
        The output index corresponding to the unsafe/injection label.
    """
    try:
        id2label = json.loads(config_path.read_text(encoding="utf-8")).get("id2label", {})
    except (OSError, json.JSONDecodeError):
        return 1
    for idx, label in id2label.items():
        if any(k in str(label).lower() for k in ("inject", "unsafe", "jailbreak", "malicious")):
            return int(idx)
    return 1


def _injection_score_to_level(score: float) -> RiskLevel:
    """Bucket an injection probability into a RiskLevel.

    Args:
        score: Injection/jailbreak likelihood in [0, 1].

    Returns:
        HIGH at >= 0.9, MEDIUM at >= 0.5, otherwise LOW.
    """
    if score >= 0.9:
        return RiskLevel.HIGH
    if score >= 0.5:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


@functools.lru_cache(maxsize=4)
def _load_onnx_model(model_id: str) -> tuple[Any, Any, int, tuple[str, ...]]:
    """Download and load an ONNX text-classification model once, cached per model_id.

    Args:
        model_id: Hugging Face model identifier to fetch and load.

    Returns:
        A tuple of (session, tokenizer, injection_index, input_names).

    Raises:
        RiskDependencyError: The `risk` extra is missing, or the model lacks a
            usable `.onnx` file or fast tokenizer.
    """
    try:
        import onnxruntime as ort
        from huggingface_hub import snapshot_download
        from huggingface_hub.utils import disable_progress_bars
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise RiskDependencyError(
            "local risk model needs the 'risk' extra: pip install 'agent-init[risk]'"
        ) from exc
    # Quiet the "Fetching N files" / "Download complete" tqdm bars on every scan.
    disable_progress_bars()
    local_dir = Path(
        snapshot_download(
            model_id,
            cache_dir=str(risk_model_dir()),
            # Pull only what inference needs — never the torch/tf weights.
            allow_patterns=["*.onnx", "*.json", "tokenizer*", "spm.model", "*.txt"],
        )
    )
    onnx_files = sorted(local_dir.rglob("*.onnx"))
    if not onnx_files:
        raise RiskDependencyError(f"no .onnx file found in {model_id}")
    # Prefer the full-precision model.onnx over quantized variants for accuracy.
    model_path = next((p for p in onnx_files if p.name == "model.onnx"), onnx_files[0])
    tok_path = local_dir / "tokenizer.json"
    if not tok_path.exists():
        raise RiskDependencyError(
            f"{model_id} has no tokenizer.json (a fast tokenizer is required)"
        )
    tokenizer = Tokenizer.from_file(str(tok_path))
    # Saved tokenizer.json files commonly bake in a 512-token truncation; clear it so
    # classify() can window long inputs across several forward passes. Otherwise an
    # injection past the model's max length is silently dropped by head-only truncation.
    tokenizer.no_truncation()
    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    input_names = tuple(i.name for i in session.get_inputs())
    return session, tokenizer, _injection_label_index(local_dir / "config.json"), input_names


def _token_windows(tokenizer: Any, text: str):
    """Yield (ids, attention_mask) windows that together cover the whole text.

    The injection screen must see the entire artifact: head-only truncation would let
    a payload past the model's max length pass as clean. The text is sliced into
    overlapping windows by token offset and each slice re-encoded, so every window
    carries its own special tokens and stays within the model's limit.

    Args:
        tokenizer: The fast tokenizer (truncation disabled).
        text: The full artifact text to window.

    Yields:
        (ids, attention_mask) pairs, one per window; a single window for short text.
    """
    enc = tokenizer.encode(text)
    # Special tokens ([CLS]/[SEP]/[PAD]) report a (0, 0) offset; drop them so windows
    # are measured in content tokens only.
    offsets = [o for o in enc.offsets if o != (0, 0)]
    if not offsets:
        yield list(enc.ids), list(enc.attention_mask)
        return
    budget = _MODEL_MAX_TOKENS - 2  # leave room for each window's own [CLS]/[SEP]
    step = budget - _WINDOW_OVERLAP
    for start in range(0, len(offsets), step):
        window = offsets[start : start + budget]
        chunk = text[window[0][0] : window[-1][1]]
        sub = tokenizer.encode(chunk)
        yield list(sub.ids)[:_MODEL_MAX_TOKENS], list(sub.attention_mask)[:_MODEL_MAX_TOKENS]
        if start + budget >= len(offsets):
            break


@dataclass
class LocalOnnxClassifier:
    """Cheap on-device injection/jailbreak screen backed by a local ONNX model.

    Uses a local ONNX text-classification model (default
    deberta-v3-base-prompt-injection-v2) with no network egress. Raises
    RiskDependencyError if the `risk` extra is not installed.
    """

    model_id: str

    def classify(self, text: str, *, source: str | None = None) -> RiskVerdict:
        """Score `text` for injection/jailbreak likelihood into a verdict.

        Long inputs are windowed so the whole artifact is screened; the verdict
        reflects the highest-scoring window (the model's max length cannot hide a
        payload in an artifact's tail).

        Args:
            text: The artifact text to screen.
            source: Optional label for the artifact being scanned.

        Returns:
            A verdict whose level reflects the model's injection probability.
        """
        session, tokenizer, injection_index, input_names = _load_onnx_model(self.model_id)
        score = max(
            (
                self._score_window(ids, mask, session, injection_index, input_names)
                for ids, mask in _token_windows(tokenizer, text)
            ),
            default=0.0,
        )
        return RiskVerdict(
            _injection_score_to_level(score),
            [f"prompt-injection/jailbreak likelihood {score:.2f}"],
            "local:onnx",
        )

    @staticmethod
    def _score_window(
        ids: list[int],
        mask: list[int],
        session: Any,
        injection_index: int,
        input_names: tuple[str, ...],
    ) -> float:
        """Run one forward pass over a single token window and return its injection score.

        Args:
            ids: The window's token ids.
            mask: The window's attention mask.
            session: The loaded ONNX inference session.
            injection_index: Output index of the injection/unsafe label.
            input_names: The session's expected input tensor names.

        Returns:
            The softmax probability of the injection class for this window.
        """
        import numpy as np

        feed: dict[str, Any] = {}
        if "input_ids" in input_names:
            feed["input_ids"] = np.array([ids], dtype=np.int64)
        if "attention_mask" in input_names:
            feed["attention_mask"] = np.array([mask], dtype=np.int64)
        if "token_type_ids" in input_names:
            feed["token_type_ids"] = np.zeros((1, len(ids)), dtype=np.int64)
        logits = np.asarray(session.run(None, feed)[0])[0]
        exp = np.exp(logits - np.max(logits))
        probs = exp / exp.sum()
        return float(probs[injection_index])


def _parse_findings(raw: str) -> list[dict]:
    """Tolerantly parse the judge's findings JSON, stripping fences and stray prose.

    Args:
        raw: The judge's raw output, possibly wrapped in code fences or text.

    Returns:
        The list of finding dicts, or an empty list when parsing fails.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1 :] if "\n" in text else text
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [f for f in data if isinstance(f, dict)] if isinstance(data, list) else []


def _judge_verdict(findings: list[dict], rules: list[policy.RiskRule]) -> RiskVerdict:
    """Fold per-rule findings into a single verdict.

    Level is the max severity among violated rules; reasons name the rule that
    fired and its evidence.

    Args:
        findings: Parsed per-rule findings from the judge.
        rules: The active rules, used to resolve each finding's severity.

    Returns:
        A verdict aggregating the violated rules, or LOW when none violated.
    """
    severity_by_id = {r.id: r.severity for r in rules}
    violated = [f for f in findings if f.get("violated") in (True, "true", "True", 1)]
    if not violated:
        return RiskVerdict(RiskLevel.LOW, [], "judge")
    level = max(
        (
            RiskLevel.from_severity(severity_by_id.get(str(f.get("rule_id")), "high"))
            for f in violated
        ),
        default=RiskLevel.LOW,
    )
    # Always keep the "rule_id: evidence" separator so the renderer can split a judge
    # finding back into its (rule, evidence) columns even when the model gave no evidence.
    reasons = [
        f"{f.get('rule_id')}: {str(f.get('evidence') or '(no evidence given)').strip()}"
        for f in violated
    ]
    return RiskVerdict(level, reasons, "judge")


@functools.lru_cache(maxsize=4)
def _judge_lm(judge: str) -> Any:
    """Build a DSPy LM once per model string, reused across scans.

    Cached so client/connection setup is not paid on every classify call.

    Args:
        judge: The DSPy model string (e.g. "ollama_chat/llama3").

    Returns:
        The constructed DSPy LM instance.
    """
    import dspy

    return dspy.LM(judge)


@functools.lru_cache(maxsize=1)
def _rule_judge_signature() -> Any:
    """Return the judge DSPy Signature, built once and cached.

    Re-declaring it per call made DSPy reparse it every scan.
    """
    import dspy

    class _RuleJudge(dspy.Signature):
        """Assess an AI agent artifact (skill/rule/subagent instructions) against
        each risk rule. Judge execution-time intent — data exfiltration, destructive
        operations, remote code execution, privilege escalation, obfuscation — not
        mere mention. Mark `violated` true only with concrete evidence in the text."""

        artifact: str = dspy.InputField(desc="the artifact text to assess")
        rules: str = dspy.InputField(desc="JSON list of rules [{id, severity, guidance}]")
        findings: str = dspy.OutputField(
            desc='JSON list [{"rule_id": str, "violated": bool, "evidence": str}]'
        )

    return _RuleJudge


@dataclass
class JudgeClassifier:
    """Evaluate the artifact against the policy's rule set via a DSPy judge.

    Returns per-rule findings. Lazily loads DSPy; raises RiskDependencyError if the
    `risk-judge` extra is not installed.

    aim only needs DSPy here — DSPy accesses the configured language model on its
    own via the model string in `[risk].judge` (e.g. "ollama_chat/llama3" or
    "openai/gpt-4o"). How that model is hosted and authenticated (local server,
    remote endpoint, env credentials) is the user's concern, not aim's.
    """

    config: RiskConfig
    lm: Any = None  # pre-built DSPy LM; tests/advanced users inject one here

    def classify(self, text: str, *, source: str | None = None) -> RiskVerdict:
        """Judge `text` against the active rules and return a verdict.

        Args:
            text: The artifact text to assess.
            source: Optional label for the artifact being scanned.

        Returns:
            A verdict folding the judge's per-rule findings; LOW when no rules apply.

        Raises:
            RiskDependencyError: The `risk-judge` extra is missing, or no judge
                model is configured or available.
        """
        try:
            import dspy
        except ImportError as exc:
            raise RiskDependencyError(
                "risk judge needs the 'risk-judge' extra: pip install 'agent-init[risk-judge]'"
            ) from exc
        lm = self.lm if self.lm is not None else dspy.settings.lm
        if lm is None:
            if not self.config.judge:
                raise RiskDependencyError(
                    "no judge model configured; set [risk].judge in the policy"
                )
            lm = _judge_lm(self.config.judge)
        rules = self.config.rules
        if not rules:
            return RiskVerdict(RiskLevel.LOW, [], "judge")

        payload = json.dumps(
            [{"id": r.id, "severity": r.severity, "guidance": r.prompt} for r in rules]
        )
        with dspy.context(lm=lm):
            out = dspy.Predict(_rule_judge_signature())(artifact=text, rules=payload)
        return _judge_verdict(_parse_findings(out.findings), rules)


@dataclass
class TieredClassifier:
    """Run the cheap local screen first and only escalate clean text to the judge.

    The local injection screen is the first gate. It runs BEFORE the judge: if it
    flags (level >= escalate_threshold) the verdict is returned as-is and the judge is
    never consulted — an injection hit is not one of the judge's rules, so it must not
    be merged into the judge's findings. Only a clean screen falls through to the judge.
    """

    local: RiskClassifier | None
    judge: RiskClassifier | None
    escalate_threshold: RiskLevel

    def classify(self, text: str, *, source: str | None = None) -> RiskVerdict:
        """Screen `text` locally, escalating to the judge only when below threshold.

        Args:
            text: The artifact text to classify.
            source: Optional label for the artifact being scanned.

        Returns:
            The screen's verdict if it meets the escalate threshold, else the
            judge's verdict, or LOW when neither backend is configured.
        """
        if self.local is not None:
            base = self.local.classify(text, source=source)
            if base.level >= self.escalate_threshold:
                return base
        if self.judge is not None:
            return self.judge.classify(text, source=source)
        return RiskVerdict(RiskLevel.LOW, [], "none")


_override: RiskClassifier | None = None


def set_classifier(classifier: RiskClassifier | None) -> None:
    """Override the active classifier (tests inject a fake here)."""
    global _override
    _override = classifier


def reset_classifier() -> None:
    """Clear any classifier override, restoring policy-driven selection."""
    global _override
    _override = None


def get_classifier(config: RiskConfig) -> RiskClassifier:
    """Select the classifier for `config`, honoring any test override.

    Args:
        config: The resolved risk settings selecting which backends run.

    Returns:
        A tiered classifier when both backends are on, the single enabled
        backend, the override when set, or a NullClassifier when risk is off.
    """
    if _override is not None:
        return _override
    if not config.active:
        return NullClassifier()
    local = LocalOnnxClassifier(config.model_id) if config.classifier else None
    judge = JudgeClassifier(config) if config.llm_judge else None
    if local is not None and judge is not None:
        # Both on -> the cheap screen gates the judge: a screen hit blocks here, a
        # clean screen falls through to the judge (which never sees the screen's hit).
        return TieredClassifier(local, judge, config.escalate_threshold)
    return local or judge or NullClassifier()


def prewarm(project_root: Path | None) -> None:
    """Best-effort: warm the active risk model(s) in a daemon thread.

    The expensive one-time init (ONNX session build, DSPy import) overlaps the
    artifact fetch instead of blocking the scan. A no-op when risk is off. Never
    raises — policy resolution and load errors are swallowed; the real scan inside
    the deploy surfaces them.

    Args:
        project_root: Project directory used to resolve the effective policy.
    """
    try:
        config = config_from_policy(policy.effective_policy(project_root))
    except Exception:
        return
    if not config.active:
        return

    def _load() -> None:
        """Warm the active backend, swallowing any load error."""
        try:
            if config.classifier:
                # The screen gates the judge, so only warm the screen here — eagerly
                # spinning up the judge LM would do DSPy/model work for artifacts the
                # screen is about to block.
                _load_onnx_model(config.model_id)
            elif config.llm_judge and config.judge:
                _judge_lm(config.judge)
                _rule_judge_signature()
        except Exception:
            pass

    threading.Thread(target=_load, daemon=True).start()


_HISTORY = 3


def _verdict_store_path(qualified_name: str) -> Path:
    """Return the per-artifact verdict-history file path, sanitizing the name.

    Args:
        qualified_name: The artifact's qualified name, used as the file stem.

    Returns:
        The cache path holding that artifact's verdict history.
    """
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", qualified_name)
    return paths.user_cache_dir() / "risk-verdicts" / f"{safe}.json"


def _load_history(qualified_name: str) -> list[dict]:
    """Load an artifact's stored verdict history, or empty on missing/corrupt file.

    Args:
        qualified_name: The artifact whose history to read.

    Returns:
        The list of stored verdict entries, or an empty list.
    """
    path = _verdict_store_path(qualified_name)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def config_fingerprint(config: RiskConfig) -> str:
    """Compute a stable identity hash of the classifier configuration.

    A cached verdict is only valid for the same fingerprint, so changing the
    rules/backend/judge/thresholds correctly invalidates stale verdicts instead of
    reusing a level computed under old rules.

    Args:
        config: The risk configuration to fingerprint.

    Returns:
        A 16-character hex digest identifying the configuration.
    """
    payload = json.dumps(
        {
            "classifier": config.classifier,
            "llm_judge": config.llm_judge,
            "model_id": config.model_id,
            "judge": config.judge,
            "escalate": int(config.escalate_threshold),
            "rules": sorted((r.id, r.severity, r.prompt) for r in config.rules),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def verdict_cache_get(
    qualified_name: str, content_hash: str, fingerprint: str
) -> RiskVerdict | None:
    """Return a cached verdict matching the content hash and config fingerprint.

    Args:
        qualified_name: The artifact whose history to search.
        content_hash: Hash of the exact content scanned.
        fingerprint: Config fingerprint the cached verdict must match.

    Returns:
        The matching verdict, or None when no valid cache entry exists.
    """
    for entry in _load_history(qualified_name):
        if entry.get("content_hash") == content_hash and entry.get("fingerprint") == fingerprint:
            return RiskVerdict(RiskLevel(entry["level"]), list(entry.get("reasons", [])), "cache")
    return None


_store_lock = threading.Lock()


def verdict_cache_put(
    qualified_name: str, content_hash: str, fingerprint: str, verdict: RiskVerdict
) -> None:
    """Store a verdict in the artifact's bounded history, replacing any stale match.

    Args:
        qualified_name: The artifact whose history to update.
        content_hash: Hash of the exact content scanned.
        fingerprint: Config fingerprint under which the verdict was produced.
        verdict: The verdict to persist.
    """
    path = _verdict_store_path(qualified_name)
    key = (content_hash, fingerprint)
    with _store_lock:  # sync fans gates out across threads; serialize the RMW + write
        history = [
            e
            for e in _load_history(qualified_name)
            if (e.get("content_hash"), e.get("fingerprint")) != key
        ]
        history.append(
            {
                "content_hash": content_hash,
                "fingerprint": fingerprint,
                "level": int(verdict.level),
                "reasons": verdict.reasons,
                "source": verdict.source,
            }
        )
        history = history[-_HISTORY:]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(history), encoding="utf-8")
        os.replace(tmp, path)  # atomic publish (intra-process lock only)


_risk_warnings: list[str] = []
_warnings_lock = threading.Lock()


def _warn(message: str) -> None:
    """Append an advisory risk message to the thread-safe warnings buffer."""
    with _warnings_lock:
        _risk_warnings.append(message)


def take_risk_warnings() -> list[str]:
    """Drain and return the buffered advisory risk warnings."""
    with _warnings_lock:
        out = list(_risk_warnings)
        _risk_warnings.clear()
    return out


def assert_acceptable_risk(
    text: str, *, source: str, config: RiskConfig, override_risk: bool = False
) -> RiskVerdict:
    """Classify `text` and enforce the policy's risk mode.

    Returns a LOW verdict immediately when risk is disabled (the default). Caches
    verdicts by content hash so re-scans are deterministic and cheap. Sub-block
    findings are pushed to the advisory warnings buffer; the reasons are always
    populated.

    Args:
        text: The artifact content to classify.
        source: Qualified name of the artifact, used for caching and messages.
        config: The resolved risk configuration to enforce.
        override_risk: Bypass blocking when the policy permits overrides.

    Returns:
        The computed (or cached) risk verdict.

    Raises:
        RiskBlockedError: The policy sets `mode = "block"` and the verdict meets
            the block threshold (and no honored override applies), or block mode
            is required but the classifier dependency is unavailable.
    """
    if not config.active:
        return RiskVerdict(RiskLevel.LOW, [], "disabled")

    # The override is honored only when the policy permits it (allow_override).
    override = override_risk and config.allow_override

    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    fingerprint = config_fingerprint(config)
    verdict = verdict_cache_get(source, content_hash, fingerprint)
    if verdict is None:
        try:
            verdict = get_classifier(config).classify(text, source=source)
        except RiskDependencyError as exc:
            # In block mode the policy demands enforcement, so an unavailable
            # classifier must fail CLOSED — otherwise block mode is silently vacuous.
            if config.mode == "block" and not override:
                raise RiskBlockedError(
                    f"{source}: policy requires risk blocking but the classifier is "
                    f"unavailable ({exc}); install the risk extra or pass --override-risk"
                ) from exc
            _warn(f"{source}: risk scan skipped ({exc})")
            return RiskVerdict(RiskLevel.LOW, [], "unavailable")
        verdict_cache_put(source, content_hash, fingerprint, verdict)

    if verdict.level >= config.block_threshold:
        detail = "; ".join(verdict.reasons) or "no detail"
        if config.mode == "block" and not override:
            hint = (
                "override disabled by policy"
                if not config.allow_override
                else "pass --override-risk to override"
            )
            raise RiskBlockedError(
                f"{source}: risk {verdict.level.name} >= {config.block_threshold.name}: "
                f"{detail} ({hint})",
                source=source,
                level=verdict.level.name,
                threshold=config.block_threshold.name,
                violations=[_split_reason(r) for r in verdict.reasons],
                override_hint=hint,
            )
        _warn(f"{source}: risk {verdict.level.name}: {detail}")
    elif verdict.level >= RiskLevel.MEDIUM:
        _warn(f"{source}: risk {verdict.level.name}: {'; '.join(verdict.reasons) or 'no detail'}")
    return verdict


def gate(
    content: str, *, qualified_name: str, pol: policy.Policy, override_risk: bool = False
) -> None:
    """Classify and enforce content at a deploy chokepoint using the policy's risk settings.

    A no-op when risk is disabled.

    Args:
        content: The artifact content to gate.
        qualified_name: Qualified name of the artifact, used as the source label.
        pol: The governing policy supplying risk settings.
        override_risk: Bypass blocking when the policy permits overrides.
    """
    assert_acceptable_risk(
        content,
        source=qualified_name,
        config=config_from_policy(pol),
        override_risk=override_risk,
    )
