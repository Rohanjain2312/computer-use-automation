"""Redaction for everything that gets persisted.

Two mechanisms, because pattern matching alone is not enough:

1. *Registered values* — the concrete strings the run was given (a passcode, an
   override code, a member name declared ``pii``). These are replaced exactly,
   so a passcode that happens to look like an ordinary word is still removed.
2. *Patterns* — SSNs, card numbers (Luhn-checked so order numbers survive),
   emails, phone numbers, long digit runs and API-key shapes, for sensitive
   data that appears on screen without ever having been declared.

Screenshots cannot be regex-scrubbed, so the surface masks secret-typed fields
with an opaque overlay before the capture is taken.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[REDACTED]"


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _card_sub(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return f"{MASK}:card" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)


_PATTERNS: list[tuple[str, re.Pattern[str], Any]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), f"{MASK}:ssn"),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b"), _card_sub),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), f"{MASK}:email"),
    ("phone", re.compile(r"\b(?:\+1[ -]?)?\(?\d{3}\)?[ -]\d{3}-\d{4}\b"), f"{MASK}:phone"),
    (
        "api_key",
        re.compile(r"\b(?:sk|pk|api|tok)[-_][A-Za-z0-9_\-]{12,}\b", re.I),
        f"{MASK}:token",
    ),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}", re.I), f"{MASK}:token"),
    ("long_account", re.compile(r"(?<![\d*.,$])\d{10,19}(?![\d.,])"), f"{MASK}:account"),
]

_SENSITIVE_KEY = re.compile(
    r"(pass|passcode|secret|token|api_?key|credential|ssn|authorization|cookie|ovrcode)", re.I
)


class Redactor:
    """Scrubs strings and nested structures before they are written anywhere."""

    def __init__(self) -> None:
        self._values: list[tuple[str, str]] = []

    def register(self, value: str | None, label: str = "secret") -> None:
        """Register a literal value that must never appear in persisted output."""
        if value and len(str(value)) >= 3:
            self._values.append((str(value), f"{MASK}:{label}"))
            # Longest first so overlapping registrations mask greedily.
            self._values.sort(key=lambda v: len(v[0]), reverse=True)

    def scrub_text(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        out = text
        for raw, replacement in self._values:
            if raw in out:
                out = out.replace(raw, replacement)
        for _, pattern, replacement in _PATTERNS:
            out = pattern.sub(replacement, out)
        return out

    def scrub(self, obj: Any) -> Any:
        """Recursively scrub a JSON-shaped structure.

        Keys whose *name* implies a secret are masked wholesale, so a field we
        did not anticipate cannot leak through a pattern gap.
        """
        if isinstance(obj, str):
            return self.scrub_text(obj)
        if isinstance(obj, dict):
            out = {}
            for key, value in obj.items():
                if isinstance(key, str) and _SENSITIVE_KEY.search(key) and isinstance(value, str):
                    out[key] = MASK if value else value
                else:
                    out[key] = self.scrub(value)
            return out
        if isinstance(obj, (list, tuple)):
            return [self.scrub(v) for v in obj]
        return obj

    def contains_registered(self, text: str) -> bool:
        return any(raw in text for raw, _ in self._values)
