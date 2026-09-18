# Architecture

The system is one Python process with five boundaries that matter. Each is a
directory under `src/cua/`, and the dependency arrows only point one way.

```
 tasks/*.yaml ──► discovery/ ──► CapabilityArtifact ──► replay/ ──► ReplayResult
   goal, typed      (model in         (artifacts/)      (no model)      (typed)
   contract          the loop,
                     exactly once)
                          │                                 │
                          └────────────┬────────────────────┘
                                       ▼
                     surface/    perception + physical action
                     safety/     one policy gate, one redactor
                     handoff/    who owns the live session
                     observability/  structured log + evidence
```

**`surface/`** is the only place that knows Playwright exists. It exposes
`observe() -> Observation` and physical actions — click a point, type into
focus, press a key. Everything above it speaks in `UiNode`: role, accessible
name, value, bounding box, frame, table context. Perception is a script that
walks every reachable document (framesets and nested iframes included),
computes roles from behaviour as well as tags, and resolves accessible names the
way a person does — including "the table cell to the left", which is how legacy
forms are actually labelled. The agent clicks the coordinates the screenshot
shows, so what it sees and what it touches cannot silently diverge.

**`discovery/`** runs the model loop — observe, decide, act — with a tool
vocabulary restricted to what a human at a keyboard could do. There is no "run
this JavaScript" tool and no way to hand the system a CSS selector. It runs in
two phases on one session: accomplish the goal, then deliberately probe how the
application reports problems.

**`artifact/`** is the contract. `synthesize.py` converts a trajectory into it.

**`replay/`** executes artifacts. It imports no model client — that is a
structural guarantee, not a policy.

**`safety/`**, **`handoff/`** and **`observability/`** are used identically by
both loops. The same `PolicyGate` decides whether the model may click a button
and whether replay may; the same `SessionControl` decides who owns the session
in either phase.

**Key decisions and trade-offs.** The architecture is deliberately one process
and synchronous. A run owns a live browser session *and* a live human-handoff
channel; both are session-affine, so a queue would either have to pin work to
the process holding the session — a queue that cannot distribute — or serialize
session state, which is the exact anti-pattern the handoff requirement forbids.
The cost is that concurrency is bounded by the host, and horizontal scale needs
a session-pool coordinator. That coordinator would sit in front of `_replay` and
change nothing below it. Full reasoning, with the alternatives, is in
`DECISIONS.md`.

# Artifact schema

A `CapabilityArtifact` (`src/cua/artifact/schema.py`) is typed with pydantic,
serialized as JSON, versioned twice — `schema_version` for the format and
`version` for the recording — and signed with a checksum over its canonical
form. It is stored as a file in the repository because its purpose is to be
*reviewed*: it diffs in a pull request, and "who changed this capability and
why" becomes a question git already answers.

It carries: `surface` (entry point, vendor/product profile, tenant binding,
viewport, required strategies) · `safety` (allowlist profile, risk ceiling,
steps needing approval) · `inputs` · `outputs` · `steps` · `business_outcomes` ·
`recovery_rules` · `failure_rules` · `success_condition` · `provenance` ·
`verification`.

**Why it is shaped this way.**

*Targeting is a ranked plan, not a selector.* Every `TargetPlan` holds ordered
`TargetStrategy` entries — `role_name`, `label_proximity`, `text_anchor`,
`table_cell`, `dom_hint`, `viewport_ratio` — each with a confidence derived from
*measured uniqueness on the live screen* and a written rationale. The engine
generates these deterministically from the control the surface perceived; the
model never writes a locator. That is what makes the artifact reproducible: the
same trajectory always synthesizes the same locators.

Controls and values are generated differently, and that distinction is
load-bearing. A button labelled "Search" is legitimately found by its text. A
cell showing `$4,812.37` must not be — that is the datum being read, so keying
on it would record one member's balance as the way to find every member's
balance, and would write account data into the artifact. Value targets get
`table_cell` ("the Current Balance column of the Regular Savings row") and
adjacent-label strategies instead.

*Inputs carry a sensitivity and a source.* A `secret` input must declare
`source: environment` and may not carry an example — the schema refuses
otherwise. Steps reference values as `{{ inputs.operator_passcode }}`, never
literally, and validation rejects any literal that matches a sensitive-data
pattern. The artifact says "type the passcode here"; it never contains one.

