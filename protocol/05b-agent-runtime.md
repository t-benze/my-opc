# Agent Runtime: Execution, Memory & Lifecycle

How agents are spawned, how they remember across sessions, and when they run.

---

## 1. Agent Execution Model

### Every agent runs as a coding-agent session

Each agent in the organization is not just an LLM call — it's a full coding-agent session that can read files, write files, run commands, search the web, and interact with APIs. The orchestrator layer decides *when* each session runs, *what context* it gets, and *how* outputs flow between them.

### Per-agent executor selection

Agents run through a configured coding-agent CLI. The runtime ships with four
built-in adapter profiles: Claude Code (`claude -p` with `--permission-mode auto`),
Codex (`codex exec --json -`), opencode (`opencode run`), and Pi (`pi -p ... --mode json`).
Custom executors use only registered profiles whose
``command_adapter_id: custom-adapter:<id>`` binds to a conformance-passed,
founder-approved adapter. The stable v1 ``AdapterInput``/``AdapterOutput`` contract,
profile binding, server-authoritative eligibility, and executable SHA-256 checks apply
at approval and every launch; the direct-connect flow uses the same approval and bind
primitives. There is no automatic or versioned fallback. Recovery is ordinary
reassignment to a built-in executor or ordinary re-registration of a valid approved
custom-adapter profile. Executor selection is stored in the org agent frontmatter
(``AgentDef.executor``), so agents can run on different executors in the same org.

