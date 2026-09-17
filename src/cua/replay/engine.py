"""Deterministic replay.

No model is consulted anywhere in this file. Every decision comes from the
artifact: which control to find, how long to wait, what proves the step worked,
which screen states are business outcomes, which are recoverable and which are
hard failures.

The engine's shape is a loop of:

    observe -> scan conditions -> resolve target -> policy gate -> act
            -> wait for the declared condition -> verify the checkpoint

with one deliberate asymmetry: a failed checkpoint re-runs the condition scan
before it is treated as a failure, because the most common reason a checkpoint
fails is that the application answered something legitimate instead ("no member
matching that number"), and calling that a crash would be the single worst bug
this system could have.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Protocol

from ..artifact.schema import (
    ActionKind,
    CapabilityArtifact,
    OutcomeDisposition,
    Predicate,
    RecoveryAction,
    RecoveryRule,
    RecoveryThen,
    RiskLevel,
    Step,
    TargetPlan,
)
from ..artifact.templating import (
    InputValidationError,
    bind_inputs,
    coerce_output,
    render_template,
    render_value,
)
from ..artifact.validate import errors as validation_errors
from ..artifact.validate import validate_artifact
from ..handoff.control import ControlOwner, SessionControl
from ..handoff.intervention import InterventionRequest
from ..handoff.recorder import HumanActionRecorder
from ..locate.strategies import ResolveResult, resolve
from ..observability.evidence import EvidenceWriter
from ..observability.logging import RunLogger
from ..safety.policy import PolicyGate
from ..safety.redact import Redactor
from ..surface.base import Observation, UiNode
from ..surface.web import PlaywrightWebSurface
from .checkpoints import Verdict, evaluate
from .result import FailureDetail, OutcomeDetail, ReplayResult, ReplayStatus, StepRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Operator(Protocol):
    """Whoever answers an intervention request. Human or scripted stand-in."""

    name: str

    def handle(
        self,
        request: InterventionRequest,
        *,
        control: SessionControl,
        surface: PlaywrightWebSurface,
        recorder: HumanActionRecorder,
        logger: RunLogger,
        evidence: EvidenceWriter,
    ) -> str:  # "resumed" | "aborted" | "unavailable"
        ...


class _Terminal(Exception):
    """Internal control flow: this run has reached a terminal state."""

    def __init__(self, result_kwargs: dict[str, Any]) -> None:
        self.kwargs = result_kwargs
        super().__init__(result_kwargs.get("status"))


class _Rewind(Exception):
    """A recovery rule invalidated work already done; resume from an earlier step."""

    def __init__(self, step_id: str, rule: str) -> None:
        self.step_id = step_id
        self.rule = rule
        super().__init__(f"rewind to {step_id} after {rule}")


class ReplayEngine:
    MAX_ESCALATIONS = 3

    def __init__(
        self,
        artifact: CapabilityArtifact,
        surface: PlaywrightWebSurface,
        gate: PolicyGate,
        *,
        redactor: Redactor,
        logger: RunLogger,
        evidence: EvidenceWriter,
        control: SessionControl,
        operator: Operator | None = None,
        config: dict[str, Any] | None = None,
        run_id: str | None = None,
        approved_steps: set[str] | None = None,
    ) -> None:
        self.artifact = artifact
        self.surface = surface
        self.gate = gate
        self.redactor = redactor
        self.log = logger
        self.evidence = evidence
        self.control = control
        self.operator = operator
        self.config = config or {}
        self.run_id = run_id or control.run_id
        self.approved_steps = approved_steps or set()
        self.recorder = HumanActionRecorder(surface, redactor)

        self.steps: list[StepRecord] = []
        self.recoveries: list[dict] = []
        self.escalations: list[dict] = []
        self._recovery_counts: dict[str, int] = {}
        self._deadline = time.time() + gate.profile.max_run_seconds
        self._inputs: dict[str, Any] = {}
        self._reached_steps: set[str] = set()
        self._rewinds = 0
        # A rule that just restarted the flow must not fire again on the same
        # stale screen before the restarted flow has moved at all.
        self._suppressed_rule: str | None = None

    # ------------------------------------------------------------------ run

    def run(self, supplied_inputs: dict[str, Any]) -> ReplayResult:
        started = _now()
        t0 = time.time()
        self.log.log(
            "run_started",
            capability_id=self.artifact.capability_id,
            capability_version=self.artifact.version,
            status=self.artifact.status.value,
            allowlist_profile=self.gate.profile.name,
            inputs=sorted(supplied_inputs),
        )

        try:
            self._validate_artifact()
            self._bind(supplied_inputs)
            self._execute_steps()
            outputs = self._extract_outputs()
            self._verify_success()
            result = self._result(ReplayStatus.success, started, t0, outputs=outputs)
            self.control.complete("capability completed successfully")
        except _Terminal as terminal:
            result = self._result(started=started, t0=t0, **terminal.kwargs)
            if result.status is ReplayStatus.failure:
                self.control.fail(result.failure.message if result.failure else "run failed")
            else:
                self.control.complete(f"run ended: {result.status.value}")
        except Exception as exc:  # unexpected: still returns a structured failure
            failure = FailureDetail(
                error_class="internal_error",
                message=f"{type(exc).__name__}: {exc}",
                evidence=self._capture_failure_evidence("internal_error"),
            )
            result = self._result(ReplayStatus.failure, started, t0, failure=failure)
            self.control.fail(str(exc))

        result.control_log = [e.as_dict() for e in self.control.history]
        result.human_actions = [a.as_dict() for a in self.control.human_actions]
        self.log.log(
            "run_finished",
            status=result.status.value,
            duration_ms=result.duration_ms,
            outcome=result.outcome.name if result.outcome else None,
            detail=result.summary_line(),
        )
        self.evidence.write_json("result.json", result.to_dict())
        return result

    def _result(
        self,
        status: ReplayStatus,
        started: str,
        t0: float,
        *,
        outputs: dict[str, Any] | None = None,
        outcome: OutcomeDetail | None = None,
        failure: FailureDetail | None = None,
    ) -> ReplayResult:
        return ReplayResult(
            run_id=self.run_id,
            capability_id=self.artifact.capability_id,
            capability_version=self.artifact.version,
            status=status,
            started_at=started,
            finished_at=_now(),
            duration_ms=int((time.time() - t0) * 1000),
            inputs_echo=self.redactor.scrub(dict(self._inputs)),
            outputs=outputs or {},
            outcome=outcome,
            failure=failure,
            steps=self.steps,
            recoveries=self.recoveries,
            escalations=self.escalations,
            llm_used=False,
            evidence_dir=self.evidence.rel(self.evidence.root),
            log_path=self.evidence.rel(self.evidence.log_path),
        )

    # ------------------------------------------------------------- preflight

    def _validate_artifact(self) -> None:
        issues = validate_artifact(self.artifact)
        for issue in issues:
            self.log.log("artifact_issue", level=issue.level, code=issue.code,
                         detail=issue.message, location=issue.location)
        bad = validation_errors(issues)
        if bad:
            raise _Terminal(
                {
                    "status": ReplayStatus.failure,
                    "failure": FailureDetail(
                        error_class="artifact_invalid",
                        message="; ".join(str(i) for i in bad),
                        expected="an artifact that passes validation",
                        observed=f"{len(bad)} validation error(s)",
                    ),
                }
            )

    def _bind(self, supplied: dict[str, Any]) -> None:
        try:
            bound, secrets = bind_inputs(self.artifact.inputs, supplied, dict(os.environ))
        except InputValidationError as exc:
            self.log.log("input_rejected", reason=str(exc))
            raise _Terminal(
                {
                    "status": ReplayStatus.invalid_input,
                    "failure": FailureDetail(
                        error_class="invalid_input",
                        message=str(exc),
                        expected="inputs matching the declared contract",
                        observed="caller arguments rejected before touching the application",
                    ),
                }
            ) from exc
        for value in secrets:
            self.redactor.register(value, "input")
        self._inputs = bound
        self.log.log("inputs_bound", inputs={k: self.redactor.scrub_text(str(v))
                                             for k, v in bound.items()})

    # ---------------------------------------------------------------- steps

    MAX_REWINDS = 2

    def _execute_steps(self) -> None:
        index = 0
        while index < len(self.artifact.steps):
            self._check_deadline()
            step = self.artifact.steps[index]
            self._reached_steps.add(step.id)
            self._recovery_counts = {}
            try:
                record = self._run_step(step, index)
                self._suppressed_rule = None
            except _Rewind as rewind:
                target = next((i for i, s in enumerate(self.artifact.steps)
                               if s.id == rewind.step_id), None)
                if target is None:  # pragma: no cover - validated at load time
                    raise
                self._rewinds += 1
                self.log.log("flow_rewound", rule=rewind.rule, step_id=step.id,
                             detail=f"resuming from {rewind.step_id!r} "
                                    f"(rewind {self._rewinds}/{self.MAX_REWINDS})")
                if self._rewinds > self.MAX_REWINDS:
                    raise _Terminal({
                        "status": ReplayStatus.failure,
                        "failure": FailureDetail(
                            error_class="recovery_loop",
                            message=f"recovery rule {rewind.rule!r} restarted the flow "
                                    f"{self._rewinds} times without progressing",
                            step_id=step.id, step_index=index, step_intent=step.intent,
                            expected="the recovery to clear the condition for good",
                            observed="the same condition keeps recurring",
                            evidence=self._capture_failure_evidence("recovery_loop"),
                        ),
                    }) from rewind
                self._suppressed_rule = rewind.rule
                index = target
                continue
            self.steps.append(record)
            index += 1

    def _run_step(self, step: Step, index: int, *, attempt: int = 1) -> StepRecord:
        t0 = time.time()
        record = StepRecord(
            id=step.id, index=index, intent=step.intent,
            action=step.action.value, status="ok", risk=step.risk.value,
        )
        self.log.log("step_started", step_id=step.id, intent=step.intent,
                     action=step.action.value, index=index, attempt=attempt)

        self.control.assert_can_act(f"execute step {step.id}")
        obs = self.surface.observe()

        # Conditions can be true before we even act (an interstitial, an error
        # page left over from the previous step).
        if self._scan_conditions(obs, step, index, record):
            obs = self.surface.observe()

        node: UiNode | None = None
        resolution: ResolveResult | None = None
        if step.target is not None:
            obs = self.surface.observe()
            resolution = self._resolve(step.target, obs, step, index, record)
            node = resolution.node

        decision = self.gate.check(
            action=step.action,
            url=step.url and self._render(step.url) or None,
            control_label=(node.name if node else (step.target.description if step.target else "")),
            declared_risk=step.risk,
            approved=step.id in self.approved_steps,
        )
        self.log.log("policy_check", step_id=step.id, action=step.action.value,
                     **decision.as_dict())
        if not decision.allowed:
            if decision.requires_human:
                self._escalate(
                    reason_class="risky_action",
                    reason=decision.reason,
                    step=step, index=index, obs=obs,
                    suggested=[
                        f"Perform '{step.intent}' manually in the browser window",
                        "Then press Release & resume so automation continues from this step",
                    ],
                )
                record.status = "escalated"
                record.notes.append(f"escalated: {decision.reason}")
                return self._after_escalation(step, index, record, attempt)
            raise _Terminal(
                {
                    "status": ReplayStatus.blocked_by_policy,
                    "failure": FailureDetail(
                        error_class=f"policy_{decision.violated or 'blocked'}",
                        message=decision.reason,
                        step_id=step.id, step_index=index, step_intent=step.intent,
                        expected=f"an action within the {self.gate.ceiling.value} ceiling",
                        observed=f"{decision.risk.value} action ({'; '.join(decision.signals)})",
                        observed_url=obs.url,
                        evidence=self._capture_failure_evidence(f"policy_{step.id}"),
                    ),
                }
            )

        record.risk = decision.risk.value
        self._act(step, node, record, index, obs)

        # A click can navigate somewhere the allowlist forbids.
        landing = self.gate.check_landing_url(self.surface.current_url())
        if not landing.allowed:
            raise _Terminal(
                {
                    "status": ReplayStatus.blocked_by_policy,
                    "failure": FailureDetail(
                        error_class="policy_allowlist_landing",
                        message=f"step {step.id!r} navigated outside the allowlist: {landing.reason}",
                        step_id=step.id, step_index=index, step_intent=step.intent,
                        expected="a URL permitted by the allowlist profile",
                        observed=self.surface.current_url(),
                        evidence=self._capture_failure_evidence(f"landing_{step.id}"),
                    ),
                }
            )

        obs = self._wait_for(step, index, record)
        if self._scan_conditions(obs, step, index, record):
            obs = self.surface.observe()

        if step.checkpoint is not None:
            verdict = evaluate(step.checkpoint, obs)
            record.checkpoint = verdict.detail
            self.log.log("checkpoint", step_id=step.id, status="pass" if verdict.ok else "fail",
                         detail=verdict.detail)
            if not verdict.ok:
                return self._handle_failed_checkpoint(step, index, record, obs, verdict, attempt)

        record.duration_ms = int((time.time() - t0) * 1000)
        record.evidence.append(
            self.evidence.screenshot(obs.screenshot, f"{index:02d}_{step.id}") or ""
        )
        record.evidence = [e for e in record.evidence if e]
        self.log.log("step_finished", step_id=step.id, status=record.status,
                     duration_ms=record.duration_ms)
        return record

    # --------------------------------------------------------------- acting

    def _render(self, text: str) -> str:
        return render_template(text, self._inputs, self.config)

    def _resolve(
        self, plan: TargetPlan, obs: Observation, step: Step, index: int, record: StepRecord
    ) -> ResolveResult:
        resolution = resolve(plan, obs, supported=self.surface.capabilities())
        self.log.log("target_resolved", step_id=step.id, target=plan.description,
                     **resolution.summary())
        if not resolution.ok:
            raise _Terminal(
                {
                    "status": ReplayStatus.failure,
                    "failure": FailureDetail(
                        error_class="target_not_found",
                        message=f"could not find {plan.description} on screen",
                        step_id=step.id, step_index=index, step_intent=step.intent,
                        expected=f"a control matching {plan.description}",
                        observed=f"{len(obs.nodes)} controls perceived; none matched any strategy",
                        observed_url=obs.url,
                        locator_attempts=resolution.attempts,
                        evidence=self._capture_failure_evidence(f"notfound_{step.id}"),
                    ),
                }
            )
        record.strategy_used = resolution.strategy.kind.value if resolution.strategy else None
        record.strategy_confidence = resolution.strategy.confidence if resolution.strategy else None
        record.strategy_ambiguous = resolution.ambiguous
        if resolution.strategy and plan.strategies and resolution.strategy is not plan.strategies[0]:
            record.strategy_degraded = True
            record.notes.append(
                f"primary strategy {plan.strategies[0].kind.value!r} did not resolve; "
                f"fell back to {resolution.strategy.kind.value!r}"
            )
            self.log.log("locator_degraded", step_id=step.id,
                         detail=record.notes[-1], target=plan.description)
        if resolution.ambiguous:
            record.notes.append(resolution.error)
        return resolution

    def _act(self, step: Step, node: UiNode | None, record: StepRecord, index: int,
             obs: Observation) -> None:
        action = step.action
        if action is ActionKind.navigate:
            url = self._render(step.url or "")
            outcome = self.surface.open(url)
            self.log.log("action", step_id=step.id, action="navigate", detail=outcome.detail,
                         status="ok" if outcome.ok else "failed")
            if not outcome.ok:
                raise _Terminal(
                    {
                        "status": ReplayStatus.failure,
                        "failure": FailureDetail(
                            error_class="navigation_failed",
                            message=outcome.detail,
                            step_id=step.id, step_index=index, step_intent=step.intent,
                            expected=f"the application to load {url}",
                            observed=outcome.detail,
                            evidence=self._capture_failure_evidence(f"nav_{step.id}"),
                        ),
                    }
                )
            return

        if action is ActionKind.wait:
            return

        if action is ActionKind.assert_state:
            return

        if node is None:  # pragma: no cover - schema guarantees a target
            raise _Terminal({"status": ReplayStatus.failure, "failure": FailureDetail(
                error_class="internal_error", message=f"step {step.id} has no resolved target")})

        if not node.in_viewport:
            self.surface.scroll_to(node)
            fresh = self.surface.observe(screenshot=False)
            again = resolve(step.target, fresh, supported=self.surface.capabilities())
            if again.ok:
                node = again.node

        if action is ActionKind.click:
            x, y = node.rect.center
            outcome = self.surface.click_point(x, y)
            self.log.log("action", step_id=step.id, action="click", detail=outcome.detail,
                         target=node.name, status="ok" if outcome.ok else "failed")
        elif action is ActionKind.type:
            value = render_value(step.value, self._inputs, self.config)
            x, y = node.rect.center
            self.surface.click_point(x, y)
            self.surface.clear_focused()
            outcome = self.surface.type_text(value)
            self.log.log("action", step_id=step.id, action="type", target=node.name,
                         detail=f"entered value for {step.value.input or 'literal'}",
                         status="ok" if outcome.ok else "failed")
        elif action is ActionKind.select:
            value = render_value(step.value, self._inputs, self.config)
            outcome = self.surface.set_combobox_value(node, value)
            self.log.log("action", step_id=step.id, action="select", target=node.name,
                         detail=outcome.detail, status="ok" if outcome.ok else "failed")
        elif action is ActionKind.press:
            outcome = self.surface.press(step.key or "Enter")
            self.log.log("action", step_id=step.id, action="press", detail=outcome.detail,
                         status="ok" if outcome.ok else "failed")
        elif action is ActionKind.extract:
            return
        else:  # pragma: no cover
            raise _Terminal({"status": ReplayStatus.failure, "failure": FailureDetail(
                error_class="unsupported_action", message=f"cannot execute {action.value}")})

        if not outcome.ok:
            raise _Terminal(
                {
                    "status": ReplayStatus.failure,
                    "failure": FailureDetail(
                        error_class="action_failed",
                        message=outcome.detail,
                        step_id=step.id, step_index=index, step_intent=step.intent,
                        expected=f"{action.value} on {step.target.description}",
                        observed=outcome.detail,
                        observed_url=obs.url,
                        evidence=self._capture_failure_evidence(f"action_{step.id}"),
                    ),
                }
            )

    # ---------------------------------------------------------------- waits

    def _wait_for(self, step: Step, index: int, record: StepRecord) -> Observation:
        """Poll until the declared condition holds. Recovery rules run each poll.

        This is why a "slow load" is a latency cost rather than a failure, and
        why an interstitial that appears *during* the wait still gets cleared.
        """
        spec = step.wait
        if spec.settle_ms:
            self.surface.wait_ms(spec.settle_ms)
        obs = self.surface.observe()
        if spec.until is None:
            return obs

        deadline = time.time() + spec.timeout_ms / 1000.0
        last: Verdict = evaluate(spec.until, obs)
        while not last.ok and time.time() < deadline:
            self._check_deadline()
            if self._try_recover(obs, step, index, record, during="wait"):
                obs = self.surface.observe()
                last = evaluate(spec.until, obs)
                continue
            self.surface.wait_ms(spec.poll_ms)
            obs = self.surface.observe()
            last = evaluate(spec.until, obs)

        if last.ok:
            self.log.log("wait_satisfied", step_id=step.id, detail=last.detail)
            return obs

        # A timed-out wait is only a failure if nothing legitimate explains it.
        self._scan_conditions(obs, step, index, record)
        self.log.log("wait_timeout", step_id=step.id, detail=last.detail,
                     timeout_ms=spec.timeout_ms)
        raise _Terminal(
            {
                "status": ReplayStatus.failure,
                "failure": FailureDetail(
                    error_class="wait_timeout",
                    message=f"condition never became true within {spec.timeout_ms}ms: {last.detail}",
                    step_id=step.id, step_index=index, step_intent=step.intent,
                    expected=spec.until.description or last.detail,
                    observed=last.observed or obs.text[:200],
                    observed_url=obs.url,
                    evidence=self._capture_failure_evidence(f"timeout_{step.id}"),
                ),
            }
        )

    def _check_deadline(self) -> None:
        if time.time() > self._deadline:
            raise _Terminal(
                {
                    "status": ReplayStatus.failure,
                    "failure": FailureDetail(
                        error_class="run_timeout",
                        message=f"run exceeded {self.gate.profile.max_run_seconds}s",
                        expected="the flow to complete within the configured budget",
                        observed="run budget exhausted",
                        evidence=self._capture_failure_evidence("run_timeout"),
                    ),
                }
            )

    # ----------------------------------------------------- condition scanning

    def _scan_conditions(self, obs: Observation, step: Step, index: int,
                         record: StepRecord) -> bool:
        """Business outcome first, then recovery, then hard failure.

        Returns True when the scan *changed the page* — a recovery ran, or a
        human cleared a block — so the caller knows its observation is stale and
        must look again before it decides anything.

        Ordering is deliberate: a declared business outcome is the most specific,
        positively identified state and must never be masked by a generic error
        detector. Recovery comes next because it is bounded and observable.
        Failure rules are the catch-all.
        """
        outcome = self._match_business_outcome(obs)
        if outcome is not None:
            return self._handle_business_outcome(outcome, obs, step, index, record)

        if self._try_recover(obs, step, index, record, during="scan"):
            fresh = self.surface.observe()
            outcome = self._match_business_outcome(fresh)
            if outcome is not None:
                self._handle_business_outcome(outcome, fresh, step, index, record)
            return True

        for rule in self.artifact.failure_rules:
            verdict = evaluate(rule.when, obs)
            if verdict.ok:
                self.log.log("failure_detected", rule=rule.name, step_id=step.id,
                             detail=verdict.detail)
                raise _Terminal(
                    {
                        "status": ReplayStatus.failure,
                        "failure": FailureDetail(
                            error_class=rule.error_class,
                            message=rule.message,
                            step_id=step.id, step_index=index, step_intent=step.intent,
                            expected="the application to continue the flow normally",
                            observed=verdict.observed or verdict.detail,
                            observed_url=obs.url,
                            evidence=self._capture_failure_evidence(f"apperr_{step.id}"),
                        ),
                    }
                )
        return False

    def _match_business_outcome(self, obs: Observation):
        for rule in self.artifact.business_outcomes:
            if rule.active_from_step and rule.active_from_step not in self._reached_steps:
                continue
            if evaluate(rule.when, obs).ok:
                return rule
        return None

    def _handle_business_outcome(self, rule, obs: Observation, step: Step, index: int,
                                 record: StepRecord) -> bool:
        self.log.log("business_outcome_detected", outcome=rule.name, step_id=step.id,
                     disposition=rule.disposition.value, detail=rule.message)
        evidence = [
            self.evidence.screenshot(obs.screenshot, f"outcome_{rule.name}") or "",
            self.evidence.observation_snapshot(obs, f"outcome_{rule.name}"),
        ]
        evidence = [e for e in evidence if e]

        if rule.disposition is OutcomeDisposition.return_to_caller:
            raise _Terminal(
                {
                    "status": ReplayStatus.business_outcome,
                    "outcome": OutcomeDetail(
                        name=rule.name, message=rule.message, detected_at_step=step.id,
                        remediation=rule.remediation, evidence=evidence,
                    ),
                }
            )

        # escalate_to_human: a real outcome a person can clear on this session.
        self._escalate(
            reason_class=f"business_outcome:{rule.name}",
            reason=rule.message,
            step=step, index=index, obs=obs,
            suggested=[rule.remediation or "Clear the condition in the live browser window",
                       "Then press Release & resume"],
            outcome_name=rule.name,
        )
        record.notes.append(f"escalated on business outcome {rule.name!r}")
        return True

    # ------------------------------------------------------------- recovery

    def _try_recover(self, obs: Observation, step: Step, index: int, record: StepRecord,
                     *, during: str) -> bool:
        for rule in self.artifact.recovery_rules:
            if rule.name == self._suppressed_rule:
                continue
            if not evaluate(rule.when, obs).ok:
                continue
            used = self._recovery_counts.get(rule.name, 0)
            if used >= rule.max_attempts:
                self.log.log("recovery_exhausted", rule=rule.name, step_id=step.id,
                             attempts=used)
                continue
            self._recovery_counts[rule.name] = used + 1
            self.log.log("recovery_started", rule=rule.name, step_id=step.id,
                         attempt=used + 1, during=during, detail=rule.description)
            ok = self._apply_recovery(rule, step, index) if rule.actions else True
            entry = {
                "rule": rule.name, "step_id": step.id, "attempt": used + 1,
                "during": during, "applied": ok, "then": rule.then.value,
                "detail": rule.description,
            }
            self.recoveries.append(entry)
            record.notes.append(f"recovered via {rule.name!r} (attempt {used + 1})")
            if record.status == "ok":
                record.status = "recovered"
            self.log.log("recovery_finished", rule=rule.name, step_id=step.id, status=str(ok))
            if ok and rule.then is RecoveryThen.resume_from_step:
                raise _Rewind(rule.resume_from_step or step.id, rule.name)
            if ok:
                return True
        return False

    def _apply_recovery(self, rule: RecoveryRule, step: Step, index: int) -> bool:
        for action in rule.actions:
            if not self._apply_recovery_action(action, rule, step, index):
                return False
        return True

    def _apply_recovery_action(self, action: RecoveryAction, rule: RecoveryRule, step: Step,
                               index: int) -> bool:
        if action.action == "rerun_tagged_steps":
            for tagged in self.artifact.steps_tagged(action.tag or ""):
                self.log.log("recovery_rerun_step", rule=rule.name, step_id=tagged.id,
                             intent=tagged.intent)
                sub = self._run_step(tagged, self.artifact.steps.index(tagged))
                self.steps.append(sub)
            return True

        if action.action == "navigate":
            url = self._render(action.url or "")
            decision = self.gate.check(action=ActionKind.navigate, url=url)
            if not decision.allowed:
                self.log.log("recovery_blocked", rule=rule.name, reason=decision.reason)
                return False
            return self.surface.open(url).ok

        if action.action == "wait":
            self.surface.wait_ms(action.wait.settle_ms or 500)
            return True

        obs = self.surface.observe(screenshot=False)
        if action.target is None:
            return False
        resolution = resolve(action.target, obs, supported=self.surface.capabilities())
        if not resolution.ok:
            self.log.log("recovery_target_missing", rule=rule.name,
                         target=action.target.description)
            return False

        decision = self.gate.check(
            action=ActionKind.click if action.action == "click" else ActionKind.type,
            control_label=resolution.node.name,
        )
        if not decision.allowed:
            self.log.log("recovery_blocked", rule=rule.name, reason=decision.reason)
            return False

        x, y = resolution.node.rect.center
        if action.action == "click":
            return self.surface.click_point(x, y).ok
        value = render_value(action.value, self._inputs, self.config)
        self.surface.click_point(x, y)
        self.surface.clear_focused()
        return self.surface.type_text(value).ok

    def _handle_failed_checkpoint(self, step: Step, index: int, record: StepRecord,
                                  obs: Observation, verdict: Verdict, attempt: int) -> StepRecord:
        if step.optional:
            record.status = "skipped"
            record.notes.append(f"optional step skipped: {verdict.detail}")
            return record

        # The scan may reclassify this as a business outcome or a hard failure,
        # or a human may have just cleared the block — so re-check before failing.
        if self._scan_conditions(obs, step, index, record):
            obs = self.surface.observe()
            recheck = evaluate(step.checkpoint, obs)
            if recheck.ok:
                record.checkpoint = recheck.detail
                record.status = "recovered" if record.status == "ok" else record.status
                self.log.log("checkpoint", step_id=step.id, status="pass",
                             detail=f"satisfied after intervention: {recheck.detail}")
                return record

        if attempt == 1 and self._try_recover(obs, step, index, record, during="checkpoint"):
            return self._run_step(step, index, attempt=attempt + 1)

        if attempt == 1:
            self.log.log("step_retry", step_id=step.id, reason=verdict.detail)
            record.notes.append("retried once after checkpoint failure")
            return self._run_step(step, index, attempt=attempt + 1)

        raise _Terminal(
            {
                "status": ReplayStatus.failure,
                "failure": FailureDetail(
                    error_class="checkpoint_failed",
                    message=f"step {step.id!r} did not reach its expected state: {verdict.detail}",
                    step_id=step.id, step_index=index, step_intent=step.intent,
                    expected=step.checkpoint.description or verdict.detail,
                    observed=verdict.observed or obs.text[:240],
                    observed_url=obs.url,
                    recovery_attempts=[r["rule"] for r in self.recoveries],
                    evidence=self._capture_failure_evidence(f"checkpoint_{step.id}"),
                ),
            }
        )

    # ----------------------------------------------------------- escalation

    def _escalate(self, *, reason_class: str, reason: str, step: Step, index: int,
                  obs: Observation, suggested: list[str], outcome_name: str | None = None) -> None:
        if len(self.escalations) >= self.MAX_ESCALATIONS:
            raise _Terminal(
                {
                    "status": ReplayStatus.failure,
                    "failure": FailureDetail(
                        error_class="escalation_limit",
                        message=f"run escalated {len(self.escalations)} times without resolving",
                        step_id=step.id, step_index=index, step_intent=step.intent,
                        expected="a human to clear the blocking condition",
                        observed=reason,
                        evidence=self._capture_failure_evidence(f"esc_limit_{step.id}"),
                    ),
                }
            )

        shot = self.evidence.screenshot(obs.screenshot or self.surface.screenshot(),
                                        f"intervention_{step.id}")
        dom = self.evidence.dom_snapshot(self.surface.dom_snapshot(), f"intervention_{step.id}")
        request = InterventionRequest(
            run_id=self.run_id, phase="replay",
            capability_id=self.artifact.capability_id,
            capability_version=self.artifact.version,
            goal=self.artifact.description,
            step_id=step.id, step_index=index, step_intent=step.intent,
            reason_class=reason_class, reason=self.redactor.scrub_text(reason),
            observed_url=obs.url, observed_title=obs.title,
            observed_excerpt=self.redactor.scrub_text(obs.text[:600]),
            screenshot_path=shot, dom_snapshot_path=dom,
            suggested_actions=suggested,
            resume_hint=f"automation will retry step {step.id!r} ({step.intent}) after release",
        )
        paths = request.write(extra_dir=self.evidence.root)
        self.control.pause_for_human(reason, request.id, step.id)
        self.log.log("escalation_requested", intervention_id=request.id,
                     reason_class=reason_class, reason=request.reason, step_id=step.id,
                     owner=self.control.owner.value, paths=paths)
        for line in request.summary_lines():
            self.log.log("intervention_context", detail=line)

        if self.operator is None:
            raise _Terminal(
                {
                    "status": ReplayStatus.failure,
                    "failure": FailureDetail(
                        error_class="escalation_unattended",
                        message=f"intervention {request.id} was raised but no operator channel is "
                                "configured for this run",
                        step_id=step.id, step_index=index, step_intent=step.intent,
                        expected="an operator to take control of the live session",
                        observed=reason,
                        evidence=[p for p in (shot, dom) if p],
                    ),
                }
            )

        before = len(self.control.human_actions)
        resolution = self.operator.handle(
            request, control=self.control, surface=self.surface,
            recorder=self.recorder, logger=self.log, evidence=self.evidence,
        )
        performed = [a.as_dict() for a in self.control.human_actions[before:]]
        request.status = "resolved" if resolution == "resumed" else "aborted"
        request.resolution = resolution
        request.operator = getattr(self.operator, "name", "operator")
        request.write(extra_dir=self.evidence.root)

        self.escalations.append(
            {
                "intervention_id": request.id, "reason_class": reason_class, "reason": request.reason,
                "step_id": step.id, "operator": request.operator, "resolution": resolution,
                "outcome_name": outcome_name, "human_actions": performed,
                "screenshot": shot, "dom_snapshot": dom,
            }
        )
        self.log.log("escalation_resolved", intervention_id=request.id, status=resolution,
                     human_actions=len(performed), owner=self.control.owner.value)

        if resolution != "resumed":
            raise _Terminal(
                {
                    "status": ReplayStatus.failure,
                    "failure": FailureDetail(
                        error_class="escalation_unresolved",
                        message=f"operator did not return control ({resolution})",
                        step_id=step.id, step_index=index, step_intent=step.intent,
                        expected="a human to clear the blocking condition and release control",
                        observed=resolution,
                        evidence=[p for p in (shot, dom) if p],
                    ),
                }
            )

        self.control.resumed()
        self.log.log("control_resumed", owner=self.control.owner.value, step_id=step.id)

    def _after_escalation(self, step: Step, index: int, record: StepRecord,
                          attempt: int) -> StepRecord:
        """After a human clears the block, check before repeating the work.

        The human may have already produced the state the step was trying to
        reach — they signed the override off, and the record is now on screen.
        Blindly re-running the step would look for a control that is no longer
        there, so the checkpoint is re-verified first.
        """
        if step.checkpoint is not None:
            obs = self.surface.observe()
            verdict = evaluate(step.checkpoint, obs)
            if verdict.ok:
                record.checkpoint = verdict.detail
                record.notes.append("step satisfied by the operator's manual action")
                self.log.log("checkpoint", step_id=step.id, status="pass",
                             detail=f"satisfied by human intervention: {verdict.detail}")
                return record
        if attempt > 2:
            raise _Terminal(
                {
                    "status": ReplayStatus.failure,
                    "failure": FailureDetail(
                        error_class="escalation_did_not_unblock",
                        message=f"step {step.id!r} is still blocked after human intervention",
                        step_id=step.id, step_index=index, step_intent=step.intent,
                        expected="the blocking condition to be cleared",
                        observed="the same condition is still present",
                        evidence=self._capture_failure_evidence(f"still_blocked_{step.id}"),
                    ),
                }
            )
        retried = self._run_step(step, index, attempt=attempt + 1)
        retried.notes = record.notes + retried.notes
        if retried.status == "ok":
            retried.status = "escalated"
        return retried

    # --------------------------------------------------------------- outputs

    def _extract_outputs(self) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        if not self.artifact.outputs:
            return outputs
        obs = self.surface.observe()
        for spec in self.artifact.outputs:
            resolution = resolve(spec.extract.target, obs, supported=self.surface.capabilities())
            if not resolution.ok:
                if not spec.required:
                    self.log.log("extraction_skipped", output=spec.name,
                                 reason="optional output not present")
                    continue
                raise _Terminal(
                    {
                        "status": ReplayStatus.failure,
                        "failure": FailureDetail(
                            error_class="output_not_found",
                            message=f"declared output {spec.name!r} could not be located",
                            expected=f"a control matching {spec.extract.target.description}",
                            observed=f"{len(obs.nodes)} controls perceived; none matched",
                            observed_url=obs.url,
                            locator_attempts=resolution.attempts,
                            evidence=self._capture_failure_evidence(f"output_{spec.name}"),
                        ),
                    }
                )
            raw = self.surface.read_node(resolution.node, spec.extract.attribute) or resolution.node.text
            try:
                value = coerce_output(spec, raw)
            except ValueError as exc:
                raise _Terminal(
                    {
                        "status": ReplayStatus.failure,
                        "failure": FailureDetail(
                            error_class="output_type_mismatch",
                            message=str(exc),
                            expected=f"{spec.name} readable as {spec.type.value}",
                            observed=self.redactor.scrub_text(raw),
                            observed_url=obs.url,
                            evidence=self._capture_failure_evidence(f"outtype_{spec.name}"),
                        ),
                    }
                ) from exc
            outputs[spec.name] = value
            if spec.sensitivity.value in {"pii", "secret"}:
                self.redactor.register(str(value), f"output:{spec.name}")
            self.log.log("extraction", output=spec.name, type=spec.type.value,
                         strategy=resolution.strategy.kind.value if resolution.strategy else None,
                         detail=self.redactor.scrub_text(str(value)))
        return outputs

    def _verify_success(self) -> None:
        obs = self.surface.observe()
        verdict = evaluate(self.artifact.success_condition, obs)
        self.log.log("success_condition", status="pass" if verdict.ok else "fail",
                     detail=verdict.detail)
        self.evidence.screenshot(obs.screenshot, "final_state")
        self.evidence.observation_snapshot(obs, "final_state")
        if not verdict.ok:
            raise _Terminal(
                {
                    "status": ReplayStatus.failure,
                    "failure": FailureDetail(
                        error_class="success_condition_failed",
                        message=f"capability finished but its success condition did not hold: "
                                f"{verdict.detail}",
                        expected=self.artifact.success_condition.description or verdict.detail,
                        observed=verdict.observed or obs.text[:240],
                        observed_url=obs.url,
                        evidence=self._capture_failure_evidence("success_condition"),
                    ),
                }
            )

    # -------------------------------------------------------------- evidence

    def _capture_failure_evidence(self, label: str) -> list[str]:
        """On failure, capture more than the log line: pixels, markup, and the tree."""
        out: list[str] = []
        try:
            obs = self.surface.observe()
            out.append(self.evidence.screenshot(obs.screenshot, f"FAIL_{label}") or "")
            out.append(self.evidence.observation_snapshot(obs, f"FAIL_{label}"))
            out.append(self.evidence.dom_snapshot(self.surface.dom_snapshot(), f"FAIL_{label}"))
        except Exception as exc:  # pragma: no cover
            self.log.log("evidence_capture_failed", detail=str(exc))
        return [o for o in out if o]
