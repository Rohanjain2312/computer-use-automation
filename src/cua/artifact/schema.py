"""The capability artifact: the contract between discovery and replay.

Everything in this module is deliberately surface-neutral. A capability
artifact describes *what a human operator does* to accomplish a task — in
terms of roles, accessible names, visible labels and table structure — not in
terms of any one automation library. A Playwright web surface, a legacy
frameset and a native desktop surface can all execute the same artifact as
long as they implement the ``Surface`` protocol and support some subset of the
targeting strategies declared here.

Design rules enforced by ``cua.artifact.validate``:

* no literal secret or PII values are ever stored (only ``{{ inputs.x }}`` refs)
* every step that changes state carries an explicit risk level
* every step that must be proven carries a checkpoint
* the raw model transcript is referenced by hash + evidence path, never inlined
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class ValueType(str, Enum):
    """Types usable for declared inputs and outputs."""

    string = "string"
    integer = "integer"
    decimal = "decimal"
    money = "money"
    boolean = "boolean"
    date = "date"
    enum = "enum"


class Sensitivity(str, Enum):
    """Persistence policy for a declared value.

    ``public``/``internal`` may be written to logs and evidence verbatim.
    ``pii`` is returned to the caller but redacted everywhere it is persisted.
    ``secret`` is never persisted and never typed into a screenshot unmasked.
    """

    public = "public"
    internal = "internal"
    pii = "pii"
    secret = "secret"


class ActionKind(str, Enum):
    navigate = "navigate"
    click = "click"
    type = "type"
    select = "select"
    press = "press"
    extract = "extract"
    wait = "wait"
    assert_state = "assert_state"


class RiskLevel(str, Enum):
    """How hard an action is to undo.

    The ordering matters: ``PolicyGate`` compares against a configured ceiling.
    """

    read_only = "read_only"
    reversible_write = "reversible_write"
    irreversible_write = "irreversible_write"


RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.read_only: 0,
    RiskLevel.reversible_write: 1,
    RiskLevel.irreversible_write: 2,
}


class StrategyKind(str, Enum):
    """How a control is identified, ordered roughly by portability.

    ``role_name`` and ``label_proximity`` survive markup churn and have direct
    equivalents in desktop accessibility APIs. ``dom_hint`` is web-only and
    brittle; ``viewport_ratio`` is a last resort recorded so a run can still
    complete (and so the failure is visible in evidence when it is used).
    """

    role_name = "role_name"
    label_proximity = "label_proximity"
    text_anchor = "text_anchor"
    table_cell = "table_cell"
    dom_hint = "dom_hint"
    viewport_ratio = "viewport_ratio"


PORTABLE_STRATEGIES = {
    StrategyKind.role_name,
    StrategyKind.label_proximity,
    StrategyKind.text_anchor,
    StrategyKind.table_cell,
}


class OutcomeDisposition(str, Enum):
    """What replay does when a declared business outcome is detected."""

    return_to_caller = "return_to_caller"
    escalate_to_human = "escalate_to_human"


class RecoveryThen(str, Enum):
    """What replay does once a recovery rule has cleared the condition."""

    retry_step = "retry_step"
    continue_flow = "continue_flow"
    resume_from_step = "resume_from_step"


class ArtifactStatus(str, Enum):
    draft = "draft"
    approved = "approved"


# --------------------------------------------------------------------------
# Targeting
# --------------------------------------------------------------------------


class TargetStrategy(BaseModel):
    """One way to find a control, with an explicit robustness argument."""

    model_config = ConfigDict(extra="forbid")

    kind: StrategyKind
    params: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(
        description="Why this identifies the control and how it survives change."
    )

    @property
    def portable(self) -> bool:
        return self.kind in PORTABLE_STRATEGIES


class TargetPlan(BaseModel):
    """Ordered strategies for one control: primary first, fallbacks after.

    Replay tries each in order and records which one resolved, so drift shows
    up as "the primary stopped working" rather than as a silent behaviour change.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = Field(description="What a human would call this control.")
    strategies: list[TargetStrategy] = Field(min_length=1)
    frame: str | None = Field(
        default=None,
        description="Named frame/iframe the control lives in; None means the top document.",
    )
    require_unique: bool = True

    @model_validator(mode="after")
    def _ordered_by_confidence(self) -> "TargetPlan":
        confidences = [s.confidence for s in self.strategies]
        if confidences != sorted(confidences, reverse=True):
            raise ValueError("strategies must be ordered from highest to lowest confidence")
        return self


