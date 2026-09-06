# Completion Contract

This document is the **canonical specification** of the universal completion-report format, the manager decision schema, and the agent-callback command list. The contract is identical for every agent except where explicitly noted.

The workspace bootstrap docs (`CLAUDE.md` / `AGENTS.md`) and the `start-task` skill (`.claude/skills/start-task/` or `.agents/skills/start-task/`) are the **operational** restatements that agents read at session time. They point back at this contract; they do not re-inline its body. When an agent's runtime behavior conflicts with this document, fix one of them — they must agree.

## Task completion report

When you finish a task, write your completion payload to `/tmp/completion-<task_id>.json` and call back via:

```
happyranch report-completion --from-file /tmp/completion-<task_id>.json
```

The `--from-file` form is mandatory across executors — multi-line `happyranch` invocations are blocked by the shared permission matcher.

Payload shape (required keys: `task_id`, `session_id`, `agent`, `status`, `summary`; everything else optional):

```json
{
  "task_id": "<the task_id from the prompt>",
  "session_id": "<the session_id from the prompt>",
  "agent": "<this agent's name>",
  "status": "completed",
  "summary": "<short prose summary of what you did>",
  "confidence": 85,
  "risks": ["<concern the reviewer should look at hardest>"],
  "dependencies": ["<work this depends on or blocks>"],
  "reviewer_focus": ["<which file(s) or aspect to review first>"],
  "output_dir": "output/<task_id>",
  "local_ci": {
    "command": "scripts/local_ci.sh all",
    "exit_code": 0
  }
}
```

`summary` is prose; the structured arrays (`risks`, `dependencies`, `reviewer_focus`) are first-class JSON keys, not subfields embedded inside `summary`. `confidence` is an integer 0–100 indicating how sure you are the work is correct (default 80 if omitted).

**Local-CI evidence (required for pushed PRs).** Any worker completion report
for a task that pushed a PR MUST include `local_ci` with the exact command
(normally `scripts/local_ci.sh all`) and a zero exit status. Engineering
managers reject a PR completion missing this evidence. The `local_ci` field is
a plain object with `command` (string) and `exit_code` (integer). If the local-CI
hook ran the full real suite, state its exact command and exit code; do not
claim it without output. Tasks that do not push a PR (e.g., blocked tasks,
analysis-only tasks) may omit `local_ci`.

**GitHub CI is authoritative.** Local pre-push hooks provide feedforward
signal only. The full Python 3.12/3.13/3.14 matrix and nightly integration
runs on clean Ubuntu runners in GitHub Actions are the only merge gate.
Local-CI hooks CANNOT prevent `git push --no-verify` — `--no-verify`
bypasses hooks entirely and remains prohibited by engineering policy.

For review/QA-type workers, optionally include a structured verdict:

```json
{
  "task_id": "...",
  "session_id": "...",
  "agent": "senior_dev",
  "status": "completed",
  "confidence": 92,
  "summary": "Code review complete. All 7 verification rows green...",
  "verdict": "APPROVE"
}
```

`verdict` is a free-string field. Each team's workflow KB entry documents the allowed values (e.g., engineering uses `APPROVE | REQUEST_CHANGES | BLOCK` for reviews; `PASS | REVISE | BLOCK` for QA). Omit when not applicable. Inline delegation chains (see `decision.then` below) use this field to gate auto-advance.

### Merge-evidence contract (guarded merge; THR-204)

The guarded-merge engine (`runtime/daemon/pr_ci_merge.py`, `_recall_fetch_verdict`) extracts merge evidence from a persisted completion report — the structured top-level `verdict` field and the prose `Verdict:` lines in `summary`/`output_summary`. This contract is canonical for that extraction; it supersedes the earlier KB rule `guarded-merge-verdict-extraction` (which required a strict single-line, annotation-free grammar).

**Canonical vocabulary.** Every extracted token — structured or prose — must be one of the canonical tokens `APPROVE | REQUEST_CHANGES | BLOCK | PASS | REVISE | FAIL`. This is the full shared review/QA producer vocabulary (never narrowed to the passing tokens): review persists `APPROVE | REQUEST_CHANGES | BLOCK`; QA persists `PASS | REVISE | BLOCK`, plus the legacy persisted `FAIL`. Tokens outside the vocabulary (e.g. `APPROVED`, `pass`, `COMMENT`, `ready_for_review`) are unknown/malformed and fail closed.