*Predicates are one mechanism, used four ways.* Checkpoints, business-outcome
detectors, recovery triggers and failure detectors are all `Predicate` trees
(`text_present`, `url_matches`, `element_present`, `table_cell_matches`,
`all_of`/`any_of`/`not`). One evaluator, one semantics, one place to be wrong.

*Provenance references the transcript; it never contains it.* `Provenance`
carries the run id, model, call count, goal, a SHA-256 of the redacted trace and
the evidence path where that trace lives. You can prove which conversation
produced the artifact without the artifact being a conversation.

**How it supports replay.** Every field replay needs is declarative: what to
find and how, how long to wait and for what condition, what proves the step
worked, which screens mean something other than success.

**How it supports a calling agent.** `catalog/tools.py` projects an artifact
into a JSON-schema tool definition — description, typed arguments with their
patterns, and a result schema enumerating the outcomes the caller must handle.
Inputs sourced from the environment are omitted: the agent cannot pass a
credential, and should not know one exists.

# Determinism & error handling

Replay takes an artifact plus typed inputs and consults no model. The loop per
step is: **observe → scan conditions → resolve target → policy gate → act →
wait for the declared condition → verify the checkpoint.**

**Locators.** Strategies are tried in confidence order; the first *unambiguous*
match wins. If a strategy matches several controls, it is skipped in favour of
the next; if every strategy is ambiguous, the highest-confidence one is used
with a deterministic reading-order tie-break and the result is flagged
`ambiguous` rather than failing — an ambiguous but consistent resolution beats a
hard stop, provided the evidence says so. When a fallback resolves, the step
record carries `strategy_degraded` and the run log gets a `locator_degraded`
event. That is the drift signal: the run still succeeds, but the artifact has
started to rot, visibly.

**Waits.** Never a fixed sleep. Every wait is a condition with a timeout, and
the condition is `any_of(expected state, every recognized outcome, every
recognized failure)`. So a slow host costs latency but not correctness, and a
host that returns an error page fails in milliseconds instead of burning the
full timeout before anyone looks at what is on screen. Recovery rules are
re-evaluated on every poll, so an interstitial that appears *during* a wait is
still cleared.

**Checkpoints.** Every step that changes screen state must declare one —
validation rejects the artifact otherwise. A click executing is not evidence
that it worked.

**The three categories.**

*Category A — business outcome.* A declared `OutcomeRule` matched: the
application gave a legitimate answer. `status="business_outcome"`, `outcome.name
= "member_not_found"`, no failure object. The detectors are grounded: during the
probe phase the model must quote wording it actually saw, and any marker never
observed during the run is dropped at synthesis with a note. A guessed
error string would silently misclassify a business outcome as a crash, which is
the worst bug this system could ship.

*Category B — recoverable.* `RecoveryRule`s are triggered by page state, not
step index, and are bounded by `max_attempts`. Each declares what follows:
retry the step, continue, or **resume from an earlier step**. The third exists
because an expired session invalidates everything the flow established —
retrying the step that happened to notice is wrong, and without it session
handling degrades into a retry loop. (It did, in testing, before the option
existed.)

*Category C — hard failure.* `status="failure"` with the failed step id, index
and intent, what was expected, what was observed, the URL, every locator attempt
made, which recoveries were tried, and paths to a screenshot, an accessibility
snapshot and a DOM snapshot.

Two further statuses keep distinct things distinct: `invalid_input` (the
caller's arguments failed the declared contract — the application was never
touched) and `blocked_by_policy`.

**The ordering that matters most.** A failed checkpoint re-runs the condition
scan *before* it is treated as a failure, because the commonest reason a
checkpoint fails is that the application answered something legitimate instead.
And because a scan can change the page — a recovery ran, a human cleared a block
— it reports that, and the engine re-observes before deciding anything. Getting
this wrong produced two real bugs during development, both caught by the
end-to-end tests.