**Per-agent model override (Issue #568).** An agent may override the default
model its executor launches with via the ``model:`` field in its frontmatter
(``AgentDef.model``). This is the SINGLE authoritative per-agent model store.
When set, the runtime forwards it to the executor via ``executor.run(model=...)``
in every built-in invocation class:

- **Task/subtask:** ``Orchestrator._run_agent`` resolves model via
  ``_resolve_model_name`` and passes it to ``executor.run(model=model_name)``.
- **Thread bootstrap/reply:** ``thread_runner.run_invocation`` resolves model
  from ``AgentDef.model`` and passes it in the `_invoke` closure.
- **Working-hours wake:** ``wake_runner.run_wake`` resolves model from
  ``AgentDef.model`` and passes it to ``executor.run(model=...)``.
- **Dream:** ``dream_runner.run_dream`` resolves model from ``AgentDef.model``
  and passes it to ``executor.run(model=...)``.
- **Schedule fire:** ``schedule_runner.run_schedule`` resolves model from
  ``AgentDef.model`` and passes it to ``executor.run(model=...)``.

When ``model`` is absent (``None`` or not set in frontmatter), the executor
launches with its CLI-default behavior — the same as before Issue #568.
Model overrides are executor-specific. When an existing agent's executor
actually changes, the old configured model is cleared so it cannot carry into
the new executor; an idempotent executor update preserves it. A
``manage-agent`` update that supplies both a changed executor and an explicit
``model`` treats that model (including explicit ``null``) as the caller's new
executor choice. Omitting ``model`` clears the old override on a real switch.
Custom executor profiles and custom-adapter profiles are unaffected: model
is forwarded through the standard ``executor.run(model=...)`` API without
modifying the executor factory, permission construction, or adapter semantics.

The session-not-found eviction fallback in ``run_invocation`` also forwards
the model on its clean-slate retry — both the initial resume attempt and the
fallback full-prompt launch receive the same ``model`` value.

**Prompt transport and the pre-spawn argv guard (THR-200).** The invocation
prompt is one opaque string; the kernel caps a single argv element (Linux
``MAX_ARG_STRLEN`` = 128 KiB − 1, measured 131,071 bytes on this org's
hosts; macOS/Windows limits differ). Prompt bodies therefore belong on **stdin**
for every stdin-capable built-in:

Before any Popen, ``_run_command`` runs a **portable pre-spawn argv guard**:
when the prompt travels via argv (``input_text`` is None), every argv element
is checked against the platform-safe per-argument byte limit and an oversized
element fails deterministically with the normalized category
``prompt_transport_too_large`` BEFORE the kernel can raise ``E2BIG``
mid-launch. The prompt is NEVER truncated. Encoded byte size is
**transport-only** — it is not a cost or reset policy; future cost policy
must use turn count or cumulative session tokens (transcript bytes do not
track provider-session cost). The guard preserves known-good smaller argv
executions (the limit is never below the platform's kernel floor).

**Thread provider-session lifecycle (THR-200).** ``thread_participants``
carries the resumable provider session id + delta watermark
(``agent_session_id``, ``last_resumed_seq``). Lifecycle rules:

**Thread reply delivery lifecycle (GH-688 Phase 1).** Conversational
``REPLY`` wakes are coalesced and durably tracked per ``(thread_id,
agent_name)`` in the additive ``thread_reply_delivery_state`` table. The store
owns every state transition: ``record_conversational_arrival`` appends a
message and raises/creates at most one queued wake per pair in one
transaction; ``claim_conversational_reply`` is the durable queued→running CAS
a runner must pass before any prompt or provider work (a stale/duplicate
queue notification no-ops there); ``settle_conversational_reply`` is the
single seam for every terminal path (reply, silent decline, clean-no-callback,
provider failure, timeout, materialization failure). A successful/declined
range acknowledges only the claimed coverage; arrivals during the run yield
exactly one follow-on; failures leave ``retry_required`` for the next
conversational arrival (no hot loop). Abort/archive/participant-removal
discard through an explicit boundary and never resurrect. At daemon startup,
``_sweep_on_startup`` replaces only the conversational ``REPLY`` portion of
the generic reaper with store-owned recovery: a valid queued wake — a
pending, **unstarted** same-pair ``REPLY`` (the claim CAS enforces the same
precondition) — is retained and re-enqueued; an interrupted running
``REPLY`` becomes exactly one ``daemon_restart`` replacement; and a malformed
queued slot referencing a **started** receipt fails closed — the owned
pending ``REPLY`` receipts for the pair are retired, the slot clears with a
truthful diagnostic, nothing is re-enqueued, and the preserved
``required_through_seq`` lets the next conversational arrival mint the single
covering wake. ``BOOTSTRAP`` / ``TASK_FOLLOWUP`` keep the
generic reaper exactly. See ``docs/agent-guides/features-and-invariants.md`` →
Thread Broadcast Routing for the full contract.

Every Phase-1 store transition emits a lifecycle audit row atomically in the
same SQLite transaction (Slice C), under the existing ``audit_log.task_id =
THR-*`` scope convention: ``thread_reply_wake_created`` (arrival mints a queued
wake), ``thread_reply_wake_coalesced`` (arrival advances an existing wake),
``thread_reply_wake_claimed`` (queued→running CAS success),
``thread_reply_wake_settled`` (reply/decline/failure/timeout terminal),
``thread_reply_wake_cancelled`` (abort/archive/participant-removal discard and
fail-closed recovery sweeps), and ``thread_reply_wake_recovered`` (startup
retention/replacement of a wake). Duplicate queue notifications (stale claim
no-ops) and idempotent recovery can never fabricate events; pure slot-clears
record only the existing ``last_terminal_reason`` diagnostic. Payloads carry
agent, inclusive range, 8-char token prefix and outcome/reason/follow-on
result — never full single-use tokens. The web UI projects the same
``reply_delivery`` pair state honestly (running, queued/coalesced count,
healthy-neutral held waiting proved by both an OPEN exchange and matching HELD
participant deferral, or retry_required diagnostic) without fabricating
subprocesses. Precedence is running > queued > held > retry_required > settled
omission. A genuine current retry also carries a bounded failure category
(``no_callback``, ``no_callback_after_reprompt``, or ``infra_fail``) derived by
the same server classifier as responder history. Compact captions use only
that category; raw terminal reasons remain historical detail metadata and are
never used as compact copy or to fault-style held waiting.
Per-message ``responder_status`` entries additionally carry the authoritative
invocation ``purpose`` (``reply`` | ``task_followup``; BOOTSTRAP stays
excluded) so the web classifies/dedups in-flight responders by purpose —
never by the triggering row's kind, which would mislabel a
system-row-anchored coalesced REPLY range as a special wake.

**Thread reply provider breaker (THR-200 PR A + PR B).** The additive
`thread_reply_breaker_episodes` and `thread_reply_breaker_receipts` tables, plus
their due/lease/receipt indexes, are created idempotently by the shipping
`Database` initializer. Existing columns and their meanings are unchanged. Pinned v0, v1, and
interrupted-stage SQLite stores prove forward creation/repair and legacy-row
preservation, while an isolated harness executes the actual `e197b20`
application `Database` reader and asserts its model return contract. Rollout is
single-version with daemon admission stopped; mixed-version writes are not
authorized. PR B activates exactly-three structured final post-launch provider
failures, count-once settlement, OPEN coalescing with no launch, and one durable
HALF_OPEN probe after exactly 900 seconds. Threshold 3 and cooldown 900 are
non-configurable policy constants; environment/config input cannot change them
or fork continuity across restart. A recovered ownerless gap with no episode uses
that cooldown-probe path, never ordinary CLOSED launch. Held OPEN-exchange
deferrals remain held and excluded; breaker recovery never releases them. Probe
failure rearms 15 minutes and success closes/reset continuity. Durable receipts,
lease CAS and redacted audits make restart, duplicates and stale callbacks safe.
Every scheduler tick re-emits a committed-but-still-pending queued probe. The
process queue's token ownership spans queued and in-flight delivery: normal
worker return acknowledges it, while exception or cancellation before claim
releases it for a later tick. The durable invocation claim remains the
exactly-once provider-launch authority.
PR B adds no API/OpenAPI/TypeScript/web projection or manual action.

Failed thread-invocation audits add capped ``stdout_tail`` and ``stderr_tail``
payload fields without changing the audit scope identity. Human-facing Claude
failure detail prefers a parsed structured terminal result only when all raw
stderr lines are the known workspace-trust warning shape. Classifier inputs,
exact eviction detection, retry ownership, and breaker qualification continue
to consume the unchanged executor result fields. A session-limit notice is not
an automatic short-backoff rate-limit retry signal.

The envelope schema maps 1:1 to
the ``TokenUsage`` model (``runtime/models.py:302``) with identical key names.
Token-accounting invariants (``total`` excludes cache reads, nullable tolerance,
model-null backfill to provider label) apply uniformly to envelope-reported tokens.
The full contract is documented in
``docs/superpowers/specs/2026-07-19-custom-cli-adapter-envelope-design.md``.

**Custom adapter contract (THR-107 D7B — custom-adapter profiles).** Custom
executor profiles with ``command_adapter_id: custom-adapter:<id>`` bind to exactly
one registered, conformance-passed, founder-APPROVED custom adapter executable.
The CustomAdapterExecutor launches the adapter as a subprocess, passing a v1
``AdapterInput`` JSON on stdin and parsing a v1 ``AdapterOutput`` JSON from stdout.
The stable v1 contract is defined by the Pydantic models at
``runtime/orchestrator/adapter_contract.py``; the **canonical contract surface
for external consumers** (candidates implementing adapter wrappers) is the
versioned ``GET /api/v1/runtime/adapters/contract-reference`` endpoint
(THR-107 seq184), which returns JSON Schemas generated from those models at
runtime. **THR-107 seq339/340:** the contract-reference response also returns
``canonical_directory`` and ``required_executable_path`` — the daemon-managed
canonical adapter path (<daemon-home>/adapters/<canonical-id>, 0700). Scoped
submissions must create the wrapper at exactly this path; the route and
registration seam independently enforce canonical placement. The
master-bearer ``/register`` route is unchanged. The server-derived schema is canonical. Key invariants:

**THR-200 session plumbing (PR 1/3).** The v1 contract remains version 1:
``AdapterInput.session`` may carry a non-empty provider session id only for a
thread invocation, and ``AdapterOutput.session_status`` is the optional
``fresh | resumed | not_found`` outcome. ``fresh`` means a new provider session,
``resumed`` means the supplied session continued, and ``not_found`` means the
supplied session did not exist. A sent session requires a status; ``resumed`` or
``not_found`` without a sent session, successful ``not_found``, and
``not_found`` with non-empty result text are contract failures. This additive
field keeps legacy kimi/codebuddy-shaped outputs valid without re-registration.
Tasks always stay fresh. This PR earns and consumes no resume capability:
custom profiles remain excluded by ``thread_runner`` and SQLite thread
transcripts and delivery state remain canonical.

**THR-200 resume conformance (PR 2/3).** ``thread_resume`` is a
server-reserved, server-earned ``AdapterEntry.capabilities`` value. Both
registration request shapes reject a submitted claim and expose an explicit
``verify_thread_resume`` opt-in. The probe makes three sequential provider
calls in one runtime-owned temporary workspace: fresh must return an opaque
canary and nonempty provider session id; resume receives a new canary but not
the old one and must return both with ``session_status=resumed`` and a nonempty
stable-or-replaced id; a mandatory fabricated ``hr-probe-missing-<uuid>`` id
must return false success, ``session_status=not_found``, and no result text or
canary output. Only a full pass atomically publishes the capability plus
``thread_resume_verified_at`` and the probed contract version in the existing
adapter YAML entry. Every failure removes the workspace and leaves any durable
entry byte-identical. Re-registration without a new passing opt-in proof clears
the earned state and always clears approval; executable, dependency, declared
capability, or workspace-adapter identity changes likewise cannot inherit it.
PR 2 does not consume this receipt: ``thread_runner``, built-in resume, task
freshness, existing custom-profile full-transcript behavior, and contract v1
remain unchanged.

**Wrapper-owned headless launch posture.** A custom-adapter wrapper MUST
choose and apply its underlying CLI's own non-interactive, sufficiently
permissive launch posture for every unattended daemon session. It MUST NOT
rely on ``executor_context.permission_mode`` for its CLI-specific headless
posture or on daemon translation of policy or provider-specific allow-rule
strings. That existing v1 field remains a legacy nullable, provider-specific
compatibility field; ``CustomAdapterExecutor`` supplies ``null`` for
custom-adapter invocations. The wrapper MUST preserve callback availability
from the daemon-provided environment (including ``PATH``) so the invoked agent
can perform ordinary workspace actions and invoke its required ``happyranch``
callback. This is a wrapper implementation and founder-approval responsibility:
approval requires evidence of a successful end-to-end unattended session that
invokes the required callback. It adds no new daemon-supplied or
daemon-translated permission policy or field to ``AdapterInput`` and no daemon
permission/sandbox expansion.

**THR-107 Slice 1A mint authority foundation.** A master-authenticated runtime
adapter-purpose token mint may additionally carry an exact first-party
``workspace_adapter_id``. When present, the daemon writes only a
domain-separated one-way token fingerprint and server-derived intent into a
separate runtime-root SQLite authority store. These ``minted_nonlaunchable``
rows are not profiles, adapters, projections, connection state, or launch
eligibility. **Slice A** adds one loopback, registration-token-only
``POST /api/v1/runtime/custom-cli/connect`` ingress. It accepts only a strict
v2 manifest and nonsecret metadata, derives profile/adapter/wrapper authority
solely from the fingerprinted mint, validates the existing canonical
``<daemon-home>/adapters/<canonical-adapter-id>`` wrapper and declared child
paths, then writes a read-back ``received_nonlaunchable`` receipt plus
append-only event. It never creates/copies/chmods a wrapper, runs a probe or
``Popen``, creates a YAML profile/registry/adapter entry, or claims Connected.
Malformed known-direct attempts terminalize their authority; unknown or
foreign registration context remains ordinary invalid context. Legacy adapter
mints without the optional field remain the existing PENDING-submission path.
If the final registration-token commit fails, Slice A compensates the
receipt/event boundary.

**THR-107 Slices 1–3: projection, launch fence, UI cutover.** Building on
Slice A's `received_nonlaunchable` receipt:

- **Slice 1 (projection).** A separate, master-bearer-authed
  `POST /api/v1/runtime/custom-cli/{operation_id}/commit` route (never the
  registration-token-authed `/connect` route, which is pinned to spawn zero
  subprocesses) drives one receipt through a durable `planned` →
  `committed`/`failed` state machine (`direct_connect_projections` table,
  additive to the Slice A authority store). Committing runs the SAME bounded
  conformance probe the legacy master-bearer registration path uses. For direct
  projection, the wrapper forwards the entire normal v1 ``AdapterInput.prompt``
  through one real provider invocation, obtains a genuine terminal provider
  response, and returns the wrapper-owned ``AdapterOutput``; the provider does
  not construct that envelope. The fresh opaque canary must appear complete in
  ``result.text``; a fabricated/static success does not prove delivery. The
  short probe guides no optional tool use or workspace exploration without
  enforcing or collecting telemetry about those provider-internal actions, and normal task
  behavior is unchanged. The projection then
  reuses the EXISTING `adapter_store`/`custom_adapter_registry` persistence
  primitives (not a second write path) to durably write an
  `AdapterEntry(status="approved", registered_by="direct-connect",
  approved_by="direct-connect")` with a `dependency_manifest_version: 1`
  manifest from the receipt's declared children, then binds a runtime
  profile via the same `_perform_adapter_profile_binding` the seq237
  atomic-approve-and-bind path uses. `version`/`contract_version` are read
  from the probe's own `AdapterOutput.adapter_metadata` (not the manifest,
  which carries no such fields); `capabilities` defaults to `[]`
  (D5 baseline-only — direct-connect has no manual capability-declaration
  surface). Idempotent on retry; a single winner under concurrent commit
  attempts (the `direct_connect_projections` insert is the sole arbiter;
  losers reconcile its durable `planned` or terminal state instead of racing
  the probe a second time); every failure path compensates (removes the just-created
  `AdapterEntry` if profile binding fails) so no partial adapter/profile/
  registry state survives. `GET /api/v1/runtime/custom-cli/status` (keyed
  only by `intended_profile_name`, never token plaintext) exposes the
  deterministic wrapper destination plus the latest live receipt's id and
  projection state. A historical `committed` projection whose profile is no
  longer present in both the durable runtime profile store and active executor
  registry is not a receipt: `operation_id`, `profile_state`, and `reason`
  are all reported as null. This read-time reconciliation lets the founder
  reconnect after removing a profile without mutating the historical
  projection record. The browser can find the daemon-issued
  wrapper path to show in a connect prompt and detect when the candidate
  CLI's own `/connect` call has landed.
  The daemon also runs a periodic projection sweep that finds each
  `received_nonlaunchable` operation without a projection row and invokes the
  same coordinator directly; therefore completing a connection does not
  depend on a browser calling `/commit`, while `/connect` remains a strict
  zero-subprocess receipt boundary.
  `POST /api/v1/runtime/custom-cli/{operation_id}/forget` is the separate
  master-bearer-authenticated Settings → Executors cleanup for a terminal `failed`
  projection. It is the only direct-connect route that deletes derived rows.
  It refuses missing, `planned`, and `committed` operations, and also refuses
  while retry validation is `running` or after it `succeeded` (the latter
  retains the live connected binding). Cleanup removes only the permitted
  derived artifacts for that operation, its failed projection row, and any
  retry-attempt row. The immutable parent authority, accepted candidate record,
  canonical identity history, receipt, operation row, and event trail remain
  append-only and retained for the parent authority lifetime; they are never
  deleted, rewritten, or truncated. Before removing derived artifacts, the
  route opens the failed receipt's persisted wrapper path with no-symlink
  handling and hashes its regular-file descriptor. Because the supported
  filesystem APIs provide no compare-and-unlink operation bound to that
  descriptor, cleanup never unlinks any present wrapper: an absent wrapper is
  reported absent, a hash mismatch is reported changed, and a matching,
  symlink, nonregular, or unreadable candidate is reported preserved-unsafe.
  This fail-closed retention prevents a successor from being silently unlinked
  or displaced at the canonical path.
  `happyranch custom-cli forget
  <profile>` first reads the status route and refuses to call this cleanup
  route unless the profile state is `failed`.
  **THR-160 corrected-artifact retry and immutable-snapshot validation.**
  The normal direct-connect flow has two separate retry paths.

  1. **Corrected-artifact retry (same-token).** When a first candidate fails
     only the conformance probe, the canonical candidate-ledger state is
     `failed_retryable` with `retry_eligible: true`. The founder modifies the
     wrapper or child artifacts and reruns the *existing* generated prompt
     before the original 30-minute expiry. The candidate CLI's `/connect`
     admits exactly one genuinely changed candidate; unchanged or merely
     reordered artifacts receive an indefinite, non-consuming `409 Duplicate`
     and are refused. There is no cooldown. A second terminal failure moves
     the ledger to `exhausted` (no further candidates allowed), while expiry
     moves it to `expired`; both are nonretryable. Success at any point
     closes the lifecycle as `connected`. Terminal legacy v0 operations that
     predate the THR-160 candidate ledger are classified on store open before
     any backfill: the operation, receipt, projection, artifacts, and authority
     must all correlate (matching operation/receipt IDs, receipt token
     fingerprint equal to the operation fingerprint, receipt in the expected
     received state, a terminal `conformance_probe_failed` projection with no
     bound/approved `profile_name` or `adapter_id`, no succeeded retry, and a
     derivable normalized identity). Only a fully trusted conformance failure
     backfills as an `open` parent with a failed ordinal-1 candidate so a
     genuinely changed corrected candidate is admitted as ordinal 2. Every
     other terminal category, corrupted receipt correlation, bound/approved
     profile fact, expired authority, or integrity-invalid artifact set is
     backfilled closed (`failed`/`expired`) without ever opening the parent;
     where the identity is still derivable it is retained so identical/reordered
     replay is rejected non-consumingly. The two-candidate cap still refuses a
     third candidate. This path never calls `/{operation_id}/retry`, never
     replays a generic token, and never requires `/forget` first.

  2. **Immutable-snapshot validation.** A distinct master-bearer
     `POST /api/v1/runtime/custom-cli/{operation_id}/retry` action is
     eligible only for a terminal `failed` projection that is also the
     latest accepted candidate for its parent token fingerprint. Once a
     newer candidate has been accepted under the same parent, the older
     candidate is stale and retry claims are refused without consuming a
     retry attempt or running a probe. It re-checks the *unchanged*
     persisted wrapper/child snapshot; it is not an artifact retry. It never updates, replaces, or deletes that projection or its
     failure reason, and `/commit` remains idempotent for the historical
     failure. A separate durable retry-attempt lifecycle supplies the atomic
     single-probe winner, terminal outcome, and append-only category-only
     events. Concurrent retry callers share that single running attempt and
     wait (bounded by the winner's own bounded probe work) for its real
     terminal outcome; a concurrent caller never receives a fabricated
     failure while the winner is still legitimately in flight. Before any probe it reads only the receipt's persisted wrapper
     path/SHA and every persisted child path/SHA, then independently rechecks
     each artifact with the intake/launch regular file, executable,
     no-symlink, exact-path, and SHA-256 checks. Missing, duplicate, unusable,
     or changed snapshot data fails closed before invocation; the retry never
     accepts user artifact fields, a mutable adapter record, ambient PATH, a
     new manifest, or a token replay. A successful bounded conformance probe
     writes/binds through the same adapter/profile persistence primitives and
     compensation rules as projection, then records a distinct retry-success
     fact. Status may report the resulting *live* connection as `committed`
     for existing consumers, but includes the retained historical failed
     projection state/reason whenever retry success is the source; it never
     claims the original projection row changed. Failed retries retain both
     the original projection/evidence and no adapter/profile/registry residue.

  Both paths are bounded by: a two-candidate cap per direct-connect lifecycle;
  durable retry after a generic initial consume or daemon restart; append-only
  identity/audit/receipt retention (forget only removes derived projection and
  retry-attempt rows); the legacy submit fence (`/connect` is receipt-only and
  spawns no subprocess); status redaction (no token plaintext, fingerprint,
  identity digest, candidate/artifact history, hash, probe output, or error
  output in `GET /runtime/custom-cli/status`); and the canonical wrapper
  destination as the only required prompt value.
- **Slice 2 (launch fence — proof, not new gating).** `build_executor()` /
  `ExecutorRegistry._resolve_custom_adapter_eligibility()` /
  `CustomAdapterExecutor._launch()` already refuse to construct or launch
  anything but an `AdapterEntry.status == "approved"` adapter with a live
  on-disk SHA-256 re-check at every `Popen` attempt including throttle
  retries — this predates THR-107 slice 1 and applies with no
  origin-specific branching, so a Slice-1-committed direct-connect profile
  is launch-eligible through the identical seam a legacy founder-approved
  adapter is. `runtime/daemon/wake_runner.py`, `dream_runner.py`, and
  `schedule_runner.py` import the SAME `_build_executor_for_provider`
  function `thread_runner.py` defines (not independent copies);
  `Orchestrator._build_executor` is a second thin wrapper over the same
  `build_executor()`. `tests/test_thr107_launch_fence.py` proves this
  end-to-end against a real Slice-1-committed profile, plus that an
  operation which never reached COMMITTED has no registered profile and
  fails closed with `"Unregistered executor"` at the same seam.
- **Slice 3 (UI cutover).** The normal Settings ▸ Executors and onboarding
  custom-CLI flow drives `POST /connect`, then derives connection state by
  polling `GET /runtime/custom-cli/status`. Its receipt-landing handler is
  observation-only: it polls status, never auto-fires `/commit`. A
  `failed_retryable` status instructs the founder to modify artifacts and
  rerun the existing generated prompt; the UI does not call
  `/{operation_id}/retry`, mint a new token, or call `/forget` first. The
  dedicated `/retry` action is exposed only as the historical
  immutable-snapshot validation path for terminal nonretryable failures,
  textually distinct from artifact retry. The daemon-owned projection
  sweep actually completes receipts to `committed`/`failed`, including when no
  browser is present or the tab closes — Connect → Connected in one perceived
  action, no founder-approval wait and no separate conformance-checkin round
  trips. The
  normal-flow PENDING/approve/reject/legacy-bind-recovery UI
  (`PendingAdaptersSection`, `RecoveryBindCard`, the `useAdapterConnect`/
  `useAdapterRecovery` hooks) is deleted outright, not hidden behind an
  advanced panel. The backend `POST /runtime/adapters/{id}/approve|reject|
  bind-profile` routes and their `lib/api/adapters.ts` TS bindings are
  UNCHANGED and preserved as operator-only one-time disposition tooling
  outside the normal user flow (`tests/contract/route-classification.json`
  reclassifies them from `included` to `excluded` — no normal-flow browser
  consumer remains, but the routes and Slice-1-era tests stay intact for
  manual/scripted operator use).

**Correction — `workspace_adapter_id` is CLI-declared, not founder-chosen
(post-slice-3 follow-up).** The Slice 1 paragraph above and the original
Slice 1A mint-authority paragraph both describe `workspace_adapter_id` as
a mint-time founder choice; that shipped, then was reversed after tracing
where the field is actually consumed. It is read ONLY at `happyranch
init-agent` time (`ContextBuilder._adapter()`, `runtime/orchestrator/
context_builder.py`), to pick which workspace-bootstrap convention an
agent's workspace uses (`.claude/settings.json` + `CLAUDE.md` vs
`AGENTS.md`-style) — it plays no role in the connect/probe handshake
itself, and the wrapper author is the only one who actually knows which
convention their CLI expects. `DirectManifestV2` now carries a REQUIRED
`workspace_adapter_id` field (one of `claude`/`codex`/`opencode`/`pi`),
declared by the connecting wrapper in its own `POST /connect` body.
`DirectConnectAuthorityStore.receive()` takes this as an explicit
parameter and stores it on `direct_connect_operations`, superseding
whatever value was set at mint time — which remains ONLY the pre-existing
Slice-1A authority-row activation trigger (the founder's browser now sends
a fixed internal value; the founder never picks one). Downstream
(`get_receipt_artifacts` → projection → `AdapterEntry.workspace_adapter` +
the bound runtime profile) is unaffected, since it already read from this
same column. The Settings/onboarding "Workspace CLI" dropdown described
above no longer exists — `AdapterConnect`'s form is name-only; the
generated connect prompt instead has the wrapper author pick their own
convention and send it in the manifest body.

The signed architecture is at
``docs/superpowers/specs/2026-07-24-unified-adapter-runtime-architecture.md``.

**THR-181 S6b/S7 manager runtime visibility.** A newly launched eligible
Engineering Manager remains the semantic evaluator for its own proposed
decision; no separate production evaluator process is launched. The manager
Agent page may read only bounded secret-free immutable history and durable
outcome receipts, with missing causal linkage explicitly marked
`receipt_incomplete`. Workers and other managers receive no API or DOM surface.
Code landing/redeploy and explicit founder-authorized production activation are
separate events; activation retains the ordinary daemon mechanical fences.

Each agent's configuration specifies context and workspace:

```
agent_config:
  dev_agent:
    executor: claude
    system_prompt: 03-system-prompts-workers.md#dev-agent
    workspace: workspaces/dev_agent/
    context_files:
      - 01-org-charter.md
      - knowledge_base/technical/
      - agent_memory/dev_agent/memory/
    permission_mode: auto
```

### Context injection via executor bootstrap docs

The orchestrator assembles each agent's context into an executor-specific bootstrap file placed in the workspace root. Claude workspaces use `CLAUDE.md`; Codex, opencode, and Pi workspaces use `AGENTS.md`. This file is regenerated at the start of every session. It includes:
- Agent system prompt (role, accountability contract)
- Relevant org charter sections
- Pointer to the agent's persistent memory store
- Task-specific brief (the actual assignment)

### Permission enforcement and callbacks

Claude workspaces have a `.claude/settings.json` that configures Claude Code's auto-allowed tools. Codex, opencode, and Pi workspaces do not use that file. Across executors, agents call back through the same single-line `happyranch ... --from-file` contract. Agents can read, write, and execute freely within their workspace and the cloned codebase, subject to the executor's sandbox mode and the orchestrator's workflow rules. Pi has no HappyRanch-managed sandbox or permission file in this integration.

### Skill materialization at session spawn

Skills — structured guidance packages that tell an agent how to perform specific
operations — are materialized into the agent's workspace on every session spawn
by `materialize_workspace_skills` (`workspace_adapters.py`). This runs on all
spawn contexts (task, thread, wake, dream, schedule, bootstrap, executor-switch).
An unrecognised context string — one that is not a valid ``SessionContext`` value
— is a no-op: the function returns immediately without creating, building,
preflighting, or reconciling any links, and must not withdraw or mutate an
existing valid workspace state.  For valid contexts, system-contract links are
unioned across all ordinary session contexts so a later single-context launch
never withdraws a valid link belonging to another context; release-managed and
B2 custom-skill links remain policy-reconciled and withdrawable.

The universal persistent-verification enforcement point is
``protocol/skills/jobs/SKILL.md``. In all six contexts it requires
``scripts/local_ci.sh all``, canonical/full suites, and pushes whose hooks run
such verification to use a durable job whenever expected to exceed one minute
or the safe interactive-session window: ``review_required:false``,
``persistent:true``, then ``blocked`` + ``waiting_on_job_ids`` and resumed
inspection of the exact-command/runtime/exit-0 receipt. Short/focused tests
expected to complete inside one minute remain valid in-session. Descendants,
inline-chain legs, fan-out children/carriers, revisits, and daemon-composed
cleanup work all launch through TASK context, so brief reconstruction cannot
withdraw this contract.

#### Canonical skill store + workspace symlinks (macOS and Linux)

As of TASK-4009/TASK-4012, skill materialization uses a **canonical skill store**
outside executor workspaces. Skills are built once into hash-addressed packages
and workspace entries are **validated relative symlinks** to exact approved
package versions under both `.claude/skills` and `.agents/skills` roots
(including Codex, Opencode, Pi, and mapped custom profiles).

**Supported platforms:** macOS (darwin) and Linux. Windows and unknown platforms
explicitly fail closed before launch/materialization with a named
`PlatformIsolationError`.

**Delivery model (same-owner):**

The executor and daemon share the same OS identity on macOS and Linux. Linked,
validated relative skill links live under BOTH ``.claude/skills`` and
``.agents/skills`` (including Codex, Opencode, Pi, and mapped custom
profiles). Every user-facing and executor-facing guidance surface names
both roots, never only the provider-selected root. Guidance is operational,
not a technical security boundary.

The executor runs under the SAME OS identity as the daemon — there is NO
OS-level isolation. An agent-controlled executor process can read, write,
chmod, or chown the canonical skill store and anything else the daemon
account can reach. A same UID may mutate, race validation, and affect
active/overlapping sessions. Integrity checks are DETECTION-ONLY,
FAIL-CLOSED behavior, not prevention. Do NOT call the target immutable,
protected, trusted source, or claim write/chmod/ACL denial, a security
boundary, or cross-agent isolation.

**Detection and refusal:** Before every executor launch (and at retry-time
before Popen/retry), every resolved package member's artifact bytes are
validated against the ledger-declared SHA-256 hashes. Both ``.claude/skills``
and ``.agents/skills`` root links are validated. A mismatched existing
canonical package is NEVER automatically rebuilt, copied, replaced, or healed
from same-UID local source. On mismatch, malformed/broken/malicious link,
or event-persistence failure, the daemon
emits a durable visible integrity event and refuses the session before
Popen/retry. First-ever materialization of an absent package remains
allowed; valid existing packages may be reused.

**Manual recovery only:** (a) For broken links: ``happyranch set-executor
<agent> --executor <current-executor>`` (re-materializes links only, NEVER
recovers corrupted bytes). (b) For corrupted canonical bytes:
``happyranch skills recover <slug> <version> <content_hash>`` — the sole
operator-invoked recovery path. Accepts only the eligible current B2 version,
validates its ledger provenance and every
declared member SHA-256 hash against the ArtifactStore before deletion;
refuses already-valid targets. The next materialization will rebuild the
package from the ArtifactStore. No automatic repair from same-UID local
source. This command can ONLY be used after an authoritative external
re-sync/redeploy of the release or custom artifacts has restored verified
artifact bytes outside the compromised same-owner local source — the
recovery route validates against ArtifactStore, which may itself be
corrupted if the same-UID executor previously tampered with it.

Policy withdrawal and atomic link repair remain safe.

**Ownership and provenance:**
- Canonical packages are content-addressed trees from exact verified
provenance/members for system, release-managed, and B2 custom-skill
version-pinned packages.
- The readonly hardening is cosmetic — the executor shares the daemon's uid
and can chmod files back to writable. Do not describe byte targets, local
sources, ArtifactStore, or links as OS-immutable, ACL-protected, trusted,
executor-only writable/unwritable, or automatically recovered.

**Integrity verification:**
Before each executor launch, the daemon compares actual canonical package
content against the ledger-declared member hashes:
- System-contract packages: compared against the shipped source tree hash.
- B2 custom skills: each member's actual hash compared against the
  ArtifactStore manifest.
On mismatch the daemon emits a durable integrity/operations event and
refuses the session. Corrupted bytes are NEVER silently accepted as valid
and NEVER automatically rebuilt, copied, or healed from same-UID local
source. The ArtifactStore is NOT a trusted or immutable source — a
same-UID process may also tamper with artifact bytes. This is
detection-only with fail-closed refusal; it is NOT an attacker-independent
external attestation authority.

**Platform contract (macOS and Linux):**
- Both platforms use native POSIX relative symlinks, same-directory
``os.replace`` publication, cosmetic chmod hardening, and direct same-identity
``subprocess.Popen`` launch. Linux support assumes filesystems preserve those
semantics; missing primitives fail through the existing named refusal paths.
- The executor launches directly under the daemon's identity. The prompt
guard directs agents not to edit managed skill links and states that
integrity verification is not a security boundary.
- Ordinary directories, malicious/broken/external/wrong-version links, unsafe
targets, failed permission check, or repair errors fail closed and prevent
launch. Never recursively delete or follow attacker nodes.

**Link validation and repair:**
- Materialized links are validated relative symlinks resolving inside the
canonical store. Stale, broken, wrong-version, non-symlink, external, or
mismatched-hash entries are atomically repaired.
- Withdrawal removes only owned validated links, retains canonical packages.
- The full expected union is derived once per provider root:
  **system-contract links are unioned across all ordinary session contexts**
  (task, thread, wake, dream, schedule, bootstrap) and retained so a later
  single-context launch never withdraws a valid link belonging to another
  context.  **Release-managed and B2 custom-skill links remain policy-reconciled**
  and are withdrawn when the agent becomes ineligible, retired, or unassigned.

**Workspace containment (THR-190 PR-B):**

*Threat principal:* a SANDBOXED Codex/Pi agent with workspace-write can
pre-position symlinked workspace/provider/nested-skills paths — e.g.
``<ws>/.claude``, ``<ws>/.claude/skills``, ``<ws>/.agents/skills``, or deeper
nested entries — that point OUTSIDE the real workspace. On the next session
start the daemon's materializer must NOT follow those pre-positioned parents
when creating, replacing, or withdrawing skill links; following them would
write, unlink, or replace files OUTSIDE the real workspace under the daemon's
identity. An unsandboxed Claude session (which can already reach anything the
daemon can) is NOT the defended principal.

*Structural enforcement:* resolved-parent containment inside the REAL
(resolved) workspace is enforced at the actual lowest-level link writer
(``PlatformIsolation.create_relative_symlink``) immediately before each link
creation/replacement — not by a route manifest, caller convention,
lexical-only check, or one-time earlier validation:
- **No-follow dirfd walk.** Every path component of the link's parent below
  the resolved workspace root is admitted (and, where authorized, created)
  RELATIVE to its already-pinned parent directory fd in a
  component-by-component walk rooted at a pinned no-follow
  (``O_NOFOLLOW``) fd for the REAL workspace root: ``os.open(part,
  O_RDONLY|O_DIRECTORY|O_NOFOLLOW, dir_fd=parent)`` /
  ``os.mkdir(part, dir_fd=parent)``. A full pathname is never re-resolved
  or reopened after admission, so a symlink at ANY level — workspace-level
  provider dir (``.claude``), provider root (``.claude/skills`` /
  ``.agents/skills``), or a nested skills root — fails closed with a named
  ``escaped_parent`` error, and a same-UID swap of an already-admitted
  ancestor cannot redirect any later step. Missing components are created
  as genuine directories anchored to the pinned parent.
- **Pinned-fd mutation.** The final parent fd is retained through the ENTIRE
  mutation — mkdir, stale temporary-parent/temp-link cleanup, temporary-
  symlink creation, ``os.replace`` repair, and withdrawal ``unlink`` — so
  every same-UID ancestor-swap window is closed: the write/unlink/replace
  is bound to the admitted inode, never re-resolved through a pathname.
- **Contained withdrawal, admission, and enumeration.** ``withdraw_workspace_link``
  and ``admit_skills_directory`` apply the same component-by-component dirfd
  walk; ``admit_skills_directory`` returns the ADMITTED directory fd,
  retained open, and repair enumerates the skills root ONLY through that
  admitted fd (the full pathname is never re-resolved to list after
  admission) — so no symlink swap or escaped parent can list, write, unlink,
  or replace outside the real workspace.
- **Ordinary workspaces unchanged.** Canonical relative symlinks wholly
  inside a normal workspace materialize and repair exactly as before.

*Failure ordering:* containment failures surface as named materialization
errors during the pre-spawn transaction, BEFORE executor construction or
launch — every session-start family (task, thread, wake, dream, schedule)
persists a terminal failure and returns without invoking the executor.

**Legacy compatibility fallback:** The legacy per-session copy model
(``_copy_skills_tree``, ``refresh_session_skills``, and the former
``_WHOLESALE_DUMP_ENABLED`` flag) is removed as an executable path. No
catch-and-copy or silent fallback survives. The canonical store + symlink
model is the sole production materialization path.

**Org context:** `{ORG_SLUG}` placeholders in canonical skill bodies are NOT
substituted. The org slug is passed to the child process via
`HAPPYRANCH_ORG_SLUG` environment variable from the authorized session/task
metadata. Existing multi-org commands receive a real existing-org slug.

**Release-shipped managed-catalog skills.** Bundled skills ship inside the
repo at `<project_root>/runtime/skills/<slug>/` and are read-only at runtime.
These are resolved via the `SkillRegistry` and unioned with system contracts;
release and system-contract slugs win on collision.

**THR-055 B2 custom skills.** Custom skills use `custom_skills`, immutable `custom_skill_versions`, eligibility rules/events, per-session materialization evidence, and custom-skill events. The only agent create path is `POST /api/v1/orgs/{slug}/skills/agent`, invoked by `happyranch skills create --from-file <package.json> --session-id <session-id> [--org <slug>]`. It is bearer-free, derives org/task/agent/session from the verified SessionTracker binding, returns `{skill, version, hidden_reason, provenance}`, and creates a default-hidden editable B2 record. Every verified agent may use it; founders configure eligibility later.

**Current-v2 logical purge (TASK-6143/TASK-6423).** Retirement remains reversible. A separate shared-bearer founder/human `POST /api/v1/orgs/{slug}/custom-skills/{id}/purge` accepts an exact `typed_slug` only for an already-retired skill and synchronously commits one additive tombstone. The commit reserves ID and slug forever and makes resolver, eligibility, mutation, restore, future canonical publication, both provider-root link publication, and serving fail closed. Identical later calls return the same tombstone (`already_purged=true`). The default `GET .../custom-skills/catalog` omits tombstones; `view=removed` returns tombstones only, rejects unknown values with 422, and does not reuse the separate general Skills source `filter`. Direct tombstone detail remains reachable by ID, and human create returns `409 slug_permanently_reserved` when the requested slug is held by a tombstone. `physical_erasure=false`: versions (valid and invalid), findings, rules/events, lifecycle/materialization evidence, authorship/provenance/audit, cached bodies/manifests, artifact objects, existing canonical packages, and historical/unregistered/relocated links remain byte-for-byte retained. Current rules are withdrawn only by setting `superseded_at`; their rows remain verbatim and an empty-policy eligibility event records the transition. Unsupported/partial schemas and FK-off connections refuse with `schema_contract_unsupported`; v0/v1 are not converted. Purge retains `retired_at`, so downgrade alone stays denied by every preserved old resolver. The explicit compatibility limitation is that resurrection on a pre-purge binary requires both operator downgrade and explicit restore of that exact record; the superseded-current-policy latch then produces `no_eligibility_policy`, while purge-aware restore rejects the tombstone permanently.

At the single unified canonical publication function, a process-local per-org re-entrant lock spans only the final authoritative tombstone/current-version re-read and both provider-root repairs. Purge takes the same lock before its synchronous `BEGIN IMMEDIATE` transaction. Lock order is the existing workspace materialization lock, then this per-org publication lock, then database reads; purge has no workspace lock. If purge committed during selection/build, the final read excludes the spec from the returned set and neither root can receive a new link; a concurrent current-version change also fails closed. Candidate construction, backend launch, subprocess/Popen, and session execution remain outside. There is deliberately no org-wide generation or launch fence, no callback/token plumbing in producers, supervisors, or executors, and no running-session revocation or wait. Existing canonical bytes and historical links remain retained; later materialization withdraws links through the ordinary reconciliation path.

**Supported SKILL.md authoring contract (THR-169, THR-210 PR 2).** Newly authored SKILL.md bodies are accepted when they are either (a) heading-first — the first line at column zero is a Markdown ATX heading (1–6 `#` markers followed by whitespace or end-of-line, CommonMark §4.2) — or (b) YAML-frontmatter-first: a valid opening `---` fence at column zero, a YAML mapping, a closing `---` fence, then a Markdown body heading (the same ATX boundary). One canonical shape validator (`runtime/skills/skill_md.py`, reached by every custom-skill write route through `_validate_skill_package`) enforces the grammar. Leading BOM/whitespace before either accepted opening shape is not tolerated — the accepted shapes must open the document at column zero (no silent healing). Malformed/unclosed/non-mapping frontmatter, frontmatter without a Markdown body heading, and bodies with neither a column-zero heading nor frontmatter — including hash-prefixed lines that are not ATX headings (e.g. `#not-a-heading`, seven-or-more hashes) — are classified invalid under the authoring contract and persisted as immutable validation/provenance evidence — they are not accepted as valid or materializable versions. Stored `validation_state` remains authoritative at the resolver/materialization seams: pre-PR-2 records — heading-first versions stored valid under the earlier contract, and PR-1-era heading-first candidates stored as invalid evidence — keep reading exactly as persisted, without rewriting or silent healing.

**Atomic version writes and invalid-candidate evidence (THR-210 PR 1).** `POST /api/v1/orgs/{slug}/custom-skills/{skill_id}/versions` (and the human/agent create paths) finish validation before any persistence. A candidate that satisfies the supported authoring contract is persisted atomically and advances `current_version_id`. A candidate that FAILS validation is appended as immutable validation/provenance evidence: exactly one version row (`validation_state=invalid`) with deterministic validator findings, author/task/session provenance where applicable, its content-addressed artifact, and the usual `created`/`version_saved` + `validated` events — it NEVER displaces an existing `current_version_id`, so a malformed edit cannot darken a working skill whose last valid immutable version still exists. The invalid candidate remains inspectable via catalog/detail/version history but is never eligible (`current_version_invalid`) or materializable. Initial creation with no prior version sets the FIRST version as the current pointer regardless of validity (there is no prior pointer to preserve, and a NULL pointer is unreadable by every JOIN-based list/detail consumer and uneditable through the version route); the skill then darkens as `current_version_invalid` until a valid successor advances the pointer. Version rows and events are never rewritten; every version's parent is the current version it was authored against. Protected-slug candidates remain hard-rejected (HTTP 409 — a policy gate, not evidence), as do missing-body/missing-metadata requests (HTTP 422). A byte-identical body conflicts with the append-only `UNIQUE (skill_id, content_hash)` invariant as HTTP 409 `version_content_exists` (THR-210 PR 3) with zero residue: the version INSERT fails before any artifact write, so no artifact is rewritten or created, no new version row and no new event/audit row appear, and `current_version_id` is unchanged — a replay can never mutate or extend the immutable history. The 409 translation is scoped strictly to that version INSERT: an integrity failure at any later persistence stage (artifact write, current-pointer update, either event append, or the commit itself) is not a duplicate — it returns HTTP 500 and compensates the artifact this request wrote. Append-only `(skill_id, content_hash)` uniqueness is never relaxed. Compensation for the content artifact stays armed through the transaction's final `commit()`: a persistence failure at any later stage — including the commit itself — rolls back every DB row and removes the artifact (and any now-empty parent directories) this request wrote, so a failed write never leaves durable residue.

**FAIL-CLOSED materialization.** Any error during materialization raises
immediately. A failed materialization must NOT leave a partially-populated
skills directory passing as complete. All five caller contexts (orchestrator
`run_step`, `thread_runner`, `wake_runner`, `dream_runner`, `schedule_runner`)
persist a database-terminal failure and return BEFORE executor spawn — a
materialization error in any spawn path blocks the agent launch, never silently
skipped.

**Process-local workspace serialization (Issue #536).** All pre-spawn skill
materialization for a given agent workspace — system-contract injection +
on-disk verification, managed-skill injection, and B2 custom-skill injection
— runs inside a single unified transaction (``materialize_workspace_skills``)
protected by a process-local ``threading.RLock`` (re-entrant lock) keyed by
the canonical (resolved) workspace path. The legacy wholesale copy
(``_WHOLESALE_DUMP_ENABLED`` / ``refresh_session_skills``) is permanently
removed. Concurrent task, thread, wake, dream, schedule, and
executor-switch/bootstrap callers targeting the same workspace serialize
their complete pre-spawn materialization. The three executor adapter
``_copy_skills`` methods (Claude, Codex, Opencode) and the set-executor
route's all-context materialization also participate in this lock boundary.
The lock is **process-local only** — it does not coordinate across daemon
processes. Cross-process protection for the same agent workspace relies on
the daemon's per-agent concurrency ceiling (at most one ``run_step`` session
plus one thread invocation per agent).

The RLock allows safe re-entrant use: when the executor-switch route
acquires the lock and calls ``ensure_workspace_ready``, the adapter's
``_copy_skills`` can re-acquire the same lock without deadlocking.

Per-file ``os.replace`` reader safety is preserved: a concurrent reader
(or an agent session already running in the workspace) always sees either
the complete old or complete new skill file, never a half-written one.
The lock serializes writers only; it does NOT block readers.

Named fail-closed behavior: if materialization fails for a real filesystem
error (disk full, permission denied, missing source), the error propagates
as a named exception (``SystemContractMaterializationError``,
or the underlying ``OSError``) — never
a bare ``FileNotFoundError``. The caller persists the terminal failure and
no agent subprocess is launched. Recovery requires fixing the underlying
filesystem/permission issue and explicitly re-dispatching.

 **Visibility only — NO capability change.** Skills govern which guidance
playbooks an agent sees. They grant no tools, credentials, network access,
filesystem access, sandbox policy, or permission-map/allow-rule/auth changes.

**Only founder-concern boundaries are restricted** (as defined in the org charter):
- No `git push` to `main` / production deploy
- No actions involving spend >$200 single or >$100/month recurring
- No raw payment card data storage (PCI-DSS)
- No publishing content touching political sensitivity

These guardrails are enforced by the agent's system prompt (in `CLAUDE.md` or `AGENTS.md`) and the orchestrator's post-session review — not by provider-specific deny rules. If an agent violates a founder-concern boundary, the orchestrator catches it and escalates.

### Full codebase access

All agents can clone the project's git repo into their workspace for read access to the full codebase. Repository clones are provisioned from the agent's authoritative `repos` frontmatter (`org/agents/<name>.md` — `AgentDef.repos`): founder enrollment, pending-enrollment approval, `POST /agents/init`, and `manage-repo add/update` call `ContextBuilder.clone_repo`, which clones a missing repo and runs `git pull --ff-only` when an existing clone is encountered. At session spawn (task, thread, schedule, wake, dream) the daemon only fast-forward-pulls existing `repos/*/.git` clones via `refresh_workspace_repos`; it never clones a declared-but-missing repo (fail-open). Agents can also pull on their own during a session.

Write restrictions are role-based but minimal:
- Dev Agent: can create branches, commit, push to feature branches (not main)
- Payment Agent: can create branches within `src/payments/**`, push to feature branches
- Product Manager: writes specs to workspace, no code commits
- Engineering Head: reviews only, no direct code changes

### Task attachment materialization at session spawn (THR-109)

When a task (or an ancestor it inherits from) has file attachments, the runtime
resolves them at session spawn by walking up the `parent_task_id` chain, unioning
any `task_attachments` rows found. The durable bytes are read from the private
task-attachment store (separate from the org-wide shared artifact store) and
written into a per-task session attachment directory under the agent's workspace
(`workspace/.happyranch/attachments/<session_id>/`). An `Attachments:` block is
injected into the brief prompt naming each file, its on-disk path, size, and
content-type hint. Delivery is by-path for all executors; image perception
depends on the executor CLI's own abilities. The materialized per-session
directory is a regenerable cache — the bytes of record live in the task-attachment
private store.

**Legacy rows.** Rows with non-`NULL` `legacy_status` (e.g. `duplicate_v1`) are
included in ancestor resolution and materialization. The collision-safe
materialized filename (`{storage_key}__{sanitized_display_name}__{id}`) uses
the immutable `task_attachments.id` row identity to produce distinct per-row
paths — legitimate duplicate legacy attachments do not overwrite each other
even when they share both `storage_key` and `display_name`.

### Executor abstraction

The executor interface supports four built-in adapters (`claude`, `codex`,
`opencode`, and `pi`) plus registered custom profiles. Every custom profile must
carry an explicit `command_adapter_id: custom-adapter:<id>` bound to one
registered, conformance-passed, founder-approved adapter. Executable identity,
SHA-256 checks, shared health/conformance routes, direct-connect projection, and
launch eligibility are server-authoritative. Generic command/argv templates and
omitted adapter identifiers are rejected; there is no silent conversion or
automatic/versioned fallback. Supported recovery is reassignment to a built-in
executor or ordinary re-registration of a valid approved custom-adapter profile.
Swapping an agent's registered profile remains a one-line `AgentDef.executor`
frontmatter change; workspace `agent.yaml` is no longer read (THR-095).

### Host-session admission and terminal cleanup ordering (THR-207 / TASK-5584)

A daemon-wide ``HostSessionSupervisor`` (``runtime/orchestrator/host_supervisor.py``)
owns admission and containment ordering for every top-level agent invocation.
The governing spec is ``docs/superpowers/specs/2026-08-24-host-resource-concurrency.md``
and the platform-neutral backend contract is ``runtime/platform/session_backend.py``.

**Load-bearing ordering invariants** (enforced by the supervisor core):

1. **No agent subprocess launches before admission.** ``backend.prepare`` /
   ``backend.launch`` and the executor launch body run only after an admission
   lease is granted. Queued cancellation removes the request with no
   launch/handle/lease leak.
2. **Every terminal path finishes containment before lease release**, success
   included, in the fixed order: freeze terminal result → collect receipt →
   backend finish (tree teardown + quiescence check) → capability-appropriate
   residue accounting/reconciliation → publish bounded receipt → release lease
   exactly once. Cleanup errors never replace the primary terminal reason.
3. **Cancellation goes through the opaque containment handle**, never a bare
   PID-only signal, and is idempotent with the executor's own finish.
4. **Policy snapshots are immutable per invocation** and are explicit startup
   inputs (healthy enforcement-capable global cap 13 over the 13-slot producer
   envelope: 6 task + 4 thread + dream + wake + schedule; Linux `<=11`
   non-binding shadow retained; macOS/no-enforcement 4 binding; low-single-digit
   measured cleanup grace), never host-derived permanent defaults.
5. **Ownership transfers atomically at admission grant.** The controller
   creates the ownership record under its lock and keeps it in its registry
   until lease release; the durable first-wins terminal reason lives on that
   record from grant; the daemon drain iterates the same registry. A shutdown
   that fires when or immediately after admission is granted freezes SHUTDOWN
   on the record, and the attempt's next gate observes it — refusing launch
   before any handle, or finishing containment exactly once if the launch was
   already committed. No reason- or window-specific special case.

**Wired producers (THR-207 / TASK-5584 real-caller amendment + task-producer
slice).** The daemon-wide supervisor is constructed in
``runtime/daemon/state.py`` with the capability-factory-selected backend and
run through the **real task producer** (``Orchestrator._run_agent`` — every
task session owns a real admission lease + cancellation token, transfers
atomic ownership at grant, launches through the selected capability backend,
registers an opaque cancellation/cleanup control with ``SessionTracker``
while the PID stays diagnostic/restart evidence only, and finishes
containment before exactly-once lease release on every terminal path — the
supervisor's terminal hook clears the control/PID/session on the final path
after finalization and before lease release, ownership-safe against a newer
session of the same (task, agent); PID diagnostics and opaque cancel
controls are generation-versioned by session_id — a superseded invocation's
late registration is rejected and its terminal cleanup is a no-op, so
cancellation always resolves the currently active generation's control/PID
and never invokes an old/already-terminal token) and
**BOTH executor Popen launch bodies** (``executors._run_command`` and
``CustomAdapterExecutor.run`` receive the backend-created ``RunningHandle``
and communicate/parse only — no self-Popen, no ``on_started``, no internal
429 retry; the supervisor owns the 5/15/45 finish/release/sleep/reacquire
with the original enqueue age and a fresh backend handle). The schedule
producer (``runtime/daemon/schedule_runner.py``) was wired in Slice A with
an honest no-enforcement ``PassthroughBackend`` (all capabilities
unavailable); with the executor bodies now wired it launches the real
argv through the selected backend too, and its honest-passthrough branch
disables the executor's internal 429 retry so the supervisor is the single
retry owner (no launch multiplication). Thread/dream/wake producers run
through the same daemon-wide supervisor (THR-207 producer wiring):
``thread_runner.run_invocation``, ``dream_runner.run_dream``, and
``wake_runner.run_wake`` each own a real admission lease + atomic ownership
at grant, launch through the selected capability backend, finish containment
before exactly-once lease release on every terminal path
(finish → residue → publish → release), and a daemon drain/cancellation that
interrupts a producer leaves its row for the existing daemon-restart recovery
(threads reaped/replaced, dreams ``recover_running_dreams``, wakes
``recover_running``) instead of settling it. Thread-specific semantics are
preserved: the Claude session-not-found eviction fallback and the THR-071
no-callback nudge re-invoke run as additional supervised phases, each
publishing its own honest bounded receipt. The app-lifespan drain calls
``supervisor.shutdown()`` in the app lifespan finally before producer
workers are cancelled. ``runtime/platform/isolation.py``
(canonical-skill-store integrity + same-owner launch) is layered beneath the
supervisor and is unchanged by this design.

**Slice B (THR-207 / TASK-5637) ships the real backends behind the capability
factory** (``runtime/platform/backend_factory.py``):

- ``runtime/platform/process_census.py`` — portable identity-safe descendant-
  tree census + sampler (OS-shipped ``/proc`` / libproc only, no dependency):
  start-identity PID-reuse rejection, zombie-aware liveness, sampled
  RSS+CPU+process peaks, inter-sample gaps, ``unavailable`` never rendered
  as zero.
- ``runtime/platform/linux_systemd.py`` — real Linux systemd/cgroup-v2
  backend: ``probe`` creates/verifies/tears down a transient scope (applied
  limits, membership, counters, empty-cgroup teardown, no residue);
  ``launch`` runs the target into a per-session transient scope under the
  aggregate ``happyranch.slice``; ``finish`` **explicitly stops the whole
  scope on every terminal path, clean success included**, escalates to
  ``KILL`` within the measured grace, and verifies **cgroup emptiness**
  (unit ``inactive`` alone is never quiescence — a TERM-resistant member can
  linger in the cgroup after the main PID exits). Quiescence is
  **fail-closed**: an unreadable ``cgroup.procs`` or an errored unit-state
  interrogation is UNKNOWN evidence that never yields ``CLEAN``/``quiescent``
  (the receipt stays ``INCOMPLETE`` with explicit ``cgroup_procs_unreadable``
  evidence so admission blocks). Verified residue is reported as
  guaranteed-cleanup residue (admission-blocking). Counters are captured
  **while the scope is alive**: a per-session exit-watcher opens
  ``memory.peak``, ``cpu.stat`` ``usage_usec`` and ``pids.peak``
  **independently** — an absent old-kernel ``pids.peak`` disables only
  that counter, never the guaranteed memory/CPU capture, and never invents
  provenance — and is woken by a deterministic exit notification (pidfd
  poll, with a ``waitid(WNOWAIT)`` fallback; no polling cadence for the
  exit itself) at the process-exit instant (systemd collects the transient
  scope within ~0.3–0.6 ms of the contained process exiting — structurally
  before ``finish`` runs on a clean-success path, so a finish-time read is
  too late) and carries the immutable observation through wait/reap and
  drain/cancellation/cleanup into the receipt with KERNEL provenance;
  final-read validity is tracked **per counter** — when a counter's
  exit-instant read loses the collection race, that counter's retained
  last-live value is downgraded to ``sampled`` provenance (never silently
  labeled the authoritative final total/peak merely because another
  counter's final read succeeded) and the receipt records a precise
  per-counter ``capture_final_read_lost:<counter>`` event. ``finish``'s own pre-stop read
  is the authoritative fallback when the process is still running (user
  cancellation / daemon drain). ``pids.current`` is only a best-effort
  live count — merged with sampled evidence under ``sampled`` provenance
  when ``pids.peak`` is absent, never labeled authoritative (an empty-tree
  teardown value of 0 must not masquerade as a kernel peak). A cgroup that
  has **vanished** at finish time is recorded as an explicit
  ``cgroup_vanished`` event and only yields CLEAN/quiescent when
  corroborated by a positively-terminal unit state (an UNKNOWN
  unit-state interrogation still fails closed to INCOMPLETE); a missing
  cgroup can never short-circuit to a silently-verified clean or a
  fabricated kernel measurement. An absent counter falls back to the
  sampled peak with ``sampled`` provenance only when a sample exists,
  otherwise it is ``unavailable`` — never a fabricated value.
- ``runtime/platform/macos_process_group.py`` — honestly capped macOS
  backend: process-group launch, TERM/KILL bounded cleanup with
  group-ownership proof before signaling, identity-safe escaped-descendant
  survivor census (a ``setsid``-escaped child is censused, never falsely
  claimed clean), sampled-provenance peaks. ``finish`` runs its **own fresh
  final identity-safe descendant census** (never the last periodic snapshot)
  so an escaped descendant created after the last sample is detected by the
  shipping finish seam; a census/measurement exception propagates as
  explicit failure evidence that blocks admission — it never collapses into
  an empty clean group. ``limits_*`` stay ``unavailable`` — no
  Linux-equivalent descendant-tree controller exists on macOS.

The supervisor retains a **cardinality-bounded** sample history per attempt
(dropping the oldest past the bound) so the bounded receipt's serialized
sampling gaps stay bounded; the truncated prefix's elapsed span is preserved
as the truthful leading gap, so cadence is never presented as continuous
or gap-free truth.

**Receipt observability (THR-207 observability slice).** The daemon wiring
(`runtime/daemon/state.py`) binds the supervisor's `publisher` seam to one
process-wide, thread-safe, **bounded in-memory** `HostSessionStore`
(`runtime/daemon/host_session_store.py`). The EXISTING bounded operator
surfaces consume it additively — no schema migration, new route, dependency,
or config change:

- `GET /api/v1/metrics` (bearer-authed; `compose_metrics_snapshot`) gains a
  `host_sessions` block: bounded receipt aggregates + a newest-first recent
  window (per-receipt `memory_peak_bytes`, `cpu_total_seconds`, `process_peak`
  each WITH their provenance — `kernel`/`sampled`/`unavailable` — plus
  cleanup status/duration, quiescence, residue counts, gap/event summaries)
  AND the supervisor's live admission/backpressure view (cap, active, queue
  depth, oldest wait, head stall reason, shutdown, cumulative counters),
  residue gate/census, and cached capability probe. The block is persisted by
  the existing periodic writer (additive; snapshot format marker unchanged;
  legacy rows stay readable).
- `GET /api/v1/health` (unauthenticated liveness) gains a **bounded,
  non-sensitive** `host_sessions` block only when the supervisor is wired:
  aggregates and admission/backpressure counts yes; per-receipt detail,
  censused survivor identities (PIDs / start identities), and the backend
  probe evidence string (a failed probe can embed raw exception text) are
  dropped; the stable backend classification stays observable.

**Slice C bounded enforcement + receipt attribution (THR-207).** Real
supervised sessions get the founder-approved **fixed initial Linux
enforcement policy** (`runtime/platform/enforcement_policy.py`) — an
immutable per-invocation envelope selected deterministically from the
existing `AdmissionRequest.invocation_kind` and applied **only** by the
healthy Linux systemd/cgroup-v2 backend at `launch`: task sessions
`MemoryHigh=14G` (soft throttle) / `MemoryMax=24G` (hard ceiling) /
`TasksMax=1024`; thread/dream/wake/schedule (and any unknown kind,
conservatively) `MemoryHigh=2G` / `MemoryMax=4G` (exactly) / `TasksMax=1024`;
**no `CPUQuota`** is ever emitted for real sessions (the probe keeps its
deliberately tiny probe-only values and they are never confused with session
policy). Properties are emitted as exact byte integers and **verified as
applied** byte-for-byte in the scope's cgroup (`memory.high` / `memory.max`
/ `pids.max`) at launch — a mismatch fails the launch closed, never a
silent best-effort claim of guaranteed limits. Selection is immutable
across 429 retry/reacquire. macOS stays honestly capped/best-effort (no
limits applied); the passthrough/unsupported/degraded backends remain
explicit about unavailable enforcement. Receipts also carry **bounded
attribution** (`invocation_kind` + `executor_profile`, sourced only from
existing `AdmissionRequest` data) populated honestly by every backend at
`finish`; the store aggregates by the fixed canonical invocation-kind
vocabulary (unknown kinds fold into one `other` bucket — no dynamic
attribution keys) and the authed recent window carries the bounded kind +
length/character-redacted executor profile, while the unauthenticated
`/health` never exposes per-receipt attribution (the recent window stays
dropped).

