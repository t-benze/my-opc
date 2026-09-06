# Orchestrator: Routing, Permissions & State

> **SUPERSESSION NOTICE (TASK-4009/TASK-4012/TASK-4195/TASK-4346):** The skill
> materialization model described in §4.6–§4.10 has been superseded by the
> **canonical skill store + workspace symlink architecture**. The legacy
> wholesale-copy model (``_WHOLESALE_DUMP_ENABLED``, ``_copy_skills_tree``,
> ``refresh_session_skills``, direct copy/injection helpers) is REMOVED as an
> executable path. See ``protocol/05b-agent-runtime.md`` § "Canonical skill
> store + workspace symlinks" for the current canonical model. All skill
> delivery now routes through ``materialize_workspace_skills``, which creates
> validated relative symlinks to hash-addressed canonical packages under
> BOTH ``.claude/skills`` and ``.agents/skills``. The executor and daemon
> share the same OS identity — integrity is enforced by synchronous pre-launch
> hash detection against ledger-declared member hashes with DETECTION-ONLY,
> FAIL-CLOSED refusal (no automatic repair from same-UID local source).
> **macOS (darwin) and Linux**; Windows and unknown platforms fail closed. The
> legacy sections below are preserved for historical reference.

The application layer that drives the organization — task routing, inter-team communication, permissions, and the task state machine.

---

## 1. Orchestrator Responsibilities

The orchestrator is the application code that ties everything together. It spawns executor-backed agent sessions, feeds manager decisions back into a loop, routes work between teams, and persists every step.

```
┌─────────────────────────────────────────────────┐
│                  ORCHESTRATOR                     │
│                                                   │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Escalation│  │  Audit   │  │  Performance  │  │
│  │  Router   │  │  Logger  │  │   Tracker     │  │
│  └──────────┘  └──────────┘  └───────────────┘  │
│                                                   │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │Inter-Team │  │ Knowledge│  │   Founder     │  │
│  │  Comms    │  │   Base   │  │  Dashboard    │  │
│  └──────────┘  └──────────┘  └───────────────┘  │
│                                                   │
│  ┌──────────────────────────────────────────┐    │
│  │         Agent Executor Abstraction        │    │
│  │   Claude Code │ Codex │ OpenCode │ Pi │ … │    │
│  └──────────────────────────────────────────┘    │
└─────────────────────────────────────────────────┘
        │              │              │
   ┌────▼────┐   ┌────▼────┐   ┌────▼────┐   ┌────▼────┐
   │ Content  │   │ Product │   │   Ops   │   │   CX    │
   │  Team    │   │  Team   │   │  Team   │   │  Team   │
   └─────────┘   └─────────┘   └─────────┘   └─────────┘
```

### What the orchestrator does

**1. Receives work requests** and routes them to the right Team. A new content brief goes to Content Team. A partner application goes to Ops Team. A bug report goes to the Product & Engineering Team.

**2. Manages inter-Team communication.** When the Content Team publishes a guide, it notifies the CX Team so Support Agent knows about new content. When the Product & Engineering Team changes a payment flow, it triggers a cross-audit task in the Ops Team. These are not internal to any one Team — the orchestrator handles the handoff.

**3. Runs the escalation router.** When an agent calls the `escalate` tool, the orchestrator evaluates the 12 escalation rules (from `04-escalation-rules.md`) and either routes to the relevant manager's Team or sends a notification to the founder.

**4. Manages the revision loop.** When QA returns REVISE, the orchestrator tracks the revision count and either re-triggers the Content Team with feedback or escalates after max rounds.

**5. Audits delegations.** After each delegated child task terminates, the orchestrator writes a `review_verdict` audit row. The audit verdict is a **distinct fact** from the child's completion status: when the child reported an explicit structured `verdict` (a free-string workflow value such as `APPROVE`, `PASS`, or `REQUEST_CHANGES`), that reported verdict is preserved verbatim. Only when no structured verdict is present does the orchestrator fall back to the legacy implicit mapping (`approved` for COMPLETED, `rejected` for FAILED/no-report). Dashboard readers normalize the free-string spellings case-insensitively and with benign whitespace/separator variation; an explicitly blank or unknown verdict is treated as unknown (no approval tone/count), never as approved. The founder reviews these via `happyranch audit` to identify which agents need attention. (The legacy 30-day rolling tier classification was removed on 2026-05-27 — see §2.)

**6. Assembles agent context.** Before each session, the orchestrator gathers the system prompt, learnings file, team health, and task-specific context, then writes them into the agent's workspace in the format expected by the configured executor.

**Manager root supersession (THR-152 phase 1).** The `supersede` completion decision is a standard, always-available manager action. It is a same-manager, same-team, currently claimed root replacement with a nonblank successor brief, rationale, and strict manager-supplied `attestation`; it has no arbitrary task-id/API/CLI/dashboard surface. The compact attestation contains a nonblank recovery reason plus affirmative declarations covering policy/product intent, budget/external commitment, permission/cross-team, schema/auth/security/privacy/data access, and unresolved Founder gates. The decision and nested attestation reject missing, malformed, nested-extra, blank, mixed/override, and explicit contrary declarations before mutation. This is structural validation of the supplied evidence only: it is not proof that declarations are true, cannot detect a valid-looking false declaration, and does not infer authority from prose. Managers MUST escalate the named categories rather than supersede them; that policy obligation is distinct from postmortem audit evidence. A dedicated append-only relation stores the rationale, literal briefs, SHA-256 hashes, actor/session/rule-versioned JSON attestation evidence, and bidirectional audit records in the same transaction as predecessor `superseded` and successor `pending`. A successful supersession is informational only: no Founder escalation, approval, or notification is emitted. Phase 1 rejects every root with a nonempty `dispatched_from_thread_id` before any supersession mutation; thread-origin traversal/dispatch integration is explicitly deferred to phase 2. If such a structurally valid decision reaches the runtime consumer, a dedicated transaction revalidates the complete currently claimed manager root/session and thread-origin predicate before atomically committing the existing runtime-raised escalation and audit denominator. Predicate loss is a silent no-op, preserving any competing continuation, block, or cancellation without escalation, notification, authority-hook, or thread projection. A successful rejection terminalizes the orchestration attempt without creating a successor, so startup and zombie recovery cannot replay the unchanged decision.

**7. Provides the founder dashboard.** Aggregates audit logs, escalation summaries, and team health metrics into a weekly report.

**D7B custom-adapter profiles** (``command_adapter_id: custom-adapter:<id>``):
bind to exactly one registered, conformance-passed, founder-APPROVED custom
adapter executable. The ``CustomAdapterExecutor`` spawns the adapter as a
subprocess with v1 ``AdapterInput`` JSON on stdin and parses v1
``AdapterOutput`` JSON from stdout. The stable v1 contract is defined by the
Pydantic models at ``runtime/orchestrator/adapter_contract.py``; the **canonical
contract surface for external consumers** is the versioned
``GET /api/v1/runtime/adapters/contract-reference`` endpoint (THR-107 seq184),
which returns server-generated JSON Schemas. **THR-107 seq339/340:** the
contract-reference response now also returns ``canonical_directory`` and
``required_executable_path`` — the daemon-managed canonical adapter path.
Scoped submissions (``POST /runtime/adapters/submit``) enforce that the
wrapper is at exactly this path; the registration seam independently rechecks.
The normative prose is the signed
architecture §2. Key invariants: exact approved artifact SHA-256 verified at
EVERY launch (including throttle retries — the check is inside the per-attempt
launch closure); mandatory valid AdapterOutput; adapter version, contract
version, and session-id echo enforced before mapping any result; subprocess-only
(no Python import/discovery); PENDING adapters cannot bind or launch; D5
baseline-only permission posture.

THR-200 PR 1/3 adds dormant, backward-compatible v1 session plumbing only.
For a legitimate thread invocation ``AdapterInput.session`` may contain a
non-empty resume id, and optional ``AdapterOutput.session_status`` reports
``fresh``, ``resumed``, or ``not_found``. Coherence is validated after launch
and before any provider session id is exposed; violations are
``post_launch_contract`` failures with forensic tails preserved. Supplying a
resume id outside a thread fails pre-launch. ``contract_version`` remains 1,
legacy outputs without the optional status remain valid when no session was
sent, tasks remain fresh, and no capability is earned or consumed here.
``thread_runner`` still excludes custom profiles, and SQLite transcript and
delivery semantics remain canonical.

THR-200 PR 2/3 reserves ``thread_resume`` against all submitted capability
lists and adds only an explicit registration-time ``verify_thread_resume``
path. In one daemon-owned temporary workspace, the server proves fresh canary
delivery and a nonempty provider id, resumes that id with a prompt that omits
the first canary while requiring the result to recall it alongside a second,
then requires a fabricated-id ``not_found`` failure with empty result text.
All three must pass before one atomic AdapterEntry write appends the earned
capability and records its verification time and probed contract version.
Failure publishes nothing and cleans the workspace; re-registration clears
approval and cannot inherit the receipt without another full proof. The receipt
has no dispatch consumer in PR 2, so custom profiles remain fresh/full-prompt,
built-in behavior is unchanged, and contract version 1 remains current.

**THR-107 Slice 1A mint authority foundation.** The existing
master-authenticated runtime adapter-purpose mint optionally accepts an exact
first-party ``workspace_adapter_id`` and persists a nonsecret,
domain-separated fingerprint plus server-derived intent in daemon-global
runtime-root SQLite. Slice A adds the single loopback registration-token-only
``POST /api/v1/runtime/custom-cli/connect`` route ahead of adapter catch-alls.
It produces a durable, fingerprint-only ``received_nonlaunchable`` receipt
after canonical wrapper and v2 manifest validation; it has no projection,
profile/adapter state, executor eligibility, runner effect, or browser client.
If the final registration-token commit fails, Slice A compensates the
receipt/event boundary. Existing adapter mints that omit the optional field
retain the legacy PENDING submission behavior.

**THR-107 Slices 1–3 landed** (full contract in `protocol/05b-agent-runtime.md`
§ "Slices 1–3: projection, launch fence, UI cutover"). Summary for the
orchestrator/executor surface this doc covers: a separate master-bearer
`POST .../{operation_id}/commit` route (never `/connect`, which stays
zero-subprocess) projects a receipt through `planned → committed|failed` and
durably writes the SAME `AdapterEntry`/runtime-profile shape the legacy
founder-approval path writes (`registered_by`/`approved_by:
"direct-connect"`) — so `build_executor()` / `CustomAdapterExecutor`'s
existing APPROVED + hash-reverified-at-every-Popen launch fence covers a
direct-connect profile with zero origin-specific branching. No new gating
code was needed for the launch fence; `tests/test_thr107_launch_fence.py`
proves this across the ordinary-task and thread/wake/dream/schedule call
shapes. A daemon-owned periodic projection sweep also invokes that coordinator
for every receipt without a projection row, so browser closure cannot strand a
connection; see `protocol/05b-agent-runtime.md` for the normative detail.
Each direct projection runs one bounded behavioral conformance probe before
any adapter/profile persistence: the wrapper forwards the entire normal v1
``AdapterInput.prompt`` through one real provider invocation, obtains a genuine
terminal provider response, and returns the wrapper-owned ``AdapterOutput``.
The provider does not construct that envelope; a fabricated/static success is
not proof. The fresh opaque canary must appear complete in ``result.text``;
the probe accepts only a successful, returncode-consistent terminal output with
the matching HappyRanch invocation ID and canonical adapter ID. The short probe
guides no optional tool use or workspace exploration, but does not enforce or
collect telemetry about those provider-internal actions; normal task behavior is unchanged. Provider
``agent_session_id`` is optional and resume-only; when a provider returns one,
the adapter must preserve it faithfully, but it cannot substitute for the
HappyRanch invocation-identity proof. Candidate-controlled stdout/stderr/error text and the canary
are never persisted or returned on failure; the projection records only a
bounded category. ``/connect`` remains receipt-only and starts zero
subprocesses. This direct-only behavioral gate does not require a registered
adapter or bound executor profile and does not make legacy/operator
registration stricter. Token usage remains optional here: it is required only
when a candidate declares the established ``token_metering`` capability and
supplies trustworthy canonical ``token_usage``; direct candidates are not
rejected merely for omitting optional usage fields.

**Thread reply breaker lifecycle (THR-200 PR A + PR B).** The runtime store
contains an additive episode/receipt/index substrate, but PR A adds no admission
fence, scheduler, transition execution, config activation, API projection, or
visible behavior in PR A. The `Database` initializer is the only shipping migration
caller, and pinned historical stores plus the actual `e197b20` application
reader establish the single-version compatibility boundary. The founder-approved
runtime lifecycle in PR B counts only structured final post-launch outcomes,
opens at exactly three, coalesces without launching, and uses a daemon-owned
exactly-900-second scheduler for one durable HALF_OPEN probe. Threshold 3 and
cooldown 900 are fixed policy constants, not settings or environment overrides.
Each tick also republishes any durable pending probe token. Process-queue
deduplication owns the token while queued or in flight, acknowledges normal
worker return, and releases pre-claim exception/cancellation for retry; the
durable claim CAS permits exactly one provider launch. Recovered ownerless gaps
without an episode enter only through cooldown probing. Held OPEN-exchange
deferrals are excluded and never released by breaker recovery. Durable receipts
and lease CAS preserve OPEN/PROBE across restart and reject duplicate/stale
callbacks. No manual action or wire/web projection is added.

Thread failure audit payloads expose capped raw stdout/stderr tails additively.
Known workspace-trust warning-only stderr cannot outrank a parsed structured
Claude terminal reason in the human summary; raw classification inputs and the
existing throttle/supervisor/breaker retry ownership are unchanged.

**Correction — `workspace_adapter_id` is CLI-declared, not founder-chosen.**
The Slice 1A paragraph above describes `workspace_adapter_id` as a
mint-time founder choice; that shipped, then was reversed once tracing
showed the field is read ONLY at `happyranch init-agent` time (to pick a
workspace-bootstrap convention) and plays no role in the connect/probe
handshake — the connecting wrapper is the only party that actually knows
which convention its CLI expects. The `/connect` manifest now carries a
REQUIRED `workspace_adapter_id` declared by the wrapper itself; `receive()`
stores that value (not the mint-time one) onto `direct_connect_operations`,
which projection already reads from. The mint-time value is now just a
fixed internal trigger for the Slice-1A authority row, never founder-chosen
and never read for real behavior. See `protocol/05b-agent-runtime.md`'s
matching correction for the full contract.

**THR-107 seq244 dependency manifest:** new adapter registrations and
submissions MUST declare a versioned dependency manifest
(``dependency_manifest_version: 1``, non-empty ``dependencies`` list of
``{executable: absolute-path, sha256: hex}`` records).  Each declared child
executable is validated at registration (absolute path, regular file,
executable, SHA-256 match) and re-validated before EVERY launch attempt.
An adapter with a declared manifest forbids HappyRanch's selection of
wrapper/agentic child executors by ambient PATH — they are explicitly
absolute and hash-pinned/revalidated with no executor fallback. The
adapter process inherits normalized PATH for normal callback/utility
availability.
An adapter declaring ``token_metering`` capability MUST produce a valid
non-null ``token_usage`` at conformance time.  Legacy entries without the
manifest retain their exact current launch behavior and are never
auto-mutated.  A dependency change requires re-submission and founder
re-approval.

### Inter-Team communication patterns