**UI drift, secondarily.** Drift shows up as fallback usage rather than as
silent breakage, and every degradation is recorded per step and per run. A
recorded artifact's `required_strategies` state what a surface must support, so
a capability that fell back to a web-only `dom_hint` is visibly not portable.
What is *not* built: automatic re-recording on drift, and a cross-run flakiness
score. Both are noted below.

# Heterogeneity & multi-tenant

**The seam.** It is `Surface` (`src/cua/surface/base.py`). Above it, everything
speaks `UiNode` and `Observation`; below it, a surface implements perception and
five physical actions. Nothing above `surface/` imports Playwright. The recorded
flow is therefore not coupled to an automation library — it is coupled to the
idea of *a screen with named controls on it*.

**Legacy web** is not an extension — it is the implemented target. The mock
console is a frameset with nested iframes, table-based layout, `<font>` tags,
`<td onclick=...>` navigation, no test ids and no `<label for>`. The perception
layer handles it by treating a clickable cell as a button and by reading the
cell to the left as a field's label.

**Desktop.** `src/cua/surface/desktop.py` is interface-complete and deliberately
unimplemented, with a per-strategy mapping: `role_name` → `AXRole`+`AXTitle` /
UIA `ControlType`+`Name`; `label_proximity` → `AXTitleUIElement` / `LabeledBy`;
`table_cell` → `AXTable`/`AXRow` / UIA `Grid`. Two web-only strategies have no
analogue, which is exactly why `Surface.capabilities()` exists: a surface
declares what it can resolve, the resolver skips the rest and records that it
skipped them. Handoff keeps its shape — "the same live session" becomes the same
OS window on the same host, with automation ceasing to synthesize input while
the operator drives it.

**Cross-tenant reuse.** Artifacts are bound to a tenant *by reference, not by
value*: `TenantBinding{tenant_id, variant}` and an entry point of
`{{ config.base_url }}/…` rather than a baked host. Recorded URLs are
canonicalized — `/members/100244` becomes `/members/\d+$` — so a checkpoint is
about the *shape* of where you are, not one record. Running the same artifact
against another institution's instance of the same vendor product is a config
change today.

What would make that safe at hundreds of tenants, and is designed but not built:
resolve an artifact as `base capability + variant overlay`, where the overlay
patches only what differs — usually a step's `TargetPlan` or an outcome marker's
wording, both already isolated as small declarative objects. A tenant runs the
base artifact until something degrades; the degradation signal (fallback used,
or a specific step failing on a specific `variant`) is the trigger to record an
overlay rather than a whole capability. Version drift is detectable the same
way, with `AppProfile.version_observed` giving the grouping key: if a step
starts failing across every tenant on vendor version 7.3, that is a vendor
upgrade, not a tenant quirk, and it wants one re-recording promoted to all of
them.

**Deliberately not built:** a tenant registry, overlay resolution, credential
brokering per institution, or any scheduling across instances. The abstractions
do not prevent them; building them would have been infrastructure for
theoretical scale, which the assignment explicitly does not reward.

# Escalation & handoff

**Detecting "stuck".** Four triggers, all real: the policy gate returns
`requires_human` (a human-only control such as `Supervisor Override`, or an
action over the risk ceiling); a business outcome declares
`disposition: escalate_to_human` (an entitlement block is a legitimate outcome a
person can clear); the model calls `request_human_help`; or the discovery loop's
repeat guard sees the screen unchanged across several actions.

**The intervention request** (`handoff/intervention.py`) carries the capability
and version, the goal, the step id/index/intent, why automation stopped, the
observed URL and title, an excerpt of what was on screen, a screenshot, a DOM
snapshot, suggested manual steps, and what will happen on resume. It is written
to a shared inbox and to the run's own evidence directory.

**Same live session.** The `BrowserContext` is never torn down. `SessionControl`
holds `owner ∈ {automation, human}` and a state machine
(`running → paused_pending_human → human_control → resuming → running`).
`assert_can_act()` is called before every single action, so "automation stopped
acting while the human was in control" is enforced by the control object rather
than by discipline — the test for it asserts the `ControlViolation`.

The operator opens a small console, clicks **Take control**, and acts *in the
browser window automation is driving*, then clicks **Release & resume**. The
console is a control plane only: it shows the request context and a live view,
and it never touches the browser (Playwright's sync API is single-threaded, so
the HTTP handlers enqueue commands and the run thread does all browser work).
Serializing cookies so a human can open their own browser would lose in-page
state and is the anti-pattern the requirement names.

