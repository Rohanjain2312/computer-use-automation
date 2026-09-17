"""The structured result a calling agent receives.

Five terminal statuses, each meaning something different to the caller:

``success``            the goal was reached; ``outputs`` is populated.
``business_outcome``   the application gave a legitimate answer that is not the
                       happy path (no such member, entitlement refused). The
                       caller acts on it; nothing is broken.
``invalid_input``      the caller's arguments failed the declared contract. The
                       run never touched the application.
``blocked_by_policy``  the flow required an action safety policy forbids.
``failure``            something went wrong. ``failure`` carries the failed step,
                       what was expected, what was observed, and evidence paths.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class ReplayStatus(str, Enum):
    success = "success"
    business_outcome = "business_outcome"
    invalid_input = "invalid_input"
    blocked_by_policy = "blocked_by_policy"
    failure = "failure"


@dataclass
class StepRecord:
    id: str
    index: int
    intent: str
    action: str
    status: str  # ok | recovered | skipped | failed | escalated
    duration_ms: int = 0
    strategy_used: str | None = None
    strategy_confidence: float | None = None
    strategy_ambiguous: bool = False
    strategy_degraded: bool = False
    checkpoint: str | None = None
    risk: str = "read_only"
    notes: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass
class FailureDetail:
    error_class: str
    message: str
    step_id: str | None = None
    step_index: int | None = None
    step_intent: str = ""
    expected: str = ""
    observed: str = ""
    observed_url: str = ""
    locator_attempts: list[dict] = field(default_factory=list)
    recovery_attempts: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass
class OutcomeDetail:
    name: str
    message: str
    detected_at_step: str | None = None
    remediation: str | None = None
    evidence: list[str] = field(default_factory=list)


@dataclass
class ReplayResult:
    run_id: str
    capability_id: str
    capability_version: str
    status: ReplayStatus
    started_at: str
    finished_at: str = ""
    duration_ms: int = 0
    inputs_echo: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    outcome: OutcomeDetail | None = None
    failure: FailureDetail | None = None
    steps: list[StepRecord] = field(default_factory=list)
    recoveries: list[dict] = field(default_factory=list)
    escalations: list[dict] = field(default_factory=list)
    control_log: list[dict] = field(default_factory=list)
    human_actions: list[dict] = field(default_factory=list)
    llm_used: bool = False
    evidence_dir: str = ""
    log_path: str = ""

    @property
    def ok(self) -> bool:
        return self.status is ReplayStatus.success

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["outputs"] = {k: _plain(v) for k, v in self.outputs.items()}
        return data

    def summary_line(self) -> str:
        if self.status is ReplayStatus.success:
            body = ", ".join(f"{k}={_plain(v)}" for k, v in self.outputs.items())
        elif self.status is ReplayStatus.business_outcome and self.outcome:
            body = f"{self.outcome.name}: {self.outcome.message}"
        elif self.failure:
            body = f"{self.failure.error_class} at step {self.failure.step_id}: {self.failure.message}"
        else:
            body = ""
        return f"[{self.status.value}] {self.capability_id} v{self.capability_version} — {body}"


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return value
