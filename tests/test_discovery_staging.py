"""Staging sandbox faults for the probe phase.

Discovery can only record how an application reports a problem if it *sees* the
problem, and the agent cannot cause one — the allowlist denies the fault
endpoint. The platform stages it out of band instead. Some conditions cannot be
true of the same request, so each needs a phase of its own; these tests pin the
normalisation that decides how many phases there are and what is staged for each.
"""

from __future__ import annotations

from cua.cli import _extra_probe_phases, _fault_stager, _probes, _rehearsals

ONE = {"discovery": {"probes": ["p1", "p2"],
                     "fault_rehearsal": {"mode": "app_error", "probe": "break it"}}}
MANY = {"discovery": {"probes": ["p1"],
                      "fault_rehearsal": [{"mode": "app_error", "probe": "break it"},
                                          {"mode": "expire", "probe": "time it out"}]}}
NONE: dict = {"discovery": {"probes": ["p1"]}}


def test_a_single_mapping_keeps_the_original_one_phase_behaviour():
    rehearsals = _rehearsals(ONE)
    assert [r["mode"] for r in rehearsals] == ["app_error"]
    # Its probe joins the base phase rather than getting a phase of its own.
    assert _probes(ONE, rehearsals) == ["p1", "p2", "break it"]
    assert _extra_probe_phases(rehearsals) == []


def test_a_list_gives_each_fault_its_own_phase():
    rehearsals = _rehearsals(MANY)
    assert [r["mode"] for r in rehearsals] == ["app_error", "expire"]
    # The base phase runs clean; each fault is rehearsed separately.
    assert _probes(MANY, rehearsals) == ["p1"]
    assert [p["name"] for p in _extra_probe_phases(rehearsals)] == [
        "probe_app_error", "probe_expire"]


def test_no_rehearsal_declared_stages_nothing():
    rehearsals = _rehearsals(NONE)
    assert rehearsals == []
    assert _probes(NONE, rehearsals) == ["p1"]
    assert _fault_stager(rehearsals, "http://h") is None


def test_entries_without_a_mode_are_ignored():
    spec = {"discovery": {"fault_rehearsal": [{"probe": "no mode here"}, "nonsense"]}}
    assert _rehearsals(spec) == []


def test_the_stager_stages_each_fault_for_its_own_phase_and_clears_the_base_one():
    staged: list[tuple[str, str]] = []

    import cua.cli as cli

    original = cli._set_fault
    cli._set_fault = lambda mode, base_url: staged.append((mode, base_url))
    try:
        stage = _fault_stager(_rehearsals(MANY), "http://h")
        assert stage("goal") is None
        assert staged == []                      # the goal phase is never faulted
        assert stage("probe") is None
        assert staged == [("none", "http://h")]  # the base probe phase runs clean
        assert "app_error" in (stage("probe_app_error") or "")
        assert "expire" in (stage("probe_expire") or "")
    finally:
        cli._set_fault = original

    assert [mode for mode, _ in staged] == ["none", "app_error", "expire"]


def test_a_single_rehearsal_is_staged_for_the_one_probe_phase():
    staged: list[str] = []

    import cua.cli as cli

    original = cli._set_fault
    cli._set_fault = lambda mode, base_url: staged.append(mode)
    try:
        stage = _fault_stager(_rehearsals(ONE), "http://h")
        assert stage("goal") is None
        assert "app_error" in (stage("probe") or "")
    finally:
        cli._set_fault = original

    assert staged == ["app_error"]
