# Architecture

One Python process, five boundaries, arrows pointing one way.

```
 tasks/*.yaml ──► discovery/ ──► CapabilityArtifact ──► replay/ ──► ReplayResult
   goal, typed      (model in         (artifacts/)      (no model)      (typed)
   contract          the loop,
                     exactly once)
                          │                                 │
                          └────────────┬────────────────────┘
                                       ▼
                     surface/  perception + physical action
                     safety/   one policy gate, one redactor
                     handoff/  who owns the live session
                     observability/  structured log + evidence
```

**`surface/`** is the only place that knows Playwright exists. Everything above
speaks `UiNode` — role, accessible name, value, bounding box, frame, table
context — plus five physical actions. Perception walks every reachable document
and resolves names as a person would, including "the table cell to the left",
which is how legacy forms are actually labelled. The agent clicks the coordinates
the screenshot shows, so what it sees and what it touches cannot diverge.

**`discovery/`** runs the model loop with a tool vocabulary restricted to what a
human at a keyboard could do: no "run this JavaScript", no way to hand it a
selector. **`artifact/`** is the contract, and **`replay/`** executes it while
importing no model client, structurally. **`safety/`**, **`handoff/`** and
**`observability/`** serve both loops identically — the same `PolicyGate` decides
whether the model may click a button and whether replay may.

**The trade-off.** A run owns a live browser session *and* a live handoff
channel, both session-affine, so a queue would have to pin work to the process
holding the session or serialize session state — the anti-pattern the handoff
requirement forbids. Concurrency is therefore bounded by the host, and horizontal
scale needs a session-pool coordinator in front of `_replay` that changes nothing
below it (`DECISIONS.md` D11).

# Artifact schema

A `CapabilityArtifact` is typed with pydantic, serialized as JSON, versioned
twice — `schema_version` for the format, `version` for the recording — and signed
with a checksum. It lives in the repository because its purpose is to be
*reviewed*: it diffs in a pull request, and "who changed this capability and why"
becomes a question git already answers.

*Targeting is a ranked plan, not a selector.* Each `TargetPlan` holds ordered
strategies — `role_name`, `label_proximity`, `text_anchor`, `table_cell`,
`dom_hint`, `viewport_ratio` — each with a confidence derived from measured
uniqueness on the live screen and a written rationale. The engine generates them
from what the surface perceived; the model never writes a locator, so the same
trajectory always synthesizes the same targeting.

*Controls and values are targeted differently.* A button labelled "Search" is
legitimately found by its text; a cell showing `$4,812.37` must not be, because
that is the datum being read. Value targets get `table_cell` instead
(`DECISIONS.md` D3).

*Inputs carry a sensitivity and a source.* A `secret` must come from the
environment and may not carry an example; steps reference
`{{ inputs.operator_passcode }}`, so the artifact says "type the passcode here"
and never contains one. *Predicates are one mechanism used four ways* —
checkpoints, outcome detectors, recovery triggers and failure detectors are the
same `Predicate` tree, so there is one evaluator and one place to be wrong.
*Provenance references the transcript, never contains it*: run id, model, call
count, goal, a SHA-256 of the redacted trace, and where it lives.

`catalog/tools.py` projects an artifact into a JSON-schema tool definition for a
calling agent, with a result schema enumerating the outcomes it must handle.
Environment-sourced inputs are omitted: the agent cannot pass a credential and
should not know one exists.

# Determinism & error handling

Replay takes an artifact plus typed inputs and consults no model. Per step:
**observe → scan conditions → resolve target → policy gate → act → wait for the
declared condition → verify the checkpoint.**

**Pre-flight.** The artifact is validated and its `surface.required_strategies`
checked against `Surface.capabilities()`, so a surface that cannot resolve what
the recording used fails with `surface_unsupported` before step 0 rather than
part-way through with the application half-driven.

**Locators** are tried in confidence order and the first *unambiguous* match
wins. If all are ambiguous the best is used with a reading-order tie-break and
flagged `ambiguous`, because a consistent resolution beats a hard stop provided
the evidence says so. A resolving fallback sets `strategy_degraded`: the run
still succeeds, but the artifact has started to rot, visibly.

**Waits** are never sleeps — each is a condition with a timeout, and the
condition is `any_of(expected state, every recognized outcome, every recognized
failure)`, so a slow host costs latency but not correctness while a host
returning an error page fails in milliseconds. Recovery rules re-evaluate on
every poll. **Checkpoints** are mandatory on any step that changes screen state,
because a click executing is not evidence that it worked.