| Trigger | From Team | To Team | Payload |
|---------|-----------|---------|---------|
| Content published | Content | CX | New guide summary + URL for Support Agent |
| Payment flow change proposed | Product | Ops | Change spec for Compliance Agent cross-audit |
| Compliance audit finding on payment | Ops | Product | Finding + recommended fix for Payment Agent |
| Recurring support issue identified | CX | Content | Feedback ticket requesting guide update |
| Recurring support issue (feature gap) | CX | Product | Feature request with user data |
| Partner communication drafted | Ops | Content | Draft for brand voice review |
| CX feature request submitted | CX | Product | Feature request for feasibility check |

### Implementation approach

The orchestrator is a Python application that:
- Instantiates the 4 Teams with their agents and task templates
- Exposes an API (or CLI) for submitting work requests
- Maintains state in a database (SQLite for prototype, PostgreSQL for production)
- Runs agent sessions via the executor abstraction (not all running simultaneously)
- Listens for escalation signals and inter-team communication
- Persists audit logs and agent memory

---

## 2. ~~Performance Tier Impact on Team Configuration~~ (REMOVED)

The performance-tier feature was removed on 2026-05-27. The audit log
(`review_verdict` rows after every delegation, plus completion / failure
events) is sufficient for the founder to identify which agents need
attention via `happyranch audit`. The audit verdict is separate from the
child's completion status — a completed child that reported
`REQUEST_CHANGES` carries `REQUEST_CHANGES` as its audit verdict, not an
inferred `approved`. Tier classification on top of the
verdicts added no behavioral enforcement in code, and the per-agent tier
prose in agent `.md` files was not actionable (workers never saw their
own tier; managers saw worker tiers but the tier didn't gate delegation).

---

## 3. Permission and Authority Model

### Approach: executor-native sandboxing + system prompt guardrails

Agents run through their configured executor. Claude sessions use `claude --permission-mode auto` plus a narrow `Bash(happyranch:*)` allow rule for callbacks. Codex sessions use `codex exec` with the configured sandbox mode. opencode sessions use `opencode.json` for bash permission mapping. Pi sessions use `pi -p ... --mode json` and have no HappyRanch-managed sandbox or permission file. Permissions are otherwise generous — agents can read, write, and execute within their workspace.

**Founder-concern boundaries** (the only things that truly need restricting) are enforced through two layers:

1. **System prompt** — each agent's bootstrap doc (`CLAUDE.md` or `AGENTS.md`) explicitly states what it cannot do. The agent is instructed to call `escalate()` when it encounters these boundaries.
2. **Orchestrator post-session review** — the orchestrator inspects completion reports and audit logs for violations. If an agent somehow bypasses its system prompt instructions, the orchestrator catches it and escalates.

This approach avoids building a complex custom permission layer. The executor handles low-level sandboxing, while the system prompt provides the "soft" guardrails and the orchestrator provides the "hard" backstop.

### What counts as a founder-concern boundary

Per the org charter, these are the ONLY restrictions that matter:

| Boundary | Enforced by |
|---|---|
| No `git push` to main / production deploy | System prompt + orchestrator review |
| Spend >$200 single or >$100/month recurring | System prompt → escalation tool |
| Raw payment card data storage (PCI-DSS) | System prompt + orchestrator review |
| Political sensitivity in content | System prompt → escalation tool |
| Refunds >$150 | System prompt → escalation tool |
| Downtime >30 minutes | System prompt → escalation tool |

Everything else — file access, shell commands, network requests, git operations on feature branches — is auto-approved.

### What happens when an action is blocked

There are four types of permission blocks, each handled differently:

#### Type 1: Out-of-scope action
**What**: Agent tries something outside its role entirely.
**Example**: Content Writer tries to run `git push` or modify `src/payments/stripe.py`.
**Response**: Executor blocks immediately. Agent receives: "Permission denied: file write to src/payments/ is outside Content Writer scope. This is Payment Agent's domain."
**Agent behavior**: Notes the blocker in its completion report under "dependencies." Completes everything else it can.
**Orchestrator action**: Logs the attempt. No further action needed — the system worked correctly.

#### Type 2: Needs higher authority
**What**: Agent needs approval that exceeds its authority level.
**Example**: CX Manager tries to approve a $200 refund (above $150 limit). Ops Manager wants to agree to a 6-month partner contract (above 3-month limit).
**Response**: Agent calls `escalate(category="budget", severity="medium", summary="Refund of $200 requested by tourist for cancelled tour. Exceeds my $150 authority.")`.
**Task state**: Moves to `waiting_for_approval`. The agent completes all other work on the task and submits a completion report with the pending approval clearly noted.
**Orchestrator action**: Routes the escalation per the 12 rules in `04-escalation-rules.md`. Creates a founder notification with the agent's summary and recommendation. Holds the specific blocked step (not the entire Team). non-root tasks do not escalate directly to the founder.
**Resolution**: The direct `POST /tasks/{id}/resolve-escalation` / CLI route remains a named **manual break-glass** exception under the shared-bearer trust model. It is auditable as `resolution_path=manual_break_glass`, but is not the autonomous-continuation path. Supersede mints a successor task from the provided brief and closes the escalation as `superseded`. Cancelling an escalated task uses the normal `POST /tasks/{id}/cancel` route, which terminates the task in `cancelled` (cancelled_at set) with no resume/context injection.

### THR-181 pre-escalation authority evaluation (Track A)

**S6a semantic-decision supersession.** For a newly launched eligible manager with an active immutable policy, the semantic result is the strict closed `manager_self_evaluation` carried by that manager's authenticated completion. It is bound to task-result/session/release/activation/contract and the effective manager provider/executor/model. Production constructs no subprocess evaluator. Missing, malformed, extra-field, mismatched, stale, replayed, ambiguous, and low-confidence results fail closed with digest-only diagnostics. The injectable evaluator remains only for strict isolated tests and legacy static-policy compatibility. This paragraph supersedes older “production evaluator” wording below; every daemon-owned gate, ordering rule, and denominator invariant remains normative.

**Immutable release-controlled policy authority.** Before a **current manager-owned Engineering root's** proposed escalation (`decision.action == "escalate"`) is committed, including a thread-originated current root, the orchestrator runs exactly **one audited LLM authority evaluation** of the proposed reason against the immutable, release-controlled Engineering policy `engineering/pre-escalation-authority@v1` (`runtime/orchestrator/authority_policy.py`). The policy is code-and-deploy controlled under the accepted shared-identity posture: there is no mutable database/config activation switch, and agents/managers cannot self-modify or self-activate their governing policy. Historical census eligibility is NOT a prerequisite and is NEVER consulted — reachability depends only on a release-controlled policy existing for the manager's team and the root being current/manager-owned. Thread id/origin remain structured provenance, not an eligibility or final-consumption fence; they do not relax same-root identity or the no-revisit/no-successor/no-supersession/no-fresh-root-replacement rules.

**Semantic authority only.** The policy is semantic authority; **server-owned mechanical fences are non-overridable** — cancellation, budget exhaustion, protected gates, root-only escalation, same-root-only continuation, and every server-derived predicate bind regardless of any policy output, and no policy output may override a mechanical fence. The proposed escalation reason is **untrusted input**: it can never establish a server fact, waive a fence, or widen the hook's reach; only its digest is persisted. A committed escalation remains founder/human-resolved.

**Server-derived facts outrank reason prose.** The evaluation snapshot carries authoritative **structured server facts** with provenance — fence outcomes, budget counters/ceilings, lineage (revisit/parent/successor/thread/fresh-root), active-work/block/cancellation/session state, adverse child review verdicts, partial-work (daemon zombie) evidence, the org permission/allow-rule digest, and the DB schema digest — each an immutable digest-carrying fact the untrusted reason can never establish or alter. A server-**PROVEN** must-escalate fact forces ESCALATE regardless of what the evaluator says: an adverse child review verdict → `esc-adverse-review-qa`; partial-work evidence → `esc-partial-work`; and **DB-schema drift** — the live `sqlite_master` digest differs from the release-pinned schema digest (the schema a fresh `Database()` built from the current code creates) → `esc-schema-overloaded-column`. A **protected-surface change landing while the evaluator runs** (the org permission digest or the live schema digest captured in the snapshot changed by the post-evaluation recheck) also fails closed with the matched clause (`esc-permission-sandbox-allow` / `esc-schema-overloaded-column`). Neither a misleading nor an omitted reason can authorize CONTINUE_SAME_ROOT (the hook gate and the strict CI fake both apply these predicates).

**CONTINUE_SAME_ROOT is a server decision, never a prose classification.** Because the proposed reason is untrusted input, a CONTINUE_SAME_ROOT grant is honored ONLY when the reason is a **byte-exact member of the release-controlled closed routine set** (`CONTINUE_ACCEPTED_REASONS` in `authority_policy.py`; the canonical phrase is `"routine same-root follow-through of the already-completed slice"`) AND every server-derived predicate above is clean. The server then has complete knowledge of the prose content, so the grant never depends on keyword classification or on the completeness/truthfulness of the reason for any protected boundary; any other reason — including a semantically similar paraphrase that omits, misstates, or hides a protected-boundary condition — fails closed to ESCALATE (`esc-ambiguity-novelty`). The exact narrow permitted action (return the current root to `pending` + re-enqueue) is additionally server-proven safe across every protected category: it is a fixed, audited transaction (status flip + audit rows only — no schema DDL, no permission/auth code, no destructive operation, no external commitment), bounded to one further orchestration step by the budget fence, reversible, and the root remains re-escalatable.

**Dark DB-policy persistence foundation (S1).** Additive immutable
`authority_policy_releases` and append-only `authority_policy_activations`
tables represent authored team policy and monotonic activation history without
overloading the existing attempt tables. `authority_candidate_policy_pins` is
an immutable one-to-one sidecar for candidates created through the future
DB-backed-policy path; it binds the candidate to a same-team release and
activation, the activation's exact epoch, and non-null provider/executor
identity. Candidate plus pin insertion is one transaction. Historical and
current static-policy candidates remain intentionally unpinned: there is no
backfill, inference, or fabricated activation identity. S1 exposes this as a
test-callable store API only. The shipped `run_authority_hook` continues to use
the legacy candidate claim and therefore S1 is not evidence that production
candidates are populated or that DB policy is activated. Production source
integration and activation are separately reviewed later slices.

**Read-only team policy projection (S2).** The dedicated authenticated route
`GET /api/v1/orgs/{slug}/agents/{agent_name}/team-escalation-policy` is
available only when the per-request roster resolves the exact Engineering
manager tuple (`engineering_manager`, `engineering`, `manager`). Every other
target returns the same `policy_surface_not_available` 404 and the shared
Agent roster/serializer remains unchanged. Every eligible projection includes
a deterministic `bootstrap_template` derived from the same server-owned
`POLICY_BY_TEAM` definition and canonical phrase used by release validation;
clients carry no second clause authority, and POST requires the canonical
clause id/category/action ordering. An eligible empty store returns 200 with
`bootstrap_required=true` and omits `active`; an eligible active
store exposes only immutable release/activation data and honest
`shared local operator credential` attribution. The database/store current
read selects the maximum team epoch and reuses the S1 release, activation
seal, and full-history validators; missing/corrupt/incoherent state is the
sanitized `policy_store_unavailable` failure. The S2 read itself neither
creates nor activates policy. S3 exposes immutable release creation and thus
truthfully reports `can_mutate=true` in both empty and active responses; that
capability never implies activation. S4/S6a shipped runtime injection,
candidate pins, and manager self-evaluation; S7 adds the bounded operator
projection and explicit founder-authorized activation described below.

**Dark policy mutation API (S3).** `POST .../releases` server-owns the exact
Engineering team/policy identity and next version, validates the closed typed
clause vocabulary (all protected/mechanical clauses, unique ids, exact
category/action pairs, one continuation clause), byte-exact canonical phrase,
bounded nonblank prose/JSON, and secret-shaped input rejection, then derives
the canonical APR id/digest. It appends the immutable release and one
`config:authority-policy:engineering` conventional audit receipt atomically;
the audit contains only ids, digests, action, team, and shared-local-operator
attribution, with that closed receipt derived and verified at the transaction
owner rather than accepted from a caller. The transaction resolves a request
id's exact request-digest replay before validating the mutable active base; a
new request still validates that base and a changed digest still conflicts.
`POST .../activations` accepts the CAS/reactivation contract as an explicit
founder-authorized action. The authenticated route enforces the exact
Engineering-manager allowlist and closed request model before proceeding
through the S1 store's sealed CAS, exact replay, stale-epoch, same-team, audit,
and older-version rollback checks without exposing guessed release existence
as a distinct oracle. Shipping the route is not production activation.

**S6b/S7 projection and activation.** The authenticated Engineering
Manager surface exposes bounded stable snapshot/keyset pagination, with opaque
independent cursors and deterministic tie-breakers, for immutable release and
activation receipts plus secret-free self-evaluation outcomes. Concurrent
newer inserts are outside the initial traversal snapshot and cannot shift,
duplicate, or omit its older rows. It projects
only durable release/activation/policy, prompt, provider, executor, model,
task/result/session/thread/hook/envelope pins and emits `receipt_incomplete`
when a causal join is absent or corrupt; raw evaluator output, rationale,
prompts, policy prose, and secrets are prohibited. Workers and ineligible
managers receive the same surface-unavailable 404 and omit the surface in the
Agent payload and DOM. Rollback creates a new monotonic epoch pointing at an
older immutable release. Eligibility remains explicitly
`engineering/engineering_manager`; the role/team seam is reusable but enables
no other manager. Activation is an explicit founder-authorized operator act
subject to the ordinary daemon authentication, allowlist, closed request,
immutable release linkage, exact replay/idempotency, audit, and CAS fences.
Code landing or redeploy is not production activation.

Release identity is derived at the typed store boundary, never supplied as a
second caller-controlled authority. Its canonical JSON and SHA-256 cover
exactly `policy_id`, `version`, `team`, `title`, `normative_text`, the closed
canonical `clauses` array (`id`, `category`, `condition`, `action` only), and
`continuation_phrase`; the release id is exactly `APR-<sha256>`. Ancestry
(`based_on_release_id`), shared-credential actor attribution, and creation time
are immutable provenance receipts outside that semantic digest. Activation
actions are truthful history labels: only a history-free team may bootstrap;
`activate` selects a release never activated for that team; and
`reactivate_rollback` selects a non-current, previously activated release of
the same `policy_id` whose positive integer `version` is strictly lower than
the currently active release's version. This release-version relation—not
lexicographic release-id ordering—defines "older". These rules hold at both
the store transaction and direct-SQL trigger
boundaries while preserving append-only monotonic epochs and request replay
identity. Existing databases receive the canonical activation-validation
trigger through a guarded definition retrofit: an absent or stale trigger is
rebuilt once, while an ordinary open with the canonical definition performs no
trigger DDL.

Each activation also carries a distinct immutable `activation_digest`; the
`request_digest` remains request provenance and is never treated as the
activation seal. Deterministic canonical JSON (UTF-8, sorted keys, compact
separators) covers the exact closed field set `id`, `team`, `epoch`,
`release_id`, `previous_activation_id`, `expected_previous_epoch`, `action`,
`actor_kind`, `request_id`, `request_digest`, and `created_at`. The trusted
fresh-activation factory derives the seal from an already validated
closed construction payload. The persisted/transport receipt requires a
non-empty canonical SHA-256 seal. The typed store ingress and durable database
ingress each rebuild a deep primitive snapshot without deriving, defaulting,
backfilling, or resealing it, then independently recompute and compare the
expected digest before beginning a transaction. Missing, malformed, and
mismatched seals therefore fail before any durable side effect.
Every activation lookup, including lookup through a candidate pin, recomputes
the seal, resolves the release and same-team linkage, and replays sealed
predecessor, epoch, action, and rollback-version history through the receipt;
corruption fails closed. SQLite triggers continue to enforce only relational
and history facts, not a false claim that SQLite authenticates canonical JSON.
Because these S1 tables are unmerged and dark, an interrupted pre-seal
activation table is upgraded only when empty. A populated pre-seal table is
refused on reopen: no activation seal or provenance is inferred or fabricated.

**Hook ordering.** (1) server eligibility + mechanical fences are evaluated and recorded (`manager_ownership`, `current_session`, `cancellation`, `claimed_root`, `revisit_lineage`, `successor_lineage`, `active_work`, `budget_exhausted`) — any failure is an `ineligible` outcome and the escalation proceeds unchanged; thread id/origin remain in structured lineage provenance but are not a fence; (2) the immutable input snapshot (with the structured server facts above) is built and one durable candidate is claimed via the deterministic CAS tuple (root/session/causal-event/policy/prompt/model); (3) exactly one bounded evaluation runs through the injectable evaluator seam (production: extract exactly one object, enforce the strict closed schema, validate the exact echo contract, validate/normalize policy-clause membership and action, and only then scan explicitly model-controlled free-text fields for credential-like content; CI: strict deterministic fake whose CONTINUE also requires the byte-exact release-controlled routine phrase). Invalid trusted, structural, or closed-vocabulary fields therefore use the malformed-output path even when free text also contains a credential marker; (4) the server-derived must-escalate gate AND the closed-pattern CONTINUE gate are applied to the normalized verdict — a server-PROVEN fact or a non-accepted reason forces ESCALATE; the server-selected clause/action remain authoritative while any independent evaluator error or uncertainty code remains attached as diagnostic evidence rather than being replaced; (5) the single immutable evaluation row is recorded (`created -> evaluated`); (6) a post-evaluation FULL fence re-check closes any category that changed while the evaluator ran (cancelled/stale, never continue), including a permission/schema surface digest change during the attempt; (7) the candidate is consumed exactly once (`evaluated -> consumed`); (8) all audit events and the single `authority_hook` outcome row are persisted; (9) the verdict executes. The only semantic results are `ESCALATE` — which proceeds through the exact existing escalation path (try_escalate CAS, `escalation` audit row, founder notification, thread `task_escalated` projection) — and `CONTINUE_SAME_ROOT` — which must name the matched immutable policy clause and exact permitted action, and executes ONLY that action: return the current root to `pending` (atomic CAS + audit) and re-enqueue it for another manager decision step. No successor, supersession, revisit, fresh root, or new task is ever created; no escalation is suppressed, retried, or resolved.

**Fail-closed matrix.** The hook fails closed to ESCALATE on ambiguity; a reason that is not a byte-exact release-controlled routine phrase; malformed/missing/unknown/extra-field output; wrong clause/action; timeout/provider error; policy/team/version/digest/candidate/input mismatch; audit persistence failure (an audit failure can never permit continuation — every audit event and the outcome row are written before or atomically with the continuation); protected boundary (including every server-derived predicate and a permission/schema surface change during the attempt); cancellation (before, during, and at the final CAS); any exhausted limit; stale/CAS conflict; restart-incomplete state; and any successor/supersede/revisit/fresh-root signal. Teams without a release-controlled policy (e.g. Content) are outside the hook: their manager-root escalations proceed unchanged with no authority records.

**Audit denominator.** Every committed escalation after Track-A activation has exactly one `authority_hook` audit_log denominator outcome. Manager-decision attempts retain the evaluated outcomes (`continued_same_root` | `escalated` | `ineligible` | `cas_lost` | `cancelled_stale` | `evaluator_failure` | `audit_failure` | `capture_failure`). Runtime-raised mechanical escalations that are not authority decisions record `not_applicable` with a stable bounded reason code and `causal_escalation_audit_id`; they never create a candidate/evaluation or invoke policy/LLM evaluation. The runtime task transition, `escalation` row, and `not_applicable` row commit atomically, so replay/CAS loss or rollback leaves no duplicate or orphan denominator. A continuation is impossible unless the manager-attempt row says `continued_same_root`. Each candidate carries immutable input provenance (root/session/manager/team identity, causal event id+digest, reason digest, policy/prompt/model ids+versions+digests, snapshot digest, fence results, structured server facts) plus the single evaluation (disposition, code, response digest) and the four audit events. Raw credentials/prose/model exchanges are never persisted.

**Restart/CAS behavior.** Candidate identity is derived from the **immutable task-result/session causality** — the persisted `task_results` row id that produced the escalate decision (`result:<row id>`) — never from a freshly written orchestration-step audit id, so real restart/recovery re-entry (`_consume_completion_report` via the boot sweep or zombie reaper, which rebuild the report from the SAME row) maps to the SAME candidate: no second candidate and no second evaluation are possible. The DB is the single-evaluation and exactly-once-consumption arbiter (UNIQUE candidate id on `authority_evaluations`; `created -> evaluated -> consumed` CAS; append-only triggers; lifecycle guard trigger). A restart left in any state — claimed/created, evaluation-missing, evaluated-but-unconsumed, already-consumed — fails closed: a replayed attempt loses the claim (`cas_lost`, `ineligible`, or `cancelled_stale`), never re-evaluates, never continues. A `continue_same_root` commit is atomic with its audit rows (`commit_authority_continue_same_root`) AND **atomically re-validates the complete current fence set at consumption time** — candidate/policy/input identity, manager ownership and session, exact team, root status, cancellation, block/active-work, revisit/successor lineage, orchestration and revise budgets (against the current ceilings), zombie/partial-work evidence, and adverse child verdicts — inside the same transaction as the continuation + audit rows, so a fence that changed while the evaluator ran can never slip through a stale verdict; thread id/origin remain provenance and are not a final-CAS rejection. Post-commit re-enqueue is best-effort and the run-step claim CAS remains at-most-once.

**Single-use continuation lifecycle envelope (founder ruling).** Every `CONTINUE_SAME_ROOT` commit atomically mints an `authority_continue_envelopes` row bound to the evaluation, immutable causal result, matched clause, manager/team/root/session, and policy identity. Its `active -> consumed | violated` lifecycle is DB-enforced and terminally audited across inline consumption, boot recovery, zombie recovery, cancellation, and session failure. The next daemon-accepted manager result consumes the envelope after normal parsing; the envelope is not an exact-action whitelist. The continued manager turn uses that agent's ordinary configured executor permissions on Claude, Codex, opencode, pi, generic, and custom adapters where the ordinary runtime supports them. There is no executor capability refusal, Claude `--allowedTools` narrowing, or opencode permission-map swap. Supersession/successor/revisit/fresh-root replacement remains outside the same-root grant, while ordinary manager decisions (including ESCALATE and bounded delegation) retain their normal validation. The daemon remains authoritative for identity, cancellation, replay, CAS, budgets, protected boundaries, and audit reconciliation.

### THR-166 bounded autonomous continuation

The sole autonomous edge is `POST /threads/{thread_id}/resolve-escalation`
with `decision=continue`, exercised by the task's **same assigned manager**
using the pending `TASK_FOLLOWUP` causally minted from that root's own
`task_escalated` system message. REPLY, BOOTSTRAP, another manager, another
thread, a replayed token, or a noncausal follow-up fail closed.

The server accepts only immutable founder policy
`THR-166-genuine-human-blocker@1` (`founder:THR-166:seq-29`) and the exact
class `repair_review_reverify_reevaluate_original_gate`. It never authorizes
the original protected/destructive action. A structured attestation and the
single exact terminal result that caused the bound escalation are audited. The
causal `TASK_FOLLOWUP` records that result's task, result id, terminal status,
verdict, output snapshot, and timestamp; the route re-derives and compares the
same record, root, owner, thread, invocation, escalation, lineage, and
freshness server-side. Later descendants, unrelated terminal records, prose,
brief text, KB text, and quotes are audit context only, never authority.

### THR-225 task-followup replacement root

An authoritative pending `TASK_FOLLOWUP` invocation owned by the dispatching
team manager may dispatch one corrected replacement root for its causal
thread-dispatched root. The one-replacement budget and permanent lineage fence
come only from existing persisted invocation, system-message, task-link, thread,
and audit state. A single `BEGIN IMMEDIATE` transaction revalidates pending
token identity/purpose, manager authority held by the route's teams-registry
lock, open thread, causal root, and unspent budget, then creates a true root
(`parent_task_id IS NULL`) with its thread link, invocation marker, system
message, and audits. Queue notification is post-commit.

The replacement root and all supported persisted descendants, retry/revisit
ancestry, chain/fanout legs, and terminal followups are forever ineligible.
Manager supersession is a separate product path that rejects every
thread-originated root, so it cannot create a successor of a THR-225
replacement and is not part of this acceptance matrix. Replay/concurrent losers return
`task_followup_dispatch_already_used` without mutation. Malformed/noncausal or
stale tokens fail closed; archive/cancellation ordering is decided by the final
transactional revalidation. No migration/backfill or column reinterpretation is
involved, so historical v0 DB-backed and v1 flat/single-org stores retain their
existing behavior; absence of the required persisted evidence fails closed for
this new edge only.

Absolute human blockers remain escalated: schema/migration or overloaded
meaning; permission/sandbox/allow-rule changes; auth, credentials, security,
privacy, or data access; spend/budget; destructive/irreversible action;
external contract/product commitment; genuine ambiguity/novel situations;
cancellation; live children; exhausted orchestration-step, revise-round, or
per-slice retry budget; and absent, stale, unrelated,
nonterminal, malformed, or conflicting evidence. Unknown conditions fail
closed. The same server-derived budget predicate is checked both before the
user-visible response and in the final atomic transition. A successful transition atomically consumes the follow-up, records a
distinct audit event, consumes notification intent, and moves `escalated` to
`pending`; post-commit queue delivery is recoverable because run-step's claim
CAS remains at-most-once. Cancellation wins if it commits first and prevents a
late delivery from resurrecting execution.

#### Type 3: Needs another agent's work
**What**: The task has a cross-agent or cross-team dependency.
**Example**: Payment Agent proposes a payment flow change, but it needs Compliance Agent review before Engineering Head can approve. Dev Agent needs to implement a feature that requires a partner API endpoint, but Partner Liaison hasn't onboarded that partner yet.
**Response**: Agent identifies the dependency in its completion report: "Blocked on: Compliance Agent cross-audit of this payment flow change. Cannot proceed until PCI-DSS and cross-border compliance is verified."
**Task state**: Moves to `blocked_on_dependency`.
**Orchestrator action**: Reads the dependency from the completion report. Spawns the required agent session (Compliance Agent in the Ops Team) with the dependency context. The blocking task is queued, not abandoned.
**Resolution**: Once the dependency agent completes its work, the orchestrator resumes the blocked task with the dependency result injected into context.

#### Type 4: Ambiguous or novel situation
**What**: Agent encounters something not covered by existing permissions or SOPs.
**Example**: Content QA discovers content that might be politically sensitive but isn't sure. Compliance Agent finds a regulation that could be interpreted two ways.
**Response**: Agent calls `escalate(category="novel", severity="medium", summary="...")` with its best assessment and a recommendation.
**Task state**: Moves to `waiting_for_guidance`.
**Orchestrator action**: Routes to founder. The agent's recommendation is included so the founder can often just approve/deny rather than research from scratch.
**Resolution**: The unified `resolve-escalation` verb offers two decisions:
`supersede` (mint a successor task from a provided brief, close the
predecessor as `superseded`) or `continue` (re-enqueue the same task to
pending). Continue is reachable from both the task surface
(`POST /tasks/{id}/resolve-escalation`) and the thread surface
(`POST /threads/{id}/resolve-escalation`). Cancel is NOT part of the
resolution vocabulary — cancelling an escalated task uses the normal
`POST /tasks/{id}/cancel` route. When the ruling should bind future
occurrences, the founder writes a KB entry via `happyranch kb add` (with
`source_task: <task-id>` in frontmatter) so the next agent finds the
answer without re-escalating.

### Task state machine

#### States (7)
- **pending** — created; no agent subprocess started yet.
- **in_progress** — an agent subprocess is running, OR the task is a parent waiting on its own children/jobs. A parent waiting on its own children/jobs stays `in_progress`; the waiting reason is recorded in `block_kind` (`delegated` = waiting on one or more child subtasks to terminate; `blocked_on_job` = waiting on one or more background jobs to reach a terminal state, set when a completion report carries a non-empty `waiting_on_job_ids`); `block_kind IS NULL` ⟺ a subprocess is running now.
- **escalated** — waiting on a founder or manager resolution (via `happyranch resolve-escalation`); was `blocked(escalated)`.
- **completed** — terminal, success.
- **failed** — terminal, unsuccessful.
- **cancelled** — terminal; founder-initiated stop, distinct from `failed`.
- **superseded** — terminal. An `escalated` / `in_progress(delegated)` task closed because a human-authorized continuation (founder `revisit`, or a founder/manager thread-dispatch) superseded it; the close cites the successor task and does **not** re-run the work.

> **Deprecated — `blocked` (fully retired Phase 3).** Before THR-037 Change B (Path B, stored source-of-truth), the surfaced vocabulary used a single `blocked` state discriminated by `block_kind` (`delegated`/`escalated`/`blocked_on_job`). Path B collapsed it; the value was retained for the transition window + reverse migration and was fully retired in Phase 3 after a soak.

#### Failure-recovery contract (TASK-573, THR-028, THR-078)

When a subtask reaches a terminal state, the orchestrator evaluates the parent task
for advancement. If any subtask FAILED (rather than COMPLETED), the parent is NOT
cascade-failed. Instead:

1. **Bounded manager-wake.** The parent task (a task with `task_type='task'`) is
   re-enqueued for a fresh manager decision step. The failed subtask's reason
   (`note` + completion report / error context) is available to the parent so it
   can author an updated brief and re-delegate.

2. **Owner-adjudication primary (THR-078).** Any fan-out round with ≥1 non-clean
   slice packs per-slice terminal context (status + verdict + confidence + note +
   output_dir) and wakes the root owner to adjudicate — the orchestrator does NOT
   auto-escalate to founder on a mixed round. The owner classifies each slice
   (merge greens / re-dispatch legit REQUEST_CHANGES as revise / drop no-ops /
   retry genuine failures). Applies uniformly to benign AND real failures.

3. **Per-slice retry ceiling (THR-078).** A per-slice retry ceiling of 1
   replaces the old count-based `_FAILURE_ROUND_BOUND` (2). The owner may
   re-drive a given slice ONCE; if that same slice fails AGAIN (its 2nd failure),
   the orchestrator forces escalation to founder. The guard moves from 'count of
   FAILED siblings anywhere in the fan-out' to 'this specific slice is genuinely
   stuck after one retry'. Per-slice retry count is derived from EXISTING database
   lineage: the child's `revisit_of_task_id` chain (no schema migration). When a
   fan-out owner re-dispatches a failed slice, the new child carries
   `revisit_of_task_id` pointing to the failed predecessor; if that retry child
   also fails, the orchestrator detects the revisit ancestor within the same
   parent and escalates.  **The `revisit_of_task_id` field is MANDATORY** —
   a re-delegate to an agent with a FAILED child under the same parent that
   omits this field is HARD-REJECTED (feedback, no child spawned), even on
   the first retry.  Only FAILED ancestors count toward the ceiling; a retry
   of a COMPLETED predecessor does not trigger escalation on its first failure.
   A later COMPLETED or SUPERSEDED descendant in the same `revisit_of_task_id`
   lineage retires earlier FAILED ancestors for ceiling evaluation (THR-183).

4. **Exhaustion escalation.** When the per-slice ceiling is exhausted (a slice's
   2nd failure), the parent transitions to `escalated` via
   `db.try_escalate()`, carrying the causal terminal event — the current
   unresolved FAILED leaf of the slice's lineage — in the escalation reason.
   A later COMPLETED or SUPERSEDED descendant retires earlier FAILED siblings
   so a completed-child wake cannot select a stale failure reason. The parent
   does NOT cascade-fail — the founder can resolve the escalation per existing
   routes. non-root tasks never escalate directly — they fail and hand back to
   their parent; only the (root) parent escalates on exhaustion.

5. **Chain-leg failure.** When a workflow chain leg fails (subtask is FAILED, not
   COMPLETED), the chain does NOT cascade-fail the parent. Instead, the
   chain is cleared and the parent is handed back to its manager decision step.

6. **Happy path unchanged.** All subtasks COMPLETED → parent advances to its
   next decision step (existing behavior). REVISE-verdict auto-advance is
   unchanged.

#### Fan-out (parallel delegation)

A manager may declare a fan-out decision (`action: fanout`) to spawn N children
in parallel (2 ≤ N ≤ 8). The orchestrator:

1. **Validates** width, width_cap_ack, workspace presence, and scope. A child may optionally carry `then`/`expect_verdict` — a *pipeline carrier* (Phase 2) — whose legs are validated exactly like an inline `delegate + then` chain (each leg needs `agent` + `prompt`; a configured reviewer leg additionally MUST declare `expect_verdict: "APPROVE"` — omitted is a HARD REJECT, THR-175). HARD REJECT denies the whole fan-out before any child spawns and returns feedback to the owner naming the required `expect_verdict: "APPROVE"`, leaving the root PENDING and re-enqueued for a corrected decision — never a root failure. Missing agent name / missing workspace are unrecoverable and keep hard terminal failure.
2. **Atomically mints** all N children via `try_delegate_many`, transitioning
   the parent to `in_progress(delegated)` with `active_fanout` set (an additive
   JSON metadata column). For pipeline carriers, the child's inline chain is
   materialized on its own row (see Pipeline carriers below).
   **Child task_type:** a child targeted at a team manager receives
   `task_type='task'` so its delegate-chain decisions are parsed (mutating
   fan-out, THR-056 msg39); a child targeted at a worker receives
   `task_type='subtask'` (read-only). Pipeline carriers are always `subtask`
   (they never run agent sessions of their own).
3. **Parks** the parent — the existing `DELEGATED` barrier wakes it once when
   all N children are terminal (same CAS as single-child delegation).
4. **Injects join context** into the manager's wake prompt: a structured block
   listing each child's id, agent, status, summary excerpt, output_dir, and
   failure note.
5. **Clears** `active_fanout` after successful join claim or terminal parent
   close.

**No fan-out review gate (THR-012 msg 129/131).** The width cap (8) is a
pure machine-resource limit — children are spawned immediately at any width
2–8. The former `pending_review` status and `review_required` job gate are
removed. The real control over what code lands is the per-PR merge gate:
every mutating child opens its own PR requiring reviewer APPROVE +
`qa_engineer` PASS + CI + founder/EM merge. The founder cannot add useful
judgment to "6 vs 8 children" — it is a resource question for the runtime.

**Pipeline carriers (Phase 2).** A fan-out child that carries a non-empty `then` is a *carrier*: on spawn the orchestrator materializes its inline chain (`active_chain` on the child's row, via the same path as an ordinary `delegate + then`) instead of dispatching a bare read-only child. The composition is safe because `active_fanout` lives on the parent's row and `active_chain` lives on each child's row — **two independent columns on two different rows, never the same row, so there is no clobber** (the two-column-two-row invariant). Carrier detection is schema-free: a carrier is any task whose id is in its parent's `active_fanout.children_ids` and which has a non-empty `active_chain`; no new column. **Lifecycle rule: a carrier reaches a terminal state only after its own chain completes.** When a carrier's final leg matches its `expect_verdict`, the carrier has no session of its own to run — it terminates directly and feeds the parent's fan-out barrier (`_enqueue_parent_if_waiting`) without waking a manager. A carrier's internal legs never wake the parent; only the carrier's own terminal status counts toward the barrier.

**Mutating children (THR-056 msg39, option 3).** A fan-out child targeted at a
team manager that does NOT pre-declare `then`/`expect_verdict` (i.e., a plain PENDING
child, not a pipeline carrier) receives `task_type='task'` instead of
`task_type='subtask'`. Its agent session runs, and when it returns a
`delegate` decision with an inline chain (`then` legs), the orchestrator
parses the decision (since `task_type='task'`) and spawns the implementation
subtree inside that branch using the standard chain mechanism. The child parks
as `in_progress(delegated)` with `active_chain` set, and `_enqueue_parent_if_waiting`
handles chain auto-advance exactly as for a top-level inline delegate chain.
When its chain completes, it terminates and feeds the original fan-out
parent's barrier. The fan-out parent does not join until ALL children
(including mutating ones) are terminal. A mutating child's internal chain legs
do not count toward the fan-out parent's barrier — only the mutating child's
own terminal status does.

Failure-join (THR-078): any fan-out round with ≥1 failed child wakes the
root owner with structured per-slice join context (status + verdict +
confidence + note + output_dir) — the orchestrator does NOT auto-escalate
based on failed-sibling count.  The owner adjudicates each slice.  A
retained per-slice retry ceiling (ceiling = 1) fires escalation only when
the SAME slice fails twice: the re-dispatched child carries
`revisit_of_task_id` pointing to the failed predecessor, and the
orchestrator detects the revisit ancestor within the same parent.  For a
pipeline carrier this is **fail-closed at the carrier**: a leg
verdict-mismatch or a failed leg fails the whole carrier (no partial-chain
completion), and the failed carrier then feeds the parent's barrier exactly
as any failed child does.  No partial-join or cascade-fail semantics are
introduced.

**Worktree isolation (mutating fan-out).** Each mutating child inherits the
make-worktree pattern: it receives its own git worktree on a per-task branch
(`task/<task_id>`), edits its disjoint file set, commits, and pushes. The
child's worktree is created at spawn and torn down after completion.
Cost (~200-500ms + disk per child) is bounded by MAX_FANOUT_WIDTH=8.

**Integration model (a).** Each mutating child opens its own PR. The parent
join surfaces each child's PR reference (number/URL) when present in the
child's output, using the existing join-context child output (no new PR
entity, no new schema). The parent join summarizes outcomes; the founder or
EM merges each child PR individually through the normal per-PR gates.

**Shared-file serial doctrine.** Children must own DISJOINT file sets — it
is the manager's responsibility to partition work so no two children touch
the same file. Shared-file convergence does NOT route through a fan-out
child; it routes through a SERIAL follow-up delegate spawned by the manager
after the fan-out join. This is a binding design rule, not a runtime
enforcement (the manager brief carries the obligation).

**Fail-closed at the child.** A failed mutating child discards its worktree
and cascades per bounded failure-recovery. No partial integration — if one
child fails, the parent's join context shows the failure and the manager
decides next steps (retry, revise, escalate). Successful children are not
rolled back; their PRs remain open for independent merge.

Startup recovery (daemon restart) re-enqueues parked `in_progress(delegated)`
fan-out parents when all children are already terminal (same as
single-delegation). The join context is built from persisted audit rows when
the CAS winner processes the wake.

#### Daemon restart recovery — pid-liveness probe (THR-079)

On daemon restart, tasks that were `in_progress` with `block_kind IS NULL`
(i.e., had a live executor subprocess) are NOT assumed dead. Instead, the
sweep reads the persisted `executor_pid` (set at session start by the
orchestrator's `_on_started` closure) and probes the OS with `os.kill(pid, 0)`:

| Probe result | Action |
|---|---|
| pid ALIVE | **Leave alone** — session survived the restart; no reconcile. |
| pid DEAD (`ProcessLookupError`) | See orphaned-result check below; if no orphaned result exists → **FAILED** with reason "session died on daemon restart — executor pid not alive". |
| pid NULL or probe inconclusive (`PermissionError`, etc.) | See orphaned-result check below; if no orphaned result exists → **FAILED** with reason "session liveness undeterminable on daemon restart" (fail-closed default). |

**Orphaned task_result consumption (THR-090 Track A).** Before failing a
dead-pid task in Branch 1, the sweep checks for an unconsumed ``task_result``
row from the CURRENT session (the definitive TASK-2625 fingerprint: a
completion callback that landed after the daemon died). If one exists, the
sweep honors the completion by consuming the report via
``_consume_completion_report`` — the transition the agent already reported is
preserved via the same machinery used in inline consumption. No new
``TaskStatus`` value and no new transition edge is added; the sweep is merely
closing the loop that the daemon crash opened.

**Session-scoping is mandatory.** The sweep reads the persisted
``current_session_id`` (set at session start alongside ``executor_pid`` in
``_on_started``) and calls ``get_latest_task_result(task_id, agent,
current_session_id)``. A prior-step result row carries a different session
uuid and is never matched. This prevents replay of already-consumed
delegate/fanout results from earlier orchestration steps.

| Condition | Action |
|---|---|
| ``current_session_id`` is None | Fall through to dead-pid FAIL path (no session-scoping possible — TRANSITIONAL: pre-migration/backfill row from the rollout window only, NOT permanent designed behavior). |
| Row found under current session | **Consume** the report via ``_consume_completion_report`` — honor the agent's reported transition. |
| No row under current session | Fall through to dead-pid FAIL path (no unconsumed result exists). |

Governing invariant: err toward a MISS (fail-closed), NEVER replay an
already-consumed decision. Within Branch 1 (in_progress + block_kind NULL +
dead pid), a result row from the CURRENT session is definitionally UNCONSUMED
— a consumed manager result would have set block_kind (delegate/fanout) or
terminalized the task, moving it OUT of this branch.

No auto-revisit is spawned for any of these outcomes — the founder receives
a `daemon_restart_failure` audit row and decides whether to re-dispatch.
Pre-migration rows (NULL ``executor_pid``) are fail-closed on the first
post-deploy restart (intended and acceptable).

NOTE: `os.kill(pid, 0)` carries a pid-recycle caveat — a recycled pid could
read as falsely-alive. A falsely-alive false-positive is acceptable relative
to the risk of duplicate runs from a false-negative.

**Duplicate live callback (TASK-3127).** Independently of restart recovery,
the `/completion` route itself short-circuits a duplicate POST of an
already-succeeded call: on a tracker miss it probes
`get_latest_task_result(task_id, agent, session_id)` and returns an
idempotent `200` when the row exists (the same probe the boot sweep and
ongoing reaper use, §Ongoing zombie reaper). This closes the false-orphan
class where a lost HTTP response drove a duplicate POST into
`409 unknown_session`. It does **not** add a new transition edge and does
**not** re-consume the decision — the terminal-status idempotence guard
(`_is_already_terminal`) remains the single point that applies a decision
at most once across the inline, boot-sweep, and reaper paths.

#### Completion-status lag (THR-211)

**Actual contract.** The completion POST (`submit_completion`) durably
persists the session-scoped `task_results` row and clears the session
tracker, but intentionally does **not** terminalize the `tasks.status` row:
`in_progress` with `block_kind IS NULL` still represents a live executor,
and the status transition (`COMPLETED` / `FAILED` / blocked-on-job park)
is applied only when the orchestrator consumes the report at
executor/session finalization (inline `run_step_impl` tail, daemon boot
sweep §Daemon restart recovery, or ongoing zombie reaper). A `200` from
the completion route therefore means *the result has landed*, not *the
status row has transitioned* — the durable `task_results` row is the
landing proof and the read surfaces (`happyranch details`, `/tasks/{id}`,
`/recall`) keep showing the honest row state (`in_progress` + the landed
result) until consumption.

**Dispatch/gate recognition.** The parent-dispatch seams that previously
keyed strictly on `child.status` now also recognize a durably-landed
structured terminal report during the lag window, so a landed verdict can
never be masked by the deferred status transition:

- `_enqueue_parent_if_waiting` sibling-terminal check and `run_step_impl`
  step-1 delegated-parent eligibility treat a child as dispatch-terminal
  when its **current** session has landed a report (see authority below) —
  the fan-out barrier and bounded parent wake unblock as soon as the last
  child's result is durable, without waiting for session finalization.
- `_enqueue_parent_if_waiting` chain-advance branch may auto-advance a
  chain from a landed report while the leg row is still `in_progress`.
  The gate consumer carries the exact authenticated report (the
  `(task_id, assigned_agent, current_session_id)`-scoped row) through
  `compute_advance_action` and the carrier verdict-mismatch check — an
  unrelated newer row (wrong agent or wrong session) can never advance or
  clear the chain, and a non-authoritative passing row can never substitute
  for the authenticated verdict. Recognition is **at-most-once**: the
  advance is gated to the parent's
  newest child (the current chain leg), so the delayed session-finalization
  consumption of the same child can neither spawn a duplicate next leg nor
  prematurely clear the chain. The atomic `try_advance_chain` transaction
  (rollback on write failure) and the run-step claim CAS preserve the
  single eventual advance/wake.

**Authority (session-safe, fail-closed).** Recognition uses exactly the
same fingerprint the boot sweep and zombie reaper use: the exact
`(task_id, assigned_agent, current_session_id)` triple via the scoped
`get_latest_completion_report(task_id, agent, session_id)` lookup, with
report `status == "completed"`. It fails
closed on absent agent/session, no row, prior-session or wrong-agent rows,
`blocked` reports (the blocked-on-job park is a live state owned by the
resume flow), unknown statuses, and malformed rows. It never infers
terminality from prose, never suppresses a genuinely-running task with no
terminal result, and never terminalizes the task row early. The unscoped
`get_latest_completion_report(task_id)` newest-row read remains available
for non-chain readers (status/display, fan-out join context) and as the
fallback when no agent/session fingerprint exists (legacy rows). When a
modern fingerprint EXISTS but the exact authenticated report is missing or
unacceptable (exact-row miss, or an exact row that is malformed/unknown/
blocked/nonterminal), the chain/gate consumers fail closed: the chain is
cleared and the parent wakes, and task-wide evidence is NEVER consulted for
chain advancement — only a genuinely legacy child (no fingerprint) may use
the unscoped newest-row fallback.

**Required contract.** Do NOT set `tasks.status` terminal inside the
completion POST: early transition risks lifecycle/restart/session races.
Keep the deferred-transition design and the session-scoped authority above;
any new consumer of a task's outcome must read the durable structured
result (or use the dispatch recognition), never assume the row status is
the outcome.

#### Ongoing zombie reaper (THR-090 Track B)

The daemon runs a periodic zombie reaper loop (``zombie_reaper_loop``,
registered in ``runtime/daemon/app.py``'s lifespan alongside the dream and
work-hours scheduler tasks) that sweeps ``in_progress`` tasks while the daemon
stays alive. It catches a session that silently dies mid-flight (dead process,
no completion callback) and leaves its task stranded ``in_progress``. This is
the complement to the one-shot boot sweep (§Daemon restart recovery): the boot
sweep handles restart-time recovery; the ongoing reaper handles the mid-flight
death case.

**Predicate (AND-gate, founder-approved — THR-090 seq12).** ALL of the
following must hold for a task to even be considered:

1. ``status == in_progress`` **and** ``block_kind IS NULL`` — state allowlist
   (requirement 3). Never touch a healthy ``in_progress`` (fresh heartbeat),
   nor any blocked/terminal task. Allowlist, not blocklist.
2. ``last_heartbeat`` is stale — older than ``2 × HEARTBEAT_INTERVAL_SECONDS``
   (60s, i.e. ≥2 missed heartbeat intervals).
3. ``executor_pid`` probes DEAD via ``os.kill(pid, 0)`` →
   ``ProcessLookupError``. Alive or indeterminate (``PermissionError``, ``None``
   pid) → not a zombie.

**Warm-up grace (requirement 1).** The reaper does NOT trust staleness until
the daemon has been up ≥ ``HEARTBEAT_INTERVAL_SECONDS`` (30s post-boot). This
prevents false-reaping freshly-spawned sessions whose heartbeat hasn't been
stamped yet after a boot.

**Fingerprint-tiered confidence (requirement 2).** The reaper checks for an
unconsumed ``task_result`` row from the current session via
``get_latest_task_result(task_id, agent, current_session_id)``:

| Fingerprint | Confidence | TTL after flag | Action on expiry |
|---|---|---|---|
| **Present** — task_result row found | HIGH — the agent definitely completed | None — consumed/honored immediately on the next sweep (no TTL wait). A real result is never a false-reap. | **Consume/honor** the result via ``_consume_completion_report`` (do NOT cancel). This is the Track A consume case; the ongoing reaper applies the same consumption path for mid-flight discoveries. |
| **Absent** — no task_result row | LOW — cancel-on-TTL is an inference | 5 × HEARTBEAT_INTERVAL (150s) | **Cancel** via the existing ``cancelled`` status transition. |

**Action = flag-then-cancel-on-TTL.** On first detection the reaper FLAGS
the task by persisting ``zombie_flagged_at`` (an additive ``TEXT`` column with
NULL default) and emits a ``zombie_flagged`` audit row. It does NOT cancel.
For the absent-fingerprint tier, on a later sweep, only if the task is STILL
a zombie AND ``flagged_at ≥ TTL`` (150s) ago, the reaper cancels the task.
For the present-fingerprint tier, there is no TTL wait — the result is
consumed/honored immediately on the next sweep after flagging, the flag is
cleared, and a ``zombie_cleared`` audit row is emitted.

**No auto-revisit (THR-079 ruling).** Neither the cancel path nor the
consumption path spawns an auto-revisit. The founder receives an audit row
and decides whether to re-dispatch.

**Recovery.** If a flagged task recovers before TTL expiry (heartbeat
refreshes, pid becomes alive, or a result appears), the flag is CLEARED
(``zombie_flagged_at`` set to NULL) and a ``zombie_cleared`` audit row is
emitted. No cancel occurs.

**Loss function (requirement 4).** Err toward a MISS, NEVER a false-reap.
When uncertain — indeterminate pid probe, missing heartbeat, no executor_pid —
the reaper leaves the task alone. It extends the TTL and re-flags rather than
cancelling on ambiguity.

**Schema (additive-only).** One new nullable column: ``tasks.zombie_flagged_at
TEXT`` (NULL default). No new ``TaskStatus``, ``block_kind``, or overload of
any existing column — all founder-gated. Flagged via ``zombie_flagged_at``;
cancel = the existing ``cancelled`` transition.

#### Organization portability — preflight & reconciliation (THR-187 Slice A)

**Slice A is preflight/reconciliation only.** It adds a read-only CLI
``happyranch orgs portability-preflight <slug>`` and a distinct founder-only
``happyranch orgs reconcile-portability <slug> --from-file <request.json>``.
There is **no** archive, export, import, staging, transfer fence, source
retirement, cancellation-of-live-work, or other transfer side effect in this
slice. Export/import/rebind are later, separately authorized slices.

**Exhaustive root classification.** The preflight classifies every direct
child of a source org root exactly once as `include`, a *named* `exclude`
(generated marker, derived projection, SQLite sidecar, cache, zero-byte legacy
residue, non-memory workspace data, task output), or `reject` (unknown root,
nonregular/unsafe entry, nonzero legacy-residue DB, invalid legacy skill).
The allow-list is `happyranch.db`, `org`, `artifacts`, `kb`, `threads`,
`task-attachments`, `jobs`, `dreams`, `work_hours`, `schedules`, `talks`,
conditional valid legacy `skills`, and only `workspaces/<agent>/memory/**`.
There is no fall-through/default copy: an unclassified present root rejects.

**Quiescence & zombie reporting.** Preflight refuses any pending/in_progress/
escalated task (including live, delegated, and job-parked forms), any active
session binding/PID or queued item, any pending thread invocation, pending/
running job, pending/running dream/work-hour, or any armed/firing schedule. It
*reports* possible zombies (in_progress, no block_kind, stale heartbeat, dead
pid) but never resolves them.

**Conservative schedule policy (founder, THR-187).** Preflight refuses when
**any** schedule is **armed or firing** — both are live source-readiness
hazards, so a relocation-specific disarm command or export fence is never
built. The preflight response reports only *existing* controls as the exact
actionable remedies:

* an **armed** schedule → `happyranch todos pause --org <slug> <schedule_id>`
  or `happyranch todos cancel --org <slug> <schedule_id>` (both are permitted
  by the existing schedule state machine);
* a **firing** schedule → no pause/cancel is permitted under the existing
  state machine; the correct non-mutating remedy is to wait for it to reach a
  terminal state, then re-run the preflight. No new control is added;
* live nonterminal tasks / active jobs → the existing
  `happyranch cancel <task_id> --org <slug>` and
  `happyranch jobs stop <job_id> --org <slug>` lifecycle controls;
* active sessions, queued items, pending thread invocations, dreams, and
  work-hours have no founder cancel control — the remedy is the non-mutating
  wait/resolve condition; and
* a confirmed zombie → the separately audited, founder-only
  `happyranch orgs reconcile-portability <slug> --from-file <absolute-json>`
  path.

Preflight itself is read-only: it never invokes any of these controls, never
calls reconciliation, and performs no export/import/archive action.

**Reconciliation limits.** ``reconcile-portability`` is founder/master-bearer
only (reuses the existing human-authority dependency unchanged). It names
exactly one candidate plus evidence/disposition; revalidates a true zombie
under the org DB lock; and invokes the shared result/terminalization seam
(``_consume_completion_report`` for an orphaned result, or the reaper's
``cancelled`` transition). It audits actor, SHA-256 request hash, evidence,
disposition, and before/after state under the ordinary ``task_id`` scope. A
delegated/job-blocked task is never a zombie merely because it is old.
Preflight never calls reconciliation; reconciliation offers no export
cancellation path. CLI-private only — no UI, TS client, or browser contract.

#### Transitions

```
pending → (run_step pickup) → in_progress → { completed | failed | cancelled | in_progress(delegated) | in_progress(blocked_on_job) | escalated }

in_progress(delegated) → (all children terminal) → in_progress (re-entry, block_kind cleared on claim)
in_progress(blocked_on_job) → (all blocking jobs reach terminal state; _maybe_resume_blocked_task enqueues while the row stays in_progress) → in_progress (run_step CAS admits exactly one on pickup, clearing block_kind)
escalated → (POST /resolve-escalation continue) → pending (re-enqueued; manager's next prompt carries an ESCALATION RESOLVED header with the rationale; also reachable from the thread surface via POST /threads/{id}/resolve-escalation)
escalated → (POST /resolve-escalation supersede) → superseded (mints a successor task from the provided brief; closes the predecessor as terminal; audit cites the successor root; NO re-enqueue of predecessor)
escalated | in_progress(delegated) → (revisit / thread-dispatch names it in lineage) → superseded (terminal; block_kind cleared, audit cites the continuation root task_id; NO re-enqueue. The delegated close is gated on all children being terminal and never cascade-SIGTERMs live siblings)
escalated → (POST /resolve-escalation continue on exhaustion escalation) → pending (re-enqueued; parent carries the exhaustion context + failure reason from the failed subtask — manager can re-ground and re-delegate)
escalated → (POST /cancel) → cancelled (cancel is NOT part of the resolve-escalation vocabulary; cancelling an escalated task uses the normal cancel route — parity preserved with job-cleanup + parent-notify)
(any non-terminal) → (founder cancel) → cancelled
```

#### Execution model

The orchestrator exposes exactly one primitive: `Orchestrator.run_step(task_id)`.
It picks up a task that is `pending` or `in_progress(delegated)` with all children
terminal, invokes its `assigned_agent` once, classifies the result, persists
the transition, and enqueues the next task to advance. Recursion is via queue
re-entry — no loops inside `run_step`. A task that is `in_progress` with
`block_kind IS NULL` is a *live subprocess* and is never re-admitted (admitting
it would double-spawn).

Budget: each `run_step` call increments `orchestration_step_count` persisted
on the task. When the count exceeds `max_orchestration_steps` the task parks
in `escalated` for founder review (root tasks only; a non-root over-budget task fails and hands back to its parent). A second budget — `max_revise_rounds` (org_config, 0 = disabled) — caps the number of genuine revise cycles (worker-of-record re-delegations) per slice — i.e. a slice runs its initial attempt plus up to `max_revise_rounds` revises. Each revise increments `revision_count`; when `revision_count >= max_revise_rounds` the next genuine revise trips a DELIBERATE stop-with-best (best-effort partial preserved) that mirrors the step-budget terminal: non-root fails back to parent, root escalates. The stop is explicitly NOT auto-revisited.

#### External waits

The orchestrator does not gain new task states for external systems. External waits use jobs. A task that cannot continue until an external job finishes reports `status="blocked"` with non-empty `waiting_on_job_ids`; the row parks as `in_progress` with `block_kind='blocked_on_job'`; and the existing all-terminal job predicate resumes the task. The orchestrator never infers task completion from intermediate external signals such as submission, handoff, or an absent result.

The exact persistent-verification policy is the universal runtime-materialized
``protocol/skills/jobs/SKILL.md`` contract, not reconstructed task prose. Across
TASK, THREAD, WAKE, DREAM, SCHEDULE, and BOOTSTRAP, verification expected to
exceed one minute/session safety — including ``scripts/local_ci.sh all``,
canonical/full suites, and pushes whose hooks run them — uses
``review_required:false`` + ``persistent:true``, parks through
``waiting_on_job_ids``, and verifies the exact-command/runtime/exit-0 receipt
on resume. Short/focused tests may remain synchronous.

Example: a PR CI / guarded merge helper is a bounded job that polls an external CI system and wakes the task through `blocked_on_job_ids`. The engineering-domain specifics live in the jobs skill and agent guides.

### Global host admission and backpressure (THR-207 / TASK-5584)

One daemon-wide admission controller (``runtime/orchestrator/host_supervisor.py``
``AdmissionController``) covers every top-level agent invocation across orgs,
producers, providers, and profiles. Governing spec:
``docs/superpowers/specs/2026-08-24-host-resource-concurrency.md``.

- **Admission is backpressure, not task failure.** Requests queue FIFO with
  aging (original enqueue time preserved across 429 retry re-entry); a
  request stalled by a pressure gate stays queued with a ``stall_reason`` and
  its age.
- **Queued cancellation removes the request without launch** — no lease, no
  handle, no subprocess. A 429 retry fully finishes the attempt (containment
  + receipt), releases the lease, sleeps without capacity, then re-enters
  admission with the original age and a fresh containment handle.
- **Effective cap is capability-derived, never OS-name-derived**: the minimum
  of the configured cap and binding capability caps. With enforcement
  guaranteed, the Linux `<=11` ceiling is a retained non-binding shadow input
  under the configured 13-slot producer envelope; without enforcement
  (macOS-style), the binding
  cap (4) applies — missing enforcement tightens admission.
- **Cancellation routes through the opaque containment handle**, idempotent
  with the executor's own finish; the PID remains a diagnostic only.
- **Ownership transfers atomically at admission grant**: the controller
  creates the ownership record under its lock and keeps it in its registry
  until lease release; the durable first-wins terminal reason lives on that
  record from grant; the daemon drain iterates the same registry, so a
  shutdown that fires when or immediately after admission is granted is
  durably observed by the attempt's next gate (no launch, or exactly-once
  containment before release).

**Wired producers.** Schedule fires were wired first per the founder-approved
real-caller amendment (THR-207 seq 41–44); the task-producer slice then wired
the real task path. The daemon-wide supervisor is constructed in
``runtime/daemon/state.py`` with the capability-factory-selected backend and
the configured 429 retry schedule, and the daemon drain calls
``supervisor.shutdown()`` in the app lifespan finally. The **task producer**
(``Orchestrator._run_agent``) runs every task session through the supervisor:
the invocation owns a real admission lease + cancellation token, ownership
transfers at grant, the subprocess launches through the selected capability
backend via BOTH executor Popen launch bodies (``executors._run_command`` and
``CustomAdapterExecutor.run`` receive the backend-created ``RunningHandle``),
an opaque cancellation/cleanup control is registered with ``SessionTracker``
(PID stays diagnostic/restart evidence only) and the ``/tasks/{id}/cancel``
route invokes it off the event loop, and a 429 attempt fully finishes and
releases before sleeping and reacquires with the original enqueue age and a
fresh backend handle. Every final terminal path (pre-launch/admission
failure, prepare/spawn failure, nonzero/no-callback exit, cancel, timeout,
429-final, shutdown) clears the control/PID/session after supervisor
finalization and before lease release (ownership-safe — an old attempt never
clears a newer session of the same (task, agent); PID diagnostics and opaque
cancel controls are generation-versioned by session_id — a superseded
invocation's late registration is rejected and its terminal cleanup is a
no-op, so cancellation always resolves the currently active generation's
control/PID and never invokes an old/already-terminal token). The schedule producer's
honest-passthrough branch disables the executor's internal 429 retry so the
supervisor is the single retry owner. Thread/dream/wake producers stay
structurally unchanged; later serial slices attach them to the same contract.

**Slice B backend selection (THR-207 / TASK-5637).** The daemon-wide
supervisor's backend is selected through the capability factory
(`runtime/platform/backend_factory.py`) — the single OS-name site, and even
it selects by operational probe rather than OS/version strings:

- healthy Linux systemd/cgroup-v2 probe (user manager reachable, cgroup v2
  controllers mounted, transient scope created with applied limits,
  membership/counters verified, empty-cgroup teardown, no residue) →
  `LinuxSystemdBackend` (`runtime/platform/linux_systemd.py`);
- otherwise a healthy process-group/census probe → `MacOSProcessGroupBackend`
  (`runtime/platform/macos_process_group.py`);
- anything else → the honest no-capability fallback (`PassthroughBackend`,
  every capability `unavailable` — never a fabricated guarantee or zero
  measurement).

Callers above the factory branch on **capabilities** (`enforcement_guaranteed`
and the binding-cap logic are unchanged), never on backend names. With the
executor launch bodies wired, the daemon's truthful selection is the real
Linux/macOS backend when the operational probe is healthy; the honest
no-enforcement passthrough remains the deterministic default of
``build_default_host_supervisor()`` (schedule-fire integration suites) and
the unsupported/unhealthy-environment fallback. The real backends are
exercised by
real integration suites gated on the operational probe. Guaranteed-cleanup
residue from the Linux backend blocks admission until reconciliation;
best-effort macOS survivors stay censused/charged/visible and block only on
census/measurement failure or the conservative survivor threshold.

Containment/measurement is **fail-closed**: on the Linux backend an
unreadable `cgroup.procs` or an errored `systemctl` unit-state interrogation
is UNKNOWN evidence that never yields `CLEAN`/`quiescent` (guaranteed-cleanup
lease release requires verified cgroup emptiness), and an absent counter is
`unavailable` unless a real sample backs it; on the macOS backend a
census/measurement exception at finish is explicit failure evidence that
blocks admission — never an empty clean group. Retained samples and
serialized sampling gaps are cardinality-bounded (truncated-prefix span
preserved truthfully).

**Admission/backpressure observability (THR-207 observability slice).** The
bounded `host_sessions` block on `GET /api/v1/metrics` (and a bounded,
non-sensitive subset on the unauthenticated `GET /api/v1/health` when the
supervisor is wired) exposes the live admission state: effective cap, active
leases, queue depth, oldest wait, head stall reason, shutdown state, and the
cumulative admitted/released/cancelled-while-queued counters — so
backpressure (queue depth, stall reasons, cap tightness) is observable from
the existing operator surfaces without any schema change. Receipt aggregates
(by terminal reason / cleanup status, per-provenance peaks, cleanup
duration, residue counts) and the residue admission gate/census ride the
same block; publication failures are contained at the supervisor seam.

**Slice C bounded enforcement + receipt attribution (THR-207).** Real
supervised sessions apply the founder-approved **fixed initial Linux
enforcement policy** (`runtime/platform/enforcement_policy.py`) — an
immutable per-invocation envelope selected deterministically from the
existing `AdmissionRequest.invocation_kind` and applied **only** by the
healthy Linux systemd/cgroup-v2 capability backend.

Daemon startup snapshots `queue_workers=6` and
`host_global_session_cap=13`, exposing the complete healthy 13-slot producer
envelope (6 task + 4 thread + dream + wake + schedule). Both values require a
restart; reload does not resize live workers or admission. Capability-derived
fallbacks remain conservative (macOS/no-enforcement effective cap 4).

Task sessions use `MemoryHigh=14G` / `MemoryMax=24G` / `TasksMax=1024`;
thread/dream/wake/
schedule (and any unknown kind, conservatively) `MemoryHigh=2G` /
`MemoryMax=4G` (exactly) / `TasksMax=1024`; **no `CPUQuota`** for real
sessions (probe values stay probe-only). Exact byte properties are emitted
at launch and **verified as applied** in the scope's cgroup (fail-closed on
mismatch); selection is immutable across 429 retry/reacquire. macOS stays
honestly capped/best-effort; passthrough/unsupported/degraded backends stay
explicit about unavailable enforcement. Receipts carry bounded attribution
(`invocation_kind` + redacted `executor_profile`) aggregated by the fixed
canonical invocation-kind vocabulary (unknown kinds fold into one `other`
bucket — no dynamic attribution keys); per-receipt attribution reaches only
the authed `/metrics` recent window, never the unauthenticated `/health`
(per-receipt detail stays dropped there).