**Resume.** Control returns, `resumed()` fires, and the engine **re-verifies the
step's checkpoint before repeating any work** — the human may have already
produced the state the step was reaching for, and blindly re-running it would
hunt for a control that is no longer on screen. Rewinds and escalations are both
budgeted, so a condition a human cannot clear becomes a clear failure rather
than a loop.

**Recording what the human did.** Capture-phase listeners are installed in every
reachable document and re-installed after each navigation, capturing clicks,
field changes and form submits with the control's accessible name; frame-URL
diffing on the Python side catches navigations the in-page listeners cannot
report because their document was replaced mid-event. Password fields are never
captured, at the source. Everything is redacted before it is written and lands
in `ReplayResult.human_actions` and the run log.

The listeners go in the moment the session *pauses*, not when the operator
presses *Take control*. That ordering was originally the other way round and it
lost work: a person who sees the parked browser and fixes the problem before
touching the console had their actions recorded nowhere, while the run still
reported success. Silent loss of evidence is a worse failure than a loud one, so
actions taken before the formal takeover are now recorded and flagged with
`before_takeover`, and releasing without having taken control still resumes the
run rather than leaving the operator stuck. The control *state machine* is
unchanged — ownership still transfers explicitly — but the record now reflects
what actually happened to the session rather than what the button sequence
implied.

**What is mocked, precisely.** The operator *interface* is minimal but real, and
the repository contains a run of it performed by an actual person
(`evidence/replay/replay_handoff_human_…04c40d/`, every action
`"simulated": false`). For headless evidence runs and CI there is a
`ScriptedOperator` that drives the identical seam — it takes control, acts on the same live session, is recorded by
the same in-page listeners, and releases control through the same state machine
— and every action it records is tagged `simulated: true`. A real human takeover
runs via `./scripts/handoff_demo.sh` with `--headed --operator console`. What is
*not* built is streaming co-browsing (the console polls a screenshot), operator
identity and authorization, and routing to a queue of operators.

# Safety

**Allowlist.** `config/allowlist.yaml`, profile-based and configurable, not
hardcoded. A URL must satisfy both origin and path, denied patterns beat allowed
ones, and action types are enumerated. It is checked before every navigation
*and* after every action, because a click can navigate somewhere a navigation
never would. The mock app's fault-injection endpoint is explicitly denied: it is
operator tooling, and the agent must not be able to reach it.

**Action-risk model.** Three levels — `read_only`, `reversible_write`,
`irreversible_write` — and risk is computed **twice**. The artifact declares a
level per step, visible to a reviewer; the gate re-derives it at execution from
the live control's label. The stricter wins. This matters: without it, the
artifact becomes a trust boundary, and a hand-edited artifact could declare a
"Transfer Funds" click as `read_only` and walk past the ceiling. The control's
own label re-escalates it.

**Policy.** Irreversible actions are **blocked**, not confirmed. In a back
office the cost of an unintended irreversible action is unbounded and often
unrecoverable; the cost of a paused run is an operator's minute. Confirmation
prompts are also the weakest control available — they get answered reflexively.
Blocking is not failing: a handoff channel exists, so entitlement-elevating
controls are marked human-only and route to a person, which is the correct
behaviour regardless. One gate object serves discovery and replay; there is no
code path to the surface that bypasses it.

**Secrets.** Secret inputs must come from the environment — the schema refuses a
`secret` that does not. Artifacts store `{{ inputs.operator_passcode }}` and
validation rejects literals matching sensitive patterns or credential-shaped
words. The discovery loop refuses a `type_text` whose literal matches a
registered secret and tells the model to bind the input instead.

**PII and sensitive data.** Redaction is on every persistence path, by
construction: `RunLogger` and `EvidenceWriter` scrub before writing, and there is
no unredacted write. Two mechanisms, because patterns alone are not enough:
registered values (the actual passcode, the actual member name) are replaced
exactly, and patterns catch SSNs, Luhn-valid card numbers, emails, phones, long
account numbers and API-key shapes that were never declared. Outputs marked
`pii` are returned to the caller — that is the point of the capability — but
redacted everywhere they are stored; the end-to-end test asserts the member's
name does not appear in the written `result.json`. Screenshots cannot be
regex-scrubbed, so secret-typed fields are masked with an opaque overlay
*before* the capture is taken.

