"""Evidence directory management.

Every run gets its own directory. Screenshots are captured at each step for
discovery and at step boundaries for replay; on failure the writer additionally
captures a DOM snapshot and the full perceived node list, which together are
enough to answer "what did the machine actually see" without re-running.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from ..safety.redact import Redactor
from ..surface.base import Observation

REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_ROOT = REPO_ROOT / "evidence"


class EvidenceWriter:
    def __init__(self, phase: str, run_id: str, redactor: Redactor, root: Path | None = None) -> None:
        self.root = (root or EVIDENCE_ROOT) / phase / run_id
        self.screens = self.root / "screens"
        self.snapshots = self.root / "snapshots"
        for d in (self.root, self.screens, self.snapshots):
            d.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self.run_id = run_id
        self.phase = phase
        self._n = 0

    @property
    def log_path(self) -> Path:
        return self.root / "run.jsonl"

    def rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(REPO_ROOT))
        except ValueError:  # pragma: no cover
            return str(path)

    def screenshot(self, data: bytes | None, label: str) -> str | None:
        if not data:
            return None
        self._n += 1
        path = self.screens / f"{self._n:03d}_{_slug(label)}.png"
        path.write_bytes(data)
        return self.rel(path)

    def observation_snapshot(self, obs: Observation, label: str) -> str:
        """The perceived accessibility tree — the machine's view of the screen."""
        payload = {
            "url": obs.url,
            "title": obs.title,
            "captured_at": obs.captured_at,
            "frames": obs.frames,
            "text": obs.text,
            "nodes": [_node_dict(n) for n in obs.nodes],
        }
        path = self.snapshots / f"{_slug(label)}.ax.json"
        path.write_text(json.dumps(self.redactor.scrub(payload), indent=2, default=str))
        return self.rel(path)

    def dom_snapshot(self, html: str, label: str) -> str:
        path = self.snapshots / f"{_slug(label)}.dom.html"
        path.write_text(self.redactor.scrub_text(html))
        return self.rel(path)

    def write_json(self, name: str, payload: Any) -> str:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.redactor.scrub(_plain(payload)), indent=2, default=str))
        return self.rel(path)

    def append_jsonl(self, name: str, payload: Any) -> str:
        path = self.root / name
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(self.redactor.scrub(_plain(payload)), default=str) + "\n")
        return self.rel(path)


def _plain(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def _node_dict(node) -> dict:
    d = asdict(node)
    d["rect"] = {
        "x": node.rect.x,
        "y": node.rect.y,
        "width": node.rect.width,
        "height": node.rect.height,
    }
    if node.secret:
        d["value"] = "[REDACTED]"
    return d


def _slug(text: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_" else "_" for c in text.strip().lower())
    return (out[:48] or "item").strip("_")
