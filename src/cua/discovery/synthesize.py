"""Trajectory -> capability artifact.

The split here is the point of the whole design:

* **Deterministic** — the ordered steps, the targeting strategies, the waits, the
  checkpoints and the risk levels are computed from what the surface actually
  perceived during the run. The model does not get to invent a locator, so the
  same trajectory always synthesizes the same artifact.
* **Model-supplied** — the business meaning: the intent behind each step, which
  values are parameters, which screens are legitimate business outcomes, which
  are recoverable, which are faults. Those are judgements, and they are grounded
  by requiring the model to quote wording it actually saw.

Nothing from the transcript is carried into the artifact except intents and
declared semantics; the transcript itself is referenced by hash.
"""

from __future__ import annotations

import re
from typing import Any

from ..artifact.schema import (
    ActionKind,
    AppProfile,
    CapabilityArtifact,
    ExtractSpec,
    FailureRule,
    InputSpec,
    OutcomeDisposition,
    OutcomeRule,
    OutputSpec,
    Predicate,
    Provenance,
    RecoveryAction,
    RecoveryRule,
    RecoveryThen,
    RiskLevel,
    SafetyBinding,
    Sensitivity,
    Step,
    SurfaceBinding,
    TenantBinding,
    ValueRef,
    ValueType,
    WaitSpec,
    utc_now,
)
from ..locate.candidates import build_target_plan
from ..locate.strategies import _norm
from ..surface.base import Observation
from .loop import DiscoveryResult, RecordedAction

# Wording that means "you are no longer signed in"; such a recovery rule must
# re-run the sign-on steps, not merely dismiss the screen.
_SESSION_EXPIRY = re.compile(r"session (has )?(expired|ended|timed out)|sign on again", re.I)
_SIGNON_MARKERS = ("sign on", "log in", "login", "sign in")


class SynthesisError(RuntimeError):
    pass


def synthesize(
    discovery: DiscoveryResult,
    *,
    capability_id: str,
    name: str,
    version: str,
    description: str,
    input_specs: list[dict[str, Any]],
    tenant_id: str,
    variant: str,
    app_profile: dict[str, str],
    base_url: str,
    allowlist_profile: str,
    transcript_sha256: str,
    transcript_path: str,
    model: str,
    model_calls: int,
    viewport: dict[str, int],
) -> tuple[CapabilityArtifact, list[str]]:
    """Returns the artifact and a list of synthesis notes (things dropped or inferred)."""
    notes: list[str] = []
    goal_actions = [a for a in discovery.actions if a.phase == "goal"]
    if not goal_actions:
        raise SynthesisError("discovery recorded no goal-phase actions; nothing to synthesize")

    inputs = _build_inputs(input_specs, goal_actions, notes)
    outcomes = _build_outcomes(discovery, notes)
    failures = _build_failures(discovery, notes)

    # Any recognized terminal wording short-circuits a wait, so a run that hits
    # an error page fails in milliseconds instead of burning the full timeout.
    terminal_markers = [r.when for r in outcomes] + [r.when for r in failures]

    steps = _build_steps(discovery, goal_actions, base_url, terminal_markers, notes)
    recoveries = _build_recoveries(discovery, steps, notes)
    outputs = _build_outputs(discovery, viewport, notes)
    success = _build_success_condition(steps, outputs)

    risky = [s.id for s in steps if s.risk is RiskLevel.irreversible_write]
    artifact = CapabilityArtifact(
        capability_id=capability_id,
        version=version,
        name=name,
        description=description,
        surface=SurfaceBinding(
            kind="web",
            entry_point="{{ config.base_url }}" + _path_of(discovery.entry_point, base_url),
            app_profile=AppProfile(**app_profile),
            tenant=TenantBinding(tenant_id=tenant_id, variant=variant),
            viewport=viewport,
            required_strategies=sorted(
                {s.kind for step in steps if step.target for s in step.target.strategies}
                | {s.kind for o in outputs for s in o.extract.target.strategies},
                key=lambda k: k.value,
            ),
        ),
        safety=SafetyBinding(
            allowlist_profile=allowlist_profile,
            max_autonomous_risk=RiskLevel.reversible_write,
            steps_requiring_approval=risky,
        ),
        inputs=inputs,
        outputs=outputs,
        steps=steps,
        business_outcomes=outcomes,
        recovery_rules=recoveries,
        failure_rules=failures,
        success_condition=success,
        provenance=Provenance(
            discovery_run_id=discovery.run_id,
            model=model,
            model_calls=model_calls,
            created_at=utc_now(),
            goal=discovery.goal,
            transcript_sha256=transcript_sha256,
            transcript_evidence_path=transcript_path,
            notes="; ".join(notes) or None,
        ),
    )
    return artifact.with_checksum(), notes


