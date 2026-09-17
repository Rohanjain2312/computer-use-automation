"""Agent-facing capability catalog.

Saved artifacts are exposed as a catalog of callable tools: discoverable by
name, typed arguments in, typed results out. This is the shape an AI agent
actually consumes — the same JSON-schema tool definition it would get for any
other tool — which is the point of recording capabilities in the first place.

Inputs sourced from the environment (credentials) are deliberately omitted from
the tool schema: the calling agent must not be able to pass them, and should not
know them.
"""

from __future__ import annotations

from typing import Any

from ..artifact.schema import CapabilityArtifact, Sensitivity, ValueType
from ..artifact.store import ArtifactStore

_JSON_TYPE = {
    ValueType.string: "string",
    ValueType.enum: "string",
    ValueType.date: "string",
    ValueType.money: "string",
    ValueType.decimal: "string",
    ValueType.integer: "integer",
    ValueType.boolean: "boolean",
}


def tool_schema(artifact: CapabilityArtifact) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for spec in artifact.inputs:
        if spec.source == "environment":
            continue
        prop: dict[str, Any] = {
            "type": _JSON_TYPE.get(spec.type, "string"),
            "description": spec.description,
        }
        if spec.enum_values:
            prop["enum"] = spec.enum_values
        if spec.pattern:
            prop["pattern"] = spec.pattern
        if spec.example and spec.sensitivity in {Sensitivity.public, Sensitivity.internal}:
            prop["examples"] = [spec.example]
        properties[spec.name] = prop
        if spec.required:
            required.append(spec.name)

    outcomes = ", ".join(o.name for o in artifact.business_outcomes)
    returns = ", ".join(f"{o.name} ({o.type.value})" for o in artifact.outputs)
    description = (
        f"{artifact.description}\n\n"
        f"Returns on success: {returns or 'no outputs'}.\n"
        f"May instead return status='business_outcome' with one of: {outcomes or 'none'}.\n"
        f"Surface: {artifact.surface.app_profile.vendor} {artifact.surface.app_profile.product}. "
        f"Artifact status: {artifact.status.value}, version {artifact.version}."
    )
    return {
        "name": artifact.capability_id,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def result_schema(artifact: CapabilityArtifact) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string",
                       "enum": ["success", "business_outcome", "invalid_input",
                                "blocked_by_policy", "failure"]},
            "outputs": {
                "type": "object",
                "properties": {
                    o.name: {"type": _JSON_TYPE.get(o.type, "string"), "description": o.description}
                    for o in artifact.outputs
                },
            },
            "outcome": {"type": "object",
                        "properties": {"name": {"type": "string",
                                                "enum": [o.name for o in artifact.business_outcomes]},
                                       "message": {"type": "string"}}},
            "failure": {"type": "object",
                        "properties": {"error_class": {"type": "string"},
                                       "step_id": {"type": "string"},
                                       "expected": {"type": "string"},
                                       "observed": {"type": "string"}}},
        },
        "required": ["status"],
    }


class CapabilityCatalog:
    def __init__(self, store: ArtifactStore | None = None) -> None:
        self.store = store or ArtifactStore()

    def list(self, *, approved_only: bool = False) -> list[CapabilityArtifact]:
        latest: dict[str, CapabilityArtifact] = {}
        for artifact in self.store.list_all():
            latest[artifact.capability_id] = artifact
        out = list(latest.values())
        if approved_only:
            out = [a for a in out if a.status.value == "approved"]
        return sorted(out, key=lambda a: a.capability_id)

    def get(self, capability_id: str, version: str | None = None) -> CapabilityArtifact:
        return self.store.get(capability_id, version)

    def tool_schemas(self, *, approved_only: bool = False) -> list[dict[str, Any]]:
        return [tool_schema(a) for a in self.list(approved_only=approved_only)]