# --------------------------------------------------------------------------
# Predicates (checkpoints, outcome detectors, recovery triggers)
# --------------------------------------------------------------------------


class Predicate(BaseModel):
    """A boolean assertion over one observation of the surface.

    Predicates are the single mechanism behind checkpoints, business-outcome
    detectors, recovery triggers and hard-failure detectors. One evaluator,
    one set of semantics, one place to test.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "text_present",
        "text_absent",
        "element_present",
        "element_absent",
        "url_matches",
        "table_cell_matches",
        "all_of",
        "any_of",
        "not",
    ]
    params: dict[str, Any] = Field(default_factory=dict)
    children: list["Predicate"] = Field(default_factory=list)
    description: str = ""

    @model_validator(mode="after")
    def _shape(self) -> "Predicate":
        if self.kind in {"all_of", "any_of"} and not self.children:
            raise ValueError(f"{self.kind} requires children")
        if self.kind == "not" and len(self.children) != 1:
            raise ValueError("not requires exactly one child")
        return self


Predicate.model_rebuild()


# --------------------------------------------------------------------------
# Waits
# --------------------------------------------------------------------------


class WaitSpec(BaseModel):
    """What "the page is ready" means after an action.

    Deliberately never a fixed sleep: every wait is a condition with a timeout,
    so a slow host costs latency but not correctness, and a hung host produces
    a timeout that names the condition that never became true.
    """

    model_config = ConfigDict(extra="forbid")

    until: Predicate | None = None
    settle_ms: int = Field(default=250, ge=0, le=5000)
    timeout_ms: int = Field(default=10_000, ge=100, le=120_000)
    poll_ms: int = Field(default=250, ge=50, le=5000)


# --------------------------------------------------------------------------
# Values, inputs, outputs
# --------------------------------------------------------------------------


class ValueRef(BaseModel):
    """A value supplied to an action.

    Either a literal (non-sensitive only) or a reference to a declared input.
    This is what keeps credentials out of the artifact: the artifact says
    "type the value of ``operator_passcode`` here", never the passcode.
    """

    model_config = ConfigDict(extra="forbid")

    literal: str | None = None
    input: str | None = None
    config: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "ValueRef":
        provided = [v for v in (self.literal, self.input, self.config) if v is not None]
        if len(provided) != 1:
            raise ValueError("ValueRef must set exactly one of literal/input/config")
        return self


class InputSpec(BaseModel):
    """A parameter the calling agent supplies per invocation."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: ValueType
    required: bool = True
    description: str
    sensitivity: Sensitivity = Sensitivity.internal
    enum_values: list[str] | None = None
    pattern: str | None = Field(default=None, description="Regex the value must match.")
    example: str | None = Field(
        default=None, description="Synthetic example only; never a real value."
    )
    source: Literal["caller", "environment"] = Field(
        default="caller",
        description="'environment' inputs (credentials) are resolved from env vars at run time.",
    )
    env_var: str | None = None

    @model_validator(mode="after")
    def _secret_from_env(self) -> "InputSpec":
        if self.sensitivity is Sensitivity.secret:
            if self.source != "environment" or not self.env_var:
                raise ValueError(
                    f"secret input {self.name!r} must declare source='environment' and env_var"
                )
            if self.example is not None:
                raise ValueError(f"secret input {self.name!r} must not carry an example value")
        return self


