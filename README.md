# Computer-Use Automation System

An LLM operates a legacy back-office application through its UI, once. What it
learned becomes a typed, reviewable **capability artifact**. After that the
capability replays deterministically — same flow, different inputs, no model in
the decision loop — and returns typed results to a calling agent.

```
natural-language goal
   └─► LLM-driven discovery on a live UI ──► capability artifact (JSON, typed, checksummed)
                                                   └─► deterministic replay ──► typed outputs
                                                        │                       business outcome
                                                        │                       or debuggable failure
                                                        └─► stuck or risky? ──► human takes the SAME
                                                                                live session, acts,
                                                                                releases, run resumes
```

The target is **MeridianCore**, a mock legacy credit-union servicing console
included in this repo: a frameset with nested iframes, table-based layout,
`<td onclick=...>` navigation, no test ids and no `<label for>`. It is
deliberately hostile, because that is what the real systems look like.

- **[REPORT.md](REPORT.md)** — architecture, artifact schema, determinism and
  error handling, heterogeneity and multi-tenancy, escalation, safety, cuts.
- **[DECISIONS.md](DECISIONS.md)** — the open-ended choices, the alternatives
  considered, and what each one costs.
- **[evidence/README.md](evidence/README.md)** — the real discovery run, six
  deterministic replays, and a human takeover performed by an actual person,
  annotated.
- **[requirements_matrix.md](requirements_matrix.md)** — every requirement, where
  it lives, what proves it.

---

## Setup

