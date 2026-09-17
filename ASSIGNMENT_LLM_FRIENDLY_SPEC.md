# Take-Home Project: Computer-Use Automation System
## LLM / AI Coding-Agent Friendly Specification

**Source:** `Assignment A — Computer-Use Automation System.pdf`  
**Purpose of this file:** Preserve the assignment requirements and intent while expressing them in a form optimized for Claude Code, Codex, Cursor, Copilot, or another coding agent.

> **Important:** The sections marked **[SOURCE REQUIREMENT]** are a faithful restructuring of the assignment. The sections marked **[AGENT GUIDANCE]** are additional instructions derived from coding-agent best practices. Agent guidance must not override or weaken any source requirement.

---

# 0. Agent Mission

You are implementing a take-home project for the interface.ai engineering team.

Your objective is to produce a **small but real end-to-end computer-use automation system** that demonstrates:

1. Natural-language goal → live UI interaction by an LLM.
2. Successful discovery run → structured reusable capability artifact.
3. Saved artifact + typed inputs → deterministic replay without an LLM deciding each step.
4. Runtime error/business-outcome handling.
5. Human escalation and handoff on the **same live session**.
6. Safety/allowlist enforcement and sensitive-data protection.
7. Evidence proving the discovery and replay actually happened.
8. A design that can credibly extend to heterogeneous legacy surfaces and many tenants.

The project is intentionally open-ended. Make explicit technical decisions where the assignment leaves choices open, and document the reasoning and trade-offs.

**Primary evaluation principle:** a complete, coherent vertical slice is more valuable than a broad or polished product.

---

# 1. Non-Negotiable Assignment Context

## 1.1 Product context

The company builds AI agents for banks and credit unions.

This project is the **backend integration layer** that gives those agents the ability to operate back-office applications.

The system is intended for legacy applications where an API does not exist.

### API rule

If the target application exposes an API, API integration is the preferred approach, but **API integration is out of scope for this project**.

The problem being solved is UI-only access to applications such as:

- core banking screens
- servicing tools
- administrative consoles

The computer-use system uses an LLM to determine how to accomplish a task the first time. After successful discovery, it converts what was learned into a deterministic, replayable automation.

Each recorded automation becomes a:

- reusable capability
- reviewable artifact
- parameterized capability
- capability that an AI agent can invoke
- capability that does not need an LLM in the decision loop during normal production replay

### Core mental model

**The model discovers.**  
**The artifact becomes a reusable capability.**  
**Deterministic replay is how the AI agent invokes it in production.**

---

# 2. Real Environment the Design Must Account For

You only need to implement against one concrete surface, but the design must account for the following real-world conditions.

## 2.1 Stable enterprise UIs + runtime failures

Enterprise business applications tend to be relatively stable and change slowly.

That makes record-once/replay-many viable.

The main replay challenge is **not constant UI drift**. It is legitimate runtime exceptions such as:

- validation errors
- "record not found"
- permission denials
- unexpected confirmation dialogs
- session expiration/timeouts
- transient slowness
- application errors

A replay implementation that only handles the happy path is insufficient.

## 2.2 Heterogeneous application surfaces

A target application may be:

- modern web
- legacy web
- server-rendered web
- frameset-based
- deeply nested tables
- non-semantic markup
- missing test IDs
- native desktop software

Do not assume:

- a clean DOM
- stable CSS selectors
- test IDs
- an API

The design should work from the perspective of what a human operator can observe and do.

Possible computer-use mechanisms include:

- DOM-level automation
- accessibility tree
- screenshots + coordinates
- OS-level automation
- other appropriate computer-use mechanisms

## 2.3 Multi-tenant scale

The real environment contains:

- hundreds of tenants/institutions
- approximately 20 applications per tenant
- thousands of application instances overall

Many tenants use the same underlying vendor product but with differences in:

- configuration
- branding
- version

A capability recorded for one tenant should ideally:

- generalize to other tenants, OR
- degrade gracefully, OR
- support safe specialization/overrides

It should not require rebuilding every capability from scratch.

---

# 3. What You Must Build

## 3.1 End-to-end behavior

The system must support this overall flow:

```text
Natural-language goal
        ↓
Target application / entry point
        ↓
LLM-driven computer-use discovery
        ↓
Live UI interaction
        ↓
Goal completed
        ↓
Structured capability artifact saved
        ↓
Deterministic replay using artifact + typed inputs
        ↓
Structured outputs / business outcome / failure
        ↓
Evidence
```

Additionally:

```text
Agent gets stuck / unsafe condition
        ↓
Human intervention request
        ↓
Human takes control of SAME live session
        ↓
Human performs required action
        ↓
Automation regains control
        ↓
Run resumes or completes
        ↓
Human actions are recorded
```

---

# 4. Core Requirements — MUST HAVE

These are the requirements against which the implementation will be evaluated.

## 4.1 Goal-driven agent loop

### Required

Accept:

- a natural-language goal
- a target application / URL / entry point

