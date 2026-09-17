from __future__ import annotations

import pytest
import yaml

from cua.artifact.schema import ActionKind, RiskLevel
from cua.safety.allowlist import AllowlistError, DEFAULT_CONFIG, load_profile
from cua.safety.policy import PolicyGate
from cua.safety.redact import MASK, Redactor


@pytest.fixture
def gate():
    return PolicyGate(load_profile("default"))


# -- allowlist ------------------------------------------------------------


def test_allowed_route_passes(gate):
    ok, why = gate.profile.url_verdict("http://127.0.0.1:8799/members/100244")
    assert ok and "allowed pattern" in why


def test_foreign_origin_is_refused(gate):
    ok, why = gate.profile.url_verdict("https://evil.example.com/members/1")
    assert not ok and "origin" in why


def test_unlisted_route_on_an_allowed_origin_is_refused(gate):
    ok, why = gate.profile.url_verdict("http://127.0.0.1:8799/wire-transfer")
    assert not ok and "no allowed pattern" in why


def test_denied_route_beats_allowed(gate):
    """The fault-injection surface is operator tooling and must stay unreachable."""
    ok, why = gate.profile.url_verdict("http://127.0.0.1:8799/admin/inject?mode=slow")
    assert not ok and "denied pattern" in why


def test_unknown_profile_is_an_explicit_error():
    with pytest.raises(AllowlistError):
        load_profile("does-not-exist")


def test_profile_is_configurable_not_hardcoded(tmp_path):
    data = yaml.safe_load(DEFAULT_CONFIG.read_text())
    data["profiles"]["tighter"] = {
        **data["profiles"]["default"],
        "allowed_origins": ["http://localhost:9999"],
        "max_autonomous_risk": "read_only",
    }
    path = tmp_path / "a.yaml"
    path.write_text(yaml.safe_dump(data))
    profile = load_profile("tighter", path)
    assert profile.url_allowed("http://localhost:9999/members")
    assert not profile.url_allowed("http://127.0.0.1:8799/members")


# -- risk model -----------------------------------------------------------


def test_reading_and_navigating_are_read_only(gate):
    for action in (ActionKind.navigate, ActionKind.extract, ActionKind.wait):
        decision = gate.check(action=action, url="http://127.0.0.1:8799/members")
        assert decision.allowed and decision.risk is RiskLevel.read_only


def test_inquiry_click_is_read_only_and_allowed(gate):
    decision = gate.check(action=ActionKind.click, control_label="Search")
    assert decision.allowed and decision.risk is RiskLevel.read_only


def test_signon_is_a_reversible_write_and_allowed(gate):
    decision = gate.check(action=ActionKind.click, control_label="Sign On")
    assert decision.allowed and decision.risk is RiskLevel.reversible_write


def test_money_moving_click_is_blocked(gate):
    decision = gate.check(action=ActionKind.click, control_label="Transfer Funds")
    assert not decision.allowed
    assert decision.risk is RiskLevel.irreversible_write
    assert decision.violated == "risk_ceiling"


def test_live_control_label_overrides_an_under_declared_risk(gate):
    """An artifact cannot smuggle a money-moving click past the ceiling."""
    decision = gate.check(action=ActionKind.click, control_label="Post Transaction",
                          declared_risk=RiskLevel.read_only)
    assert not decision.allowed
    assert decision.risk is RiskLevel.irreversible_write
    assert any("re-escalated" in s for s in decision.signals)


def test_declared_risk_is_honoured_when_it_is_stricter(gate):
    decision = gate.check(action=ActionKind.click, control_label="Continue",
                          declared_risk=RiskLevel.irreversible_write)
    assert not decision.allowed and decision.risk is RiskLevel.irreversible_write


def test_human_only_control_routes_to_a_person_rather_than_failing(gate):
    decision = gate.check(action=ActionKind.click, control_label="Supervisor Override")
    assert not decision.allowed and decision.requires_human
    assert decision.violated == "human_only"


def test_action_type_outside_the_profile_is_refused(gate):
    profile = gate.profile
    tight = PolicyGate(type(profile)(**{**profile.__dict__,
                                        "allowed_actions": frozenset({"navigate"})}))
    assert not tight.check(action=ActionKind.click, control_label="Search").allowed


def test_landing_url_is_checked_after_the_action(gate):
    assert not gate.check_landing_url("https://elsewhere.example/").allowed
    assert gate.check_landing_url("http://127.0.0.1:8799/members").allowed


# -- redaction ------------------------------------------------------------


def test_registered_values_are_removed_even_when_they_look_ordinary():
    r = Redactor()
    r.register("hunter2-but-ordinary", "passcode")
    assert "hunter2-but-ordinary" not in r.scrub_text("passcode is hunter2-but-ordinary ok")


def test_patterns_catch_data_that_was_never_declared():
    r = Redactor()
    out = r.scrub_text("ssn 123-45-6789 card 4111 1111 1111 1111 mail a@b.co key sk-abcdefghijklmno")
    assert "123-45-6789" not in out
    assert "4111 1111 1111 1111" not in out
    assert "a@b.co" not in out
    assert "sk-abcdefghijklmno" not in out


def test_luhn_check_avoids_masking_ordinary_long_numbers():
    r = Redactor()
    assert f"{MASK}:card" not in r.scrub_text("reference 4111 1111 1111 1112")


def test_sensitive_keys_are_masked_wholesale():
    r = Redactor()
    out = r.scrub({"opsecret": "anything", "api_key": "xyz", "member": "ok"})
    assert out["opsecret"] == MASK and out["api_key"] == MASK and out["member"] == "ok"


def test_scrub_walks_nested_structures():
    r = Redactor()
    r.register("Dana Whitfield", "member_name")
    out = r.scrub({"steps": [{"observed": "member Dana Whitfield"}]})
    assert "Dana Whitfield" not in str(out)


def test_longest_registration_wins_so_overlaps_do_not_leak():
    r = Redactor()
    r.register("secret", "short")
    r.register("supersecretvalue", "long")
    assert "secret" not in r.scrub_text("supersecretvalue")
