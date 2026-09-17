"""Explicit ownership of one live surface session.

The whole handoff design rests on one invariant: there is exactly one live
session (one browser context, one page, one set of cookies), and at any instant
exactly one party owns it. The session object is never torn down and rebuilt for
the human; only the *owner* changes.

``assert_can_act`` is called by the engine before every single action, so
"automation stopped acting while the human was in control" is enforced by the
control object rather than by discipline.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ControlOwner(str, Enum):
    automation = "automation"
    human = "human"


class SessionState(str, Enum):
    running = "running"
    paused_pending_human = "paused_pending_human"
    human_control = "human_control"
    resuming = "resuming"
    completed = "completed"
    failed = "failed"
    aborted = "aborted"


class ControlViolation(RuntimeError):
    """Raised if automation tries to act while it does not own the session."""


@dataclass
class ControlEvent:
    at: str
    kind: str
    from_owner: str | None
    to_owner: str | None
    state: str
    reason: str
    actor: str = "system"

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "kind": self.kind,
            "from_owner": self.from_owner,
            "to_owner": self.to_owner,
            "state": self.state,
            "reason": self.reason,
            "actor": self.actor,
        }


@dataclass
class HumanAction:
    """One thing the human did to the live session, as observed, not as claimed."""

    at: str
    kind: str
    description: str
    frame_url: str = ""
    value: str | None = None
    coordinates: list[float] | None = None
    actor: str = "human"
    simulated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "kind": self.kind,
            "description": self.description,
            "frame_url": self.frame_url,
            "value": self.value,
            "coordinates": self.coordinates,
            "actor": self.actor,
            "simulated": self.simulated,
        }


@dataclass
class SessionControl:
    run_id: str
    owner: ControlOwner = ControlOwner.automation
    state: SessionState = SessionState.running
    history: list[ControlEvent] = field(default_factory=list)
    human_actions: list[HumanAction] = field(default_factory=list)
    intervention_id: str | None = None
    resume_from_step: str | None = None
    last_note: str = ""
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        self._record("session_opened", None, self.owner.value, "run started")

    # -- internals --------------------------------------------------------

    def _record(self, kind: str, frm: str | None, to: str | None, reason: str, actor: str = "system") -> ControlEvent:
        event = ControlEvent(
            at=_ts(), kind=kind, from_owner=frm, to_owner=to, state=self.state.value,
            reason=reason, actor=actor,
        )
        self.history.append(event)
        return event

    # -- automation side --------------------------------------------------

    def can_act(self) -> bool:
        return self.owner is ControlOwner.automation and self.state in {
            SessionState.running,
            SessionState.resuming,
        }

    def assert_can_act(self, what: str = "act") -> None:
        if not self.can_act():
            raise ControlViolation(
                f"automation may not {what}: owner={self.owner.value} state={self.state.value}"
            )

    def pause_for_human(self, reason: str, intervention_id: str, resume_from_step: str | None) -> ControlEvent:
        """Automation stops issuing actions but keeps the session alive."""
        with self._lock:
            self.state = SessionState.paused_pending_human
            self.intervention_id = intervention_id
            self.resume_from_step = resume_from_step
            return self._record("automation_paused", self.owner.value, self.owner.value, reason)

    # -- human side -------------------------------------------------------

    def grant_human_control(self, actor: str = "operator") -> ControlEvent:
        with self._lock:
            if self.state is not SessionState.paused_pending_human:
                raise ControlViolation(
                    f"cannot grant human control from state {self.state.value!r}"
                )
            prev = self.owner.value
            self.owner = ControlOwner.human
            self.state = SessionState.human_control
            return self._record(
                "control_granted", prev, self.owner.value, "human took control of the live session", actor
            )

    def record_human_action(self, action: HumanAction) -> None:
        with self._lock:
            self.human_actions.append(action)

    def release_to_automation(self, note: str = "", actor: str = "operator") -> ControlEvent:
        with self._lock:
            if self.owner is not ControlOwner.human:
                raise ControlViolation("cannot release control that the human does not hold")
            prev = self.owner.value
            self.owner = ControlOwner.automation
            self.state = SessionState.resuming
            self.last_note = note
            return self._record(
                "control_released", prev, self.owner.value, note or "human released control", actor
            )

    def abort(self, reason: str, actor: str = "operator") -> ControlEvent:
        with self._lock:
            self.state = SessionState.aborted
            return self._record("run_aborted", self.owner.value, None, reason, actor)

    # -- terminal ---------------------------------------------------------

    def resumed(self) -> ControlEvent:
        with self._lock:
            self.state = SessionState.running
            self.intervention_id = None
            return self._record("automation_resumed", "automation", "automation", "run continues")

    def complete(self, reason: str = "run finished") -> ControlEvent:
        with self._lock:
            self.state = SessionState.completed
            return self._record("session_closed", self.owner.value, None, reason)

    def fail(self, reason: str) -> ControlEvent:
        with self._lock:
            self.state = SessionState.failed
            return self._record("session_failed", self.owner.value, None, reason)

    # -- serialization ----------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "owner": self.owner.value,
            "state": self.state.value,
            "intervention_id": self.intervention_id,
            "resume_from_step": self.resume_from_step,
            "history": [e.as_dict() for e in self.history],
            "human_actions": [a.as_dict() for a in self.human_actions],
        }