**Structured evidence is primary (non-null only).** A NON-NULL top-level `verdict` value must be a canonical non-empty token. Empty/whitespace-only strings, non-string non-null values, case-variant, unknown, or in-field-annotated values (e.g. `"REVISE — STRUCTURAL ESCALATION — …"`) are unusable structured evidence: extraction fails closed and never falls back to prose. Producers must persist ONLY the canonical token in `verdict`; human context belongs in the prose summary.

**Serializer-null legacy representation.** The durable recall producer (`runtime/infrastructure/database.py::get_recall_payload`) ALWAYS emits the top-level `verdict` key — `null` for legacy/no-structured rows (no persisted `task_results` verdict). Serialized `null` therefore represents ABSENCE of historical structured evidence, and the strictly parsed legacy prose candidate below MAY be used (exactly-one rule, full fail-closed grammar). An input that omits the key entirely is treated identically (direct-input compatibility).

**Prose grammar.** A prose `Verdict:` line is anchored at line start and carries exactly one canonical token, optionally followed by horizontal whitespace and a human annotation — e.g. `Verdict: PASS` or `Verdict: PASS — rationale`. Whitespace-separated annotations (em dash, parentheses, `/`, `for …`) are valid; tokens with attached punctuation (`PASS.`, `PASS—…`) are malformed. Newline-split `Verdict:\nPASS`, case variants, and non-canonical tokens are malformed. Exactly ONE candidate line is accepted; zero → missing evidence (fail closed); two or more (including duplicate same-token candidates) → ambiguous (fail closed).

**Agreement.** When both forms exist, their normalized canonical tokens must agree exactly; contradiction fails closed (no fallback, no equality bypass).

**Fail-closed summary.** Missing evidence (serialized-null or absent key with no valid prose candidate), role-invalid outcomes at the downstream gate, contradictory structured/prose evidence, malformed/unknown tokens, ambiguous or multiple `Verdict:` candidates, newline-split candidates, non-null unusable structured values, and any other genuinely invalid form block the merge. Serialized `null` unlocks ONLY the strict prose grammar — malformed, duplicate, contradictory, or newline-split prose candidates still fail closed. The role gates are unchanged: review must equal `APPROVE` and QA must equal `PASS` — known non-passing canonical tokens (`REQUEST_CHANGES`, `BLOCK`, `REVISE`, `FAIL`) parse correctly (from a non-null structured value or a valid prose line) and are then rejected by the role gate (`merge_guard_review` / `merge_guard_qa`). Malformed evidence instead fails the extractor and surfaces as `github_error`. There is no permissive unanchored scraping.

**Known grammar boundary (reconciled with durable evidence).** Live records show producers write bare tokens, `Verdict: TOKEN — rationale`, and whitespace-separated parenthesized/slash annotations; all are covered above. A small set of historical rows attach punctuation to the token (`Verdict: PASS.` / `Verdict: REQUEST_CHANGES.`) or embed annotations inside the structured field; those rows are genuinely invalid under this contract and fail closed — no grammar accommodation is made for them.

## Blocker path

Use `"status": "blocked"` when you cannot finish and need the orchestrator to route around you. Set `"confidence": 0` and put the blocker reason in `summary` — the orchestrator reads it verbatim when deciding the next step.

## Idempotent retry semantics

`POST /tasks/{id}/completion` is safe to retry for the *same session*. If a result for the exact `(task_id, agent, session_id)` was already recorded, the route returns `200 {"ok": true}` ("already recorded") instead of `409 unknown_session`, even though the in-memory session tracker was cleared by the first successful call. A retry whose `session_id` was **never** persisted still receives `409 unknown_session` — the `(task_id, agent, session_id)` triple is the authenticator, and all three must match a persisted result. Agents MUST treat a `200` (including an idempotent 200) as "landed" and MUST NOT record the callback as orphaned. A terminal task still returns `409 task_not_active`; verify task status in the DB before treating that as a failure.

## Completion-status lag (THR-211)

