"""Tests for the semantic risk classifier (advisory-first, fake classifier)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aim.core import agent_install, policy, risk


@pytest.fixture(autouse=True)
def _reset_classifier() -> Iterator[None]:
    risk.reset_classifier()
    risk.take_risk_warnings()
    yield
    risk.reset_classifier()
    risk.take_risk_warnings()


class FakeClassifier:
    """Returns a fixed level and counts how often it is asked."""

    def __init__(self, level: risk.RiskLevel) -> None:
        self.level = level
        self.calls = 0

    def classify(self, text: str, *, source: str | None = None) -> risk.RiskVerdict:
        self.calls += 1
        return risk.RiskVerdict(self.level, [f"rule:{self.level.name}"], "fake")


def _enabled_config(*, mode: str = "warn", block: str = "high") -> risk.RiskConfig:
    pol = policy.Policy(name="local")
    pol.risk.enabled = True
    pol.risk.mode = mode
    pol.risk.block_threshold = block
    return risk.config_from_policy(pol)


# ---------------------------------------------------------------------------
# enablement & policy mapping
# ---------------------------------------------------------------------------


def test_disabled_returns_low_without_classifying(home: Path) -> None:
    fake = FakeClassifier(risk.RiskLevel.HIGH)
    risk.set_classifier(fake)
    cfg = risk.config_from_policy(policy.Policy())  # risk disabled by default
    verdict = risk.assert_acceptable_risk("x", source="a/b", config=cfg)
    assert verdict.level is risk.RiskLevel.LOW
    assert fake.calls == 0  # short-circuited; classifier never consulted


def test_config_from_policy_includes_active_rules() -> None:
    cfg = risk.config_from_policy(policy.Policy())
    ids = {r.id for r in cfg.rules}
    assert "secret_exfiltration" in ids and "destructive_ops" in ids


# ---------------------------------------------------------------------------
# warn vs block + threshold + override
# ---------------------------------------------------------------------------


def test_warn_mode_pushes_warning_never_raises(home: Path) -> None:
    risk.set_classifier(FakeClassifier(risk.RiskLevel.HIGH))
    verdict = risk.assert_acceptable_risk("danger", source="a/b", config=_enabled_config())
    assert verdict.level is risk.RiskLevel.HIGH
    warnings = risk.take_risk_warnings()
    assert any("a/b" in w for w in warnings)


def test_block_mode_raises_and_allow_risky_overrides(home: Path) -> None:
    risk.set_classifier(FakeClassifier(risk.RiskLevel.HIGH))
    cfg = _enabled_config(mode="block")
    with pytest.raises(risk.RiskBlockedError, match="rule:HIGH"):
        risk.assert_acceptable_risk("danger", source="a/b", config=cfg)
    # override succeeds and never raises
    verdict = risk.assert_acceptable_risk("danger", source="a/b", config=cfg, allow_risky=True)
    assert verdict.level is risk.RiskLevel.HIGH


def test_below_block_threshold_does_not_block(home: Path) -> None:
    risk.set_classifier(FakeClassifier(risk.RiskLevel.MEDIUM))
    cfg = _enabled_config(mode="block", block="high")
    verdict = risk.assert_acceptable_risk("x", source="a/b", config=cfg)  # MEDIUM < HIGH
    assert verdict.level is risk.RiskLevel.MEDIUM


def test_reasons_always_populated(home: Path) -> None:
    risk.set_classifier(FakeClassifier(risk.RiskLevel.HIGH))
    verdict = risk.assert_acceptable_risk("x", source="a/b", config=_enabled_config())
    assert verdict.reasons  # never empty


# ---------------------------------------------------------------------------
# verdict cache (determinism) + history
# ---------------------------------------------------------------------------


def test_verdict_cached_by_content_hash(home: Path) -> None:
    fake = FakeClassifier(risk.RiskLevel.LOW)
    risk.set_classifier(fake)
    cfg = _enabled_config()
    risk.assert_acceptable_risk("same", source="a/b", config=cfg)
    risk.assert_acceptable_risk("same", source="a/b", config=cfg)
    assert fake.calls == 1  # second call served from cache
    risk.assert_acceptable_risk("different", source="a/b", config=cfg)
    assert fake.calls == 2  # new content reclassifies


def test_history_capped_at_three(home: Path) -> None:
    risk.set_classifier(FakeClassifier(risk.RiskLevel.LOW))
    cfg = _enabled_config()
    for i in range(5):
        risk.assert_acceptable_risk(f"content-{i}", source="a/b", config=cfg)
    history = risk._load_history("a/b")
    assert len(history) == 3


def test_dependency_unavailable_degrades_to_low(home: Path) -> None:
    class Missing:
        def classify(self, text: str, *, source: str | None = None) -> risk.RiskVerdict:
            raise risk.RiskDependencyError("not installed")

    risk.set_classifier(Missing())
    verdict = risk.assert_acceptable_risk("x", source="a/b", config=_enabled_config(mode="block"))
    assert verdict.level is risk.RiskLevel.LOW  # never blocks on a missing dependency
    assert any("skipped" in w for w in risk.take_risk_warnings())


# ---------------------------------------------------------------------------
# tiered escalation
# ---------------------------------------------------------------------------


def test_tiered_escalates_and_takes_max() -> None:
    local = FakeClassifier(risk.RiskLevel.MEDIUM)
    judge = FakeClassifier(risk.RiskLevel.HIGH)
    tiered = risk.TieredClassifier(local, judge, risk.RiskLevel.MEDIUM)
    verdict = tiered.classify("x")
    assert verdict.level is risk.RiskLevel.HIGH
    assert judge.calls == 1


def test_tiered_skips_judge_below_escalation() -> None:
    local = FakeClassifier(risk.RiskLevel.LOW)
    judge = FakeClassifier(risk.RiskLevel.HIGH)
    tiered = risk.TieredClassifier(local, judge, risk.RiskLevel.MEDIUM)
    verdict = tiered.classify("x")
    assert verdict.level is risk.RiskLevel.LOW
    assert judge.calls == 0


# ---------------------------------------------------------------------------
# deploy-gate wiring
# ---------------------------------------------------------------------------


def test_gate_blocks_high_risk_when_policy_blocks(home: Path) -> None:
    pol = policy.Policy(name="local")
    pol.risk.enabled = True
    pol.risk.mode = "block"
    policy.save_local_policy(pol)
    risk.set_classifier(FakeClassifier(risk.RiskLevel.HIGH))
    with pytest.raises(risk.RiskBlockedError):
        agent_install._gate_agent("r/ok", "danger")


def test_gate_noop_when_risk_disabled(home: Path) -> None:
    # No local policy -> built-in permissive (risk disabled): the HIGH fake is
    # never consulted, so the gate does not block.
    risk.set_classifier(FakeClassifier(risk.RiskLevel.HIGH))
    agent_install._gate_agent("r/ok", "danger")  # no raise