**Runtime conditions sort into three kinds.** A *business outcome* is a
legitimate answer, so there is no failure object; its detectors are grounded, in
that the probe phase makes the model quote wording it actually saw and any marker
never observed is dropped at synthesis. A guessed error string would misclassify
a legitimate answer as a crash — the worst bug this system could ship. A
*recoverable* condition fires a `RecoveryRule` keyed on page state, not step
index, declaring what follows: retry, continue, or **resume from an earlier
step** — the third because an expired session invalidates everything the flow
established, so retrying the step that noticed is wrong. A *hard failure* reports
the step, what was expected and observed, every locator attempt, and a
screenshot, accessibility snapshot and DOM snapshot. Two further statuses keep
distinct things apart: `invalid_input` (the caller's arguments failed the
contract; the application was never touched) and `blocked_by_policy`.

Ordering matters most at the boundary: a failed checkpoint re-runs the condition
scan *before* being treated as a failure, because the commonest reason a
checkpoint fails is that the application answered something legitimate instead
(`DECISIONS.md` D12).

# Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `Surface`: above it everything speaks
`UiNode` and `Observation`, below it a surface implements perception and five
physical actions. Nothing above `surface/` imports Playwright, so the recorded
flow is coupled not to an automation library but to the idea of *a screen with
named controls on it*.

Legacy web is not an extension, it is the implemented target — a frameset with
nested iframes, table layout, `<td onclick=...>` navigation, no test ids, no
`<label for>`. `surface/desktop.py` is interface-complete and deliberately
unimplemented, carrying the per-strategy mapping (`role_name` →
`AXRole`+`AXTitle` / UIA `ControlType`+`Name`, `table_cell` → `AXTable`/`AXRow` /
UIA `Grid`, and so on). Exactly one strategy, `dom_hint`, has no desktop
analogue, which is what `capabilities()` is for: the pre-flight refuses an
artifact needing more than the surface has, and within a step the resolver skips
an unsupported strategy and records the skip.

That pre-flight is coarser than it looks, though. `required_strategies` is the
*union* over every target and candidate generation always appends a `dom_hint`
fallback, so every web recording lists it and the check refuses web artifacts on
desktop as a class rather than judging each on its merits. Refusing up front is
still right; making the signal diagnostic is under Cuts.

**Multi-tenant reuse.** Artifacts bind to a tenant *by reference, not value*:
`TenantBinding{tenant_id, variant}` and an entry point of `{{ config.base_url }}`
rather than a baked host. Recorded URLs are canonicalized — `/members/100244`
becomes `/members/\d+$` — so a checkpoint is about the *shape* of where you are,
not one record. Policy is configuration too: `config/allowlist.yaml` ships a
second profile for the same product, under which `member_stop_payment_request`
replays unchanged and stops at `blocked_by_policy` instead of escalating, because
that institution classifies a stop payment as irreversible.

Designed, not built: resolve an artifact as `base capability + variant overlay`,
patching only what differs — usually a step's `TargetPlan` or an outcome marker's
wording, both already small declarative objects. A tenant runs the base until
something degrades, and that degradation signal triggers an overlay rather than a
whole re-recording; `AppProfile.version_observed` groups the same signal by
vendor version, separating a vendor upgrade from a tenant quirk. No tenant
registry or overlay resolution exists — that is infrastructure for theoretical
scale.

# Escalation & handoff

**Detecting "stuck".** Four triggers, all real: the policy gate returns
`requires_human`; a business outcome declares `disposition: escalate_to_human`
(an entitlement block is a legitimate result a person can clear); the model calls
`request_human_help`; or discovery's repeat guard sees the screen unchanged
across several actions. The second capability makes this the ordinary path rather
than the exceptional one — `member_stop_payment_request` commits through a
control the default profile marks human-only, so every unattended replay pauses
for an operator, `safety.steps_requiring_approval` says so before anyone runs it,
and invoked with no operator channel it fails cleanly rather than committing.

**The intervention request** carries the capability, goal, step and why
automation stopped, plus the observed URL, a screen excerpt, a screenshot, a DOM
snapshot, suggested manual steps and what happens on resume.

**Same live session.** The `BrowserContext` is never torn down. `SessionControl`
holds `owner ∈ {automation, human}` and a state machine (`running →
paused_pending_human → human_control → resuming → running`), and
`assert_can_act()` runs before every action, so "automation stopped acting while
the human was in control" is enforced by an object rather than by discipline. The
operator clicks **Take control** in a small console and acts *in the browser
window automation is driving*, then **Release & resume**; the console is a
control plane that never touches the browser itself (`DECISIONS.md` D9).
Serializing cookies so a human could open their own browser would lose in-page
state and is the anti-pattern the requirement names.

**Resume** re-verifies the step's checkpoint before repeating work, because the
human may already have produced the state the step was reaching for. Rewinds and
escalations are budgeted, so a condition nobody can clear becomes a clear failure
rather than a loop. Capture-phase listeners record what the human did — clicks,
field changes and submits by accessible name, never password fields — and go in
when the session *pauses*, not when *Take control* is pressed, so someone who
fixes the problem first is still recorded, flagged `before_takeover`
(`DECISIONS.md` D13).