# -------------------------------------------------------------------- steps


def _build_steps(discovery: DiscoveryResult, actions: list[RecordedAction], base_url: str,
                 terminal_markers: list[Predicate], notes: list[str]) -> list[Step]:
    steps: list[Step] = []
    first = actions[0]

    entry_checkpoint = _entry_checkpoint(first)
    steps.append(
        Step(
            id="s00_open",
            intent="Open the servicing console entry point",
            action=ActionKind.navigate,
            url="{{ config.base_url }}" + _path_of(discovery.entry_point, base_url),
            risk=RiskLevel.read_only,
            wait=WaitSpec(until=entry_checkpoint, timeout_ms=15_000),
            checkpoint=entry_checkpoint,
            tags=["entry"],
        )
    )

    signon_open = True
    for i, action in enumerate(actions):
        step_id = f"s{i + 1:02d}_{_slug(action.intent)}"
        checkpoint = _derive_checkpoint(action)
        wait_until = _wait_condition(checkpoint, terminal_markers)
        target = None
        if action.node is not None:
            target = build_target_plan(action.node, action.obs_before,
                                       viewport=discovery_viewport(action))
        value = None
        if action.action is ActionKind.type:
            value = (ValueRef(input=action.input_name) if action.input_name
                     else ValueRef(literal=action.literal or ""))
        elif action.action is ActionKind.select:
            value = ValueRef(literal=action.literal or "")

        tags: list[str] = []
        label = (action.node.name if action.node else "").casefold()
        if signon_open:
            tags.append("signon")
            if action.action is ActionKind.click and any(m in label for m in _SIGNON_MARKERS):
                signon_open = False

        if checkpoint is None and action.action in {ActionKind.click, ActionKind.navigate,
                                                    ActionKind.press}:
            checkpoint = _fallback_checkpoint(action)
            if checkpoint is None:
                notes.append(
                    f"step {step_id!r} produced no observable state change; marked optional")
        steps.append(
            Step(
                id=step_id,
                intent=action.intent,
                action=action.action,
                target=target,
                value=value,
                url=("{{ config.base_url }}" + _path_of(action.url, base_url)) if action.url else None,
                key=action.key,
                risk=action.risk,
                wait=WaitSpec(until=wait_until, timeout_ms=15_000),
                checkpoint=checkpoint,
                tags=tags,
                optional=checkpoint is None and action.action in {ActionKind.click,
                                                                 ActionKind.navigate,
                                                                 ActionKind.press},
            )
        )
    return steps


def discovery_viewport(action: RecordedAction) -> dict[str, int]:
    return {"width": 1280, "height": 900}


def _entry_checkpoint(first: RecordedAction) -> Predicate:
    obs = first.obs_before
    if first.node is not None and first.node.name:
        return Predicate(
            kind="element_present",
            params={"strategy": "role_name",
                    "params": {"role": first.node.role, "name": first.node.name}},
            description=f"the {first.node.name!r} control is on screen, "
                        "so the entry screen finished loading",
        )
    marker = _distinctive(obs.text, set())
    return Predicate(kind="text_present", params={"text": marker},
                     description=f"the entry screen shows {marker!r}")


