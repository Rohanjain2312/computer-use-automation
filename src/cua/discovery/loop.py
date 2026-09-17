"""The LLM-driven discovery loop: OBSERVE -> DECIDE -> ACT.

This is the only place a model is allowed to decide what happens next, and it
runs once per capability. Its product is not a transcript — it is a trajectory
of *grounded* actions: each one records the control the surface actually
perceived, the observation before and after, and the model's stated intent. The
synthesizer turns that into the artifact.

Every action passes the same ``PolicyGate`` that replay uses, and every action is
gated on ``SessionControl``, so a discovery run that gets stuck hands over the
live session exactly the way a replay run does.
"""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..artifact.schema import ActionKind, RiskLevel
from ..handoff.control import SessionControl
from ..handoff.intervention import InterventionRequest
from ..handoff.recorder import HumanActionRecorder
from ..observability.evidence import EvidenceWriter
from ..observability.logging import RunLogger
from ..replay.engine import Operator
from ..safety.policy import PolicyGate
from ..safety.redact import Redactor
from ..surface.base import Observation, UiNode
from ..surface.web import PlaywrightWebSurface
from .llm import LlmClient, tool_definitions
from .prompts import GOAL_PROMPT, PROBE_PROMPT, SYSTEM, render_observation

DEFAULT_PROBES = [
    "What does the application show when the record you searched for does not exist? "
    "Use an obviously invalid identifier such as 999999.",
    "What does it show when the operator is not entitled to view a record? "
    "Member 100999 is flagged for executive services.",
    "Did any interstitial or notice screen appear during the goal phase that the "
    "automation would have to acknowledge before it could continue?",
]

_ACTION_TOOLS = {"navigate", "click", "type_text", "select_option", "press_key"}
_TOOL_TO_ACTION = {
    "navigate": ActionKind.navigate,
    "click": ActionKind.click,
    "type_text": ActionKind.type,
    "select_option": ActionKind.select,
    "press_key": ActionKind.press,
}


@dataclass
class RecordedAction:
    index: int
    phase: str
    tool: str
    action: ActionKind
    intent: str
    expect: str
    node: UiNode | None
    obs_before: Observation
    obs_after: Observation
    input_name: str | None = None
    literal: str | None = None
    url: str | None = None
    key: str | None = None
    risk: RiskLevel = RiskLevel.read_only
    evidence: list[str] = field(default_factory=list)


@dataclass
class DeclaredOutput:
    name: str
    node: UiNode
    observation: Observation
    type: str
    description: str
    sensitivity: str = "internal"


@dataclass
class DiscoveryResult:
    run_id: str
    goal: str
    success: bool
    stop_reason: str
    summary: str
    actions: list[RecordedAction] = field(default_factory=list)
    outputs: list[DeclaredOutput] = field(default_factory=list)
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    escalations: list[dict[str, Any]] = field(default_factory=list)
    entry_point: str = ""
    model: str = ""
    model_calls: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    evidence_dir: str = ""