**Boundedness:** at most 64 receipts retained (oldest dropped); aggregate
maps keyed by the fixed terminal-reason / cleanup-status vocabularies (and
the fixed canonical invocation-kind vocabulary for attribution), never by
session/org/task identity; survivor-identity list and evidence/event
strings truncated; peak aggregates grouped **per provenance** (kernel values
never blended with sampled); `unavailable` values counted, never rendered as
fabricated zeros. **Publication failure is operationally contained** at the
supervisor's `finalize_once` seam: a raising publisher is logged and never
replaces the primary terminal reason, never disrupts the cleanup ordering
(finish → residue accounting → publish → release), and never leaks the
admission lease (released exactly once; the `on_terminal` hook still fires
with the real outcome).

Callers above the factory branch on **capabilities**, never OS names; the
factory is the single OS-name site and even it selects by operational probe,
never by OS/version strings. Unsupported or unhealthy environments select the
honest no-capability fallback (``PassthroughBackend``). The daemon's wired
schedule, task, thread, dream, and wake producers all launch through the
selected capability backend (the executor bodies and the producer seams are
wired); the real backends are exercised by real integration suites gated on
the operational probe (explicit skip reason thereafter).

### Executor binary-path resolution (THR-085 / THR-107 seq155)

1. **Machine-local registry** — for non-custom-adapter profiles, consult the
   per-host binary-path registry at
   `<daemon-home>/executors.json`. The executor name (e.g. `claude`) is the sole
   resolution key (THR-107 seq155 hard no-PATH cutover).  If the name is
   registered, validate the stored path: it must exist and be executable.
   - **Valid** → use the stored path.
   - **Invalid (stale path)** → raise an **actionable block** that names the
     kind, the stale path, and the fix (`happyranch executor-binaries register <kind> --path <absolute-path>`). No silent
     fallback to PATH.