### Timeout handling

Blocked tasks don't wait forever:

| Block type | Default timeout | On timeout |
|---|---|---|
| Waiting for founder approval | 24 hours | Re-notify founder + flag in dashboard as urgent |
| Blocked on dependency (same team) | 2 hours | Escalate to manager agent |
| Blocked on dependency (cross-team) | 4 hours | Escalate to both managers |
| Waiting for guidance (novel situation) | 24 hours | Re-notify founder + agent proceeds with conservative default if one exists |

The orchestrator runs a background check every 15 minutes for timed-out tasks. If a founder approval times out twice (48 hours total), the task is flagged as critical on the dashboard through whatever channel the founder has configured.

### Permission evolution

Permissions aren't static. As the system matures:
- Novel situations that the founder resolves become codified rules — the orchestrator updates the permission config and knowledge base so that situation is handled automatically next time
- The founder can adjust any agent's permissions at any time via the dashboard

### Reviewer/QA verdict discipline

Review and QA leg tasks MUST complete their leg with a verdict
(APPROVE / REVISE / PASS / FAIL) and MUST NOT self-block. A completion report
with `status=blocked` and an EMPTY `waiting_on_job_ids` is a MALFORMED report
— the leg is treated as FAILED, and the parent wakes for a manager decision
step (not cascade-failed). Self-blocked reviews that omit a verdict waste the
delegation and burn a re-spawn round.