Requires **Python 3.11+** and [`uv`](https://docs.astral.sh/uv/getting-started/installation/).
One command:

```bash
./scripts/setup.sh
```

That creates `.venv`, installs the package, downloads Chromium for Playwright,
and copies `.env.example` to `.env`.

### Configuration

Edit `.env`:

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | `cua discover` only | Replay, tests and the catalog never call a model. |
| `CUA_MODEL` | optional | Defaults to `claude-sonnet-5`. |
| `MERIDIAN_OPERATOR_ID` | discovery + replay | Sign-on id for the mock console. Fixture value: `ops.demo`. |
| `MERIDIAN_OPERATOR_PASSCODE` | discovery + replay | Read from the environment, never stored in artifacts, logs or evidence. Fixture value is in `.env.example`. |
| `MERIDIAN_OVERRIDE_CODE` | the handoff demo | The supervisor override the operator applies. |
| `MERIDIAN_BASE_URL` | optional | Defaults to `http://127.0.0.1:8799`. If you change it, add the new origin to `config/allowlist.yaml` — otherwise the safety gate will refuse the run, which is the intended behaviour. |

No other services are required. `.env` is gitignored; `.env.example` contains
only fixture values for the local mock app.

---

## Demo path

Start the target application in one terminal and leave it running:

```bash
.venv/bin/cua serve-app
```

Then, in another terminal:

**1 — Run the agent on a goal, and save the artifact.** This is the only step
that uses a model. It records the flow, probes how the application reports
failures, synthesizes the artifact, then replays it once to promote it from
`draft` to `approved`.

```bash
.venv/bin/cua discover --task tasks/member_savings_lookup.yaml
```

The goal and the typed contract live in
[`tasks/member_savings_lookup.yaml`](tasks/member_savings_lookup.yaml). The
artifact is written to `artifacts/member_savings_balance_lookup/v1.0.0.json` and
evidence to `evidence/discovery/<run_id>/`.

**2 — Replay it deterministically, with different inputs.** No model is used.

```bash
.venv/bin/cua replay -c member_savings_balance_lookup -i member_id=100731
```

**3 — Replay the exceptional paths.**

```bash
.venv/bin/cua replay -c member_savings_balance_lookup -i member_id=999999   # business outcome
```
```bash
.venv/bin/cua replay -c member_savings_balance_lookup -i member_id=oops     # invalid input
```
```bash
.venv/bin/cua inject app_error && .venv/bin/cua replay -c member_savings_balance_lookup -i member_id=100244 ; .venv/bin/cua inject none
```

**4 — Escalation, same-session human takeover, resume.** Member 100999 is
entitlement-restricted; a supervisor override clears it.

```bash
.venv/bin/cua replay -c member_savings_balance_lookup -i member_id=100999 --operator scripted
```

**5 — Call it the way an AI agent would.**

```bash
.venv/bin/cua catalog tools
```
```bash
.venv/bin/cua invoke member_savings_balance_lookup -i member_id=100244
```

### Or run the whole thing at once

```bash
./scripts/demo.sh
```

Starts the mock app, runs discovery, and walks every path above.

---

## Running without model access

Everything except discovery works with no API key, because replay never calls a
model. The artifact is committed, so a reviewer can go straight to the
production path:

```bash
./scripts/replay.sh
```

That starts the mock app and runs the happy path, the business outcome, the
invalid input, the injected failure and the human handoff. The full test suite
is also model-free:

```bash
./scripts/test.sh
```

117 tests, none of which call a model: schema and validation, templating and
typed coercion, locator resolution and candidate generation, predicate
evaluation, the safety gate and redactor, the control state machine and operator
console, synthesis, the catalog, and ten browser-backed end-to-end replays
covering every runtime-condition category.

### Verifying that replay really uses no model

The claim that production replay has no LLM in the decision loop is the whole
point of the design, so it is made falsifiable rather than asserted:

```bash
./scripts/prove_no_llm.sh
```

It runs a real replay inside a process with three seals — every `*_API_KEY`
stripped from the environment, the model SDKs made unimportable, and outbound
sockets blocked to everything except `127.0.0.1` — and the replay completes
anyway.

Then it runs the control experiment, which matters just as much: **discovery**
under the identical seals, which must fail. A test that cannot fail proves
nothing, so the seals are shown to bite before the replay result is believed.

It finishes by checking that no replay run has ever written a `model_trace.jsonl`
— discovery writes one with an entry per model call; replay has no such file,
because there is nothing to trace.

The underlying reason is structural: nothing under `src/cua/replay/` imports a
model client, directly or transitively. Replay reads the saved artifact — which
control to find, how long to wait, what text proves the step worked — and
follows it.

---

## Human-in-the-loop with a real person

This is the one part of the demo you drive yourself. It takes about a minute.

```bash
./scripts/handoff_demo.sh
```

**Two windows open.** A Chromium window — that is the live session the
automation is driving — and an operator console at **http://127.0.0.1:8811**.
The terminal prints the console URL as it starts; if that port is busy it picks
another and tells you which.

The run signs on, searches for member `100999`, and stops: that member is
entitlement-restricted. The console shows why, and the browser window is parked
on the "Access Restricted" screen.

**Then you do this:**

| | Where | What |
|---|---|---|
| 1 | Console | Click **Take control**. |
| 2 | **Chromium window** | Click **Supervisor Override**. |
| 3 | **Chromium window** | Type the override code into **Override Code**, then click **Apply Override**. |
| 4 | Console | Click **Release & resume**. |

The override code is `MERIDIAN_OVERRIDE_CODE` in your `.env` (`OVR-4417` by
default).

**What happens next.** Automation takes the session back, re-checks the step it
was blocked on, finds you already got the screen there, and finishes — returning
the balance for a member it could not reach on its own. Your clicks appear in
the result as recorded human actions.

### Things that trip people up

- **Act in the Chromium window, not the console.** The console is a control
  panel; it shows a screenshot, not a live page. Clicking the screenshot does
  nothing.
- **Don't open a new tab or a second browser.** The whole point is that you and
  the automation share one session. A new tab is a different session and will not
  be signed in.
- **Order doesn't have to be perfect.** If you fix the problem before pressing
  *Take control*, your actions are still recorded and *Release & resume* still
  works — the recorder is live from the moment the run pauses.
- **Taking your time is fine.** The run waits up to 15 minutes; there is no
  penalty for a slow first attempt.

### Prefer to watch it without driving?

The same path runs unattended with a scripted stand-in operator, headless and in
about ten seconds:

```bash
.venv/bin/cua replay -c member_savings_balance_lookup -i member_id=100999 --operator scripted
```

It uses the identical control-transfer machinery and its recorded actions are
tagged `"simulated": true`, so the evidence never overstates what happened.

---

## Command reference

| Command | Purpose |
|---|---|
| `cua serve-app` | Run the MeridianCore mock console. |
| `cua discover --task <yaml>` | LLM-driven discovery → artifact. The only command that uses a model. |
| `cua replay -c <capability> -i k=v` | Deterministic replay. `--operator scripted\|console\|none`, `--headed`, `--json`. |
| `cua invoke <capability> -i k=v` | Agent-style invocation; typed JSON result. Refuses drafts unless `--allow-draft`. |
| `cua approve <capability> -i k=v` | Verify by replaying, then promote draft → approved. |
| `cua catalog list` / `catalog tools` / `catalog show <id>` | What an agent can call, and its tool schema. |
| `cua validate [artifact]` | Structure, safety and provenance checks. |
| `cua inject none\|app_error\|slow\|expire` | Fault injection on the mock app. Operator tooling — the allowlist denies the agent this route. |
| `./scripts/prove_no_llm.sh` | Run a replay sealed off from every model SDK, key and network, plus the control experiment. |

---

## Repository layout

```
src/cua/
  surface/      perception + physical action. The only place Playwright exists.
                perceive.js builds an accessibility-style tree across frames;
                desktop.py is the documented, unimplemented seam.
  discovery/    the model loop (observe → decide → act) and artifact synthesis.
  artifact/     the schema, store, validation and value binding.
  replay/       the deterministic engine, predicates and result contract.
  safety/       allowlist, risk policy, redaction. One gate, used by both loops.
  handoff/      control state machine, intervention requests, human action
                recorder, operator console.
  observability/ structured run log and evidence capture.
  catalog/      artifacts projected as agent-callable tools.
mockapp/        the MeridianCore legacy console.
tasks/          discovery task definitions.
config/         allowlist profiles.
artifacts/      the capability store (JSON, reviewable in diffs).
evidence/       discovery and replay evidence.
```

---

## A note on scope

This is a vertical slice, not a product. The pieces that are deliberately
minimal, mocked or cut — the desktop surface, multi-tenant infrastructure,
streaming co-browsing, assisted recovery — are listed with their reasoning and
their production seam in [REPORT.md § Cuts](REPORT.md#cuts). Where something is
simulated, the evidence says so explicitly.