A `200` from the completion route means the result row is **durably landed**, not that `tasks.status` has transitioned. The POST persists the session-scoped `task_results` row and clears the tracker, but intentionally leaves the task row `in_progress` (block_kind NULL) until the orchestrator consumes the report at executor/session finalization (inline `run_step_impl` tail, daemon boot sweep, or ongoing zombie reaper). This is the **actual-vs-required contract**:

- **Actual:** after a successful POST the task may legitimately still read `in_progress` (with the landed result visible under `happyranch details` / `/tasks/{id}` / `/recall`) for the remainder of the live session. That is normal, not a lost callback — do not re-report or treat the callback as orphaned; the durable `task_results` row is the landing proof.
- **Required:** do NOT terminalize `tasks.status` inside the completion POST — early transition risks lifecycle/restart/session races. The deferred-transition design is load-bearing.
- **Dispatch/gate recognition:** the orchestrator's parent-dispatch seams (`_enqueue_parent_if_waiting` sibling-terminal check + chain-advance branch, `run_step_impl` delegated-parent eligibility) recognize a durably-landed structured terminal report during the lag window so a landed verdict is never masked by the deferred transition. The recognition authority is the exact `(task_id, assigned_agent, current_session_id)` triple (the boot-sweep/reaper fingerprint) with report status `completed`; it fails closed on prior-session/wrong-agent/blocked/unknown rows and never suppresses a genuinely-running task with no terminal result. The chain/gate consumer carries that exact authenticated report through `compute_advance_action` and the carrier verdict-mismatch check — an unrelated newer row (wrong agent or wrong session) can never advance or clear the chain, and a non-authoritative passing row can never substitute for the authenticated verdict. When the child carries the modern fingerprint but no acceptable exact report exists (exact-row miss, or an exact row that is malformed/unknown/blocked/nonterminal), the chain fails closed — it is cleared and the parent wakes, and task-wide evidence is never consulted for advancement; only a genuinely legacy child (no agent/session fingerprint) may use the unscoped newest-row fallback. Recognition is at-most-once (newest-leg gating + atomic `try_advance_chain` + claim CAS) — a landed verdict while the child row is still `in_progress` can never create a duplicate next chain leg or a re-attestation dispatch.
- **Task-status read surfaces** (`happyranch tasks`/`details`/`recall`, `/tasks/{id}`, web task detail) keep showing the honest row state: `in_progress` + the landed `results` list. Consumers that need the *outcome* read the structured durable result (or the guard-merge recall path), never a guessed terminality from the row status.

## Manager decision field (manager-only)

Team-manager sessions must additionally include a structured `decision` object alongside the prose `summary`. The orchestrator parses `decision` directly via the `NextStep` pydantic model — it never infers intent from prose. Workers omit `decision` entirely.

`decision.action` is one of:

- `"delegate"` — spawn a child task on another agent. Requires `agent` (target agent name) and `prompt` (child task brief). When re-delegating to an agent that has a FAILED child under this parent (e.g., retrying a failed fan-out slice), the field `revisit_of_task_id` is MANDATORY — it must carry the failed predecessor's task id so the orchestrator can track per-slice retry count from existing DB lineage (no schema migration). Omitting `revisit_of_task_id` in that context is a hard reject — the delegate is denied and the owner receives feedback to retry with the field set.
- `"done"` — terminal; the root task finishes here. Optional `summary` for a final outcome note.
- `"escalate"` — surface to the founder for resolution. Requires `reason`.
- `"fanout"` — spawn N child tasks in parallel (2 ≤ N ≤ 8). Requires `children` (array of `{agent, prompt}` objects). `width_cap_ack` is required and must exactly equal the child count. Optional `join_summary` (prose directive for the join prompt). Each child may optionally carry `then`/`expect_verdict` to run its own inline delegation chain — a *pipeline carrier* (Phase 2). A child targeted at a **team manager** is decision-capable (mutating fan-out, THR-056 msg39): it can return delegate-chain decisions that spawn implementation subtrees inside its branch. A child targeted at a regular **worker** is read-only (its structured decisions are ignored; it completes with a summary). A child that re-targets an agent with a FAILED child under this parent MUST carry `revisit_of_task_id` — the id of that FAILED child (same parent, same agent). A supplied link must reference a FAILED child of this parent assigned to the same agent; a missing or invalid `revisit_of_task_id` on ANY child rejects the WHOLE fanout fail-closed before any child is spawned (THR-078 — the same mandatory-retry-link rule as `delegate`). NO fan-out review gate of any kind (founder ruling THR-012 msg 129/131) — the width cap (8) is a machine-resource limit only; the real control over what lands is the per-PR merge gate: each mutating child opens its own PR needing reviewer APPROVE + qa PASS + CI + founder/EM merge.
- `"supersede"` — Its entire payload is exactly `{action, successor_brief, rationale, attestation}`; the two top-level strings are nonblank and `attestation` is strict/extra-forbid. The attestation requires a nonblank `recovery_reason` and `true` declarations for `policy_product_intent_unchanged`, `no_budget_or_external_commitment`, `no_permission_or_cross_team_change`, `no_schema_auth_security_privacy_or_data_access_change`, and `no_unresolved_founder_gate`. Missing, malformed, nested-extra, blank, mixed/override, or explicitly contrary declarations reject the decision before supersession mutation. This structural validation is **not proof** the declarations are true, cannot detect a valid-looking false declaration, and does not infer authority from prose. Managers MUST escalate policy/product intent, budget/external commitment, permission/cross-team, schema/auth/security/privacy/data-access, and unresolved Founder-gate concerns; that policy obligation is distinct from the retained audit evidence. The target, manager, session, and team are derived from the current claimed root server-side. It creates one pending same-manager/same-team successor and terminalizes the predecessor as `superseded` atomically, preserving the rationale, literal briefs, SHA-256 hashes, attestation evidence (including actor, session, and rule version), and bidirectional append-only provenance/audit rows. It is not a Founder escalation or approval, emits no Founder notification, and **rejects every thread-originated root** (`dispatched_from_thread_id` nonempty) until phase 2.