Reviewer identities are configured per-org in the DB-backed `reviewer_agents`
setting (default `["code_reviewer"]`, THR-175) — never hardcoded in the
transition logic. A configured reviewer leg MUST declare
`expect_verdict: "APPROVE"`; omission is a HARD REJECT at authoring — the
delegation is denied before any child spawns and the owner receives a feedback
orchestration step naming the required `expect_verdict: "APPROVE"`, with the
root back to PENDING and re-enqueued for a corrected decision (never a root
failure). A delegate with a missing agent name or missing workspace is NOT
recoverable and keeps hard terminal failure. At the
execution seam a configured reviewer leg with a downstream leg only
auto-advances on an explicit `APPROVE` — a missing verdict or any non-approve
verdict (`REQUEST_CHANGES` / `REVISE` / `BLOCK` / equivalent) clears the chain
and wakes the manager, so QA/downstream is never spawned after a failed review.

---

### Agent Todos: internal Schedule fire mechanism (THR-105)

Agent Todos use the internal ``schedules`` SQLite table (not the cron-like
scheduled tasks described in ``05b-agent-runtime.md`` Mode 2). Every Schedule
row represents one agent-owned recurring or one-shot work item.

**Schedule lifecycle.** Schedules are created via the schedule service
(``runtime/orchestrator/schedule_service.py``), which validates the one-shot,
legacy weekly, and bounded recurring envelopes (including the one-shot 90-day
horizon, recurring grammar, and agent/org caps). A new Schedule enters in
ARMED status with a computed ``fire_at``. The service does NOT enqueue or
execute anything — it is a pure lifecycle-management surface.