2. **Not registered** → raise an **actionable block** naming the kind and
   the fix (`happyranch executor-binaries register <kind> --path <absolute-path>`).
   **Never discover, resolve, or auto-pin a PATH executable** (THR-107 seq155).

**Custom-adapter profiles** resolve the adapter executable directly from
the approved adapter entry — its absolute path, SHA-256 hash, version, and
contract version are verified at construction time and re-verified before
each ``Popen``. Missing, tampered, non-regular, or non-executable adapters
fail closed.

The actionble block is an `ExecutorBinaryBlocked` exception (subclass of
`RuntimeError`). It always names the specific executor kind and gives the
operator a concrete command to fix it — never an opaque `rc=143` or bare
ENOENT death.

**Why a separate `executors.json` file?** The binary-path registry is
machine-local and must be writable at runtime by the `/api/v1/executor-binaries/register`
route (master-bearer-authed, for manual operator use) and by
`/api/v1/executors/runtime/register-binary` (scoped-token loopback, for
built-in agentic CLI self-registration — THR-088). Keeping it in a dedicated file under `<daemon-home>` isolates runtime
writes from `config.yaml` (which holds Settings values that may be under
version control or shared across hosts). This is distinct from the THR-052
executor profile registry (`org/config.yaml`), which describes *which*
executor kinds and capabilities exist and is org-portable.

