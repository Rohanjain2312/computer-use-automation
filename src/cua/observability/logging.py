"""Structured run log.

One JSONL file per run. Every line answers both "what did the system do" and
"why did it do it": actions carry the intent they came from, decisions carry the
model's stated rationale, policy checks carry the rule that fired, and recoveries
carry the condition that triggered them.

Every value written passes through the redactor first. There is no unredacted
write path.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..safety.redact import Redactor


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunLogger:
    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        phase: str,
        redactor: Redactor,
        echo: bool = True,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.phase = phase
        self.redactor = redactor
        self.echo = echo
        self._seq = 0
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8")
        self.events: list[dict[str, Any]] = []

    def log(self, event: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            record = {
                "ts": _ts(),
                "seq": self._seq,
                "run_id": self.run_id,
                "phase": self.phase,
                "event": event,
                **self.redactor.scrub(fields),
            }
            self._fh.write(json.dumps(record, default=str) + "\n")
            self._fh.flush()
            self.events.append(record)
        if self.echo:
            self._echo(record)
        return record

    def _echo(self, record: dict[str, Any]) -> None:
        event = record["event"]
        bits = []
        for key in ("step_id", "intent", "action", "status", "outcome", "reason", "detail", "owner"):
            if key in record and record[key] not in (None, ""):
                bits.append(f"{key}={record[key]}")
        line = f"  [{record['seq']:>3}] {event:<22} " + "  ".join(str(b) for b in bits)
        print(line[:220], file=sys.stderr, flush=True)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