Thread callback note (THR-225): this decision schema is unchanged. Separately,
a pending manager-owned `purpose=task_followup` invocation may use the thread
dispatch callback once to create a true replacement root. The allowance is one
per persisted causal lineage, not one per turn; the replacement lineage can
never use it again. Admission, marker, thread message, and audit are one
transaction, followed by queue notification.

**Field-name note:** the child task's brief lives in `decision.prompt`, not `decision.brief`. The schema silently ignores unknown keys, so writing `"brief"` produces a child task with an empty brief. Use `"prompt"`.

Examples — same payload shape as a worker's, plus a top-level `decision`:

```json
{
  "task_id": "...",
  "session_id": "...",
  "agent": "<this agent's name>",
  "status": "completed",
  "summary": "Triaged the request; staging implementation work for dev_agent.",
  "decision": {
    "action": "delegate",
    "agent": "<target agent name>",
    "prompt": "<child task brief>"
  }
}
```

```json
{
  "task_id": "...",
  "session_id": "...",
  "agent": "<this agent's name>",
  "status": "completed",
  "summary": "Reviewed dev_agent's output and verified tests pass; root task complete.",
  "decision": {
    "action": "done",
    "summary": "<one-line outcome>"
  }
}
```

```json
{
  "task_id": "...",
  "session_id": "...",
  "agent": "<this agent's name>",
  "status": "completed",
  "summary": "Hit a budget threshold beyond my authority; surfacing to founder.",
  "decision": {
    "action": "escalate",
    "reason": "<why founder intervention is required>"
  }
}
```

### Inline delegation chains

A manager can declare a multi-leg workflow in one decision via `decision.then` (additional legs) and per-leg `expect_verdict` gates:

```json
{
  "task_id": "...",
  "session_id": "...",
  "agent": "engineering_head",
  "status": "completed",
  "summary": "Dispatching Item 1a small-feature gate chain.",
  "decision": {
    "action": "delegate",
    "agent": "dev_agent",
    "prompt": "Build Item 1a Gallery uplift...",
    "then": [
      {"agent": "senior_dev",  "prompt": "Code-review the PR described in prior-leg context.", "expect_verdict": "APPROVE"},
      {"agent": "qa_engineer", "prompt": "QA the PR described in prior-leg context.",          "expect_verdict": "PASS"}
    ]
  }
}
```

The orchestrator spawns the first leg, then auto-advances to the next leg on each child terminal whose `verdict` matches the leg's `expect_verdict`. Any mismatch (or `status=blocked`) clears the chain and wakes the manager. The final leg's match wakes the manager too — chains do not auto-`done`. Each subsequent leg's brief is auto-suffixed with a "Prior leg context" block (the upstream worker's summary + verdict + output_dir).

