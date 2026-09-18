"""End-to-end replay against the live mock console.

These are the tests that matter: they drive a real browser against a real
frameset application and assert on the structured result contract, including the
three runtime-condition categories and the same-session human handoff.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from cua.artifact.schema import ArtifactStatus
from cua.handoff.control import ControlOwner, SessionState
from cua.handoff.operators import ScriptedOperator
from cua.observability.evidence import EvidenceWriter
from cua.observability.logging import RunLogger
from cua.replay.engine import ReplayEngine
from cua.replay.result import ReplayStatus
from cua.safety.policy import PolicyGate
from cua.safety.redact import Redactor
from cua.handoff.control import SessionControl
from fixture_artifact import build_artifact

pytestmark = pytest.mark.browser

OVERRIDE_SCRIPT = [
    {"do": "click", "role": "button", "name": "Supervisor Override"},
    {"do": "type", "role": "textbox", "name": "Override Code", "env": "MERIDIAN_OVERRIDE_CODE"},
    {"do": "click", "role": "button", "name": "Apply Override"},
]


def run_replay(surface, allowlist, mock_app, tmp_path, inputs, *, operator=None, artifact=None):
    artifact = artifact or build_artifact()
    redactor = Redactor()
    run_id = "test_" + tmp_path.name
    evidence = EvidenceWriter("replay", run_id, redactor, root=tmp_path / "evidence")
    logger = RunLogger(evidence.log_path, run_id=run_id, phase="replay", redactor=redactor,
                       echo=False)
    control = SessionControl(run_id=run_id)
    engine = ReplayEngine(artifact, surface, PolicyGate(allowlist), redactor=redactor,
                          logger=logger, evidence=evidence, control=control, operator=operator,
                          config={"base_url": mock_app}, run_id=run_id)
    result = engine.run(inputs)
    logger.close()
    return result, control, evidence


def test_happy_path_returns_typed_outputs(surface, allowlist, mock_app, tmp_path, reset_faults):
    result, control, evidence = run_replay(surface, allowlist, mock_app, tmp_path,
                                           {"member_id": "100244"})

    assert result.status is ReplayStatus.success, result.summary_line()
    assert result.outputs["current_savings_balance"] == Decimal("4812.37")
    assert isinstance(result.outputs["current_savings_balance"], Decimal)
    assert result.outputs["savings_account_status"] == "OPEN"
    assert result.outputs["member_name"] == "Dana Whitfield"
    assert result.llm_used is False
    assert control.state is SessionState.completed

    # The one-per-sign-on interstitial must have been cleared by a recovery rule,
    # not by a hardcoded step.
    assert any(r["rule"] == "acknowledge_system_notice" for r in result.recoveries)

    # Every step that changed the screen proved it with a checkpoint.
    verified = [s for s in result.steps if s.checkpoint]
    assert len(verified) >= 4
    assert all(s.status in {"ok", "recovered"} for s in result.steps)

    written = json.loads((evidence.root / "result.json").read_text())
    assert written["status"] == "success"

    # PII is returned to the caller but redacted on *every* persistence path,
    # not just in result.json — the run log, the snapshots and any intervention
    # record go through the same redactor. Checking only result.json would pass
    # while the DOM snapshot beside it still named the member.
    leaked = [
        path.relative_to(evidence.root)
        for path in evidence.root.rglob("*")
        if path.is_file()
        and path.suffix != ".png"
        and "Dana Whitfield" in path.read_text(errors="ignore")
    ]
    assert not leaked, f"declared pii value written unredacted to: {leaked}"


def test_member_not_found_is_a_business_outcome_not_a_failure(
    surface, allowlist, mock_app, tmp_path, reset_faults
):
    result, _, _ = run_replay(surface, allowlist, mock_app, tmp_path, {"member_id": "999999"})

    assert result.status is ReplayStatus.business_outcome
    assert result.outcome is not None
    assert result.outcome.name == "member_not_found"
    assert result.failure is None
    assert result.outcome.evidence


def test_invalid_input_is_rejected_before_touching_the_application(
    surface, allowlist, mock_app, tmp_path, reset_faults
):
    result, _, _ = run_replay(surface, allowlist, mock_app, tmp_path, {"member_id": "not-a-number"})

    assert result.status is ReplayStatus.invalid_input
    assert result.failure.error_class == "invalid_input"
    assert result.steps == []


def test_missing_required_input_is_rejected(surface, allowlist, mock_app, tmp_path, reset_faults):
    result, _, _ = run_replay(surface, allowlist, mock_app, tmp_path, {})
    assert result.status is ReplayStatus.invalid_input
    assert "member_id" in result.failure.message


def test_application_error_is_a_hard_failure_with_debuggable_detail(
    surface, allowlist, mock_app, tmp_path, reset_faults
):
    reset_faults("app_error")
    result, _, _ = run_replay(surface, allowlist, mock_app, tmp_path, {"member_id": "100244"})

    assert result.status is ReplayStatus.failure
    assert result.failure.error_class == "application_error"
    assert result.failure.step_id
    assert result.failure.expected and result.failure.observed
    assert any(p.endswith(".png") for p in result.failure.evidence)
    assert any(p.endswith(".dom.html") for p in result.failure.evidence)


def test_permission_denied_escalates_and_resumes_on_the_same_session(
    surface, allowlist, mock_app, tmp_path, reset_faults
):
    operator = ScriptedOperator(OVERRIDE_SCRIPT)
    result, control, _ = run_replay(surface, allowlist, mock_app, tmp_path,
                                    {"member_id": "100999"}, operator=operator)

    assert result.status is ReplayStatus.success, result.summary_line()
    assert result.outputs["current_savings_balance"] == Decimal("27640.18")

    # One escalation, resolved by the operator, resumed on the same session.
    assert len(result.escalations) == 1
    escalation = result.escalations[0]
    assert escalation["reason_class"] == "business_outcome:permission_denied"
    assert escalation["resolution"] == "resumed"

    # The human's actions were observed, not assumed.
    descriptions = [a["description"] for a in result.human_actions]
    assert any("Supervisor Override" in d for d in descriptions)
    assert any("Apply Override" in d for d in descriptions)
    assert all(a["simulated"] for a in result.human_actions)

    # Control ownership is explicit and ends back with automation.
    kinds = [e["kind"] for e in result.control_log]
    assert kinds.index("automation_paused") < kinds.index("control_granted")
    assert kinds.index("control_granted") < kinds.index("control_released")
    assert "automation_resumed" in kinds
    assert control.owner is ControlOwner.automation

    # The override code must not appear anywhere that was written to disk.
    assert "OVR-4417" not in json.dumps(result.to_dict(), default=str)


def test_escalation_without_an_operator_fails_loudly(
    surface, allowlist, mock_app, tmp_path, reset_faults
):
    result, control, _ = run_replay(surface, allowlist, mock_app, tmp_path,
                                    {"member_id": "100999"}, operator=None)

    assert result.status is ReplayStatus.failure
    assert result.failure.error_class == "escalation_unattended"
    assert control.state is SessionState.failed


def test_session_expiry_is_recovered_by_reauthenticating(
    surface, allowlist, mock_app, tmp_path, reset_faults
):
    """A session that dies mid-flow is a recoverable condition, not a failure."""
    artifact = build_artifact()
    redactor = Redactor()
    run_id = "test_expiry_" + tmp_path.name
    evidence = EvidenceWriter("replay", run_id, redactor, root=tmp_path / "evidence")
    logger = RunLogger(evidence.log_path, run_id=run_id, phase="replay", redactor=redactor,
                       echo=False)
    control = SessionControl(run_id=run_id)
    engine = ReplayEngine(artifact, surface, PolicyGate(allowlist), redactor=redactor,
                          logger=logger, evidence=evidence, control=control,
                          config={"base_url": mock_app}, run_id=run_id)

    # Expire the session exactly once, just before the member inquiry runs.
    original = engine._run_step
    fired = {"done": False}

    def patched(step, index, *, attempt=1):
        if step.id == "s05_member_number" and not fired["done"]:
            fired["done"] = True
            reset_faults("expire")
        return original(step, index, attempt=attempt)

    engine._run_step = patched
    result = engine.run({"member_id": "100244"})
    logger.close()

    assert fired["done"]
    assert result.status is ReplayStatus.success, result.summary_line()
    assert any(r["rule"] == "reauthenticate_after_session_expiry" for r in result.recoveries)
    assert result.outputs["current_savings_balance"] == Decimal("4812.37")


def test_allowlist_blocks_a_step_that_leaves_the_permitted_origin(
    surface, allowlist, mock_app, tmp_path, reset_faults
):
    artifact = build_artifact()
    steps = list(artifact.steps)
    steps[0] = steps[0].model_copy(update={"url": "https://example.com/"})
    artifact = artifact.model_copy(update={"steps": steps, "checksum": None}).with_checksum()

    result, _, _ = run_replay(surface, allowlist, mock_app, tmp_path, {"member_id": "100244"},
                              artifact=artifact)
    assert result.status is ReplayStatus.blocked_by_policy
    assert result.failure.error_class == "policy_allowlist"


def test_locator_falls_back_and_records_the_degradation(
    surface, allowlist, mock_app, tmp_path, reset_faults
):
    """A broken primary strategy degrades to a fallback and says so."""
    artifact = build_artifact()
    steps = list(artifact.steps)
    search = steps[6]
    broken = search.target.strategies[0].model_copy(
        update={"params": {"role": "button", "name": "Find Member"}})
    target = search.target.model_copy(
        update={"strategies": [broken] + list(search.target.strategies[1:])})
    steps[6] = search.model_copy(update={"target": target})
    artifact = artifact.model_copy(update={"steps": steps, "checksum": None}).with_checksum()

    result, _, _ = run_replay(surface, allowlist, mock_app, tmp_path, {"member_id": "100244"},
                              artifact=artifact)
    assert result.status is ReplayStatus.success
    degraded = [s for s in result.steps if s.strategy_degraded]
    assert degraded and degraded[0].id == "s06_search"
    assert degraded[0].strategy_used != "role_name"
