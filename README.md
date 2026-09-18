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

Two capabilities are recorded against it:

| Capability | What it does | Why it is here |
|---|---|---|
| `member_savings_balance_lookup` | Sign on → search → read a member's Regular Savings balance and status | The read path: typed outputs, business outcomes, a not-found result |
| `member_stop_payment_request` | Sign on → search → multi-field stop-payment form → **confirmation screen with a host reference** | The write path: the commit is human-only, so it escalates; `Transfer Funds` on the same screen is refused outright |

- **[REPORT.md](REPORT.md)** — architecture, artifact schema, determinism and
  error handling, heterogeneity and multi-tenancy, escalation, safety, cuts.
- **[DECISIONS.md](DECISIONS.md)** — the open-ended choices, the alternatives
  considered, and what each one costs.
- **[evidence/README.md](evidence/README.md)** — two real discovery runs, the
  deterministic replays behind every result status, and a human takeover
  performed by an actual person, annotated.

---

## Quickstart — 2 minutes, no API key needed

Both recorded capabilities are committed, so you can run the production path
immediately. Nothing here calls a model.

```bash
./scripts/setup.sh
```
```bash
./scripts/replay.sh
```

`setup.sh` creates the virtualenv, installs the package and downloads Chromium
(~1 min, mostly the browser download). `replay.sh` starts the mock application
and runs five paths in about 40 seconds. You should see exactly this:

```text
==> happy path
[success] … current_savings_balance=4812.37, savings_account_status=OPEN, member_name=Dana Whitfield
==> business outcome
[business_outcome] … member_not_found: The searched member number does not exist …
==> invalid input
[invalid_input] … input 'member_id' value does not match the declared pattern '[0-9]{6}'
==> injected failure
[failure] … host_system_fault at step s06_search_member_s_account_summary …
==> human handoff
[success] … current_savings_balance=27640.18, savings_account_status=OPEN, member_name=Priya Raghunathan
```

Five different statuses, one artifact. That is the whole argument of the project
in one command. Each run writes to `evidence/replay/<run_id>/` — `result.json`
is the structured result, `run.jsonl` is the step-by-step log, `screens/` is
what the machine saw.

The write-path capability runs the same way, once the mock app is up
(`.venv/bin/cua serve-app` in another terminal). It pauses for an operator
before it commits, which is the point:

```bash
.venv/bin/cua replay -c member_stop_payment_request -i member_id=100731 -i check_number=884 -i check_amount='$91.40' -i requested_by='R. Okafor' --operator scripted
```

```text
[success] member_stop_payment_request v1.0.0 — stop_payment_reference=SP-100731-884-32, request_status=RECORDED
  human actions recorded: 3
    - clicked the 'Place Stop Payment' button
```

Then, if you want:

```bash
./scripts/test.sh
```

137 tests in about 75 seconds, none of which call a model: schema and
validation, templating and typed coercion, locator resolution and candidate
generation, predicate evaluation, the safety gate and redactor, the control
state machine and operator console, synthesis, the catalog, replay pre-flight,
fault staging, and ten browser-backed end-to-end replays covering every
runtime-condition category.

---

## Setup, in detail

