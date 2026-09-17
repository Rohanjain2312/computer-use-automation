from __future__ import annotations

import pytest

from cua.artifact.schema import StrategyKind, TargetPlan, TargetStrategy
from cua.locate.candidates import build_target_plan
from cua.locate.strategies import resolve
from synthetic import accounts_table_nodes, node, observation


def plan(kind: StrategyKind, params: dict, *, frame=None, unique=True) -> TargetPlan:
    return TargetPlan(description="t", frame=frame, require_unique=unique,
                      strategies=[TargetStrategy(kind=kind, params=params, confidence=0.9,
                                                 rationale="test")])


def test_role_name_resolves_exactly_one_control():
    obs = observation([node("n1", "button", "Search"), node("n2", "button", "Clear")])
    result = resolve(plan(StrategyKind.role_name, {"role": "button", "name": "Search"}), obs)
    assert result.ok and result.node.ref == "n1"
    assert result.strategy.kind is StrategyKind.role_name


def test_table_cell_addresses_by_row_and_column_not_position():
    obs = observation(accounts_table_nodes())
    result = resolve(plan(StrategyKind.table_cell,
                          {"row_label": "Regular Savings", "column": "Current Balance"},
                          frame="acctframe"), obs)
    assert result.ok and result.node.text == "$4,812.37"


def test_table_cell_never_returns_the_header_row():
    obs = observation(accounts_table_nodes())
    result = resolve(plan(StrategyKind.table_cell,
                          {"row_label": "Type", "column": "Current Balance"},
                          frame="acctframe"), obs)
    assert not result.ok


def test_label_proximity_uses_the_cell_to_the_left_in_a_key_value_panel():
    nodes = [
        node("k1", "cell", "Member Name"),
        node("v1", "cell", "Dana Whitfield", label_left="Member Name", label_above="100244",
             table={"headers": ["Member Number", "100244"], "row_index": 2, "col_index": 1,
                    "row_label": "Member Name", "col_header": "100244",
                    "header_confident": False}),
        node("k2", "cell", "Branch", label_above="Member Name",
             table={"headers": ["Member Number", "100244"], "row_index": 3, "col_index": 0,
                    "row_label": "Branch", "col_header": "Member Number",
                    "header_confident": False}),
    ]
    result = resolve(plan(StrategyKind.label_proximity,
                          {"role": "cell", "label": "Member Name"}), observation(nodes))
    assert result.ok and result.node.text == "Dana Whitfield"


def test_fallback_is_used_when_the_primary_stops_resolving():
    obs = observation([node("n1", "button", "Search", label_left="Actions")])
    target = TargetPlan(description="the Search button", strategies=[
        TargetStrategy(kind=StrategyKind.role_name, params={"role": "button", "name": "Find"},
                       confidence=0.94, rationale="primary"),
        TargetStrategy(kind=StrategyKind.text_anchor,
                       params={"role": "button", "text": "Search", "match": "ci_exact"},
                       confidence=0.7, rationale="fallback"),
    ])
    result = resolve(target, obs)
    assert result.ok and result.strategy.kind is StrategyKind.text_anchor
    assert result.attempts[0]["matches"] == 0


def test_unresolvable_target_reports_every_attempt():
    obs = observation([node("n1", "button", "Search")])
    result = resolve(plan(StrategyKind.role_name, {"role": "button", "name": "Nope"}), obs)
    assert not result.ok
    assert result.attempts and result.error


def test_ambiguity_is_flagged_rather_than_silently_picking():
    obs = observation([node("a", "button", "Go", rect=(10, 40, 30, 10)),
                       node("b", "button", "Go", rect=(10, 10, 30, 10))])
    result = resolve(plan(StrategyKind.role_name, {"role": "button", "name": "Go"}), obs)
    assert result.ok and result.ambiguous
    assert result.node.ref == "b"  # reading order tie-break, deterministic


def test_unsupported_strategy_is_skipped_for_a_surface_that_cannot_do_it():
    obs = observation([node("n1", "button", "Search", dom_hint={"css": "input[name=go]"})])
    target = TargetPlan(description="x", strategies=[
        TargetStrategy(kind=StrategyKind.dom_hint, params={"css": "input[name=go]"},
                       confidence=0.5, rationale="web only")])
    result = resolve(target, obs, supported={StrategyKind.role_name})
    assert not result.ok
    assert result.attempts[0]["skipped"]


# -- candidate generation -------------------------------------------------


def test_control_candidates_prefer_role_plus_name_and_always_have_a_fallback():
    obs = observation([node("n1", "button", "Sign On", name_source="value-attr"),
                       node("n2", "textbox", "Operator ID", name_source="label-proximity",
                            label_left="Operator ID")])
    target = build_target_plan(obs.nodes[0], obs)
    assert target.strategies[0].kind is StrategyKind.role_name
    assert len(target.strategies) > 1
    assert all(s.rationale for s in target.strategies)
    confidences = [s.confidence for s in target.strategies]
    assert confidences == sorted(confidences, reverse=True)


def test_value_candidates_never_key_on_the_value_itself():
    """A balance cell must not be found by the balance it happens to show."""
    obs = observation(accounts_table_nodes())
    balance = next(n for n in obs.nodes
                   if n.table and n.table.row_label == "Regular Savings"
                   and n.table.col_header == "Current Balance")
    target = build_target_plan(balance, obs, purpose="value")
    assert target.strategies[0].kind is StrategyKind.table_cell
    serialized = str([s.params for s in target.strategies])
    assert "4,812.37" not in serialized and "4812.37" not in serialized


def test_control_candidates_may_key_on_the_control_text():
    obs = observation([node("n1", "button", "Search", name_source="value-attr")])
    target = build_target_plan(obs.nodes[0], obs, purpose="control")
    assert any("Search" in str(s.params) for s in target.strategies)


def test_generated_plan_actually_resolves_the_node_it_came_from():
    obs = observation(accounts_table_nodes() + [node("b1", "button", "Search",
                                                     name_source="value-attr")])
    for source in (obs.nodes[0], obs.nodes[-1]):
        target = build_target_plan(source, obs)
        assert resolve(target, obs).ok
