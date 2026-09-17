"""The intervention request: everything a person needs in order to act."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
INBOX = REPO_ROOT / "runtime" / "interventions"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class InterventionRequest:
    """Why automation stopped, where it stopped, and what a human may do next."""

    id: str = field(default_factory=lambda: "int_" + uuid.uuid4().hex[:10])
    created_at: str = field(default_factory=_ts)
    run_id: str = ""
    phase: str = "replay"

    capability_id: str = ""
    capability_version: str = ""
    goal: str = ""

    step_id: str | None = None
    step_index: int | None = None
    step_intent: str = ""

    reason_class: str = "stuck"
    reason: str = ""

    observed_url: str = ""
    observed_title: str = ""
    observed_excerpt: str = ""
    screenshot_path: str | None = None
    dom_snapshot_path: str | None = None

    suggested_actions: list[str] = field(default_factory=list)
    resume_hint: str = ""

    status: str = "open"  # open | claimed | resolved | aborted
    resolution: str = ""
    resolved_at: str | None = None
    operator: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    def write(self, extra_dir: Path | None = None) -> list[str]:
        """Publish to the shared inbox (and the run's evidence directory)."""
        paths = []
        for directory in [INBOX] + ([extra_dir] if extra_dir else []):
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{self.id}.json"
            path.write_text(json.dumps(self.as_dict(), indent=2, default=str))
            paths.append(str(path))
        return paths

    def summary_lines(self) -> list[str]:
        return [
            f"intervention {self.id}  [{self.reason_class}]",
            f"  capability : {self.capability_id} v{self.capability_version}",
            f"  goal       : {self.goal}",
            f"  step       : {self.step_index} {self.step_id} — {self.step_intent}",
            f"  reason     : {self.reason}",
            f"  at         : {self.observed_url}  ({self.observed_title})",
            f"  screenshot : {self.screenshot_path}",
            f"  next       : {self.resume_hint}",
        ]