### Bundled CLI PATH resolution (THR-085)

When the daemon is running as a PyInstaller-frozen bundle inside the Mac app,
the bundled `happyranch` CLI binary sits alongside `happyranch-daemon` inside
`Contents/Resources/daemon/`. The daemon MUST ensure that bare-name
`happyranch` invocations by agentic executors resolve to this bundled binary
— not to a stale `~/.local/bin/happyranch` left over from a previous install.

**Detection mechanism.** The ONLY signal available to the Python daemon is
PyInstaller's canonical frozen-detection flag: `getattr(sys, 'frozen', False)`
is `True` when running as the frozen bundle. (The Swift-side
`PACKAGING_MODE=bundled` environment variable is deliberately stripped by
`EnvironmentSanitizer` before the daemon child process launches, so the
Python daemon never sees it.) When frozen, `sys.executable` is the bundled
`happyranch-daemon` at `Contents/Resources/daemon/happyranch-daemon`, so
`os.path.dirname(sys.executable)` is the directory that also contains the
bundled `happyranch` CLI.

**Resolution rule.** At daemon startup, during PATH normalization:

- **Frozen (bundled Mac app):** Prepend `os.path.dirname(sys.executable)`
  (the bundled CLI directory) at the very front of the executor child's PATH,
  *before* the standard tool directories (`/opt/homebrew/bin`,
  `/usr/local/bin`, `~/.local/bin`). This ensures bare-name `happyranch`
  resolves to the bundled binary and beats any stale `~/.local/bin/happyranch`.
  The prepend is idempotent — repeated normalization does not duplicate the
  directory.