Run an LLM-driven loop:

```text
OBSERVE → DECIDE → ACT → OBSERVE → ...
```

Continue until:

- the goal is achieved, OR
- a stopping condition is reached

Required stopping conditions include appropriate limits such as:

- maximum steps
- timeout
- dead-end / inability to proceed

The agent must **actually interact with a real UI**.

The implementation may use:

- DOM automation
- accessibility tree
- screenshot + coordinates
- OS-level automation
- another suitable computer-use mechanism

### Important design constraint

Bias toward a mechanism that can still work when the target has **no clean DOM**, because that is representative of the real environment.

---

## 4.2 Structured reusable artifact

After a successful discovery run, produce a **typed, serializable, versioned, reviewable artifact**.

The artifact is not simply a transcript of the LLM conversation.

It must be a reusable capability contract that is decoupled from the raw model transcript.

At minimum, it must contain:

### A. Ordered actions

The sequence of steps/actions needed to perform the flow.

### B. Target identification

For every target element/control:

- how the element/control is identified
- the targeting strategy
- reasoning about robustness

### C. Typed inputs

Define the parameters supplied by the calling agent for each invocation.

Example:

```text
member_id: string
```

### D. Typed outputs

Define:

- data extracted
- output names
- output types
- output shape

Example conceptually:

```text
current_savings_balance: decimal
```

### E. Checkpoint / success condition

Define a condition proving the expected state was actually reached.

The artifact must be understandable by:

- a human reviewer
- a calling AI agent

The schema is a **major evaluation focus**. Design it deliberately.

---

# 5. Deterministic Replay — Production Path

Given:

```text
saved artifact + typed input parameters
```

the system must replay the flow **without invoking the LLM for decisions**.

This is the path an AI agent would use in production.

## Replay requirements

Replay must:

1. Execute the saved flow.
2. Use stable element/control targeting.
3. Verify checkpoints/success conditions.
4. Return declared outputs.
5. Detect runtime errors and exceptional states.
6. Respond deliberately rather than blindly continuing.
7. Return a clear structured result.

## 5.1 Runtime conditions to handle

At minimum, design for:

- validation error
- "record not found"
- permission denial
- unexpected dialog
- session timeout
- slow load
- failed load
- application error

The assignment explicitly distinguishes three categories.

### Category A — Expected business outcome

A legitimate result that the caller needs to know about.

Example:

```text
member_not_found
```

This is **not a system crash**.

### Category B — Recoverable condition

A condition the replay engine can safely recover from.

Examples:

- dismiss a known interstitial
- wait for a transient load
- retry a transient operation

### Category C — Hard failure

A condition requiring the run to stop and surface a clear, debuggable error.

## 5.2 Replay result contract

Return a structured result that clearly identifies one of:

### Success

Includes outputs.

### Known business outcome

Example:

```text
status = "business_outcome"
outcome = "member_not_found"
```

### Failure

Must contain enough information to debug:

- failed step
- expected state/action
- observed state/result
- useful error details

---

# 6. Safety and Policy Guardrails — MUST HAVE

The system operates in a financial-data context.

## 6.1 Explicit allowlist

Implement an explicit, configurable allowlist.

It should constrain things such as:

- permitted domains
- permitted routes
- permitted action types

The agent must not act outside the allowlist.

## 6.2 Risk classification

Distinguish between:

### Safe / reversible actions

Examples may include:

- navigation
- reading data
- other low-risk operations

### Risky / irreversible actions

Examples may include actions that change financial/account state.

The assignment allows you to choose the treatment, but risky actions must be handled conservatively.

Possible policies:

- block
- require confirmation
- flag for human review

Choose a policy and justify it.

## 6.3 Sensitive data

Never persist:

- secrets
- credentials
- tokens
- raw sensitive data
- full PII

inside artifacts or logs.

Redact sensitive information appropriately.

---

# 7. Evidence and Observability — MUST HAVE

Produce enough evidence to understand and debug a run.

## Required evidence

At minimum:

### Structured run log

Record:

- what the agent did
- why it did it

### Rich failure signal

On failure, capture at least one richer signal such as:

- screenshot
- DOM snapshot
- trace
- another useful diagnostic artifact

The specific mechanism is your choice.

---

# 8. Human-in-the-Loop Escalation — MUST HAVE

The system must support human intervention when automation cannot safely finish.

Trigger examples:

- discovery gets stuck
- replay encounters an unrecoverable condition
- risky/irreversible action requires a person

## 8.1 Detect and route

Detect a stuck/blocked state and create an intervention request.

The request must carry enough context for the human to act.

Include:

- capability / goal
- current step
- current state or screenshot
- reason the automation stopped

## 8.2 Human takes over the SAME live session

This is critical.

The human must operate the **same live session** used by automation.

Do NOT implement handoff as:

```text
automation session dies
→ human opens a new session
```

Instead:

