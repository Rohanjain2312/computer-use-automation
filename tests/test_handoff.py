from __future__ import annotations

import json
import urllib.request

import pytest

from cua.handoff.console import OperatorConsole
from cua.handoff.control import (
    ControlOwner,
    ControlViolation,
    HumanAction,
    SessionControl,
    SessionState,
)
from cua.handoff.intervention import InterventionRequest


def test_automation_owns_the_session_until_it_pauses():
    control = SessionControl(run_id="r1")
    assert control.can_act() and control.owner is ControlOwner.automation
    control.assert_can_act()


def test_automation_cannot_act_while_a_human_holds_control():
    """The invariant is enforced by the control object, not by convention."""
    control = SessionControl(run_id="r1")
    control.pause_for_human("blocked", "int_1", "s06")
    assert not control.can_act()
    control.grant_human_control("alice")
    with pytest.raises(ControlViolation, match="owner=human"):
        control.assert_can_act("click")


def test_control_can_only_be_granted_from_a_paused_state():
    control = SessionControl(run_id="r1")
    with pytest.raises(ControlViolation):
        control.grant_human_control("alice")


def test_control_cannot_be_released_by_someone_who_does_not_hold_it():
    control = SessionControl(run_id="r1")
    control.pause_for_human("blocked", "int_1", "s06")
    with pytest.raises(ControlViolation):
        control.release_to_automation()


def test_the_full_transfer_cycle_is_recorded_in_order():
    control = SessionControl(run_id="r1")
    control.pause_for_human("needs a supervisor", "int_1", "s06")
    control.grant_human_control("alice")
    control.record_human_action(HumanAction(at="t", kind="click",
                                            description="clicked 'Apply Override'"))
    control.release_to_automation("override applied", "alice")
    control.resumed()
    control.complete()

    kinds = [e.kind for e in control.history]
    assert kinds == ["session_opened", "automation_paused", "control_granted",
                     "control_released", "automation_resumed", "session_closed"]
    assert control.owner is ControlOwner.automation
    assert control.state is SessionState.completed
    assert control.as_dict()["human_actions"][0]["description"] == "clicked 'Apply Override'"


def test_resume_target_is_remembered_across_the_handoff():
    control = SessionControl(run_id="r1")
    control.pause_for_human("blocked", "int_1", "s06_search")
    assert control.resume_from_step == "s06_search"


def test_intervention_request_carries_what_a_human_needs(tmp_path):
    request = InterventionRequest(
        run_id="r1", capability_id="cap", capability_version="1.0.0", goal="look up a member",
        step_id="s06", step_index=6, step_intent="run the member inquiry",
        reason_class="business_outcome:permission_denied",
        reason="the operator is not entitled to view this member",
        observed_url="http://h/members/100999", observed_title="Access Restricted",
        screenshot_path="evidence/replay/r1/screens/007.png",
        suggested_actions=["Apply a supervisor override"],
        resume_hint="automation retries step s06 after release")
    paths = request.write(extra_dir=tmp_path)
    written = json.loads((tmp_path / f"{request.id}.json").read_text())
    assert written["goal"] and written["step_intent"] and written["reason"]
    assert written["screenshot_path"] and written["status"] == "open"
    assert len(paths) == 2
    summary = "\n".join(request.summary_lines())
    assert "s06" in summary and "screenshot" in summary


def test_console_serves_state_and_accepts_commands_without_touching_the_browser():
    control = SessionControl(run_id="r1")
    console = OperatorConsole(control, port=0)
    url = console.start()
    try:
        console.publish(b"\x89PNG-not-really", {"goal": "look up a member", "step_id": "s06"})
        state = json.loads(urllib.request.urlopen(url + "state.json", timeout=5).read())
        assert state["control"]["owner"] == "automation"
        assert state["request"]["goal"] == "look up a member"

        urllib.request.urlopen(urllib.request.Request(url + "take", method="POST"), timeout=5)
        assert console.poll_command() == "take"
        assert console.poll_command() is None

        page = urllib.request.urlopen(url, timeout=5).read().decode()
        assert "Take control" in page and "Release" in page
    finally:
        console.stop()


# -- the operator console, without a browser -----------------------------


class _FakeRecorder:
    """Stands in for the in-page recorder; hands back actions once installed."""

    def __init__(self, actions):
        self.actor = "operator"
        self.simulated = True
        self.installed = False
        self._pending = list(actions)

    def install(self):
        self.installed = True

    def drain(self):
        if not self.installed:
            return []
        out, self._pending = self._pending, []
        return out


class _FakeSurface:
    def screenshot(self, **kw):
        return b""

    def is_alive(self):
        return True


class _FakeEvidence:
    def screenshot(self, *a, **kw):
        return None