**Reviewer legs (THR-175).** Reviewer identities are configured per-org in the
`reviewer_agents` org setting (DB-backed, default `["code_reviewer"]`), not
hardcoded. Any chain leg whose `agent` is one of the org's configured
reviewer agents is a *reviewer leg* and MUST declare
`expect_verdict: "APPROVE"`. A reviewer leg that **omits** `expect_verdict` is
a **HARD REJECT** — the whole delegation is denied before any child spawns.
HARD REJECT is a recoverable authoring error: the owner receives a feedback
task result and a feedback orchestration step explicitly naming the required
`expect_verdict: "APPROVE"` remediation, the root stays PENDING, and the task
is re-enqueued for one corrected decision — it never fails the root and never
spawns a child. A delegate with a missing agent name or a missing workspace is
NOT recoverable and keeps hard terminal failure. At the execution seam, a
configured reviewer leg with a downstream leg
never auto-advances unless it returns an explicit `APPROVE` verdict: a missing
verdict or any non-approve verdict (`REQUEST_CHANGES` / `REVISE` / `BLOCK` /
equivalent) clears the chain and wakes the manager — QA/downstream is never
spawned. Ordinary verdict-less non-reviewer legs are unaffected.

Step-budget effect: declaring a chain consumes one orchestration step; auto-advances do NOT consume steps. A clean small-item workflow (`dev → senior_dev[APPROVE] → qa_engineer[PASS]`) costs 2 steps (declare + final wake) instead of 4.

Cross-team validation runs on every leg at decision-parse time; any off-team agent rejects the whole decision via the feedback mechanism.

See `docs/superpowers/specs/2026-05-30-inline-delegation-chain-design.md`.

### Task attachments in decisions (THR-109)

A manager may attach pre-uploaded task-attachment-store refs to spawned children via the optional `attachments` field. This field is available on:

- **Direct delegate:** `decision.attachments` — refs become the spawned child's own links.
- **Inline chain leg:** `decision.then[].attachments` — applied when the orchestrator auto-advances and spawns that leg.
- **Fanout child / pipeline carrier:** `decision.children[].attachments` — refs become the spawned child's own links. A pipeline carrier owns its declared refs; its spawned first leg receives them only through normal ancestor inheritance (never a duplicate link/claim).

**Shape:** `attachments: [{storage_key, display_name?}]`. The `storage_key` MUST reference a key previously uploaded via `happyranch tasks attach-upload --file <path>`. No ambient paths, upload-on-decision route, or public store exists.

**Security boundary:** the orchestrator only references keys that already exist in the private task-attachment store. It never accepts host paths, URL references, or inline file content in the decision contract.

**One-time claim semantics:** every `storage_key` is globally claimed at first link time. A duplicate, already-claimed, or missing key rejects the ENTIRE decision before any child is spawned, any parent park, any active_chain/active_fanout metadata, any link, any audit row, or any queue entry.

**Sibling duplicate rejection:** for fan-out, all refs across ALL children (including pipeline carriers) are validated together. Duplicate storage keys across siblings are a single invalid fanout.

**Validation reuse:** the orchestrator and the POST /tasks daemon route share the same validation semantics (count limit, duplicate detection, key existence, claim check, display-name sanitisation, content-type resolution).

**Inheritance:** existing parent→child attachment inheritance via `resolve_ancestor_attachments` is unchanged. Decision attachments are the child's OWN records, materialized at session spawn alongside inherited ancestor attachments.

**Example — direct delegate with attachment:**

```json
{
  "task_id": "...",
  "session_id": "...",
  "agent": "engineering_head",
  "status": "completed",
  "summary": "Dispatching with mockup attachment.",
  "decision": {
    "action": "delegate",
    "agent": "dev_agent",
    "prompt": "Implement the dashboard per the attached mockup.",
    "attachments": [
      {"storage_key": "upload-abc123", "display_name": "dashboard-mockup.png"}
    ]
  }
}
```

**Example — inline chain with later-leg attachment:**

