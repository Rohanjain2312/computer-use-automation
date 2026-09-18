# Decisions

The assignment leaves the important choices open. This file records the ones
that were genuinely load-bearing: the alternatives considered, why the chosen
option won, and what it costs. Trivial choices are not listed.

---

## D1 — Perception mechanism: what does the agent actually see?

**Decision.** A *synthesized accessibility tree*: a script walks every reachable
document (including framesets and nested iframes), computes a role from tag
*and behaviour*, derives an accessible name the way a human resolves one
(including "the table cell to the left", which is how legacy forms are actually
labelled), and reports absolute viewport coordinates plus table context. The
model receives this tree **and** a screenshot each turn.

**Alternatives.**

| Option | Why not |
|---|---|
| CSS/XPath selectors over the DOM | Fails the assignment's central constraint. The target class of applications has no test ids, non-semantic markup, and generated class names. It is also the one mechanism with no desktop analogue. |
| Playwright's built-in `accessibility.snapshot()` | No bounding boxes, no table relationships, no frame traversal, and deprecated. It could not address "the Current Balance cell of the Regular Savings row" at all. |
| Pure screenshot + vision coordinates | Most general, and genuinely tempting. Rejected for *replay*: a recorded flow whose only locator is "the model looked at a picture" cannot be replayed deterministically. Vision is kept as an input to discovery, not as the recorded locator. |
| OS-level automation (AX/UIA) for the web target | Right long-term shape, wrong cost today: driving a browser through the OS accessibility layer adds a large amount of platform code for no additional evidence that the design works. |

**Why this one.** It is the only option that is simultaneously (a) independent of
markup quality, (b) recordable as a stable locator, and (c) directly portable —
every field of `UiNode` has an `AXUIElement`/UI Automation equivalent, which is
what makes `surface/desktop.py` a mapping exercise rather than a redesign.

**Cost.** The perception script is real code with real edge cases (it is the
largest single piece of logic in the project), and it only reaches same-origin
frames. A cross-origin iframe would need a per-frame execution context.

---

## D2 — How actions are performed: coordinates, not element handles

**Decision.** Resolve a control to a point and click that point;
type into whatever then has focus.

**Alternative.** Keep a handle to the resolved element and call
`element.click()`. Simpler, more reliable in a browser.

**Why coordinates.** Two reasons that outweigh the reliability gain. First, it
is the action model every other surface has — a desktop surface cannot call
`element.click()`. Second, it keeps the system honest: if the click lands
somewhere the screenshot does not show, that is a perception bug, and hiding it
behind a DOM handle would mean the evidence and the behaviour could silently
diverge.

**Cost.** Elements must be scrolled into view first, and an element covered by an
overlay receives the overlay's click. Both are handled; neither is free.

**Deliberate exceptions.** `read_node` and `set_combobox_value` address the
element directly, because reading a value and setting a combobox are primitives
that every accessibility API exposes (`AXValue`, UIA `ValuePattern`) and
because driving a native select by keyboard is genuinely unreliable.

---

## D3 — Who writes the locators: the model or the engine?

**Decision.** The engine. After the model acts on a control, the engine reads
the control the *surface actually perceived* and enumerates every strategy that
would find it, scoring each by how many other controls on the same screen it
also matches. The model supplies intent and business meaning; it never supplies
a selector.

**Alternatives.**

- *Model writes the locators.* Rejected: it makes the artifact only as
  reproducible as the model's phrasing on that particular day, and a vague
  "the search button" cannot be scored for ambiguity.
- *Model proposes, engine validates.* Considered seriously. Rejected as strictly
  worse than the chosen option: the engine has to enumerate candidates anyway in
  order to validate, so letting the model propose adds a failure mode for no
  gain.

**Why this one.** The same trajectory always synthesizes the same artifact. It
also produces something the model could not: a *confidence* per strategy,
derived from measured uniqueness on the live screen, which is what drives the
fallback ordering at replay time.

**Consequence worth naming.** Locators for *values* are generated differently
from locators for *controls*. A button labelled "Search" is legitimately found
by its text; a cell showing `$4,812.37` must not be, because that is the datum
being read — keying on it would record one member's balance as the way to find
every member's balance, and would bake account data into the artifact. See
`build_target_plan(..., purpose="value")`.

