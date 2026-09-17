"""Deterministic generation of targeting strategies from an observed control.

The discovery LLM decides *what to do*; it does not invent locators. Once it has
acted on a control, this module reads the control the surface actually perceived
and emits every strategy that would have found it, scored by how many other
controls in the same observation the strategy also matches. The model is then
asked only to review and annotate them.

Keeping locator synthesis deterministic is what makes replay reproducible: the
same discovery trajectory always yields the same locators, and a model that is
vague about "the search button" cannot degrade them.
"""

from __future__ import annotations

from ..artifact.schema import StrategyKind, TargetPlan, TargetStrategy
from ..surface.base import Observation, UiNode
from .strategies import label_pool_for, match_strategy

# Name sources that come from something a human would read as a label.
_STRONG_NAME_SOURCES = {
    "aria-label",
    "aria-labelledby",
    "label-for",
    "label-wrap",
    "value-attr",
    "inner-text",
    "placeholder",
    "title",
}


def _uniqueness(strategy: TargetStrategy, obs: Observation, frame: str | None, node: UiNode) -> int:
    matches = match_strategy(strategy, obs, frame)
    if not any(m.ref == node.ref for m in matches):
        return 0  # does not even find the node it was derived from
    return len(matches)


def _adjust(base: float, count: int) -> float:
    if count == 0:
        return 0.0
    if count == 1:
        return base
    if count == 2:
        return round(base * 0.6, 3)
    return round(base * 0.35, 3)


def build_target_plan(
    node: UiNode,
    obs: Observation,
    *,
    description: str | None = None,
    viewport: dict[str, int] | None = None,
    purpose: str = "control",
) -> TargetPlan:
    """Emit an ordered primary+fallback plan for ``node``.

    ``purpose`` matters more than it looks. A *control* is identified by the text
    on it — a button labelled "Search" is still labelled "Search" next week. A
    *value* is the opposite: the cell holding ``$4,812.37`` is the thing we came
    to read, so keying on that text would record one member's balance as the way
    to find any member's balance, and would bake account data into the artifact.
    For values, strategies derived from the node's own text are suppressed and
    the locator must describe the cell's *position in the table* or its adjacent
    label instead.
    """
    is_value = purpose == "value"
    viewport = viewport or {"width": 1280, "height": 900}
    frame = node.frame_name or None
    proposals: list[tuple[TargetStrategy, float]] = []

    def propose(kind: StrategyKind, params: dict, base: float, rationale: str) -> None:
        strategy = TargetStrategy(kind=kind, params=params, confidence=base, rationale=rationale)
        count = _uniqueness(strategy, obs, frame, node)
        score = _adjust(base, count)
        if score <= 0:
            return
        note = (
            "uniquely identifies this control on the screen"
            if count == 1
            else f"also matches {count - 1} other control(s) on this screen"
        )
        proposals.append(
            (
                strategy.model_copy(
                    update={"confidence": score, "rationale": f"{rationale} ({note})."}
                ),
                score,
            )
        )

    if node.name and node.name_source in _STRONG_NAME_SOURCES and not (
        is_value and node.name_source == "inner-text"
    ):
        propose(
            StrategyKind.role_name,
            {"role": node.role, "name": node.name, "match": "ci_exact"},
            0.94,
            f"The control exposes the accessible name {node.name!r} via {node.name_source}; "
            "role plus accessible name is the most portable identifier and has a direct "
            "equivalent in desktop accessibility APIs",
        )

    for label in dict.fromkeys(l for l in label_pool_for(node) if l != node.text):
        propose(
            StrategyKind.label_proximity,
            {"role": node.role, "label": label, "match": "ci_exact"},
            0.93 if is_value else 0.87,
            f"The visible text {label!r} sits immediately beside the control, which is how this "
            "legacy screen labels fields; this survives restyling and added columns because it "
            "keys on what the operator reads, not on markup structure",
        )

    if node.table and node.table.col_header and node.table.header_confident:
        base = 0.96 if is_value else 0.9
        propose(
            StrategyKind.table_cell,
            {
                "headers": node.table.headers,
                "row_label": node.table.row_label,
                "column": node.table.col_header,
                "row_match": "ci_exact",
            },
            base,
            f"Addresses the cell by table semantics — the {node.table.col_header!r} column of the "
            f"{node.table.row_label!r} row, in the table whose columns are "
            f"{node.table.headers} — so inserted rows, reordered rows and restyled markup do not "
            "move the target",
        )
        # Same addressing without pinning the full column set, so that a tenant
        # variant with an extra column degrades instead of failing outright.
        propose(
            StrategyKind.table_cell,
            {
                "row_label": node.table.row_label,
                "column": node.table.col_header,
                "row_match": "ci_exact",
            },
            round(base - 0.08, 3),
            f"Same row/column addressing but without requiring the exact column set, so a tenant "
            "variant that adds or renames a neighbouring column still resolves",
        )

    anchor_text = "" if is_value else (node.text or node.name or "").strip()
    if anchor_text and len(anchor_text) <= 80:
        propose(
            StrategyKind.text_anchor,
            {"role": node.role, "text": anchor_text, "match": "ci_exact"},
            0.72,
            f"Matches the visible text {anchor_text!r}; weaker than role+name because copy changes "
            "break it, but it needs no markup at all",
        )

    hint = node.dom_hint or {}
    if hint.get("name_attr"):
        propose(
            StrategyKind.dom_hint,
            {"name_attr": hint["name_attr"], "css": hint.get("css", "")},
            0.55,
            f"Falls back to the form field name {hint['name_attr']!r}; web-only and tied to markup, "
            "so it is kept strictly as a fallback and its use is recorded as degradation",
        )
    elif hint.get("css"):
        propose(
            StrategyKind.dom_hint,
            {"css": hint["css"]},
            0.5,
            "Falls back to a coarse CSS hint; web-only and brittle, kept only so a run can finish",
        )

    cx, cy = node.rect.center
    propose(
        StrategyKind.viewport_ratio,
        {
            "role": node.role,
            "x_ratio": round(cx / viewport["width"], 4),
            "y_ratio": round(cy / viewport["height"], 4),
            "tolerance": 0.04,
            "viewport_width": viewport["width"],
            "viewport_height": viewport["height"],
        },
        0.25,
        "Last-resort positional fallback recorded from the discovery run; only correct while the "
        "layout is unchanged, and its use is reported so the artifact can be re-recorded",
    )

    proposals.sort(key=lambda p: p[1], reverse=True)
    strategies = [s for s, _ in proposals]
    if not strategies:  # pragma: no cover - viewport_ratio always survives
        raise ValueError(f"no targeting strategy could be derived for {node!r}")

    label = description or _describe(node, is_value)
    return TargetPlan(description=label, strategies=strategies, frame=node.frame_name or None)


def _describe(node: UiNode, is_value: bool = False) -> str:
    """Name the target the way a reviewer would — never by the datum it holds."""
    if node.table and node.table.col_header and node.table.header_confident:
        return f"the {node.table.col_header!r} cell of the {node.table.row_label!r} row"
    if is_value:
        if node.label_left:
            return f"the value cell beside the {node.label_left!r} label"
        return f"the {node.role} holding the extracted value"
    label = node.name or node.text or "(unnamed)"
    return f"the {label!r} {node.role}"