Requires **Python 3.11+** and [`uv`](https://docs.astral.sh/uv/getting-started/installation/).

```bash
./scripts/setup.sh
```

It creates `.venv`, installs the package, downloads Chromium for Playwright, and
copies `.env.example` to `.env`.

**You do not need to edit anything to run the quickstart.** `.env.example`
already contains working fixture values for the local mock application. The only
variable you must supply yourself is `ANTHROPIC_API_KEY`, and only if you want
to run discovery.

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | `cua discover` only | Supply your own. Replay, tests and the catalog never call a model. |
| `CUA_MODEL` | optional | Defaults to `claude-sonnet-5`. |
| `MERIDIAN_OPERATOR_ID` | discovery + replay | Sign-on id for the mock console. Ships as `ops.demo`. |
| `MERIDIAN_OPERATOR_PASSCODE` | discovery + replay | Read from the environment, never stored in artifacts, logs or evidence. Ships with a fixture value. |
| `MERIDIAN_OVERRIDE_CODE` | the handoff demo | The supervisor override. Ships as `OVR-4417`. |
| `MERIDIAN_BASE_URL` | optional | Defaults to `http://127.0.0.1:8799`. If you change it, add the new origin to `config/allowlist.yaml` — otherwise the safety gate refuses the run, which is the intended behaviour. |

`.env` is gitignored. `.env.example` contains only fixture values for the local
mock app; there are no real credentials anywhere in this repository.

---

## The full demo, including discovery

This is the only part that calls a model. One discovery run is 22 model calls
and takes about 90 seconds.

```bash
./scripts/demo.sh
```

It starts the mock application itself, then walks the whole slice: discovery →
artifact → smoke replay → approval → happy path → business outcome → invalid
input → injected failure → human handoff → agent invocation. Roughly 3 minutes
end to end.

If you already have the mock app running on port 8799, that is fine — the script
notices and uses it.

### Or step by step

Start the application and leave it running in its own terminal:

```bash
.venv/bin/cua serve-app
```

**1 — Run the agent on a goal and save the artifact.** ~90s. The goal and the
typed contract are in
[`tasks/member_savings_lookup.yaml`](tasks/member_savings_lookup.yaml).

```bash
.venv/bin/cua discover --task tasks/member_savings_lookup.yaml
```

The agent signs on, works out the flow, extracts the outputs, then deliberately
probes how the application reports failures so the error rules are grounded in
wording it actually saw. It writes
`artifacts/member_savings_balance_lookup/v1.0.0.json`, replays it once to
promote it from `draft` to `approved`, and leaves evidence in
`evidence/discovery/<run_id>/`.

**2 — Replay it deterministically, with a different member.** ~10s, no model.

```bash
.venv/bin/cua replay -c member_savings_balance_lookup -i member_id=100731
```

```text
[success] member_savings_balance_lookup v1.0.0 — current_savings_balance=129.05, savings_account_status=OPEN, member_name=Marcus Ellery
```

Member `100731` was never seen during recording. The flow is parameterized, not
a transcript.

**3 — Replay the exceptional paths.** Each ~10s.

```bash
.venv/bin/cua replay -c member_savings_balance_lookup -i member_id=999999
```
A legitimate business answer, not a crash: `status=business_outcome`,
`outcome=member_not_found`.

```bash
.venv/bin/cua replay -c member_savings_balance_lookup -i member_id=oops
```
The caller's argument fails its declared pattern: `status=invalid_input`,
rejected in 1 ms without touching the application.

```bash
.venv/bin/cua inject app_error && .venv/bin/cua replay -c member_savings_balance_lookup -i member_id=100244 ; .venv/bin/cua inject none
```
A genuine host fault: `status=failure` with the failed step, what was expected,
what was observed, and a screenshot, accessibility snapshot and DOM snapshot.

**4 — Escalation, same-session human takeover, resume.** ~12s. Member `100999`
is entitlement-restricted; a supervisor override clears it. This runs with a
scripted stand-in operator so it needs no interaction — see
[the section below](#human-in-the-loop-with-a-real-person) to do it yourself.

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

The first emits JSON-schema tool definitions; the second invokes the capability
by name and returns a typed JSON result.

**6 — The write path: a state-changing flow that needs a person.** ~15s each.
Recording it is a separate discovery run against a separate capability id:

```bash
.venv/bin/cua discover --task tasks/member_stop_payment.yaml
```

The goal ends on a confirmation screen, so the flow has a committing control.
Under the default profile that control is human-only, and the recorded artifact
says so in `safety.steps_requiring_approval` — an unattended replay pauses for
an operator before anything is recorded against the member:

```bash
.venv/bin/cua replay -c member_stop_payment_request -i member_id=100731 -i check_number=884 -i check_amount='$91.40' -i requested_by='R. Okafor' --operator scripted
```

The same capability under a second institution's profile, which classifies a
stop payment as irreversible rather than merely approval-worthy, does every
harmless step and then refuses:

```bash
.venv/bin/cua replay -c member_stop_payment_request -i member_id=100244 -i check_number=2041 -i check_amount='$482.60' -i requested_by='T. Alvarez' --allowlist-profile no_unattended_writes --operator scripted
```
```text
[blocked_by_policy] … policy_risk_ceiling at step s11_submit_stop_payment_request
```

And the two runtime conditions that need a staged fault — a session timeout that
rewinds the flow to sign-on, and a slow host the declared waits absorb:

```bash
.venv/bin/cua inject expire && .venv/bin/cua replay -c member_stop_payment_request -i member_id=100244 -i check_number=2041 -i check_amount='$482.60' -i requested_by='T. Alvarez' --operator scripted
```
```bash
.venv/bin/cua inject slow && .venv/bin/cua replay -c member_savings_balance_lookup -i member_id=100244 ; .venv/bin/cua inject none
```

---

## Verifying that replay really uses no model

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
- **Don't close the Chromium window.** That window *is* the shared session, so
  closing it ends the run. You will get a clear `session_closed` failure rather
  than a confusing crash, but the run is over — use *Release & resume* instead.

### Prefer to watch it without driving?

The same path runs unattended with a scripted stand-in operator, headless and in
about ten seconds:

```bash
.venv/bin/cua replay -c member_savings_balance_lookup -i member_id=100999 --operator scripted
```

It uses the identical control-transfer machinery and its recorded actions are
tagged `"simulated": true`, so the evidence never overstates what happened.

---

## If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `Address already in use` / `Port 8799 is in use` | A mock app is already running. | Harmless if you meant to reuse it — the scripts detect and use it. Otherwise `pkill -f mockapp.app`. |
| `blocked_by_policy … origin … is not in the allowlist` | You changed `MERIDIAN_BASE_URL`. | This is the safety gate working. Add the origin to `config/allowlist.yaml`. |
| `ANTHROPIC_API_KEY is not set` | Only `cua discover` needs it. | Add it to `.env`, or skip discovery — the artifact is already committed. |
| `the target application is not answering` | The mock app is not running. | `.venv/bin/cua serve-app`, or use `./scripts/demo.sh` which starts it for you. |
| `invalid_input … does not match the declared pattern` | Member ids are six digits. | Try `100244`, `100731`, `100999`, or `999999` for the not-found path. |
| `session_closed` during a handoff | The Chromium window was closed. | That window *is* the shared session. Re-run and use *Release & resume*. |
| `playwright … Executable doesn't exist` | Chromium was not downloaded. | `.venv/bin/python -m playwright install chromium`. |

Every run leaves a full record under `evidence/<phase>/<run_id>/`: `result.json`
(the structured result), `run.jsonl` (what happened and why), `screens/` and,
on failure, `snapshots/` with the accessibility tree and DOM at the moment it
broke. That is usually faster than re-running with more logging.

---

## Command reference

| Command | Purpose |
|---|---|
| `cua serve-app` | Run the MeridianCore mock console. |
| `cua discover --task <yaml>` | LLM-driven discovery → artifact. The only command that uses a model. |
| `cua replay -c <capability> -i k=v` | Deterministic replay. `--operator scripted\|console\|none`, `--allowlist-profile`, `--headed`, `--json`. |
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
config/         allowlist profiles — `default`, and a second-tenant profile
                that refuses the same capability's commit.
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