def _derive_checkpoint(action: RecordedAction) -> Predicate | None:
    """Prefer what the model said it expected; fall back to what actually changed."""
    after = action.obs_after
    before = action.obs_before

    if action.expect:
        marker = action.expect.strip().strip('"').strip("'")[:90]
        if marker and _norm(marker) in _norm(after.text):
            return Predicate(
                kind="text_present", params={"text": marker},
                description=f"after this step the screen shows {marker!r}",
            )

    new_marker = _distinctive(after.text, _lines(before.text))
    if new_marker:
        return Predicate(
            kind="text_present", params={"text": new_marker},
            description=f"after this step the screen shows {new_marker!r}, which was not there before",
        )

    changed = _changed_url(before, after)
    if changed:
        return Predicate(
            kind="url_matches", params={"pattern": changed, "scope": "any"},
            description=f"after this step a frame is at a URL matching {changed}",
        )
    return None


def _fallback_checkpoint(action: RecordedAction) -> Predicate | None:
    changed = _changed_url(action.obs_before, action.obs_after)
    if changed:
        return Predicate(kind="url_matches", params={"pattern": changed, "scope": "any"},
                         description=f"a frame is at a URL matching {changed}")
    return None


def _wait_condition(checkpoint: Predicate | None, terminal: list[Predicate]) -> Predicate | None:
    """Wait until the expected state OR any recognized terminal state appears."""
    if checkpoint is None:
        return None
    if not terminal:
        return checkpoint
    return Predicate(
        kind="any_of",
        children=[checkpoint] + terminal,
        description=(checkpoint.description or "the expected state")
        + ", or a recognized business outcome / application error",
    )


# ------------------------------------------------------------------- inputs


def _build_inputs(specs: list[dict[str, Any]], actions: list[RecordedAction],
                  notes: list[str]) -> list[InputSpec]:
    used = {a.input_name for a in actions if a.input_name}
    out: list[InputSpec] = []
    for spec in specs:
        sensitivity = Sensitivity(spec.get("sensitivity", "internal"))
        if spec["name"] not in used:
            notes.append(f"declared input {spec['name']!r} was not used by any recorded step")
        out.append(
            InputSpec(
                name=spec["name"],
                type=ValueType(spec.get("type", "string")),
                required=bool(spec.get("required", True)),
                description=spec.get("description", ""),
                sensitivity=sensitivity,
                pattern=spec.get("pattern"),
                example=spec.get("example") if sensitivity in {Sensitivity.public,
                                                               Sensitivity.internal} else None,
                source="environment" if sensitivity is Sensitivity.secret else "caller",
                env_var=spec.get("env_var"),
            )
        )
    for name in sorted(used - {s["name"] for s in specs}):
        notes.append(f"step bound undeclared input {name!r}; added as a required string")
        out.append(InputSpec(name=name, type=ValueType.string, description="(inferred from the "
                                                                          "discovery run)"))
    return out


# ------------------------------------------------------------------ outputs


def _build_outputs(discovery: DiscoveryResult, viewport: dict[str, int],
                   notes: list[str]) -> list[OutputSpec]:
    out: list[OutputSpec] = []
    for declared in discovery.outputs:
        vtype = ValueType(declared.type if declared.type in {v.value for v in ValueType}
                          else "string")
        transforms = ["strip", "collapse_ws"]
        if vtype in (ValueType.decimal, ValueType.money):
            transforms = ["strip", "money_to_decimal"]
        elif vtype is ValueType.integer:
            transforms = ["strip", "digits_only"]
        plan = build_target_plan(declared.node, declared.observation, viewport=viewport,
                                 purpose="value")
        out.append(
            OutputSpec(
                name=declared.name,
                type=vtype,
                description=declared.description,
                sensitivity=Sensitivity(declared.sensitivity
                                        if declared.sensitivity in {s.value for s in Sensitivity}
                                        else "internal"),
                extract=ExtractSpec(target=plan, attribute="text", transforms=transforms),
            )
        )
    if not out:
        notes.append("discovery declared no outputs; the capability returns success only")
    return out


# -------------------------------------------------------- conditions / rules