**Arming (creating) schedules.** Agents create schedules autonomously via
the ``POST /schedules`` callback route (``runtime/daemon/routes/schedules.py``).
The CLI form is:

  happyranch schedules create --org <slug> --from-file <path>

The payload carries ``task_id``, ``session_id``, ``agent`` (self-target
binding), ``source_instruction``, ``normalized_brief``, ``kind``, ``fire_at``,
and optionally ``recurrence`` and ``timezone``.  Server-side enforcement:

- **Self-target:** ``task_id`` + ``session_id`` + ``agent`` are validated
  against the in-memory SessionTracker; the payload cannot pick another agent.
- **In-org availability:** every agent with a valid active session and a
  resolvable in-org team may create a self-owned Todo. Legacy
  ``scheduling.enabled_agents`` config is accepted as a no-op and does not
  authorize or deny creation.
- **Mandatory normalization:** both ``source_instruction`` and
  ``normalized_brief`` must be non-blank; NL-only arming is refused.
- **Envelope validation** (one-shot horizon, weekly shape, caps, expiry
  default, and the recurring RRULE-subset grammar) is enforced by
  ``ScheduleService.create()``. A recurring callback uses only the documented
  ``freq``/``interval``/selector/``time``/``tz``/end-condition shape; its
  ``anchor_date`` is server-owned, and unsupported instructions must be
  clarified rather than approximated.

