"""Shared exception groups for the TUI.

Deploy actions (install/update/rollback) can raise governance/security errors from
the policy and risk layers; screens should surface these as notifications rather
than crashing the Textual worker.
"""

from __future__ import annotations

from aim.core import content_guard, policy, risk

# Errors a policy/risk-gated deploy can raise that a screen should catch + notify.
GOVERNANCE_ERRORS: tuple[type[Exception], ...] = (
    policy.PolicyViolationError,
    policy.PolicyError,
    risk.RiskBlockedError,
    risk.RiskDependencyError,  # risk model/judge unavailable — notify, don't crash the worker
    content_guard.HiddenUnicodeError,
    content_guard.InsecureTransportError,
)
