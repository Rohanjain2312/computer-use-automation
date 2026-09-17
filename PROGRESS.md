# Progress

State of the work. See `requirements_matrix.md` for the requirement-by-requirement
view and `DECISIONS.md` for why things are the way they are.

## Status: complete

Every MUST-HAVE in the assignment is implemented and demonstrated by evidence in
`evidence/`. The acceptance checklist in the specification has been verified
item by item.

## What exists

- **Target surface** — `mockapp/`, a frameset-based legacy servicing console
  with table layout, no test ids, an interstitial, an entitlement block, and a
  fault-injection endpoint the agent cannot reach.
- **Discovery** — a real `claude-sonnet-5` run (22 calls) that completed the
  goal and then probed four runtime conditions on the same session.
- **Artifact** — `artifacts/member_savings_balance_lookup/v1.0.0.json`, typed,
  versioned, checksummed, `approved`, verified by a replay whose run id it
  records.
- **Replay** — seven recorded runs covering success, cross-record
  parameterization, business outcome, invalid input, hard failure, human
  handoff, and agent-style invocation.
- **Tests** — 113, all passing, none requiring a model.

## Commands that work

```bash
./scripts/setup.sh                                        # env + chromium + .env
./scripts/test.sh                                         # 113 tests, no API key
./scripts/replay.sh                                       # every path except discovery
./scripts/demo.sh                                         # full slice, needs ANTHROPIC_API_KEY
./scripts/handoff_demo.sh                                 # real human takeover, headed
.venv/bin/cua discover --task tasks/member_savings_lookup.yaml
.venv/bin/cua replay -c member_savings_balance_lookup -i member_id=100731
```

## Things worth knowing if you pick this up

- **Discovery costs a model call; nothing else does.** `replay/` imports no
  model client, deliberately. Keep it that way — it is the property the design
  is arguing for.
- **The perception script is the subtle part.** `src/cua/surface/perceive.js`
  decides what a "role" and an "accessible name" mean for markup that has
  neither. Three real bugs during development came from it: table headers being
  inferred from a form's second row, the cell *above* being treated as a label
  in a key/value panel, and the recorder's guard flag stopping re-attachment
  after a frame navigated. All three have tests.
- **Synthesis must never bake in the run's own data.** Checkpoints, markers,
  messages, target descriptions and recorded URLs are all filtered against the
  values the run was given and read. Without it the artifact leaks PII and only
  replays for one record. `tests/test_synthesis_and_catalog.py` covers this.
- **The dangerous bug class is misclassification**, not crashes: reporting a
  legitimate "no such member" as a system failure, or an outage as an answer.
  That is why a failed checkpoint re-runs the condition scan before failing, and
  why outcome markers must be grounded in observed text.

## Next, in order of value

1. Overlay resolution for tenant variants (`base + variant` artifact merge).
2. `cua stability` — replay N times, report a flakiness score, gate approval on it.
3. Desktop surface: implement `observe()` against macOS `AXUIElement`.
4. Bounded assisted recovery, in a separate module, never on the unattended path.