```json
{
  "decision": {
    "action": "delegate",
    "agent": "dev_agent",
    "prompt": "Build feature X.",
    "then": [
      {
        "agent": "code_reviewer",
        "prompt": "Review the PR.",
        "expect_verdict": "APPROVE"
      },
      {
        "agent": "qa_engineer",
        "prompt": "QA the feature.",
        "expect_verdict": "PASS",
        "attachments": [
          {"storage_key": "upload-def456", "display_name": "test-plan.md"}
        ]
      }
    ]
  }
}
```

**Example — fanout with sibling attachment uniqueness:**

```json
{
  "decision": {
    "action": "fanout",
    "children": [
      {
        "agent": "dev_agent",
        "prompt": "Implement module A.",
        "attachments": [
          {"storage_key": "upload-aaa", "display_name": "spec-a.png"}
        ]
      },
      {
        "agent": "qa_engineer",
        "prompt": "Test module A.",
        "attachments": [
          {"storage_key": "upload-bbb", "display_name": "spec-b.png"}
        ]
      }
    ],
    "width_cap_ack": 2
  }
}
```

**Example — pipeline carrier (attachment on carrier, inherited by first leg):**

```json
{
  "decision": {
    "action": "fanout",
    "children": [
      {
        "agent": "senior_dev",
        "prompt": "Review and QA the feature.",
        "expect_verdict": "APPROVE",
        "then": [
          {"agent": "qa_engineer", "prompt": "QA pass.", "expect_verdict": "PASS"}
        ],
        "attachments": [
          {"storage_key": "upload-ccc", "display_name": "review-checklist.md"}
        ]
      }
    ],
    "width_cap_ack": 1
  }
}
```

### Fan-out (parallel delegation, Phase 1)

A manager can spawn N child tasks in parallel:

```json
{
  "task_id": "...",
  "session_id": "...",
  "agent": "engineering_head",
  "status": "completed",
  "summary": "Dispatching parallel read-only investigation across 3 agents.",
  "decision": {
    "action": "fanout",
    "children": [
      {"agent": "dev_agent",    "prompt": "Investigate module A"},
      {"agent": "qa_engineer",   "prompt": "Investigate module B"},
      {"agent": "product_manager", "prompt": "Investigate module C"}
    ],
    "width_cap_ack": 3,
    "join_summary": "Synthesize findings into a unified plan"
  }
}
```

Constraints: 2 ≤ N ≤ 8 (hard cap); `width_cap_ack` is required and must exactly equal the child count. No fan-out review gate of any kind (founder ruling THR-012 msg 129/131) — the width cap (8) is a machine-resource limit only; the real control over what lands is the per-PR merge gate. Each child may optionally carry `then`/`expect_verdict` (a *pipeline carrier* — Phase 2): the child runs its own inline delegation chain (`{agent, prompt, expect_verdict}` legs, validated like an inline `delegate + then` chain) and reaches a terminal state only after that chain completes, at which point it counts toward the parent's fan-out barrier. A child targeted at a **team manager** is decision-capable (mutating fan-out, THR-056 msg39): it receives `task_type='task'` so its delegate-chain decisions are parsed and can spawn implementation subtrees inside its branch. A child targeted at a regular **worker** is read-only (its structured decisions are ignored; it completes with a summary). The parent parks in `in_progress(delegated)` with `active_fanout` metadata and wakes once when all children (carriers included) are terminal. The manager receives a structured join context block with each child's outcome.

**Retry fan-out (THR-078).** When re-dispatching failed slices, a retrying child MUST carry `revisit_of_task_id` naming its FAILED predecessor — a FAILED child of this parent assigned to the same agent. A missing or invalid `revisit_of_task_id` on ANY child rejects the WHOLE fanout fail-closed before any child is spawned, exactly like a `delegate` retry. The field is only required for children that re-target an agent with a FAILED sibling — fresh children omit it:

```json
{
  "task_id": "...",
  "session_id": "...",
  "agent": "engineering_head",
  "status": "completed",
  "summary": "Retrying the failed slice alongside a fresh investigation.",
  "decision": {
    "action": "fanout",
    "children": [
      {"agent": "dev_agent", "prompt": "Retry module A slice.", "revisit_of_task_id": "TASK-042"},
      {"agent": "qa_engineer", "prompt": "Investigate module B"}
    ],
    "width_cap_ack": 2
  }
}
```

