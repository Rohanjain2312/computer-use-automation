"""The single gate every action passes through.

Risk is decided twice and the stricter answer wins:

* the artifact declares a risk level per step (set at discovery time, visible to
  a human reviewer);
* the gate re-derives risk at execution time from the *live* control's label.

Taking the maximum means an artifact cannot under-declare a step's risk to slip
a money-moving click past the ceiling — the control's own label re-escalates it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..artifact.schema import RISK_ORDER, ActionKind, RiskLevel
from .allowlist import AllowlistProfile


@dataclass(frozen=True)
class Decision:
    allowed: bool
    risk: RiskLevel
    reason: str
    requires_human: bool = False
    violated: str = ""
    signals: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "risk": self.risk.value,
            "reason": self.reason,
            "requires_human": self.requires_human,
            "violated": self.violated,
            "signals": self.signals,
        }


_READ_ONLY_ACTIONS = {
    ActionKind.navigate,
    ActionKind.extract,
    ActionKind.wait,
    ActionKind.assert_state,
}


class PolicyGate:
    """Allowlist + risk enforcement. Every act() call goes through ``check``."""

    def __init__(self, profile: AllowlistProfile) -> None:
        self.profile = profile
        self.ceiling = RiskLevel(profile.max_autonomous_risk)

    # -- risk -------------------------------------------------------------

    def classify(self, action: ActionKind, control_label: str = "") -> tuple[RiskLevel, list[str]]:
        label = (control_label or "").strip().casefold()
        signals: list[str] = []

        for marker in self.profile.irreversible_control_markers:
            if marker in label:
                signals.append(f"control label matches irreversible marker {marker!r}")
                return RiskLevel.irreversible_write, signals

        if action in _READ_ONLY_ACTIONS:
            return RiskLevel.read_only, signals

        for marker in self.profile.reversible_control_markers:
            if marker in label:
                signals.append(f"control label matches reversible marker {marker!r}")
                return RiskLevel.reversible_write, signals

        if action in {ActionKind.type, ActionKind.select, ActionKind.press}:
            # Filling an inquiry field changes nothing until it is submitted.
            return RiskLevel.read_only, signals
        return RiskLevel.read_only, signals

    def needs_human(self, control_label: str) -> bool:
        label = (control_label or "").strip().casefold()
        return any(m in label for m in self.profile.human_only_control_markers)

    # -- gate -------------------------------------------------------------

    def check(
        self,
        *,
        action: ActionKind,
        url: str | None = None,
        control_label: str = "",
        declared_risk: RiskLevel | None = None,
        approved: bool = False,
    ) -> Decision:
        if not self.profile.action_allowed(action.value):
            return Decision(
                False,
                RiskLevel.read_only,
                f"action type {action.value!r} is not permitted by profile {self.profile.name!r}",
                violated="action_type",
            )

        if url is not None:
            ok, why = self.profile.url_verdict(url)
            if not ok:
                return Decision(False, RiskLevel.read_only, why, violated="allowlist")

        live_risk, signals = self.classify(action, control_label)
        risk = live_risk
        if declared_risk is not None and RISK_ORDER[declared_risk] > RISK_ORDER[live_risk]:
            risk = declared_risk
            signals.append(f"artifact declared higher risk {declared_risk.value!r}")
        elif declared_risk is not None and RISK_ORDER[live_risk] > RISK_ORDER[declared_risk]:
            signals.append(
                f"live control re-escalated risk above the declared {declared_risk.value!r}"
            )

        if self.needs_human(control_label):
            return Decision(
                False,
                risk,
                f"control {control_label!r} is marked human-only by profile "
                f"{self.profile.name!r}; automation must hand off",
                requires_human=True,
                violated="human_only",
                signals=signals,
            )

        if RISK_ORDER[risk] > RISK_ORDER[self.ceiling]:
            if risk is RiskLevel.irreversible_write:
                policy = self.profile.irreversible_policy
            else:
                policy = self.profile.approval_required_policy
            if policy == "require_human" or approved:
                return Decision(
                    approved,
                    risk,
                    (
                        f"{risk.value} action approved by operator"
                        if approved
                        else f"{risk.value} action exceeds the {self.ceiling.value} ceiling "
                        "and requires a human decision"
                    ),
                    requires_human=not approved,
                    violated="" if approved else "risk_ceiling",
                    signals=signals,
                )
            return Decision(
                False,
                risk,
                f"{risk.value} action is blocked by profile {self.profile.name!r} "
                f"(policy={policy!r}); ceiling is {self.ceiling.value}",
                violated="risk_ceiling",
                signals=signals,
            )

        return Decision(True, risk, f"{risk.value} action within policy", signals=signals)

    def check_landing_url(self, url: str) -> Decision:
        """Post-action check: a click may navigate somewhere not on the allowlist."""
        ok, why = self.profile.url_verdict(url)
        return Decision(ok, RiskLevel.read_only, why, violated="" if ok else "allowlist_landing")
