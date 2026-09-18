"""Command line entry point.

Four verbs matter: ``discover`` (once, with a model), ``replay`` (many times,
without one), ``catalog`` (what an agent can call) and ``invoke`` (call it).
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import typer
import yaml

from .artifact.schema import ArtifactStatus, CapabilityArtifact, Sensitivity, utc_now
from .artifact.store import ArtifactStore
from .artifact.templating import bind_inputs
from .artifact.validate import validate_artifact
from .catalog.tools import CapabilityCatalog, result_schema, tool_schema
from .handoff.console import OperatorConsole
from .handoff.control import SessionControl
from .handoff.operators import ConsoleOperator, ScriptedOperator
from .observability.evidence import REPO_ROOT, EvidenceWriter
from .observability.logging import RunLogger
from .safety.allowlist import load_profile
from .safety.policy import PolicyGate
from .safety.redact import Redactor
from .surface.web import PlaywrightWebSurface, wait_for_app

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Computer-use automation: discover once with a model, replay forever without one.")
catalog_app = typer.Typer(no_args_is_help=True, help="Capabilities an AI agent can call.")
app.add_typer(catalog_app, name="catalog")

DEFAULT_BASE_URL = os.environ.get("MERIDIAN_BASE_URL", "http://127.0.0.1:8799")

# What the scripted stand-in operator does when a run parks for a human.
#
# The supervisor override code comes from the environment, never from here: the
# operator's credential is not in the repository, the artifact, or the logs.
#
# One list covering every block the capabilities in this repo can raise, rather
# than a script per capability. ``ScriptedOperator._perform`` skips an entry
# whose control is not on the parked screen and logs that it did, so the
# entitlement remediation is a no-op on the stop-payment screen and vice versa.
# A real deployment would route the request to a person, who needs no script at
# all; this exists so the same seam can run headless in CI and in evidence runs.
OPERATOR_SCRIPT = [
    # Entitlement block: a supervisor clears the executive-services flag.
    {"do": "click", "role": "button", "name": "Supervisor Override"},
    {"do": "type", "role": "textbox", "name": "Override Code", "env": "MERIDIAN_OVERRIDE_CODE"},
    {"do": "click", "role": "button", "name": "Apply Override"},
    # Human-only commit: the operator, not the automation, records the request.
    {"do": "click", "role": "button", "name": "Place Stop Payment"},
]


def _load_dotenv() -> None:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _run_id(prefix: str) -> str:
    return f"{prefix}_{utc_now().replace(':', '').replace('-', '')}_{uuid.uuid4().hex[:6]}"


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise typer.BadParameter(f"--input expects name=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key.strip()] = value
    return out


def _make_operator(mode: str, control: SessionControl, script: list[dict] | None,
                   console_port: int) -> tuple[Any, OperatorConsole | None]:
    if mode == "none":
        return None, None
    if mode == "scripted":
        return ScriptedOperator(script or OPERATOR_SCRIPT), None
    console = OperatorConsole(control, port=console_port)
    try:
        console.start()
    except OSError:
        # A leftover console, or anything else on that port, should not sink the
        # run the operator is standing in front of. Take any free port and say so.
        console = OperatorConsole(control, port=0)
        console.start()
        typer.secho(f"  port {console_port} was busy; using {console.port} instead",
                    fg=typer.colors.YELLOW)
    typer.secho(f"  operator console: {console.url}", fg=typer.colors.CYAN, bold=True)
    return ConsoleOperator(console), console


# ---------------------------------------------------------------- serve-app


@app.command("serve-app")
def serve_app(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8799),
) -> None:
    """Run the MeridianCore mock legacy servicing console."""
    sys.path.insert(0, str(REPO_ROOT))
    from mockapp.app import create_app

    typer.secho(f"MeridianCore mock console on http://{host}:{port}/", fg=typer.colors.GREEN)
    create_app().run(host=host, port=port, threaded=True)


# ----------------------------------------------------------------- discover


@app.command()
def discover(
    task: Path = typer.Option(..., "--task", "-t", help="Discovery task YAML."),
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url"),
    model: str = typer.Option(None, "--model", help="Anthropic model id."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window."),
    profile: str = typer.Option("default", "--allowlist-profile"),
    operator: str = typer.Option("scripted", "--operator",
                                 help="scripted | console | none — who answers an escalation."),
    console_port: int = typer.Option(8811, "--console-port"),
    probe: bool = typer.Option(True, "--probe/--no-probe",
                               help="Also investigate how the app reports failures."),
    approve: bool = typer.Option(True, "--approve/--no-approve",
                                 help="Smoke-replay the artifact and promote draft -> approved."),
    out_version: str = typer.Option(None, "--version", help="Override the artifact version."),
) -> None:
    """Run a real LLM-driven discovery run and save a capability artifact."""
    _load_dotenv()
    from .discovery.llm import LlmClient
    from .discovery.loop import DiscoveryAgent
    from .discovery.synthesize import synthesize

    spec = yaml.safe_load(Path(task).read_text())
    allowlist = load_profile(profile)
    gate = PolicyGate(allowlist)
    run_id = _run_id("disc")
    redactor = Redactor()

    entry_point = base_url.rstrip("/") + (spec.get("entry_point") or "/")
    if not wait_for_app(base_url.rstrip("/") + "/healthz"):
        typer.secho(f"the target application is not answering at {base_url} — "
                    "start it with `cua serve-app`", fg=typer.colors.RED)
        raise typer.Exit(2)

    input_specs = list(spec.get("inputs") or [])
    supplied = dict((spec.get("discovery") or {}).get("values") or {})
    try:
        bound, secrets = bind_inputs(
            [_as_input_spec(s) for s in input_specs], supplied, dict(os.environ)
        )
    except Exception as exc:
        typer.secho(f"input binding failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(2)
    for value in secrets:
        redactor.register(value, "input")

    rehearsals = _rehearsals(spec)
    evidence = EvidenceWriter("discovery", run_id, redactor)
    logger = RunLogger(evidence.log_path, run_id=run_id, phase="discovery", redactor=redactor)
    control = SessionControl(run_id=run_id)
    op, console = _make_operator(operator, control, OPERATOR_SCRIPT, console_port)

    typer.secho(f"\ndiscovery run {run_id}", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  goal        : {spec['goal'].strip()[:150]}")
    typer.echo(f"  entry point : {entry_point}")
    typer.echo(f"  evidence    : {evidence.rel(evidence.root)}\n")

    surface = PlaywrightWebSurface(headless=not headed,
                                   viewport=spec.get("viewport") or {"width": 1280, "height": 900})
    try:
        llm = LlmClient(model=model, redactor=redactor, evidence=evidence)
        agent = DiscoveryAgent(
            goal=spec["goal"].strip(), entry_point=entry_point, inputs=bound,
            input_specs=input_specs, output_hints=list(spec.get("outputs") or []),
            surface=surface, gate=gate, llm=llm, logger=logger, evidence=evidence,
            control=control, redactor=redactor, operator=op,
            probes=_probes(spec, rehearsals) if probe else [],
            extra_probe_phases=_extra_probe_phases(rehearsals) if probe else [],
            on_phase_start=_fault_stager(rehearsals, base_url) if probe else None,
            run_id=run_id,
        )
        result = agent.run()
        evidence.write_json("discovery_summary.json", {
            "run_id": run_id, "goal": result.goal, "success": result.success,
            "stop_reason": result.stop_reason, "summary": result.summary,
            "model": result.model, "model_calls": result.model_calls, "usage": result.usage,
            "actions": [{"index": a.index, "phase": a.phase, "action": a.action.value,
                         "intent": a.intent, "expect": a.expect,
                         "target": a.node.name if a.node else a.url,
                         "risk": a.risk.value, "evidence": a.evidence}
                        for a in result.actions],
            "declared_outputs": [{"name": o.name, "type": o.type,
                                  "sensitivity": o.sensitivity} for o in result.outputs],
            "declared_outcomes": result.outcomes,
            "declared_recoveries": result.recoveries,
            "declared_failures": result.failures,
            "escalations": result.escalations,
            "control_log": control.as_dict(),
        })

        if not result.success:
            typer.secho(f"\ndiscovery did not complete: {result.stop_reason} — {result.summary}",
                        fg=typer.colors.RED)
            raise typer.Exit(1)

        artifact, notes = synthesize(
            result,
            capability_id=spec["capability_id"], name=spec["name"],
            version=out_version or spec.get("version", "1.0.0"),
            description=spec["description"].strip(), input_specs=input_specs,
            tenant_id=(spec.get("tenant") or {}).get("tenant_id", "default"),
            variant=(spec.get("tenant") or {}).get("variant", "base"),
            app_profile=spec.get("app_profile") or {"vendor": "unknown", "product": "unknown"},
            base_url=base_url.rstrip("/"), allowlist_profile=profile,
            transcript_sha256=llm.transcript_sha256(), transcript_path=llm.transcript_path(),
            model=llm.model, model_calls=llm.calls,
            viewport=spec.get("viewport") or {"width": 1280, "height": 900},
        )
        for note in notes:
            logger.log("synthesis_note", detail=note)
    finally:
        surface.close()
        if console is not None:
            console.stop()
        if rehearsals:
            _set_fault("none", base_url)

    issues = validate_artifact(artifact)
    for issue in issues:
        logger.log("artifact_issue", level=issue.level, code=issue.code, detail=issue.message)
    store = ArtifactStore()
    path = store.save(artifact)
    evidence.write_json("artifact.json", artifact.model_dump(mode="json"))
    typer.secho(f"\nartifact saved: {path.relative_to(REPO_ROOT)}", fg=typer.colors.GREEN, bold=True)
    _print_artifact_summary(artifact)
    logger.close()

    if approve:
        typer.secho("\nsmoke replay (draft -> approved)", fg=typer.colors.CYAN, bold=True)
        smoke_inputs = {k: v for k, v in supplied.items()}
        code = _replay(artifact_path=path, inputs=smoke_inputs, base_url=base_url,
                       profile=profile, headed=False, operator_mode="scripted",
                       console_port=console_port, label="smoke", promote=True)
        raise typer.Exit(code)


def _rehearsals(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise ``discovery.fault_rehearsal`` to a list.

    A single mapping means "stage this one fault for the probe phase", which is
    the original shape and still behaves exactly as it did. A list means one
    extra probe phase per entry, each with its own staged fault — which is the
    only way to observe conditions that are mutually exclusive on one session,
    such as an application error and a session expiry.
    """
    raw = (spec.get("discovery") or {}).get("fault_rehearsal")
    if not raw:
        return []
    entries = raw if isinstance(raw, list) else [raw]
    return [e for e in entries if isinstance(e, dict) and e.get("mode")]