class DiscoveryAgent:
    def __init__(
        self,
        *,
        goal: str,
        entry_point: str,
        inputs: dict[str, Any],
        input_specs: list[dict[str, Any]],
        output_hints: list[str],
        surface: PlaywrightWebSurface,
        gate: PolicyGate,
        llm: LlmClient,
        logger: RunLogger,
        evidence: EvidenceWriter,
        control: SessionControl,
        redactor: Redactor,
        operator: Operator | None = None,
        probes: list[str] | None = None,
        max_goal_steps: int = 22,
        max_probe_steps: int = 16,
        run_id: str | None = None,
    ) -> None:
        self.goal = goal
        self.entry_point = entry_point
        self.inputs = inputs
        self.input_specs = input_specs
        self.output_hints = output_hints
        self.surface = surface
        self.gate = gate
        self.llm = llm
        self.log = logger
        self.evidence = evidence
        self.control = control
        self.redactor = redactor
        self.operator = operator
        self.probes = probes if probes is not None else DEFAULT_PROBES
        self.max_goal_steps = max_goal_steps
        self.max_probe_steps = max_probe_steps
        self.run_id = run_id or control.run_id
        self.recorder = HumanActionRecorder(surface, redactor)

        self.actions: list[RecordedAction] = []
        self.outputs: list[DeclaredOutput] = []
        self.outcomes: list[dict[str, Any]] = []
        self.recoveries: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.escalations: list[dict[str, Any]] = []
        self._messages: list[dict[str, Any]] = []
        self._step = 0
        self._deadline = time.time() + gate.profile.max_run_seconds
        self._repeat_guard: list[str] = []

    # ------------------------------------------------------------------ run

    def run(self) -> DiscoveryResult:
        self.log.log("run_started", goal=self.goal, entry_point=self.entry_point,
                     model=self.llm.model, allowlist_profile=self.gate.profile.name)

        decision = self.gate.check(action=ActionKind.navigate, url=self.entry_point)
        if not decision.allowed:
            self.log.log("policy_check", **decision.as_dict())
            return self._result(False, "allowlist_denied_entry_point", decision.reason)

        self.surface.open(self.entry_point)

        goal_stop, goal_summary, goal_ok = self._phase(
            "goal",
            GOAL_PROMPT.format(
                goal=self.goal,
                entry_point=self.entry_point,
                inputs=self._render_inputs(),
                outputs="\n".join(f"  - {h}" for h in self.output_hints) or "  (none declared)",
            ),
            self.max_goal_steps,
        )
        if not goal_ok:
            return self._result(False, goal_stop, goal_summary)

        probe_stop, probe_summary, _ = ("skipped", "probe phase skipped", True)
        if self.probes:
            probe_stop, probe_summary, _ = self._phase(
                "probe",
                PROBE_PROMPT.format(probes="\n".join(f"  {i+1}. {p}" for i, p in
                                                     enumerate(self.probes))),
                self.max_probe_steps,
            )

        self.control.complete("discovery finished")
        return self._result(True, f"goal:{goal_stop}|probe:{probe_stop}",
                            f"{goal_summary} // {probe_summary}")

    def _result(self, success: bool, stop_reason: str, summary: str) -> DiscoveryResult:
        self.log.log("run_finished", status="success" if success else "failed",
                     reason=stop_reason, detail=summary, model_calls=self.llm.calls,
                     actions=len(self.actions))
        return DiscoveryResult(
            run_id=self.run_id, goal=self.goal, success=success, stop_reason=stop_reason,
            summary=summary, actions=self.actions, outputs=self.outputs, outcomes=self.outcomes,
            recoveries=self.recoveries, failures=self.failures, escalations=self.escalations,
            entry_point=self.entry_point, model=self.llm.model, model_calls=self.llm.calls,
            usage=dict(self.llm.usage), evidence_dir=self.evidence.rel(self.evidence.root),
        )

    def _render_inputs(self) -> str:
        lines = []
        for spec in self.input_specs:
            secret = spec.get("sensitivity") in {"secret"}
            shown = "(supplied at run time; never shown to you)" if secret else \
                f"= {self.inputs.get(spec['name'], '')!r}"
            lines.append(
                f"  - {spec['name']} : {spec.get('type', 'string')} — "
                f"{spec.get('description', '')} {shown}"
            )
        return "\n".join(lines) or "  (none)"

    # --------------------------------------------------------------- phases

    def _phase(self, phase: str, prompt: str, max_steps: int) -> tuple[str, str, bool]:
        self.log.log("phase_started", detail=phase, max_steps=max_steps)
        obs = self._observe(phase)
        self._messages.append({"role": "user", "content": self._user_blocks(prompt, obs, [])})
        steps = 0

        while True:
            if steps >= max_steps:
                self.log.log("stopping_condition", reason="max_steps", detail=f"{phase}:{steps}")
                return "max_steps", f"{phase} phase hit the {max_steps}-step limit", phase != "goal"
            if time.time() > self._deadline:
                self.log.log("stopping_condition", reason="timeout")
                return "timeout", f"{phase} phase exceeded the run time budget", phase != "goal"

            call = self.llm.complete(system=SYSTEM, messages=self._messages,
                                     tools=tool_definitions())
            if call.text:
                self.log.log("model_reasoning", detail=call.text[:400])

            if not call.tool_calls:
                self.log.log("stopping_condition", reason="no_action",
                             detail=call.text[:200] or "model returned no tool call")
                return "no_action", call.text[:400] or "model stopped without acting", phase != "goal"

            self._messages.append({
                "role": "assistant",
                "content": ([{"type": "text", "text": call.text}] if call.text else [])
                + [{"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["input"]}
                   for t in call.tool_calls],
            })

            results: list[dict[str, Any]] = []
            finished: tuple[str, str, bool] | None = None
            for tool in call.tool_calls:
                if tool["name"] == "finish":
                    ok = bool(tool["input"].get("success", True))
                    summary = str(tool["input"].get("summary", ""))
                    self.log.log("phase_finished", detail=phase, status=str(ok), reason=summary)
                    results.append(_tool_result(tool["id"], "phase ended"))
                    finished = ("finished", summary, ok)
                    continue
                results.append(self._dispatch(tool, phase))
                steps += 1
                self._step += 1

            if finished is not None:
                self._messages.append({"role": "user", "content": results})
                return finished

            obs = self._observe(phase)
            self._messages.append({"role": "user", "content": self._user_blocks(None, obs, results)})
            self._messages = _trim(self._messages)

    # ---------------------------------------------------------------- tools

    def _dispatch(self, tool: dict[str, Any], phase: str) -> dict[str, Any]:
        name = tool["name"]
        args = tool["input"]
        handlers = {
            "extract_output": self._tool_extract,
            "declare_outcome": self._tool_declare_outcome,
            "declare_recovery": self._tool_declare_recovery,
            "declare_failure": self._tool_declare_failure,
            "request_human_help": self._tool_request_help,
        }
        if name in _ACTION_TOOLS:
            return self._tool_action(tool, phase)
        handler = handlers.get(name)
        if handler is None:
            return _tool_result(tool["id"], f"unknown tool {name!r}", error=True)
        return handler(tool, args)

    def _tool_action(self, tool: dict[str, Any], phase: str) -> dict[str, Any]:
        name, args = tool["name"], tool["input"]
        action = _TOOL_TO_ACTION[name]
        intent = str(args.get("intent", "")).strip() or f"{name} action"
        expect = str(args.get("expect", "")).strip()

        try:
            self.control.assert_can_act(name)
        except Exception as exc:
            return _tool_result(tool["id"], str(exc), error=True)

        obs_before = self.surface.observe()
        node: UiNode | None = None
        if name != "navigate" and name != "press_key":
            node = obs_before.by_ref(str(args.get("ref", "")))
            if node is None:
                return _tool_result(
                    tool["id"],
                    f"ref {args.get('ref')!r} is not on the current screen. Refs are only valid "
                    "for the observation they came from — use a ref from the latest observation.",
                    error=True,
                )

        url = str(args.get("url", "")) if name == "navigate" else None
        label = node.name if node else (url or "")
        decision = self.gate.check(action=action, url=url, control_label=label)
        self.log.log("policy_check", action=action.value, target=label, intent=intent,
                     **decision.as_dict())
        if not decision.allowed:
            if decision.requires_human:
                resolution = self._escalate(
                    reason_class="risky_action", reason=decision.reason, intent=intent,
                    obs=obs_before,
                    suggested=[f"Perform '{intent}' manually in the live browser window",
                               "Then release control so discovery can continue"],
                )
                return _tool_result(
                    tool["id"],
                    f"This action needs a person and was handed to an operator ({resolution}). "
                    "The screen may have changed; look at the next observation and continue.",
                )
            return _tool_result(
                tool["id"],
                f"REFUSED by safety policy: {decision.reason}. Do not retry this action; "
                "find another way to accomplish the goal or call request_human_help.",
                error=True,
            )

        # -- perform
        detail = ""
        if name == "navigate":
            outcome = self.surface.open(url or "")
            detail = outcome.detail
        elif name == "press_key":
            outcome = self.surface.press(str(args.get("key", "Enter")))
            detail = outcome.detail
        elif name == "click":
            x, y = node.rect.center
            outcome = self.surface.click_point(x, y)
            detail = f"clicked {node.name!r}"
        elif name == "type_text":
            input_name = args.get("input_name")
            if input_name:
                if input_name not in self.inputs:
                    return _tool_result(
                        tool["id"],
                        f"input {input_name!r} is not one of the declared inputs "
                        f"({sorted(self.inputs)})", error=True)
                value = str(self.inputs[input_name])
            else:
                value = str(args.get("text", ""))
                if self.redactor.contains_registered(value):
                    return _tool_result(
                        tool["id"],
                        "REFUSED: that value is a declared secret. Pass input_name instead of "
                        "text so the capability stores a reference rather than the value.",
                        error=True)
            x, y = node.rect.center
            self.surface.click_point(x, y)
            self.surface.clear_focused()
            outcome = self.surface.type_text(value)
            detail = f"typed into {node.name!r}"
        else:  # select_option
            outcome = self.surface.set_combobox_value(node, str(args.get("value", "")))
            detail = outcome.detail

        landing = self.gate.check_landing_url(self.surface.current_url())
        if not landing.allowed:
            self.log.log("allowlist_landing_violation", detail=landing.reason)
            self.surface.open(self.entry_point)
            return _tool_result(
                tool["id"],
                f"That action navigated outside the allowlist ({landing.reason}); the session was "
                "returned to the entry point. Choose a different route.", error=True)

        obs_after = self.surface.observe()
        shot = self.evidence.screenshot(obs_after.screenshot, f"{self._step:02d}_{phase}_{name}")
        ax = self.evidence.observation_snapshot(obs_after, f"{self._step:02d}_{phase}_{name}")

        record = RecordedAction(
            index=len(self.actions), phase=phase, tool=name, action=action, intent=intent,
            expect=expect, node=node, obs_before=_light(obs_before), obs_after=_light(obs_after),
            input_name=args.get("input_name"),
            literal=(str(args.get("text")) if name == "type_text" and not args.get("input_name")
                     else (str(args.get("value")) if name == "select_option" else None)),
            url=url, key=str(args.get("key")) if name == "press_key" else None,
            risk=decision.risk, evidence=[p for p in (shot, ax) if p],
        )
        self.actions.append(record)
        self.log.log("action", step_id=f"a{record.index}", action=action.value, intent=intent,
                     target=label, detail=detail, risk=decision.risk.value,
                     status="ok" if outcome.ok else "failed")

        if self._stuck(name, args, obs_after):
            return _tool_result(
                tool["id"],
                f"{detail}. NOTE: the screen has not changed across several actions. Try a "
                "different approach, or call request_human_help if you are blocked.")

        return _tool_result(tool["id"], f"{detail}. The next observation shows the result.")

    def _tool_extract(self, tool: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        obs = self.surface.observe(screenshot=False)
        node = obs.by_ref(str(args.get("ref", "")))
        if node is None:
            return _tool_result(tool["id"], f"ref {args.get('ref')!r} is not on the current screen",
                                error=True)
        value = self.surface.read_node(node, "text") or node.text
        self.outputs.append(DeclaredOutput(
            name=str(args["name"]), node=node, observation=_light(obs),
            type=str(args.get("type", "string")), description=str(args.get("description", "")),
            sensitivity=str(args.get("sensitivity", "internal")),
        ))
        self.log.log("output_declared", output=args["name"], type=args.get("type"),
                     sensitivity=args.get("sensitivity", "internal"),
                     detail=self.redactor.scrub_text(value))
        return _tool_result(tool["id"], f"recorded output {args['name']!r} = "
                                        f"{self.redactor.scrub_text(value)!r}")

    def _verify_marker(self, marker: str) -> tuple[bool, Observation]:
        """Ground a declared marker in text the run genuinely saw.

        The current screen is checked first, then every observation recorded so
        far. That second half matters: a one-shot interstitial is long gone by
        the time the model declares a recovery rule for it, and dropping the rule
        because the screen has moved on would lose real knowledge.
        """
        obs = self.surface.observe(screenshot=False)
        needle = " ".join(marker.split()).casefold()
        if not needle:
            return False, obs
        haystacks = [obs.text]
        for action in self.actions:
            haystacks.append(action.obs_before.text)
            haystacks.append(action.obs_after.text)
        for text in haystacks:
            if needle in " ".join((text or "").split()).casefold():
                return True, obs
        return False, obs

    def _tool_declare_outcome(self, tool: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        marker = str(args.get("marker_text", ""))
        seen, obs = self._verify_marker(marker)
        entry = {**args, "marker_verified": seen, "observed_url": obs.url}
        self.outcomes.append(entry)
        self.log.log("outcome_declared", outcome=args.get("name"), verified=str(seen),
                     detail=marker[:120])
        if not seen:
            return _tool_result(
                tool["id"],
                f"recorded, but the marker text {marker!r} is NOT on the screen right now. The "
                "deterministic engine matches this text literally, so re-declare it with the exact "
                "wording from the screen where this state appears.")
        return _tool_result(tool["id"], f"recorded business outcome {args.get('name')!r}; "
                                        "marker text verified against the live screen")

    def _tool_declare_recovery(self, tool: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        marker = str(args.get("marker_text", ""))
        seen, obs = self._verify_marker(marker)
        self.recoveries.append({**args, "marker_verified": seen, "observed_url": obs.url})
        self.log.log("recovery_declared", rule=args.get("name"), verified=str(seen),
                     detail=marker[:120])
        return _tool_result(tool["id"], f"recorded recovery rule {args.get('name')!r} "
                                        f"(marker currently visible: {seen})")

    def _tool_declare_failure(self, tool: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        marker = str(args.get("marker_text", ""))
        seen, obs = self._verify_marker(marker)
        self.failures.append({**args, "marker_verified": seen, "observed_url": obs.url})
        self.log.log("failure_rule_declared", rule=args.get("name"), verified=str(seen),
                     detail=marker[:120])
        return _tool_result(tool["id"], f"recorded failure rule {args.get('name')!r} "
                                        f"(marker currently visible: {seen})")

    def _tool_request_help(self, tool: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        obs = self.surface.observe()
        resolution = self._escalate(
            reason_class="model_requested", reason=str(args.get("reason", "agent is stuck")),
            intent="model asked for human help", obs=obs,
            suggested=list(args.get("suggested_actions") or
                           ["Unblock the session in the browser window", "Then release control"]),
        )
        return _tool_result(tool["id"], f"a human was engaged ({resolution}); "
                                        "look at the next observation and continue")

    # ----------------------------------------------------------- escalation

    def _escalate(self, *, reason_class: str, reason: str, intent: str, obs: Observation,
                  suggested: list[str]) -> str:
        shot = self.evidence.screenshot(obs.screenshot or self.surface.screenshot(),
                                        f"intervention_{self._step:02d}")
        dom = self.evidence.dom_snapshot(self.surface.dom_snapshot(),
                                         f"intervention_{self._step:02d}")
        request = InterventionRequest(
            run_id=self.run_id, phase="discovery", capability_id="(discovery)",
            capability_version="-", goal=self.goal,
            step_id=f"a{len(self.actions)}", step_index=len(self.actions), step_intent=intent,
            reason_class=reason_class, reason=self.redactor.scrub_text(reason),
            observed_url=obs.url, observed_title=obs.title,
            observed_excerpt=self.redactor.scrub_text(obs.text[:600]),
            screenshot_path=shot, dom_snapshot_path=dom, suggested_actions=suggested,
            resume_hint="discovery continues from the next observation after release",
        )
        request.write(extra_dir=self.evidence.root)
        self.control.pause_for_human(reason, request.id, f"a{len(self.actions)}")
        self.log.log("escalation_requested", intervention_id=request.id,
                     reason_class=reason_class, reason=request.reason,
                     owner=self.control.owner.value)

        if self.operator is None:
            self.control.resumed()
            self.log.log("escalation_unattended", intervention_id=request.id,
                         detail="no operator channel configured; discovery continues unassisted")
            return "unattended"

        before = len(self.control.human_actions)
        resolution = self.operator.handle(
            request, control=self.control, surface=self.surface, recorder=self.recorder,
            logger=self.log, evidence=self.evidence)
        performed = [a.as_dict() for a in self.control.human_actions[before:]]
        request.status = "resolved" if resolution == "resumed" else "aborted"
        request.resolution = resolution
        request.operator = getattr(self.operator, "name", "operator")
        request.write(extra_dir=self.evidence.root)
        self.escalations.append({"intervention_id": request.id, "reason_class": reason_class,
                                 "reason": request.reason, "resolution": resolution,
                                 "human_actions": performed})
        if resolution == "resumed":
            self.control.resumed()
        self.log.log("escalation_resolved", intervention_id=request.id, status=resolution,
                     human_actions=len(performed), owner=self.control.owner.value)
        return resolution

    # ------------------------------------------------------------- plumbing

    def _observe(self, phase: str) -> Observation:
        obs = self.surface.observe()
        self.evidence.screenshot(obs.screenshot, f"{self._step:02d}_{phase}_observe")
        return obs

    def _user_blocks(self, prompt: str | None, obs: Observation,
                     tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = list(tool_results)
        if prompt:
            blocks.append({"type": "text", "text": prompt})
        if obs.screenshot:
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png",
                           "data": base64.b64encode(obs.screenshot).decode()},
            })
        blocks.append({"type": "text",
                       "text": self.redactor.scrub_text(render_observation(obs, step=self._step))})
        return blocks

    def _stuck(self, tool: str, args: dict[str, Any], obs: Observation) -> bool:
        digest = f"{tool}:{args.get('ref', '')}:{obs.url}:{hash(obs.text[:500])}"
        self._repeat_guard.append(digest)
        self._repeat_guard = self._repeat_guard[-4:]
        return len(self._repeat_guard) == 4 and len(set(self._repeat_guard)) == 1


def _tool_result(tool_use_id: str, content: str, *, error: bool = False) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if error:
        block["is_error"] = True
    return block


def _light(obs: Observation) -> Observation:
    """Drop screenshot bytes from a retained observation; they are already on disk."""
    return Observation(url=obs.url, title=obs.title, nodes=obs.nodes, text=obs.text,
                       frames=obs.frames, screenshot=None, captured_at=obs.captured_at)


def _trim(messages: list[dict[str, Any]], keep_images: int = 3) -> list[dict[str, Any]]:
    """Keep only the most recent screenshots; older turns keep their text."""
    seen = 0
    for message in reversed(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for i, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "image":
                seen += 1
                if seen > keep_images:
                    content[i] = {"type": "text",
                                  "text": "[earlier screenshot omitted to save context]"}
    return messages