Arming emits a ``schedule_created`` audit row with
``task_id=<SCHEDULE-NNN>``.

**Scheduler loop (Phase 3).** A 60-second daemon loop
(``schedule_scheduler_loop`` in ``runtime/daemon/schedule_scheduler.py``)
scans every org for due ARMED rows. One-shots are due at ``fire_at <= now``;
weekly and bounded recurring rows are claimed only when their due instant is
within the 120-second recurrence tolerance. For a stale weekly or recurring
occurrence (missed during daemon downtime), the scheduler does not replay or
backfill it. It records ``occurrence_missed`` and re-arms the row only when a
future next occurrence exists within any finite review expiry. The terminal
alternatives do not emit ``occurrence_missed``: a recurring rule exhausted by
its inclusive ``until`` date becomes FIRED with ``end_reason=date_ended`` and
``schedule_fired``; a future candidate beyond the review expiry becomes
EXPIRED with ``schedule_expired``; and an otherwise unexplained missing
candidate becomes FAILED with ``error=recurrence_no_candidate`` and
``schedule_failed``. Eligible rows are claimed: ARMED → FIRING, then enqueued
as a ``ScheduleJob`` into the org's ``ScheduleQueue``.

**Runner + worker loop.** A dedicated ``schedule_worker_loop`` drains the
``ScheduleQueue`` and invokes ``run_schedule`` (``schedule_runner.py``) for
each job. The runner transitions FIRING → RUNNING, composes the schedule-fire
prompt via ``build_schedule_prompt``, and invokes the owning agent's executor
in its workspace. The fire prompt instructs the agent to call exactly one
callback:

```bash
happyranch schedules spawn --org <slug> --schedule-id SCHEDULE-NNN --from-file <path>
```

**Spawn callback.** The ``/schedules/{id}/spawn`` route
(``runtime/daemon/routes/schedules.py``) is the single-use, record-scoped
fire endpoint:

- Accepts only FIRING rows (409 on any other status).
- Creates exactly one root task from the stored ``normalized_brief``, targeted
  to the owning agent on its own team (self-targeted).
- Records ``spawned_task_ids`` and increments ``fire_count``.
- Resolves terminal state:
  - **One-shot** → FIRED (terminal, ``active=0``).
  - **Weekly** → re-armed (ARMED, ``active=1``) with the next ``fire_at``
    computed via ``next_weekly_occurrence``, OR expired (EXPIRED, ``active=0``)
    when the next occurrence exceeds ``expires_at`` and ``indefinite=0``.
  - **Recurring** → after a successful dispatch, reaches FIRED with
    ``end_reason=count_exhausted`` when its successful-dispatch ``count`` is
    reached; otherwise it reaches FIRED with ``end_reason=date_ended`` when
    its inclusive ``until`` is exhausted, EXPIRED when a real next occurrence
    is beyond review expiry, or re-arms with the next recurring ``fire_at``.
- Writes ``schedule_spawned``, ``schedule_completed``, and (when applicable)
  ``schedule_expired`` audit log rows.
- Enqueues the spawned task via ``enqueue_task``.
- Writes a schedule transcript under ``<org_root>/schedules/SCHEDULE-NNN.md``.

**Token usage.** Token usage for the schedule-fire executor session is stored
in ``session_token_usage`` with ``scope_type="schedule"`` and
``scope_id=<schedule_id>``.

**Runner resolution.** After the executor returns, ``run_schedule`` checks the
row's updated status. If the spawn callback drove it to FIRED (natural completion),
ARMED (weekly or recurring re-arm), or EXPIRED (review-expiry terminal), the
runner exits — the callback already handled resolution. If a weekly or
recurring session returns without spawning, fails, or times out, the runner
records the failure/timeout and advances to the next eligible occurrence
without retrying the claimed instant or incrementing ``fire_count``; it
re-arms unless the same ``until``/review-expiry/no-candidate terminal rules
apply. One-shots retain terminal FAILED/TIMEOUT behavior for those cases.

**No hidden schedules.** Every Schedule is visible to the CLI ``list`` command
and to the owning agent in the schedule-fire prompt. There is no mechanism for
hidden or silent schedules.

**No cross-agent scheduling.** Every Schedule targets a single agent on its
own team. The spawn endpoint resolves the agent's team and creates the root
task on that team — cross-team and cross-agent scheduling are not supported.

**Distinct from cron-like scheduled tasks.** The Mode 2 cron-like scheduled
tasks (documented in ``05b-agent-runtime.md``) are a separate mechanism using
a different table and different triggers. Agent Todos are agent-owned,
agent-driven Schedule records with a dedicated scheduler/runner/spawn-callback
pipeline. The two systems coexist and do not share data or scheduling
infrastructure.

**Daemon-managed workspace cleanup scheduler (THR-195 seq 129).** Workspace
cleanup is a **daemon-managed, system-default capability** that runs on its
own without user configuration and is fully independent of user Schedules
(founder ruling THR-195 seq 129; manager resolution seq 130; implementation
direction seq 131; per-agent defaults TASK-6036). The daemon registers a
periodic loop (``runtime/daemon/workspace_cleanup_scheduler.py`` — the sixth
daemon-owned loop alongside dream/schedule/zombie/direct-connect, one
registration in ``runtime/daemon/app.py``) that, per agent workspace:

The advisory's fail-open `statvfs` inode fields report used/free/total/percent
and threshold state. They are explicitly non-authoritative: they never grant
cleanup authority or cause this loop to inspect, move, quarantine, restore,
unlink, or recursively delete `/tmp` content. The first producer-redirection
unit puts executor language/package caches below each agent workspace's
``.happyranch/cache`` and requires disposable review/QA/scratch worktrees below
``.happyranch/scratch/worktrees``. Those paths are workspace-owned and measurable,
but ``.happyranch/cache`` is not cleanup-eligible and this unit
grants no new cleanup authority. Cache setup refuses symlinked child components
and fails closed without falling back to ``/tmp``; same-UID post-validation
races remain because the workspace is not an OS isolation boundary.
Task-agent and job launches prepare a canonical per-task mode-0700
``.happyranch/task-tmp/TASK-N`` root, inject it through
``TMPDIR``/``TMP``/``TEMP``, and atomically append bounded
required-versus-observed producer evidence to a separate v1 manifest under
``.happyranch/task-scratch-manifests``. Every executor branch, including
generic and registered custom adapters, consumes the shared environment seam;
the job runner applies the same contract before subprocess creation.
Containment-variable/sidecar drift fails closed through existing task/job
error and audit handling while unrelated environment survives.

The manifest reader is report-only: missing, corrupt, and stale observations
are evidence, not repair or cleanup triggers. No teardown or periodic cleanup
path imports or consumes this manifest, no deletion eligibility changes, and
no file removal is activated. Later deletion remains separately gated on the
existing THR-090 zombie-reaper lifecycle evidence plus immediate fail-closed
OS process/cwd/open-file checks; Git/repository evidence remains a skip rule.
Small daemon callback payload contracts are unchanged.

THR-195 B1 also defines a dormant `task_scratch_reclamation` engine imported
only by tests. Its executor accepts immutable finalized ledger rows rather than
manifest paths or candidate lists, and performs literal-root fd-relative
no-follow same-device removal with exact allocated-byte/inode accounting and
protected parent/manifest/lock/sibling postconditions. Sealing requires explicit
bounded typed lifecycle/liveness/current-boot coverage evidence that rejects
missing, stale-boot, truncated, ambiguous, unsupported-platform, recovery/job/live-
reference, or unavailable authority and the 60-second newest-mtime floor. Final
removal is bound to verified parent/root identity and complete sibling/directory-
entry postconditions; Git/worktree/bare-repository ancestor or descendant evidence
fails closed. B1 adds no teardown/scheduler caller, activation, deployment,
live deletion, or legacy-backlog eligibility.

1. **Measures** the OWNING AGENT's workspace with an explicit bounded,
   fail-open budget: one true wall-clock deadline shared across all
   collection — each git subprocess receives ``min(per-call cap, remaining
   deadline)`` and expiry is re-checked after every subprocess and after the
   last repository; workspace/repo/worktree cardinality caps propagate
   truncation. Every timeout, error, or cap hit yields an explicit
   ``measurement unavailable`` advisory status and can never block daemon
   operation or task/session spawning.
2. **Decides** on the weekly occurrence (Sunday 03:30 in the org's effective
   timezone; TASK-5552 §6). The live scheduler evaluates an occurrence once
   when its scan cursor crosses that boundary, so polling phase and bounded
   processing drift cannot skip it. On startup it evaluates only the current
   weekly window once; there is no earlier historical backfill across daemon
   lifetimes. It preserves one run at a time (a later
   occurrence fires only after the preceding cleanup task of that agent is
   terminal — TASK-5552 §3), a seven-day per-agent cooldown, and the
   1 GiB founder threshold. A bounded timeout, error, or cap/truncation result
   that makes measurement unavailable bypasses only numeric threshold
   evaluation, so otherwise-due spawning continues with honest unavailable
   advisory context; only an available numeric result below 1 GiB skips.
   Below-threshold state is audited once at that meaningful
   weekly/cooldown boundary, not once per minute for the rest of the week;
   measurement-unavailable fails open around the numeric threshold gate, so an
   otherwise-eligible task spawns with unavailable advisory and trigger-audit
   context; an available numeric below-threshold result skips and remains
   audited. Other exceptional/fail-closed trigger skips remain explicitly
   audited. A decision-level
   task-history lookup failure before trigger entry creates no cleanup task and
   emits exactly one ``workspace_cleanup_skipped(history_indeterminate)`` row
   for the crossed boundary; adjacent non-boundary scans remain silent.