**Integration model (a).** Each mutating child opens its own PR. The parent join summarizes outcomes. Children own DISJOINT file sets (manager responsibility); shared-file convergence routes through a SERIAL follow-up delegate after join, never a fan-out child.

See KB `fanout-primitive-founder-ratification` and
`output/TASK-1101/native-fanout-phase1-refresh.md`.

## Completion blocked on an asynchronous external condition

A task whose requested outcome depends on an ASYNCHRONOUS EXTERNAL TERMINAL CONDITION — a long-running external job, a deploy, an external approval workflow, an external CI run — is NOT complete until that condition resolves. The task owner may not report `done` at an intermediate milestone (e.g., submission, handoff, or initiation) when completion requires the external system's terminal verdict.

The runtime primitive for waiting on external conditions is the existing jobs plus `waiting_on_job_ids` path:

1. The task owner captures the identity of the external artifact or process it must wait on.
2. The task owner submits a bounded poller job that monitors the external condition to a terminal verdict.
3. The task owner reports `status="blocked"` with `waiting_on_job_ids=["JOB-NNN"]`.
4. The task remains `in_progress(blocked_on_job)` until the job is terminal. The normal blocked-on-job resume path reinvokes the task owner with the job result.
5. On resume, the task owner inspects the job output. It reports `done` only if the job proves the external condition resolved successfully. Failure, timeout, or a missing/disputed result must produce a revise/fail/escalate decision — never a false completion.

The same self-block and resume path applies to a delegated subtask; it receives the job result before continuing its brief.

Do not infer external success from an intermediate signal. The poller job — not the task owner's session — reaches the terminal verdict; the task owner gates completion on that verdict alone.

Example: a task that must land a pull request waits on that PR's external CI through this path; the engineering-domain specifics (SHA-pinning, settle window, guarded-merge gates) live in the jobs skill and agent guides.

### Who emits a decision, and delegation scope

**Decision emitters:** Any agent that owns a `task_type=task` task must emit a `decision` field — not only `role: manager` agents. Conversely, an agent owning a `task_type=subtask` task is a leaf: it reports `status` + `output_summary` and omits `decision` entirely. The orchestration gate keys on `task.task_type`, not on agent role.

**Self-delegation (self-decomposition):** A non-manager owner may `delegate` only to **itself** — spawning the next sub-task in a sequence it owns and orchestrates, getting woken on each child terminal. Team managers may delegate to own-team agents or to themselves. Any attempt by a non-manager to delegate to a different agent is rejected with feedback; the task re-runs so the owner can revise its decision.

**S6a completion authority supersession:** For an eligible manager session carrying an active immutable team policy, semantic evidence is one strict closed `manager_self_evaluation` object in the authenticated completion. The daemon binds it to the immutable task-result, manager session, release/activation, contract, and effective manager provider/executor/model. Production launches no separate evaluator subprocess. Missing, malformed, extra-field, mismatched, stale, replayed, ambiguous, or low-confidence evidence fails closed with digest-only diagnostics. Workers and legacy/no-active-policy completions omit the object. This paragraph supersedes this section's older references to a production LLM evaluator; the remaining daemon fences and lifecycle text stays normative.

**S6b/S7 operator projection.** Only the currently allowlisted Engineering Manager surface may read bounded immutable policy history and self-evaluation outcomes. Outcomes expose durable pins and causal receipts only; incomplete joins say `receipt_incomplete`, never inferred success. Raw evaluation prose, prompt/policy bodies, and secrets are prohibited. Production activation is a separate, explicit founder-authorized operator act subject to the ordinary daemon authentication, allowlist, validation, immutable-linkage, idempotency, audit, and CAS fences; code landing or redeploy does not activate a policy.

**Escalation:** Only a **root** task (`task_type='task'`, no parent) escalates to the founder. A non-root subtask that would escalate instead **fails** and hands back to its parent; bounded failure-recovery (TASK-573) carries it up, and the root escalates if it cannot resolve.

