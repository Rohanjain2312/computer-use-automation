"""Domain / route / action allowlist."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "allowlist.yaml"


@dataclass(frozen=True)
class AllowlistProfile:
    name: str
    description: str
    allowed_origins: tuple[str, ...]
    allowed_path_patterns: tuple[str, ...]
    denied_path_patterns: tuple[str, ...]
    allowed_actions: frozenset[str]
    max_autonomous_risk: str
    irreversible_policy: str
    approval_required_policy: str
    irreversible_control_markers: tuple[str, ...] = ()
    reversible_control_markers: tuple[str, ...] = ()
    human_only_control_markers: tuple[str, ...] = ()
    max_discovery_steps: int = 32
    max_run_seconds: int = 420
    extra: dict = field(default_factory=dict)

    # -- checks -----------------------------------------------------------

    def url_verdict(self, url: str) -> tuple[bool, str]:
        parsed = urlparse(url)
        if parsed.scheme in {"about", "data", "blob", "chrome"} or not parsed.netloc:
            return False, f"non-navigable or opaque URL {url!r}"
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.allowed_origins:
            return False, f"origin {origin!r} is not in the allowlist for profile {self.name!r}"
        path = parsed.path or "/"
        for pattern in self.denied_path_patterns:
            if re.search(pattern, path):
                return False, f"path {path!r} matches denied pattern {pattern!r}"
        for pattern in self.allowed_path_patterns:
            if re.search(pattern, path):
                return True, f"path {path!r} matches allowed pattern {pattern!r}"
        return False, f"path {path!r} matches no allowed pattern for profile {self.name!r}"

    def url_allowed(self, url: str) -> bool:
        return self.url_verdict(url)[0]

    def action_allowed(self, action: str) -> bool:
        return action in self.allowed_actions


class AllowlistError(RuntimeError):
    pass


def load_profile(name: str = "default", path: Path | str | None = None) -> AllowlistProfile:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.exists():
        raise AllowlistError(f"allowlist config not found at {cfg_path}")
    data = yaml.safe_load(cfg_path.read_text()) or {}
    profiles = data.get("profiles") or {}
    if name not in profiles:
        raise AllowlistError(
            f"allowlist profile {name!r} not found in {cfg_path} "
            f"(available: {sorted(profiles) or 'none'})"
        )
    p = profiles[name]
    return AllowlistProfile(
        name=name,
        description=(p.get("description") or "").strip(),
        allowed_origins=tuple(p.get("allowed_origins") or ()),
        allowed_path_patterns=tuple(p.get("allowed_path_patterns") or ()),
        denied_path_patterns=tuple(p.get("denied_path_patterns") or ()),
        allowed_actions=frozenset(p.get("allowed_actions") or ()),
        max_autonomous_risk=p.get("max_autonomous_risk", "read_only"),
        irreversible_policy=p.get("irreversible_policy", "block"),
        approval_required_policy=p.get("approval_required_policy", "require_human"),
        irreversible_control_markers=tuple(p.get("irreversible_control_markers") or ()),
        reversible_control_markers=tuple(p.get("reversible_control_markers") or ()),
        human_only_control_markers=tuple(p.get("human_only_control_markers") or ()),
        max_discovery_steps=int(p.get("max_discovery_steps", 32)),
        max_run_seconds=int(p.get("max_run_seconds", 420)),
    )