```text
automation pauses
→ same live session becomes human-controlled
→ human performs manual steps
→ control returns to automation
→ run resumes/completes
```

Preserve:

- session context
- evidence
- control state

Record:

- what the human did

## 8.3 Control-transfer seam

The architecture must explicitly model:

- automation can pause
- automation can cede control
- human can take control
- human can release control
- automation can resume
- system knows who is currently in control

## 8.4 Operator console scope

A complete real-time co-browsing console is **out of scope**.

A minimal but real handoff is sufficient:

1. pause automation
2. expose the live session for manual control
3. provide a minimal/mock operator surface if necessary
4. signal resume
5. capture human actions
6. document the remaining production design

The handoff mechanism and control-transfer model must be real and well-reasoned even if the UI is mocked.

---

# 9. Heterogeneity and Multi-Tenant Design

This is primarily a **design requirement**, not an implementation requirement.

You implement against one concrete surface.

Your write-up must explain how the design extends to the real environment.

## 9.1 Surface abstraction

Explain how the artifact schema and replay engine could extend from your chosen surface to:

- legacy web
- native desktop

Explain the seam between:

```text
surface perception/action mechanism
        ↕
recorded capability / artifact
```

The recorded flow should not be tightly coupled to one particular automation library or surface implementation.

## 9.2 Multi-tenant artifact reuse

The real environment has:

- hundreds of institutions
- ~20 apps per institution
- many institutions using the same vendor product

Explain how artifacts could be:

- reused across tenants
- safely specialized
- overridden per tenant/version

Explain how you would detect and manage:

- tenant-specific differences
- application-version drift
- configuration differences

### Explicit scope

Do **not** spend the project implementing:

- full multi-tenant infrastructure
- full desktop support

The assignment only requires that the core abstractions do not prevent these future extensions.

---

# 10. Explicitly Your Design Choices

The assignment deliberately does **not** prescribe the following.

You must choose and defend them.

## You choose

### Language / runtime / frameworks

Any appropriate stack.

### LLM provider / model

Choose:

- provider
- model
- prompting strategy
- agent-loop structure

### Computer-use technology

Possible choices include:

- Playwright
- Puppeteer
- Selenium
- computer-use agent SDK
- screenshot-based control
- accessibility APIs
- OS automation
- another suitable mechanism

### Target application

The assignment does NOT provide a real bank system and says you should not try to obtain one.

Choose a safe proxy target that exercises a meaningful multi-step flow.

Good options:

- public demo/sandbox
- local sample application you build
- local mock application
- intentionally hostile surface

A hostile surface could include:

- iframes
- framesets
- table-based layouts
- no test IDs
- poor/non-semantic markup

A desktop application is also acceptable.

If using a public site:

- respect terms of service
- respect rate limits
- never use real credentials
- never use real PII

### Artifact schema/storage

Choose:

- schema
- serialization
- storage mechanism

### Deterministic replay strategy

Choose and justify:

- locator strategy
- locator fallbacks
- waits
- checkpoints
- error handling
- replay validation

### Architecture

Choose:

- single process vs services
- synchronous vs queued
- persistence approach
- component boundaries

A simple architecture is acceptable if justified.

---

# 11. One Requirement That Cannot Be Stubbed

The **discovery run must be real**.

You must perform at least:

```text
1 genuine LLM-driven run
+
1 live application surface
+
evidence proving the run occurred
```

The evidence must be under:

```text
/evidence/
```

This is the heart of the assignment.

You need model API access.

One successful discovery run is considered reasonable in cost.

## What may be mocked/stubbed

The assignment explicitly permits deliberate stubbing/mocking for areas such as:

- operator console
- desktop surface
- other cleanly separated non-core pieces

If something is mocked:

1. state exactly what was mocked
2. explain why
3. show the production seam/design
4. keep the interface real enough that the design is credible

A clean seam is preferred over getting stuck trying to implement every environment.

---

# 12. Scope and Expected Depth

AI-assisted development is explicitly expected and encouraged.

You may use:

- Cursor
- Claude
- Copilot
- other coding assistants
- libraries
- tools

The assignment assumes modern coding agents make scaffolding relatively fast.

Therefore, do not submit only a partial feature such as:

- just the agent loop
- just artifact generation
- just replay
- just a design document

The expected result is a **complete vertical slice**.

## Required vertical slice

```text
goal
  ↓
real LLM-driven discovery
  ↓
goal completed
  ↓
saved capability artifact
  ↓
deterministic replay
  ↓
typed inputs
  ↓
typed outputs
  ↓
error / business-outcome handling
  ↓
human escalation path
  ↓
same-session human takeover
  ↓
resume
  ↓
evidence for discovery + replay
```

## Where to spend engineering effort

Go deep on:

1. artifact schema
2. deterministic replay
3. runtime error handling
4. safety
5. escalation/handoff

Do not spend disproportionate effort on:

- polished UI
- infrastructure for theoretical scale
- queues/clusters
- full multi-tenancy
- desktop implementation
- unnecessary framework complexity

