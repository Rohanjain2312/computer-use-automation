"""Resolving a :class:`TargetPlan` against one observation.

Replay never queries the application directly. It resolves a plan against the
nodes the surface perceived, in declared confidence order, and reports which
strategy won. That makes drift observable: when the primary strategy stops
resolving and a fallback takes over, the run still succeeds but the evidence
says the primary degraded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..artifact.schema import StrategyKind, TargetPlan, TargetStrategy
from ..surface.base import Observation, UiNode


@dataclass
class ResolveResult:
    node: UiNode | None
    strategy: TargetStrategy | None
    attempts: list[dict] = field(default_factory=list)
    ambiguous: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.node is not None

    def summary(self) -> dict:
        return {
            "resolved": self.ok,
            "strategy": self.strategy.kind.value if self.strategy else None,
            "strategy_confidence": self.strategy.confidence if self.strategy else None,
            "ambiguous": self.ambiguous,
            "node": (
                {
                    "role": self.node.role,
                    "name": self.node.name,
                    "frame": self.node.frame_name,
                    "rect": [self.node.rect.x, self.node.rect.y],
                }
                if self.node
                else None
            ),
            "attempts": self.attempts,
            "error": self.error,
        }


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().casefold()


def _cmp(actual: str, expected: str, mode: str) -> bool:
    a, e = _norm(actual), _norm(expected)
    if mode == "exact":
        return (actual or "").strip() == (expected or "").strip()
    if mode == "contains":
        return e in a
    if mode == "regex":
        return bool(re.search(expected, actual or "", re.I))
    return a == e  # ci_exact (default)


def _frame_ok(node: UiNode, frame: str | None) -> bool:
    if not frame:
        return True
    return node.frame_name == frame or node.frame_path.endswith(frame)


def _match_role_name(nodes: list[UiNode], p: dict) -> list[UiNode]:
    role, name = p.get("role"), p.get("name", "")
    mode = p.get("match", "ci_exact")
    return [n for n in nodes if (not role or n.role == role) and _cmp(n.name, name, mode)]


def label_pool_for(node: UiNode) -> list[str]:
    """The nearby text that genuinely reads as this node's label.

    Shared by the matcher and by candidate generation so the two can never
    disagree about what "the label beside it" means:

    * in a real column table the header is the label, and ``table_cell`` already
      addresses that, so proximity contributes nothing;
    * in a key/value panel the label is strictly the cell to the LEFT — the cell
      above belongs to the previous row;
    * outside a table, any immediate neighbour may be the label.
    """
    if node.table and node.table.header_confident:
        pool: list[str] = []
    elif node.table:
        pool = [node.label_left] if node.label_left else []
    else:
        pool = list(node.label_candidates)
    if node.name_source == "label-proximity" and node.name and node.name not in pool:
        pool.insert(0, node.name)
    return [p for p in pool if p]


def _match_label_proximity(nodes: list[UiNode], p: dict) -> list[UiNode]:
    role, label = p.get("role"), p.get("label", "")
    mode = p.get("match", "ci_exact")
    out = []
    for n in nodes:
        if role and n.role != role:
            continue
        if any(_cmp(c, label, mode) for c in label_pool_for(n)):
            out.append(n)
    return out


def _match_text_anchor(nodes: list[UiNode], p: dict) -> list[UiNode]:
    role, text = p.get("role"), p.get("text", "")
    mode = p.get("match", "contains")
    return [
        n
        for n in nodes
        if (not role or n.role == role) and (_cmp(n.text, text, mode) or _cmp(n.name, text, mode))
    ]


def _match_table_cell(nodes: list[UiNode], p: dict) -> list[UiNode]:
    row_label = p.get("row_label", "")
    column = p.get("column", "")
    headers = p.get("headers")
    row_mode = p.get("row_match", "ci_exact")
    out = []
    for n in nodes:
        t = n.table
        if not t:
            continue
        if headers and _norm(" | ".join(t.headers)) != _norm(" | ".join(headers)):
            continue
        if column and not _cmp(t.col_header, column, "ci_exact"):
            continue
        if row_label and not _cmp(t.row_label, row_label, row_mode):
            continue
        if t.row_index == 0 and t.col_header and _cmp(n.text, t.col_header, "ci_exact"):
            continue  # the header row itself is never the data cell
        out.append(n)
    return out


def _match_dom_hint(nodes: list[UiNode], p: dict) -> list[UiNode]:
    css = p.get("css", "")
    name_attr = p.get("name_attr")
    out = []
    for n in nodes:
        hint = n.dom_hint or {}
        if css and hint.get("css") == css:
            out.append(n)
        elif name_attr and hint.get("name_attr") == name_attr:
            out.append(n)
    return out


def _match_viewport_ratio(nodes: list[UiNode], p: dict, obs: Observation) -> list[UiNode]:
    xr, yr = float(p.get("x_ratio", 0)), float(p.get("y_ratio", 0))
    tol = float(p.get("tolerance", 0.04))
    vw = float(p.get("viewport_width", 1280))
    vh = float(p.get("viewport_height", 900))
    role = p.get("role")
    tx, ty = xr * vw, yr * vh
    scored = []
    for n in nodes:
        if role and n.role != role:
            continue
        cx, cy = n.rect.center
        dist = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
        if dist <= tol * max(vw, vh):
            scored.append((dist, n))
    scored.sort(key=lambda s: s[0])
    return [n for _, n in scored[:1]]


_MATCHERS = {
    StrategyKind.role_name: lambda nodes, p, obs: _match_role_name(nodes, p),
    StrategyKind.label_proximity: lambda nodes, p, obs: _match_label_proximity(nodes, p),
    StrategyKind.text_anchor: lambda nodes, p, obs: _match_text_anchor(nodes, p),
    StrategyKind.table_cell: lambda nodes, p, obs: _match_table_cell(nodes, p),
    StrategyKind.dom_hint: lambda nodes, p, obs: _match_dom_hint(nodes, p),
    StrategyKind.viewport_ratio: _match_viewport_ratio,
}


def _tie_break(matches: list[UiNode]) -> UiNode:
    """Deterministic disambiguation: enabled and on screen first, then reading order."""
    return sorted(
        matches,
        key=lambda n: (not n.enabled, not n.in_viewport, round(n.rect.y), round(n.rect.x)),
    )[0]


def match_strategy(
    strategy: TargetStrategy, obs: Observation, frame: str | None = None
) -> list[UiNode]:
    nodes = [n for n in obs.nodes if _frame_ok(n, frame)]
    matcher = _MATCHERS.get(strategy.kind)
    if matcher is None:
        return []
    return matcher(nodes, strategy.params, obs)


def resolve(
    plan: TargetPlan,
    obs: Observation,
    *,
    supported: set[StrategyKind] | None = None,
) -> ResolveResult:
    """Try each strategy in order; return the first unambiguous match.

    If every strategy that matched was ambiguous, the highest-confidence one is
    used with a deterministic tie-break and the result is flagged ``ambiguous``
    rather than failing the run — an ambiguous but consistent resolution is more
    useful than a hard stop, as long as the evidence records it.
    """
    attempts: list[dict] = []
    ambiguous_fallback: tuple[TargetStrategy, list[UiNode]] | None = None

    for strategy in plan.strategies:
        if supported is not None and strategy.kind not in supported:
            attempts.append(
                {"kind": strategy.kind.value, "skipped": "unsupported by this surface"}
            )
            continue
        matches = match_strategy(strategy, obs, plan.frame)
        attempts.append(
            {
                "kind": strategy.kind.value,
                "confidence": strategy.confidence,
                "matches": len(matches),
            }
        )
        if len(matches) == 1:
            return ResolveResult(node=matches[0], strategy=strategy, attempts=attempts)
        if len(matches) > 1:
            if not plan.require_unique:
                return ResolveResult(node=_tie_break(matches), strategy=strategy, attempts=attempts)
            if ambiguous_fallback is None:
                ambiguous_fallback = (strategy, matches)

    if ambiguous_fallback is not None:
        strategy, matches = ambiguous_fallback
        return ResolveResult(
            node=_tie_break(matches),
            strategy=strategy,
            attempts=attempts,
            ambiguous=True,
            error=f"{len(matches)} controls matched {strategy.kind.value}; used reading-order tie-break",
        )

    return ResolveResult(
        node=None,
        strategy=None,
        attempts=attempts,
        error=f"no strategy resolved {plan.description!r}",
    )
