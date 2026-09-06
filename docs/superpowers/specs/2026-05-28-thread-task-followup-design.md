# Thread Task-Followup Re-Invocation — Design Spec

**Date:** 2026-05-28
**Status:** Implemented. Revised 2026-07-27 (TASK-3604): daemon auto-revisit removed; opaque failures are terminal FAILED with no daemon successor.
**Origin:** Founder-reported gap on THR-002 (2026-05-28): manager replied "我会在本 thread 回贴附链接" after dispatching `TASK-005` / `TASK-007`, but the promised follow-up never arrived because nothing in the runtime re-engages the thread on task completion.
**Relates to:**
- `docs/superpowers/specs/2026-05-13-threads-design.md` — the threads primitive this extends; today thread invocations are message-driven only.
- `docs/superpowers/specs/2026-04-21-opc-revisit-design.md` — the explicit human revisit primitive that can create a successor chain.
- `protocol/skills/thread/SKILL.md` — the agent-facing skill that gained a new `task_followup` turn shape.

## 1. Goal

When a task dispatched from a thread reaches its **true terminal state**, the runtime injects a structured system message into the thread and re-invokes the dispatching agent so it can compose the follow-up reply it promised. The bridge between `dispatched_from_thread_id` (written at dispatch) and the thread's invocation channel is built explicitly; no more silent broken promises.

## 2. Motivation

THR-002 evidence (family org, 2026-05-28):

- Seq 1 (founder) → Seq 3 (family_manager, `reply`): *"已派单 TASK-005 …报告完成上传后我会在本 thread 回贴确认"*
- Seq 4 (founder) → Seq 6 (family_manager, `reply`): *"已派单 TASK-007 …报告完成并上传到你指定的 Drive 文件夹后，我会在本 thread 回贴附链接"*
- `TASK-005` and `TASK-007` both reached `completed` (DB) within minutes.
- No seq 7+: the manager's thread turn ended at seq 6 (its single-use invocation_token was consumed by the reply), and the dispatched task ran in a separate session that finalized without any thread linkage.

Root cause (verified in code):

- `tasks.dispatched_from_thread_id` is **written** at dispatch (`src/daemon/routes/threads.py:899`) and **read by nothing** afterwards — there is no `get_tasks_by_thread` query, and `src/orchestrator/run_step.py` has zero thread handling.
- Thread invocations are minted only on message-driven triggers (founder send, agent reply triggering others, compose) — never on task terminal.
- The only existing system `kind_tag` values are `archived`, `task_dispatched`, `participant_added`, `turn_cap_extended`, `archive_requested`; there is no `task_completed` / `task_failed`.

The manager's promise is reasonable behavior for an LLM that doesn't know how thin its substrate is. The fix is to make the substrate honor the promise rather than tell the manager to stop making it.

## 3. Non-goals

**Out of scope for v1:**

- Backfilling follow-ups for already-terminal tasks on existing threads (THR-001, THR-002). The hook fires only on future terminal transitions. A one-off `happyranch threads backfill-followups` is left as a possible follow-up.
- A persistent task↔thread agent (the "Open" item 18 in CLAUDE.md). This spec adds one bounded re-invocation per dispatched task, not a long-running participant.
- Changing the dispatch authority rules. Cross-team / participant / token semantics are unchanged.
- Surfacing intermediate task progress in the thread. Only the **single** terminal event posts back; mid-task heartbeats stay in `happyranch progress` / audit.
- Allowing the followup agent to chain a new dispatch in the followup turn (see §6.4).

## 4. Fire predicate

The helper `_maybe_post_thread_followup(orch, task_id, status, auto_revisit_spawned)` is invoked at run_step's terminal-transition sites. Since TASK-3604 removed daemon auto-revisit, `auto_revisit_spawned` is always `False` in current production; the parameter is retained for compatibility. The predicate fires iff:

| Terminal status | `cancelled_at` | Action |
|---|---|---|
| `COMPLETED` | n/a | **Fire** |
| `FAILED` | `None` | **Fire** (terminal FAILED; no daemon successor exists since TASK-3604 — recovery is explicit manager/founder action) |
| `FAILED` | set | **Fire** (founder-cancelled — surface in thread record) |

In every "Fire" case, the helper still no-ops if the chain has no thread linkage (§5) or the thread is not OPEN (§7).

