"""Who answers an intervention request.

Two implementations behind one interface, and the interface is the point: the
engine pauses, hands over an ``InterventionRequest``, and waits for control to
come back. It does not know or care whether a person or a script cleared the
block.

``ConsoleOperator`` is the real path: a person watches the console, clicks in the
*same* browser window the automation is driving, and releases control.

``ScriptedOperator`` is a deliberate stand-in so the handoff can be demonstrated
in CI and in headless evidence runs. It is not a shortcut around the seam — it
takes control, acts on the same live session, has its actions captured by the
same in-page recorder, and releases control through the same state machine.
Everything it records is tagged ``simulated: true`` so evidence never overstates
what happened.
"""

from __future__ import annotations

import os
import time
from typing import Any

from ..locate.strategies import resolve
from ..observability.evidence import EvidenceWriter
from ..observability.logging import RunLogger
from ..surface.web import PlaywrightWebSurface
from ..artifact.schema import StrategyKind, TargetPlan, TargetStrategy
from .console import OperatorConsole
from .control import ControlOwner, SessionControl
from .intervention import InterventionRequest
from .recorder import HumanActionRecorder


def _plan(role: str, name: str, description: str) -> TargetPlan:
    return TargetPlan(
        description=description,
        strategies=[
            TargetStrategy(kind=StrategyKind.role_name, params={"role": role, "name": name},
                           confidence=0.95, rationale="operator-supplied control reference"),
            TargetStrategy(kind=StrategyKind.label_proximity, params={"role": role, "label": name},
                           confidence=0.8, rationale="legacy label-to-the-left fallback"),
            TargetStrategy(kind=StrategyKind.text_anchor, params={"text": name, "match": "contains"},
                           confidence=0.6, rationale="visible-text fallback"),
        ],
        require_unique=False,
    )


class ScriptedOperator:
    """A simulated operator that clears the block on the live session."""

    name = "scripted-operator"

    def __init__(self, script: list[dict[str, Any]], *, actor: str = "sim.supervisor",
                 note: str = "manual remediation complete") -> None:
        self.script = script
        self.actor = actor
        self.note = note

    def handle(
        self,
        request: InterventionRequest,
        *,
        control: SessionControl,
        surface: PlaywrightWebSurface,
        recorder: HumanActionRecorder,
        logger: RunLogger,
        evidence: EvidenceWriter,
    ) -> str:
        logger.log("operator_engaged", operator=self.name, intervention_id=request.id,
                   simulated=True, actor=self.actor)
        control.grant_human_control(self.actor)
        recorder.actor = self.actor
        recorder.simulated = True
        recorder.install()
        logger.log("control_transferred", owner=control.owner.value, actor=self.actor,
                   detail="human now owns the live session; automation is paused")
        evidence.screenshot(surface.screenshot(), f"handoff_{request.id}_before_human")

        for entry in self.script:
            self._perform(entry, surface, logger)
            for action in recorder.drain():
                control.record_human_action(action)
                logger.log("human_action", detail=action.description, actor=action.actor,
                           simulated=action.simulated)

        for action in recorder.drain():
            control.record_human_action(action)
            logger.log("human_action", detail=action.description, actor=action.actor,
                       simulated=action.simulated)

        evidence.screenshot(surface.screenshot(), f"handoff_{request.id}_after_human")
        control.release_to_automation(self.note, self.actor)
        logger.log("control_transferred", owner=control.owner.value, actor=self.actor,
                   detail=self.note)
        return "resumed"

    def _perform(self, entry: dict[str, Any], surface: PlaywrightWebSurface,
                 logger: RunLogger) -> None:
        do = entry.get("do")
        if do == "wait":
            surface.wait_ms(int(entry.get("ms", 400)))
            return

        obs = surface.observe(screenshot=False)
        plan = _plan(entry.get("role", "button"), entry["name"],
                     f"the {entry['name']!r} control (operator step)")
        found = resolve(plan, obs, supported=surface.capabilities())
        if not found.ok:
            logger.log("human_action_failed", detail=f"operator could not find {entry['name']!r}")
            return
        x, y = found.node.rect.center
        if do == "click":
            surface.click_point(x, y)
        elif do == "type":
            value = os.environ.get(entry["env"], "") if entry.get("env") else entry.get("value", "")
            surface.click_point(x, y)
            surface.clear_focused()
            surface.type_text(value)
        surface.wait_ms(int(entry.get("settle_ms", 350)))


class ConsoleOperator:
    """A real person, on the same live session, via the operator console."""

    name = "console-operator"

    def __init__(self, console: OperatorConsole, *, timeout_s: float = 900.0,
                 poll_s: float = 0.5, actor: str = "operator") -> None:
        self.console = console
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self.actor = actor

    def handle(
        self,
        request: InterventionRequest,
        *,
        control: SessionControl,
        surface: PlaywrightWebSurface,
        recorder: HumanActionRecorder,
        logger: RunLogger,
        evidence: EvidenceWriter,
    ) -> str:
        self.console.publish(surface.screenshot(), request.as_dict())
        logger.log("operator_engaged", operator=self.name, intervention_id=request.id,
                   console_url=self.console.url)
        print("\n" + "=" * 74)
        print(f"  HUMAN INTERVENTION REQUIRED  —  {self.console.url}")
        for line in request.summary_lines():
            print("  " + line)
        print("  Act in the browser window the automation is using, then Release & resume.")
        print("=" * 74 + "\n", flush=True)

        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            command = self.console.poll_command()
            if command == "take" and control.owner is ControlOwner.automation:
                control.grant_human_control(self.actor)
                recorder.actor = self.actor
                recorder.simulated = False
                recorder.install()
                logger.log("control_transferred", owner=control.owner.value, actor=self.actor,
                           detail="human took control of the live session")
                evidence.screenshot(surface.screenshot(), f"handoff_{request.id}_before_human")
            elif command == "release" and control.owner is ControlOwner.human:
                for action in recorder.drain():
                    control.record_human_action(action)
                    logger.log("human_action", detail=action.description, actor=action.actor)
                evidence.screenshot(surface.screenshot(), f"handoff_{request.id}_after_human")
                control.release_to_automation("operator released control", self.actor)
                logger.log("control_transferred", owner=control.owner.value, actor=self.actor,
                           detail="control returned to automation")
                return "resumed"
            elif command == "abort":
                control.abort("operator aborted the run", self.actor)
                logger.log("run_aborted_by_operator", actor=self.actor)
                return "aborted"

            if control.owner is ControlOwner.human:
                for action in recorder.drain():
                    control.record_human_action(action)
                    logger.log("human_action", detail=action.description, actor=action.actor)

            self.console.publish(surface.screenshot(), request.as_dict())
            time.sleep(self.poll_s)

        logger.log("operator_timeout", operator=self.name, intervention_id=request.id,
                   timeout_s=self.timeout_s)
        return "unavailable"
