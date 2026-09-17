"""Artifact validation beyond what the type system can express.

Two jobs: keep a bad artifact out of the store, and keep sensitive data out of
an artifact. Both run before an artifact is written and again before it is
replayed, so a hand-edited artifact is checked too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..safety.redact import Redactor
from .schema import ActionKind, CapabilityArtifact, RISK_ORDER, RiskLevel, Sensitivity, StrategyKind


@dataclass(frozen=True)
class Issue:
    level: str  # "error" | "warning"
    code: str
    message: str
    location: str = ""

    def __str__(self) -> str:
        where = f" [{self.location}]" if self.location else ""
        return f"{self.level.upper()} {self.code}{where}: {self.message}"


def _literals(artifact: CapabilityArtifact) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for step in artifact.steps:
        if step.value is not None and step.value.literal is not None:
            out.append((f"steps.{step.id}.value", step.value.literal))
        if step.url:
            out.append((f"steps.{step.id}.url", step.url))
    for rule in artifact.recovery_rules:
        for i, action in enumerate(rule.actions):
            if action.value is not None and action.value.literal is not None:
                out.append((f"recovery.{rule.name}.actions[{i}].value", action.value.literal))
    return out


def validate_artifact(artifact: CapabilityArtifact) -> list[Issue]:
    issues: list[Issue] = []
    add = issues.append
    input_names = {i.name for i in artifact.inputs}

    # -- structure --------------------------------------------------------
    ids = [s.id for s in artifact.steps]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        add(Issue("error", "duplicate_step_id", f"step ids repeat: {sorted(dupes)}", "steps"))

    for step in artifact.steps:
        for ref, where in ((step.value, "value"),):
            if ref is not None and ref.input is not None and ref.input not in input_names:
                add(Issue("error", "unknown_input_ref",
                          f"step {step.id!r} binds undeclared input {ref.input!r}", f"steps.{step.id}.{where}"))
        if step.checkpoint is None and not step.optional and step.action in {
            ActionKind.click, ActionKind.navigate, ActionKind.press
        }:
            add(Issue("error", "missing_checkpoint",
                      f"step {step.id!r} ({step.action.value}) changes screen state but declares no "
                      "checkpoint; replay would assume the action worked", f"steps.{step.id}"))
        if step.checkpoint is None and step.action in {ActionKind.type, ActionKind.select}:
            add(Issue("warning", "unverified_input",
                      f"step {step.id!r} enters a value without verifying it landed", f"steps.{step.id}"))
        if step.target is not None:
            portable = [s for s in step.target.strategies if s.kind in
                        {StrategyKind.role_name, StrategyKind.label_proximity,
                         StrategyKind.text_anchor, StrategyKind.table_cell}]
            if not portable:
                add(Issue("warning", "no_portable_strategy",
                          f"step {step.id!r} relies only on markup/position strategies and will not "
                          "port to another surface", f"steps.{step.id}.target"))

    for out in artifact.outputs:
        portable = [s for s in out.extract.target.strategies if s.kind != StrategyKind.viewport_ratio]
        if not portable:
            add(Issue("warning", "fragile_extraction",
                      f"output {out.name!r} is extracted positionally only", f"outputs.{out.name}"))

    for bad in set(artifact.safety.steps_requiring_approval) - set(ids):
        add(Issue("error", "unknown_approval_step",
                  f"safety.steps_requiring_approval names unknown step {bad!r}", "safety"))

    tags = {t for s in artifact.steps for t in s.tags}
    for rule in artifact.recovery_rules:
        if rule.resume_from_step and rule.resume_from_step not in ids:
            add(Issue("error", "unknown_resume_step",
                      f"recovery rule {rule.name!r} resumes from unknown step "
                      f"{rule.resume_from_step!r}", f"recovery.{rule.name}"))
        for i, action in enumerate(rule.actions):
            if action.action == "rerun_tagged_steps":
                if not action.tag:
                    add(Issue("error", "missing_tag",
                              f"recovery rule {rule.name!r} rerun action has no tag",
                              f"recovery.{rule.name}"))
                elif action.tag not in tags:
                    add(Issue("error", "unknown_tag",
                              f"recovery rule {rule.name!r} reruns tag {action.tag!r}, which no step carries",
                              f"recovery.{rule.name}"))
            if action.action in {"click", "type"} and action.target is None:
                add(Issue("error", "missing_target",
                          f"recovery rule {rule.name!r} action[{i}] needs a target",
                          f"recovery.{rule.name}"))

    names = [r.name for r in artifact.business_outcomes] + [r.name for r in artifact.failure_rules]
    dup_rules = {n for n in names if names.count(n) > 1}
    if dup_rules:
        add(Issue("error", "duplicate_rule_name", f"rule names repeat: {sorted(dup_rules)}", "rules"))

    # -- safety -----------------------------------------------------------
    ceiling = RiskLevel(artifact.safety.max_autonomous_risk)
    for step in artifact.steps:
        if RISK_ORDER[step.risk] > RISK_ORDER[ceiling] and step.id not in artifact.safety.steps_requiring_approval:
            add(Issue("error", "unapproved_risky_step",
                      f"step {step.id!r} is {step.risk.value} which exceeds the artifact ceiling "
                      f"{ceiling.value} but is not listed in safety.steps_requiring_approval",
                      f"steps.{step.id}"))

    for spec in artifact.inputs:
        if spec.sensitivity is Sensitivity.secret and spec.source != "environment":
            add(Issue("error", "secret_not_from_env",
                      f"secret input {spec.name!r} must come from the environment", f"inputs.{spec.name}"))

    # -- no sensitive literals -------------------------------------------
    probe = Redactor()
    for location, literal in _literals(artifact):
        scrubbed = probe.scrub_text(literal)
        if scrubbed != literal:
            add(Issue("error", "sensitive_literal",
                      "artifact stores a literal that matches a sensitive-data pattern "
                      f"(redacts to {scrubbed!r}); bind it to a declared input instead", location))
        if re.search(r"(passw|passcode|secret|token|api[_-]?key)", literal, re.I):
            add(Issue("error", "credential_literal",
                      "artifact stores a credential-looking literal", location))

    for spec in artifact.inputs:
        if spec.sensitivity in {Sensitivity.secret, Sensitivity.pii} and spec.example:
            add(Issue("error", "sensitive_example",
                      f"input {spec.name!r} is {spec.sensitivity.value} and must not carry an example",
                      f"inputs.{spec.name}"))

    # -- provenance -------------------------------------------------------
    if artifact.checksum and not artifact.checksum_valid():
        add(Issue("error", "checksum_mismatch",
                  "artifact checksum does not match its content; it was modified after signing",
                  "checksum"))
    if not artifact.provenance.transcript_sha256:
        add(Issue("warning", "no_transcript_hash",
                  "artifact does not reference the discovery transcript it came from", "provenance"))

    return issues


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == "error"]


class ArtifactInvalid(ValueError):
    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("artifact validation failed:\n  " + "\n  ".join(str(i) for i in issues))