3. **Triggers** an ordinary root task ASSIGNED TO THE OWNING AGENT
   (``insert_task`` + ``enqueue_task`` — the same pattern the Schedule spawn
   callback uses, minus the Schedule) with a **daemon-composed brief** that
   carries the fresh advisory snapshot at trigger time. The brief is never a
   Schedule brief: no Schedule is looked up, created, or modified, and
   nothing is persisted in any Schedule field. The first TWO triggered runs
   per agent are STRICTLY report-only (TASK-5552 §6 rollout); from run #3
   the brief is the approved TASK-5552 §4 fixed normalized cleanup contract
   (bounded, Git-aware, non-force, action-time-re-derived eligibility).

The packed advisory block is sizing context ONLY: **stale on arrival**, **not
an eligibility list**, **not a candidate list**, labels no path safe,
recommends no removal, contains only aggregate counts/sizes/status for the
owning agent's workspace, never enumerates paths, and never uses pending
jobs or ``blocked_on_job_ids`` as liveness. Every path and fact must be
re-derived independently and immediately before any action.

The responsible agent reports results to the founder in **one durable
founder-visible thread PER AGENT** (consultant seq 131: "one durable thread,
not one per run" — per-agent per the founder default). The daemon resolves
the thread by AUTHORITATIVE IDENTITY, not by the fixed per-agent subject
alone: a thread is the agent's durable report thread only when the subject
matches AND its daemon-cleanup provenance resolves (``composed_from_task_id``
→ one of the agent's daemon-marked cleanup tasks, queried by the existing
``threads.composed_from_task_id`` index — no presentation-page bound can
hide an older thread) AND the owning agent is a participant (the
participant-authorized send requires membership) AND it is open AND its
opening message carries the daemon's distinctive composition text (a
user-created subject collision is never selected). The identity lookup is
**tri-state**: an authoritative absence is the ONLY state that may create a
thread; a lookup error is ``indeterminate`` and FAILS CLOSED — no duplicate
thread, no task, no enqueue — with an audited reason (TASK-6046). It creates
the thread on first trigger ATOMICALLY WITH THE CLEANUP TASK (see the
producer contract below; composer = the owning agent, recipient @founder —
the owning agent is therefore a participant) and passes only the thread id
in the brief. NO
minted report token: the agent appends the report during its task session
via the existing participant-authorized, task-bound ``happyranch threads
send`` path (composer + task_id + session_id binding), which requires
participant membership and a live task-session binding but no invocation
token. Silence on that thread is the loop-stopped signal. Report content:
measured before/after sizes, exact removals (none in report-only), skips,
and any ambiguity (seq 130).

Persistence is schema-free by design: the first-two run counter and the
seven-day cooldown derive from the owning agent's daemon-marked cleanup task
rows via an authoritative SQL-side ``brief`` prefix filter
(``Database.list_tasks_by_brief_prefix`` — no bounded scan of ordinary tasks
can hide older cleanup rows, and a lookup failure is represented as
indeterminate so triggering FAILS CLOSED; a dedup-blind daemon can never
double-fire, reset the first-two counter, or bypass the cooldown). The
per-agent thread identity resolves as described above. No
schema/API/CLI/auth/permission change is introduced. A kill switch —
``workspace_cleanup.enabled`` in the org ``config.yaml`` (default true) —
disables the capability per org; it is an existing daemon/org config
mechanism with no new public API/CLI/UI surface.

Trigger task production is **rollback-safe and atomic** (TASK-6046): the
task id is allocated only after every awaited step (the bounded measurement
is done first), and allocation + tri-state thread-identity resolution +
brief composition + insertion run as one synchronous block under
``org.db_lock`` with no awaits between ``next_task_id`` and the insert — no
id is ever selected before awaited work, so another producer cannot claim it
mid-trigger and no collision can leave a report thread falsely linked to an
unrelated task. On an agent's FIRST trigger the report thread (row,
participant, opening message, turn accounting, and the
``thread_started``/``thread_message_sent`` audit rows) and the cleanup task
are written in ONE transaction
(``Database.insert_cleanup_report_thread_and_task`` — the existing
BEGIN IMMEDIATE/COMMIT/rollback compound pattern); on ANY failure every
durable row rolls back — ZERO residue across threads/participants/messages/
turns/audits/tasks — nothing is enqueued, and a later retry succeeds exactly
once. When the thread already exists only the task is inserted, so an insert
failure can never leave thread residue either. The inserted task is a clean
root (no parent, no thread dispatch); every compensation path is audited
(``workspace-cleanup:skipped`` with the exact reason). Workspace enumeration
is paged in bounded batches of 64 per call across every registered agent
workspace, so an org with more than one batch of agents never alphabetically
starves the later ones.

---

## 4. Runtime-Managed Skill Policy (CONTEXT/ADMISSION)

The runtime-managed skill policy is an agent **context/admission** mechanism
— it controls which skills appear in an agent session's compact skill index.
It is **explicitly NOT a permission layer**. Capability remains governed
ONLY by the existing permission model (§3). Skills do not grant tools,
credentials, network access, filesystem access, sandbox policy, or
permission-map/allow-rule/auth changes.

**Founder ruling (THR-055 seq 55):** The catalog-approval gate is REMOVED for
first-party HappyRanch skills. For first-party skills, runtime approval
duplicates the release pipeline — PR review + merge + deploy IS the approval.
Exposure is now: catalog-presence + status==enabled + eligibility-matched.
Runtime approval is DEFERRED to a future user-authored-skills feature and will
be re-introduced only if/when that audience ships.

### 4.1 Two-Gate Model

A skill reaches an agent session only when **both** gates pass:

1. **Catalog Gate** — the skill is present in the catalog and enabled.
   - `status` must be `enabled`.
   - Disabled skills are blocked.
   - There is NO approval gate — for first-party skills, the release pipeline
     (PR review + merge + deploy) IS the approval.

2. **Eligibility Gate** — org/team/agent policy makes the skill eligible.
   - Additive inheritance with explicit deny (`deny` wins over `allow`):
     ```
     effective = present_catalog
       ∩ (org.allow ∪ team.allow ∪ agent.allow)
       \ (org.deny ∪ team.deny ∪ agent.deny)
     ```
   - A disabled registry entry remains unavailable even if eligible.
   - Unknown skill ids in eligibility config produce validation warnings and
     are excluded from the session index.

### 4.2 Policy Classes

| Policy class | Governance |
| --- | --- |
| `standard_operational` | Workflow guidance, repo conventions, role playbooks, debugging aids (e.g., `reflection`). Passes the catalog gate with status=enabled. |
| `high_impact_policy` | Pricing, legal/compliance, security, production release, escalation thresholds, agent roster governance (e.g., ``manage-agent``, ``manage-repo``). Scoped to managers/operators via eligibility policy (`policy_class` still scopes eligibility). Passes the catalog gate with status=enabled (no per-version approval gate — release pipeline IS the approval). |
| `system_contract` | Runtime protocol and mandatory operating-contract skills (e.g., `start-task`, `thread`, `jobs`). **Outside the toggleable catalog** — not shown, not toggleable. |

### 4.3 Compact Session Skill INDEX

At session creation, HappyRanch injects a compact skill **index** into the
agent prompt — not full skill bodies. Each index line carries: `id`, `version`,
`description`, `when_to_use`, and `source` (the on-disk path to `SKILL.md`).
The agent loads the full skill body on demand through the executor's normal
skill-loading mechanism.

Format:
```
- hr:<slug>@<version> — <description>. <when_to_use> Load full instructions from <source>/SKILL.md.
```

The compact index is stable and deterministic for the same registry + config
inputs. Skills omitted by policy do not appear. Global CLI skills are untouched.

### 4.4 Admin Surface (CLI-first)

V1 provides CLI commands for release-managed catalog diagnostics from the
file/YAML-backed registry + resolver + exposure. `skills effective` also reads
the existing authenticated daemon effective-skills projection when available,
because B2 custom-skill eligibility and materialization evidence are
database-backed and must not be re-resolved by the CLI:

- `happyranch skills catalog list` — list all registered skills.
- `happyranch skills catalog validate` — validate registry entries and
  eligibility policy; surfaces unknown-id warnings and malformed skill.yaml
  entries.
- `happyranch skills effective --agent <name>` — show release-managed skills
  from local files and B2 custom skills from the daemon projection. Custom
  output distinguishes hidden/no-policy, visible-next-session, and successful
  current-version materialization with rule provenance, version, and hash.
  JSON reports custom-projection availability; text labels an unavailable
  daemon and retains the local managed-catalog output. `--offline` explicitly
  selects that managed-catalog-only fallback.
- `happyranch skills policy explain <skill_id> --agent <name>` — explain why
  a release-managed skill is or isn't available, including both gate results
  and eligibility provenance; it remains catalog-only rather than inventing a
  second B2 custom-policy resolver.

### 4.6 Session-Time Skill Freshness & Protocol Doc Injection (THR-070)

**Skill body freshness.** System/contract skill bodies are copied from the
bundled ``project_root/protocol/skills/`` into the agent workspace at
`ensure_workspace_ready` time (lifecycle events like init-agent,
set-executor). Before THR-070, live agents' on-disk skill bodies froze until
the next lifecycle event — an edit to a skill in the bundle would not reach a
running agent.

**Phase-4 cutover (THR-055, COMPLETED).** The session-time wholesale refresh
(``refresh_session_skills``) and the bootstrap ``_copy_skills`` wholesale copy
in the three executor adapters (``ClaudeWorkspaceAdapter``,
``CodexWorkspaceAdapter``, ``OpencodeWorkspaceAdapter``) are PERMANENTLY REMOVED
as executable paths. The former ``_WHOLESALE_DUMP_ENABLED`` toggle flag is
also removed from source — it cannot be set, re-enabled, or used for
rollback. The canonical skill store + workspace symlink architecture
is the sole delivery path. The explicit injection paths —
``inject_system_contracts`` (§4.7) and ``inject_managed_skills`` (§4.10) —
deliver skills exclusively through the canonical store, never through a
copy path.

