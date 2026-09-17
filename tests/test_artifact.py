"""Schema, templating, validation and store: the artifact contract itself."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cua.artifact.schema import (
    ActionKind,
    InputSpec,
    Predicate,
    RiskLevel,
    Sensitivity,
    Step,
    StrategyKind,
    TargetPlan,
    TargetStrategy,
    ValueRef,
    ValueType,
)
from cua.artifact.store import ArtifactStore
from cua.artifact.templating import (
    BindingError,
    InputValidationError,
    apply_transforms,
    bind_inputs,
    coerce,
    render_template,
    render_value,
)
from cua.artifact.validate import errors, validate_artifact
from fixture_artifact import build_artifact


# -- schema ---------------------------------------------------------------


def test_strategies_must_be_ordered_by_confidence():
    with pytest.raises(ValidationError):
        TargetPlan(description="x", strategies=[
            TargetStrategy(kind=StrategyKind.role_name, params={}, confidence=0.4, rationale="a"),
            TargetStrategy(kind=StrategyKind.text_anchor, params={}, confidence=0.9, rationale="b"),
        ])


def test_value_ref_must_bind_exactly_one_source():
    with pytest.raises(ValidationError):
        ValueRef(literal="x", input="y")
    with pytest.raises(ValidationError):
        ValueRef()


def test_secret_inputs_must_come_from_the_environment():
    with pytest.raises(ValidationError):
        InputSpec(name="pw", type=ValueType.string, description="d",
                  sensitivity=Sensitivity.secret)


def test_secret_inputs_may_not_carry_an_example_value():
    with pytest.raises(ValidationError):
        InputSpec(name="pw", type=ValueType.string, description="d",
                  sensitivity=Sensitivity.secret, source="environment", env_var="PW",
                  example="letmein")


def test_actions_require_the_fields_they_need():
    with pytest.raises(ValidationError):
        Step(id="s", intent="i", action=ActionKind.click)  # no target
    with pytest.raises(ValidationError):
        Step(id="s", intent="i", action=ActionKind.navigate)  # no url


def test_composite_predicates_require_children():
    with pytest.raises(ValidationError):
        Predicate(kind="all_of")


def test_checksum_detects_post_hoc_edits():
    artifact = build_artifact()
    assert artifact.checksum_valid()
    tampered = artifact.model_copy(update={"description": "something else"})
    assert not tampered.checksum_valid()


def test_artifact_round_trips_through_json():
    artifact = build_artifact()
    restored = type(artifact).model_validate(json.loads(json.dumps(artifact.model_dump(mode="json"))))
    assert restored.checksum == artifact.checksum
    assert restored.steps[0].id == artifact.steps[0].id


def test_artifact_never_contains_the_raw_transcript():
    artifact = build_artifact()
    assert artifact.provenance.transcript_sha256
    assert "messages" not in artifact.model_dump(mode="json")


# -- templating -----------------------------------------------------------


def test_placeholders_are_limited_to_two_namespaces():
    assert render_template("{{ config.base_url }}/x", {}, {"base_url": "http://h"}) == "http://h/x"
    assert render_template("{{ inputs.a }}", {"a": "1"}, {}) == "1"
    # Anything else is left alone rather than evaluated.
    assert render_template("{{ os.system('x') }}", {}, {}) == "{{ os.system('x') }}"


def test_unbound_placeholder_is_an_error():
    with pytest.raises(BindingError):
        render_template("{{ inputs.missing }}", {}, {})


def test_value_ref_rendering():
    assert render_value(ValueRef(input="m"), {"m": "100244"}, {}) == "100244"
    assert render_value(ValueRef(literal="SUM"), {}, {}) == "SUM"


def test_money_transform_then_decimal_coercion():
    assert apply_transforms("  $4,812.37 ", ["strip", "money_to_decimal"]) == "4812.37"
    assert coerce("4812.37", ValueType.decimal) == Decimal("4812.37")


def test_transform_failure_is_a_clear_message():
    with pytest.raises(ValueError, match="no monetary amount"):
        apply_transforms("not money", ["money_to_decimal"])


def test_input_pattern_is_enforced_before_the_run_starts():
    specs = [InputSpec(name="member_id", type=ValueType.string, description="d",
                       pattern="[0-9]{6}")]
    with pytest.raises(InputValidationError, match="pattern"):
        bind_inputs(specs, {"member_id": "abc"}, {})
    bound, _ = bind_inputs(specs, {"member_id": "100244"}, {})
    assert bound["member_id"] == "100244"


def test_unknown_inputs_are_rejected_rather_than_ignored():
    specs = [InputSpec(name="member_id", type=ValueType.string, description="d")]
    with pytest.raises(InputValidationError, match="unknown input"):
        bind_inputs(specs, {"member_id": "1", "typo": "x"}, {})


def test_secrets_are_read_from_the_environment_and_reported_for_redaction():
    specs = [InputSpec(name="pw", type=ValueType.string, description="d",
                       sensitivity=Sensitivity.secret, source="environment", env_var="PW")]
    bound, secrets = bind_inputs(specs, {}, {"PW": "s3cret-value"})
    assert bound["pw"] == "s3cret-value" and "s3cret-value" in secrets


def test_missing_secret_environment_variable_is_a_clear_error():
    specs = [InputSpec(name="pw", type=ValueType.string, description="d",
                       sensitivity=Sensitivity.secret, source="environment", env_var="PW")]
    with pytest.raises(InputValidationError, match="environment variable"):
        bind_inputs(specs, {}, {})


# -- validation -----------------------------------------------------------


def test_reference_artifact_has_no_validation_errors():
    assert errors(validate_artifact(build_artifact())) == []


def test_a_state_changing_step_without_a_checkpoint_is_an_error():
    artifact = build_artifact()
    steps = list(artifact.steps)
    steps[3] = steps[3].model_copy(update={"checkpoint": None})
    broken = artifact.model_copy(update={"steps": steps})
    assert any(i.code == "missing_checkpoint" for i in errors(validate_artifact(broken)))


def test_a_literal_that_looks_like_sensitive_data_is_rejected():
    artifact = build_artifact()
    steps = list(artifact.steps)
    steps[5] = steps[5].model_copy(update={"value": ValueRef(literal="123-45-6789")})
    broken = artifact.model_copy(update={"steps": steps})
    assert any(i.code == "sensitive_literal" for i in errors(validate_artifact(broken)))


def test_a_credential_shaped_literal_is_rejected():
    artifact = build_artifact()
    steps = list(artifact.steps)
    steps[5] = steps[5].model_copy(update={"value": ValueRef(literal="my-password-is-abc")})
    broken = artifact.model_copy(update={"steps": steps})
    assert any(i.code == "credential_literal" for i in errors(validate_artifact(broken)))


def test_a_step_binding_an_undeclared_input_is_rejected():
    artifact = build_artifact()
    steps = list(artifact.steps)
    steps[5] = steps[5].model_copy(update={"value": ValueRef(input="nope")})
    broken = artifact.model_copy(update={"steps": steps})
    assert any(i.code == "unknown_input_ref" for i in errors(validate_artifact(broken)))


def test_a_risky_step_without_approval_is_rejected():
    artifact = build_artifact()
    steps = list(artifact.steps)
    steps[3] = steps[3].model_copy(update={"risk": RiskLevel.irreversible_write})
    broken = artifact.model_copy(update={"steps": steps})
    assert any(i.code == "unapproved_risky_step" for i in errors(validate_artifact(broken)))


def test_a_modified_artifact_fails_its_checksum():
    artifact = build_artifact()
    broken = artifact.model_copy(update={"name": "tampered"})
    assert any(i.code == "checksum_mismatch" for i in errors(validate_artifact(broken)))


def test_a_recovery_rule_pointing_at_a_missing_step_is_rejected():
    artifact = build_artifact()
    rules = list(artifact.recovery_rules)
    rules[1] = rules[1].model_copy(update={"resume_from_step": "s99_nope"})
    broken = artifact.model_copy(update={"recovery_rules": rules})
    assert any(i.code == "unknown_resume_step" for i in errors(validate_artifact(broken)))


# -- store ----------------------------------------------------------------


def test_store_round_trip_and_catalog_index(tmp_path):
    store = ArtifactStore(tmp_path)
    artifact = build_artifact()
    path = store.save(artifact)
    assert path.exists()
    loaded = store.get(artifact.capability_id)
    assert loaded.checksum == artifact.checksum and loaded.checksum_valid()
    index = json.loads((tmp_path / "index.json").read_text())
    entry = index["capabilities"][0]
    assert entry["capability_id"] == artifact.capability_id
    assert [o["name"] for o in entry["outputs"]] == [o.name for o in artifact.outputs]


def test_store_refuses_to_save_an_invalid_artifact(tmp_path):
    from cua.artifact.validate import ArtifactInvalid

    artifact = build_artifact()
    steps = list(artifact.steps)
    steps[3] = steps[3].model_copy(update={"checkpoint": None})
    with pytest.raises(ArtifactInvalid):
        ArtifactStore(tmp_path).save(artifact.model_copy(update={"steps": steps}))