def _build_outcomes(discovery: DiscoveryResult, notes: list[str]) -> list[OutcomeRule]:
    out: list[OutcomeRule] = []
    seen: set[str] = set()
    for declared in discovery.outcomes:
        name = _slug(declared.get("name", "outcome"))
        if name in seen:
            continue
        marker = (declared.get("marker_text") or "").strip()
        if not marker:
            notes.append(f"business outcome {name!r} declared without marker text; dropped")
            continue
        if not declared.get("marker_verified"):
            notes.append(
                f"business outcome {name!r} dropped: its marker text was never observed during "
                "the run, so it could not be grounded")
            continue
        seen.add(name)
        disposition = OutcomeDisposition(
            declared.get("disposition", "return_to_caller")
            if declared.get("disposition") in {d.value for d in OutcomeDisposition}
            else "return_to_caller"
        )
        out.append(
            OutcomeRule(
                name=name,
                when=Predicate(kind="text_present", params={"text": marker[:120]},
                               description=f"the screen shows {marker[:60]!r}"),
                disposition=disposition,
                message=declared.get("message", name),
                remediation=declared.get("remediation"),
            )
        )
    return out


def _build_failures(discovery: DiscoveryResult, notes: list[str]) -> list[FailureRule]:
    out: list[FailureRule] = []
    seen: set[str] = set()
    for declared in discovery.failures:
        name = _slug(declared.get("name", "app_error"))
        marker = (declared.get("marker_text") or "").strip()
        if not marker or name in seen:
            continue
        if not declared.get("marker_verified"):
            notes.append(f"failure rule {name!r} dropped: marker text was never observed")
            continue
        seen.add(name)
        out.append(
            FailureRule(
                name=name,
                when=Predicate(kind="text_present", params={"text": marker[:120]},
                               description=f"the screen shows {marker[:60]!r}"),
                error_class=declared.get("error_class", "application_error"),
                message=declared.get("message", name),
            )
        )
    return out


def _build_recoveries(discovery: DiscoveryResult, steps: list[Step],
                      notes: list[str]) -> list[RecoveryRule]:
    from ..artifact.schema import StrategyKind, TargetPlan, TargetStrategy

    out: list[RecoveryRule] = []
    seen: set[str] = set()
    has_signon = any("signon" in s.tags for s in steps)

    for declared in discovery.recoveries:
        name = _slug(declared.get("name", "recovery"))
        marker = (declared.get("marker_text") or "").strip()
        control = (declared.get("control_name") or "").strip()
        if not marker or not control or name in seen:
            continue
        if not declared.get("marker_verified"):
            notes.append(f"recovery rule {name!r} dropped: marker text was never observed")
            continue
        seen.add(name)
        role = declared.get("control_role") or "button"
        plan = TargetPlan(
            description=f"the {control!r} control that clears this screen",
            strategies=[
                TargetStrategy(kind=StrategyKind.role_name, params={"role": role, "name": control},
                               confidence=0.9,
                               rationale="the control is identified by its visible label, which is "
                                         "what the operator clicks to clear the screen"),
                TargetStrategy(kind=StrategyKind.text_anchor,
                               params={"text": control, "match": "contains"}, confidence=0.6,
                               rationale="visible-text fallback if the role is reported differently"),
            ],
            require_unique=False,
        )
        actions = [RecoveryAction(action="click", target=plan)]
        description = declared.get("description", "")
        then = RecoveryThen.retry_step
        resume_from: str | None = None

        # An expired session invalidates everything the flow established, so the
        # right recovery is to restart the flow, not to retry the step that
        # happened to notice.
        if _SESSION_EXPIRY.search(marker) or _SESSION_EXPIRY.search(description):
            if has_signon and steps:
                then = RecoveryThen.resume_from_step
                resume_from = steps[0].id
                actions = []
                description = (description + " Restarts the flow from the entry point, because "
                               "dismissing this screen alone leaves the session unauthenticated.")
            else:
                notes.append(f"recovery rule {name!r} looks like session expiry but no sign-on "
                             "steps were recorded to re-run")

        out.append(
            RecoveryRule(
                name=name,
                when=Predicate(kind="text_present", params={"text": marker[:120]},
                               description=f"the screen shows {marker[:60]!r}"),
                actions=actions,
                then=then,
                resume_from_step=resume_from,
                max_attempts=2,
                description=description,
            )
        )
    return out