**What is mocked.** The operator interface is minimal but real, and the repo
holds a run of it performed by an actual person — every action `"simulated":
false`. For headless runs a `ScriptedOperator` drives the identical seam and tags
everything `simulated: true`.

# Safety

**Allowlist.** `config/allowlist.yaml`, profile-based. A URL must satisfy both
origin and path, denied patterns beat allowed ones, action types are enumerated,
and it is checked before every navigation *and* after every action, because a
click can navigate somewhere a navigation never would. The fault-injection
endpoint is explicitly denied: it is operator tooling.

**Risk is computed twice** (`DECISIONS.md` D7). Three levels — `read_only`,
`reversible_write`, `irreversible_write`. The artifact declares one per step,
visible to a reviewer; the gate re-derives it at execution from the live
control's label; the stricter wins. Without this the artifact becomes a trust
boundary, and a hand-edited one could declare a "Transfer Funds" click as
`read_only` and walk past the ceiling.

**Policy.** Irreversible actions are **blocked**, not confirmed: the cost of an
unintended one in a back office is unbounded, the cost of a paused run is an
operator's minute, and confirmation prompts get answered reflexively
(`DECISIONS.md` D8). Blocking is not failing — controls needing judgement rather
than refusal are marked human-only and route to a person. The stop-payment screen
shows both at once: `Place Stop Payment` escalates, `Transfer Funds` beside it is
refused outright, and `/evidence/` has a run of each.

**Secrets and PII.** Secret inputs come from the environment or the schema
refuses them, and discovery refuses a `type_text` matching a registered secret.
Redaction is on every persistence path by construction — `RunLogger` and
`EvidenceWriter` scrub before writing, and there is no unredacted write. Two
mechanisms, because patterns alone are not enough: registered values are replaced
exactly, and patterns catch SSNs, Luhn-valid cards, emails, phones and API-key
shapes never declared. Outputs marked `pii` go to the caller — that is the point
of the capability — but are redacted everywhere they are stored, and screenshots
mask secret fields *before* capture, since an image cannot be regex-scrubbed.

**Limits.** Redaction covers what a capability *declares*: a name is not a
pattern, so it is scrubbed only because the artifact types it `pii`. A flow that
merely passes a screen showing data it never declares will capture it in DOM and
accessibility snapshots — which is why a capability should declare the sensitive
fields its screens show, not only the ones it returns. Discovery is weaker still:
sensitivity is *learned* there, so anything captured before a value is classified
retains it, which is why discovery runs against synthetic fixtures
(`DECISIONS.md` D14). Label-based risk classification is a heuristic — the
over-triggering asymmetry is deliberate, but production wants a reviewed per-app
action registry. Redaction patterns are Western-formatted, masking covers only
fields the surface knows are secret, the allowlist constrains the agent rather
than the application, and the checksum detects tampering without preventing it.

# Cuts

**Desktop surface.** The largest single piece of work here, demonstrating nothing
about the *design* that the web surface does not. `surface/desktop.py` is
interface-complete with the AX/UIA mapping and an honest `capabilities()`. Next:
`observe()` against macOS `AXUIElement` for one real application — nothing above
`surface/` should change, and if it does the abstraction was wrong.

**Multi-tenant infrastructure.** No registry, no overlay resolution, no
credential brokering; the assignment says not to build for theoretical scale.
Next: `base + variant overlay` resolution in `ArtifactStore`, keyed on
`(tenant_id, variant, app version)`.

**Assisted LLM fallback on replay failure.** Deliberately skipped: a bounded
one-step recovery is a real feature, but `replay/` importing a model client turns
this project's central guarantee from a fact into a policy. Next, if built:
`replay/assisted.py`, entered only after a hard failure, policy-checked per
action, capped at one step, never unattended.

**Precise portability signalling.** `required_strategies` is a union over all
targets, so it is conservative rather than diagnostic. Next: record the minimal
set each target depends on, turning the pre-flight from "this came from the web"
into "these three steps need markup".

**Drift re-recording and stability scoring.** The signals exist —
`strategy_degraded`, ambiguity flags, per-run outcomes — but nothing aggregates
them across runs. Next: `cua stability`, feeding a flakiness score into the
existing `draft → approved` gate.

**Smaller things.** Streaming co-browsing: the console polls a screenshot, with
no operator auth or routing — cut by the assignment's own scoping, and only the
transport would change. Code generation from artifacts demonstrates templating,
not judgement. A `select`'s value is always recorded as a literal, so a dropdown
cannot yet be driven by a typed input. `viewport_ratio` is recorded but is a last
resort, and its use should be read as a signal to re-record. There is no way to
*update* an artifact short of recording a new version.