**Protocol doc manifest.** Protocol ``.md`` docs (the files in
``project_root/protocol/*.md``) are NEVER copied to agent workspaces. Instead,
a minimal one-line-per-doc **manifest** is injected into every session prompt
alongside the compact skill index. Each line carries the doc title, a one-line
purpose, and the absolute bundled path. Agents read full doc bodies on-demand
from the bundled path — no new tool, no new daemon route, no injection of full
bodies.

This replaces the legacy model where agents read protocol docs from the
``repos/happyranch/protocol/`` clone (which was fresh only at
once-per-session git-pull).

**Session-path coverage.** The six materialization callers that inject the
manifest and refresh skills are:
1. ``Orchestrator._run_agent`` (task/subtask) — ``TASK`` context
2. ``wake_runner.run_wake`` (working-hours wake) — ``WAKE`` context
3. ``thread_runner.run_invocation`` (thread reply/bootstrap) — ``THREAD`` context
4. ``dream_runner.run_dream`` (private dream) — ``DREAM`` context
5. ``schedule_runner.run_schedule_fire`` (schedule fire) — ``SCHEDULE`` context

Additionally, the **executor-switch/bootstrap** path (set-executor route in
``runtime/daemon/routes/agents.py``) materializes a single full-expected-spec
union from all six session contexts (task, thread, wake, dream, schedule,
bootstrap) before switch/launch. The legacy ``_copy_skills`` copy is permanently
removed; no ``_WHOLESALE_DUMP_ENABLED`` flag remains.

**Process-local workspace serialization (Issue #536).** All pre-spawn skill
materialization for a given agent workspace — system-contract injection +
on-disk verification, managed-skill injection, and B2 custom-skill injection
— runs inside a single unified transaction (``materialize_workspace_skills``)
protected by a process-local ``threading.RLock`` keyed by the canonical
(resolved) workspace path. The legacy wholesale copy and its former
``_WHOLESALE_DUMP_ENABLED`` flag are permanently removed. Concurrent task,
thread, wake, dream, schedule, and bootstrap callers targeting the same
workspace serialize their complete pre-spawn materialization. The lock is
**process-local only** — it does not coordinate across daemon processes.
Cross-process protection for the same agent workspace relies on the daemon's
per-agent concurrency ceiling.

Per-file ``os.replace`` reader safety is preserved: a concurrent reader always
sees either the complete old or complete new skill file, never a half-written
one. The lock serializes writers only; it does NOT block readers.

Named fail-closed behavior: a materialization failure produces a named
actionable error (``SystemContractMaterializationError``,
``PermissionError``, or ``OSError``) — never
a bare ``FileNotFoundError``. The caller persists the terminal failure and no
agent subprocess is launched.

**Hard constraints.** Skill refresh and manifest injection are additive only —
they do not modify ``resolve_managed_skills_index``, ``render_compact_skill_index``,
the permission model, executor skill-load paths, or the SQLite schema. No new
daemon routes are added.

### 4.7 System-Contract Injection (THR-055 Phase 1 + Phase 4)

System-contract skills — ``start-task``, ``jobs``, ``make-worktree``, ``thread``,
``dream``, ``todos`` — are mandatory operating-contract skills injected by the runtime based
on session/context type. They are defined in the single-source-of-truth module
``runtime/skills/system_contracts.py`` and are OUTSIDE the toggleable managed
catalog (they are NOT displayed by ``skills catalog list`` and are never
manager-toggleable).

**Injection model (Phase 4 — CUT OVER / REMOVED).** The wholesale
``protocol/skills/`` dump and its former ``_WHOLESALE_DUMP_ENABLED`` gate
are permanently removed from source. The explicit injection paths —
``inject_system_contracts`` (§4.7) and ``inject_managed_skills`` (§4.10) —
route through the canonical skill store + workspace symlink architecture.
All skill delivery now goes through ``materialize_workspace_skills``, which
creates validated relative symlinks to hash-addressed canonical packages
under BOTH ``.claude/skills`` and ``.agents/skills``. The executor and daemon share the same OS identity; integrity is enforced
by synchronous
pre-launch hash detection against ledger-declared member hashes.

**Phase 1 (historical).** The initial deployment ran ``inject_system_contracts``
ADDITIVELY alongside the wholesale dump. This was the safety net proved correct
in the guard test, then removed in Phase 4.

**Context-exposure predicates** (``SessionContext`` enum — 6 contexts):

| Contract | TASK | THREAD | WAKE | DREAM | SCHEDULE | BOOTSTRAP | Requires repos? |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| ``start-task`` | ✓ | | ✓ | | ✓ | | no |
| ``jobs`` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | no |
| ``make-worktree`` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | yes |
| ``thread`` | ✓ | ✓ | ✓ | | ✓ | ✓ | no |
| ``dream`` | | | | ✓ | | | no |
| ``todos`` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | no |

**Session-context mapping:**
- ``TASK`` — ``Orchestrator._run_agent`` (ordinary task/subtask session)
- ``THREAD`` — ``thread_runner.run_invocation`` (thread reply/bootstrap)
- ``WAKE`` — ``wake_runner.run_wake`` (working-hours wake / task-followup)
- ``DREAM`` — ``dream_runner.run_dream`` (scheduled dream)
- ``SCHEDULE`` — ``schedule_runner.run_schedule_fire`` (schedule fire)
- ``BOOTSTRAP`` — executor-switch / set-executor lifecycle event

**Repo-capability check.** ``make-worktree`` is gated on the agent workspace
having at least one cloned git repository under ``repos/``. Agents with no
repo write surface never receive ``make-worktree``.

**Worktree-root guard (THR-117).** The ``make-worktree`` skill delivers a
stdlib-only worktree guard script (``worktree_guard.py``) alongside its
``SKILL.md`` via the system-contract injection path. The guard runs at
setup and at verify points (before test, commit, and report).

At setup it validates repository/worktree identity — the worktree and
primary must share the same git common directory and the worktree must be
registered under the primary — and rejects unrelated repo pairings with
diagnostics naming both resolved roots and corrective action. It records
canonical absolute primary and task worktree roots, captures a robust
primary checkout baseline (content hashes for dirty tracked, staged, and
untracked files — not just path membership), and exports ``WORKTREE_ROOT``
for all subsequent repo commands.

At verify it detects new changes in the primary checkout — including
mutations of already-dirty tracked paths, new or mutated staged files,
and new or mutated untracked files (using content-hash comparison, not
mere path-set membership) — and fails loudly, naming both canonical roots
and every changed primary-checkout path categorized as tracked, staged, or
untracked. The failure diagnostic offers preservation-first recovery
instructions (``git diff`` inspection, ``patch``-based save for tracked and
staged changes, ``tar`` archive for untracked content) with all paths
shell-quoted via ``shlex.quote()`` when they appear in generated shell
commands, and never suggests destructive commands (``git checkout``,
``git reset``, ``rm``). Changes in the task worktree are never falsely
accused; an empty worktree diff is never the sole criterion.

The delivered skill locates the guard by computing the workspace root as
five parents above the task worktree
(``<workspace>/repos/<repo>/.claude/worktrees/<task_id>`` → workspace root =
``$WORKTREE_ROOT/../../../../..``), then checking both ``.claude/skills/``
and ``.agents/skills/`` for the injected script. If neither destination
holds the guard, the skill fails loudly with a non-destructive exit code.

The guard catches the recurring agent bug where absolute
``repos/<repo>/...`` paths land edits in the primary checkout while
tests/build run in the isolated worktree. It is a narrow, non-permission
tool — no DB, API, schema, audit, auth, notification, or sandbox
involvement.

**Debug visibility.** ``happyranch skills effective --agent <name>`` displays
a distinct "System Contracts (runtime-injected)" section separate from managed
catalog skills, plus a distinct daemon-backed custom-skills section when the
projection is available. The optional ``--context`` flag filters the display by
session context; ``--workspace`` enables the repo check. B2 eligibility changes
guidance visibility only, never permissions; a session-effect claim requires a
successful materialization of the current version/hash.

**Fences.** System-contract injection does not:
- Grant tools, credentials, or capabilities (skills are permission-inert)
- Modify the managed catalog, registry, or eligibility resolver
- Require a SQLite migration (file/YAML-backed only)
- Add new daemon routes
- Change the existing permission model

### 4.8 Managed-Catalog Standard-Operational Entry — ``reflection`` (THR-055 Phase 2)

The ``reflection`` skill (the operational self-reflection workflow; named
``review`` until the THR-106 rename) is the first HappyRanch skill migrated
into the managed catalog as a ``standard_operational`` entry. It was
previously delivered via the wholesale ``protocol/skills/`` dump alongside
the system contracts.

**Package location.** ``runtime/skills/reflection/{skill.yaml,SKILL.md}``.

**Registration metadata.**
- ``id``: ``hr:reflection``
- ``policy_class``: ``standard_operational``
- ``owner``: ``engineering_manager``
- ``version``: ``1.0.0``

**Eligibility scoping.** ``reflection`` is **org-wide** (universal) — ALL agents
receive it through the durable managed-skill policy. The default eligibility
policy in ``org/config.yaml`` grants access via ``skills.org.allow:
[hr:reflection]``, making it available to every agent in the org regardless of
team or role.

The eligibility formula is the standard additive-inheritance model (see §4.1):
org-scoped allow grants ``reflection`` to all agents. Per-team or per-agent
denies would take precedence (deny wins over allow) but the shipped default is
universal.

**Provenance.** ``skills effective --agent dev_agent`` shows ``reflection`` with
``org(org) ALLOW`` eligibility provenance and ``standard_operational``
policy class. ``skills policy explain hr:reflection --agent dev_agent`` shows the
catalog gate (PASS — present, enabled) and eligibility gate (org-scoped allow).

**Rename migration (THR-106).** Skill eligibility policy is persisted ONLY in
each deployed org's ``org/config.yaml`` — there is no database storage for it.
A one-shot daemon-startup migration (``migrate_hr_review_skill_id``) rewrites
``hr:review`` → ``hr:reflection`` inside the persisted skills section (allow
AND deny lists, at org/team/agent scope), scoped to the ``skills:`` block so
unrelated config survives byte-for-byte. It is gated by a durable
``.hr_review_renamed`` sentinel in the org root (mirroring the
``.agent_yaml_consumed`` one-shot pattern) so it never re-runs.

**Delivery model.** ``reflection`` is delivered exclusively through the managed-skill
policy injection path (``inject_managed_skills``, see §4.10) — the legacy wholesale
``protocol/skills/`` dump is permanently removed (the former
``_WHOLESALE_DUMP_ENABLED`` flag is absent from source). The ``SKILL.md`` body
lives in ``runtime/skills/reflection/`` (the managed catalog). Universal delivery is
proven by the contract-completeness guard test (``test_skill_cutover_completeness.py``).

**Fences.** ``reflection`` does not:
- Grant tools, credentials, or capabilities (skill content is permission-inert)
- Require a SQLite migration (file/YAML-backed only)
- Add new daemon routes
- Change the existing permission model or auth

### 4.9 Managed-Catalog High-Impact Entries — ``manage-agent`` + ``manage-repo`` (THR-055 Phase 3)

The ``manage-agent`` and ``manage-repo`` skills are registered as
``high_impact_policy`` managed-catalog entries. They govern agent roster
management (enroll/update/terminate agents) and agent workspace repository
configuration (add/remove/update repos).

**Package locations.**
- ``runtime/skills/manage-agent/{skill.yaml,SKILL.md}``
- ``runtime/skills/manage-repo/{skill.yaml,SKILL.md}``

**Registration metadata.**
- ``hr:manage-agent`` and ``hr:manage-repo``
- ``policy_class``: ``high_impact_policy``
- ``owner``: ``engineering_manager``
- ``version``: ``1.0.0``

**Eligibility-scoped exposure.** ``manage-agent`` and ``manage-repo`` visibility
is governed EXCLUSIVELY by the two-gate model (§4.1): catalog-presence +
status==enabled + eligibility-matched. There is NO per-version approval gate —
for first-party skills, the release pipeline (PR review + merge + deploy) IS the
approval. An eligible manager/operator resolves them as exposed; a non-eligible
agent does not.

Any future approval concept (for user-authored or third-party skills) would be a
PLATFORM-OWNER catalog-admission gate — not a second-stage gate within the
first-party release pipeline and not a customer-self-serve feature.

**Eligibility scoping.** ``manage-agent`` and ``manage-repo`` visibility is
scoped to **MANAGER/OPERATOR agents** — NOT org-wide. The default eligibility
policy in ``org/config.yaml`` grants access to:
- ``engineering_manager`` via agent-scoped allow (engineering team manager).
- ``product_lead`` via agent-scoped allow (product team manager).

Non-manager agents (including engineering team workers such as ``dev_agent``,
``code_reviewer``, ``qa_engineer``) do NOT resolve ``manage-agent`` or
``manage-repo`` as exposed — even if they are in the engineering team. The
eligibility formula is the standard additive-inheritance model (see §4.1):
agent-scoped allow only; no team or org scope.

**HIGH-IMPACT POLICY = GUIDANCE VISIBILITY + VERSION PROVENANCE ONLY — NOT
COMMAND ACCESS.** ``high_impact_policy`` governs guidance visibility + version
provenance in the compact skill index. It does NOT grant or deny command
execution. ``manage-agent`` and ``manage-repo`` command access remains
separately governed by allow_rules / daemon auth per the existing permission
model (§3). The policy model is additive and permission-inert.

**Phase-3 (HISTORICAL — superseded by completed Phase-4 cutover).** During
Phase 3 the managed-catalog entries were registered and eligibility was
scoped while the ``protocol/skills/`` directory still existed as a
wholesale-dump safety net. This transitional state is now obsolete.

**Phase-4 cutover (COMPLETED / REMOVED).** The wholesale ``protocol/skills/``
dump and its former ``_WHOLESALE_DUMP_ENABLED`` flag are permanently removed
from source. The 8 ``protocol/skills/`` directories remain on disk only as
packaged source material for the system-contract injection path — they are
never copied into workspaces. The ``SKILL.md`` source of truth for the 3
managed-catalog skills lives in ``runtime/skills/<id>/``. See §4.6 and
§4.10 for the full delivery model.

**Fences.** Phase 3 does not:
- Grant tools, credentials, or capabilities (manage-agent/manage-repo command
  access remains in allow_rules / daemon auth per the existing permission model)
- Require a SQLite migration (file/YAML-backed only)
- Add new daemon routes
- Change the existing permission model or auth
- Add a web admin UI
- Record any founder approval for the version (maker-checker — founder action only)

### 4.10 Phase-4 Cutover — Managed-Skill Workspace Injection (THR-055 Phase 4)

The Phase-4 cutover completes the migration by STOPPING the wholesale
``protocol/skills/`` dump and delivering skills EXCLUSIVELY through:
1. ``inject_system_contracts`` — context-aware system-contract injection (§4.7)
2. ``inject_managed_skills`` — policy-resolved managed-catalog injection (this section)

**Injection model.** On EVERY session creation (task/subtask, thread reply,
wake, dream), ``inject_managed_skills`` resolves the two-gated catalog +
eligibility policy for the session's (agent, team) and copies each EXPOSED
managed skill from ``runtime/skills/<id>/`` into ``.claude/skills/<id>/``
and ``.agents/skills/<id>/``.

**Resolution flow:**
1. Load ``SkillRegistry`` from ``<project_root>/runtime/skills/``.
2. Load eligibility policy from ``<project_root>/org/config.yaml``
   (``skills`` section).
3. Resolve exposed skills via ``resolve_exposed_skills`` (both gates:
   catalog gate + eligibility gate).
4. Copy each exposed skill's package into the workspace skill dirs.

**Context-exposure rules:** managed skills are context-AGNOSTIC — ``reflection``,
``manage-agent``, and ``manage-repo`` are injected into ALL session types
where the agent is eligible ($4.1 two-gate model). System contracts remain
context-aware ($4.7).

**Fail-closed.** Disabled, catalog-absent, or ineligible skills are NOT
injected. The catalog gate (presence + enabled) is independent of the
eligibility gate — both must pass.

**No config rollback gate.** The former ``_WHOLESALE_DUMP_ENABLED`` flag in
``workspace_adapters.py`` is permanently removed — there is no re-enable
path. The wholesale copy is unreachable. The ``protocol/skills/`` directories
remain on disk as packaged source material for the system-contract injection
path and are never deleted.

**Coverage.** The contract-completeness guard test
(``test_skill_cutover_completeness.py``) proves that every agent (7) × every
session context (4) × every repo state (2) = 56 combinations receive the
complete required set without the wholesale dump. The test asserts:
- System contracts are context-correct per §4.7 predicates.
- ``reflection`` is injected for ALL agents (org-wide universal).
- ``manage-agent`` / ``manage-repo`` are exposed to eligible managers/operators
  only and hidden from non-eligible agents (eligibility gate).
- ``dream`` is excluded from non-dream contexts for session guidance only;
  its workspace link survives across all ordinary contexts.
- ``make-worktree`` is repo-gated.

**Session-path coverage.** ``inject_managed_skills`` is wired into all 6
session-creation callers via ``materialize_workspace_skills``:
1. ``Orchestrator._run_agent`` (task/subtask) — resolves team via
   ``load_agent``.
2. ``thread_runner.run_invocation`` (thread reply/bootstrap) — resolves
   team from ``ThreadParticipant`` record.
3. ``wake_runner.run_wake`` (working-hours wake) — resolves team from
   ``agent_def``.
4. ``dream_runner.run_dream`` (private dream) — resolves team via
   ``load_agent``.
5. ``schedule_runner.run_schedule_fire`` (schedule fire) — resolves team
   via ``load_agent``.
6. **Executor-switch / set-executor** — resolves team from ``agent_def``
   and builds a SINGLE full-expected-spec union from all six contexts
   before materialization (§4.6).

**System-contract union rule (all 6 callers).** For valid SessionContext
values, every caller unions system-contract expectations across all
ordinary session contexts before reconciling workspace links — a later
single-context launch never withdraws a valid system-contract link
belonging to another context.  An unrecognised context string is a no-op:
the function returns immediately without creating, building, preflighting,
or reconciling any links, and must not withdraw or mutate an existing
valid workspace state.  Release-managed and B2 custom-skill links remain
policy-reconciled and are withdrawn when the agent becomes ineligible or
unassigned.

**No config rollback gate.** The former ``_WHOLESALE_DUMP_ENABLED`` flag is
absent from source — there is no "set to True" escape hatch. The executor
adapter ``_copy_skills`` bootstrap writers and the set-executor route both
participate in the same process-local workspace lock (§4.6.1) through the
canonical skill store + symlink materialization path.

**Fences.** Phase 4 does not:
- Grant tools, credentials, or capabilities (skills are permission-inert)
- Modify the permission model, auth, allow_rules, or daemon authorization
- Require a SQLite migration (file/YAML-backed only)
- Add new daemon routes
- Add a web admin UI
- Delete ``protocol/skills/`` directories (reversible via flag)
### Active manager policy prompt and evaluator pin (THR-181 S4)

For Engineering only, the runtime resolves the authenticated current immutable
authority-policy release at each launch producer and renders one server-reserved,
clearly delimited section containing release id, version, digest, clauses, and the
exact canonical continuation phrase. The task, thread (new/resumed/fallback), wake,
dream, and schedule producers all consume this resolver. Only the eligible
`engineering_manager` receives it; worker prompt bytes omit it. Agent-authored
system/brief/role material and thread history/deltas, schedule briefs, wake
routines, and dream history/audit inputs using the reserved header or either
delimiter marker are rejected before launch. Absence of an
activation is the compatible ordinary-launch state; corrupt or incoherent active
history fails closed.

Each eligible task launch persists the exact authenticated release/activation
identity rendered in that session (or an explicit static-mode receipt) in the
existing append-only audit structure. The authority hook reads that session
receipt and never resolves mutable current activation. At the hook, a DB-backed
policy candidate and its S1 policy pin are one
transaction. The pin binds immutable release and activation epoch plus evaluator
provider/executor; candidate identity binds exact policy, prompt/template, and
model id/version/digests. Evaluation uses the authenticated pinned release snapshot,
re-authenticating the persisted pin and immutable release before evaluation
recording and continuation commit. A newly DB-backed candidate
without a valid pin fails closed. Historical static-policy candidates retain their
documented unpinned compatibility behavior and are never backfilled.

> **TASK-6514 direct retirement:** Custom executor identity is exclusively an
> explicit `custom-adapter:<id>` binding. The former generic template identity,
> aliases/default, parser and launch path are retired without read compatibility
> or automatic migration. Recovery uses a built-in reassignment or ordinary
> registration of a valid approved custom-adapter profile.