**The limit of redaction during discovery.** Worth being exact about, because it
is the one place the guarantee is weaker. Discovery is where sensitivity is
*learned*: the agent classifies `member_name` as `pii` partway through the run,
and from that moment the value is registered and masked in everything written
afterwards. Anything captured *before* the declaration — the screenshots, the
observation text the model was shown, the accessibility snapshot — still
contains it. You cannot redact a field you have not yet classified, and a
screenshot shows it regardless. This is why discovery runs against synthetic
fixture data, and why in production a recording run belongs in a sandbox with
seeded records rather than against live member data. Replay has no such
limitation: the sensitivity is already declared in the artifact before the run
starts.

**Limitations, stated plainly.** Risk classification from control labels is a
heuristic: it will over-trigger on a button innocently labelled "Post Comment"
and could under-trigger on one labelled ambiguously. The asymmetry is
intentional — over-triggering routes to a human, under-triggering moves money —
but it is still a heuristic, and in production it would be backed by a per-app
reviewed action registry. Redaction patterns are Western-formatted and will miss
other identifier shapes. Screenshot masking only covers fields the *surface*
knows are secret; sensitive data rendered as ordinary page text appears in
evidence images. The allowlist constrains the agent, not the application: a
permitted route that itself redirects somewhere harmful is caught only by the
landing check. And the artifact checksum detects tampering, it does not prevent
it — there is no signing key.

# Cuts

Deliberately not built, with the reasoning and what comes next.

**Desktop surface implementation.** Cut because it would have been the single
largest piece of work in the project and would have demonstrated nothing the web
surface does not already demonstrate about the *design*. What exists instead is
`surface/desktop.py`: interface-complete, with a per-strategy AX/UIA mapping and
an honest `capabilities()` that refuses markup-based strategies. Next: implement
`observe()` against macOS `AXUIElement` for one real application; nothing above
`surface/` should need to change, and if it does, the abstraction was wrong.

**Multi-tenant infrastructure.** No tenant registry, no overlay resolution, no
per-tenant credential brokering. Cut because the assignment explicitly says not
to build infrastructure for theoretical scale. The hooks are in place —
`TenantBinding`, config-referenced base URLs, canonicalized URLs, the
degradation signal that tells you *when* a tenant needs an override. Next:
`base + variant overlay` resolution in `ArtifactStore`, keyed on
`(tenant_id, variant, app version)`.

**Streaming co-browsing.** The console polls a screenshot rather than streaming
frames, and there is no operator authentication or routing. Cut by the
assignment's own scoping. The control-transfer model underneath is real and
would not change; only the transport would.

**Assisted LLM fallback on replay failure (stretch D).** Tempting and
deliberately skipped. A bounded one-step model recovery is a real feature, but it
weakens the property this project is actually arguing for — that production
replay has no model in the decision loop — and `replay/` importing a model
client would make that guarantee a policy rather than a fact. Next, if built:
a separate `replay/assisted.py` entered only after a hard failure, policy-checked
per action, capped at one step, recorded as evidence, and never enabled for
unattended runs.

**Automatic re-recording on drift, and multi-run stability scoring (stretch F).**
The signals exist — `strategy_degraded` per step, ambiguity flags, per-run
outcomes — but nothing aggregates them across runs. Next: a `cua stability`
command that replays an artifact N times and reports a flakiness score, feeding
the same approval gate that `draft → approved` already uses.

**Code generation from artifacts (stretch B).** Skipped as the least valuable of
the options for this evaluation — it demonstrates templating, not judgement.

**Smaller things.** No retry/backoff around the model API beyond three attempts
and a model fallback chain. No structured concurrency for parallel replays. The
`viewport_ratio` strategy is recorded but is a genuine last resort and its use
should be treated as a signal to re-record. Discovery is the only phase a model
touches; there is no mechanism to *update* an artifact from a new discovery run
short of recording a new version.