def _operator_fixtures(tmp_path, actions, commands):
    from cua.observability.logging import RunLogger
    from cua.safety.redact import Redactor

    control = SessionControl(run_id="r1")
    control.pause_for_human("entitlement block", "int_1", "s06")
    console = OperatorConsole(control, port=0)
    for c in commands:
        console.commands.put(c)
    recorder = _FakeRecorder(actions)
    logger = RunLogger(tmp_path / "run.jsonl", run_id="r1", phase="replay",
                       redactor=Redactor(), echo=False)
    request = InterventionRequest(run_id="r1", capability_id="cap", goal="g",
                                  step_id="s06", step_index=6, step_intent="run the inquiry",
                                  reason="restricted")
    return control, console, recorder, logger, request


def test_console_operator_records_actions_taken_before_the_takeover_button(tmp_path):
    """A person who fixes the problem first and presses the button after.

    The recorder used to be installed by "Take control", so anything done before
    pressing it vanished — and the run still succeeded, making the loss silent.
    """
    from cua.handoff.operators import ConsoleOperator

    did = HumanAction(at="t", kind="click", description="clicked the 'Apply Override' button",
                      actor="operator", simulated=False)
    control, console, recorder, logger, request = _operator_fixtures(
        tmp_path, [did], commands=["release"])

    result = ConsoleOperator(console, poll_s=0.01).handle(
        request, control=control, surface=_FakeSurface(), recorder=recorder,
        logger=logger, evidence=_FakeEvidence())
    logger.close()

    assert recorder.installed, "the recorder must be live as soon as the session pauses"
    assert result == "resumed"
    assert [a.description for a in control.human_actions] == [did.description]
    assert control.human_actions[0].simulated is False


def test_console_operator_normal_order_still_transfers_control(tmp_path):
    from cua.handoff.control import ControlOwner
    from cua.handoff.operators import ConsoleOperator

    did = HumanAction(at="t", kind="click", description="clicked the 'Apply Override' button",
                      actor="operator", simulated=False)
    control, console, recorder, logger, request = _operator_fixtures(
        tmp_path, [did], commands=["take", "release"])

    result = ConsoleOperator(console, poll_s=0.01).handle(
        request, control=control, surface=_FakeSurface(), recorder=recorder,
        logger=logger, evidence=_FakeEvidence())
    logger.close()

    assert result == "resumed"
    kinds = [e.kind for e in control.history]
    assert "control_granted" in kinds and "control_released" in kinds
    assert control.owner is ControlOwner.automation
    assert len(control.human_actions) == 1


def test_console_operator_abort_stops_the_run(tmp_path):
    from cua.handoff.operators import ConsoleOperator

    control, console, recorder, logger, request = _operator_fixtures(
        tmp_path, [], commands=["abort"])
    result = ConsoleOperator(console, poll_s=0.01).handle(
        request, control=control, surface=_FakeSurface(), recorder=recorder,
        logger=logger, evidence=_FakeEvidence())
    logger.close()
    assert result == "aborted"
    assert control.state is SessionState.aborted


def test_console_page_states_the_required_order(tmp_path):
    """The page must say to act in the automation's window, in order."""
    control = SessionControl(run_id="r1")
    console = OperatorConsole(control, port=0)
    url = console.start()
    try:
        page = urllib.request.urlopen(url, timeout=5).read().decode()
        assert "Take control" in page and "Release" in page
        assert "What to do" in page
        assert "browser window" in page
    finally:
        console.stop()


def test_closing_the_window_mid_handoff_is_reported_as_a_closed_session(tmp_path):
    """Closing the browser ends the shared session; say that, not TargetClosedError."""
    from cua.handoff.operators import ConsoleOperator

    class _DeadSurface(_FakeSurface):
        def is_alive(self):
            return False

    control, console, recorder, logger, request = _operator_fixtures(
        tmp_path, [], commands=[])
    result = ConsoleOperator(console, poll_s=0.01, timeout_s=2).handle(
        request, control=control, surface=_DeadSurface(), recorder=recorder,
        logger=logger, evidence=_FakeEvidence())
    logger.close()
    assert result == "session_closed"
    assert any(e["event"] == "session_closed_during_handoff" for e in logger.events)


def test_a_closed_session_error_is_classified_not_swallowed():
    from cua.replay.engine import _is_session_closed

    class TargetClosedError(Exception):
        pass

    assert _is_session_closed(TargetClosedError("Page.wait_for_timeout: Target page, "
                                                "context or browser has been closed"))
    assert _is_session_closed(RuntimeError("Browser closed unexpectedly"))
    assert not _is_session_closed(ValueError("member_id must be numeric"))