def _build_success_condition(steps: list[Step], outputs: list[OutputSpec]) -> Predicate:
    children: list[Predicate] = []
    for spec in outputs:
        primary = spec.extract.target.strategies[0]
        if primary.kind.value == "table_cell":
            children.append(
                Predicate(
                    kind="table_cell_matches",
                    params={"row_label": primary.params.get("row_label", ""),
                            "column": primary.params.get("column", ""),
                            "frame": spec.extract.target.frame},
                    description=f"the source cell for output {spec.name!r} is present and non-empty",
                )
            )
        else:
            children.append(
                Predicate(
                    kind="element_present",
                    params={"strategy": primary.kind.value, "params": primary.params,
                            "frame": spec.extract.target.frame},
                    description=f"the source control for output {spec.name!r} is present",
                )
            )
    final = next((s.checkpoint for s in reversed(steps) if s.checkpoint is not None), None)
    if final is not None:
        children.append(final)
    if not children:  # pragma: no cover - steps always yield at least one checkpoint
        children.append(Predicate(kind="text_absent", params={"text": "SYSTEM ERROR"},
                                  description="no application error on screen"))
    return Predicate(kind="all_of", children=children,
                     description="every declared output is readable on the final screen and the "
                                 "last recorded step's checkpoint still holds")


# ------------------------------------------------------------------- helpers


_STOPWORDS = {"the", "a", "an", "of", "to", "for", "and", "on", "in", "is"}


def _slug(text: str, limit: int = 34) -> str:
    words = re.findall(r"[A-Za-z0-9]+", (text or "step").lower())
    kept = [w for w in words if w not in _STOPWORDS] or words or ["step"]
    return "_".join(kept)[:limit].strip("_")


def _lines(text: str) -> set[str]:
    return {ln.strip() for ln in (text or "").splitlines() if ln.strip()}


def _distinctive(text: str, exclude: set[str]) -> str:
    """Pick a short, stable line that identifies this screen."""
    best = ""
    for line in (text or "").splitlines():
        candidate = line.strip()
        if not (8 <= len(candidate) <= 70) or candidate in exclude:
            continue
        if candidate.replace(" ", "").isdigit() or candidate.startswith("---"):
            continue
        letters = sum(c.isalpha() for c in candidate)
        if letters < len(candidate) * 0.5:
            continue
        if not best or len(candidate) > len(best):
            best = candidate
    return best[:90]


def _changed_url(before: Observation, after: Observation) -> str | None:
    before_urls = {before.url} | {f.get("url", "") for f in before.frames}
    for frame in after.frames:
        url = frame.get("url") or ""
        if url and url not in before_urls:
            return _canonical_path_pattern(url)
    if after.url != before.url:
        return _canonical_path_pattern(after.url)
    return None


def _canonical_path_pattern(url: str) -> str:
    """``/members/100244`` -> ``/members/\\d+$`` so the check is not id-specific.

    This is the small, useful half of cross-tenant canonicalization: a recorded
    URL that embeds a concrete record id becomes a shape, which is what lets the
    same artifact run for a different member — and, with a different host in
    config, for a different tenant.
    """
    path = re.sub(r"^https?://[^/]+", "", url) or "/"
    path = path.split("?")[0]
    segments = [re.sub(r"^\d+$", r"\\d+", re.escape(seg)) for seg in path.split("/")]
    return "/".join(segments) + "$"


def _path_of(url: str | None, base_url: str) -> str:
    if not url:
        return "/"
    if url.startswith(base_url):
        return url[len(base_url):] or "/"
    return re.sub(r"^https?://[^/]+", "", url) or "/"