- **Not frozen (dev/headless/CI):** No change. The bundled CLI directory is
  NOT injected. PATH resolution stays exactly as today — the existing PATH
  `happyranch` (e.g. from `~/.local/bin` in `_STANDARD_TOOL_DIRS`) wins.

Because `_callee_env()` copies `os.environ` for child subprocesses, every
executor spawn inherits the normalized PATH with the bundled directory
leading when frozen.

### Spawn-Environment Invariant

Every runtime-created child subprocess — agent executor sessions (through
``_callee_env()`` in ``runtime/orchestrator/executors.py``), custom-adapter
launches, and job-script subprocesses (through ``_sanitize_child_env()`` in
``runtime/daemon/jobs_runner.py``) — inherits a sanitized copy of the daemon's
environment that **strips** the following variables:

- ``VIRTUAL_ENV`` — standard venv activation marker; its presence directs
  ``pip``, ``uv``, and other Python tooling to install into the venv.
- ``UV_PROJECT_ENVIRONMENT`` — uv project environment target override; can
  redirect ``uv sync`` / ``uv pip install`` away from the default ``.venv``.
- ``UV_PYTHON`` — uv ``--python`` selector: the interpreter into which
  packages are installed; can steer installation into the shared venv.
- ``UV_SYSTEM_PYTHON`` — uv ``--system`` flag: installs into the system
  Python environment instead of a managed venv.

These variables are stripped because the daemon process itself typically runs
inside the shared canonical HappyRanch venv.  If an agent executor or job
script inherits ``VIRTUAL_ENV``, a bare ``uv sync`` or ``uv pip install -e .``
executed from a **disposable worktree** would rewrite the shared venv's
editable-install ``.pth`` file to point at the worktree instead of the
canonical source checkout.  When the worktree is removed, every agent using
that venv loses the ability to import the ``cli`` and ``runtime`` packages.

**Preserved variables:** ``PATH`` (including the daemon-normalized standard
tool directories), ``HAPPYRANCH_ORG_SLUG``, and all other ``HAPPYRANCH_*``
runtime variables.  No unrelated configuration is blanket-removed.

#### Worktree Rule (hard)

**Never run** ``pip install -e .``, ``uv pip install -e .``, or
``uv sync --active`` from inside a per-task worktree when the inherited
environment carries the shared canonical venv.  These commands rewrite the
shared ``.pth`` entry and break every agent using that venv.

Instead, create an **isolated worktree-local venv** before installing:

```bash
python3 -m venv .venv-local
source .venv-local/bin/activate
uv pip install -e .
```

#### Recovery (secondary)

If a stale ``.pth`` has already broken the CLI, prefix every invocation with
the canonical source checkout on ``PYTHONPATH``:

```bash
PYTHONPATH=/path/to/canonical/happyranch happyranch <args>
```

This is a non-destructive workaround — it does not modify the ``.pth`` file
or run ``pip``/``uv``.  Use it for one-off recovery; the permanent fix is to
restore the editable install from the canonical checkout.

The ``happyranch doctor`` command (local, read-only, no daemon required) checks
whether the editable-install pointer resolves to the canonical source and emits
the exact repair command on failure.

---

## 2. Agent Memory Architecture

### Problem
Coding-agent sessions are stateless — context is lost when a session ends. Agents need to remember past work and learn from experience across sessions.

### Solution: persistent workspaces with file-based memory

Every agent has a **persistent workspace directory** that survives across sessions. The workspace contains the agent's memory files, any work products it creates (specs, code, proposals), and clones of the repositories declared in the agent's `org/agents/<name>.md` frontmatter. The orchestrator regenerates the executor bootstrap file (`CLAUDE.md` or `AGENTS.md`) and Claude settings when applicable at session start, but everything else persists.

```
workspaces/
├── engineering_head/
│   ├── CLAUDE.md or AGENTS.md   # Regenerated each session
│   ├── .claude/settings.json    # Claude-only permission config
│   ├── memory/                  # Per-entry store, persists across sessions (was learnings.md; LRN- ids resolve via permanent shim)
│   ├── task_history.md          # Rolling summary of last N tasks
│   └── repos/<name>/            # Clones of repos declared in org/agents/<name>.md (provisioned at enrollment/init; fast-forward-pulled at session spawn)
├── product_manager/
│   ├── CLAUDE.md
│   ├── .claude/settings.json
│   ├── memory/
│   ├── task_history.md
│   ├── specs/                   # Specs PM writes accumulate here
│   └── repos/<name>/
├── dev_agent/
│   ├── CLAUDE.md
│   ├── .claude/settings.json
│   ├── memory/
│   ├── task_history.md
│   └── repos/<name>/            # Agent works on branches here
├── payment_agent/
│   ├── CLAUDE.md
│   ├── .claude/settings.json
│   ├── memory/
│   ├── task_history.md
│   ├── proposals/               # Payment change proposals
│   └── repos/<name>/
└── ...
```

