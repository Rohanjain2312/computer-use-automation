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


# -- checkpoints must describe the screen, not the record -----------------


def test_a_checkpoint_never_carries_the_run_s_own_data():
    """Otherwise the artifact leaks PII and only ever replays for one member."""
    volatile = {"100244", "Dana Whitfield", "$4,812.37"}
    checkpoint = _derive_checkpoint(
        action(before="Member Lookup",
               after="Member Record\nMember Name Dana Whitfield Member Since 2014-03-19\n"
                     "Account Detail"),
        volatile=volatile)
    assert checkpoint is not None
    assert "Dana Whitfield" not in checkpoint.params["text"]
    assert checkpoint.params["text"] == "Account Detail"


def test_a_model_expectation_containing_data_is_also_rejected():
    checkpoint = _derive_checkpoint(
        action(expect="Member Name Dana Whitfield",
               before="Member Lookup", after="Member Name Dana Whitfield\nAccount Detail"),
        volatile={"Dana Whitfield"})
    assert "Dana Whitfield" not in (checkpoint.params.get("text") or "")


def test_distinctive_prefers_a_screen_label_over_a_data_row():
    text = "Account Summary\nMember Since 2014-03-19 Branch 014 Opened 2019-11-04"
    assert _distinctive(text, set()) == "Account Summary"


# -- detectors must generalise beyond the record that was probed ----------


def test_a_marker_carrying_probe_data_is_trimmed_to_generalisable_wording():
    from cua.discovery.synthesize import _ground_marker

    marker = _ground_marker(
        "No member matching that number was found. Searched member number: 999999",
        {"999999"})
    assert marker == "No member matching that number was found."


def test_a_marker_that_is_entirely_record_specific_is_dropped():
    from cua.discovery.synthesize import _ground_marker

    assert _ground_marker("Member 100999 is restricted", {"100999"}) == ""


def test_a_clean_marker_is_left_alone():
    from cua.discovery.synthesize import _ground_marker

    text = "You do not have permission to view this member."
    assert _ground_marker(text, {"100999"}) == text


def test_messages_do_not_name_the_record_the_recording_used():
    from cua.discovery.synthesize import _sanitize_message

    out = _sanitize_message("Member 100999 is flagged EXECUTIVE SERVICES.", {"100999"})
    assert "100999" not in out and "EXECUTIVE SERVICES" in out


def test_removing_a_record_id_takes_its_label_with_it():
    """Otherwise the message reads 'Member the requested record is flagged'."""
    from cua.discovery.synthesize import _sanitize_message

    out = _sanitize_message("Member 100999 is flagged EXECUTIVE SERVICES.", {"100999"})
    assert out == "The requested record is flagged EXECUTIVE SERVICES." or \
           out == "the requested record is flagged EXECUTIVE SERVICES."
    assert "Member the requested record" not in out


def test_a_bare_identifier_with_no_label_is_still_replaced():
    from cua.discovery.synthesize import _sanitize_message

    out = _sanitize_message("Searched member number: 999999", {"999999"})
    assert "999999" not in out and "the requested record" in out


def test_sanitising_leaves_wording_that_carries_no_record_alone():
    from cua.discovery.synthesize import _sanitize_message

    text = "The host transaction could not be completed."
    assert _sanitize_message(text, {"100999"}) == text


def test_a_value_target_is_described_without_its_value():
    """The reviewer should read 'the cell beside Member Name', not the name itself."""
    from cua.locate.candidates import build_target_plan

    nodes = [node("k", "cell", "Member Name"),
             node("v", "cell", "Dana Whitfield", label_left="Member Name",
                  table={"headers": ["a", "b"], "row_index": 1, "col_index": 1,
                         "row_label": "Member Name", "col_header": "b",
                         "header_confident": False})]
    plan = build_target_plan(nodes[1], observation(nodes), purpose="value")
    assert "Dana Whitfield" not in plan.description
    assert "Member Name" in plan.description


# -- steps that need a person --------------------------------------------


def _synthesize(actions):
    from cua.discovery.loop import DiscoveryResult
    from cua.discovery.synthesize import synthesize

    result = DiscoveryResult(
        run_id="disc_test", goal="record a stop payment", success=True,
        stop_reason="finished", summary="ok", actions=actions,
        entry_point="http://h/", model="test-model", model_calls=1,
    )
    artifact, _notes = synthesize(
        result, capability_id="cap", name="Cap", version="1.0.0", description="d",
        input_specs=[], tenant_id="t", variant="base",
        app_profile={"vendor": "v", "product": "p"}, base_url="http://h",
        allowlist_profile="default", transcript_sha256="0" * 64,
        transcript_path="evidence/x", model="test-model", model_calls=1,
        viewport={"width": 1280, "height": 900},
    )
    return artifact


def _click(index, intent, control, *, requires_human=False, risk=RiskLevel.read_only,
           before="Before", after="After"):
    from cua.discovery.loop import RecordedAction

    return RecordedAction(
        index=index, phase="goal", tool="click", action=ActionKind.click, intent=intent,
        expect=after, node=node(f"n{index}", "button", control),
        obs_before=observation([node(f"n{index}", "button", control)], text=before),
        obs_after=observation([], text=after),
        risk=risk, requires_human=requires_human)


def test_a_step_the_gate_refused_to_automation_is_listed_as_needing_approval():
    """The committing click is recorded, and the artifact says it needs a person."""
    artifact = _synthesize([
        _click(0, "open the request form", "Stop Payment",
               before="Member Record", after="Stop Payment Request"),
        _click(1, "record the stop payment", "Place Stop Payment", requires_human=True,
               risk=RiskLevel.reversible_write,
               before="Stop Payment Request", after="Stop Payment Recorded"),
    ])

    approval = artifact.safety.steps_requiring_approval
    assert len(approval) == 1
    needs = next(s for s in artifact.steps if s.id in approval)
    assert needs.intent == "record the stop payment"
    assert "needs_human" in needs.tags


def test_an_ordinary_step_is_not_listed_as_needing_approval():
    artifact = _synthesize([
        _click(0, "open the request form", "Stop Payment",
               before="Member Record", after="Stop Payment Request"),
    ])
    assert artifact.safety.steps_requiring_approval == []
