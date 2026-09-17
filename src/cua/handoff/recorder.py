"""Recording what the human actually did to the live session.

Two sources, because neither alone is complete:

* in-page capture-phase listeners, which see clicks, field changes and form
  submits with the control's accessible name;
* frame-URL diffing from the Python side, which catches navigations that the
  in-page listeners cannot report because the document they lived in was
  replaced mid-event.

Every recorded action is passed through the same redactor as the rest of the
run before it is persisted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..safety.redact import Redactor
from ..surface.web import PlaywrightWebSurface
from .control import HumanAction

_RECORDER_JS = (Path(__file__).parent / "recorder.js").read_text()
_DRAIN_JS = """() => {
  let t = window; try { t = window.top || window; } catch (e) { t = window; }
  const out = t.__cua_human || [];
  t.__cua_human = [];
  return out;
}"""


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class HumanActionRecorder:
    def __init__(self, surface: PlaywrightWebSurface, redactor: Redactor, *, actor: str = "human",
                 simulated: bool = False) -> None:
        self.surface = surface
        self.redactor = redactor
        self.actor = actor
        self.simulated = simulated
        self._frame_urls: dict[str, str] = {}

    def install(self) -> None:
        """Idempotent; re-run after every navigation to cover new documents."""
        try:
            self.surface.page.evaluate(_RECORDER_JS)
        except Exception:
            pass
        self._frame_urls = self._current_frame_urls()

    def _current_frame_urls(self) -> dict[str, str]:
        urls: dict[str, str] = {}
        try:
            for frame in self.surface.page.frames:
                urls[frame.name or frame.url] = frame.url
        except Exception:
            pass
        return urls

    def drain(self) -> list[HumanAction]:
        actions: list[HumanAction] = []

        # Navigations first: they explain why the in-page buffer may be short.
        current = self._current_frame_urls()
        for key, url in current.items():
            before = self._frame_urls.get(key)
            if before is not None and before != url:
                actions.append(
                    HumanAction(
                        at=_ts(),
                        kind="navigation",
                        description=f"frame {key!r} moved to a new page",
                        frame_url=self.redactor.scrub_text(url),
                        actor=self.actor,
                        simulated=self.simulated,
                    )
                )
        self._frame_urls = current

        raw = []
        try:
            raw = self.surface.page.evaluate(_DRAIN_JS) or []
        except Exception:
            raw = []

        for rec in raw:
            kind = rec.get("kind", "event")
            name = rec.get("name") or rec.get("text") or "(unnamed control)"
            if kind == "click":
                desc = f"clicked the {name!r} {rec.get('role', 'control')}"
            elif kind == "input":
                desc = f"entered a value into {name!r}"
            elif kind == "submit":
                desc = f"submitted form {name!r}"
            else:
                desc = f"{kind} on {name!r}"
            coords = (
                [float(rec["x"]), float(rec["y"])]
                if rec.get("x") is not None and rec.get("y") is not None
                else None
            )
            actions.append(
                HumanAction(
                    at=rec.get("at") or _ts(),
                    kind=kind,
                    description=self.redactor.scrub_text(desc),
                    frame_url=self.redactor.scrub_text(rec.get("frame_url", "")),
                    value=(
                        self.redactor.scrub_text(str(rec["value"]))
                        if rec.get("value") is not None
                        else None
                    ),
                    coordinates=coords,
                    actor=self.actor,
                    simulated=self.simulated,
                )
            )

        # Re-attach for any document created since the last drain.
        self.install()
        return actions