## Thin-but-real principle

Prefer:

```text
minimal real implementation of EVERY core requirement
```

over:

```text
excellent implementation of only a few requirements
```

You may keep parts:

- minimal
- stubbed
- mocked

if the seam is intentional and documented.

Document:

- what you cut
- why you cut it
- what you would build next

---

# 13. Required Repository Deliverables

The repository must be public.

## 13.1 `/README.md`

Must explain:

### Setup

- how to install/setup
- required configuration
- required API keys
- required services
- how to run
- how to run without live services, where applicable

### Demo path

Give the exact commands needed to:

1. run the agent on a goal
2. generate/save the artifact
3. replay the resulting artifact

A reviewer should be able to follow the README without guessing commands.

---

# 14. Required `/REPORT.md`

Target length:

```text
~1–3 pages
```

Use these **exact seven headings**:

```markdown
# Architecture
# Artifact schema
# Determinism & error handling
# Heterogeneity & multi-tenant
# Escalation & handoff
# Safety
# Cuts
```

## Required content

### 1. Architecture

Explain:

- architecture
- boundaries
- key decisions
- trade-offs

### 2. Artifact schema

Explain:

- schema
- why it is shaped that way
- how it supports replay
- how it supports calling agents

### 3. Determinism & error handling

Explain:

- deterministic replay
- locator strategy
- waits
- checkpoints
- runtime error detection
- runtime error handling
- expected business outcomes
- recoverable conditions
- hard failures
- secondarily, UI drift handling

### 4. Heterogeneity & multi-tenant

Explain:

- extension to legacy web
- extension to desktop
- surface abstraction
- cross-tenant artifact reuse
- tenant/version differences

### 5. Escalation & handoff

Explain:

- how "stuck" is detected
- intervention request
- same-session human takeover
- control transfer
- resume
- human action recording

### 6. Safety

Explain:

- allowlist
- action-risk model
- confirmation/blocking policy
- secret handling
- PII/sensitive-data redaction
- limitations

### 7. Cuts

Explain:

- deliberately omitted functionality
- why it was omitted
- what you would build next

---

# 15. `/evidence/` Requirements

The repository must contain evidence of the end-to-end flow.

Include:

1. saved example artifact
2. discovery-run logs
3. replay-run logs

## Strongly recommended

Include at least one replay that encounters an error/exceptional state, such as:

- bad input
- not-found result
- injected failure
- simulated application failure

Show that the system:

- detects it
- classifies it
- reports it correctly

## Optional

A short screen recording is welcome but not required.

---

# 16. Evaluation Criteria

The assignment says these are weighed roughly in this order.

## 16.1 System design

Look for:

- clear boundaries
- sensible data models
- appropriate simplicity
- good trade-offs
- strong artifact schema
- clear replay contract

## 16.2 Correctness of core loop

The system must:

- actually complete a real goal
- produce an artifact
- replay deterministically
- verify success

## 16.3 Robustness and error handling

Evaluate:

- runtime-error detection
- exceptional-state handling
- distinction between business outcomes and failures
- locator strategy
- wait strategy
- checkpoint strategy

## 16.4 Human-in-the-loop escalation

Must be a real mechanism, not a TODO.

Evaluate:

- stuck detection
- intervention routing
- context supplied to human
- same-session control transfer
- resume behavior

## 16.5 Generalization

Evaluate whether the design credibly handles:

- heterogeneous surfaces
- shared vendor applications
- multiple tenants
- version/configuration differences

without requiring brittle per-tenant rebuilds.

## 16.6 Safety and data handling

Evaluate:

- allowlist
- risky/irreversible action treatment
- sensitive-data redaction

## 16.7 Code quality

Look for:

- readable code
- reasonable typing
- meaningful tests
- easy setup/run experience

## 16.8 Communication

The report should clearly explain:

- reasoning
- trade-offs
- cut lines
- design decisions

---

# 17. What Is NOT Rewarded

Do not optimize for:

- feature breadth
- framework name-dropping
- large distributed infrastructure
- queues merely for appearance
- clusters merely for appearance
- premature multi-tenant plumbing

Designing abstractions that **could** scale is valuable.

Building unnecessary scale infrastructure is not.

The goal is:

> A small, correct, well-argued system.

---

# 18. Optional Stretch Goals

Only implement these after the core is solid.

Pick **at most one or two**.

## A. Agent-facing capability interface

Expose saved artifacts as a catalog of callable capabilities.

For example:

- tool/function-calling interface
- API endpoint

The capability should:

- be discoverable by name
- accept typed arguments
- return typed results

Show one capability being invoked.

## B. Code generation

Generate a runnable:

- test
- automation snippet
- page object
- test file

from an artifact.

## C. Confidence and approval

Add:

```text
draft → approved
```

and optionally:

- replay reliability score
- approval state
- unattended replay gate

## D. Assisted fallback

On replay failure:

- allow a bounded LLM recovery
- only for one step
- policy-check it
- never allow open-ended LLM takeover
- record the recovery as evidence

## E. Canonicalization / cross-tenant reuse

Normalize concrete values/routes.

Example:

```text
/item/12345
```

becomes:

```text
/item/:id
```

Optionally demonstrate:

```text
artifact recorded on base app
        ↓
artifact applied to slightly different app variant
        ↓
per-variant override
```

This represents tenants using different configurations/versions of the same vendor application.

## F. Multi-run stability

Replay the same artifact N times and report a stability/flakiness signal.

---

# 19. Ground Rules

## AI-assisted development

AI assistance is explicitly encouraged.

You own everything you submit.

You must be able to:

- explain it
- defend the design
- explain important implementation details

## Website usage

Do not automate against sites where doing so would:

- violate terms
- harm the service
- require inappropriate real credentials

Prefer:

- sandbox
- demo site
- local app

for sensitive work.

## Secrets

Keep secrets out of the repository.

Use environment variables or another appropriate secret mechanism.

## Time box

There is no deadline, but the assignment is not intended to consume an entire month.

The evaluation is about:

- judgment
- engineering decisions
- integration
- reasoning

If stopping early:

- document unfinished work
- explain why
- list next steps

---

# 20. Glossary

## Computer use

An LLM operating a computer interface like a human:

```text
read screen/page
→ decide
→ click/type/navigate
```

instead of calling an API.

## DOM

The browser's structured representation of a page.

A clean DOM has:

- meaningful elements
- stable identifiers

Legacy applications often do not.

## Accessibility tree

A representation exposed by browsers/operating systems for accessibility tools and screen readers.

It can be more stable than raw markup and can also exist for desktop applications.

## Locator / selector

The mechanism used to tell automation which control to interact with.

The locator strategy strongly affects replay reliability.

## Test ID

An attribute added by developers specifically so automation can find an element reliably.

Legacy enterprise applications often do not have test IDs.

## Deterministic replay

Re-running a recorded flow without an LLM making decisions.

Conceptually:

```text
same inputs
→ same recorded actions
→ same expected checkpoints
→ same declared outputs
```

## Checkpoint

An assertion proving the system actually reached the expected state.

Do not assume a click succeeded merely because the click command executed.

## Business outcome vs. failure

Example:

```text
"No such member"
```

can be a legitimate business outcome rather than a system failure.

The design must not conflate the two.

## Tenant

One customer institution.

The real environment has hundreds of institutions, many using the same vendor software with different configurations.

---

# 21. Submission Requirements

Submit to:

```text
public GitHub repository
```

Then email the repository link to:

```text
assignments@interface.ai
```

Requirements:

- put the repository URL on its own line
- use the email address you applied with
- do not send a zip

---

# 22. Agent Implementation Contract

## [AGENT GUIDANCE — derived from coding-agent best practices]

The following does not add assignment requirements. It is a recommended operating contract for an AI coding agent implementing the assignment.

### 22.1 Investigate before changing code

Before making implementation decisions or modifying the repository:

1. inspect the existing directory structure
2. inspect existing files relevant to the task
3. inspect Git status
4. inspect Git configuration
5. inspect configured Git remotes
6. inspect configuration and dependency files
7. inspect existing tests
8. inspect existing README/documentation
9. understand the current architecture
10. identify existing commands for setup/test/lint/run

Do not delete, overwrite, reset, or substantially modify existing work until you understand what it is.

Do not blindly initialize Git or replace an existing remote. First determine the current repository state and configuration.

Do not speculate about code that has not been inspected.

Anthropic specifically recommends grounding coding-agent behavior in files actually opened and investigated. citeturn1search0

### 22.2 Treat the assignment as the source of truth

Maintain a requirement checklist.

Do not accidentally replace an explicit assignment requirement with a more convenient implementation.

Do not silently omit, weaken, or reinterpret an explicit requirement.

### Important open-ended decisions

For any genuinely important implementation or architectural decision that is open to interpretation:

1. Identify the decision.
2. Consider 3–4 genuinely distinct approaches.
3. Compare them based on:
   - assignment requirements
   - correctness
   - reliability
   - simplicity
   - maintainability
   - ability to demonstrate the requirement
   - realistic production extensibility
   - implementation effort
4. Select the approach with the strongest overall fit.
5. Record the decision and rationale in `DECISIONS.md`.

Do not generate alternatives for trivial implementation choices.
Do not create alternatives merely to appear thorough.
Do not over-engineer simply because multiple approaches are possible.

When an ambiguity does not materially affect architecture, correctness, reliability, or evaluation, make a reasonable decision and continue.

### 22.3 Work outcome-first

The desired outcome is more important than blindly following an invented implementation sequence.

Use:

```text
requirement
→ design decision
→ implementation
→ verification
→ evidence
```

rather than creating unnecessary process steps.

Current OpenAI guidance similarly recommends defining expected outcomes, success criteria, constraints, evidence requirements, and stopping conditions, while allowing the coding model to choose an efficient implementation path. citeturn0search2