> **Org-root portability (THR-187 Slice A).** The *only* workspace content
> that is portable across a same-slug relocation is
> ``workspaces/<agent>/memory/**``. ``task_history.md`` (rebuilt from the DB),
> ``repos/<name>/`` clones, regenerated bootstrap files, injected settings/skills,
> caches, task-output directories, and every other workspace byte are
> non-portable and named as exclusions by the preflight classifier.
> ``runtime/portability/roots.py`` is the authoritative exhaustive direct-org-root
> classification (allow / named exclusion / reject); see 05c-orchestrator
> §Organization portability.

**Daemon-managed workspace cleanup scheduler (THR-195 seq 129).** Cleanup is a
**daemon-managed, system-default capability** independent of all user
Schedules. The daemon runs a periodic loop
(``runtime/daemon/workspace_cleanup_scheduler.py``, registered in
``runtime/daemon/app.py``) that measures EACH AGENT's own workspace on a
bounded, fail-open budget and, per agent, when the weekly occurrence is due
and unserviced, no prior cleanup task of that agent is non-terminal, the
seven-day per-agent cooldown has elapsed, triggers an ordinary root task
ASSIGNED TO THAT OWNING AGENT with a **daemon-composed brief** that
packs the fresh measurement as **ADVISORY** context at trigger time. It never
uses, creates, or modifies a Schedule, never injects anything into the shared
session-prompt seam (``protocol_doc_manifest``), and never performs cleanup
itself. Ordinary task, thread, wake, dream, and Schedule-spawned sessions are
byte-identical to a runtime without the feature.

A bounded timeout, error, or cap/truncation result that makes measurement
unavailable bypasses only numeric threshold evaluation, so otherwise-due
spawning continues with honest unavailable advisory context. Of available
numeric results, only an available numeric result below 1 GiB skips
(founder-approved threshold, TASK-6036).

The packed block is advisory sizing context ONLY. It is **stale on arrival**,
is **not an eligibility list** and **not a candidate list**, labels no path
safe, and recommends no removal — every path and fact must be re-derived
independently and immediately before any action. It contains only aggregate
measurements/status for the owning agent's workspace: ``measured_at``;
total and largest workspace sizes; registered-worktree counts joined to task
status (``TASK-\d+`` prefix match handles suffixed worktree names like
``TASK-5567-base691``; unknown or missing tasks are unclassified, never
assumed terminal); dependency-directory (``node_modules``/``.venv``)
counts/sizes including the inside-``.claude/worktrees`` split; and live
sessions by agent from ``SessionTracker``. It never enumerates paths and
never uses pending jobs or ``blocked_on_job_ids`` as liveness. Measurement
is explicitly bounded (one wall-clock deadline shared across every
subprocess — each git call receives ``min(per-call cap, remaining)`` with
expiry re-checked after every call and after the last repository — plus
entry/depth/workspace/repo/worktree caps) and fail-open: every timeout,
error, or cardinality-cap hit yields an explicit ``measurement unavailable``
advisory note and can never block daemon operation or task/session spawning.

Cadence and trigger policy: weekly (Sunday 03:30 in the org's effective
timezone). The live scheduler evaluates an occurrence once when its scan
cursor crosses that boundary, so polling phase and bounded processing drift
cannot skip it. On startup it evaluates only the current weekly window once;
there is no earlier historical backfill across daemon lifetimes.
Below-threshold state therefore
emits one ``workspace_cleanup_skipped(workspace_below_threshold)`` audit at a
meaningful weekly/cooldown boundary, never once per minute for the rest of an
unserviced week. Measurement-unavailable fails open around the numeric
threshold gate, so an otherwise-eligible task spawns with unavailable advisory
and trigger-audit context; an available numeric below-threshold result skips
and remains audited. The other exceptional/fail-closed trigger skips remain
explicitly audited. A
decision-level task-history lookup failure before trigger entry creates no
cleanup task and emits exactly one
``workspace_cleanup_skipped(history_indeterminate)`` row for the crossed
boundary; adjacent non-boundary scans remain silent. The
policy also preserves one run at a time (a later occurrence fires only after
the preceding cleanup task of that agent is terminal), a seven-day per-agent
cooldown, and a per-agent >= 1 GiB workspace-total threshold. The first TWO
triggered runs per agent are **STRICTLY report-only** (TASK-5552 §6
rollout: inventory and nothing else); from run #3 the daemon composes the
approved TASK-5552 §4 fixed normalized cleanup brief (bounded, Git-aware,
non-force, action-time-re-derived eligibility). The advisory block itself
never authorizes removal in either variant. The responsible agent reports
results to the founder in ONE durable founder-visible thread PER AGENT (fixed
per-agent subject, created by the daemon on first trigger with the owning
agent as composer/participant and @founder as recipient) via the existing
participant-authorized, task-bound ``happyranch threads send`` path — NO
minted report token. The first-two-run counter, the cooldown, and the
per-agent thread identity are persisted through existing durable mechanisms
only — the owning agent's daemon-marked cleanup task rows via an
authoritative SQL-side ``brief`` prefix filter (no bounded scan of ordinary
tasks can hide older cleanup rows; a lookup failure is represented as
indeterminate and FAILS CLOSED for triggering) and ``threads.composed_from_task_id``
provenance plus the fixed per-agent subject, participant membership of the
owning agent, open status, and the daemon's distinctive opening message (a
fixed subject alone is not identity). The thread-identity lookup is
tri-state (found / absent / indeterminate): only an authoritative absence
may create the thread; any lookup error fails closed — no duplicate thread,
no task, no enqueue — with an audited reason (TASK-6046). On the first
trigger the report thread and the cleanup task are created in ONE atomic
transaction (rollback leaves ZERO residue across every affected durable
table; nothing is enqueued; a later retry succeeds exactly once).
No schema/API/CLI/auth change. The
task id is allocated atomically at insertion (never before the awaited
measurement), and workspace enumeration is paged in bounded batches so every
registered agent is reached. A kill switch (``workspace_cleanup.enabled``
in the org ``config.yaml``, default true) disables the capability per org.
See 05c-orchestrator §Daemon-managed workspace cleanup scheduler for the
full contract.

**Temporary-filesystem inode advisory (THR-200).** Workspace measurement adds
fail-open `statvfs` inode used/free/total/percent/threshold fields, and full CI
performs a matching inode preflight. Both are advisory and non-authoritative:
they never grant cleanup authority or cause the daemon scheduler to inspect,
move, quarantine, restore, unlink, or recursively delete `/tmp` content.
The first producer-redirection unit roots executor-owned language/package
caches under
``workspaces/<agent>/.happyranch/cache/{xdg,uv,pip,npm,node-compile,go-build}``
and requires disposable review/QA/scratch Git worktrees under
``workspaces/<agent>/.happyranch/scratch/worktrees``. These paths are
workspace-owned and measurable, but ``.happyranch/cache`` is not cleanup-eligible
and this unit grants no new cleanup authority. Cache-root
setup refuses symlinked child components and fails closed without falling back
to shared ``/tmp``; same-UID post-validation races remain because the
workspace is not an OS isolation boundary. The daemon still never inspects or
reclaims ``/tmp``.
Small atomic daemon callback payloads retain their explicit ``/tmp`` contracts.

THR-195 B1 adds a dormant, production-unreferenced engine whose executor
accepts only immutable final ledger rows derived from canonical manifests plus
explicit complete lifecycle, liveness, 60-second newest-mtime, and current-boot
coverage evidence. It uses fd-relative no-follow same-device removal, exact
allocated-byte/inode accounting, and manifest/lock/parent/sibling postconditions.
No production path imports it; teardown/scheduler wiring, activation, live
deletion, deployment, and legacy backlog eligibility remain absent.
Runtime-launched task-agent and job subprocesses instead receive one canonical
mode-0700 root at
``<workspace>/.happyranch/task-tmp/<canonical TASK-N>`` through ``TMPDIR``,
``TMP``, ``TEMP``, and a runtime sidecar variable. The runtime rejects a
containment-variable/sidecar change between preparation and launch, preserves
all unrelated inherited environment, and refuses traversal, ambiguous task
suffixes, symlink substitution, or non-directory roots before spawning.

Each launch atomically records a bounded v1 observation under
``.happyranch/task-scratch-manifests/TASK-N.json``: required versus observed
root/ownership, producer kind/id, and explicit ``regenerable_scratch`` root
versus ``durable_recovery_artifact`` manifest classification. The reader is
report-only and reports missing/corrupt/stale state without repair. The
manifest is never deletion authority. TASK-6501 ships no teardown removal,
periodic eligibility expansion, deletion driver, backlog mutation, or deploy;
the later teardown design must combine THR-090 lifecycle evidence with fresh
fail-closed OS cwd/open-file/process checks, while Git/repository evidence
remains a skip rule.

### Three layers of memory

**1. Institutional memory (knowledge base)**
Shared across all agents. Org charter, SOPs, brand guidelines, partner directory, regulatory summaries. Read-only for most agents, write access scoped per role.

**2. Agent-specific memory (memory store)**
Each agent accumulates its own operational learnings. The Content QA records "DSAL website is more reliable than MGTO for Macau visa info." The Content Writer records "always show Octopus + AlipayHK side-by-side on HK transport guides — tourists usually only know one." These files persist across sessions and are loaded as context at session start.

After each task, the orchestrator prompts the agent: "Based on this task, are there any new memory entries to record?" Responses are appended to the memory store. Over time, when the store gets long, the orchestrator periodically asks the agent to consolidate and prune it.

Entries are addressed as `MEM-NNN`. Items migrated from the prior learnings store keep a permanent `LRN-NNN` alias so historical cross-references resolve forever. The audit trail is forward-only: new events log as `log_memory_*`; historical `log_learning_*` rows are never rewritten.

**3. ~~Performance memory~~ (REMOVED 2026-05-27)**
The 30-day rolling scorecard / tier classification was removed. The audit log (`review_verdict` rows after every delegated child terminates, plus completion / failure events) is sufficient for the founder to identify which agents need attention — via `happyranch audit`. A `review_verdict` row's verdict is a distinct fact from the child's completion status: an explicit structured `verdict` reported by the child (a free-string workflow value such as `APPROVE`, `PASS`, or `REQUEST_CHANGES`) is preserved verbatim; only when no structured verdict is present is `approved`/`rejected` inferred from the completion status. The legacy `scorecards` table is no longer created on fresh DBs.

### How context gets assembled at session start

The orchestrator regenerates the bootstrap document in the agent's workspace with:

```
1. System prompt (from 02/03-system-prompts-*.md)
2. Org charter summary (from 01-org-charter.md — key sections only)
3. Pointers to persistent files (memory/, task_history.md)
4. Team health summary (generated by orchestrator)
5. Task-specific context (brief, prior drafts, QA feedback, etc.)
```

The agent's persistent files (memory entries, prior work products) are already in the workspace — the bootstrap document just references them. At session spawn the daemon fast-forward-pulls each existing `repos/<name>/` clone (`refresh_workspace_repos`, fail-open); it does not clone repositories that are declared in `AgentDef.repos` but missing from the workspace — provisioning of missing clones is handled by enrollment, `POST /agents/init`, and `manage-repo`.

### Write-back protocol

After each session completes, the orchestrator:
1. Extracts the completion report (`completion_report.json` written by the agent)
2. Checks for new memory entries and appends to the memory store
3. Writes a `review_verdict` audit row for delegated work so the founder can audit per-agent outcomes via `happyranch audit`. The audit verdict is a distinct fact from completion status: an explicit structured `verdict` reported by the worker is preserved verbatim (a completed worker that reports `REQUEST_CHANGES` carries `REQUEST_CHANGES`, not `approved`); only when no structured verdict is present is the implicit `approved`/`rejected` mapping from completion status applied. Missing/blank/unknown verdicts are normalized at dashboard read boundaries and never counted as accepted.
4. Appends to `recent_tasks.md` with a summary of the task
5. Logs everything to the audit trail (SQLite)
6. Does NOT clean up the workspace — files persist for future sessions

---

## 3. Agent Lifecycle and Scheduling

### Principle: agents are not always running
Agents are not persistent processes. Running 12 agent sessions continuously would burn LLM credits and produce nothing — most agents are idle most of the time. Instead, the orchestrator manages agent lifecycles: spinning up sessions when there's work, and tearing them down when the task is done.

### Three operating modes

#### Mode 1: On-demand (most agents, most tasks)
The orchestrator spins up an agent session only when a task is assigned. The session starts, the agent completes the task, submits its completion report, and the session ends. Between tasks, the agent does not exist as a running process.

**Lifecycle:**
```
Task arrives in queue
    │
    ▼
Orchestrator assembles context (system prompt, memory, task brief)
    │
    ▼
Orchestrator spawns agent session (via configured executor)
    │
    ▼
Agent works on task (minutes, not hours)
    │
    ▼
Agent submits completion report
    │
    ▼
Orchestrator extracts output, logs results, writes back memory
    │
    ▼
Session terminates — agent no longer running
```

**Typical session duration:** 1-5 minutes for most tasks. Complex tasks (Dev Agent implementing a feature, Compliance Agent running a full audit) may take 10-30 minutes.

**Which agents use this mode:** Content Writer, Content QA, SEO Agent, Dev Agent, Payment Agent, QA Engineer, Partner Liaison, Compliance Agent, and all 4 Manager Agents for their review/approval tasks.

#### Mode 2: Scheduled (recurring tasks on a cron)
Some work happens on a fixed schedule. The orchestrator's scheduler triggers these sessions at configured times. The session runs, completes its task, and shuts down — same as on-demand, but the trigger is a clock instead of a task queue.

**Scheduled tasks:**

| Schedule | Agent | Task |
|---|---|---|
| Daily 9:00 AM | Content Manager | Generate and send daily report to founder |
| Daily 9:15 AM | Product Manager | Generate and send daily report |
| Daily 9:30 AM | Ops Manager | Generate and send daily report |
| Daily 9:45 AM | CX Manager | Generate and send daily report |
| Every Friday | Content QA | Content freshness audit — flag guides older than 90 days |
| Every Monday | SEO Agent | Weekly keyword ranking report |
| 1st of month | Compliance Agent | Monthly regulatory scan across 3 jurisdictions |
| 1st of month | Ops Manager | Monthly partner SLA compliance review |
| Weekly Monday 10:00 AM | Orchestrator (not an agent) | Generate and post weekly org summary to the dashboard |

Each scheduled task is configured in the orchestrator's scheduler (a cron-like system). Missed runs (e.g., Mac Mini was off) are handled by a catch-up mechanism: on startup, the orchestrator checks for missed scheduled tasks and runs them.

#### Agent Todos (THR-105): agent-owned scheduled work

Agent Todos are persistent Schedule records stored in the ``schedules`` SQLite
table. Each agent may own up to 20 armed schedules; the org cap is 100. Every
Schedule carries a ``normalized_brief`` (what fires) and a ``source_instruction``
(the natural-language instruction the manager originally provided, preserved
for audit/reconciliation).

**Kinds.** Three kinds are supported:

- **One-shot** — fires exactly once at a specified UTC ``fire_at`` (max 90 days
  out), then transitions to ``fired`` (terminal).
- **Weekly** — fires every week on a single weekday + HH:MM local time + timezone.
  After each fire the schedule re-arms with the next occurrence and continues
  until either the founder cancels/pauses it or it reaches its ``expires_at``
  (default 90 days from creation). Indefinite weekly schedules (``indefinite=1``,
  founder-set only) have no expiry.
- **Recurring** — uses the bounded daily/weekly/monthly/yearly rule grammar.
  The server computes the first occurrence and immutable local ``anchor_date``;
  a native create or ARMED/PAUSED founder edit may request a canonical local
  ``start_date`` phase, which the server validates against the rule and DST,
  derives to ``fire_at``, and records only as the managed anchor.
  timing-only edits preserve that anchor, while cadence-shape edits reset it to
  the newly computed next occurrence's local date. A native recurring PATCH
  that changes recurrence and/or timezone may omit ``fire_at``; after merging
  and validating the rule, the server derives and persists the next eligible
  occurrence. A supplied ``fire_at`` remains an exact-match assertion against
  that server-computed occurrence. The recurring editor may explicitly send
  null for inactive ``byday``, ``bymonthday``, and ``ordinal`` selectors; the
  service removes those accepted clears after merge before validation and
  persistence, leaving canonical stored rules without stale/null selectors.
  DAILY/YEARLY remain selector-free and the bounded MONTHLY grammar is
  unchanged. One-shot and weekly PATCH semantics are unchanged.

**Founder controls and validation.** Founder/operator routes may pause, cancel,
edit, or ``POST /schedules/{schedule_id}/renew`` an ARMED or PAUSED Todo.
Normal renewal resets its 90-day review window; ``{"indefinite": true}`` grants
indefinite review authority. Renewal never changes cadence, anchor, next fire,
or dispatch count, and rejects FIRING, terminal, and EXPIRED rows with
``state_conflict``. Recurring-create validation returns stable 422 codes:
``invalid_freq_fields``, ``invalid_byday``, ``monthly_selector_missing``,
``monthly_selector_conflict``, ``invalid_interval``,
``anchor_date_not_settable``, ``invalid_start_date``, ``invalid_until``, ``invalid_count``,
``end_condition_conflict``, ``invalid_time``, and ``invalid_timezone``.

**Fire mechanism.** The schedule fire is a two-stage pipeline:

1. **Scheduler (daemon loop).** A 60-second tick scans all orgs for ARMED
   Schedule rows whose ``fire_at <= now`` (one-shot) or ``fire_at`` is within a
   120-second tolerance window (weekly/recurring). For a stale repeating
   occurrence (missed during daemon downtime), the scheduler does not replay or
   backfill it. It records ``occurrence_missed`` and re-arms the row only when a
   future next occurrence exists within any finite review expiry. The terminal
   alternatives do not emit ``occurrence_missed``: a recurring rule exhausted by
   its inclusive ``until`` date becomes FIRED with ``end_reason=date_ended`` and
   ``schedule_fired``; a future candidate beyond the review expiry becomes
   EXPIRED with ``schedule_expired``; and an otherwise unexplained missing
   candidate becomes FAILED with ``error=recurrence_no_candidate`` and
   ``schedule_failed``. A claimed row transitions ARMED → FIRING and emits
   ``schedule_claimed``.

2. **Runner + spawn callback.** The schedule worker loop drains the
   ``ScheduleQueue`` and invokes the owning agent's executor with a dedicated
   schedule-fire prompt. The agent's single job is to call the
   ``happyranch schedules spawn`` callback exactly once. The spawn callback:

   - Accepts only FIRING Schedule rows (single-use, record-scoped guard).
   - Creates one root task from the stored ``normalized_brief``, targeted to the
     owning agent on its own team.
   - Records ``spawned_task_ids`` and increments ``fire_count``.
   - Resolves one-shot → FIRED/``one_shot_completed``. A recurring successful
     dispatch first increments ``fire_count``; count exhaustion is FIRED/
     ``count_exhausted``, then an exhausted ``until`` is FIRED/``date_ended``,
     then a next candidate beyond review expiry is EXPIRED, then a defensive
     missing candidate is FAILED/``recurrence_no_candidate``; otherwise it
     re-arms with the next ``fire_at``.
   - Writes ``schedule_spawned`` and ``schedule_completed`` audit log rows.
   - Enqueues the spawned task.

**Token usage.** Token usage for the schedule-fire executor session is recorded
under ``scope_type="schedule"`` and ``scope_id=<SCHEDULE-NNN>``, keeping it
separate from task-scoped token usage.

**Constraints.**

- The schedule's ``normalized_brief`` is the brief for the spawned root task —
  the schedule payload cannot choose the agent, team, or brief.
- Every Schedule targets a single agent on its own team (self-targeted).
- Cross-agent scheduling is not supported.
- Hidden / invisible schedules (not visible in the CLI ``list`` output) are
  not supported — every Schedule is visible to its owning agent.
- Weekly schedules never replay/backfill missed occurrences. A daemon restart
  after a missed slot advances the schedule to the next occurrence without
  enqueuing a fire job for the stale slot.
- A claimed weekly or recurring occurrence that fails or times out is audited
  but advances to the next occurrence and remains ARMED; one-shot failures and
  timeouts remain terminal.
- The spawn callback is the only fire path — no alternate trigger mechanisms
  exist.

**Arming (creating) schedules.** Agents create new schedules by calling the
``happyranch schedules create`` callback — a single-line invocation that POSTs
to ``/api/v1/orgs/{slug}/schedules``:

   happyranch schedules create --org <slug> --from-file <path>

The payload file is a JSON object with ``task_id``, ``session_id``, ``agent``,
``source_instruction``, ``normalized_brief``, ``kind``, ``fire_at``, and
optionally ``recurrence`` and ``timezone``.  The server enforces:

- **Self-target only:** the creating agent is resolved server-side from the
  active session (``task_id`` + ``session_id`` + ``agent`` validated against
  the in-memory SessionTracker).  The payload cannot choose another agent.
- **Explicit instruction only:** both ``source_instruction`` (the verbatim
  NL instruction) and ``normalized_brief`` (the structured normalized brief)
  are mandatory.  Natural-language-only arming (without normalization)
  is refused.
- **In-org availability:** every agent with a valid active session and a
  resolvable in-org team may create a self-owned Todo. Legacy
  ``scheduling.enabled_agents`` config is accepted as a no-op and does not
  authorize or deny creation.
- **Caps and defaults:** the 20-per-agent / 100-org-wide armed caps, 90-day
  one-shot horizon, weekly shape validation (single weekday + HH:MM + IANA
  timezone only), and 90-day recurring expiry are enforced at create time
  by the ``ScheduleService``.
- **Recurring callback grammar:** a native ``kind="recurring"`` request uses
  the documented `recurrence` object: ``freq`` is ``DAILY``, ``WEEKLY``,
  ``MONTHLY``, or ``YEARLY``; ``interval`` is positive; ``time`` and ``tz``
  are required; weekly requires distinct ``byday`` tokens; monthly has exactly
  one positive ``bymonthday`` or one ``byday`` plus named ``ordinal``; and
  daily/yearly permit no selector. Its end condition is exactly never (omit
  ``until`` and ``count``), inclusive local-date ``until``, or successful-
  dispatch ``count`` (never both). The agent must not set server-owned
  ``anchor_date``. Invalid recurring grammar returns its named stable 422 code;
  the agent must correct it only from the explicit instruction or ask, never
  approximate a different recurrence.

Arming is fully autonomous — no pre-arming founder approval step — but the
schedule is immediately visible in the founder/operator ``list`` and ``show``
outputs and carries a ``schedule_created`` audit row with ``task_id=<SCHEDULE-NNN>``.

#### Mode 3: Persistent (Support Agent only)
The Support Agent is the one exception. Tourists need real-time help and the response time target is under 5 minutes. Two approaches:

**Option A: True persistent session.** The Support Agent runs as a long-lived agent session that waits for incoming inquiries. Advantages: instant response, no cold start. Disadvantages: continuous LLM session cost, needs health monitoring and auto-restart.

**Option B: Fast on-demand with warm-up.** The Support Agent is spun up on-demand like other agents, but with optimizations to reduce cold start: pre-assembled context kept ready, a lightweight executor for simple queries, full executor only for complex ones. If 10-20 second startup is acceptable within the 5-minute response window, this avoids the cost of a persistent session.

**Recommendation:** Start with Option B (fast on-demand). Switch to Option A only if response time is consistently too slow or if support volume justifies the cost.

### Concurrency

The orchestrator controls how many agent sessions run simultaneously. On a Mac Mini, practical limits:

| Constraint | Guideline |
|---|---|
| Concurrent sessions | 2-3 max (LLM API rate limits, memory, CPU for executors) |
| Task queuing | Tasks beyond concurrency limit are queued and processed FIFO |
| Priority queue | Tier 1 escalations and founder-initiated tasks jump the queue |
| Session timeout | 30 minutes max — if an agent session hasn't completed, kill it and escalate |

This means if the Content Writer is drafting a guide and the Content QA needs to review something else simultaneously, both can run. But if a third task arrives, it waits in the queue. The orchestrator logs queue wait times — if tasks are regularly waiting, it's a signal to either optimize agent session speed or increase concurrency.

### Cost profile

With on-demand sessions, daily cost scales with actual work, not idle time:

| Phase | Estimated daily sessions | Estimated daily LLM cost |
|---|---|---|
| Phase 1 (Content Team only) | 5-10 sessions | $3-8 |
| Phase 2 (+ Product/Ops Teams) | 15-25 sessions | $8-20 |
| Full org (all 4 Teams active) | 25-40 sessions | $15-35 |

These are rough estimates assuming Claude Sonnet pricing. Actual costs depend on task complexity, revision rounds, and which executor is used. The dashboard's cost tracking page (Page 6) gives you real-time visibility.
### Daemon capacity staged-write surface (TASK-6281)

The Settings Daemon / Capacity surface is a daemon-wide resource navigated
from an org. A local operator must possess the existing shared daemon bearer;
that authorizes the request but cannot identify or verify a person. The server
therefore audits actor `daemon-bearer-holder`, never client identity. The only
writable pair is `queue_workers` plus `host_global_session_cap`, staged
atomically for next restart with environment precedence, stale-revision
rejection through one quoted strong HTTP `If-Match` validator, unrelated-key
preservation, and fail-closed durable audit-before-replace. One correlation ID
links that truthful `validated_write_authorized` authorization to an honest
terminal rejected, failed, succeeded, or publication-uncertain row. The rows
carry only the server-observed allow-listed pair, truthfully known revisions,
rationale, and safe provenance; exactly one success row exists per successful
save and none claims the next-start file was applied or a person was verified.
After a successful authoritative replace, any directory durability,
read-back validation, response-snapshot, or temporary-cleanup failure returns
`config_publication_uncertain`: the new bytes are authoritative, no unaudited
compensating replacement is attempted, and the operator must reload and inspect
before retrying. The error reports temporary-artifact state as `absent`,
`present`, or `unknown`; failed inspection or unlink never fabricates absence
or overrides publication. It is not
a generic YAML editor, does not apply live, and cannot restart the daemon.
The response derives the producer envelope/components from the running
topology and the effective admission cap/reason from the active supervisor
capability. Below-envelope values remain valid with an intentional-backpressure
warning; above-envelope values warn that admission capacity cannot create
producers.
> **TASK-6514 direct retirement:** Legacy template-based generic executor
> profiles are neither readable, writable, nor launchable. There is no
> deprecation window, migration command, receipt flow, auto-conversion, or
> versioned rollback. An omitted command adapter, the retired generic identity,
> and command/argv profile fields fail before registry/store/audit mutation with
> guidance to register and approve an adapter and bind
> `command_adapter_id: custom-adapter:<id>`. Recovery is ordinary reassignment
> to a built-in executor or ordinary re-registration of a valid approved custom
> adapter profile. The AdapterInput/AdapterOutput v1 approval, binding, hash,
> conformance, health, direct-connect, and server-authoritative eligibility
> contracts remain unchanged.

# Generic remote-job v1 contracts and dark persistence (S1-S2)

`runtime.remote_jobs` defines the pure, immutable version-1 packet vocabulary.
All objects reject unknown keys and invalid versions, enums, identifiers,
timestamps, caps, and digest encodings. Canonical bytes are compact sorted-key
JSON encoded as UTF-8 with Unicode preserved; unset optional fields are omitted
while an explicitly supplied null remains present. SHA-256 is computed only
over those bytes. A whole-job bundle binds selected runner/generation and
attestation/capabilities/network policy, workspace identity/generation and
agent owner, every declared phase script/interpreter/cwd/env/cap, and reuse and
observation policy. It is frozen after validation.

V0 supports only `full_content_sha256`; the bounded coarse-manifest proposal
is omitted. The policy digest is recomputed from the complete normalized
version, root sets, exclusions, bounds, and filesystem handling rules, so an
arbitrary asserted digest cannot rename a different supported policy.

The once-per-workspace-generation skip contract has four indivisible logical
parts: admitted `pre_run` digest; exact `(runner_id, runner_generation,
workspace_id, workspace_generation)`; exact versioned exclusions/observation
policy digest; and a fresh complete observation matching one durable successful
executed-setup receipt across every required reusable root. Required roots must
be observed and cannot have required descendants excluded. S1 represents this
policy but neither observes a workspace nor persists/authorizes reuse.

Stable reasons are the closed v1 taxonomy in
`runtime.remote_jobs.contracts.StableReason`; raw exception/diagnostic text is
never a stable reason. Primary selection is deterministic and ordered exactly:
invalid/stale fence or uncertainty; finalization/persistence safety; accepted
cancellation; earliest phase timeout/output cap; pre-run failure; workspace
observation failure/mismatch; run failure; post-run failure; success. Rejected
admission and founder rejection map to the preserved public `rejected` status;
other terminal reasons map to `failed`; no reasons maps to `completed`.
Subordinate receipts remain inputs/evidence and are not erased by selection.
`PhaseFinished` rejects cross-phase reasons, impossible skip/start/exit shapes,
reversed timestamps, cap overruns, non-derived receipt digests, and missing or
mismatched observation-policy identity on observation receipts. Script-phase
receipts require their admitted `PhaseSpec` validation context, while workspace
observation receipts require the admitted observation-policy digest context;
context-free direct construction cannot skip either binding.
`TerminalProposed` carries unique complete phase/finalization receipt links and
is valid only when every link exactly matches a supplied, canonically
revalidated `PhaseFinished` receipt and those receipts alone recompute its
status/reason. The immutable bundle derives the complete `(phase, ordinal)`
identity set and cardinality (including exactly one finalization); inconsistent
caller-supplied phase context, duplicate/reused digests, and extra/missing or
same-phase/different-ordinal evidence are refused.
`TerminalProposed` public validation requires both the admitted bundle and
canonical `PhaseFinished` evidence; omitting either rejects construction instead
of falling back to caller-asserted receipt summaries. Likewise, direct
`RemoteFrame` validation requires the admitted bundle for every admission-bound
frame type, so the public models and `parse_remote_frame` do not form competing
strict and lenient authorities.
`parse_remote_frame` is the shipping untrusted-input seam: every phase and
terminal frame requires the exact admission offer plus admitted phase
name/ordinal/digest context reconciled to that offer's immutable bundle, and all
envelope runner/generation/attempt/fence/lease facts are bound to it.
`PHASE_LOG_CHUNK` carries the admitted phase digest
like the other phase frames. S1 does not produce or persist that context.

S2 installs the approved additive SQLite persistence contract at every managed
`Database()` open. It contains `remote_runners` (including the sole active
certificate serial, SPKI fingerprint, and non-null certificate expiry),
`remote_runner_workspaces`,
`remote_job_attempts`, `remote_phase_receipts`,
`remote_pre_run_observations`, and `remote_protocol_frames`; there is no
runner-key history table. Exact partial indexes and composite workspace
foreign keys enforce live-workspace, live-attempt, identity, generation, and
four-part reuse isolation. A named staged marker and preflight shape validation
make absent, complete, and expected-partial stores converge while conflicting
tables, columns, keys, predicates, or index order fail closed. Five nullable
`jobs` linkage columns are additive; legacy rows receive no backfill and retain
their exact status, reason, blocked-job, and audit-scope semantics.

The TASK-6611 additive persistence correction also installs exactly one
`remote_runner_enrollment_challenges` table and its unconsumed/unrevoked expiry
index. Its six-stage `generic_remote_runner_identity_enrollment_v1` marker is
the first schema/data operation at `Database()` open. Before completion the
guard accepts only (a) the exact merged-S2 `remote_runners` parent, and only
with every runner-graph/identity table empty, or (b) the exact amended parent;
after completion only the amended parent plus exact challenge table/index is
accepted. The empty-S2 parent replacement is atomic, exposes inert test-only
interruption hooks immediately before the drop and after the rename inside
that transaction, never invents certificate expiry, validates foreign-key
targets/checks, and leaves no temporary parent. Immediately before publishing
`complete`, canonical-shape validation and `PRAGMA foreign_key_check` run in
the same `BEGIN IMMEDIATE` transaction as the marker write; the post-commit
open check is defense in depth. Historical fixtures execute the authentic
initial-script-request commit `da539c3a…`, exact pre-jobs parent `4b73416a…`,
and immutable merged-S2 commit `1be72fe…`, rather than relabeling current rows.
Full-schema/all-row snapshots, two further reopens, systematic mutation of the
marker, all runner-graph tables, every inherited/new index and their columns,
constraints, FKs and predicates, marker/object constellations, and executable
bidirectional TASK-6611 requirement/assertion traceability preserve unrelated
values and the existing jobs/audit overloads.

There is deliberately still no shipping producer or consumer yet. The new
table, index, expiry field, and marker have no challenge lifecycle service or
S3 API/auth surface. Later slices must
prove producer completeness and implement runner authentication/enrollment,
transport, observation,
phase execution/finalization, coordination/terminal-before-resume, CLI/API/UI,
deployment, and activation. None of those behaviors, and no change to local
jobs or `JobStatus`, is claimed here.