class ExtractSpec(BaseModel):
    """How one declared output is read off the screen."""

    model_config = ConfigDict(extra="forbid")

    target: TargetPlan
    attribute: Literal["text", "value"] = "text"
    transforms: list[Literal["strip", "money_to_decimal", "digits_only", "upper", "collapse_ws"]] = (
        Field(default_factory=lambda: ["strip"])
    )


class OutputSpec(BaseModel):
    """A value the capability returns to the calling agent."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: ValueType
    description: str
    sensitivity: Sensitivity = Sensitivity.internal
    required: bool = True
    extract: ExtractSpec


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


class Step(BaseModel):
    """One ordered action, with the proof that it worked.

    ``intent`` is the human-readable "why" carried over from discovery; it is
    what a reviewer reads and what appears in logs and intervention requests.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    intent: str
    action: ActionKind
    target: TargetPlan | None = None
    value: ValueRef | None = None
    url: str | None = Field(default=None, description="For navigate actions.")
    key: str | None = Field(default=None, description="For press actions.")
    risk: RiskLevel = RiskLevel.read_only
    wait: WaitSpec = Field(default_factory=WaitSpec)
    checkpoint: Predicate | None = Field(
        default=None, description="Assertion proving the step actually took effect."
    )
    tags: list[str] = Field(default_factory=list)
    optional: bool = Field(
        default=False,
        description="If true, a failed checkpoint skips the step instead of failing the run.",
    )

    @model_validator(mode="after")
    def _action_shape(self) -> "Step":
        if self.action is ActionKind.navigate and not self.url:
            raise ValueError(f"step {self.id}: navigate requires url")
        if self.action in {ActionKind.click, ActionKind.type, ActionKind.select} and not self.target:
            raise ValueError(f"step {self.id}: {self.action.value} requires a target")
        if self.action in {ActionKind.type, ActionKind.select} and self.value is None:
            raise ValueError(f"step {self.id}: {self.action.value} requires a value")
        if self.action is ActionKind.press and not self.key:
            raise ValueError(f"step {self.id}: press requires key")
        return self


# --------------------------------------------------------------------------
# Runtime condition rules
# --------------------------------------------------------------------------


class OutcomeRule(BaseModel):
    """Category A — a legitimate business result the caller needs to know about.

    Not a crash. ``escalate_to_human`` covers the middle ground: a real outcome
    that a person can clear (an entitlement block), after which the run resumes.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    when: Predicate
    disposition: OutcomeDisposition = OutcomeDisposition.return_to_caller
    message: str
    remediation: str | None = None
    active_from_step: str | None = Field(
        default=None, description="Only evaluate once this step has been reached."
    )


class RecoveryAction(BaseModel):
    """One remediation move inside a recovery rule."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["click", "type", "navigate", "wait", "rerun_tagged_steps"]
    target: TargetPlan | None = None
    value: ValueRef | None = None
    url: str | None = None
    tag: str | None = Field(default=None, description="For rerun_tagged_steps.")
    wait: WaitSpec = Field(default_factory=WaitSpec)