### 22.4 Maintain an explicit requirement matrix

Create an internal/project tracking artifact such as:

```text
requirements_matrix.md
```

or another appropriate structured file.

Track:

| Requirement | Status | Implementation | Evidence/Test |
|---|---|---|---|
| Goal-driven loop | TODO | ... | ... |
| Real LLM discovery | TODO | ... | ... |
| Structured artifact | TODO | ... | ... |
| Deterministic replay | TODO | ... | ... |
| Runtime error taxonomy | TODO | ... | ... |
| Safety allowlist | TODO | ... | ... |
| Sensitive-data redaction | TODO | ... | ... |
| Observability | TODO | ... | ... |
| Human escalation | TODO | ... | ... |
| Same-session handoff | TODO | ... | ... |
| Heterogeneity design | TODO | ... | ... |
| Multi-tenant design | TODO | ... | ... |
| README | TODO | ... | ... |
| REPORT.md | TODO | ... | ... |
| /evidence/ | TODO | ... | ... |

Keep this accurate as implementation progresses.

### 22.5 Prefer minimal architecture

Do not introduce distributed infrastructure unless the requirement actually benefits from it.

Default toward:

```text
simple
→ modular
→ testable
→ observable
→ easy to run
```

The assignment explicitly says that simplicity is acceptable and that infrastructure breadth is not rewarded.

### 22.6 Make the artifact schema a first-class design object

Before implementing replay, define the artifact contract.

It should answer:

- What capability is this?
- What surface/app does it target?
- What version is it?
- What inputs does it require?
- What outputs does it return?
- What actions does it execute?
- How does each action locate its target?
- What waits/checkpoints exist?
- What errors/business outcomes are recognized?
- What safety policy applies?
- What evidence is generated?
- What tenant/version information matters?

Avoid storing raw LLM transcripts as the primary replay representation.

### 22.7 Separate discovery from replay

Architect the system so that:

```text
Discovery
    ↓
Artifact
    ↓
Replay
```

is a clean boundary.

The replay engine should not need to reconstruct the LLM's reasoning.

The artifact should contain the information needed for deterministic execution.

### 22.8 Build verification into the implementation

After changes, run the most relevant available checks:

- targeted unit tests
- integration tests
- type checks
- linting
- build checks
- smoke test
- real discovery run
- real replay run

Use meaningful tests rather than tests that merely mirror implementation details.

OpenAI's current coding-agent guidance recommends targeted validation, type/lint/build checks where applicable, and a smoke test when full validation is too expensive. citeturn0search2

### 22.9 Evidence is a deliverable, not an afterthought

Do not finish the implementation and discover that the required real discovery run was never captured.

Plan evidence generation into the implementation.

At minimum produce:

```text
/evidence/
    discovery/
    replay/
    artifact/
```

The exact organization is your choice, but the final repository must contain:

- saved artifact
- discovery evidence
- replay evidence

### 22.10 Test exceptional states intentionally

Do not only demonstrate:

```text
happy path → success
```

Also demonstrate at least one:

```text
invalid input
OR
not found
OR
simulated/injected failure
```

and show that the system produces the correct structured outcome.

### 22.11 Keep human handoff real

Do not implement:

```python
raise NotImplementedError("human handoff")
```

or merely document it.

At least the core control transfer should work:

```text
automation
→ pause
→ intervention state
→ human control
→ human action
→ resume
```

The operator UI may be minimal/mock.

### 22.12 Do not over-engineer

Avoid:

- unnecessary abstractions
- unnecessary services
- unnecessary configuration
- speculative features
- framework proliferation
- large infrastructure
- unrelated refactors

Anthropic's current guidance specifically warns that coding agents can over-engineer by creating extra files, abstractions, and flexibility that were not requested. citeturn1search0

### 22.13 Use state files for long-running work

For a long implementation, keep durable state in files such as:

```text
PROGRESS.md
requirements_matrix.md
DECISIONS.md
```

Useful information includes:

- completed requirements
- current implementation status
- known failures
- next task
- architectural decisions
- commands that work
- commands that fail and why

Structured state such as test results is useful in structured formats; progress notes can remain human-readable. Anthropic recommends explicit state tracking, structured test state, setup scripts, and incremental progress for long-horizon coding work. citeturn1search0

### 22.14 Provide a one-command or small-command developer workflow

Where practical, provide scripts such as:

```text
scripts/setup.sh
scripts/test.sh
scripts/demo.sh
scripts/replay.sh
```

or equivalent commands.

The README must remain the authoritative user-facing entry point.

### 22.15 Use Git as a recovery mechanism

Make coherent commits/checkpoints during development.

Before risky changes:

- inspect `git status`
- understand existing changes
- avoid deleting unfamiliar work
- avoid destructive Git commands unless explicitly appropriate

Anthropic recommends Git-based state tracking for long-running coding-agent work. citeturn1search0