**Pre-escalation authority hook (THR-181 Track A):** before a **current manager-owned Engineering root's** proposed escalation is committed, the orchestrator runs exactly one audited LLM authority evaluation of the proposed reason against the immutable, release-controlled Engineering policy `engineering/pre-escalation-authority@v1` (see `05c-orchestrator.md` §THR-181 for hook ordering, the fail-closed matrix, the audit denominator, and restart/CAS behavior). The policy is semantic authority only; server-owned mechanical fences are non-overridable and no policy output may override one. The evaluation snapshot carries authoritative **structured server facts** with provenance (fence outcomes, budget counters/ceilings, lineage, active-work/block/cancellation/session state, adverse child review verdicts, partial-work evidence, org permission digest, DB schema digest); a server-PROVEN must-escalate fact (adverse child verdict, partial-work evidence, DB-schema drift vs the release-pinned schema) forces ESCALATE regardless of the reason prose, and a permission/schema surface change during the attempt fails closed with the matched clause — neither a misleading nor an omitted reason can authorize CONTINUE_SAME_ROOT. CONTINUE_SAME_ROOT is additionally honored ONLY when the proposed reason is a **byte-exact member of the release-controlled closed routine set** (`CONTINUE_ACCEPTED_REASONS`), so the grant never depends on keyword classification or on the completeness/truthfulness of the untrusted reason for any protected boundary; the exact narrow permitted action (return the current root to pending for another manager decision step) is server-proven safe across every protected category. The only semantic results are `ESCALATE` (the exact existing escalation path) and `CONTINUE_SAME_ROOT` (the named same-root permitted action). Candidate identity is bound to the immutable `task_results` row id (restart replay cannot mint a second candidate/evaluation), and the final continuation CAS atomically re-validates the complete current fence set (identity, ownership/session/team, status, cancellation, block/active-work, lineage, budgets, partial-work, adverse child verdicts) at consumption time. Every ambiguity, error, mismatch, timeout, audit failure, cancellation, exhausted limit, stale/CAS conflict, restart-incomplete state, or successor/supersede/revisit/fresh-root signal fails closed to ESCALATE. Historical census eligibility is never consulted.

Every committed post-activation escalation also has exactly one denominator row. A runtime-raised mechanical escalation (for example retry-, revise-, or orchestration-step ceiling exhaustion) is not an authority decision: it atomically records `authority_hook.outcome=not_applicable`, a stable bounded reason code, and linkage to its committed `escalation` audit row, without fabricating candidate/evaluation rows or invoking the evaluator.

Thread-originated current manager roots are included. Their thread id/origin remain structured lineage provenance; thread origin is neither an initial eligibility failure nor a final continuation-consumption failure, and it does not relax same-root identity or the no-revisit/no-successor/no-supersession/no-fresh-root-replacement rules. Manager supersession's independent rejection of thread-originated roots is unchanged.

**Single-use continuation lifecycle envelope (founder ruling, implemented).** Every CONTINUE_SAME_ROOT commit atomically mints a single-use `authority_continue_envelopes` row bound to the evaluation/candidate, immutable causal `task_results` row, matched policy clause, manager/team/root/session, and policy identity (`active -> consumed | violated` exactly once). The envelope preserves same-root identity, replay, cancellation, restart, CAS, and terminal audit value. It is not an exact-action whitelist: the continued manager turn uses its ordinary configured executor permissions and its daemon-accepted completion follows normal manager-decision validation. Supersession, revisit, and fresh-root replacement remain outside the same-root grant; independent budget and protected-boundary fences remain non-overridable.

See `docs/superpowers/specs/2026-06-03-subtask-composite-task-design.md` for the design rationale.

## Mid-task learnings

Durable lessons go through:
```
happyranch learning --agent <you> --session-id <sid> --task-id <task_id> --text "..."
```

Cross-agent reference material — SOPs, partner-API quirks, founder rulings — belongs in the Knowledge Base (`happyranch kb add --from-file ...`), not in `learnings.md`.

## Other agent-side callbacks

| Command | Purpose |
|---|---|
| `happyranch report-completion --from-file ...` | End-of-task callback (mandatory). |
| `happyranch learning --agent ... --session-id ... --task-id ... --text ...` | Durable per-agent operational lesson. |
| `happyranch manage-repo {add\|remove\|update} --agent ... --repo-name ... [--url ...]` | Add/remove/update a repo clone in your workspace. |
| `happyranch manage-agent --from-file ...` | (Team managers only) enroll/update/terminate an agent within your own team. |
| `happyranch kb add --agent ... --from-file ...` | Contribute a knowledge-base entry. |
| `happyranch kb update <slug> --agent ... --from-file ...` | Update an existing entry. |