class RecoveryRule(BaseModel):
    """Category B — a condition replay may clear on its own.

    Recovery rules are page-state triggered, not step-indexed: an interstitial
    that appears at an unpredictable point is still handled.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    when: Predicate
    actions: list[RecoveryAction] = Field(default_factory=list)
    then: RecoveryThen = RecoveryThen.retry_step
    resume_from_step: str | None = Field(
        default=None,
        description="With then='resume_from_step', the step to continue from. Used when the "
                    "condition invalidated work already done — a dropped session means every "
                    "step after sign-on must run again, not just the one that noticed.",
    )
    max_attempts: int = Field(default=2, ge=1, le=5)
    description: str = ""

    @model_validator(mode="after")
    def _shape(self) -> "RecoveryRule":
        if self.then is RecoveryThen.resume_from_step and not self.resume_from_step:
            raise ValueError(f"recovery rule {self.name!r}: then='resume_from_step' needs a step id")
        if self.then is not RecoveryThen.resume_from_step and not self.actions:
            raise ValueError(f"recovery rule {self.name!r} has no actions")
        return self


class FailureRule(BaseModel):
    """Category C — a condition that must stop the run with a debuggable error."""

    model_config = ConfigDict(extra="forbid")

    name: str
    when: Predicate
    error_class: str
    message: str


# --------------------------------------------------------------------------
# Envelope
# --------------------------------------------------------------------------


class AppProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor: str
    product: str
    version_observed: str | None = None


class TenantBinding(BaseModel):
    """Which tenant/instance this artifact was recorded against.

    ``base_url`` is a config reference rather than a baked host so the same
    artifact can run against another institution's instance of the same vendor
    product. ``variant`` names the configuration flavour for override lookup.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    variant: str = "base"
    base_url_config_key: str = "base_url"


class SurfaceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["web", "desktop"] = "web"
    entry_point: str = Field(description="Path or templated URL where the flow starts.")
    app_profile: AppProfile
    tenant: TenantBinding
    viewport: dict[str, int] = Field(default_factory=lambda: {"width": 1280, "height": 900})
    required_strategies: list[StrategyKind] = Field(
        default_factory=list,
        description="Strategies a surface must support to execute this artifact.",
    )


class SafetyBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowlist_profile: str = "default"
    max_autonomous_risk: RiskLevel = RiskLevel.read_only
    steps_requiring_approval: list[str] = Field(default_factory=list)


class Provenance(BaseModel):
    """Where the artifact came from — by reference, never by transcript."""

    model_config = ConfigDict(extra="forbid")

    discovery_run_id: str
    model: str
    model_calls: int
    created_at: str
    created_by: str = "cua-discovery"
    goal: str
    transcript_sha256: str
    transcript_evidence_path: str
    notes: str | None = None


class Verification(BaseModel):
    """Result of the smoke replay that promotes draft -> approved."""

    model_config = ConfigDict(extra="forbid")

    smoke_replay_run_id: str | None = None
    smoke_replay_status: str | None = None
    verified_at: str | None = None


class CapabilityArtifact(BaseModel):
    """A reusable, reviewable, replayable capability."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    capability_id: str = Field(description="Stable slug, e.g. 'member_savings_balance_lookup'.")
    version: str = Field(description="Semantic version of this recording.")
    name: str
    description: str
    status: ArtifactStatus = ArtifactStatus.draft

    surface: SurfaceBinding
    safety: SafetyBinding = Field(default_factory=SafetyBinding)

    inputs: list[InputSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)

    business_outcomes: list[OutcomeRule] = Field(default_factory=list)
    recovery_rules: list[RecoveryRule] = Field(default_factory=list)
    failure_rules: list[FailureRule] = Field(default_factory=list)

    success_condition: Predicate = Field(
        description="Overall proof that the capability achieved its goal."
    )

    provenance: Provenance
    verification: Verification = Field(default_factory=Verification)
    checksum: str | None = None

    # -- helpers ----------------------------------------------------------

    def step_by_id(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def steps_tagged(self, tag: str) -> list[Step]:
        return [s for s in self.steps if tag in s.tags]

    def input_by_name(self, name: str) -> InputSpec | None:
        return next((i for i in self.inputs if i.name == name), None)

    def canonical_json(self, *, include_checksum: bool = False) -> str:
        data = self.model_dump(mode="json", exclude_none=False)
        if not include_checksum:
            data.pop("checksum", None)
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    def compute_checksum(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def with_checksum(self) -> "CapabilityArtifact":
        return self.model_copy(update={"checksum": self.compute_checksum()})

    def checksum_valid(self) -> bool:
        return self.checksum == self.compute_checksum()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
