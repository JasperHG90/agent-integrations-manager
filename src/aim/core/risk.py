"""Semantic risk classification for artifact content.

Complements the hidden-Unicode scan (`content_guard`) by judging *what an artifact
instructs*. Two tiers cover two distinct threats:

- a cheap, always-on local encoder for injection/jailbreak payloads embedded in the
  text (default `protectai/deberta-v3-base-prompt-injection-v2`), and
- an optional judge that evaluates the artifact against the policy's explicit rule
  set (malicious-execution intent), driven through DSPy for structured output.

Everything heavy is imported lazily, so importing this module pulls no ML deps. The
whole subsystem is OFF unless the governing policy's `[risk]` enables it — so by
default this is a no-op. It is advisory-first: blocking requires `mode = "block"`
in the policy (intended only once a model is measured against a labeled corpus).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Protocol

from aim.core import paths, policy

_SEVERITY_TO_LEVEL: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


class RiskBlockedError(ValueError):
    """An artifact's risk verdict meets the policy's block threshold."""


class RiskDependencyError(RuntimeError):
    """A risk backend needs an optional dependency that is not installed."""


class RiskLevel(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2

    @classmethod
    def from_severity(cls, severity: str) -> RiskLevel:
        return cls(_SEVERITY_TO_LEVEL.get(severity, 2))


@dataclass(frozen=True)
class RiskVerdict:
    level: RiskLevel
    reasons: list[str] = field(default_factory=list)
    source: str = ""


@dataclass(frozen=True)
class RiskConfig:
    enabled: bool
    mode: str  # warn | block
    backend: str  # null | local | judge | tiered
    model_id: str
    block_threshold: RiskLevel
    escalate_threshold: RiskLevel
    judge: str | None
    rules: list[policy.RiskRule]


def config_from_policy(pol: policy.Policy) -> RiskConfig:
    r = pol.risk
    return RiskConfig(
        enabled=r.enabled,
        mode=r.mode,
        backend=r.backend,
        model_id=r.model_id,
        block_threshold=RiskLevel.from_severity(r.block_threshold),
        escalate_threshold=RiskLevel.from_severity(r.escalate_threshold),
        judge=r.judge,
        rules=policy.active_rules(pol),
    )


# ---------- classifier protocol + backends (all heavy imports are lazy) ----------


class RiskClassifier(Protocol):
    def classify(self, text: str, *, source: str | None = None) -> RiskVerdict: ...


class NullClassifier:
    def classify(self, text: str, *, source: str | None = None) -> RiskVerdict:
        return RiskVerdict(RiskLevel.LOW, [], "null")


def risk_model_dir() -> Path:
    """Cache location for local risk models — under platformdirs' user cache, so
    aim never downloads into the cwd or a stray HF default."""
    return paths.user_cache_dir() / "risk-model"


@dataclass
class LocalOnnxClassifier:
    """Cheap injection/jailbreak screen. Lazily loads an ONNX text-classification
    model; raises RiskDependencyError if the `risk` extra is not installed."""

    model_id: str

    def classify(self, text: str, *, source: str | None = None) -> RiskVerdict:
        try:
            import onnxruntime  # noqa: F401
            from huggingface_hub import snapshot_download  # noqa: F401
            from tokenizers import Tokenizer  # noqa: F401
        except ImportError as exc:
            raise RiskDependencyError(
                "local risk model needs the 'risk' extra: pip install agent-init[risk]"
            ) from exc
        cache_dir = risk_model_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Real inference is wired once a measured model is pinned. When enabled it
        # downloads into the platformdirs cache and loads the session/tokenizer from
        # there: snapshot_download(self.model_id, cache_dir=str(cache_dir)).
        raise RiskDependencyError("local risk model inference not yet enabled")


@dataclass
class JudgeClassifier:
    """Evaluates the artifact against the policy's explicit rule set via DSPy,
    returning per-rule findings. Lazily loads DSPy; raises RiskDependencyError if
    the `risk-judge` extra is not installed.

    aim only needs DSPy here — DSPy accesses the configured language model on its
    own. How that model is hosted (a local server, a remote endpoint) and which
    model the `judge` setting names is the user's concern, configured through DSPy,
    not something aim manages."""

    config: RiskConfig

    def classify(self, text: str, *, source: str | None = None) -> RiskVerdict:
        try:
            import dspy  # noqa: F401
        except ImportError as exc:
            raise RiskDependencyError(
                "risk judge needs the 'risk-judge' extra: pip install agent-init[risk-judge]"
            ) from exc
        raise RiskDependencyError("risk judge inference not yet enabled")


@dataclass
class TieredClassifier:
    local: RiskClassifier | None
    judge: RiskClassifier | None
    escalate_threshold: RiskLevel

    def classify(self, text: str, *, source: str | None = None) -> RiskVerdict:
        base = (
            self.local.classify(text, source=source)
            if self.local is not None
            else RiskVerdict(RiskLevel.LOW, [], "none")
        )
        if self.judge is None or base.level < self.escalate_threshold:
            return base
        judged = self.judge.classify(text, source=source)
        if judged.level >= base.level:
            return RiskVerdict(judged.level, [*base.reasons, *judged.reasons], "tiered")
        return RiskVerdict(base.level, [*base.reasons, *judged.reasons], "tiered")


_override: RiskClassifier | None = None


def set_classifier(classifier: RiskClassifier | None) -> None:
    """Override the active classifier (tests inject a fake here)."""
    global _override
    _override = classifier


def reset_classifier() -> None:
    global _override
    _override = None


def get_classifier(config: RiskConfig) -> RiskClassifier:
    if _override is not None:
        return _override
    if not config.enabled or config.backend == "null":
        return NullClassifier()
    local = LocalOnnxClassifier(config.model_id) if config.backend in ("local", "tiered") else None
    judge = (
        JudgeClassifier(config)
        if (config.backend in ("judge", "tiered") and config.judge)
        else None
    )
    if config.backend == "local":
        return local or NullClassifier()
    if config.backend == "judge":
        return judge or NullClassifier()
    return TieredClassifier(local, judge, config.escalate_threshold)


# ---------- verdict store: per-artifact history (last 3), keyed by content hash ----------

_HISTORY = 3


def _verdict_store_path(qualified_name: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", qualified_name)
    return paths.user_cache_dir() / "risk-verdicts" / f"{safe}.json"


def _load_history(qualified_name: str) -> list[dict]:
    path = _verdict_store_path(qualified_name)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def verdict_cache_get(qualified_name: str, content_hash: str) -> RiskVerdict | None:
    for entry in _load_history(qualified_name):
        if entry.get("content_hash") == content_hash:
            return RiskVerdict(RiskLevel(entry["level"]), list(entry.get("reasons", [])), "cache")
    return None


def verdict_cache_put(qualified_name: str, content_hash: str, verdict: RiskVerdict) -> None:
    history = [e for e in _load_history(qualified_name) if e.get("content_hash") != content_hash]
    history.append(
        {
            "content_hash": content_hash,
            "level": int(verdict.level),
            "reasons": verdict.reasons,
            "source": verdict.source,
        }
    )
    history = history[-_HISTORY:]
    path = _verdict_store_path(qualified_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history), encoding="utf-8")


# ---------- advisory warnings buffer (drained by the CLI/TUI) ----------

_risk_warnings: list[str] = []


def take_risk_warnings() -> list[str]:
    out = list(_risk_warnings)
    _risk_warnings.clear()
    return out


# ---------- enforcement entry ----------


def assert_acceptable_risk(
    text: str, *, source: str, config: RiskConfig, allow_risky: bool = False
) -> RiskVerdict:
    """Classify `text` and enforce the policy's risk mode.

    Returns a LOW verdict immediately when risk is disabled (the default). Caches
    verdicts by content hash so re-scans are deterministic and cheap. Raises
    RiskBlockedError only when the policy sets `mode = "block"` and the verdict
    meets the block threshold (unless `allow_risky`). Sub-block findings are pushed
    to the advisory warnings buffer; the reasons are always populated.
    """
    if not config.enabled:
        return RiskVerdict(RiskLevel.LOW, [], "disabled")

    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    verdict = verdict_cache_get(source, content_hash)
    if verdict is None:
        try:
            verdict = get_classifier(config).classify(text, source=source)
        except RiskDependencyError as exc:
            _risk_warnings.append(f"{source}: risk scan skipped ({exc})")
            return RiskVerdict(RiskLevel.LOW, [], "unavailable")
        verdict_cache_put(source, content_hash, verdict)

    if verdict.level >= config.block_threshold:
        detail = "; ".join(verdict.reasons) or "no detail"
        if config.mode == "block" and not allow_risky:
            raise RiskBlockedError(
                f"{source}: risk {verdict.level.name} >= {config.block_threshold.name}: {detail}"
                " (pass --allow-risky to override)"
            )
        _risk_warnings.append(f"{source}: risk {verdict.level.name}: {detail}")
    elif verdict.level >= RiskLevel.MEDIUM:
        _risk_warnings.append(
            f"{source}: risk {verdict.level.name}: {'; '.join(verdict.reasons) or 'no detail'}"
        )
    return verdict


def gate(
    content: str, *, qualified_name: str, pol: policy.Policy, allow_risky: bool = False
) -> None:
    """Convenience wrapper for the deploy chokepoints: classify+enforce using the
    governing policy's risk settings. No-op when risk is disabled."""
    assert_acceptable_risk(
        content,
        source=qualified_name,
        config=config_from_policy(pol),
        allow_risky=allow_risky,
    )