**Call-order constraint:** `_maybe_post_thread_followup` may be called before or after `_enqueue_parent_if_waiting` — they are independent; placing it after matches the existing reading order.

## 5. Finding the originating thread

Revisit roots (founder-initiated only since TASK-3604 removed daemon auto-revisit) do **not** copy `dispatched_from_thread_id`. The founder `/revisit` route only copies `session_timeout_seconds`. Rather than change insert semantics (risk: every future revisit caller has to remember to propagate), we walk backward at trigger time.

**Lookup algorithm:**

1. `chain = db.walk_revisit_chain(task_id, direction="backward")` — earliest predecessor at the end of the returned list.
2. `original = chain[-1] if chain else <current task>`.
3. If `original.dispatched_from_thread_id is None` → no-op (not a thread-dispatched chain).
4. `thread_id = original.dispatched_from_thread_id`; `original_task_id = original.id`.

**Dispatcher identity** comes from the existing `task_dispatched` audit row, keyed by `original_task_id`. The dispatch route already writes `AuditLogger(db).log_thread_dispatch(thread_id, task_id, dispatcher, …)` (`threads.py:912`). The helper reads `dispatcher` out of that row. No new column on `tasks`.

If the audit row is missing (shouldn't happen; defense-in-depth), write `thread_followup_skipped(reason=dispatcher_unresolved, task_id, thread_id)` and return.

## 6. Hook behavior

Under `org.db_lock`:

### 6.1 Thread-state guard

```
thread = db.get_thread(thread_id)
if thread is None or thread.status is not ThreadStatus.OPEN:
    audit.log_thread_followup_skipped(thread_id, original_task_id, reason="thread_not_open",
                                      thread_status=<value>, task_status=<value>)
    return
```

No system message, no mutation. Symmetric with the existing `_send_thread_message_inprocess` 400 `thread_not_open`. Archived/abandoned threads are immutable from this code path; archiving threads are racing the finalizer and skipping is the safe call.

### 6.2 System message

Append a SYSTEM message via `db.append_thread_message`:

```
speaker = dispatcher
kind = ThreadMessageKind.SYSTEM
system_payload = {
  "kind_tag": "task_completed" | "task_failed",
  "task_id": <terminal task id, which may be a revisit root>,
  "original_task_id": <original dispatched task id>,
  "root_task_id": <chain root, same as original in v1>,
  "status": "completed" | "failed",
  "final_output_summary": <task.final_output_summary or "">,
  "final_artifact_dir": <task.final_artifact_dir or null>,
  "cancelled": <bool, cancelled_at is not None>,
  "revisit_chain_length": <len(chain)>,  # 1 if no revisits, >1 if revisited
  "revisit_task_id": null,  # always null since TASK-3604 removed daemon auto-revisit
}
```

`kind_tag`: `task_completed` when terminal status is `COMPLETED`; `task_failed` for everything else covered by §4 ("Fire" rows, status `FAILED`).

The renderers in `src/infrastructure/thread_store.py` and `src/daemon/thread_forward.py` get the two new tags. Rendering format (markdown):

```
**Task TASK-NNN completed** (chain root TASK-MMM)
{final_output_summary trimmed to 240 chars}
{artifact_dir if present}
```

```
**Task TASK-NNN failed** (chain root TASK-MMM){; founder-cancelled}{; revisiting as <SUCCESSOR> when revisit_task_id is present}{; no further revisits when chain ended without spawning}
```

### 6.3 Turn-cap auto-extend

The followup invocation consumes one turn. The pending-load helper is today module-private (`_pending_reply_load` in `src/daemon/routes/threads.py`); promote it to `Database.count_pending_turn_obligations(thread_id)` so both `run_step.py` and the existing send/compose routes can call it. The promoted helper counts pending invocations whose `purpose ∈ {REPLY, BOOTSTRAP, TASK_FOLLOWUP}` (the new purpose also increments `turns_used` on consumption — `CLOSE_OUT` continues to be excluded per the existing turn-cap rule). The three existing call sites in `threads.py` (send, compose, send-as-agent) switch to the promoted helper in the same change to keep the projection definition single-source.

Compute:

```
pending = db.count_pending_turn_obligations(thread_id)
projected = thread.turns_used + pending + 1
if projected > thread.turn_cap:
    db.bump_thread_turn_cap(thread_id, delta=1)   # new method
    audit.log_thread_turn_cap_auto_extended(thread_id, original_task_id,
                                            reason="task_followup",
                                            new_cap=thread.turn_cap + 1)
```

This is a deliberate, audited mutation. The founder set the cap assuming free-conversation budget; the followup is a known, bounded artifact of dispatch. Each followup can bump by at most 1.

### 6.4 Mint and enqueue the invocation

```
inv = db.mint_thread_invocation(
    thread_id=thread_id,
    agent_name=dispatcher,
    triggering_seq=<the seq returned from §6.2>,
    purpose=ThreadInvocationPurpose.TASK_FOLLOWUP,
)
audit.log_thread_task_followup_enqueued(
    thread_id, original_task_id=original_task_id, terminal_task_id=task_id,
    dispatcher=dispatcher, invocation_token=inv.invocation_token,
)
```

Outside the lock, `await org.thread_queue.put(ThreadJob(org_slug=slug, invocation_token=inv.invocation_token))`. Lock release before async I/O matches the existing send route.

A new enum value:

```python
class ThreadInvocationPurpose(StrEnum):
    REPLY = "reply"
    BOOTSTRAP = "bootstrap"
    CLOSE_OUT = "close_out"
    TASK_FOLLOWUP = "task_followup"     # new
```

**Existing callbacks' compatibility:**

- `reply` (`threads.py:713`) and `decline` (`threads.py:770`) today require `purpose ∈ {REPLY, BOOTSTRAP}` via `_validate_invocation_token`. **Both lists must be extended to include `TASK_FOLLOWUP`** so the dispatcher can land its followup as a reply or, if there's nothing material to add, a decline.
- `dispatch` admits `{REPLY, BOOTSTRAP, TASK_FOLLOWUP}`. A `TASK_FOLLOWUP`
  dispatch is manager-only and may create exactly one true replacement root for
  the causal original root. Admission is a single `BEGIN IMMEDIATE`
  transaction; the triggering system payload, existing task/thread links, and
  dedicated replacement audit are the authority. Prompt prose and process
  memory are not. Ordinary reply/bootstrap behavior is unchanged.
- `close_out` continues to reject everything else.

### 6.5 Failure mode

If the system message is appended but `mint_thread_invocation` or `thread_queue.put` fails, the system message stays committed (transparency win) and we log `thread_task_followup_enqueue_failed(thread_id, terminal_task_id, error)`. No rollback. The founder sees the result in the thread but no agent prose — they can re-send `@<agent>` to get a manual reply.

## 7. Prompt builder change

`src/daemon/thread_runner.py::_purpose_note` gets a new branch:

```python
if purpose == "task_followup":
    return (
        f"Task TASK-NNN that you dispatched from this thread reached "
        f"<status>. Compose a follow-up reply with the result (pull details "
        f"via `happyranch details TASK-NNN`), or decline if there is nothing "
        f"substantive to add."
    )
```

The actual `TASK-NNN` and `<status>` are read off the triggering SYSTEM message's payload (the `system_payload_json` already round-trips through `messages`), so `_purpose_note` gets passed the triggering message or `system_payload`. This is the only change to the prompt structure; the rest of the header, history, token, and skill reference are unchanged.

## 8. Loop bound

A re-invoked manager may dispatch one corrected replacement root, but the
budget belongs to the persisted causal lineage, not to each turn. The
replacement root is marked by
`thread_task_followup_replacement_dispatched`; its descendants, revisit/retry
family, chain/fanout legs, and terminal followups are permanently ineligible.
Manager supersession independently rejects thread-originated roots and cannot
produce a successor of this replacement; it is outside THR-225. Replays and concurrent attempts reject
with `task_followup_dispatch_already_used` without residue. Thus recursion
depth is structurally bounded at one replacement.

## 9. Web UI

`web/src/features/threads/` rendering already handles SYSTEM messages by `kind_tag`. The two new tags (`task_completed`, `task_failed`) need:

- a renderer case (mirror `task_dispatched`'s shape: badge + task id link + 240-char summary tail)
- the link target: `/orgs/<slug>/tasks/<task_id>` (existing route)

No API surface change in `web/src/lib/api/`; the threads stream already carries SYSTEM messages and their payload.

## 10. Audit events

New event names, all consumed by `happyranch audit`:

- `thread_task_followup_enqueued` — dispatcher, original task, terminal task, invocation_token
- `thread_followup_skipped` — reason ∈ {`thread_not_open`, `dispatcher_unresolved`, `no_thread_linkage`}, with `task_status`, `thread_status`
- `thread_turn_cap_auto_extended` — already-named convention for cap mutations, reused with `reason=task_followup`
- `thread_task_followup_enqueue_failed` — partial-failure path (§6.5)

The followup turn's own outcome reuses existing audit events (`thread_message_sent` / `thread_invocation_failed` / `thread_invocation_declined`).

## 11. Test plan

Unit (no real subprocess):

- Fire-predicate truth table from §4 — eight rows, each asserted.
- Chain-walk lookup: original task with `dispatched_from_thread_id` set; two-link revisit chain where the revisit completes (terminal carries no thread id but lookup finds it via predecessor).
- Thread-state guard: archived / abandoned / archiving each skip + audit; OPEN proceeds.
- Turn-cap projection: at cap → auto-extends; well below cap → no extend; pending_load counted in projection.
- Dispatcher unresolved (no `task_dispatched` audit row) → skipped with reason.
- First eligible manager `TASK_FOLLOWUP` dispatch creates one root; replay,
  concurrency, and every replacement-lineage attempt reject with 409 and no
  task/message/audit/queue residue.

Integration (with `fake_claude.sh` thread plan env):

- **e2e_followup_reply**: founder send → manager reply+dispatch → task completes → followup invocation runs → manager reply (seq N+2) → assert thread now has 5 messages: founder, manager reply, system task_dispatched, system task_completed, manager followup reply.
- **e2e_followup_opaque_failure**: opaque executor failure (exit 1) → terminal FAILED with `task_failed` follow-up, no successor, no `revisit_task_id`. This is the contract exercised by the current integration test `tests/integration/test_thread_task_followup_e2e.py::test_followup_fires_once_after_opaque_failure`.
- **e2e_followup_archived**: thread is archived (status flipped) between dispatch and terminal → audit-only, no new messages, no new invocations.

Each followup test sets both `fake_claude_plan_env` (task) and `fake_claude_thread_plan_env` (followup), per the dual-plan pattern documented in CLAUDE.md.

OpenAPI snapshot: no new routes (purely internal). `tests/contract/test_openapi_snapshot.py` should pass unchanged; `web/src/test/openapi-coverage.test.ts` likewise.

## 12. Migration / rollout

- No DB schema migration. New enum value (`task_followup`) on `ThreadInvocationPurpose` writes to the existing `thread_invocations.purpose` TEXT column.
- No backfill in v1 (§3).
- The new audit event names appear in v2 logs; older logs unaffected.
- Rollback: disable the hook by short-circuiting `_maybe_post_thread_followup` at its top. The new system message tags and enum value are forward-compatible.

## 13. Open questions

None blocking. Two flagged for future:

- **Followup dispatch policy** (§6.4): THR-225 resolved this question by
  allowing one persisted manager replacement per causal lineage.
- **Founder cancellation surface** (§4): firing on cancelled may produce a thread entry the founder doesn't want to read. Easy to flip to "suppress cancelled" later by adding `if original.cancelled_at: skip` to the predicate.

## 14. Implementation order

1. Enum: `ThreadInvocationPurpose.TASK_FOLLOWUP` added to `src/models.py`.
2. Promote `_pending_reply_load` → `Database.count_pending_turn_obligations(thread_id)` and switch the three existing call sites in `src/daemon/routes/threads.py`. Include `TASK_FOLLOWUP` in the counted set.
3. Add `TASK_FOLLOWUP` to reply/decline and to dispatch under the transactional
   one-replacement lineage gate (§6.4).
4. `_purpose_note` branch + prompt parameter wiring in `src/daemon/thread_runner.py`.
5. New system-message renderers (`task_completed`, `task_failed`) in `src/infrastructure/thread_store.py` and `src/daemon/thread_forward.py`.
6. `db.bump_thread_turn_cap(thread_id, delta)` method + `audit.log_thread_*` methods.
7. `_maybe_post_thread_followup` helper in `src/orchestrator/run_step.py`; wire into both terminal transition sites.
8. Web renderer cases for the two new tags.
9. Unit tests (§11 unit set).
10. Integration tests (§11 integration set).
11. CLAUDE.md update: a short "Thread task-followup" subsection under the threads notes documenting the non-obvious invariants (call order, chain-walk, dispatch policy, loop bound).