---

## D4 — Where the boundary between model and determinism sits

**Decision.** The model decides *what to do* and *what things mean*. The engine
decides *how to find things*, *how long to wait*, and *what proves it worked*.
Concretely, the model's per-action `expect` is only turned into a checkpoint if
that text genuinely appeared in the post-action observation; otherwise the
engine derives one from what actually changed.

**Alternative.** Let the model author checkpoints freely. Rejected: an
unverified assertion is worse than no assertion, because it fails later, on a
different screen, for a reason nobody can reconstruct.

**Why this one.** It gets the model's judgement (it knows "Account Detail" is
the meaningful confirmation and "Servicing v7.2.1" is not) without letting it
assert something the screen never showed.

---

## D5 — Grounding the error taxonomy: a probe phase

**Decision.** Discovery runs in two phases on the same live session. The `goal`
phase accomplishes the task. The `probe` phase deliberately triggers the failure
modes — searches a nonexistent member, opens a restricted one — reads the
application's exact wording, and registers it via `declare_outcome` /
`declare_recovery` / `declare_failure`. Markers that were never actually
observed during the run are **dropped at synthesis**, with a note.

**Alternatives.**

- *Ask the model to predict the error wording from memory.* Rejected: the
  detectors match text literally, so a plausible-but-wrong guess produces an
  artifact that silently misclassifies a business outcome as a crash. That is
  the single worst failure this system could have.
- *Hand-author the error rules.* Rejected: it would make the most interesting
  part of the artifact the part the model did not produce.
- *Learn them from production failures over time.* Correct at scale, but it
  means the first N runs each fail once to learn something. Noted as future work.

**Why this one.** It costs one extra phase of the same run and it is what makes
`member_not_found` a *grounded* business outcome rather than a guess.

---

## D6 — Recovery semantics: what does "recovered" mean?

**Decision.** Recovery rules are triggered by *page state*, not step index, and
are evaluated before every step and on every wait poll. Each rule declares what
happens after it fires: retry the step, continue, or **resume from an earlier
step**.

**Why the third option exists.** An expired session invalidates everything the
flow established. Retrying the step that happened to notice is wrong — the
right answer is to restart from the entry point and sign on again. Without
`resume_from_step`, session-timeout handling degrades into either an infinite
retry loop or a hard failure. (It did exactly that, in testing, before the
option existed.)

**Cost.** Rewinds need a budget and a suppression rule so a rule cannot
re-trigger on the same stale screen it just rewound from. Both are implemented
and capped.

---

## D7 — Risk classification is computed twice

**Decision.** The artifact declares a risk level per step, set at discovery time
and visible to a reviewer. At execution, the policy gate *re-derives* risk from
the live control's label. The stricter of the two wins.

**Alternative.** Trust the artifact's declared risk. Simpler, and it is the
obvious design.

**Why not.** It makes the artifact a trust boundary. An artifact that is
hand-edited, or synthesized from a compromised run, could declare a
"Transfer Funds" click as `read_only` and walk straight past the ceiling.
Re-deriving from the live label means the control itself re-escalates.

**Cost.** Risk classification depends on a configurable marker list, which is a
heuristic. It will over-trigger on a button innocently labelled "Post Comment".
Over-triggering routes to a human; under-triggering moves money. The asymmetry
decides the design.

---

## D8 — Irreversible actions are blocked, not confirmed

**Decision.** `irreversible_policy: block`. Actions that would change financial
state are refused outright by default. Entitlement-elevating controls
(`Supervisor Override`) are marked human-only and route to the handoff channel
rather than being blocked.

**Alternatives.** Prompt for confirmation; flag for post-hoc review.

**Why blocking.** In a back office, the cost of an unintended irreversible
action is unbounded and often unrecoverable, while the cost of a paused run is
one operator's minute. Confirmation prompts are also the weakest control
available — they are answered reflexively. Crucially, blocking here does not mean
failing: a human-in-the-loop channel already exists, so "blocked" degrades to
"ask a person", which is the correct behaviour anyway.

---

## D9 — Same-session handoff: headed browser plus a control-plane console

