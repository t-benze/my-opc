"""Implementation of Orchestrator.run_step — the single primitive that advances
a task one subprocess call at a time. Separate from orchestrator.py so the
algorithm has its own test surface.

Entry contract (Path B): task MUST be one of:
  (a) status=pending, or
  (b) status=in_progress AND block_kind=DELEGATED AND all children are terminal, or
  (c) status=in_progress AND block_kind=BLOCKED_ON_JOB AND all blocking jobs are terminal.
Any other state = stale enqueue, silent no-op — in particular
in_progress + block_kind IS NULL is a LIVE subprocess and must NOT be admitted
(it would double-spawn). Phase 3: no legacy blocked shapes are accepted;
the boot-time migration flips them before request handling.

Exit contract: task ends in exactly one of {in_progress-then-crashed,
completed, failed, in_progress(DELEGATED), in_progress(BLOCKED_ON_JOB),
escalated}.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from runtime.models import BlockKind, TaskStatus
from runtime.orchestrator.org_config import load_org_config

if TYPE_CHECKING:
    from runtime.models import TaskRecord
    from runtime.orchestrator.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({
    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SUPERSEDED,
    TaskStatus.CANCELLED,  # Path B: founder-initiated terminal stop.
})

# Path B: parked-but-in_progress carriers. A task with block_kind set
# runs no subprocess — it is waiting on children/jobs it manages.
# block_kind IS NULL (a live subprocess) is deliberately NOT a parked carrier.
# Phase 3 (THR-037): the deprecated BLOCKED status was retired.
_PARKED_CARRIER_STATUSES = frozenset({TaskStatus.IN_PROGRESS})


def is_root(task: "TaskRecord") -> bool:
    """Canonical structural root test (THR-033 Change A): a task with no parent.

    Per the subtask/composite-task model this is equivalent to
    ``task_type == "task"``, but ``parent_task_id`` is the direct fact the
    escalation/walk code keys on. Only root tasks escalate to the founder; a
    non-root task that would escalate instead fails and hands back to its parent.
    """
    return task.parent_task_id is None


def run_step_impl(orch: "Orchestrator", task_id: str, metadata: dict | None = None) -> None:
    # metadata: optional resume context (trigger, triggering_job_id); read by the CAS-win audit hook in Task 11.
    db = orch._db
    task = db.get_task(task_id)
    if task is None:
        return

    # Cancellation short-circuit. Once /cancel marks a task FAILED + sets
    # cancelled_at, any late queue event (e.g., the parent auto-resume after
    # the SIGTERM'd child's audit arrives) must be a no-op. Checking status
    # alone isn't enough — in_progress(delegated) parents get cancelled too, and
    # the terminal-state test runs in step 1 below. Re-check before returning
    # so we don't enter the in_progress transition.
    if task.cancelled_at is not None:
        logger.debug("run_step %s: cancelled, skipping", task_id)
        return

    # ---- 1. Verify entry state ----
    # Path B: parked carriers are in_progress(delegated|blocked_on_job).
    # A live subprocess is in_progress + block_kind IS NULL — it falls to the
    # `else: skip` and is never re-admitted (admitting it would double-spawn).
    if task.status == TaskStatus.PENDING:
        pass  # eligible
    elif task.status in _PARKED_CARRIER_STATUSES and task.block_kind == BlockKind.DELEGATED:
        children = [db.get_task(cid) for cid in db.get_children(task_id)]
        # THR-211: a child whose current session has durably landed a
        # structured terminal report counts as terminal for parent admission
        # even while its row reads in_progress (the status transition is
        # deferred to session finalization). A genuinely-running child with no
        # landed result still blocks the parent — never suppress live work.
        if any(
            c is None or (
                c.status not in TERMINAL_STATES
                and not _child_has_landed_terminal_result(orch, c)
            )
            for c in children
        ):
            logger.debug("run_step %s: child still running, skipping", task_id)
            return
    elif task.status in _PARKED_CARRIER_STATUSES and task.block_kind == BlockKind.BLOCKED_ON_JOB:
        # Blocked-on-job task: re-check live job table to see whether all
        # blocking jobs have reached a terminal state. Spec §5.1.
        import json as _json
        try:
            job_ids = _json.loads(task.blocked_on_job_ids or "[]")
        except _json.JSONDecodeError:
            logger.debug("run_step %s: blocked_on_job_ids unparseable", task_id)
            return
        if not job_ids:
            logger.debug("run_step %s: blocked_on_job_ids empty", task_id)
            return
        _TERMINAL_JOB_STATES = {"completed", "failed", "rejected"}
        for jid in job_ids:
            jstatus = db.get_job_status(jid)
            if jstatus not in _TERMINAL_JOB_STATES:
                logger.debug(
                    "run_step %s: blocking job %s still in-flight (status=%s)",
                    task_id, jid, jstatus,
                )
                return
        # All jobs terminal — fall through to step 2 + step 3.
    else:
        logger.debug(
            "run_step %s: not eligible (status=%s, block_kind=%s)",
            task_id, task.status, task.block_kind,
        )
        return

    # ---- 2. Budget guard (persisted, survives restarts) ----
    max_steps = orch._settings.max_orchestration_steps
    next_count = task.orchestration_step_count + 1
    if next_count > max_steps:
        reason = f"max steps ({max_steps}) exceeded"
        if not is_root(task):
            # THR-033 Change A: a NON-root task that hits the step budget must
            # NOT escalate directly to the founder — it fails and hands back to
            # its parent (bounded failure-recovery carries it up). The CAS is
            # required for the same dup-delivery reason as the root path below:
            # this guard runs BEFORE try_claim_for_step, so it has no upstream
            # CAS. Only the first delivery wins and proceeds to wake the parent
            # + post the followup exactly once.
            if not db.try_fail_over_budget(
                task_id,
                expected_status=task.status,
                expected_block_kind=task.block_kind,
                note=reason,
            ):
                logger.debug(
                    "run_step %s: lost over-budget fail race, dropping", task_id,
                )
                return
            _enqueue_parent_if_waiting(orch, task_id)
            _maybe_post_thread_followup(
                orch, task_id,
                status=TaskStatus.FAILED, auto_revisit_spawned=False,
            )
            return
        # Root: park in escalated for the founder (try_escalate* now writes the
        # top-level ESCALATED status; behavior otherwise unchanged).
        # Atomic CAS on the eligible pre-state read at step 1. This guard runs
        # BEFORE try_claim_for_step, so without it two duplicate deliveries of
        # the same stale at-cap row would both escalate and double-post the
        # thread `task_escalated` message + TASK_FOLLOWUP. If False: another
        # worker escalated first (or /cancel landed) — drop silently.
        if not db.try_escalate_runtime(
            task_id,
            reason=reason,
            agent="orchestrator",
            reason_code="runtime_orchestration_step_budget_exhausted",
            expected_status=task.status,
            expected_block_kind=task.block_kind,
            match_expected_state=True,
        ):
            logger.debug(
                "run_step %s: lost over-budget escalate race, dropping", task_id,
            )
            return
        orch.notify_escalated(
            task_id=task_id, agent="orchestrator", reason=reason,
        )
        _maybe_post_thread_escalation(orch, task_id, reason=reason)
        return

    # ---- 3. Atomic claim: unblock + increment + mark in_progress ----
    # Conditional CAS on (expected_status, expected_block_kind) — if another
    # worker has already claimed this task_id (duplicate enqueue from a
    # multi-child fan-in race, or parent auto-resume colliding with a late
    # callback), the UPDATE matches zero rows and we return silently.
    claimed = db.try_claim_for_step(
        task_id,
        expected_status=task.status,
        expected_block_kind=task.block_kind,
        new_count=next_count,
    )
    if not claimed:
        logger.debug(
            "run_step %s: lost claim race (another worker is advancing it)",
            task_id,
        )
        return

    # Spec §5.2: write task_resumed_from_jobs audit row immediately after the
    # CAS wins on an in_progress(blocked_on_job) → in_progress(NULL) transition. The
    # prompt-build at step 4 reads this row to inject BLOCKED-JOBS-RESULTS.
    if (task.status in _PARKED_CARRIER_STATUSES
            and task.block_kind == BlockKind.BLOCKED_ON_JOB):
        import json as _json
        try:
            job_ids = _json.loads(task.blocked_on_job_ids or "[]")
        except _json.JSONDecodeError:
            job_ids = []
        job_outcomes = {jid: (db.get_job_status(jid) or "unknown")
                        for jid in job_ids}
        md = metadata or {}
        orch._audit.log_task_resumed_from_jobs(
            task_id=task_id,
            blocking_job_ids=job_ids,
            trigger=md.get("trigger", "unknown"),
            triggering_job_id=md.get("triggering_job_id"),
            job_outcomes=job_outcomes,
        )

    # Fan-out dispatch: active_fanout can only be "spawned"
    # (all children terminal — inject join context).
    # pending_review removed — founder ruling THR-012 msg 129/131
    # removed the fan-out review gate entirely.
    if task.active_fanout is not None:
        from runtime.orchestrator.fanout import FanoutState as _FanoutState
        fanout_state = _FanoutState.deserialize(task.active_fanout)
        if fanout_state.status == "spawned":
            # status == "spawned" → all children terminal; inject join context.
            _inject_fanout_join_context(orch, task_id, task.active_fanout)
            db.update_task_active_fanout(task_id, None)

    # ---- 4. Run the agent subprocess ----
    agent = task.assigned_agent or _default_agent_for_root(orch, task)
    if task.assigned_agent is None:
        db.update_task(task_id, assigned_agent=agent)

    prompt = _build_agent_prompt(orch, task, agent)
    try:
        result, report = orch._run_agent(task_id, agent, prompt)
    except Exception as exc:
        note = f"agent invocation failed: {exc}"
        _fail(orch, task_id, note=note)
        _enqueue_parent_if_waiting(orch, task_id, root_auto_revisit_spawned=False)
        _maybe_post_thread_followup(
            orch, task_id,
            status=TaskStatus.FAILED, auto_revisit_spawned=False,
        )
        return

    # Persist token usage for this session, regardless of session outcome.
    # Spec 4.3: skip when None; otherwise write — including the parse-failure
    # case where token columns are NULL but ``usage_raw_json`` carries the
    # raw payload. Done before outcome classification so timeouts / blocked
    # sessions still land their usage row.
    if result.token_usage is not None:
        db.insert_session_token_usage(
            task_id=task_id,
            agent=agent,
            session_id=result.session_id,
            executor=orch._resolve_executor_name(agent),
            token_usage=result.token_usage,
            scope_type="task",
            scope_id=task_id,
            thread_id=task.dispatched_from_thread_id,
        )

    # Cancel-race Guard B: /cancel can land between try_claim_for_step and
    # subprocess exit. The l.41 entry guard only catches NEW enqueues. If we
    # observe cancelled_at != NULL here, the report (if any) is from a
    # cancelled tree and must not feed the decision pipeline — otherwise the
    # `delegate` branch (no idempotence guard) will resurrect the parent and
    # spawn a child task on a tree the founder explicitly killed.
    # Token usage stays persisted above (provider really charged for it).
    # See docs/superpowers/specs/2026-05-26-cancel-race-design.md §5.2.
    refetch = db.get_task(task_id)
    if refetch is None or refetch.cancelled_at is not None:
        logger.debug(
            "run_step %s: cancelled during session, dropping report", task_id,
        )
        # IN_PROGRESS cancellation: the cancel route sent SIGTERM and set
        # cancelled_at BEFORE run_step reached Site B, so Site B never runs.
        # Fire the followup here instead.  Disjoint with the cancel route's
        # Phase 1b (which fires for PENDING/BLOCKED only — tasks that had no
        # live subprocess at cancel time), so no double-fire risk.
        # THR-181 Track A (founder lifecycle envelope): cancellation during the
        # continued turn spends the single-use envelope fail-closed so the
        # continuation is never re-used.
        try:
            db.spend_authority_continue_envelope_if_active(
                task_id,
                audit_agent=agent,
                error="cancelled during the continued turn",
            )
        except Exception:  # pragma: no cover - fail-closed defensive
            pass
        if refetch is not None:
            _maybe_post_thread_followup(
                orch, task_id,
                status=TaskStatus.FAILED, auto_revisit_spawned=False,
            )
        return

    # ---- 5. Classify outcome ----
    if not result.success or report is None:
        note = _session_failed_note(result, report)
        _fail(orch, task_id, note=note)
        _enqueue_parent_if_waiting(orch, task_id, root_auto_revisit_spawned=False)
        _maybe_post_thread_followup(
            orch, task_id,
            status=TaskStatus.FAILED, auto_revisit_spawned=False,
        )
        return

    orch._log_step_result(task_id, result, report)
    # THR-181 Track A: resolve the IMMUTABLE task-result row id whose
    # CompletionReport produced this decision. The authority hook derives its
    # candidate claim from it, so restart/recovery re-entry of the SAME row
    # maps to the SAME candidate (never a second evaluation).
    result_row_id = None
    if report is not None and isinstance(
        getattr(result, "session_id", None), str
    ) and result.session_id:
        _result_row = db.get_latest_task_result(task_id, agent, result.session_id)
        if _result_row is not None:
            result_row_id = _result_row["id"]
    _consume_completion_report(orch, task_id, report, result_row_id=result_row_id)


def _is_continued_turn_decision(active_envelope, result_row_id: int | None) -> bool:
    """THR-181 Track A (founder lifecycle envelope): is this consumed report the
    continued turn's NEW decision (gate by the envelope), as opposed to a
    replay of the original escalate's immutable result row (ordinary path)?

    The envelope is bound to the original escalate's causal result row
    (``causal_event_id == "result:<row_id>"``). A decision whose result row
    is THAT row is a duplicate delivery of the already-granted escalation
    and must ride the ordinary fail-closed path (hook eligibility/claim
    CAS); a decision from ANY other row (or a row-less report arriving
    inside the continuation window) is the continued turn.
    """
    if active_envelope is None:
        return False
    causal = active_envelope["causal_event_id"] or ""
    if causal.startswith("result:"):
        try:
            causal_row = int(causal.split(":", 1)[1])
        except (ValueError, IndexError):
            causal_row = None
        if causal_row is not None and result_row_id == causal_row:
            return False
    return True


def _spend_envelope_as_violation(
    orch: "Orchestrator", db, task_id: str, agent: str, envelope,
    *, decision_family: str, error: str,
) -> None:
    """THR-181 Track A (founder lifecycle envelope): spend the single-use continuation
    envelope as ``violated`` (exactly-once CAS + atomic audit row). The
    attempted decision family is recorded bounded (never raw prose); the
    attempt is never silently discarded."""
    try:
        db.consume_authority_continue_envelope(
            envelope_id=envelope["id"],
            root_task_id=task_id,
            decision_family=decision_family,
            expected_manager_agent=agent,
            expected_session_id=envelope["manager_session_id"],
            expected_causal_event_id=envelope["causal_event_id"],
            expected_causal_event_digest=envelope["causal_event_digest"],
            expected_policy_id=envelope["policy_id"],
            expected_policy_version=envelope["policy_version"],
            expected_policy_digest=envelope["policy_digest"],
            expected_clause_id=envelope["clause_id"],
            expected_action=envelope["action"],
            audit_agent=agent,
            error=error,
            violation=True,
        )
    except Exception as exc:  # pragma: no cover - fail-closed defensive
        # A consumption write defect can never permit continuation: the
        # escalation path below still runs fail-closed.
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "envelope spend failed for %s: %s", task_id, exc,
        )


def _spend_envelope_as_consumed(
    orch: "Orchestrator", db, task_id: str, agent: str, envelope,
    *, decision_family: str,
) -> None:
    """THR-181 Track A (founder lifecycle envelope): consume the single-use
    continuation envelope as ``consumed`` (a normally validated manager decision).
    Exactly-once; a concurrent consumer that already spent it is a no-op."""
    try:
        db.consume_authority_continue_envelope(
            envelope_id=envelope["id"],
            root_task_id=task_id,
            decision_family=decision_family,
            expected_manager_agent=agent,
            expected_session_id=envelope["manager_session_id"],
            expected_causal_event_id=envelope["causal_event_id"],
            expected_causal_event_digest=envelope["causal_event_digest"],
            expected_policy_id=envelope["policy_id"],
            expected_policy_version=envelope["policy_version"],
            expected_policy_digest=envelope["policy_digest"],
            expected_clause_id=envelope["clause_id"],
            expected_action=envelope["action"],
            audit_agent=agent,
        )
    except Exception as exc:  # pragma: no cover - fail-closed defensive
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "envelope consume failed for %s: %s", task_id, exc,
        )


def _escalate_continued_turn_violation(
    orch: "Orchestrator", task_id: str, agent: str, *, attempted: str,
    last_summary: str = "",
) -> None:
    """THR-181 Track A: fail closed when a continued turn attempts to replace
    its same root, using the EXISTING ordinary
    founder-escalation path (``try_escalate`` CAS -> escalation audit row ->
    notification -> thread projection), with a server-derived reason. The
    authority hook is deliberately not used to authorize replacement."""
    db = orch._db
    reason = (
        f"authority-continuation envelope violation: continued turn "
        f"produced {attempted!r} outside the single-use permitted action; "
        f"escalated to founder"
    )
    if not db.try_escalate_runtime(
        task_id,
        reason=reason,
        agent=agent,
        reason_code="runtime_authority_envelope_violation",
    ):
        # Cancellation/terminal won the race — the founder's state wins.
        return
    orch.notify_escalated(
        task_id=task_id, agent=agent, reason=reason, last_summary=last_summary,
    )
    _maybe_post_thread_escalation(orch, task_id, reason=reason)


def _consume_completion_report(
    orch: "Orchestrator", task_id: str, report,
    *, result_row_id: int | None = None,
) -> None:
    """Consume a persisted CompletionReport and apply its transition.

    Used both inline in ``run_step_impl`` (after ``_log_step_result``) and
    from the boot sweep when an orphaned (unconsumed) ``task_result`` row is
    discovered for a task whose executor process died mid-turn.

    ``result_row_id`` is the immutable ``task_results`` row id that produced
    ``report`` — the authority hook binds its candidate identity to it so a
    restart replay cannot mint a second candidate/evaluation. When omitted it
    is resolved deterministically to the latest task_results row for the
    task (the row every shipping call site built the report from).

    Re-fetches the task from the DB so it works from any call site (the
    inline ``run_step_impl`` call site already has the task in scope, but
    the sweep does not). Semantic zero-delta extract — the body of this
    function was formerly the tail of ``run_step_impl``.
    """
    db = orch._db
    task = db.get_task(task_id)
    if task is None:
        return
    agent = task.assigned_agent or "unknown"
    # THR-181 Track A (founder lifecycle envelope): the single-use continuation
    # envelope, when ACTIVE, marks this consumption as the continued turn's
    # decision — PROVIDED the consumed report is NOT a replay of the original
    # escalate's immutable result row (the envelope's causal event). A replay
    # of the same row is a duplicate delivery of the already-granted
    # escalate and must ride the ordinary fail-closed path (the hook's
    # eligibility/claim CAS), never the envelope gate.
    active_envelope = db.get_active_authority_continue_envelope(task_id)
    if result_row_id is None and report is not None:
        row = db.execute(
            "SELECT id FROM task_results WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is not None:
            result_row_id = row["id"]
    # ``orchestration_step_count`` was already incremented by
    # ``try_claim_for_step`` at the top of ``run_step_impl``; use its
    # current value (not +1) for audit-log keying.
    next_count = task.orchestration_step_count

    if report.status == "blocked":
        if _is_continued_turn_decision(active_envelope, result_row_id):
            _spend_envelope_as_consumed(
                orch, db, task_id, agent, active_envelope,
                decision_family="blocked",
            )
        if report.waiting_on_job_ids:
            # Spec §5.3: block-on-jobs branch. In-place transition, NOT _fail.
            import json as _json
            deduped = sorted(set(report.waiting_on_job_ids))
            # Defensive re-validation: a job could have been deleted between the
            # route POST and run_step_impl consuming the report (extremely
            # unlikely; jobs are write-once + terminal-frozen). Degrade gracefully.
            for jid in deduped:
                if db.get_job_status(jid) is None:
                    note = f"self-blocked but job {jid} not found"
                    _fail(orch, task_id, note=note)
                    _enqueue_parent_if_waiting(orch, task_id)
                    _maybe_post_thread_followup(
                        orch, task_id,
                        status=TaskStatus.FAILED, auto_revisit_spawned=False,
                    )
                    return
            # Path B: a task waiting on jobs it submitted is in_progress, with
            # the waiting reason kept in block_kind.
            db.update_task(
                task_id,
                status=TaskStatus.IN_PROGRESS,
                block_kind=BlockKind.BLOCKED_ON_JOB,
                blocked_on_job_ids=_json.dumps(deduped),
                note=report.output_summary,
            )
            orch._audit.log_task_blocked_on_jobs(
                task_id=task_id, agent=agent,
                blocking_job_ids=deduped,
                output_summary_excerpt=(report.output_summary or "")[:200],
            )
            # Immediate predicate check (caller B). Spec §5.6: runs HERE, after
            # the agent session has already been cleared by submit_completion.
            # No session race.
            _maybe_resume_blocked_task(
                orch, task_id,
                trigger="block_submit", triggering_job_id=None,
            )
            return
        # Existing escalated path (waiting_on_job_ids empty).
        note = f"self-blocked: {report.output_summary}"
        _fail(orch, task_id, note=note)
        _enqueue_parent_if_waiting(orch, task_id)
        _maybe_post_thread_followup(
            orch, task_id,
            status=TaskStatus.FAILED, auto_revisit_spawned=False,
        )
        return

    # ---- 6. Parse next step ----
    # Orchestration is driven by task TYPE, not manager role. A type=task
    # owner (any agent) speaks the NextStep protocol; a type=subtask is
    # leaf-only. `task` is the early-fetched record; task_type is immutable
    # provenance, safe to read post-claim.
    if task.task_type == "task":
        decision = orch._parse_next_step(report)
        _step_audit_id = orch._audit.log_orchestration_step(
            task_id, next_count, decision.model_dump(exclude_none=True),
        )
    else:
        from runtime.models import NextStep
        decision = NextStep(action="done", summary=report.output_summary)
        _step_audit_id = None

    # The envelope is a single-use lifecycle/causality receipt. It does not
    # whitelist an exact manager action: after normal parsing, the continued
    # turn follows the ordinary manager-decision validation and dispatch path.
    # Independent same-root identity, cancellation, CAS, budget, and protected-
    # boundary fences remain daemon-owned and non-overridable.
    if _is_continued_turn_decision(active_envelope, result_row_id):
        if decision.action == "supersede":
            _spend_envelope_as_violation(
                orch, db, task_id, agent, active_envelope,
                decision_family=decision.action,
                error="continued turn attempted to replace the same root",
            )
            _escalate_continued_turn_violation(
                orch, task_id, agent, attempted=decision.action,
                last_summary=report.output_summary,
            )
            return
        _spend_envelope_as_consumed(
            orch, db, task_id, agent, active_envelope,
            decision_family=decision.action,
        )

    # ---- 7. Dispatch on action ----
    if decision.action == "done":
        _complete(
            orch, task_id,
            note=decision.summary or report.output_summary,
            output_dir=report.output_dir,
        )
        _enqueue_parent_if_waiting(orch, task_id)
        _maybe_post_thread_followup(
            orch, task_id,
            status=TaskStatus.COMPLETED, auto_revisit_spawned=False,
        )
        return

    if decision.action == "escalate":
        reason = decision.reason or "Escalated"
        if not is_root(task):
            # THR-033 Change A defensive guard: a non-root task never escalates
            # directly to the founder — it fails and hands back to its parent
            # (bounded failure-recovery carries it up). Currently inert (only a
            # task_type='task' parses a decision, and in production those are
            # roots), but locks the invariant against future regressions.
            _fail(
                orch, task_id,
                note=f"non-root escalation requested ({reason}); routed to parent",
            )
            _enqueue_parent_if_waiting(orch, task_id)
            _maybe_post_thread_followup(
                orch, task_id,
                status=TaskStatus.FAILED, auto_revisit_spawned=False,
            )
            return
        # Root: park in escalated for the founder (try_escalate* now writes the
        # top-level ESCALATED status; behavior otherwise unchanged).
        # THR-181 Track A: BEFORE the manager root's proposed escalation is
        # committed, run exactly one audited LLM authority evaluation of the
        # proposed reason against the release-controlled policy for this
        # team (Engineering v1). The hook returns "continue_same_root" only
        # when the policy's narrow continue clause matched and the named
        # same-root permitted action was executed + audited atomically;
        # EVERY other outcome — ambiguity, malformed/missing/unknown output,
        # timeout/provider error, policy/team/digest mismatch, audit failure,
        # cancellation, exhausted limits, stale/CAS conflict, restart-
        # incomplete state, ineligibility, or any successor/supersede/
        # revisit/fresh-root signal — fails closed to "escalate", which
        # proceeds through the exact existing escalation path below.
        from runtime.orchestrator.authority import run_authority_hook
        hook_outcome = run_authority_hook(
            orch, task, agent, reason, result_row_id,
            manager_self_evaluation=report.manager_self_evaluation,
        )
        if hook_outcome == "continue_same_root":
            # Same-root continuation already executed (audited): the root
            # returned to pending for its next manager decision step and was
            # re-enqueued. The escalation is NOT committed.
            return
        # Atomic CAS: transition to ESCALATED only if not cancelled
        # or terminal. Closes the post-_is_already_terminal race (Codex P2 on
        # PR #34) by serializing against /cancel via the Database RLock.
        # If False: founder cancellation landed between Guard B's re-fetch and
        # here. Drop the escalate silently — the founder's terminal state wins.
        if not db.try_escalate(task_id, reason=reason):
            logger.debug(
                "run_step %s: cancelled between re-check and escalate, dropping",
                task_id,
            )
            return
        orch._audit.log_escalation(task_id, agent, reason)
        orch.notify_escalated(
            task_id=task_id, agent=agent, reason=reason,
            last_summary=getattr(report, "output_summary", "") or "",
        )
        _maybe_post_thread_escalation(orch, task_id, reason=reason)
        # parent stays in_progress(delegated) until this task reaches a terminal.
        return

    if decision.action == "supersede":
        # The decision model rejects every caller-supplied identity/target field;
        # the database method re-checks this current claimed root under its lock.
        try:
            expected_manager = orch.teams.manager_for_team(task.team).name
        except (KeyError, ValueError):
            expected_manager = None
        if expected_manager != agent or task.current_session_id is None:
            _fail(orch, task_id, note="manager supersession claim is not current")
            _enqueue_parent_if_waiting(orch, task_id)
            _maybe_post_thread_followup(
                orch, task_id, status=TaskStatus.FAILED, auto_revisit_spawned=False,
            )
            return
        successor_id = db.try_manager_supersede(
            task_id,
            actor_agent=agent,
            actor_session_id=task.current_session_id,
            expected_team=task.team,
            successor_brief=decision.successor_brief or "",
            rationale=decision.rationale or "",
            attestation=decision.attestation.model_dump() if decision.attestation else {},
        )
        if successor_id is None:
            # The claim may have been superseded by cancellation or a competing
            # consumer.  Never cancel/alter live work just to make it eligible.
            return
        try:
            if orch._queue is not None:
                orch._queue.put_nowait(orch._slug, successor_id)
        except Exception:
            # The committed successor remains pending. Startup recovery
            # idempotently re-enqueues pending tasks; never reopen predecessor.
            logger.exception("manager supersession %s enqueue failed", successor_id)
        return

    if decision.action == "fanout":
        # Phase 1: read-only native fan-out core.
        # Validate structural correctness (width, cap ack, no per-child then/verdict).
        from runtime.orchestrator.fanout import (
            MAX_FANOUT_WIDTH,
            FanoutState,
            fanout_child_targets,
            validate_fanout_decision,
        )
        err = validate_fanout_decision(decision)
        if err is not None:
            note = f"invalid fanout: {err}"
            _fail(orch, task_id, note=note)
            _enqueue_parent_if_waiting(orch, task_id)
            _maybe_post_thread_followup(
                orch, task_id,
                status=TaskStatus.FAILED, auto_revisit_spawned=False,
            )
            return

        # Validate each child's workspace exists (reuse _validate_one_leg).
        reviewer_agents = _reviewer_agents_for(orch)
        for i, child in enumerate(decision.children):
            child_err = _validate_one_leg(
                orch, agent=child.agent, where=f"fanout child {i + 1}",
            )
            if child_err is not None:
                note = f"invalid fanout: {child_err}"
                _fail(orch, task_id, note=note)
                _enqueue_parent_if_waiting(orch, task_id)
                _maybe_post_thread_followup(
                    orch, task_id,
                    status=TaskStatus.FAILED, auto_revisit_spawned=False,
                )
                return
            # THR-175: a pipeline-carrier first leg that is a configured
            # reviewer must declare expect_verdict=APPROVE (only when the child
            # actually carries a downstream ``then`` chain). Omission is a
            # HARD REJECT — a recoverable authoring error, fed back to the
            # owner for correction (never a root failure).
            if child.then and child.agent in reviewer_agents and child.expect_verdict is None:
                _reject_reviewer_omission(
                    orch, task_id, agent, next_count,
                    _reviewer_omitted_expectation_error(child.agent),
                    action_label="fan-out",
                )
                return
            # Validate carrier chain legs for pipeline children (Phase 2).
            for j, leg in enumerate(child.then):
                leg_err = _validate_one_leg(
                    orch, agent=leg.agent,
                    where=f"fanout child {i + 1} then leg {j + 1}",
                )
                if leg_err is not None:
                    note = f"invalid fanout: {leg_err}"
                    _fail(orch, task_id, note=note)
                    _enqueue_parent_if_waiting(orch, task_id)
                    _maybe_post_thread_followup(
                        orch, task_id,
                        status=TaskStatus.FAILED, auto_revisit_spawned=False,
                    )
                    return
                if leg.agent in reviewer_agents and leg.expect_verdict is None:
                    _reject_reviewer_omission(
                        orch, task_id, agent, next_count,
                        _reviewer_omitted_expectation_error(leg.agent),
                        action_label="fan-out",
                    )
                    return

        # Scope validation: manager own-team/self only; non-manager self-only.
        # Reuse _legs_out_of_scope with the fan-out child targets.
        targets = fanout_child_targets(decision)
        out_of_scope: list[tuple[str, str]] = []
        if orch.teams.is_team_manager(agent):
            caller_team = orch.teams.team_for_manager(agent)
            for a in targets:
                if not a or a == agent:
                    continue
                t = orch.teams.team_for_agent(a)
                if caller_team is None or t != caller_team:
                    out_of_scope.append((a, f"on team {t!r}" if t else "not on a team"))
        else:
            for a in targets:
                if not a or a == agent:
                    continue
                out_of_scope.append(
                    (a, "non-manager owners may only delegate to themselves"),
                )
        if out_of_scope:
            parts = [f"{name!r} ({reason})" for name, reason in out_of_scope]
            if orch.teams.is_team_manager(agent):
                caller_team = orch.teams.team_for_manager(agent)
                feedback = (
                    f"Invalid fan-out: you are on team {caller_team!r}, but "
                    f"{'; '.join(parts)}. Pick agents on your own team or "
                    "yourself, or escalate."
                )
            else:
                feedback = (
                    f"Invalid fan-out: {'; '.join(parts)}. You may only "
                    f"fan-out sub-tasks to yourself ({agent!r}), or escalate."
                )
            db.insert_task_result(
                task_id=task_id,
                agent=agent,
                session_id="",
                status="completed",
                confidence_score=0,
                output_summary=feedback,
                risks_flagged=[],
            )
            orch._audit.log_orchestration_step(
                task_id, next_count, {"action": "feedback", "reason": feedback},
            )
            db.update_task(task_id, status=TaskStatus.PENDING, block_kind=None)
            if orch._queue is not None:
                orch._queue.put_nowait(orch._slug, task_id)
            return

        for i, child in enumerate(decision.children):
            retry_link_err = _check_retry_link_required(
                orch,
                task_id,
                target_agent=child.agent,
                revisit_of_task_id=child.revisit_of_task_id,
            )
            if retry_link_err is not None:
                _reject_retry_link_decision(
                    orch,
                    task_id,
                    agent,
                    next_count,
                    f"fanout child {i + 1}: {retry_link_err}",
                )
                return

        # Spawn children immediately — no review gate (founder ruling THR-012 msg 129/131).
        width = len(decision.children)
        children_payload = []
        for c in decision.children:
            cd = {"agent": c.agent, "prompt": c.prompt}
            if c.revisit_of_task_id is not None:
                cd["revisit_of_task_id"] = c.revisit_of_task_id
            if c.expect_verdict is not None:
                cd["expect_verdict"] = c.expect_verdict
            if c.then:
                cd["then"] = [
                    {"agent": l.agent, "prompt": l.prompt,
                     "expect_verdict": l.expect_verdict}
                    for l in c.then
                ]
            children_payload.append(cd)
        _spawn_fanout_children(
            orch, task, task_id, next_count,
            children=children_payload,
            width=width,
            manager_agent=agent,
            join_summary=decision.join_summary,
            step_audit_id=_step_audit_id,
        )
        return

    if decision.action == "delegate":
        # Classify delegate validation errors. A configured reviewer leg that
        # omits expect_verdict (THR-175 HARD REJECT) is a recoverable authoring
        # error — feedback task result + feedback orchestration step, root back
        # to PENDING, one self re-enqueue so the owner corrects the decision
        # (never a root failure). Missing agent name / missing workspace are
        # unrecoverable for this step and keep hard terminal failure.
        err = _validate_delegate(orch, decision)
        if err is not None:
            if _is_reviewer_omission_error(err):
                _reject_reviewer_omission(
                    orch, task_id, agent, next_count, err,
                )
            else:
                note = f"invalid delegate: {err}"
                _fail(orch, task_id, note=note)
                _enqueue_parent_if_waiting(orch, task_id)
                _maybe_post_thread_followup(
                    orch, task_id,
                    status=TaskStatus.FAILED, auto_revisit_spawned=False,
                )
            return
        # Target-scope guard. Managers: own-team agents or self. Non-manager
        # owners: self only. Violations feed a feedback step back (not a hard
        # fail) so the owner can correct its decision next step.
        out_of_scope = _legs_out_of_scope(orch, owner=agent, decision=decision)
        if out_of_scope:
            parts = [f"{name!r} ({reason})" for name, reason in out_of_scope]
            if orch.teams.is_team_manager(agent):
                caller_team = orch.teams.team_for_manager(agent)
                feedback = (
                    f"Invalid delegation: you are on team {caller_team!r}, but "
                    f"{'; '.join(parts)}. Pick agents on your own team or "
                    "yourself, or escalate."
                )
            else:
                feedback = (
                    f"Invalid delegation: {'; '.join(parts)}. You may only "
                    f"delegate sub-tasks to yourself ({agent!r}), or escalate."
                )
            db.insert_task_result(
                task_id=task_id,
                agent=agent,
                session_id="",
                status="completed",
                confidence_score=0,
                output_summary=feedback,
                risks_flagged=[],
            )
            orch._audit.log_orchestration_step(
                task_id, next_count, {"action": "feedback", "reason": feedback},
            )
            db.update_task(task_id, status=TaskStatus.PENDING, block_kind=None)
            if orch._queue is not None:
                orch._queue.put_nowait(orch._slug, task_id)
            return

        # THR-078 seq15: MANDATORY retry-link.  When this parent has FAILED
        # children and the owner is re-delegating to the same agent as a
        # failed child, revisit_of_task_id is REQUIRED — even the first retry
        # of a failed slice is DISALLOWED without the field.  Omission is
        # hard-rejected (feedback + re-enqueue), never silently treated as
        # an unlinked fresh dispatch that resets the ceiling.
        retry_link_err = _check_retry_link_required(
            orch,
            task_id,
            target_agent=decision.agent,
            revisit_of_task_id=decision.revisit_of_task_id,
        )
        if retry_link_err is not None:
            _reject_retry_link_decision(
                orch, task_id, agent, next_count, retry_link_err,
            )
            return

        from runtime.models import TaskRecord
        # Revision tracking: bump the delegating task's revision_count only
        # when the manager re-delegates to the *worker-of-record* — i.e. the
        # earliest-completed child. By convention, the first delegated child
        # is the worker for this task (true for both Content Team and
        # Engineering Team flows); subsequent same-agent delegations are
        # genuine revise cycles. Re-delegating to QA/reviewer is *not* a
        # revision and must not bump the count (spec
        # `protocol/05a-teams.md`: "manager escalates after 2 rounds").
        existing_children = db.get_children(task_id)
        completed_children = []
        for cid in existing_children:
            c = db.get_task(cid)
            if c is not None and c.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                completed_children.append(c)
        if completed_children:
            # Earliest by created_at; tie-break on id for determinism.
            completed_children.sort(key=lambda c: (c.created_at, c.id))
            worker_of_record = completed_children[0].assigned_agent
            # Self-targeted delegation is a sequence step (self-decomposition),
            # NOT a revise cycle — only bump when re-delegating to a DIFFERENT
            # worker-of-record. `agent` is this task's owner.
            if worker_of_record == decision.agent and decision.agent != agent:
                cap = load_org_config(orch._paths).max_revise_rounds
                if cap > 0 and task.revision_count >= cap:
                    # THR-026 seq33: revise-round budget exhausted.
                    # DELIBERATE stop-with-best — do NOT increment, do NOT
                    # delegate, do NOT auto-revisit. Mirror the section-2
                    # step-budget terminal's root/non-root split.
                    reason = f"iteration_budget_exhausted: revise budget ({cap} rounds) exhausted"
                    if not is_root(task):
                        _fail(orch, task_id, note=reason)
                        _enqueue_parent_if_waiting(orch, task_id)
                        _maybe_post_thread_followup(
                            orch, task_id,
                            status=TaskStatus.FAILED, auto_revisit_spawned=False,
                        )
                        return
                    # Root: park in escalated for the founder (CAS-free —
                    # we are past the atomic claim in section 3, so there is
                    # no duplicate-delivery race).
                    if not db.try_escalate_runtime(
                        task_id,
                        reason=reason,
                        agent="orchestrator",
                        reason_code="runtime_revise_budget_exhausted",
                    ):
                        logger.debug(
                            "run_step %s: cancelled between re-check and "
                            "revise-budget escalate, dropping", task_id,
                        )
                        return
                    orch.notify_escalated(
                        task_id=task_id, agent="orchestrator", reason=reason,
                    )
                    _maybe_post_thread_escalation(orch, task_id, reason=reason)
                    return
                db.increment_revision_count(task_id)
        child_id = db.next_task_id()
        child = TaskRecord(
            id=child_id,
            team=task.team,
            brief=decision.prompt or "",
            assigned_agent=decision.agent,
            parent_task_id=task_id,
            status=TaskStatus.PENDING,
            session_timeout_seconds=task.session_timeout_seconds,
            task_type="subtask",
            # THR-078: carry revisit_of_task_id from the owner's decision
            # so the fan-out barrier can derive per-slice retry count
            # from existing DB lineage (no schema migration).
            revisit_of_task_id=decision.revisit_of_task_id,
        )

        # THR-109: validate and prepare decision attachments.
        # Run validation when ANY leg (direct or chain) declares attachments.
        delegate_attachment_params: list[dict] | None = None
        has_any_decision_att = bool(decision.attachments) or any(
            getattr(leg, 'attachments', None) for leg in (decision.then or [])
        )
        if has_any_decision_att:
            try:
                prevalidated = _validate_decision_attachments(orch, decision)
                if prevalidated:
                    delegate_attachment_params = _prepare_attachment_params(
                        orch, prevalidated,
                    )
            except ValueError as e:
                note = f"invalid decision attachments: {e}"
                _fail(orch, task_id, note=note)
                _enqueue_parent_if_waiting(orch, task_id)
                _maybe_post_thread_followup(
                    orch, task_id,
                    status=TaskStatus.FAILED, auto_revisit_spawned=False,
                )
                return

        # Atomic CAS: insert child + transition parent to IN_PROGRESS(DELEGATED)
        # under the same RLock acquisition. Serializes against /cancel via
        # Database RLock — closes the spawn-new-work race (Codex P1 on PR #34).
        # If False: cancel landed between Guard B's re-fetch and here. No child
        # was inserted, no parent overwrite, no enqueue. The founder's terminal
        # state wins. Drop silently — the founder cancelled deliberately.
        #
        # THR-109: active_chain is written inside try_delegate in the SAME
        # transaction as child insert + parent update + attachment links/audit.
        # A crash or write failure rolls back everything atomically — no
        # orphan chain state, no orphan child, no broken parent state.
        chain_json: str | None = None
        if decision.then or decision.expect_verdict is not None:
            from runtime.orchestrator.chain import ChainState
            chain = ChainState(
                step_index=0,
                first_leg_expect_verdict=decision.expect_verdict,
                legs=list(decision.then),
                step_audit_id=_step_audit_id,
            )
            chain_json = chain.serialize()
        if not db.try_delegate(
            task_id, child,
            parent_note=f"Delegated to {decision.agent} (child={child_id})",
            attachments=delegate_attachment_params,
            active_chain_json=chain_json,
            uploaded_by=agent,
        ):
            logger.debug(
                "run_step %s: cancelled between re-check and delegate, dropping",
                task_id,
            )
            return
        logger.debug("run_step %s: try_delegate SUCCEEDED, child=%s", task_id, child_id)
        if orch._queue is not None:
            orch._queue.put_nowait(orch._slug, child_id)
        return

    # ---- 8. Unknown action ----
    note = f"unknown action: {decision.action}"
    _fail(orch, task_id, note=note)
    _enqueue_parent_if_waiting(orch, task_id)
    _maybe_post_thread_followup(
        orch, task_id,
        status=TaskStatus.FAILED, auto_revisit_spawned=False,
    )


def _reviewer_agents_for(orch: "Orchestrator") -> frozenset[str]:
    """The org's configured reviewer identities as a frozenset (THR-175).

    Resolved from the DB-backed ``reviewer_agents`` org setting (code default
    ``code_reviewer``) at the real Database seam, validated against the live
    active-agent roster — an unknown persisted name resolves fail-closed to
    the code default so ``code_reviewer`` is never silently demoted.
    """
    from runtime.orchestrator.org_config import (
        resolve_known_agent_names,
        resolve_org_setting_reviewer_agents,
    )
    return frozenset(resolve_org_setting_reviewer_agents(
        orch._db, known_agents=resolve_known_agent_names(orch._paths),
    ))


def _reviewer_omitted_expectation_error(agent: str) -> str:
    """HARD-REJECT message for a configured reviewer leg that omits
    ``expect_verdict``.  Remediation: set ``expect_verdict: "APPROVE"`` on
    every configured reviewer leg."""
    return (
        f"reviewer leg {agent!r} omits expect_verdict — HARD REJECT. "
        f"A configured reviewer leg ({agent!r}) must declare "
        f'expect_verdict: "APPROVE" so downstream QA/work only auto-advances '
        f"on an explicit approval."
    )


def _validate_one_leg(orch: "Orchestrator", *, agent: str | None, where: str) -> str | None:
    """Validate a single delegation leg (agent present + workspace exists).
    Returns None on success, a human-readable error string on failure.
    ``where`` is used only for chain-leg messages; the first-leg messages
    preserve the original wording for backward compatibility.
    """
    if not agent:
        return "missing agent name"
    workspace = orch._paths.workspaces_dir / agent
    if not workspace.exists():
        if where == "first leg":
            return f"no workspace for agent {agent!r}"
        return f"chain leg {where}: no workspace for agent {agent!r}"
    return None


def _prepare_attachment_params(
    orch: "Orchestrator",
    prevalidated: list[dict],
) -> list[dict]:
    """Build attachment param dicts from prevalidated refs for DB insertion.

    Re-checks file existence and reads size_bytes. Caller must already have
    validated the refs via ``validate_task_attachment_refs``.

    Returns a list of dicts with keys: ordinal, storage_key, display_name,
    size_bytes, content_type.
    """
    import json as _json
    from runtime.infrastructure.task_attachment_store import TaskAttachmentStore
    store = TaskAttachmentStore(orch._paths.task_attachments_dir)
    params: list[dict] = []
    for idx, pva in enumerate(prevalidated):
        path = store.path_for(pva["storage_key"])
        if not path.exists():
            raise ValueError(_json.dumps({
                "code": "task_attachment_not_found",
                "storage_key": pva["storage_key"],
            }))
        size_bytes = path.stat().st_size
        params.append({
            "ordinal": idx,
            "storage_key": pva["storage_key"],
            "display_name": pva["display_name"],
            "size_bytes": size_bytes,
            "content_type": pva["content_type"],
        })
    return params


def _validate_decision_attachments(
    orch: "Orchestrator",
    decision,
) -> list[dict] | None:
    """Validate all attachment refs across a delegate decision (direct +
    chain legs). Returns prevalidated attachment params for the direct
    delegate leg, or None if no attachments in any leg.

    Chain leg attachments are validated for early detection but their
    params are not returned here — they're re-validated when the leg spawns.
    The direct leg's prevalidated params ARE returned for immediate use.

    Always validates all legs — even when the direct leg has zero attachments
    but a later chain leg declares refs. This ensures whole-decision prevalidation:
    an invalid later-leg ref rejects the decision before any child is spawned,
    any parent parks, any active_chain is written, or any queue entry is created.

    Duplicate detection is DECISION-WIDE: a storage_key used in both
    ``decision.attachments`` and ``decision.then[i].attachments``, or in two
    different chain legs, is rejected before any child/spawn/park/chain state.
    One shared key ledger covers every declared ref in the decision.

    Raises ValueError (JSON-encoded error) on validation failure, causing
    the orchestrator to fail the task with an informative note.
    """
    from runtime.infrastructure.task_attachment_store import (
        TaskAttachmentStore,
        validate_task_attachment_refs,
    )
    store = TaskAttachmentStore(orch._paths.task_attachments_dir)
    db = orch._db

    has_direct = bool(decision.attachments)
    has_chain = any(
        getattr(leg, 'attachments', None) for leg in (decision.then or [])
    )
    if not has_direct and not has_chain:
        return None

    # One decision-wide key ledger for duplicate detection across all legs.
    seen_keys: set[str] = set()

    # Validate direct delegate attachments.
    direct_refs = list(decision.attachments) if decision.attachments else []
    prevalidated = None
    if direct_refs:
        prevalidated = validate_task_attachment_refs(
            store=store, db=db, refs=direct_refs,
            external_seen_keys=seen_keys,
        )
        for pva in prevalidated:
            seen_keys.add(pva["storage_key"])

    # Validate chain leg attachments (early detection — always run, even
    # when direct attachments are absent). Each leg's keys are checked
    # against the accumulating decision-wide seen_keys for duplicates.
    if decision.then:
        for i, leg in enumerate(decision.then):
            leg_refs = list(leg.attachments) if leg.attachments else []
            if leg_refs:
                leg_pv = validate_task_attachment_refs(
                    store=store, db=db, refs=leg_refs,
                    external_seen_keys=seen_keys,
                )
                for pva in leg_pv:
                    seen_keys.add(pva["storage_key"])

    return prevalidated


def _validate_delegate(orch: "Orchestrator", decision) -> str | None:
    """Return a human-readable error string if the delegate decision is
    unusable, or None if it's good to spawn. Validates the first leg and
    every entry in ``decision.then`` (chain legs), returning on the first
    failure encountered.

    THR-175: any configured reviewer leg (first leg or a ``then`` leg) that
    omits ``expect_verdict`` is a HARD REJECT before any child is spawned."""
    reviewer_agents = _reviewer_agents_for(orch)
    err = _validate_one_leg(orch, agent=decision.agent, where="first leg")
    if err is not None:
        return err
    # A reviewer FIRST leg only matters when it gates a downstream chain
    # (``then`` non-empty). A bare single-leg reviewer delegate (no ``then``,
    # no ``expect_verdict``) is not a chain and is not rejected.
    if decision.then and decision.agent in reviewer_agents and decision.expect_verdict is None:
        return _reviewer_omitted_expectation_error(decision.agent)
    for i, leg in enumerate(decision.then or []):
        err = _validate_one_leg(orch, agent=leg.agent, where=str(i + 2))
        if err is not None:
            return err
        if leg.agent in reviewer_agents and leg.expect_verdict is None:
            return _reviewer_omitted_expectation_error(leg.agent)
    return None


def _check_retry_link_required(
    orch: "Orchestrator",
    task_id: str,
    *,
    target_agent: str | None,
    revisit_of_task_id: str | None,
) -> str | None:
    """Validate a retry link for a delegate or one fan-out child.

    THR-078 seq15: when this parent has FAILED children and the delegate
    re-targets the agent of any FAILED child, ``revisit_of_task_id`` is
    MANDATORY — even the first retry is disallowed without the field. A
    supplied link must resolve to a FAILED child of this parent assigned to
    the re-targeted agent. Returns None only for a valid retry or a fresh
    dispatch without a link."""
    if target_agent is None:
        return None  # shouldn't happen after _validate_delegate, but safe.
    db = orch._db
    children = db.get_children(task_id)
    failed_sibling_ids: set[str] = set()
    for cid in children:
        child = db.get_task(cid)
        if child is None:
            continue
        if child.status == TaskStatus.FAILED and child.assigned_agent == target_agent:
            failed_sibling_ids.add(child.id)

    if revisit_of_task_id is None:
        if failed_sibling_ids:
            failed_ids = ", ".join(sorted(failed_sibling_ids))
            return (
                f"cannot re-delegate to {target_agent!r} without "
                f"revisit_of_task_id — this agent has FAILED child(ren) "
                f"({failed_ids}) under the same parent"
            )
        return None

    if revisit_of_task_id not in failed_sibling_ids:
        return (
            f"revisit_of_task_id {revisit_of_task_id!r} must reference a "
            f"FAILED child of this parent assigned to {target_agent!r}"
        )
    return None


def _feedback_and_reenqueue(
    orch: "Orchestrator",
    task_id: str,
    agent: str,
    next_count: int,
    feedback: str,
) -> None:
    """Record a feedback task result + feedback orchestration audit step and
    re-enqueue the SAME task for a corrected decision.

    The owner's next session must re-run and fix the decision; the root is
    never failed, no child is spawned, and no active_chain/fanout/park
    metadata is written. This is the established authoring-error feedback
    shape (out-of-scope and retry-link rejection reuse it): one
    ``task_results`` row (status=completed, confidence=0), one
    ``orchestration_step`` audit row keyed to the claimed step with
    ``{"action": "feedback", "reason": feedback}``, task back to PENDING
    with block_kind cleared, and exactly one self re-enqueue."""
    db = orch._db
    db.insert_task_result(
        task_id=task_id,
        agent=agent,
        session_id="",
        status="completed",
        confidence_score=0,
        output_summary=feedback,
        risks_flagged=[],
    )
    orch._audit.log_orchestration_step(
        task_id, next_count, {"action": "feedback", "reason": feedback},
    )
    db.update_task(task_id, status=TaskStatus.PENDING, block_kind=None)
    if orch._queue is not None:
        orch._queue.put_nowait(orch._slug, task_id)


def _is_reviewer_omission_error(err: str) -> bool:
    """True when a delegate/fanout validation error is the recoverable THR-175
    reviewer ``expect_verdict`` omission (HARD REJECT with remediation) rather
    than an unrecoverable structural error (missing agent name / missing
    workspace). Only ``_reviewer_omitted_expectation_error`` emits this text,
    so no structural error can be misclassified as recoverable."""
    return "omits expect_verdict" in err


def _reject_reviewer_omission(
    orch: "Orchestrator",
    task_id: str,
    agent: str,
    next_count: int,
    err: str,
    *,
    action_label: str = "delegation",
) -> None:
    """HARD REJECT a configured reviewer leg that omits ``expect_verdict`` as a
    recoverable authoring error: feedback task result + feedback orchestration
    audit step, root back to PENDING, one self re-enqueue — never a root
    failure (TASK-5921). ``err`` already carries the HARD REJECT message and
    the expect_verdict: "APPROVE" remediation."""
    feedback = (
        f"Invalid {action_label}: {err} "
        f"Correct the decision and re-submit with "
        f'expect_verdict: "APPROVE" on the reviewer leg.'
    )
    _feedback_and_reenqueue(orch, task_id, agent, next_count, feedback)


def _reject_retry_link_decision(
    orch: "Orchestrator",
    task_id: str,
    agent: str,
    next_count: int,
    retry_link_err: str,
) -> None:
    """Fail closed with feedback when a delegate or fan-out retry is invalid."""
    feedback = (
        f"Invalid revisit_of_task_id: {retry_link_err}. "
        f"When re-delegating to an agent with a FAILED child under this parent, "
        f"you MUST set revisit_of_task_id to that failed predecessor's task id."
    )
    _feedback_and_reenqueue(orch, task_id, agent, next_count, feedback)


def _legs_out_of_scope(orch: "Orchestrator", owner: str, decision) -> list[tuple[str, str]]:
    """Return [(agent_name, reason)] for delegation legs `owner` may not target.

    - Manager owner: may target agents on its own team, or itself.
    - Non-manager owner: may target ONLY itself (self-decomposition).

    Empty list = all legs in scope.
    """
    targets = [decision.agent] + [leg.agent for leg in (decision.then or [])]
    out: list[tuple[str, str]] = []
    if orch.teams.is_team_manager(owner):
        caller_team = orch.teams.team_for_manager(owner)
        for a in targets:
            if not a or a == owner:        # self always allowed
                continue
            t = orch.teams.team_for_agent(a)
            if caller_team is None or t != caller_team:
                out.append((a, f"on team {t!r}" if t else "not on a team"))
    else:
        for a in targets:
            if not a or a == owner:
                continue
            out.append((a, "non-manager owners may only delegate to themselves"))
    return out


def _default_agent_for_root(orch: "Orchestrator", task) -> str:
    """Root tasks default to the manager for their team."""
    return orch.teams.manager_for_team(task.team).name


def _build_agent_prompt(orch: "Orchestrator", task, agent: str) -> str:
    """Build the per-task `role_guidance` body — i.e., what gets indented under
    `role_guidance: |` in the outer wrapper built by
    ``Orchestrator._build_agent_prompt``.

    Subtask agents get their per-task instruction as the bare brief,
    which the outer wrapper already renders as ``Parameters.brief``. Echoing
    it here would duplicate the brief in every subtask agent spawn (the wrapper
    drops the ``role_guidance:`` line when this returns empty).

    Task owners return the capabilities prompt (decision schema, agent roster,
    prior steps). For revisited roots, a one-shot context header is prepended
    on the very first orchestration step (detected via audit log).
    """
    from runtime.orchestrator.capabilities import build_capabilities_prompt
    if task.task_type != "task":
        # Leaf subtask instruction is the brief, except a resumed job-block
        # needs its outcome pointer to avoid resubmitting the same job (THR-161).
        return _blocked_jobs_resume_header_if_applicable(orch, task.id) or ""
    from runtime.orchestrator import prompt_loader
    is_mgr = orch.teams.is_team_manager(agent)
    agents_for_prompt: list[dict] = []
    if is_mgr:
        for name in _list_candidate_agents(orch, agent):
            candidate = prompt_loader.load_agent(orch._paths, name)
            desc = (candidate.description if candidate is not None else None) or name
            agents_for_prompt.append({"name": name, "description": desc})
        # Self-targeting (spec §3): a manager may delegate a sub-task to itself
        # to break its own work into a fresh bounded session. _list_candidate_agents
        # only returns team workers (the manager is never in teams.yaml `workers`),
        # so advertise self explicitly in the roster.
        agents_for_prompt.append({
            "name": agent,
            "description": "yourself — delegate a sub-task to yourself to "
                           "decompose your own work into a fresh bounded session",
        })
    prior_steps = _build_prior_steps_from_db(orch, task.id)
    base = build_capabilities_prompt(
        agents=agents_for_prompt,
        step_number=task.orchestration_step_count + 1,  # 1-indexed for manager display
        max_steps=orch._settings.max_orchestration_steps,
        prior_steps=prior_steps,
        manager_name=agent,
        self_only=not is_mgr,
        reviewer_agents=sorted(_reviewer_agents_for(orch)),
    )
    headers: list[str] = []
    revisit = _revisit_header_if_applicable(orch, task.id)
    if revisit is not None:
        headers.append(revisit)
    resume_header = _blocked_jobs_resume_header_if_applicable(orch, task.id)
    if resume_header is not None:
        headers.append(resume_header)
    resolved = _resolved_escalation_header_if_applicable(orch, task.id)
    if resolved is not None:
        headers.append(resolved)
    # Fan-out join header
    fanout_join = _fanout_join_header_if_applicable(orch, task.id)
    if fanout_join is not None:
        headers.append(fanout_join)
    if headers:
        return "".join(headers) + base
    return base


def _list_candidate_agents(orch: "Orchestrator", calling_manager: str) -> list[str]:
    """Return the names of workers the calling manager can delegate to.

    Only includes workers on the calling manager's own team that have an
    existing workspace on disk. Returns an empty list when the calling_manager
    is not found in the registry (e.g. fallback / tests without a full layout).
    """
    caller_team = orch.teams.team_for_manager(calling_manager)
    if caller_team is None:
        return []
    team_members = set(orch.teams.manager_for_team(caller_team).workers)

    if orch._paths.workspaces_dir.exists():
        names = sorted(
            d.name for d in orch._paths.workspaces_dir.iterdir()
            if d.is_dir() and d.name in team_members
        )
    else:
        names = []
    return names


# Shared discipline tail appended to both revisit headers. Addresses the
# brief-vs-reality divergence failure mode (TALK-028, tourism-org): on a
# revisit-spawned session the literal brief is often stale, and the manager
# tends either to (a) execute the brief verbatim and stall against current
# state, or (b) improvise "the next obvious step" and get blocked by
# classifiers/workflow gates. The discipline frames the binary choice
# (execute-with-divergence-note OR escalate-with-diagnosis) and explicitly
# bans improvisation. Generic enough for any manager role.
_REVISIT_DISCIPLINE_LINES = [
    "Status-assess before acting on the brief below — it was authored before this "
    "revisit and may be stale. Inspect the predecessor (commands above) and verify "
    "ground truth for the work the brief describes. Then either: execute the real "
    "next step, noting any divergence from the brief in your output_summary; or "
    "escalate with a precise diagnosis (what the brief asked, what reality is, why "
    "the gap is unbridgeable). Do NOT improvise — half-completed work blocks the "
    "workstream.",
]


def _revisit_header_if_applicable(orch: "Orchestrator", task_id: str) -> str | None:
    """Return a revisit context header, or None.

    Trigger: the task has a `revisit_of` OR `auto_revisit_of` audit entry
    AND no `orchestration_step` audit entry. The latter is how we detect
    "first step" without timestamps — once the task owner has produced
    a decision, `log_orchestration_step` writes a row and this helper
    returns None on every subsequent call.
    """
    logs = orch._db.get_audit_logs(task_id)
    revisit_entry = next(
        (e for e in logs if e["action"] in ("revisit_of", "auto_revisit_of")),
        None,
    )
    if revisit_entry is None:
        return None
    if any(e["action"] == "orchestration_step" for e in logs):
        return None

    if revisit_entry["action"] == "auto_revisit_of":
        return _auto_revisit_header(revisit_entry["payload"])

    payload = revisit_entry["payload"]
    predecessor = payload["predecessor_root"]
    flagged = payload["flagged"]
    prior_status = payload["prior_status"]
    cascade = payload.get("cascade") or [predecessor]
    note = payload.get("founder_note")

    lines = [
        f"REVISIT CONTEXT: this root is a revisit of {predecessor} "
        f"(which ended in {prior_status}).",
        f"Founder flagged {flagged} in the predecessor lineage — "
        "start your investigation there.",
        "Cascade chain (predecessor root -> flagged): "
        + " -> ".join(cascade),
    ]
    if note:
        lines.append(f"Founder note: {note}")
    lines.append(
        f"Inspect via: `happyranch details {predecessor}`, "
        f"`happyranch audit {predecessor}`, `happyranch recall {predecessor}`."
    )
    lines.append(
        "You may reuse successful sub-tasks' artifacts (referenced by path in "
        "new child briefs); old child task rows stay frozen."
    )
    lines.extend(_REVISIT_DISCIPLINE_LINES)

    # JOB summary block — list any jobs submitted by the predecessor.
    predecessor_logs = orch._db.get_audit_logs(predecessor)
    sr_entries = [e for e in predecessor_logs if e.get("action") == "job_submitted"]
    if sr_entries:
        lines.append("")
        lines.append("This task previously submitted jobs:")
        for e in sr_entries:
            payload_e = e.get("payload") or {}
            if isinstance(payload_e, str):
                import json as _json  # noqa: PLC0415

                try:
                    payload_e = _json.loads(payload_e)
                except Exception:
                    payload_e = {}
            job_id = payload_e.get("script_request_id", "JOB-?")
            title = payload_e.get("title", "(no title)")
            sr = orch._db.get_job(job_id) if job_id != "JOB-?" else None
            status = sr.status.value if sr else "?"
            marker = ""
            if sr and sr.status.value in ("pending", "running"):
                marker = " [still pending — founder action needed]"
            lines.append(f"  - {job_id} ({status}) — {title}{marker}")
        lines.append("")
        lines.append("Read the outputs / rejection reasons before continuing:")
        for e in sr_entries:
            payload_e = e.get("payload") or {}
            if isinstance(payload_e, str):
                import json as _json  # noqa: PLC0415

                try:
                    payload_e = _json.loads(payload_e)
                except Exception:
                    payload_e = {}
            job_id = payload_e.get("script_request_id", "JOB-?")
            lines.append(f"  happyranch jobs show {job_id}")
            lines.append(f"  happyranch jobs output {job_id}")

    return "\n".join(lines) + "\n\n"


def _auto_revisit_header(payload: dict) -> str:
    """Render the first-step header for an orchestrator-triggered auto-revisit.

    Different language from the founder-revisit header: the manager needs
    to know an opaque agent failure happened (not a founder-flagged
    problem) and to consider whether the original approach is still sound
    or whether the failure mode suggests a different decomposition.
    """
    predecessor = payload["predecessor_root"]
    failed_task = payload["failed_task"]
    failed_agent = payload["failed_agent"]
    cascade = payload.get("cascade") or [failed_task]
    err = payload.get("error_context") or {}
    attempt = payload.get("attempt", 1)
    failure_kind = payload.get("failure_kind") or "session_failed"

    err_bits: list[str] = []
    mode = err.get("mode")
    if mode == "exception":
        err_bits.append(f"exception: {err.get('detail', '?')}")
    elif mode == "session_failure":
        rc = err.get("rc")
        err_bits.append(f"rc={rc if rc is not None else '?'}")
        if err.get("missing_callback"):
            err_bits.append("no completion callback")
        executor_error = err.get("executor_error")
        if executor_error:
            err_bits.append(executor_error)
        stderr_tail = err.get("stderr_tail") or ""
        stdout_tail = err.get("stdout_tail") or ""
        preview = stderr_tail or stdout_tail
        if preview:
            label = "stderr" if stderr_tail else "stdout"
            err_bits.append(f"{label}: {preview.replace(chr(10), ' ')}")
    err_summary = "; ".join(err_bits) if err_bits else "(no diagnostics)"

    lines = [
        f"AUTO-REVISIT CONTEXT (orchestrator-triggered, kind={failure_kind}, "
        f"attempt {attempt}): "
        f"this root is a revisit of {predecessor}, "
        "spawned because an agent in the predecessor lineage hit an opaque "
        "failure.",
        f"Failed task: {failed_task} (agent: {failed_agent}).",
        f"Failure: {err_summary}",
        "Cascade chain (predecessor root -> failed task): "
        + " -> ".join(cascade),
        f"Inspect via: `happyranch details {predecessor}`, "
        f"`happyranch audit {predecessor}`, `happyranch recall {predecessor}`.",
        "Re-evaluate the approach — the failure may be transient (worth "
        "the same plan with a fresh subprocess) or structural (a different "
        "decomposition is needed). Decide accordingly.",
    ]
    lines.extend(_REVISIT_DISCIPLINE_LINES)
    return "\n".join(lines) + "\n\n"


def _blocked_jobs_resume_header_if_applicable(
    orch: "Orchestrator", task_id: str,
) -> str | None:
    """Return a BLOCKED-JOBS-RESULTS header on the first agent step after a
    task resumes from a job-block, otherwise None.

    Trigger: the most recent `task_resumed_from_jobs` audit entry for this task
    has a higher row id than the most recent `orchestration_step` entry —
    i.e. the jobs are terminal AND the agent hasn't run yet. Audit `id` is
    autoincrement, so id-ordering is equivalent to chronological ordering.
    Once the agent produces its first decision after resume,
    `log_orchestration_step` writes a row with a higher id and this helper
    returns None on every subsequent call.

    Spec: §6.4.
    """
    import json as _json  # noqa: PLC0415

    logs = orch._db.get_audit_logs(task_id)
    last_resumed = None
    last_step = None
    for entry in logs:
        action = entry["action"]
        if action == "task_resumed_from_jobs":
            last_resumed = entry
        elif action == "orchestration_step":
            last_step = entry
    if last_resumed is None:
        return None
    if last_step is not None and last_step["id"] > last_resumed["id"]:
        return None

    payload = last_resumed["payload"] or {}
    if isinstance(payload, str):
        try:
            payload = _json.loads(payload)
        except Exception:
            payload = {}
    job_ids: list[str] = payload.get("blocking_job_ids", [])
    outcomes: dict[str, str] = payload.get("job_outcomes", {})

    lines: list[str] = [
        "=== BLOCKED-JOBS-RESULTS (system) ===",
        f"You self-blocked on {', '.join(job_ids)}. They are now terminal:",
        "",
    ]
    for jid in job_ids:
        status = outcomes.get(jid, "unknown")
        lines.append(f"  {jid}  {status}")
        lines.append(f"          → happyranch jobs show {jid}")
        lines.append(f"          → happyranch jobs output {jid}")
    lines.append("")
    lines.append("Re-read your task brief; decide whether to proceed, retry, or escalate.")
    lines.append("======================================")
    return "\n".join(lines) + "\n\n"


def _resolved_escalation_header_if_applicable(
    orch: "Orchestrator", task_id: str,
) -> str | None:
    """Return a 2-3 line header on the first manager step after a founder
    `resolve-escalation --continue` OR an authority-policy same-root
    continuation, otherwise None.

    Trigger: the most recent `escalation_resolved` OR
    `authority_continued_same_root` audit entry for this task has a higher
    row id than the most recent `orchestration_step` entry — i.e. the
    continuation happened AND the manager hasn't run yet. Audit `id` is
    autoincrement, so id-ordering is equivalent to chronological ordering.
    Once the manager produces its first decision after re-enqueue,
    `log_orchestration_step` writes a row with a higher id and this helper
    returns None on every subsequent call.
    """
    logs = orch._db.get_audit_logs(task_id)
    last_resolved = None
    last_authority_continue = None
    last_step = None
    for entry in logs:
        action = entry["action"]
        if action == "escalation_resolved":
            last_resolved = entry
        elif action == "authority_continued_same_root":
            last_authority_continue = entry
        elif action == "orchestration_step":
            last_step = entry
    if last_authority_continue is not None and (
        last_step is None or last_step["id"] < last_authority_continue["id"]
    ):
        # THR-181 Track A (founder lifecycle envelope): the continuation header is the
        # continued turn's same-root lifecycle notice. Fail-closed: it is
        # shown ONLY while the single-use continuation envelope is ACTIVE for
        # the root — a stale/resolved/replayed continuation without a live
        # envelope never presents as a continuation (ordinary turn instead).
        if orch._db.get_active_authority_continue_envelope(task_id) is None:
            return None
        payload = last_authority_continue["payload"] or {}
        policy_id = payload.get("policy_id", "(unknown policy)")
        policy_version = payload.get("policy_version", "?")
        clause_id = payload.get("clause_id", "(unknown clause)")
        action = payload.get("action", "(unknown action)")
        return (
            f"AUTHORITY POLICY CONTINUED SAME ROOT: policy {policy_id} "
            f"v{policy_version} matched clause {clause_id} "
            f"(permitted action: {action}).\n"
            "Your proposed escalation was not committed; continue the same "
            "root within that permitted action.\n"
            "THIS TURN USES YOUR ORDINARY CONFIGURED EXECUTOR PERMISSIONS "
            "and normal manager-decision validation. The single-use lifecycle "
            "envelope is not an exact-action whitelist. Same-root identity, "
            "cancellation, replay, CAS, budgets, protected boundaries, and "
            "terminal audit remain daemon-owned; supersession, successor, "
            "revisit, and fresh-root replacement remain outside this grant.\n\n"
        )
    if last_resolved is None:
        return None
    if last_step is not None and last_step["id"] > last_resolved["id"]:
        return None
    payload = last_resolved["payload"] or {}
    decision = payload.get("decision", "continue")
    # Cancel is terminal — no resume header should ever fire for a cancelled
    # escalation.
    if decision == "cancel":
        return None
    rationale = payload.get("rationale", "(no rationale recorded)")
    return (
        f"ESCALATION RESOLVED: founder continued your prior escalation.\n"
        f"Rationale: {rationale}\n"
        "Continue from where you parked, with this verdict in mind.\n\n"
    )


def _build_prior_steps_from_db(orch: "Orchestrator", task_id: str):
    """Reconstruct StepRecord[] for the parent task by reading subtasks'
    terminal outcomes from the DB. Only direct children of `task_id` count
    — each child is one past orchestration step. Order: creation order,
    1-indexed.

    If a chain ran since the last manager wake, a synthetic chain-summary
    entry is appended so the manager can see what happened without re-deriving
    it from raw child task records.
    """
    from runtime.models import StepRecord
    steps: list[StepRecord] = []
    for i, child_id in enumerate(orch._db.get_children(task_id), start=1):
        child = orch._db.get_task(child_id)
        if child is None:
            continue
        success = child.status == TaskStatus.COMPLETED
        steps.append(StepRecord(
            step_number=i,
            agent=child.assigned_agent or "unknown",
            action=f"delegate: {(child.brief or '')[:100]}",
            result_summary=child.note or "(no summary)",
            success=success,
        ))
    # Append chain summary if a chain ran since the last manager wake.
    chain_summary = _summarize_recent_chain(orch, task_id)
    if chain_summary is not None:
        steps.append(StepRecord(
            step_number=len(steps) + 1,
            agent="orchestrator",
            action="chain summary",
            result_summary=chain_summary,
            success=True,
        ))
    return steps


def _summarize_recent_chain(orch: "Orchestrator", parent_task_id: str) -> str | None:
    """One-line summary of the most-recent chain that ran under parent_task_id.

    Returns None if no chain_auto_advance audit rows exist on the parent.
    Otherwise pairs the audit rows (which list triggering_child_id and
    spawned_child_id) with the final spawned child's terminal verdict to
    produce a human-readable line for the manager's wake context.
    """
    audit_logs = orch._db.get_audit_logs(parent_task_id)
    rows = [r for r in audit_logs if r["action"] == "chain_auto_advance"]
    if not rows:
        return None
    # Suppress the summary if a manager decision (orchestration_step) has
    # landed AFTER the most-recent chain advance — the manager has already
    # seen this chain summary in the wake where the chain ended, and the
    # current wake is for a later non-chain event. Showing it again would
    # place a stale chain summary at the end of prior_steps, misrepresenting
    # the latest event.
    max_chain_id = max(r["id"] for r in rows)
    max_step_id = max(
        (r["id"] for r in audit_logs if r["action"] == "orchestration_step"),
        default=0,
    )
    if max_step_id > max_chain_id:
        return None
    # Filter to the most-recent chain only — multiple sequential chains may
    # share the same parent across separate manager wakes, distinguished by
    # the chain_origin_step_audit_id of the orchestration_step that minted
    # each chain.
    latest_origin_id = rows[-1]["payload"]["chain_origin_step_audit_id"]
    rows = [
        r for r in rows
        if r["payload"]["chain_origin_step_audit_id"] == latest_origin_id
    ]
    triggers = [r["payload"]["triggering_child_id"] for r in rows]
    spawned = [r["payload"]["spawned_child_id"] for r in rows]
    chain_children = triggers + ([spawned[-1]] if spawned else [])
    last_child_id = chain_children[-1]
    last_report = orch._db.get_latest_completion_report(last_child_id)
    last_verdict = last_report.verdict if last_report else None
    arrow = " → ".join(chain_children)
    if last_report and last_report.status == "blocked":
        return f"Chain aborted at {last_child_id}: self-blocked"
    if last_verdict is not None:
        return f"Chain: {len(chain_children)} legs ({arrow}), final verdict {last_verdict}"
    return f"Chain: {len(chain_children)} legs ({arrow})"


def _is_already_terminal(orch: "Orchestrator", task_id: str) -> bool:
    """Shared idempotence predicate for the four decision branches.

    Returns True when the row is gone, already terminal (COMPLETED / FAILED),
    or cancelled — any state where a subsequent decision must not overwrite
    the task's status, note, or spawn children.

    Includes `cancelled_at` explicitly as defense in depth: today `/cancel`
    always flips status to FAILED alongside stamping `cancelled_at`, so the
    `status in TERMINAL_STATES` check covers the cancelled case. But if a
    future code path ever stamps `cancelled_at` without touching status, this
    predicate still does the right thing.

    Closes the cancel-race documented in
    docs/superpowers/specs/2026-05-26-cancel-race-design.md §5.3.
    """
    existing = orch._db.get_task(task_id)
    return (
        existing is None
        or existing.status in TERMINAL_STATES
        or existing.cancelled_at is not None
    )


def _complete(orch: "Orchestrator", task_id: str, *, note: str, output_dir: str | None = None) -> None:
    from datetime import datetime, timezone
    # Idempotence guard: /cancel may have already taken this task to FAILED
    # between Popen return and here. Don't resurrect a cancelled task back to
    # COMPLETED just because the subprocess happened to finish cleanly before
    # SIGTERM arrived.
    if _is_already_terminal(orch, task_id):
        return
    orch._db.update_task(
        task_id,
        status=TaskStatus.COMPLETED,
        block_kind=None,
        note=note,
        final_output_dir=output_dir,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    _log_verdict_if_delegated(orch, task_id, success=True)
    orch._update_task_history(task_id)
    _kill_jobs_for_terminating_task(orch, task_id)


def _fail(orch: "Orchestrator", task_id: str, *, note: str) -> None:
    from datetime import datetime, timezone
    # Idempotence guard — same rationale as _complete. When /cancel SIGTERMs
    # the subprocess, run_step re-enters via the post-execution classifier and
    # tries to write a "session failed (rc=-15; ...)" note. That must NOT
    # overwrite the cancel route's "cancelled by <actor>: ..." note.
    if _is_already_terminal(orch, task_id):
        return
    # THR-181 Track A (founder lifecycle envelope): a failure closes the single-use
    # continuation window fail-closed — the continuation was never exercised
    # for its permitted action and must never be re-used.
    try:
        _t = orch._db.get_task(task_id)
        orch._db.spend_authority_continue_envelope_if_active(
            task_id,
            audit_agent=(
                _t.assigned_agent if _t is not None else "orchestrator"
            ) or "orchestrator",
            error="task failed without the permitted continued-turn decision",
        )
    except Exception:  # pragma: no cover - fail-closed defensive
        pass
    # Clear any in-flight chain so the CLI/Web UI doesn't show a chain strip
    # on a FAILED task. The chain can't re-activate (the task is terminal),
    # but the dangling state is cosmetically misleading. Always-clear is
    # cheap and works for cascade-fail, self-blocked, invalid-delegate, and
    # session-failure failure modes.
    orch._db.update_task_active_chain(task_id, None)
    orch._db.update_task_active_fanout(task_id, None)
    orch._db.update_task(
        task_id,
        status=TaskStatus.FAILED,
        block_kind=None,
        note=note,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    _log_verdict_if_delegated(orch, task_id, success=False)
    orch._update_task_history(task_id)
    _kill_jobs_for_terminating_task(orch, task_id)


def _kill_jobs_for_terminating_task(orch: "Orchestrator", task_id: str) -> None:
    """Fire-and-forget: kill all in-flight persistent jobs owned by ``task_id``.

    Called from ``_complete`` and ``_fail`` whenever a task transitions to a
    terminal state. The kill runs out-of-band so task-row progression never
    blocks on job cleanup (5s SIGTERM grace + SIGKILL).

    The DB row update for the killed jobs is a backstop in case the runner's
    own bookkeeping (via ``_KILL_REASON_OVERRIDE``) doesn't get to commit —
    e.g., the run_step thread exits before the runner coroutine finishes its
    final UPDATE. With persistent jobs the runner is its own background task
    and should complete normally; this path matters mostly during shutdown
    races. We use the same loop-detection / daemon-thread fallback as
    ``Orchestrator.notify_failed`` because ``run_step`` runs on a thread-pool
    worker with no event loop of its own.
    """
    db = orch._db
    rows = db._conn.execute(
        "SELECT id, task_id FROM jobs WHERE status='running'"
    ).fetchall()
    inflight_map = {row["id"]: row["task_id"] for row in rows}
    if not any(v == task_id for v in inflight_map.values()):
        return

    from datetime import datetime, timezone

    import asyncio
    import threading

    from runtime.daemon.jobs_runner import terminate_jobs_for_task

    async def _kill_and_backstop() -> None:
        await terminate_jobs_for_task(task_id, inflight_to_task=inflight_map)
        # Backstop DB update — guarded by status='running' so we don't trample
        # the runner's own terminal write if it got there first.
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for row_id, row_task in inflight_map.items():
            if row_task == task_id:
                db._conn.execute(
                    "UPDATE jobs SET status='failed', reason='task_ended', "
                    "finished_at=? WHERE id=? AND status='running'",
                    (now, row_id),
                )
        db._conn.commit()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop — run_step thread-pool worker. Spawn a daemon thread
        # that owns its own loop.
        threading.Thread(
            target=lambda: asyncio.run(_kill_and_backstop()),
            daemon=True,
        ).start()
    else:
        loop.create_task(_kill_and_backstop())


def _log_verdict_if_delegated(
    orch: "Orchestrator", task_id: str, *, success: bool,
) -> None:
    """Emit the review_verdict audit row for a delegated subtask.

    The parent task owner is the implicit reviewer of every delegated subtask.
    The audit row's verdict is a DISTINCT fact from the subtask's completion
    status: when the subtask reported an explicit structured
    ``CompletionReport.verdict`` (APPROVE / PASS / REQUEST_CHANGES / ...), that
    reported workflow verdict is preserved verbatim. Only when no structured
    verdict is present do we fall back to the legacy implicit mapping
    (COMPLETED -> "approved", FAILED -> "rejected").

    Audit rows are how the founder reviews which agents need attention; they
    are the canonical record of delegation outcomes.
    """
    task = orch._db.get_task(task_id)
    if task is None or task.parent_task_id is None:
        return
    agent = task.assigned_agent
    if not agent or orch.teams.is_team_manager(agent) or agent in ("orchestrator", "unknown"):
        return
    parent = orch._db.get_task(task.parent_task_id)
    reviewer_team = parent.team if parent else task.team
    try:
        reviewer = orch.teams.manager_for_team(reviewer_team).name
    except KeyError:
        reviewer = "unknown_manager"
    orch._audit.log_review_verdict(
        task_id=task_id,
        reviewer=reviewer,
        verdict=_verdict_for_delegated(orch, task_id, success=success),
        feedback=task.note,
        reviewed_agent=agent,
    )


def _verdict_for_delegated(
    orch: "Orchestrator", task_id: str, *, success: bool,
) -> str:
    """Resolve the review_verdict string for a delegated subtask.

    An explicit structured ``CompletionReport.verdict`` is the worker's own
    workflow verdict and is preserved verbatim — including an explicitly blank
    value (readers treat blank/unknown as "unknown", never as approved). Only
    when no structured verdict is present does the caller's completion-status
    mapping apply (``success`` -> "approved", otherwise "rejected").
    """
    report = orch._db.get_latest_completion_report(task_id)
    if report is not None and report.verdict is not None:
        return report.verdict
    return "approved" if success else "rejected"


def _advance_chain_for_completed_child(
    *,
    orch: "Orchestrator",
    parent_task_id: str,
    child_task_id: str,
    report=None,
) -> str:
    """Inspect the parent's active_chain against the just-completed subtask's
    report. Either spawn the next leg ("advance") or clear the chain so the
    caller falls through to the normal parent-wake path ("wake").

    Returns "advance" or "wake". When "advance" is returned, the parent is
    NOT re-enqueued and orchestration_step_count is NOT bumped.

    Only called when child.status == COMPLETED — FAILED subtasks are handled
    by the bounded-wake logic in _enqueue_parent_if_waiting (TASK-573).

    THR-211: ``report`` carries the exact authenticated CompletionReport for
    the child's current session (``_child_landed_terminal_report``). When it
    is provided it is authoritative — a newer unrelated row can never advance
    or clear the chain. When it is ``None`` (a genuinely legacy child with no
    agent/session fingerprint, or a direct caller that does not scope), the
    most-recent-row read is kept for compatibility. When it is the
    ``_NO_AUTHENTICATED_REPORT`` sentinel (a modern child whose exact report
    is missing or unacceptable — exact-row miss, wrong agent/session,
    malformed/unknown/blocked/nonterminal) the chain FAILS CLOSED: it is
    cleared and the parent wakes, and task-wide evidence is never consulted.
    """
    from runtime.models import TaskRecord
    from runtime.orchestrator.chain import (
        ChainState,
        build_prior_leg_context,
        compute_advance_action,
    )

    parent = orch._db.get_task(parent_task_id)
    if parent is None or parent.active_chain is None:
        return "wake"

    chain = ChainState.deserialize(parent.active_chain)
    if report is _NO_AUTHENTICATED_REPORT:
        # TASK-5818: a modern fingerprint exists but the exact authenticated
        # report is missing/unacceptable — fail closed.  Never consult the
        # task-wide newest-row read: a wrong-agent/wrong-session (or any
        # unrelated) row must not gate chain advancement.
        orch._db.update_task_active_chain(parent_task_id, None)
        return "wake"
    if report is None:
        report = orch._db.get_latest_completion_report(child_task_id)
    if report is None:
        orch._db.update_task_active_chain(parent_task_id, None)
        return "wake"

    completed_child = orch._db.get_task(child_task_id)
    completed_agent = completed_child.assigned_agent if completed_child is not None else None
    action = compute_advance_action(
        chain=chain,
        report=report,
        completed_agent=completed_agent,
        reviewer_agents=_reviewer_agents_for(orch),
    )
    if action.kind == "wake":
        orch._db.update_task_active_chain(parent_task_id, None)
        return "wake"

    # THR-109: validate attachments FIRST, before any state change.
    # If validation fails the chain is cleared and parent wakes — no
    # child, no link/audit, no queue entry, no advanced active_chain.
    prior_context = build_prior_leg_context(
        child_task_id=child_task_id, report=report,
    )
    full_brief = action.next_leg.prompt + prior_context

    chain_att_params: list[dict] | None = None
    if action.next_leg.attachments:
        from runtime.infrastructure.task_attachment_store import (
            TaskAttachmentStore,
            validate_task_attachment_refs,
        )
        store = TaskAttachmentStore(orch._paths.task_attachments_dir)
        try:
            prevalidated = validate_task_attachment_refs(
                store=store, db=orch._db, refs=list(action.next_leg.attachments),
            )
            if prevalidated:
                chain_att_params = _prepare_attachment_params(orch, prevalidated)
        except ValueError:
            # Attachment validation failed — do NOT spawn the leg.
            # Clear the chain so the parent can wake and handle the failure.
            orch._db.update_task_active_chain(parent_task_id, None)
            return "wake"

    # Atomic chain advance: update parent active_chain + insert next child +
    # attachment links/audit in a single transaction. A crash or write failure
    # rolls back everything — no orphan chain state, no orphan child, no
    # orphan link/audit/claim, no queue side effect.
    next_child_id = orch._db.next_task_id()
    chain.step_index = action.next_step_index
    chain_json = chain.serialize()

    next_child = TaskRecord(
        id=next_child_id,
        team=parent.team,
        brief=full_brief,
        parent_task_id=parent_task_id,
        assigned_agent=action.next_leg.agent,
        status=TaskStatus.PENDING,
        session_timeout_seconds=parent.session_timeout_seconds,
        task_type="subtask",
    )
    if not orch._db.try_advance_chain(
        parent_id=parent_task_id,
        active_chain_json=chain_json,
        next_child=next_child,
        attachments=chain_att_params,
        uploaded_by="orchestrator",
    ):
        # Chain advance + child insert failed — transaction rolled back.
        # No partial state remains; parent wakes normally.
        return "wake"

    orch._audit.log_chain_auto_advance(
        parent_task_id=parent_task_id,
        leg_index=action.next_step_index,
        spawned_child_id=next_child_id,
        triggering_child_id=child_task_id,
        triggering_verdict=report.verdict,
        chain_origin_step_audit_id=chain.step_audit_id,
    )
    if orch._queue is not None:
        orch._queue.put_nowait(orch._slug, next_child_id)
    return "advance"


def _is_carrier(orch: "Orchestrator", parent: "TaskRecord") -> bool:
    """True if ``parent`` is a pipeline carrier — its own parent has active_fanout set.
    Carrier detection is schema-free: a carrier is any task whose id is in its
    parent's active_fanout.children_ids and which has (or had) an active_chain.
    We detect it by checking if the grandparent has active_fanout."""
    if parent.parent_task_id is None:
        return False
    grandparent = orch._db.get_task(parent.parent_task_id)
    return grandparent is not None and grandparent.active_fanout is not None


def _carrier_fail_on_verdict_mismatch(
    orch: "Orchestrator", parent: "TaskRecord",
    child_task_id: str, chain_snapshot: str | None, report=None,
) -> bool:
    """After a carrier's chain leg completed but the chain did not advance
    (outcome == "wake"), check if this was a verdict mismatch.  If so, fail
    the carrier immediately (fail-closed at carrier).  Returns True if the
    carrier was failed, False otherwise (including non-carriers).

    THR-211: ``report`` carries the exact authenticated CompletionReport for
    the child's current session when one exists; it is authoritative over the
    most-recent-row read, which remains the fallback when no fingerprint
    exists (compatibility with pre-THR-211 behavior).  The
    ``_NO_AUTHENTICATED_REPORT`` sentinel (modern fingerprint, no acceptable
    exact report) fails the carrier closed — an unverifiable leg outcome must
    never complete or advance a carrier on task-wide evidence.
    """
    if chain_snapshot is None:
        return False
    if not _is_carrier(orch, parent):
        return False
    from runtime.orchestrator.chain import ChainState, compute_advance_action
    chain = ChainState.deserialize(chain_snapshot)
    if report is _NO_AUTHENTICATED_REPORT:
        # TASK-5818: the child has a modern fingerprint but no acceptable
        # exact authenticated report — the leg's outcome cannot be verified.
        # Fail the carrier closed rather than complete or advance it on
        # task-wide evidence.
        expected = chain.current_expect_verdict()
        note = (
            f"carrier leg {child_task_id} outcome unverifiable: "
            "no exact authenticated report"
        )
        if expected is not None:
            note += f" (expected {expected!r})"
        _fail(orch, parent.id, note=note)
        # Feed carrier failure into the fan-out parent's barrier.
        _enqueue_parent_if_waiting(orch, parent.id)
        return True
    if report is None:
        report = orch._db.get_latest_completion_report(child_task_id)
    if report is None:
        # No report for the completed child — fail-closed only when a verdict
        # expectation exists (pre-existing semantics).
        expected = chain.current_expect_verdict()
        if expected is None:
            return False
        _fail(
            orch, parent.id,
            note=f"carrier verdict mismatch: expected {expected!r}, got None",
        )
        # Feed carrier failure into the fan-out parent's barrier.  Returning
        # True without these side effects strands both carrier and parent.
        _enqueue_parent_if_waiting(orch, parent.id)
        return True
    completed_child = orch._db.get_task(child_task_id)
    completed_agent = completed_child.assigned_agent if completed_child is not None else None
    action = compute_advance_action(
        chain=chain,
        report=report,
        completed_agent=completed_agent,
        reviewer_agents=_reviewer_agents_for(orch),
    )
    # A verdict mismatch OR a THR-175 reviewer non-approve (omitted-expectation
    # reviewer leg) must fail the whole carrier — never chain-complete it as a
    # false success.
    if action.kind == "wake" and action.reason in ("verdict_mismatch", "reviewer_non_approve"):
        _fail(orch, parent.id,
               note=f"carrier verdict mismatch: expected {action.expected!r}, got {action.actual!r}")
        # Feed carrier failure into the fan-out parent's barrier.
        _enqueue_parent_if_waiting(orch, parent.id)
        return True
    return False


def _carrier_fail_immediate(
    orch: "Orchestrator", parent: "TaskRecord",
    child_task_id: str,
) -> bool:
    """A carrier's chain leg failed: fail the whole carrier immediately
    (fail-closed at carrier).  Returns True if the carrier was failed,
    False for non-carriers."""
    if not _is_carrier(orch, parent):
        return False
    _fail(orch, parent.id,
           note=f"carrier chain leg {child_task_id} failed")
    # Feed carrier failure into the fan-out parent's barrier.
    _enqueue_parent_if_waiting(orch, parent.id)
    return True


def _carrier_complete_on_chain_complete(
    orch: "Orchestrator", parent: "TaskRecord",
) -> bool:
    """After a carrier's chain completed successfully (all legs done,
    final leg verdict matched), complete the carrier DIRECTLY — NO
    orch._run_agent session, NO manager-wake.  Then feed the carrier's
    completion into the fan-out parent's barrier EXACTLY ONCE.

    Returns True if the carrier was completed, False for non-carriers.
    """
    if not _is_carrier(orch, parent):
        return False
    # Complete the carrier directly — it has no session of its own.
    _complete(orch, parent.id,
              note="carrier chain complete")
    # Feed carrier completion into the fan-out parent's barrier.
    _enqueue_parent_if_waiting(orch, parent.id)
    return True


# THR-078: per-slice retry ceiling = 1.  When a fan-out owner re-dispatches a
# failed slice (revisit_of_task_id on the new child points to the failed
# predecessor), the orchestrator can derive retry count from existing DB
# lineage — no schema migration.
_FAILURE_ROUND_BOUND = 2  # kept as doc-only reference (protocol/05c)
_SLICE_RETRY_CEILING = 1  # per-slice retry ceiling; 2nd failure escalates


def _is_slice_retry_exhausted(
    orch: "Orchestrator", child: "TaskRecord", parent: "TaskRecord",
) -> bool:
    """Return True if ``child`` is a retry of a previously-FAILED slice
    under the same ``parent``, meaning the per-slice ceiling (_SLICE_RETRY_CEILING)
    of 1 has been exhausted.

    Ceiling=1 means: exactly ONE retry is allowed AFTER a slice's FIRST
    FAILURE; the SAME slice's SECOND failure escalates.  A retry of a
    previously COMPLETED (successful) slice must NOT escalate on its first
    failure — the ceiling only fires after a predecessor FAILED.

    Derivation: follow the child's ``revisit_of_task_id`` chain.  A FAILED
    ancestor under the same parent counts toward the ceiling, but a COMPLETED
    or SUPERSEDED ancestor retires earlier failures in that lineage (THR-183),
    so the scan stops at the nearest COMPLETED/SUPERSEDED ancestor.  This uses
    ONLY the existing ``revisit_of_task_id`` column on TaskRecord (no schema
    migration).
    """
    if child.revisit_of_task_id is None:
        return False
    db = orch._db
    # Walk the revisit chain from this child.
    # walk_revisit_chain returns [child, predecessor, ..., original].
    from runtime.infrastructure.database import LineageTooDeep
    try:
        chain = db.walk_revisit_chain(child.id, max_hops=200, truncate=True)
    except LineageTooDeep:
        chain = []
    # Skip the first entry (the child itself); check each ancestor for
    # same-parent membership.  A FAILED predecessor counts toward the ceiling;
    # a COMPLETED/SUPERSEDED predecessor resets the lineage (retires earlier
    # failures); anything else is ignored.
    for ancestor in chain[1:]:
        if ancestor.parent_task_id != parent.id:
            continue
        if ancestor.status in (TaskStatus.COMPLETED, TaskStatus.SUPERSEDED):
            return False
        if ancestor.status == TaskStatus.FAILED:
            return True
    return False


def _current_unresolved_failed_leaves(
    orch: "Orchestrator",
    failed_siblings: list["TaskRecord"],
    parent: "TaskRecord",
) -> list["TaskRecord"]:
    """Return the current unresolved FAILED leaf of each logical retry lineage.

    A FAILED child that has a later COMPLETED or SUPERSEDED descendant in the
    same ``revisit_of_task_id`` lineage is retired and must not contribute to
    ceiling evaluation.  A FAILED child with a later FAILED descendant is not
    the leaf — the descendant is.  Only terminal FAILED leaves of non-retired
    lineages are returned.
    """
    # Map every sibling by id so we can follow revisit links forward.
    all_siblings = [
        orch._db.get_task(cid) for cid in orch._db.get_children(parent.id)
    ]
    sibling_by_id = {s.id: s for s in all_siblings if s is not None}

    predecessor_to_successors: dict[str, list["TaskRecord"]] = {}
    for s in sibling_by_id.values():
        pred = s.revisit_of_task_id
        if pred is not None and pred in sibling_by_id:
            predecessor_to_successors.setdefault(pred, []).append(s)

    leaves: list["TaskRecord"] = []
    seen: set[str] = set()

    def _is_retired(task_id: str, visited: set[str]) -> bool:
        """True if any descendant in the same lineage is COMPLETED/SUPERSEDED."""
        for succ in predecessor_to_successors.get(task_id, []):
            if succ.id in visited:
                continue
            visited.add(succ.id)
            if succ.status in (TaskStatus.COMPLETED, TaskStatus.SUPERSEDED):
                return True
            if _is_retired(succ.id, visited):
                return True
        return False

    def _collect_leaf(task_id: str, visited: set[str]) -> "TaskRecord" | None:
        """Return the terminal FAILED leaf reachable forward, if any."""
        successors = predecessor_to_successors.get(task_id, [])
        if not successors:
            task = sibling_by_id.get(task_id)
            if task is not None and task.status == TaskStatus.FAILED:
                return task
            return None
        for succ in successors:
            if succ.id in visited:
                continue
            visited.add(succ.id)
            leaf = _collect_leaf(succ.id, visited)
            if leaf is not None:
                return leaf
        return None

    for failed in failed_siblings:
        if failed.id in seen:
            continue
        if _is_retired(failed.id, {failed.id}):
            continue
        leaf = _collect_leaf(failed.id, {failed.id})
        if leaf is not None and leaf.id not in seen:
            seen.add(leaf.id)
            leaves.append(leaf)
    return leaves


def _format_slice_retry_exhausted_reason(
    orch: "Orchestrator", leaf: "TaskRecord",
) -> str:
    """Build an escalation reason that names the causal terminal event."""
    results = orch._db.get_task_results(leaf.id)
    latest = results[-1] if results else {}
    verdict = latest.get("verdict") or "n/a"
    return (
        f"per-slice retry ceiling ({_SLICE_RETRY_CEILING}) exhausted: "
        f"causal terminal event {leaf.id} status={leaf.status.value} "
        f"verdict={verdict}: {leaf.note or '(no note)'}"
    )


# THR-211 (TASK-5818 fix-forward): ``_child_landed_terminal_report`` returns
# None for BOTH a genuinely legacy child (no agent/session fingerprint) and a
# modern child whose exact authenticated report is missing or unacceptable
# (exact-row miss, or an exact row whose status is not ``completed`` — blocked,
# unknown, nonterminal).  Chain advancement may fall back to the task-wide
# newest-row read ONLY for the former; for the latter it must fail closed and
# never consult task-wide evidence (a wrong-agent/wrong-session row must never
# gate the chain).  This sentinel marks the modern-but-unverifiable case.
class _NoAuthenticatedReport:
    """Sentinel: the child carries a modern ``(assigned_agent,
    current_session_id)`` fingerprint but has no acceptable exact authenticated
    completion report.  Chain advancement MUST fail closed on this value — it
    must never fall back to the task-wide newest-row read."""

    __slots__ = ()

    def __repr__(self) -> str:  # debugging aid
        return "_NO_AUTHENTICATED_REPORT"


_NO_AUTHENTICATED_REPORT = _NoAuthenticatedReport()


def _child_has_modern_fingerprint(child: "TaskRecord") -> bool:
    """True when ``child`` carries the modern ``(assigned_agent,
    current_session_id)`` fingerprint the historical completion contract
    requires.  Legacy rows (pre-THR-211) may lack either field."""
    return (
        child is not None
        and bool(child.assigned_agent)
        and bool(child.current_session_id)
    )


def _child_landed_terminal_report(orch: "Orchestrator", child: "TaskRecord"):
    """THR-211: return the exact authenticated CompletionReport for the
    child's CURRENT session, or None when it is not dispatch-terminal.

    Authority is deliberately narrow and session-safe: the exact
    ``(task_id, assigned_agent, current_session_id)`` triple — the same
    fingerprint the daemon boot sweep and zombie reaper use — with report
    status == ``completed``.  Fails closed on absent agent/session, no row,
    blocked reports (the blocked_on_job park is a live state owned by the
    resume flow), or any other status.  Never infers terminality from prose.

    The returned report is the one the chain/gate consumers must use: a newer
    unrelated (wrong-agent/wrong-session) row can never substitute for it.

    NOTE (TASK-5818): a None return is ambiguous — it covers both a genuinely
    legacy child with no ``(assigned_agent, current_session_id)`` fingerprint
    and a modern child whose exact report is missing/unacceptable.  Callers
    that decide between the legacy newest-row fallback and fail-closed
    behavior MUST distinguish via ``_child_has_modern_fingerprint``; only a
    genuine fingerprint absence may ever consult task-wide evidence for chain
    advancement.
    """
    if child is None or not child.assigned_agent or not child.current_session_id:
        return None
    report = orch._db.get_latest_completion_report(
        child.id, child.assigned_agent, child.current_session_id,
    )
    if report is None or report.status != "completed":
        return None
    return report


def _child_has_landed_terminal_result(orch: "Orchestrator", child: "TaskRecord") -> bool:
    """THR-211: True when the child's CURRENT session has durably landed a
    structured terminal completion report even though the task row still reads
    ``in_progress`` (the status transition is deferred to executor/session
    finalization by ``_consume_completion_report``).

    See ``_child_landed_terminal_report`` for the authority predicate.
    """
    return _child_landed_terminal_report(orch, child) is not None


def _enqueue_parent_if_waiting(
    orch: "Orchestrator",
    task_id: str,
    *,
    root_auto_revisit_spawned: bool = False,
) -> None:
    """Idempotent: advance the parent only if it's actually waiting on THIS
    lineage (in_progress+delegated) AND all its children are now terminal.

    Per-slice revisit-lineage rule (THR-078, TASK-573 bounded failure-recovery):
      - every subtask COMPLETED → enqueue parent for its next manager
        decision step (unchanged happy path).
      - a subtask FAILED, and no other failed child in this delegation
        slot has exhausted its retry ceiling → clear any active chain,
        enqueue the parent for a bounded manager-wake decision step. The
        parent receives the failed subtask's reason so it can author an
        updated brief.
      - a subtask FAILED and its per-slice retry ceiling is exhausted
        (this slice was already retried once — its ``revisit_of_task_id``
        ancestor is a FAILED child of this same parent) → escalate a root
        parent to ``escalated`` via ``try_escalate``, using the causal
        terminal event: the current unresolved FAILED leaf of the logical
        retry lineage, naming its task id, terminal status, verdict, and
        note; or, for a non-root parent, fail it and recurse upward
        (THR-033 root-only escalation). The parent does NOT
        cascade-fail — the founder or upstream manager resolves the
        termination per existing routes. The ceiling is
        ``_SLICE_RETRY_CEILING = 1`` (exactly one retry after a slice's
        first failure), evaluated per-slice via ``_is_slice_retry_exhausted``
        from the failing child's ``revisit_of_task_id`` lineage (no schema
        migration).

    ``root_auto_revisit_spawned`` is a retained compatibility/bookkeeping
    input. All current production callers (opaque-failure branches and
    startup sweep) pass ``False`` — no daemon auto-successor exists
    (TASK-3604). The boolean preserves call-site symmetry for the bounded
    parent wake / per-slice escalation contract; it does not signal that a
    root has been auto-revisited. See the retired spec
    2026-05-25-session-timeout-auto-route-design.md §6 for historical context.

    Chain handling: a FAILED chain leg clears the active chain and falls
    through to the bounded-wake logic below. A COMPLETED chain leg tries
    to auto-advance as before; on mismatch it clears the chain and wakes
    the parent.
    """
    task = orch._db.get_task(task_id)
    if task is None or task.parent_task_id is None:
        return
    parent = orch._db.get_task(task.parent_task_id)
    # Path B: a delegating parent is in_progress(delegated). The non-cascading
    # failure-recovery contract (TASK-573/THR-028) below is otherwise unchanged.
    if parent is None or parent.status not in _PARKED_CARRIER_STATUSES:
        return
    if parent.block_kind != BlockKind.DELEGATED:
        return

    # Chain-advance branch: if the parent has an active chain and the just-
    # terminated subtask completed cleanly, try to auto-advance to the next
    # leg instead of waking the parent. FAILED subtasks clear the chain and
    # fall through to the bounded-wake logic below.
    #
    # Phase 2 carrier fail-closed: when the parent is a carrier (its own
    # parent has active_fanout), a verdict-mismatch or a failed leg fails
    # the whole carrier immediately — no partial-chain completion.
    child = orch._db.get_task(task_id)
    if child is not None and parent.active_chain is not None:
        # THR-211: a child whose current session has durably landed a
        # structured terminal report is dispatch-terminal even while its row
        # reads in_progress (status transition deferred to session
        # finalization).  The newest-child guard keeps the recognition
        # at-most-once: after the chain advances, the next leg child is the
        # parent's newest child, so a late re-trigger from the already-
        # recognized child (e.g. its delayed session-finalization
        # consumption) can neither re-enter the advance path nor prematurely
        # clear the chain.
        is_chain_trigger = (
            child.status == TaskStatus.COMPLETED
            or _child_has_landed_terminal_result(orch, child)
        )
        if is_chain_trigger:
            children_ids = orch._db.get_children(parent.id)
            if children_ids and child.id == children_ids[-1]:
                # THR-211: carry the EXACT authenticated report (the
                # (task_id, assigned_agent, current_session_id) fingerprint
                # scoped row) through the chain gate so a newer unrelated
                # row can never advance or clear the chain.
                authenticated_report = _child_landed_terminal_report(orch, child)
                # TASK-5818: None is ambiguous.  A GENUINELY legacy child
                # (no agent/session fingerprint) may still use the unscoped
                # newest-row fallback inside _advance_chain_for_completed_child;
                # a MODERN child whose exact report is missing or unacceptable
                # (exact-row miss, wrong agent/session, malformed/unknown/
                # blocked/nonterminal) must fail closed and must NEVER consult
                # task-wide evidence for chain advancement.
                if (
                    authenticated_report is None
                    and _child_has_modern_fingerprint(child)
                ):
                    authenticated_report = _NO_AUTHENTICATED_REPORT
                # Snapshot the chain BEFORE advance (which may clear it).
                chain_snapshot = parent.active_chain
                outcome = _advance_chain_for_completed_child(
                    orch=orch, parent_task_id=parent.id, child_task_id=task_id,
                    report=authenticated_report,
                )
                if outcome == "advance":
                    return  # next leg spawned; parent stays in_progress(delegated)
                # outcome == "wake" → chain cleared; carrier fail-closed?
                if _carrier_fail_on_verdict_mismatch(
                    orch, parent, task_id, chain_snapshot,
                    report=authenticated_report,
                ):
                    return  # carrier failed; outer _enqueue_parent_if_waiting skipped
                # Chain complete + verdict matched → carrier auto-completes
                # directly (no _run_agent session, no manager-wake).
                if _carrier_complete_on_chain_complete(orch, parent):
                    return  # carrier completed; outer _enqueue_parent_if_waiting skipped
            # Stale re-trigger for a leg the chain already advanced past, or a
            # non-newest child: fall through to sibling-check + parent-wake.
        else:
            # FAILED chain leg: do NOT clear active_chain here. A recovery or
            # startup-style caller may invoke this on a historical FAILED
            # ancestor whose retry lineage was later retired by a COMPLETED or
            # SUPERSEDED descendant. Clearing the chain before the retired-
            # lineage check would mutate parent state for no reason. The chain
            # is cleared only after the sibling evaluation below proves there
            # is a genuine unresolved failure (or a carrier fail-closed case).
            # Carrier fail-closed is applied at that point, not here.
            pass

    siblings = [orch._db.get_task(cid) for cid in orch._db.get_children(parent.id)]
    if any(
        s is None or (
            s.status not in TERMINAL_STATES
            and not _child_has_landed_terminal_result(orch, s)
        )
        for s in siblings
    ):
        return

    failed = [s for s in siblings if s.status == TaskStatus.FAILED]
    if failed:
        # THR-078: per-slice retry ceiling (replaces old count-based
        # _FAILURE_ROUND_BOUND).  THR-183: ceiling evaluation MUST use the
        # current unresolved FAILED leaf of each logical retry lineage, not
        # every historical FAILED sibling.  A later COMPLETED/SUPERSEDED
        # descendant retires earlier failures in the same lineage, so a normal
        # parent wake initiated by a completed child cannot select a stale
        # failed sibling.

        unresolved_leaves = _current_unresolved_failed_leaves(orch, failed, parent)
        if unresolved_leaves:
            # Genuine unresolved failure: clear active_chain now. For a carrier
            # (its own parent has active_fanout), fail the whole carrier
            # immediately — no partial-chain completion.
            if parent.active_chain is not None:
                orch._db.update_task_active_chain(parent.id, None)
            if _is_carrier(orch, parent):
                _carrier_fail_immediate(orch, parent, task_id)
                return  # carrier failure feeds the fan-out parent's barrier

            # Per-slice ceiling check: escalate if any unresolved leaf has
            # exhausted its retry ceiling.
            for leaf in unresolved_leaves:
                if _is_slice_retry_exhausted(orch, leaf, parent):
                    reason = _format_slice_retry_exhausted_reason(orch, leaf)
                    if is_root(parent):
                        if orch._db.try_escalate_runtime(
                            parent.id,
                            reason=reason,
                            agent="orchestrator",
                            reason_code="runtime_retry_ceiling",
                            clear_active_fanout=parent.active_fanout is not None,
                        ):
                            _maybe_post_thread_escalation(
                                orch, parent.id, reason=reason,
                            )
                    else:
                        # THR-033 Change A lock-in: a non-root parent never
                        # escalates directly.  Fail it and recurse upward.
                        _fail(orch, parent.id, note=reason)
                        _enqueue_parent_if_waiting(orch, parent.id)
                    return

            # No per-slice ceiling hit: enqueue parent for a fresh manager
            # decision step.  Do NOT cascade-fail.
            # NOTE: active_fanout is NOT cleared here — the CAS-winner needs
            # it to inject structured join context (child verdict, confidence,
            # output_dir, failure note) via _inject_fanout_join_context.  The
            # CAS-winner clears active_fanout after injecting join context.
            queue = getattr(orch, "_queue", None)
            if queue is not None:
                queue.put_nowait(orch._slug, parent.id)
            return

        # All failures are retired (lineage has a later COMPLETED/SUPERSEDED
        # descendant). Treat this as a normal bounded parent wake: queue the
        # parent once, leaving active_chain, active_fanout, status, block_kind,
        # and note completely untouched. No escalation, no audit side effect.
        queue = getattr(orch, "_queue", None)
        if queue is not None:
            queue.put_nowait(orch._slug, parent.id)
        return

    queue = getattr(orch, "_queue", None)
    if queue is not None:
        queue.put_nowait(orch._slug, parent.id)


# ---------------------------------------------------------------------------
# Legacy auto-revisit audit readers (TASK-3604: auto-revisit creation removed;
# these readers are preserved for historical compatibility — existing
# auto_revisit_of audit rows must remain readable).
# ---------------------------------------------------------------------------
def _maybe_resume_blocked_task(
    orch: "Orchestrator",
    task_id: str,
    *,
    trigger: str,
    triggering_job_id: str | None,
) -> bool:
    """Check predicate (all blocking jobs terminal) and enqueue if satisfied.

    READ-ONLY: does NOT mutate task state. The state transition happens at
    run_step_impl step 3's CAS when the worker picks up the enqueued task.

    Returns True if it enqueued; False otherwise. Idempotent — extra enqueues
    are harmless (run_step_impl's CAS admits exactly one).

    Spec: docs/superpowers/specs/2026-05-28-task-blocked-by-job-design.md §5.4
    """
    import json as _json

    db = orch._db
    audit = orch._audit
    task = db.get_task(task_id)
    if task is None:
        return False
    # Path B: a task parked on jobs is in_progress(blocked_on_job).
    if task.status not in _PARKED_CARRIER_STATUSES or task.block_kind != BlockKind.BLOCKED_ON_JOB:
        return False  # silent — steady state

    try:
        job_ids = _json.loads(task.blocked_on_job_ids or "[]")
    except _json.JSONDecodeError:
        audit.log_task_resume_skipped(
            task_id=task_id, reason="empty_job_list",
            blocked_on_job_ids_raw=task.blocked_on_job_ids,
        )
        return False
    if not job_ids:
        audit.log_task_resume_skipped(
            task_id=task_id, reason="empty_job_list",
            blocked_on_job_ids_raw=task.blocked_on_job_ids,
        )
        return False

    _TERMINAL = {"completed", "failed", "rejected"}
    for jid in job_ids:
        if db.get_job_status(jid) not in _TERMINAL:
            return False  # silent — common steady state

    # All terminal — enqueue.
    queue = getattr(orch, "_queue", None)
    if queue is not None:
        queue.enqueue(
            orch._slug, task_id,
            metadata={"trigger": trigger, "triggering_job_id": triggering_job_id},
        )
    return True


def _append_followup_system_and_reinvoke(
    orch: "Orchestrator",
    *,
    thread_id: str,
    dispatcher: str,
    original_id: str,
    source_task_id: str,
    system_payload: dict,
    reinvoke: bool = True,
) -> None:
    """Append a SYSTEM message + optionally mint/enqueue a TASK_FOLLOWUP re-invocation.

    Shared tail for `_maybe_post_thread_followup` (terminal) and
    `_maybe_post_thread_escalation`. Race-aware: the atomic cap-projection +
    conditional bump + mint is serialized by the RLock on
    `mint_followup_invocation_with_cap_extend`. `original_id` is the original
    dispatched task id (for audit keying); `source_task_id` is the task that
    triggered this followup (terminal task or escalated task).

    When `reinvoke=False`, only the SYSTEM message is appended; the
    dispatcher re-invocation (cap-extend + mint + enqueue) is suppressed.
    Historically this was used for intermediate auto-revisit failures
    (THR-046 msg99, retired TASK-3604): the thread surface needed the
    'revisiting as <SUCCESSOR>' system message while the successor fired its
    own followup at its terminal. The parameter remains as a retained
    bookkeeping seam — all production callers now pass `auto_revisit_spawned=False`
    (and thus `reinvoke=True`), but the suppressed path is preserved for
    compatibility with the `auto_revisit_spawned` contract in
    `_maybe_post_thread_followup`.
    """
    db = orch._db
    audit = orch._audit

    # Append system message (separate from the atomic cap+mint below — the
    # system message ordering relative to concurrent system messages is not
    # part of the atomicity invariant we're protecting).
    from runtime.models import ThreadMessageKind as _TMK
    sys_seq = db.append_thread_message(
        thread_id=thread_id, speaker=dispatcher,
        kind=_TMK.SYSTEM,
        system_payload=system_payload,
    )

    if not reinvoke:
        # Historical legacy stored-payload compatibility path.
        # TASK-3604 removed daemon auto-revisit creation; all current
        # production callers pass ``auto_revisit_spawned=False`` (and thus
        # ``reinvoke=True``). No current code path populates
        # ``revisit_task_id`` or enters this branch.  The branch, audit
        # log, and conditional are retained for compatibility with
        # ``_maybe_post_thread_followup``'s ``auto_revisit_spawned``
        # contract so that any pre-TASK-3604 stored system payload whose
        # stored ``reinvoke`` field resolved to ``False`` is still handled
        # correctly.
        audit.log_thread_followup_skipped(
            thread_id, original_task_id=original_id, terminal_task_id=source_task_id,
            reason="auto_revisit_spawned",
            successor_task_id=system_payload.get("revisit_task_id"),
        )
        return

    # Atomic cap-projection + conditional bump + mint.  Closes the TOCTOU race
    # where two concurrent root completions on the same thread both read the
    # same pending count, both skip the bump, both mint, and leave the thread
    # with more obligations than turn_cap.  The @_synchronized RLock on
    # mint_followup_invocation_with_cap_extend serializes all three steps.
    inv, new_cap = db.mint_followup_invocation_with_cap_extend(
        thread_id=thread_id,
        agent_name=dispatcher,
        triggering_seq=sys_seq,
    )
    if new_cap is not None:
        audit.log_thread_turn_cap_auto_extended(
            thread_id, original_task_id=original_id,
            reason="task_followup", new_cap=new_cap,
        )
    audit.log_thread_task_followup_enqueued(
        thread_id, original_task_id=original_id, terminal_task_id=source_task_id,
        dispatcher=dispatcher, invocation_token=inv.invocation_token,
    )

    # Enqueue onto the org's thread queue. The queue is bound to the daemon's
    # main event loop, but run_step runs on a worker thread, so we cross the
    # loop boundary via run_coroutine_threadsafe — same pattern as
    # the daemon uses for cross-thread async bridging.
    import asyncio as _asyncio
    from runtime.daemon.thread_queue import ThreadJob as _ThreadJob
    thread_queue = getattr(orch, "_thread_queue", None)
    main_loop = getattr(orch, "_main_loop", None)
    if thread_queue is not None and main_loop is not None:
        try:
            _asyncio.run_coroutine_threadsafe(
                thread_queue.put(_ThreadJob(
                    org_slug=orch._slug,
                    invocation_token=inv.invocation_token,
                )),
                main_loop,
            )
        except Exception as exc:
            audit.log_thread_followup_skipped(
                thread_id, original_task_id=original_id, terminal_task_id=source_task_id,
                reason="enqueue_failed", detail=str(exc),
            )
    else:
        # Defence: queue or loop not yet wired (e.g., test orchestrator constructed
        # without daemon context). Invocation stays PENDING; audit so the
        # operator can detect it if needed. In production this path is never
        # taken because _lifespan always calls _attach_thread_queue_wiring before
        # the first task step runs.
        audit.log_thread_followup_skipped(
            thread_id, original_task_id=original_id, terminal_task_id=source_task_id,
            reason="enqueue_unavailable",
        )


def _maybe_post_thread_escalation(
    orch: "Orchestrator",
    task_id: str,
    *,
    reason: str,
) -> None:
    """Post a `task_escalated` SYSTEM message + re-invoke the dispatcher when a
    thread-dispatched task escalates to the founder.

    Unlike `_maybe_post_thread_followup` (terminal-only, root-only because
    terminals cascade up to the parent), escalations do NOT cascade
    (run_step escalate branch: "parent stays in_progress(delegated)") and a team
    manager can escalate at any depth. So we walk ancestors to the chain root,
    then the revisit chain, to find the originating thread.

    Spec: docs/superpowers/specs/2026-06-06-thread-escalation-surfacing-design.md
    """
    db = orch._db
    audit = orch._audit

    task = db.get_task(task_id)
    if task is None:
        return
    # Re-read persisted state: the founder may have resolved/cancelled the
    # escalation in the window between try_escalate and this call.
    if task.status != TaskStatus.ESCALATED:
        return

    # Resolve the originating thread. Escalation can fire on a child, so walk
    # ancestors to the chain root first, then the revisit chain (only the
    # dispatched root carries dispatched_from_thread_id).
    from runtime.infrastructure.database import LineageTooDeep  # local: avoid cycle
    try:
        ancestors = db.walk_ancestors(task_id, max_hops=200)
    except LineageTooDeep:
        audit.log_thread_followup_skipped(
            "(unresolved)", original_task_id=task_id, terminal_task_id=task_id,
            reason="chain_too_deep",
        )
        return
    root = ancestors[-1] if ancestors else task
    chain = db.walk_revisit_chain(root.id, max_hops=200, truncate=True)
    original = chain[-1] if chain else root
    thread_id = original.dispatched_from_thread_id
    if thread_id is None:
        # Not a thread-dispatched chain; silent no-op.
        return

    # Thread-state guard.
    thread = db.get_thread(thread_id)
    from runtime.models import ThreadStatus as _ThreadStatus
    if thread is None or thread.status is not _ThreadStatus.OPEN:
        audit.log_thread_followup_skipped(
            thread_id, original_task_id=original.id, terminal_task_id=task_id,
            reason="thread_not_open",
            thread_status=(thread.status.value if thread else "missing"),
            task_status="escalated",
        )
        return

    # Dispatcher identity from the thread_dispatch audit row on the original.
    dispatch_rows = [
        r for r in db.get_audit_logs(thread_id)
        if r["action"] == "thread_dispatch"
        and _payload_dict(r).get("task_id") == original.id
    ]
    if not dispatch_rows:
        audit.log_thread_followup_skipped(
            thread_id, original_task_id=original.id, terminal_task_id=task_id,
            reason="dispatcher_unresolved",
        )
        return
    dispatcher = _payload_dict(dispatch_rows[0])["dispatcher"]

    # `original_task_id` is the revisit-chain origin (thread-dispatch keying);
    # `root_task_id` is the ancestor root of the escalating task. These are
    # equal for a root escalation but differ when a child escalates inside a
    # revisited chain — both are emitted for the dispatcher's downstream use.
    # The result was persisted by the ordinary completion callback before this
    # escalation path ran. Snapshot that exact durable row into the causal
    # message; the continuation route re-reads and compares it, so the caller
    # cannot substitute later repair descendants or prose as authority.
    results = db.get_task_results(task_id)
    latest_result = results[-1] if results else None
    causal_terminal_result = None
    if latest_result is not None:
        causal_terminal_result = {
            "task_id": task_id,
            "result_id": latest_result["id"],
            "terminal_status": latest_result.get("status"),
            "verdict": latest_result.get("verdict"),
            "output_summary": latest_result.get("output_summary"),
            "created_at": latest_result.get("created_at"),
        }
    escalation_rows = [
        row for row in db.get_audit_logs(task_id) if row["action"] == "escalation"
    ]
    causal_escalation_audit_id = escalation_rows[-1]["id"] if escalation_rows else None
    system_payload = {
        "kind_tag": "task_escalated",
        "task_id": task_id,
        "original_task_id": original.id,
        "root_task_id": root.id,
        "status": "escalated",
        "reason": reason,
        "revisit_chain_length": len(chain) if chain else 1,
    }
    if causal_terminal_result is not None:
        system_payload["causal_terminal_result"] = causal_terminal_result
    if causal_escalation_audit_id is not None:
        system_payload["causal_escalation_audit_id"] = causal_escalation_audit_id
    _append_followup_system_and_reinvoke(
        orch,
        thread_id=thread_id,
        dispatcher=dispatcher,
        original_id=original.id,
        source_task_id=task_id,
        system_payload=system_payload,
    )


def _maybe_post_thread_followup(
    orch: "Orchestrator",
    task_id: str,
    *,
    status: TaskStatus,
    auto_revisit_spawned: bool,
    revisit_task_id: str | None = None,
) -> None:
    """Post a task-followup system message + mint a re-invocation for the dispatcher.

    Fire predicate (spec §4, §4.1):
      - status == COMPLETED                                → always fire
      - status == SUPERSEDED                      → always fire (terminal,
                                                             completion-class — a
                                                             thread-originated task
                                                             auto-resolved by a
                                                             continuation; THR-018 §3a)
      - status == FAILED                                   → true terminal, fire
                                                             (TASK-3604: daemon
                                                             auto-revisit removed;
                                                             all current production
                                                             callers pass
                                                             ``auto_revisit_spawned=False``)

    Only root tasks fire. Non-root child terminals wake the parent via
    bounded recovery (``_enqueue_parent_if_waiting``) — the parent may
    produce a followup at its own terminal, not via cascade. The originating
    thread is found by walking ``walk_revisit_chain`` backward to the
    earliest predecessor and reading ``dispatched_from_thread_id`` off that
    row.

    ``auto_revisit_spawned`` is a retained compatibility parameter.
    Since TASK-3604 removed daemon auto-revisit creation, all current
    production callers pass ``False``. The parameter and its conditional
    ``reinvoke`` logic are preserved for compatibility with the
    ``_append_followup_system_and_reinvoke`` contract; the
    ``revisit_task_id`` path is historical and never populated by current
    code paths.

    Spec: docs/superpowers/specs/2026-05-28-thread-task-followup-design.md §4-§6
    """
    # Predicate gate — first pass using caller's claim (cheap early-out).
    # CANCELLED is a terminal followup status (Path B, THR-037): a founder cancel
    # writes the stored terminal CANCELLED and replays in the task_failed class.
    # TASK-3604: daemon auto-revisit removed — all current production
    # callers pass auto_revisit_spawned=False, so the system-message-only
    # path with revisit_task_id is historical. The parameter and conditional
    # are retained for compatibility with _append_followup_system_and_reinvoke.
    if status not in (
        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SUPERSEDED,
        TaskStatus.CANCELLED,
    ):
        return

    db = orch._db
    audit = orch._audit
    terminal_task = db.get_task(task_id)
    if terminal_task is None:
        return

    # Re-read the persisted status. Site D's caller passes COMPLETED, but
    # /cancel may have raced past Guard B and flipped the row to a terminal
    # state; _complete() short-circuits in that race. Under Path B (THR-037) a
    # founder cancel now writes the stored terminal CANCELLED (was
    # failed+cancelled_at), and both cancel callers still pass the legacy FAILED
    # hint — so CANCELLED must be honored here or a cancelled root is silently
    # dropped (no task_failed system message, no TASK_FOLLOWUP). Trust the DB,
    # not the caller's claim.
    actual_status = terminal_task.status
    if actual_status not in (
        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SUPERSEDED,
        TaskStatus.CANCELLED,
    ):
        # Row isn't terminal yet — caller raced ahead of the DB write.
        # Bail; the eventual real terminal will re-enter this helper.
        return

    # Only root tasks fire. Children are handled at their parent's terminal site.
    if terminal_task.parent_task_id is not None:
        return

    # Find the original dispatched root via the revisit chain.
    # walk_revisit_chain returns [task, predecessor, ..., original].
    # Use a hop bound large enough to cover any realistic revisit chain
    # (200 hops, matching the bound in _is_slice_retry_exhausted), and
    # handle LineageTooDeep defensively rather than crashing and silently
    # discarding the followup without any audit trail.
    from runtime.infrastructure.database import LineageTooDeep  # local: avoid cycle
    try:
        chain = db.walk_revisit_chain(task_id, max_hops=200)
    except LineageTooDeep:
        audit.log_thread_followup_skipped(
            "(unresolved)", original_task_id=task_id, terminal_task_id=task_id,
            reason="chain_too_deep",
        )
        return
    original = chain[-1] if chain else terminal_task
    thread_id = original.dispatched_from_thread_id
    if thread_id is None:
        # Not a thread-dispatched chain; silent no-op (no audit).
        return

    # Thread-state guard.
    thread = db.get_thread(thread_id)
    if thread is None:
        audit.log_thread_followup_skipped(
            thread_id, original_task_id=original.id, terminal_task_id=task_id,
            reason="thread_not_open", thread_status="missing",
            task_status=status.value,
        )
        return
    from runtime.models import ThreadStatus as _ThreadStatus
    if thread.status is not _ThreadStatus.OPEN:
        audit.log_thread_followup_skipped(
            thread_id, original_task_id=original.id, terminal_task_id=task_id,
            reason="thread_not_open",
            thread_status=thread.status.value,
            task_status=status.value,
        )
        return

    # Dispatcher identity: read from the thread_dispatch audit row on the
    # ORIGINAL task (revisit roots don't have their own dispatch row).
    dispatch_rows = [
        r for r in db.get_audit_logs(thread_id)
        if r["action"] == "thread_dispatch"
        and _payload_dict(r).get("task_id") == original.id
    ]
    if not dispatch_rows:
        audit.log_thread_followup_skipped(
            thread_id, original_task_id=original.id, terminal_task_id=task_id,
            reason="dispatcher_unresolved",
        )
        return
    dispatcher = _payload_dict(dispatch_rows[0])["dispatcher"]

    # Build system payload using DB-actual status (not the caller's claim) so
    # a cancel race at Site D doesn't emit task_completed for a FAILED row.
    # Completion-class terminals (COMPLETED, SUPERSEDED) → task_completed;
    # failure-class terminals (FAILED, and the Path B stored CANCELLED) → task_failed.
    # The payload's status field carries the precise label (status='cancelled' +
    # cancelled=true for a CANCELLED row), mirroring org_state._TERMINAL_STATUS_TO_EVENT.
    kind_tag = (
        "task_failed"
        if actual_status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
        else "task_completed"
    )
    system_payload = {
        "kind_tag": kind_tag,
        "task_id": task_id,
        "original_task_id": original.id,
        "root_task_id": original.id,
        "status": actual_status.value,
        "final_output_summary": terminal_task.note or "",
        "final_output_dir": terminal_task.final_output_dir,
        "cancelled": terminal_task.cancelled_at is not None,
        "revisit_chain_length": len(chain) if chain else 1,
        "revisit_task_id": revisit_task_id,
    }

    _append_followup_system_and_reinvoke(
        orch,
        thread_id=thread_id,
        dispatcher=dispatcher,
        original_id=original.id,
        source_task_id=task_id,
        system_payload=system_payload,
        reinvoke=not auto_revisit_spawned,
    )


def _payload_dict(row: dict) -> dict:
    """Coerce an audit row's ``payload`` field to a dict."""
    import json as _json
    p = row.get("payload")
    if p is None:
        return {}
    if isinstance(p, dict):
        return p
    return _json.loads(p)


def _session_failed_note(result, report) -> str:
    """Build an enriched `agent session failed` note.

    The pre-TASK-045 version wrote a bare constant string, so when the
    Claude subprocess finished without calling `happyranch report-completion`
    there was no trace of WHY — rc, stderr, and stdout were all dropped
    on the floor. Now we surface rc and the tail of stderr (or stdout,
    if stderr is empty) so the next class-of-TASK-045 failure is
    self-diagnosing from the audit trail alone.
    """
    bits: list[str] = []
    rc = getattr(result, "returncode", None)
    bits.append(f"rc={rc}" if rc is not None else "rc=?")
    err = (getattr(result, "stderr_tail", "") or "").strip()
    out = (getattr(result, "stdout_tail", "") or "").strip()
    preview_src, label = (err, "stderr") if err else (out, "stdout") if out else ("", "")
    if label:
        preview = preview_src.replace("\n", " ")[-300:]
        bits.append(f"{label}: {preview}")
    error_str = getattr(result, "error", None)
    if error_str:
        bits.append(error_str)
    if report is None and getattr(result, "success", False):
        bits.append("no completion callback")
    return f"agent session failed ({'; '.join(bits)})"


# --- Fan-out spawn + join context helpers ---


def _spawn_fanout_children(
    orch: "Orchestrator",
    parent: "TaskRecord",
    task_id: str,
    next_count: int,
    *,
    children: list[dict],
    width: int,
    manager_agent: str,
    join_summary: str | None = None,
    step_audit_id: int | None = None,
) -> None:
    """Allocate child IDs, build TaskRecords, atomically insert all N children
    and park the parent in in_progress(delegated) with active_fanout set.
    Shared by the fresh dispatch path and the review-gate re-entry path.

    Phase 2 pipeline: a child with non-empty ``then`` or ``expect_verdict`` is a
    *carrier* — its inline chain is materialized (active_chain set) and its first
    leg is spawned as a subtask of the carrier.  The carrier itself parks as
    delegated; it does NOT run an agent session.  Plain children (empty ``then``,
    no ``expect_verdict``) are dispatched as bare PENDING subtasks, unchanged.

    On cancel-race (try_delegate_many returns False), logs and returns
    silently — the parent was cancelled between validation and spawn.
    """
    from runtime.models import TaskRecord
    from runtime.orchestrator.fanout import FanoutState

    db = orch._db

    # Allocate sequential child IDs.
    child_records: list[TaskRecord] = []
    children_ids: list[str] = []
    base_id = db.next_task_id()
    base_num = int(base_id.split("-")[-1])
    for i, child_info in enumerate(children):
        cid = f"TASK-{base_num + i:03d}"
        children_ids.append(cid)
        has_pipeline = bool(child_info.get("then")) or child_info.get("expect_verdict") is not None
        # THR-056 option 3: mutating fan-out — children targeted at a team
        # manager are task_type='task' so their delegate-chain decisions are
        # parsed and can spawn implementation children. Pipeline carriers
        # (has_pipeline=True) never run agent sessions themselves, so their
        # task_type does not affect decision parsing; keep them as subtask.
        is_manager = (
            not has_pipeline
            and orch.teams is not None
            and bool(orch.teams.is_team_manager(child_info["agent"]))
        )
        child_task_type = "task" if is_manager else "subtask"
        child_records.append(TaskRecord(
            id=cid,
            team=parent.team,
            brief=child_info["prompt"] or "",
            assigned_agent=child_info["agent"],
            parent_task_id=task_id,
            status=TaskStatus.IN_PROGRESS if has_pipeline else TaskStatus.PENDING,
            block_kind=BlockKind.DELEGATED if has_pipeline else None,
            session_timeout_seconds=parent.session_timeout_seconds,
            task_type=child_task_type,
            revisit_of_task_id=child_info.get("revisit_of_task_id"),
        ))

    # THR-109: validate attachment refs per-child (count limit, per-child
    # duplicates) and globally (cross-sibling duplicate keys, existence,
    # claim status). Pipeline carriers own their declared refs; their first
    # legs inherit.
    #
    # Decision-wide validation:
    # 1. Per-child/per-leg: validate each child's top-level refs AND each
    #    pipeline child's nested then[].attachments (count ≤ 5 per leg,
    #    intra-leg duplicates, file exists, not claimed).
    # 2. Cross-sibling global: one shared key ledger (all_keys) catches
    #    duplicates across top-level-to-top-level, carrier-to-nested,
    #    nested-to-nested, and cross-sibling keys. A duplicate, missing,
    #    or already-claimed key anywhere rejects the entire fanout before
    #    child, parent park, active_fanout, link/audit, claim, or queue.
    children_att_params: list[list[dict] | None] = []
    has_any_attachments = any(
        child_info.get("attachments") or any(
            leg.get("attachments") for leg in (child_info.get("then") or [])
        )
        for child_info in children
    )
    if has_any_attachments:
        import json
        from runtime.infrastructure.task_attachment_store import (
            TaskAttachmentStore,
            validate_task_attachment_refs,
        )
        from runtime.models import TaskAttachmentRef
        store = TaskAttachmentStore(orch._paths.task_attachments_dir)

        # One fanout-wide key ledger for all declared refs.
        per_child_prevalidated: list[list[dict]] = []
        all_keys: set[str] = set()
        try:
            for child_info in children:
                child_refs_raw = child_info.get("attachments") or []
                refs = [TaskAttachmentRef(
                    storage_key=r["storage_key"],
                    display_name=r.get("display_name"),
                ) for r in child_refs_raw]
                # Per-child count limit + intra-child duplicates + existence
                # + claim status, AND global duplicate vs all_keys.
                child_pv = validate_task_attachment_refs(
                    store=store, db=db, refs=refs,
                    external_seen_keys=all_keys,
                )
                for pva in child_pv:
                    all_keys.add(pva["storage_key"])

                # Validate pipeline nested attachments (each then[].attachments
                # leg individually, with global duplicate detection).
                pipeline_nested_refs: list[list] = []
                for leg in child_info.get("then") or []:
                    leg_refs_raw = leg.get("attachments") or []
                    if not leg_refs_raw:
                        continue
                    leg_refs = [TaskAttachmentRef(
                        storage_key=r["storage_key"],
                        display_name=r.get("display_name"),
                    ) for r in leg_refs_raw]
                    leg_pv = validate_task_attachment_refs(
                        store=store, db=db, refs=leg_refs,
                        external_seen_keys=all_keys,
                    )
                    for pva in leg_pv:
                        all_keys.add(pva["storage_key"])

                per_child_prevalidated.append(child_pv)
        except ValueError as e:
            import json as _json
            error_detail = _json.loads(str(e))
            note = (
                f"fanout attachment validation failed: "
                f"{error_detail.get('code', 'unknown')} "
                f"({error_detail.get('storage_key', '')})"
            )
            _fail(orch, task_id, note=note)
            _enqueue_parent_if_waiting(orch, task_id)
            _maybe_post_thread_followup(
                orch, task_id,
                status=TaskStatus.FAILED, auto_revisit_spawned=False,
            )
            return

        # Build per-child params from the per-child prevalidated lists.
        for child_pv in per_child_prevalidated:
            if not child_pv:
                children_att_params.append(None)
            else:
                child_params = _prepare_attachment_params(orch, child_pv)
                children_att_params.append(child_params)
    else:
        children_att_params = [None] * len(children)

    # Build FanoutState BEFORE the atomic insert so it's ready to persist.
    fanout_state = FanoutState(
        children_ids=children_ids,
        children_details=children,
        width=width,
        manager_agent=manager_agent,
        join_summary=join_summary,
        status="spawned",
    )
    parent_note = (
        f"Fan-out to {width} children: "
        + ", ".join(
            f"{c['agent']}={cid}"
            for c, cid in zip(children, children_ids)
        )
    )

    # Build pipeline carrier chain data BEFORE the atomic transaction.
    # Carrier chains are materialized inside try_delegate_many so active_chain
    # + first leg are atomic with the fanout spawn — no partial carrier state.
    carrier_chains_data: list[dict] | None = None
    pipeline_indices: set[int] = set()
    # First leg IDs start after the last child ID. Use base_num + len(children)
    # (not another next_task_id() call — children aren't inserted yet so MAX
    # hasn't moved).
    carrier_leg_offset = 0
    for i, child_info in enumerate(children):
        has_pipeline = bool(child_info.get("then")) or child_info.get("expect_verdict") is not None
        if not has_pipeline:
            continue
        pipeline_indices.add(i)
        # Build the carrier chain and first leg data.
        from runtime.orchestrator.chain import ChainState
        from runtime.models import ChainLeg, TaskAttachmentRef
        then_legs = [
            ChainLeg(
                agent=leg["agent"], prompt=leg["prompt"],
                expect_verdict=leg.get("expect_verdict"),
                attachments=[TaskAttachmentRef(
                    storage_key=a["storage_key"],
                    display_name=a.get("display_name"),
                ) for a in leg.get("attachments", []) or []],
            )
            for leg in child_info.get("then", []) or []
        ]
        carrier_chain = ChainState(
            step_index=0,
            first_leg_expect_verdict=child_info.get("expect_verdict"),
            legs=then_legs,
            step_audit_id=step_audit_id or 0,
        )
        first_leg_id = f"TASK-{base_num + len(children) + carrier_leg_offset:03d}"
        carrier_leg_offset += 1
        first_leg_data = {
            "id": first_leg_id,
            "team": parent.team,
            "brief": child_info["prompt"] or "",
            "assigned_agent": child_info["agent"],
            "status": TaskStatus.PENDING,
            "session_timeout_seconds": parent.session_timeout_seconds,
            "task_type": "subtask",
            "revision_count": 0,
            "orchestration_step_count": 0,
        }
        if carrier_chains_data is None:
            carrier_chains_data = []
        carrier_chains_data.append({
            "child_index": i,
            "active_chain_json": carrier_chain.serialize(),
            "first_leg": first_leg_data,
            "first_leg_id": first_leg_id,
        })

    # Atomic insert-N + parent transition under a single explicit SQL
    # transaction. active_fanout, carrier active_chain, and first-leg inserts
    # are all written in the same transaction as child inserts + parent park +
    # attachment links/audit — no crash gap between spawn and metadata
    # persistence. Any failure rolls back everything.
    if not db.try_delegate_many(
        task_id, child_records, parent_note=parent_note,
        active_fanout_json=fanout_state.serialize(),
        children_attachments=children_att_params,
        carrier_chains=carrier_chains_data,
        uploaded_by=manager_agent,
    ):
        logger.debug(
            "run_step %s: cancelled between re-check and fanout spawn, dropping",
            task_id,
        )
        return

    orch._audit.log_fanout_spawned(
        task_id=task_id,
        agent=manager_agent,
        width=width,
        children_ids=children_ids,
    )

    # Enqueue: plain children go directly to the queue; pipeline carriers'
    # first legs are enqueued instead (carriers themselves never run sessions).
    for i, child_info in enumerate(children):
        cid = children_ids[i]
        has_pipeline = i in pipeline_indices
        if not has_pipeline:
            if orch._queue is not None:
                orch._queue.put_nowait(orch._slug, cid)
            continue
        # Find the carrier's first leg id from the pre-allocated data.
        for cc in (carrier_chains_data or []):
            if cc["child_index"] == i:
                if orch._queue is not None:
                    orch._queue.put_nowait(orch._slug, cc["first_leg_id"])


def _inject_fanout_join_context(
    orch: "Orchestrator", task_id: str, active_fanout_json: str,
) -> None:
    """On CAS win for a fan-out parent: collect child results, build join
    context, and write a fanout_join audit row so the manager's prompt can
    include it. Also clears active_fanout on the parent (caller must do this
    separately or here; we just write the audit row).

    The audit row written here is read by _fanout_join_header_if_applicable
    in the prompt-build step.
    """
    from runtime.orchestrator.fanout import (
        FanoutState,
        build_fanout_join_context,
        collect_child_join_info,
    )
    try:
        fanout = FanoutState.deserialize(active_fanout_json)
    except Exception:
        logger.debug("run_step %s: active_fanout unparseable, skipping join", task_id)
        return

    db = orch._db
    # Collect child results for all children in the fan-out.
    children = []
    child_reports: dict[str, dict | None] = {}
    for cid in fanout.children_ids:
        child = db.get_task(cid)
        if child is not None:
            children.append(child)
        # Fetch the latest completion report for verdict/confidence.
        report = db.get_latest_completion_report(cid)
        if report is not None:
            child_reports[cid] = {
                "verdict": report.verdict,
                "confidence_score": report.confidence,
            }
        else:
            child_reports[cid] = None

    join_infos = collect_child_join_info(children, child_reports=child_reports)
    join_context = build_fanout_join_context(
        parent_task_id=task_id,
        fanout=fanout,
        child_results=join_infos,
    )

    # Write a fanout_join audit row that the prompt builder reads.
    orch._audit.log_fanout_join(
        task_id=task_id,
        width=fanout.width,
        children_ids=fanout.children_ids,
        context_markdown=join_context,
    )


def _fanout_join_header_if_applicable(
    orch: "Orchestrator", task_id: str,
) -> str | None:
    """Return the fan-out join context header on the first manager step after
    a fan-out join, otherwise None.

    Trigger: the most recent 'fanout_join' audit entry for this task has a
    higher row id than the most recent 'orchestration_step' entry — i.e. the
    fan-out children are all terminal AND the manager hasn't run yet. Once
    the manager produces its first decision after join,
    ``log_orchestration_step`` writes a row with a higher id and this helper
    returns None on every subsequent call.
    """
    logs = orch._db.get_audit_logs(task_id)
    last_join = None
    last_step = None
    for entry in logs:
        action = entry["action"]
        if action == "fanout_join":
            last_join = entry
        elif action == "orchestration_step":
            last_step = entry
    if last_join is None:
        return None
    if last_step is not None and last_step["id"] > last_join["id"]:
        return None
    payload = last_join.get("payload") or {}
    if isinstance(payload, str):
        import json as _json
        try:
            payload = _json.loads(payload)
        except Exception:
            payload = {}
    return payload.get("context_markdown")