---

# 23. Recommended Agent Workflow

## Phase 1 — Understand

Before coding:

1. Read this entire specification.
2. Inspect the existing directory and repository state.
3. Inspect Git status, Git configuration, and configured remotes.
4. Inspect available files and dependencies.
5. Identify the target surface.
5. Identify how a genuine LLM discovery run can be performed.
6. Identify the smallest architecture that satisfies every must-have.

Output internally:

```text
requirements
→ constraints
→ decisions
→ implementation plan
```

Do not start by generating large amounts of code.

---

## Phase 2 — Lock the core contract

Define first:

1. artifact schema
2. action/locator model
3. typed inputs
4. typed outputs
5. checkpoint model
6. result contract
7. error taxonomy
8. safety policy
9. handoff/control-state model

These are the load-bearing pieces.

---

## Phase 3 — Implement the vertical slice

Implement the smallest working path:

```text
goal
→ LLM observes live surface
→ LLM acts
→ success
→ artifact
→ replay
→ output
```

Then add:

```text
runtime exception handling
→ safety
→ evidence
→ human handoff
```

---

## Phase 4 — Verify

Run:

1. unit tests
2. integration tests
3. real LLM discovery
4. real artifact generation
5. deterministic replay
6. exceptional-state replay
7. human handoff path
8. safety checks

Do not claim completion based only on code inspection.

---

## Phase 5 — Produce submission artifacts

Ensure:

```text
README.md
REPORT.md
/evidence/
```

are complete.

Verify:

- commands actually work
- paths are exact
- evidence is committed
- no secrets are committed
- report headings are exact
- repository is runnable by another engineer

---

# 24. Final Acceptance Checklist

Before declaring the project complete, verify every item below.

## Core

- [ ] Natural-language goal accepted.
- [ ] Target app/URL/entry point accepted.
- [ ] Real UI is used.
- [ ] Real LLM-driven discovery run completed.
- [ ] Discovery evidence saved under `/evidence/`.
- [ ] Successful run produces structured artifact.
- [ ] Artifact is typed.
- [ ] Artifact is serializable.
- [ ] Artifact is versioned.
- [ ] Artifact is reviewable.
- [ ] Artifact contains ordered actions.
- [ ] Artifact contains target identification strategy.
- [ ] Artifact contains typed inputs.
- [ ] Artifact contains typed outputs.
- [ ] Artifact contains checkpoint/success condition.
- [ ] Artifact is decoupled from raw model transcript.
- [ ] Replay uses artifact + inputs.
- [ ] Replay makes no LLM decisions in its normal path.
- [ ] Replay uses stable targeting.
- [ ] Replay verifies checkpoints.
- [ ] Replay returns declared outputs.
- [ ] Runtime errors are detected.
- [ ] Business outcomes are distinct from failures.
- [ ] Recoverable conditions are distinct from hard failures.
- [ ] Failure results contain useful debugging information.

## Safety

- [ ] Explicit configurable allowlist exists.
- [ ] Domain/route/action restrictions are enforced.
- [ ] Safe/reversible vs risky/irreversible actions are distinguished.
- [ ] Risky actions have a conservative policy.
- [ ] Secrets are not persisted.
- [ ] Raw sensitive data is not persisted.
- [ ] PII is appropriately redacted.

## Observability

- [ ] Structured run log exists.
- [ ] Log captures actions and rationale.
- [ ] Failure produces richer evidence.
- [ ] Discovery evidence exists.
- [ ] Replay evidence exists.
- [ ] At least one exceptional/error case is demonstrated or intentionally documented if impossible.

## Human handoff

- [ ] Stuck/blocked condition can be detected.
- [ ] Intervention request is generated.
- [ ] Request contains goal/capability context.
- [ ] Request contains current step.
- [ ] Request contains current state/screenshot.
- [ ] Request contains reason for stopping.
- [ ] Automation pauses.
- [ ] Human takes control of the same live session.
- [ ] Human can perform manual action.
- [ ] Human actions are recorded.
- [ ] Control can return to automation.
- [ ] Run can resume/complete.
- [ ] Control ownership/state is explicit.

## Generalization

- [ ] Surface abstraction is explained.
- [ ] Legacy web extension is explained.
- [ ] Desktop extension is explained.
- [ ] Multi-tenant reuse is explained.
- [ ] Tenant/version drift is explained.
- [ ] Per-tenant specialization/override strategy is explained.
- [ ] Full multi-tenant infrastructure is intentionally not overbuilt.

## Documentation