**Decision.** The live `BrowserContext` is never torn down. `SessionControl`
owns which party may act. When automation escalates it stops issuing actions and
publishes an intervention request; the operator takes control through a small
console, acts **in the browser window automation is driving**, and releases.
In-page capture-phase listeners record what they did.

**Alternatives.**

- *Serialize cookies and let the human open their own browser.* This is the
  anti-pattern the assignment explicitly names. It also loses in-page state
  (scroll position, an open dialog, a half-filled form) that cookies do not
  carry.
- *Full VNC/CDP co-browsing in the console.* Out of scope by the assignment's own
  wording, and the engineering would dwarf everything else here.

**Why this one.** It is genuinely the same session — same context, same page,
same cookies, same JavaScript state — while the console stays small enough to be
honest about what it is: a control plane, not a co-browsing product.

**Cost.** A real human takeover requires `--headed` on the same machine. For
headless evidence and CI there is a `ScriptedOperator` that drives the identical
seam; everything it records is tagged `simulated: true` so evidence never
overstates what happened.

---

## D10 — Storage: JSON files in the repository

**Decision.** `artifacts/<capability_id>/v<version>.json`, plus a generated
`index.json`.

**Alternative.** SQLite or a document store.

**Why files.** The artifact's whole purpose is to be *reviewable*. A JSON file
diffs in a pull request, is signed by a checksum, needs no service to read, and
makes "who changed this capability and why" a question git already answers. A
database would buy indexing that nothing at this scale needs, and would hide the
artifact behind a query.

**When this stops being right.** Hundreds of tenants × ~20 apps is tens of
thousands of artifacts; at that point the store needs real indexing and the
per-tenant override resolution described in REPORT.md § Heterogeneity. The
`ArtifactStore` interface is narrow enough (`save` / `get` / `list_all`) that
swapping the backing is contained.

---

## D11 — Architecture: one process, synchronous

**Decision.** A single process, a synchronous loop, a CLI. No queue, no workers,
no service boundaries.

**Why.** A run owns a live browser session and a live human-handoff channel;
both are inherently stateful and session-affine. Introducing a queue would mean
either pinning work to the process holding the session — a queue that cannot
distribute, which is no queue at all — or serializing session state, which is
exactly the handoff anti-pattern D9 rejects. The assignment is also explicit
that infrastructure breadth is not rewarded.

**What production would add, and where.** A run coordinator that owns a pool of
browser sessions, with the CLI's `_replay` becoming an RPC entry point; the
operator console becoming a shared web app reading the same intervention inbox
that `handoff/intervention.py` already writes to. Neither changes the engine.

---

## D12 — Checkpoint failure re-runs the condition scan before failing

**Decision.** When a step's checkpoint does not hold, the engine re-runs the
business-outcome / recovery / failure scan *before* treating the step as failed.
And because a scan can itself change the page, the scan reports whether it did,
and the engine re-observes before deciding anything.

**Why.** The commonest reason a checkpoint fails is not that the automation
broke — it is that the application answered something legitimate instead. The
step "search for the member" expects an account summary; what it gets is "No
member matching that number was found." Failing there would report a crash for
the single most ordinary result in the flow, which is the mistake the assignment
glossary names explicitly.

**How it was found.** Both halves came from real bugs during development, not
from foresight. The first ordering — scan, then act on the stale observation —
made a run that a recovery rule had just fixed fail anyway, because the engine
was still looking at the pre-recovery screen. The second — treating a failed
checkpoint as terminal without re-scanning — reported `member_not_found` runs as
hard failures. The end-to-end tests now pin both.

**Cost.** One extra observation on the failure path, and a scan that must be
idempotent enough to run twice. Both are cheap; the alternative is a result
contract that lies.

---

## D13 — The human-action recorder installs when the session pauses, not at takeover

**Decision.** Capture-phase listeners go into every reachable document the moment
a run pauses for a human, not when the operator presses *Take control*. Actions
taken before the formal takeover are recorded and flagged `before_takeover`, and
*Release & resume* works even if control was never formally taken.

**Why.** The obvious design — install on takeover — loses work silently. A
person who walks up to a parked browser, sees the problem, and fixes it before
touching the console has done real work on the live session and had it recorded
nowhere, while the run still reports success. Silent loss of evidence is worse
than a loud failure: the run log would claim automation reached a state that a
human actually produced.