def _probes(spec: dict[str, Any], rehearsals: list[dict[str, Any]]) -> list[str]:
    """Probes for the base phase: the declared ones, plus a single inline fault."""
    probes = list((spec.get("discovery") or {}).get("probes") or [])
    if len(rehearsals) == 1 and rehearsals[0].get("probe"):
        probes.append(rehearsals[0]["probe"])
    return probes


def _extra_probe_phases(rehearsals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One probe phase per staged fault, when more than one is declared."""
    if len(rehearsals) < 2:
        return []
    return [
        {"name": f"probe_{entry['mode']}", "probes": [entry["probe"]]}
        for entry in rehearsals
        if entry.get("probe")
    ]


def _set_fault(mode: str, base_url: str) -> str:
    import urllib.request

    url = f"{base_url.rstrip('/')}/admin/inject?mode={mode}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read().decode()


def _fault_stager(rehearsals: list[dict[str, Any]], base_url: str):
    """Stage a sandbox fault at the start of the phase that is meant to see it.

    Discovery can only record an application error if it *sees* one, and the
    agent must not be able to cause one itself — the allowlist denies the fault
    endpoint. So the platform stages it out of band, exactly as a release
    engineer would arrange a fault in a sandbox before recording a capability.

    With one declared fault, it is staged for the single probe phase. With
    several, each gets its own phase, and the phase is named after the fault so
    the mapping from a phase in the run log to the condition being rehearsed
    needs no explanation.
    """
    if not rehearsals:
        return None
    single = len(rehearsals) == 1
    by_phase = {f"probe_{e['mode']}": e["mode"] for e in rehearsals}

    def stage(phase: str) -> str | None:
        if single:
            if phase != "probe":
                return None
            mode = rehearsals[0]["mode"]
        else:
            mode = by_phase.get(phase)
            if mode is None:
                # The base probe phase, and the goal phase, run clean.
                if phase.startswith("probe"):
                    _set_fault("none", base_url)
                return None
        _set_fault(mode, base_url)
        return f"staged fault {mode!r} on the sandbox for the {phase!r} phase"

    return stage


def _as_input_spec(raw: dict[str, Any]):
    from .artifact.schema import InputSpec, ValueType

    sensitivity = Sensitivity(raw.get("sensitivity", "internal"))
    return InputSpec(
        name=raw["name"], type=ValueType(raw.get("type", "string")),
        required=bool(raw.get("required", True)), description=raw.get("description", ""),
        sensitivity=sensitivity, pattern=raw.get("pattern"),
        example=raw.get("example") if sensitivity in {Sensitivity.public,
                                                      Sensitivity.internal} else None,
        source=raw.get("source", "environment" if sensitivity is Sensitivity.secret else "caller"),
        env_var=raw.get("env_var"),
    )


# -------------------------------------------------------------------- replay


@app.command()
def replay(
    artifact_path: Path = typer.Option(None, "--artifact", "-a", help="Path to an artifact JSON."),
    capability: str = typer.Option(None, "--capability", "-c", help="Capability id from the store."),
    version: str = typer.Option(None, "--artifact-version"),
    inputs: list[str] = typer.Option(None, "--input", "-i", help="name=value (repeatable)."),
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url"),
    profile: str = typer.Option("default", "--allowlist-profile"),
    headed: bool = typer.Option(False, "--headed"),
    operator: str = typer.Option("none", "--operator",
                                 help="scripted | console | none — who answers an escalation."),
    console_port: int = typer.Option(8811, "--console-port"),
    label: str = typer.Option("", "--label", help="Tag for the evidence directory name."),
    json_only: bool = typer.Option(False, "--json", help="Print only the JSON result."),
) -> None:
    """Replay a saved artifact deterministically. No model is consulted."""
    _load_dotenv()
    if artifact_path is None:
        if not capability:
            raise typer.BadParameter("pass --artifact or --capability")
        artifact_path = ArtifactStore().path_for(
            capability, version or (ArtifactStore().versions(capability) or ["0"])[-1])
    code = _replay(artifact_path=artifact_path, inputs=_parse_inputs(inputs), base_url=base_url,
                   profile=profile, headed=headed, operator_mode=operator,
                   console_port=console_port, label=label or "run", json_only=json_only)
    raise typer.Exit(code)


def _replay(*, artifact_path: Path, inputs: dict[str, str], base_url: str, profile: str,
            headed: bool, operator_mode: str, console_port: int, label: str,
            promote: bool = False, json_only: bool = False) -> int:
    from .replay.engine import ReplayEngine
    from .replay.result import ReplayStatus

    store = ArtifactStore()
    artifact = store.load(artifact_path)
    allowlist = load_profile(profile)
    gate = PolicyGate(allowlist)
    run_id = _run_id(f"replay_{label}" if label else "replay")
    redactor = Redactor()
    evidence = EvidenceWriter("replay", run_id, redactor)
    logger = RunLogger(evidence.log_path, run_id=run_id, phase="replay", redactor=redactor,
                       echo=not json_only)
    control = SessionControl(run_id=run_id)
    op, console = _make_operator(operator_mode, control, OPERATOR_SCRIPT, console_port)

    if not json_only:
        typer.secho(f"\nreplay run {run_id}", fg=typer.colors.GREEN, bold=True)
        typer.echo(f"  capability : {artifact.capability_id} v{artifact.version} "
                   f"({artifact.status.value})")
        typer.echo(f"  inputs     : {inputs}")
        typer.echo(f"  evidence   : {evidence.rel(evidence.root)}\n")

    surface = PlaywrightWebSurface(headless=not headed, viewport=artifact.surface.viewport)
    try:
        engine = ReplayEngine(artifact, surface, gate, redactor=redactor, logger=logger,
                              evidence=evidence, control=control, operator=op,
                              config={"base_url": base_url.rstrip("/")}, run_id=run_id)
        result = engine.run(inputs)
    finally:
        surface.close()
        if console is not None:
            console.stop()
        logger.close()

    if json_only:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        colour = {
            ReplayStatus.success: typer.colors.GREEN,
            ReplayStatus.business_outcome: typer.colors.YELLOW,
            ReplayStatus.invalid_input: typer.colors.YELLOW,
            ReplayStatus.blocked_by_policy: typer.colors.MAGENTA,
            ReplayStatus.failure: typer.colors.RED,
        }[result.status]
        typer.secho("\n" + result.summary_line(), fg=colour, bold=True)
        if result.failure:
            typer.echo(f"  expected : {result.failure.expected}")
            typer.echo(f"  observed : {result.failure.observed}")
            typer.echo(f"  evidence : {', '.join(result.failure.evidence) or '-'}")
        if result.human_actions:
            typer.echo(f"  human actions recorded: {len(result.human_actions)}")
            for action in result.human_actions:
                typer.echo(f"    - {action['description']}")
        typer.echo(f"  result   : {evidence.rel(evidence.root)}/result.json")

    if promote and result.status is ReplayStatus.success:
        promoted = artifact.model_copy(update={
            # Clearing the checksum is required, not incidental: the store
            # re-signs on save, and a stale signature is a validation error.
            "checksum": None,
            "status": ArtifactStatus.approved,
            "verification": artifact.verification.model_copy(update={
                "smoke_replay_run_id": run_id,
                "smoke_replay_status": result.status.value,
                "verified_at": utc_now(),
            }),
        })
        store.save(promoted)
        typer.secho(f"artifact promoted to approved (verified by {run_id})",
                    fg=typer.colors.GREEN)

    return 0 if result.status in {ReplayStatus.success, ReplayStatus.business_outcome} else 1


@app.command()
def approve(
    capability: str = typer.Argument(..., help="Capability id to verify and approve."),
    inputs: list[str] = typer.Option(None, "--input", "-i",
                                     help="Inputs for the verification replay."),
    version: str = typer.Option(None, "--artifact-version"),
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url"),
) -> None:
    """Verify a draft artifact by replaying it, then promote it to approved.

    Approval is earned by a run, not asserted: the artifact is only promoted if a
    real replay of it succeeds, and the run id that verified it is recorded.
    """
    _load_dotenv()
    store = ArtifactStore()
    artifact = store.get(capability, version)
    path = store.path_for(artifact.capability_id, artifact.version)
    code = _replay(artifact_path=path, inputs=_parse_inputs(inputs), base_url=base_url,
                   profile=artifact.safety.allowlist_profile, headed=False,
                   operator_mode="none", console_port=8811, label="verify", promote=True)
    raise typer.Exit(code)


# ------------------------------------------------------------------- catalog


@catalog_app.command("list")
def catalog_list() -> None:
    """List the capabilities an agent can call."""
    for artifact in CapabilityCatalog().list():
        typer.secho(f"{artifact.capability_id}  v{artifact.version}  [{artifact.status.value}]",
                    bold=True)
        typer.echo(f"   {artifact.description.strip()[:150]}")
        callable_inputs = [i for i in artifact.inputs if i.source == "caller"]
        typer.echo("   inputs : " + (", ".join(f"{i.name}:{i.type.value}" for i in callable_inputs)
                                     or "none"))
        typer.echo("   outputs: " + (", ".join(f"{o.name}:{o.type.value}" for o in artifact.outputs)
                                     or "none"))
        typer.echo("   outcomes: " + (", ".join(o.name for o in artifact.business_outcomes) or "none"))


@catalog_app.command("tools")
def catalog_tools(
    out: Path = typer.Option(None, "--out", help="Write the tool definitions to a file."),
) -> None:
    """Emit the catalog as JSON-schema tool definitions for a calling agent."""
    catalog = CapabilityCatalog()
    payload = {
        "tools": catalog.tool_schemas(),
        "results": {a.capability_id: result_schema(a) for a in catalog.list()},
    }
    text = json.dumps(payload, indent=2)
    if out:
        Path(out).write_text(text)
        typer.secho(f"wrote {out}", fg=typer.colors.GREEN)
    else:
        print(text)


@catalog_app.command("show")
def catalog_show(capability: str, version: str = typer.Option(None, "--artifact-version")) -> None:
    """Show one capability's tool schema and artifact summary."""
    artifact = CapabilityCatalog().get(capability, version)
    _print_artifact_summary(artifact)
    print(json.dumps(tool_schema(artifact), indent=2))


@app.command()
def invoke(
    capability: str = typer.Argument(..., help="Capability id, as an agent would call it."),
    inputs: list[str] = typer.Option(None, "--input", "-i"),
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url"),
    version: str = typer.Option(None, "--artifact-version"),
    approved_only: bool = typer.Option(True, "--approved-only/--allow-draft"),
    operator: str = typer.Option("none", "--operator"),
) -> None:
    """Call a capability the way an AI agent would: by name, typed in, typed out."""
    _load_dotenv()
    store = ArtifactStore()
    artifact = store.get(capability, version)
    if approved_only and artifact.status is not ArtifactStatus.approved:
        typer.secho(f"capability {capability!r} is {artifact.status.value}; unattended invocation "
                    "requires an approved artifact (pass --allow-draft to override)",
                    fg=typer.colors.RED)
        raise typer.Exit(2)
    path = store.path_for(artifact.capability_id, artifact.version)
    code = _replay(artifact_path=path, inputs=_parse_inputs(inputs), base_url=base_url,
                   profile=artifact.safety.allowlist_profile, headed=False,
                   operator_mode=operator, console_port=8811, label="invoke", json_only=True)
    raise typer.Exit(code)


# ------------------------------------------------------------------ validate


@app.command()
def validate(
    artifact_path: Path = typer.Argument(None, help="Artifact JSON; omit to check all stored."),
) -> None:
    """Validate artifacts for structure, safety and provenance."""
    store = ArtifactStore()
    targets = [store.load(artifact_path)] if artifact_path else store.list_all()
    if not targets:
        typer.secho("no artifacts found", fg=typer.colors.YELLOW)
        raise typer.Exit(0)
    bad = 0
    for artifact in targets:
        issues = validate_artifact(artifact)
        head = f"{artifact.capability_id} v{artifact.version}"
        if not issues:
            typer.secho(f"{head}: ok", fg=typer.colors.GREEN)
            continue
        for issue in issues:
            colour = typer.colors.RED if issue.level == "error" else typer.colors.YELLOW
            typer.secho(f"{head}: {issue}", fg=colour)
        bad += sum(1 for i in issues if i.level == "error")
    raise typer.Exit(1 if bad else 0)


@app.command("inject")
def inject(
    mode: str = typer.Argument(..., help="none | app_error | slow | expire"),
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url"),
) -> None:
    """Inject a fault into the mock application (operator tooling, not agent-reachable)."""
    import urllib.request

    url = f"{base_url.rstrip('/')}/admin/inject?mode={mode}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        typer.echo(resp.read().decode())


def _print_artifact_summary(artifact: CapabilityArtifact) -> None:
    typer.echo(f"  capability : {artifact.capability_id} v{artifact.version} "
               f"[{artifact.status.value}]")
    typer.echo(f"  steps      : {len(artifact.steps)}")
    typer.echo(f"  inputs     : " + ", ".join(
        f"{i.name}:{i.type.value}{'(env)' if i.source == 'environment' else ''}"
        for i in artifact.inputs))
    typer.echo(f"  outputs    : " + ", ".join(f"{o.name}:{o.type.value}" for o in artifact.outputs))
    typer.echo(f"  outcomes   : " + (", ".join(
        f"{o.name}->{o.disposition.value}" for o in artifact.business_outcomes) or "none"))
    typer.echo(f"  recoveries : " + (", ".join(r.name for r in artifact.recovery_rules) or "none"))
    typer.echo(f"  failures   : " + (", ".join(r.name for r in artifact.failure_rules) or "none"))
    typer.echo(f"  checksum   : {artifact.checksum}")


if __name__ == "__main__":
    app()