- [ ] `/README.md` exists.
- [ ] Setup instructions work.
- [ ] Configuration/API-key requirements are documented.
- [ ] Offline/non-live mode is documented if available.
- [ ] Exact discovery command is documented.
- [ ] Exact replay command is documented.
- [ ] `/REPORT.md` exists.
- [ ] REPORT uses the exact seven required headings.
- [ ] REPORT explains architecture.
- [ ] REPORT explains artifact schema.
- [ ] REPORT explains determinism/error handling.
- [ ] REPORT explains heterogeneity/multi-tenant design.
- [ ] REPORT explains escalation/handoff.
- [ ] REPORT explains safety.
- [ ] REPORT explains cuts and next steps.
- [ ] `/evidence/` contains saved artifact.
- [ ] `/evidence/` contains discovery logs.
- [ ] `/evidence/` contains replay logs.
- [ ] Optional screen recording included if useful.
- [ ] No secrets committed.
- [ ] Public GitHub repository is ready.
- [ ] Submission email requirements are satisfied.

---

# 25. Recommended File Structure

This is a suggested structure, not a mandated assignment structure.

```text
repo/
├── README.md
├── REPORT.md
├── pyproject.toml / package.json / equivalent
├── .env.example
│
├── src/
│   ├── agent/
│   │   ├── loop
│   │   ├── observation
│   │   └── actions
│   │
│   ├── artifact/
│   │   ├── schema
│   │   ├── serialization
│   │   └── validation
│   │
│   ├── replay/
│   │   ├── executor
│   │   ├── locators
│   │   ├── waits
│   │   ├── checkpoints
│   │   └── errors
│   │
│   ├── safety/
│   │   ├── allowlist
│   │   └── action_policy
│   │
│   ├── handoff/
│   │   ├── control_state
│   │   ├── intervention
│   │   └── resume
│   │
│   └── observability/
│       ├── logging
│       └── evidence
│
├── tests/
│
├── evidence/
│   ├── discovery/
│   ├── replay/
│   └── artifacts/
│
└── scripts/
    ├── setup
    ├── test
    └── demo
```

Adapt this to the selected language/framework. Do not create empty modules solely to match this example.

---

# 26. Agent Prompt Template

If using this specification as context for a coding agent, a concise task prompt can be:

```text
You are implementing the Take-Home Project: Computer-Use Automation System.

Read the complete assignment specification in ASSIGNMENT_LLM_FRIENDLY_SPEC.md before coding.

Objective:
Build a small but real end-to-end implementation satisfying every MUST HAVE requirement.

Success means:
1. A real LLM-driven discovery run operates a live UI.
2. The successful run produces a typed, versioned, serializable, reviewable artifact.
3. The artifact can be replayed deterministically without LLM decision-making.
4. Replay returns typed outputs and distinguishes business outcomes, recoverable conditions, and hard failures.
5. Safety allowlists and sensitive-data protections are enforced.
6. The system produces structured evidence.
7. A stuck/risky condition can transfer control to a human on the SAME live session and then resume.
8. README.md, REPORT.md, and /evidence/ satisfy the assignment exactly.
9. The implementation has a credible design for heterogeneous surfaces and multi-tenant reuse without unnecessarily implementing that infrastructure.

Operating rules:
- Inspect the repository before making assumptions.
- Do not speculate about code you have not inspected.
- Preserve explicit assignment requirements.
- Prefer the smallest architecture that satisfies all requirements.
- Do not over-engineer.
- Treat the artifact schema, replay contract, error taxonomy, safety model, and control-transfer model as first-class design objects.
- Verify changes with meaningful tests and a real end-to-end smoke test.
- Produce actual evidence for the real discovery and replay runs.
- Keep secrets and sensitive data out of the repository.
- If something is intentionally mocked or cut, document exactly what and why.
- Continue working through implementation and verification rather than stopping after a plan.
- Before declaring completion, run the final acceptance checklist in the specification.

First:
1. inspect the repository,
2. summarize the existing state,
3. create a requirements/implementation matrix,
4. propose the minimal architecture,
5. identify the target surface and real discovery path,
6. then implement and verify the vertical slice.
```

---

# 27. Why This Format Is Better for Coding Agents

This transformation is intended to improve execution reliability without changing the assignment.

The source assignment is optimized for a human evaluator. This version makes the same requirements easier for an agent to execute by making:

- mandatory requirements explicit
- optional requirements separate
- design choices separate from fixed constraints
- implementation contracts explicit
- acceptance criteria checkable
- error categories explicit
- evidence requirements explicit
- stopping conditions explicit
- agent workflow explicit
- progress tracking explicit
- verification explicit

Anthropic recommends clear/direct instructions, sequential structure when completeness matters, useful examples, structured context, explicit success criteria, durable state tracking, incremental progress, verification tools, and avoiding over-engineering in long-running coding tasks. citeturn1search0

Claude Code also supports project-level `CLAUDE.md` instructions that can hold architecture, coding standards, workflows, and common commands; these files are automatically loaded into the relevant project context. citeturn1search1

OpenAI's coding-agent guidance similarly emphasizes outcome-first prompts, explicit success criteria and constraints, persistence, investigation before guessing, and concrete validation commands. citeturn0search2turn0search3

**Do not treat this file as a replacement for the evaluator's original PDF. Treat it as an execution-oriented translation of the same assignment plus clearly marked agent guidance.**
