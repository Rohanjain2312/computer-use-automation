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
- **[evidence/README.md](evidence/README.md)** — the real discovery run and
  seven replay runs, annotated.
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
| `MERIDIAN_BASE_URL` | optional | Defaults to `http://127.0.0.1:8799`. |

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

 tests: schema and validation, templating and typed coercion, locator
resolution and candidate generation, predicate evaluation, the safety gate and
redactor, the control state machine, synthesis, the catalog, and ten
browser-backed end-to-end replays covering every runtime-condition category.

---

## Human-in-the-loop with a real person

```bash
./scripts/handoff_demo.sh
```

A headed browser opens and the run pauses on the entitlement block. The console
URL is printed. Open it, click **Take control**, perform the supervisor override
yourself **in the browser window the automation is driving** (the code is in
`.env`), then click **Release & resume**. Automation re-verifies the step,
finds the human already got it there, and finishes the run. Your actions are
recorded and appear in the result.

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
