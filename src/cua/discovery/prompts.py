"""Prompts and observation rendering for the discovery agent."""

from __future__ import annotations

from ..surface.base import Observation, UiNode

SYSTEM = """You are a computer-use agent operating a legacy back-office banking
application on behalf of an automation platform. You interact with it exactly as
a human operator would: you look at the screen, you find controls by what they
are labelled, and you click and type.

The application is a frameset-based servicing console with table layouts and no
test ids. Every observation you receive lists the controls that were actually
perceived on screen, each with a stable `ref` for this turn only. Refs change
after every action — always use the refs from the most recent observation.

## What you are producing

This is a RECORDING run. Everything you do is being converted into a reusable,
deterministic automation that will later run thousands of times without you.
That changes how you should work:

* Do the minimum number of steps that reliably accomplishes the goal. Do not
  explore side menus, do not click things "to see what happens" during the goal
  phase.
* Give every action an `intent`: a short sentence saying WHY, in business terms
  ("enter the member number to look up"), not in mechanical terms ("click n12").
* Give every action an `expect`: the text you expect to see on screen afterwards
  if it worked. This becomes the checkpoint that proves the replayed step
  actually took effect. Quote text you genuinely expect to appear.
* When you type a value that came from the task inputs, pass `input_name`
  instead of `text`. Values bound to inputs become parameters of the reusable
  capability; hardcoded text does not. NEVER pass a credential as `text` —
  credentials are supplied by name only and you are never shown them.

## Safety

You operate under an allowlist and a risk policy. Some actions will be refused;
the refusal explains why. Do not try to work around a refusal, and never attempt
anything that moves money or alters an account record. If you are genuinely
blocked, call `request_human_help`.

## Phases

You work in two phases and you are told which one you are in.

1. `goal` — accomplish the stated goal, then call `extract_output` for each
   value the caller asked for, then call `finish`.
2. `probe` — the flow already works. Now find out how the application behaves
   when things go wrong, so the deterministic automation can tell a legitimate
   business answer apart from a broken run. Deliberately trigger the failure
   modes you are asked about, read the exact wording the application uses, and
   register it with `declare_outcome` / `declare_recovery` / `declare_failure`.
   Marker text must be copied EXACTLY from what you see on screen — the
   deterministic engine matches on it literally.

Work one action at a time. Look at each new observation before deciding."""


GOAL_PROMPT = """PHASE: goal

Goal: {goal}

Entry point: {entry_point}

Inputs available to you (use `input_name` when typing these):
{inputs}

Outputs the calling agent needs back:
{outputs}

Accomplish the goal, call `extract_output` once per required output, then call
`finish`."""


PROBE_PROMPT = """PHASE: probe

You have completed the goal. The reusable automation now needs to know how this
application reports problems, so that a legitimate business answer is never
mistaken for a system failure.

Investigate, on this same live session:

{probes}

For each thing you find, copy the application's EXACT wording into a
`declare_outcome`, `declare_recovery` or `declare_failure` call:

* `declare_outcome` — a legitimate business answer the caller must be told
  about (for example: the record does not exist; the operator is not entitled
  to view it). Use disposition `return_to_caller` when the caller simply needs
  the answer, and `escalate_to_human` when a person could clear the block on
  this same session and let the run continue.
* `declare_recovery` — a transient screen the automation can clear by itself,
  such as an interstitial notice with an acknowledge button.
* `declare_failure` — a genuine application fault that must stop the run.

Then call `finish`."""


def render_observation(obs: Observation, *, step: int, max_controls: int = 60,
                       max_cells: int = 90, max_text: int = 1800) -> str:
    lines: list[str] = [f"OBSERVATION #{step}", f"URL: {obs.url}", f"TITLE: {obs.title}"]

    frames = [f for f in obs.frames if f.get("url")]
    if frames:
        lines.append(
            "FRAMES: " + " ; ".join(f"{f['name']} -> {f['url']}" for f in frames)
        )

    controls = [n for n in obs.nodes if n.interactive][:max_controls]
    if controls:
        lines.append("")
        lines.append("CONTROLS  (ref | role | name | value)")
        for n in controls:
            value = "••••" if n.secret and n.value else (n.value or "")
            frame = f" @{n.frame_name}" if n.frame_name else ""
            state = "" if n.enabled else " [disabled]"
            lines.append(f"  {n.ref} | {n.role} | {n.name}{frame}{state} | {value}")

    tables = _render_tables(obs, max_cells)
    if tables:
        lines.append("")
        lines.extend(tables)

    text = obs.text.strip()
    if len(text) > max_text:
        text = text[:max_text] + " …[truncated]"
    lines.append("")
    lines.append("VISIBLE TEXT")
    lines.append(text)
    return "\n".join(lines)


def _render_tables(obs: Observation, budget: int) -> list[str]:
    grouped: dict[tuple, list[UiNode]] = {}
    for node in obs.nodes:
        if (node.table and node.table.headers and node.role == "cell"
                and node.table.header_confident):
            key = (node.frame_name, tuple(node.table.headers))
            grouped.setdefault(key, []).append(node)

    out: list[str] = []
    spent = 0
    for (frame, headers), cells in grouped.items():
        if spent >= budget:
            break
        out.append(f"TABLE [{' | '.join(headers)}]" + (f" @{frame}" if frame else ""))
        rows: dict[int, list[UiNode]] = {}
        for cell in cells:
            rows.setdefault(cell.table.row_index, []).append(cell)
        for row_index in sorted(rows):
            row = sorted(rows[row_index], key=lambda c: c.table.col_index)
            if row_index == 0 and all(c.text == c.table.col_header for c in row):
                continue
            pieces = [f"{c.table.col_header}={c.ref}:{c.text!r}" for c in row]
            out.append(f"  row {row[0].table.row_label!r}: " + "  ".join(pieces))
            spent += len(row)
            if spent >= budget:
                out.append("  …[more rows omitted]")
                break
    return out
