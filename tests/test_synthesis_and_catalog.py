"""Synthesis helpers and the agent-facing catalog."""

from __future__ import annotations

import json

import pytest

from cua.artifact.schema import ActionKind, RiskLevel
from cua.artifact.store import ArtifactStore
from cua.catalog.tools import CapabilityCatalog, result_schema, tool_schema
from cua.discovery.loop import RecordedAction
from cua.discovery.synthesize import (
    _canonical_path_pattern,
    _derive_checkpoint,
    _distinctive,
    _slug,
)
from fixture_artifact import build_artifact
from synthetic import node, observation


def action(expect="", before="", after="", before_frames=None, after_frames=None, n=None):
    return RecordedAction(
        index=0, phase="goal", tool="click", action=ActionKind.click, intent="do a thing",
        expect=expect, node=n,
        obs_before=observation([], text=before, frames=before_frames or []),
        obs_after=observation([], text=after, frames=after_frames or []),
        risk=RiskLevel.read_only)


# -- URL canonicalization (cross-tenant / cross-record reuse) -------------


def test_record_ids_in_urls_become_shapes_not_constants():
    assert _canonical_path_pattern("http://h/members/100244") == r"/members/\d+$"
    assert _canonical_path_pattern("http://h/members/100244/accounts") == r"/members/\d+/accounts$"


def test_non_id_segments_are_preserved_and_escaped():
    assert _canonical_path_pattern("http://h/members") == "/members$"
    assert _canonical_path_pattern("http://h/a.b/c") == r"/a\.b/c$"


def test_query_strings_are_dropped():
    assert _canonical_path_pattern("http://h/stub?m=Card+Services") == "/stub$"


# -- checkpoint derivation ------------------------------------------------


def test_model_expectation_is_used_when_it_actually_appeared():
    checkpoint = _derive_checkpoint(action(expect="Account Detail", before="Member Lookup",
                                           after="Member Record\nAccount Detail"))
    assert checkpoint.kind == "text_present"
    assert checkpoint.params["text"] == "Account Detail"


def test_a_wrong_expectation_falls_back_to_what_really_changed():
    """The model does not get to assert something the screen never showed."""
    checkpoint = _derive_checkpoint(action(expect="Totally Wrong Thing",
                                           before="Member Lookup",
                                           after="Member Lookup\nThe account summary is open"))
    assert checkpoint.params["text"] != "Totally Wrong Thing"
    assert "account summary" in checkpoint.params["text"]


def test_url_change_is_used_when_no_text_changed():
    checkpoint = _derive_checkpoint(action(
        before="same", after="same",
        before_frames=[{"name": "bodypane", "url": "http://h/members"}],
        after_frames=[{"name": "bodypane", "url": "http://h/members/100244"}]))
    assert checkpoint.kind == "url_matches"
    assert checkpoint.params["pattern"] == r"/members/\d+$"


def test_no_observable_change_yields_no_checkpoint():
    assert _derive_checkpoint(action(before="same", after="same")) is None


def test_distinctive_line_skips_numbers_and_separators():
    text = "100244\n---\nThe member account summary is open\nx"
    assert _distinctive(text, set()) == "The member account summary is open"


def test_slug_is_stable_and_readable():
    assert _slug("Enter the member number to look up") == "enter_member_number_look_up"


# -- catalog --------------------------------------------------------------


def test_tool_schema_exposes_caller_inputs_only():
    """Credentials come from the platform; an agent must not be able to pass them."""
    schema = tool_schema(build_artifact())
    assert set(schema["input_schema"]["properties"]) == {"member_id"}
    assert schema["input_schema"]["required"] == ["member_id"]
    assert schema["input_schema"]["properties"]["member_id"]["pattern"] == "[0-9]{6}"
    assert schema["input_schema"]["additionalProperties"] is False


def test_tool_description_tells_the_agent_about_business_outcomes():
    description = tool_schema(build_artifact())["description"]
    assert "member_not_found" in description
    assert "current_savings_balance" in description


def test_result_schema_enumerates_the_statuses_and_outcomes():
    schema = result_schema(build_artifact())
    assert "business_outcome" in schema["properties"]["status"]["enum"]
    assert "member_not_found" in schema["properties"]["outcome"]["properties"]["name"]["enum"]
    assert "current_savings_balance" in schema["properties"]["outputs"]["properties"]


def test_catalog_lists_the_latest_version_per_capability(tmp_path):
    store = ArtifactStore(tmp_path)
    base = build_artifact()
    store.save(base)
    store.save(base.model_copy(update={"version": "1.1.0", "checksum": None}).with_checksum())
    catalog = CapabilityCatalog(store)
    listed = catalog.list()
    assert len(listed) == 1 and listed[0].version == "1.1.0"
    assert json.dumps(catalog.tool_schemas())


def test_approved_only_filter_hides_drafts(tmp_path):
    store = ArtifactStore(tmp_path)
    store.save(build_artifact())  # status defaults to draft
    assert CapabilityCatalog(store).list(approved_only=True) == []


# -- surface portability --------------------------------------------------


def test_desktop_surface_declares_it_cannot_resolve_dom_hints():
    """The seam is real: a surface that cannot do markup says so up front."""
    from cua.artifact.schema import StrategyKind
    from cua.surface.desktop import DesktopSurface

    caps = DesktopSurface("MeridianCore.exe").capabilities()
    assert StrategyKind.dom_hint not in caps
    assert StrategyKind.role_name in caps and StrategyKind.table_cell in caps


def test_fixture_artifact_steps_all_carry_a_portable_strategy():
    """A capability recorded on the web should not be web-only by accident."""
    from cua.artifact.schema import PORTABLE_STRATEGIES

    artifact = build_artifact()
    plans = [s.target for s in artifact.steps if s.target] + \
            [o.extract.target for o in artifact.outputs]
    for plan in plans:
        assert any(s.kind in PORTABLE_STRATEGIES for s in plan.strategies), plan.description
