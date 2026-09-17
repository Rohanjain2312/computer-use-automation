from __future__ import annotations

from cua.artifact.schema import Predicate
from cua.replay.checkpoints import evaluate
from synthetic import accounts_table_nodes, node, observation


def p(kind: str, **params) -> Predicate:
    return Predicate(kind=kind, params=params)


def test_text_present_ignores_whitespace_shape():
    obs = observation([], text="Member Servicing  —\n  Inquiry")
    assert evaluate(p("text_present", text="Member Servicing — Inquiry"), obs)


def test_text_absent_is_the_negation_and_says_so():
    obs = observation([], text="all good")
    verdict = evaluate(p("text_absent", text="SYSTEM ERROR"), obs)
    assert verdict.ok and "not found" in verdict.detail


def test_url_matches_any_frame_not_only_the_top_document():
    """In a frameset the top URL never changes; the state lives in the frames."""
    obs = observation([], url="http://h/",
                      frames=[{"name": "bodypane", "url": "http://h/members/100244"}])
    assert evaluate(p("url_matches", pattern=r"/members/\d+$"), obs)
    assert not evaluate(p("url_matches", pattern=r"/members/\d+$", scope="top"), obs)


def test_element_present_uses_the_same_locator_machinery():
    obs = observation([node("n1", "button", "Search")])
    assert evaluate(p("element_present", strategy="role_name",
                      params={"role": "button", "name": "Search"}), obs)
    assert evaluate(p("element_absent", strategy="role_name",
                      params={"role": "button", "name": "Transfer"}), obs)


def test_table_cell_matches_compares_the_addressed_cell():
    obs = observation(accounts_table_nodes())
    assert evaluate(p("table_cell_matches", row_label="Regular Savings",
                      column="Current Balance", contains="4,812.37"), obs)
    assert not evaluate(p("table_cell_matches", row_label="Regular Savings",
                          column="Status", equals="CLOSED"), obs)


def test_missing_cell_is_reported_as_missing_not_as_a_mismatch():
    obs = observation(accounts_table_nodes())
    verdict = evaluate(p("table_cell_matches", row_label="Money Market", column="Status"), obs)
    assert not verdict.ok and "no cell found" in verdict.detail


def test_all_of_reports_every_failing_child():
    obs = observation([], text="only this")
    verdict = evaluate(Predicate(kind="all_of", children=[
        p("text_present", text="only this"),
        p("text_present", text="missing one"),
        p("text_present", text="missing two"),
    ]), obs)
    assert not verdict.ok
    assert "missing one" in verdict.detail and "missing two" in verdict.detail


def test_any_of_short_circuits_on_the_first_match():
    obs = observation([], text="No member matching that number was found")
    verdict = evaluate(Predicate(kind="any_of", children=[
        p("text_present", text="Account Detail"),
        p("text_present", text="No member matching that number was found"),
    ]), obs)
    assert verdict.ok


def test_not_inverts():
    obs = observation([], text="fine")
    assert evaluate(Predicate(kind="not", children=[p("text_present", text="SYSTEM ERROR")]), obs)


def test_failed_predicate_carries_an_excerpt_for_debugging():
    obs = observation([], text="the console said something else entirely here")
    verdict = evaluate(p("text_present", text="Account Detail"), obs)
    assert not verdict.ok and verdict.observed