**What did not change.** The control *state machine*. Ownership still transfers
explicitly, `assert_can_act()` still refuses automation while a human holds the
session, and the `ControlViolation` test still passes. Only the record now
reflects what happened to the session rather than what the button sequence
implied.

**Cost.** Actions can be attributed to the human that were arguably side effects
of automation's own earlier input — a `change` event firing on blur, for
instance. Over-attribution to the person who was present is the safer direction.

---

## D14 — The limit of redaction during discovery, stated exactly

**Decision.** Discovery runs against synthetic fixture data, and the weaker
redaction guarantee during discovery is documented rather than papered over.

**Why it is weaker.** Discovery is where sensitivity is *learned*. The agent
classifies `member_name` as `pii` partway through a run; from that moment the
value is registered and masked in everything written afterwards. Anything
captured *before* that declaration — screenshots, the observation text the model
was shown, the accessibility snapshot — still contains it. You cannot redact a
field you have not yet classified, and a screenshot shows it regardless of any
pattern.

**Consequence.** A recording run belongs in a sandbox with seeded records, not
against live member data, and this repository's discovery runs use fixtures for
exactly that reason.

**Replay is stronger, but not unconditional.** Sensitivity is declared in the
artifact before the run starts, so every persistence path is covered from step 0
— for the values the artifact *declares*. Redaction works by replacing
registered values, and a person's name matches no pattern, so it is scrubbed
only because something typed it `pii`. This is not hypothetical: the two
capabilities here differ on exactly that point.
`member_savings_balance_lookup` declares `member_name` as a `pii` output, so it
is registered and absent from every file its runs write.
`member_stop_payment_request` declares the `requested_by` input as `pii` (also
scrubbed) but never declares the member name its request form displays — so that
name survives in that capability's DOM and accessibility snapshots and in its
intervention excerpts.

**What follows from it.** A capability should declare the sensitive fields its
screens *show*, not only the ones it returns to the caller. The rule is
recording-time discipline rather than something the engine can infer, because
the engine cannot know that a string in a table cell is a person. What the
engine *can* do is enforce the declared case, and
`tests/test_replay_e2e.py` now asserts a declared `pii` value appears in no file
a run writes — the earlier version of that test only checked `result.json`,
which would have passed while the snapshot beside it named the member.

**What would close it.** Classifying fields from the app profile before the run
rather than during it — a per-app field registry, which is the same artifact the
risk model wants in D7. Until that exists, the honest position is that discovery
is a sandbox activity.

---

## D15 — A step the gate refuses to automation is still recorded

**Decision.** When the policy gate returns `requires_human` during discovery and
an operator performs the action on the live session, the action is recorded as a
step, tagged `needs_human`, and the tag becomes
`safety.steps_requiring_approval` in the artifact.

**Alternatives.**

| Option | Why not |
|---|---|
| Drop the escalated action from the recording (the original behaviour) | The capability silently loses its own committing step. Replay would run every harmless step and then stop short, and the artifact would not show a reviewer that the flow needs a person at all. |
| Let discovery perform the risky action itself and mark it afterwards | Discovery would be acting outside the allowlist, which is the one property the gate exists to make structural. |
| Have the task YAML declare which step needs approval | Moves a safety fact from the policy into the recording request, which is the wrong place: the same flow recorded under a stricter profile should need approval without anyone remembering to say so. |

**Why this one.** The action genuinely happened on the flow's critical path, so
the recording should contain it; and the reason it needed a person is a property
of the profile, which the gate already knows at the moment it refuses. Deriving
the tag from the refusal means the artifact's approval list and the runtime
behaviour cannot disagree.

**What it buys, concretely.** `member_stop_payment_request` carries
`steps_requiring_approval: ["s11_submit_stop_payment_request"]`. A reviewer sees
before running it that the capability cannot commit unattended; an unattended
`cua invoke` fails cleanly instead of committing; and `cua replay --operator`
routes to the same handoff machinery the entitlement block already used, rather
than a second approval path.

**Cost.** The recorded step's evidence is captured after the operator acted, so
its "before" observation is the paused screen rather than the screen the
automation itself last produced. For a step automation is never going to perform
unattended, that is the more useful record anyway.
