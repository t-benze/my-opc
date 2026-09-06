from __future__ import annotations

from pathlib import Path
import json

import pytest

from runtime.config import Settings
from runtime.daemon.__main__ import _sweep_on_startup
from runtime.daemon.queue import TaskQueue
from runtime.infrastructure.database import Database
from runtime.models import BlockKind, TaskRecord, TaskStatus, ThreadInvocationPurpose, ThreadRecord, ThreadStatus
from runtime.orchestrator._paths import OrgPaths
from runtime.orchestrator.orchestrator import Orchestrator
from runtime.orchestrator.teams import TeamsRegistry
from runtime.runtime import RuntimeDir


def _seed_manager_recovery_result(tmp_path, *, self_evaluation="valid"):
    from runtime.orchestrator.active_authority_policy import (
        SELF_EVALUATION_CONTRACT_DIGEST,
        SELF_EVALUATION_CONTRACT_ID,
        SELF_EVALUATION_CONTRACT_VERSION,
        ActivePolicySnapshot,
        persist_session_policy_binding,
    )
    from runtime.orchestrator.authority_policy import CONTINUE_ROUTINE_PHRASE
    from runtime.orchestrator.teams import TeamManager
    from tests.authority_policy_test_factory import activate_test_policy

    db, orch, queue = _seed_org_with_orch(tmp_path)
    orch._teams._teams["engineering"] = TeamManager(
        name="engineering_manager", team="engineering", workers=("dev_agent",),
    )
    task_id = "TASK-ORPH-SELF-EVAL"
    session_id = "sess-orph-self-eval"
    manager = "engineering_manager"
    db.insert_thread(ThreadRecord(id="THR-RECOVERY", subject="recovery"))
    db.insert_task(TaskRecord(
        id=task_id, brief="x", team="engineering", assigned_agent=manager,
        status=TaskStatus.IN_PROGRESS, task_type="task",
        dispatched_from_thread_id="THR-RECOVERY",
    ))
    from runtime.infrastructure.audit_logger import AuditLogger
    AuditLogger(db).log_thread_dispatch(
        "THR-RECOVERY", task_id=task_id, dispatcher=manager,
        target_agent=manager, team="engineering",
    )
    db.update_task(
        task_id, current_session_id=session_id, orchestration_step_count=1,
    )
    release, activation = activate_test_policy(db)
    persist_session_policy_binding(
        db=db, task_id=task_id, session_id=session_id, agent_name=manager,
        snapshot=ActivePolicySnapshot(release, activation),
        provider_id="openai", executor_kind="codex", model_id="gpt-5",
    )
    evaluation = {
        "contract_id": SELF_EVALUATION_CONTRACT_ID,
        "contract_version": SELF_EVALUATION_CONTRACT_VERSION,
        "contract_digest": SELF_EVALUATION_CONTRACT_DIGEST,
        "root_task_id": task_id, "manager_session_id": session_id,
        "release_id": release.id, "policy_version": str(release.version),
        "policy_digest": release.policy_digest,
        "activation_id": activation.id, "activation_epoch": activation.epoch,
        "provider_id": "openai", "executor_kind": "codex", "model_id": "gpt-5",
        "disposition": "continue_same_root",
        "clause_id": "cont-routine-same-root", "action": "continue_same_root",
        "confidence": 1.0, "uncertainty_codes": [],
    }
    if self_evaluation == "mismatch":
        evaluation["manager_session_id"] = "sess-stale"
    decision = {"action": "escalate", "reason": CONTINUE_ROUTINE_PHRASE}
    if self_evaluation == "valid" or self_evaluation == "mismatch":
        decision["_manager_self_evaluation"] = evaluation
    elif self_evaluation == "malformed":
        decision["_manager_self_evaluation"] = {"unexpected": True}
    db.insert_task_result(
        task_id=task_id, agent=manager, session_id=session_id,
        status="completed", confidence_score=90, output_summary="escalate",
        decision_json=json.dumps(decision),
    )
    row = db.get_latest_task_result(task_id, manager, session_id)
    assert row is not None and row["task_id"] == task_id
    return db, orch, queue, task_id, row


def _assert_authority_denominator(db, task_id, *, continued):
    candidates = db.list_authority_candidates_for_root(task_id)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.causal_event_id == f"result:{db.get_latest_task_result(task_id, 'engineering_manager', 'sess-orph-self-eval')['id']}"
    evaluation = db.get_authority_evaluation(candidate.id)
    assert evaluation is not None
    assert evaluation.disposition.value == (
        "continue_same_root" if continued else "escalate"
    )
    outcomes = [
        row for row in db.get_audit_logs(task_id)
        if row["action"] == "authority_hook"
    ]
    assert len(outcomes) == 1
    assert outcomes[0]["payload"]["outcome"] == (
        "continued_same_root" if continued else "escalated"
    )
    if not continued:
        assert outcomes[0]["payload"]["error"]
    return candidate


def test_sweep_orphaned_result_runs_real_authority_path(tmp_path):
    db, orch, queue, task_id, row = _seed_manager_recovery_result(tmp_path)

    _sweep_on_startup(db, queue, "test", orch)

    assert db.get_task(task_id).status is TaskStatus.PENDING
    assert queue._queue.get_nowait() == ("test", task_id, None)
    candidate = _assert_authority_denominator(db, task_id, continued=True)
    assert candidate.lifecycle_state.value == "consumed"
    assert [event.event_type for event in db.list_authority_audit(candidate.id)] == [
        "candidate_claimed", "evaluation_recorded", "candidate_consumed",
    ]
    assert row["id"] == db.get_latest_task_result(
        task_id, "engineering_manager", "sess-orph-self-eval"
    )["id"]
    assert not db.list_thread_messages("THR-RECOVERY")


@pytest.mark.parametrize("self_evaluation", ["absent", "malformed", "mismatch"])
def test_sweep_orphaned_result_invalid_evidence_fails_closed(
    tmp_path, self_evaluation,
):
    db, orch, queue, task_id, _ = _seed_manager_recovery_result(
        tmp_path, self_evaluation=self_evaluation,
    )

    _sweep_on_startup(db, queue, "test", orch)

    assert db.get_task(task_id).status is TaskStatus.ESCALATED
    assert queue._queue.empty()
    _assert_authority_denominator(db, task_id, continued=False)
    messages = db.list_thread_messages("THR-RECOVERY")
    assert len(messages) == 1
    assert messages[0].system_payload["kind_tag"] == "task_escalated"


def test_sweep_orphaned_result_replay_cannot_continue_twice(tmp_path):
    db, orch, queue, task_id, _ = _seed_manager_recovery_result(tmp_path)
    _sweep_on_startup(db, queue, "test", orch)
    assert queue._queue.get_nowait() == ("test", task_id, None)
    db.update_task(task_id, status=TaskStatus.IN_PROGRESS, block_kind=None)

    _sweep_on_startup(db, queue, "test", orch)

    assert db.get_task(task_id).status is TaskStatus.ESCALATED
    assert queue._queue.empty()
    assert len(db.list_authority_candidates_for_root(task_id)) == 1
    assert db.execute("SELECT COUNT(*) FROM authority_evaluations").fetchone()[0] == 1


@pytest.mark.parametrize("agent", ["dev_agent", "engineering_manager"])
def test_sweep_orphaned_result_worker_and_legacy_compatibility(tmp_path, agent):
    db, orch, queue = _seed_org_with_orch(tmp_path)
    task_id = f"TASK-COMPAT-{agent}"
    session_id = f"sess-{agent}"
    db.insert_task(TaskRecord(
        id=task_id, brief="x", team="engineering", assigned_agent=agent,
        status=TaskStatus.IN_PROGRESS, task_type="subtask",
    ))
    db.update_task(task_id, current_session_id=session_id)
    db.insert_task_result(
        task_id=task_id, agent=agent, session_id=session_id,
        status="completed", confidence_score=90, output_summary="done",
    )

    _sweep_on_startup(db, queue, "test", orch)

    assert db.get_task(task_id).status is TaskStatus.COMPLETED
    assert db.list_authority_candidates_for_root(task_id) == []


def _seed_org(tmp_path: Path, slug: str = "test") -> Database:
    """Initialize a multi-org runtime with one seeded org and return its DB."""
    runtime = RuntimeDir.init(tmp_path / "rt")
    org_root = runtime.orgs_dir / slug
    org_root.mkdir(parents=True)
    (org_root / "org").mkdir()
    (org_root / "org" / "teams.yaml").write_text("teams: {}\n")
    return Database(org_root / "happyranch.db")


def _seed_org_with_orch(
    tmp_path: Path, slug: str = "test",
) -> tuple[Database, Orchestrator, TaskQueue]:
    """Seed an org + construct a real Orchestrator wired to a real queue.

    Mirrors the sweep's production wiring closely enough that the
    bounded-wake and terminal-failure paths are exercisable end-to-end.
    Since TASK-3604, there is no auto-revisit path in production.
    """
    runtime = RuntimeDir.init(tmp_path / "rt")
    paths = OrgPaths(root=runtime.orgs_dir / slug)
    paths.teams_config_path.parent.mkdir(parents=True, exist_ok=True)
    paths.teams_config_path.write_text(
        "teams:\n"
        "  engineering:\n"
        "    manager: engineering_head\n"
        "    workers: [dev_agent]\n"
    )
    db = Database(paths.db_path)
    queue = TaskQueue()
    orch = Orchestrator(
        db=db, settings=Settings(), paths=paths, slug=slug,
        teams=TeamsRegistry.load(paths.root),
    )
    orch._queue = queue
    return db, orch, queue


def test_sweep_in_progress_to_failed(tmp_path: Path) -> None:
    db = _seed_org(tmp_path)
    db.insert_task(TaskRecord(id="T-1", brief="x"))
    db.update_task("T-1", status=TaskStatus.IN_PROGRESS)

    _sweep_on_startup(db, TaskQueue(), "test")

    t = db.get_task("T-1")
    assert t.status == TaskStatus.FAILED
    # THR-079: null executor_pid → fail-closed with undeterminable liveness note.
    assert t.note and "liveness undeterminable" in t.note


def test_sweep_parked_delegated_with_all_children_terminal_reenqueues(tmp_path):
    """Path B Branch 2 (the landmine): a parent parked on its children is stored
    in_progress(delegated) — NOT blocked. The sweep MUST re-enqueue it when all
    children are terminal, and MUST NOT force-fail it as a 'running' task."""
    db = _seed_org(tmp_path)
    # Parent in_progress(DELEGATED), child completed — lost the wake-up signal
    # to the daemon crash.
    db.insert_task(TaskRecord(id="T-PAR", brief="p"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS,
                   block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(id="T-CHD", brief="c", parent_task_id="T-PAR"))
    db.update_task("T-CHD", status=TaskStatus.COMPLETED, note="done")

    queue = TaskQueue()
    _sweep_on_startup(db, queue, "test")

    # Parent survives (not failed) AND is re-enqueued for its next decision step.
    assert db.get_task("T-PAR").status == TaskStatus.IN_PROGRESS
    assert db.get_task("T-PAR").block_kind == BlockKind.DELEGATED
    assert queue._queue.get_nowait() == ("test", "T-PAR", None)


def _seed_job(db: Database, job_id: str, task_id: str, status: str) -> None:
    """Insert a job row in the given status (bypasses the runner)."""
    from datetime import datetime, timezone

    from runtime.models import JobInterpreter, JobRecord
    db.insert_job(JobRecord(
        id=job_id, task_id=task_id, agent_name="dev_agent",
        title="t", rationale="r", script_text="echo x",
        interpreter=JobInterpreter.BASH,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    ))
    db._conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    db._conn.commit()


def test_sweep_blocked_on_job_with_live_job_survives_restart(tmp_path):
    """Path B Branch 3 (THE LANDMINE, #1 reviewer focus): a task parked on a
    still-in-flight job is stored in_progress(blocked_on_job) with NO live
    subprocess. The pre-Path-B sweep had no branch for it (it was status=blocked
    and simply skipped); Path B makes it in_progress, so without the explicit
    Branch-3 exclusion it would fall into Branch 1 and be WRONGLY FAILED on
    every restart. Assert it SURVIVES untouched."""
    db = _seed_org(tmp_path)
    _seed_job(db, "JOB-1", "T-JOB", status="running")  # still in-flight
    db.insert_task(TaskRecord(id="T-JOB", brief="j"))
    db.update_task("T-JOB", status=TaskStatus.IN_PROGRESS,
                   block_kind=BlockKind.BLOCKED_ON_JOB,
                   blocked_on_job_ids='["JOB-1"]', note="waiting on jobs")

    queue = TaskQueue()
    _sweep_on_startup(db, queue, "test")

    # SURVIVES: not failed, still parked, NOT re-enqueued (job still in flight).
    t = db.get_task("T-JOB")
    assert t.status == TaskStatus.IN_PROGRESS
    assert t.block_kind == BlockKind.BLOCKED_ON_JOB
    assert queue._queue.empty()


def test_sweep_blocked_on_job_with_terminal_job_reenqueues(tmp_path):
    """Path B Branch 3: when every blocking job is terminal at restart (the job
    finished while the daemon was down), the parked task is re-enqueued — the
    orphaned wake-up the live jobs_runner hook missed."""
    db = _seed_org(tmp_path)
    _seed_job(db, "JOB-1", "T-JOB", status="completed")  # finished while down
    db.insert_task(TaskRecord(id="T-JOB", brief="j"))
    db.update_task("T-JOB", status=TaskStatus.IN_PROGRESS,
                   block_kind=BlockKind.BLOCKED_ON_JOB,
                   blocked_on_job_ids='["JOB-1"]', note="waiting on jobs")

    queue = TaskQueue()
    _sweep_on_startup(db, queue, "test")

    # Not failed; re-enqueued for its resume step.
    assert db.get_task("T-JOB").status == TaskStatus.IN_PROGRESS
    assert queue._queue.get_nowait() == ("test", "T-JOB", None)


def test_sweep_leaves_escalated_alone(tmp_path):
    """Path B Branch 5: an escalated task (top-level status, founder-owned) is
    visited by the sweep — get_nonterminal_task_ids now yields it — and left
    untouched, mirroring the pre-Path-B blocked(escalated) fall-through."""
    db = _seed_org(tmp_path)
    db.insert_task(TaskRecord(id="T-1", brief="x"))
    db.update_task("T-1", status=TaskStatus.ESCALATED, block_kind=None,
                   note="needs founder")

    queue = TaskQueue()
    _sweep_on_startup(db, queue, "test")

    t = db.get_task("T-1")
    assert t.status == TaskStatus.ESCALATED
    assert queue._queue.empty()


def test_sweep_blocked_delegated_with_live_child_bounded_wake(tmp_path):
    """THR-064 / TASK-573: when sweep force-fails an in-progress child of an
    in_progress(delegated) parent, the parent gets a bounded-wake decision step
    (enqueued, NOT cascade-failed). Since TASK-3604 there is no auto-revisit —
    the parked non-terminal ancestor means the bounded-wake recovers the work
    directly without spawning any successor."""
    db, orch, queue = _seed_org_with_orch(tmp_path)
    db.insert_task(TaskRecord(
        id="T-PAR", brief="p", team="engineering",
        assigned_agent="engineering_head",
        status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED,
        note="waiting",
        task_type="task",
    ))
    db.insert_task(TaskRecord(
        id="T-CHD", brief="c", team="engineering",
        assigned_agent="dev_agent", parent_task_id="T-PAR",
        status=TaskStatus.IN_PROGRESS,
        task_type="subtask",
    ))

    # Suppress feishu side effects from the real orch.
    orch.notify_failed = lambda **kw: None  # type: ignore[assignment]

    _sweep_on_startup(db, queue, "test", orch)

    # Child force-failed.
    assert db.get_task("T-CHD").status == TaskStatus.FAILED
    # TASK-573: parent stays in_progress(delegated) for bounded-wake, not FAILED.
    assert db.get_task("T-PAR").status == TaskStatus.IN_PROGRESS
    assert db.get_task("T-PAR").block_kind == BlockKind.DELEGATED
    # NO auto-revisit twin — parked non-terminal ancestor means skip.
    revisits = [
        t for t in (db.get_task(tid)
                    for tid in db.get_nonterminal_task_ids())
        if t is not None and t.revisit_of_task_id == "T-PAR"
    ]
    assert len(revisits) == 0, (
        f"expected 0 auto-revisit twins (parked ancestor skips it); "
        f"got {len(revisits)}"
    )
    # Queue gets ONLY the parent bounded-wake enqueue (no twin root).
    enqueued = []
    while not queue._queue.empty():
        enqueued.append(queue._queue.get_nowait())
    enqueued_ids = [tid for (_slug, tid, _md) in enqueued]
    assert "T-PAR" in enqueued_ids  # parent bounded-wake


def test_sweep_leaves_blocked_escalated_alone(tmp_path):
    db = _seed_org(tmp_path)
    db.insert_task(TaskRecord(id="T-1", brief="x"))
    db.update_task("T-1", status=TaskStatus.ESCALATED, block_kind=None, note="halt")

    queue = TaskQueue()
    _sweep_on_startup(db, queue, "test")

    t = db.get_task("T-1")
    assert t.status == TaskStatus.ESCALATED
    assert t.block_kind is None
    assert queue._queue.empty()


def test_sweep_pending_stays_pending_but_gets_enqueued(tmp_path):
    """Pending rows from before the crash need a nudge — their original
    POST /tasks enqueue was lost when the daemon died."""
    db = _seed_org(tmp_path)
    db.insert_task(TaskRecord(id="T-1", brief="x"))

    queue = TaskQueue()
    _sweep_on_startup(db, queue, "test")

    assert db.get_task("T-1").status == TaskStatus.PENDING
    assert queue._queue.get_nowait() == ("test", "T-1", None)


def test_sweep_works_without_orchestrator_arg(tmp_path):
    """Degraded mode: with no orchestrator (test convenience only — production
    always passes one), the IN_PROGRESS branch marks-failed-and-audits and
    does not enqueue or notify (no orchestrator wiring)."""
    db = _seed_org(tmp_path)
    db.insert_task(TaskRecord(id="T-BC", brief="x"))
    db.update_task("T-BC", status=TaskStatus.IN_PROGRESS)
    _sweep_on_startup(db, TaskQueue(), "test")
    assert db.get_task("T-BC").status == TaskStatus.FAILED
    actions = [r["action"] for r in db.get_audit_logs("T-BC")]
    assert "daemon_restart_failure" in actions
    # No auto-revisit in degraded mode.
    assert "auto_revisit_of" not in actions


def test_sweep_in_progress_bounded_parent_wake(tmp_path):
    """THR-064: in-progress child of a parked DELEGATED root at restart is
    terminal FAILED with no successor. Parent gets bounded-wake instead;
    no twin root since TASK-3604 removed daemon auto-revisit. notify_failed
    is suppressed because the work is being retried via parent re-enqueue."""
    db, orch, queue = _seed_org_with_orch(tmp_path)
    # Root parent task is in_progress+delegated waiting on its in-flight child.
    db.insert_task(TaskRecord(
        id="T-ROOT", brief="root work", team="engineering",
        assigned_agent="engineering_head",
        status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED,
        task_type="task",
    ))
    db.insert_task(TaskRecord(
        id="T-CHD", brief="child work", team="engineering",
        assigned_agent="dev_agent", parent_task_id="T-ROOT",
        status=TaskStatus.IN_PROGRESS,
        task_type="subtask",
    ))

    notify_calls: list[dict] = []
    orch.notify_failed = lambda **kw: notify_calls.append(kw)  # type: ignore[assignment]

    _sweep_on_startup(db, queue, "test", orch)

    # Child force-failed.
    assert db.get_task("T-CHD").status == TaskStatus.FAILED
    # TASK-573: bounded-wake, not cascade-fail.
    assert db.get_task("T-ROOT").status == TaskStatus.IN_PROGRESS
    assert db.get_task("T-ROOT").block_kind == BlockKind.DELEGATED
    # NO auto-revisit twin — parked non-terminal ancestor skips it.
    revisits = [
        t for t in (db.get_task(tid)
                    for tid in db.get_nonterminal_task_ids())
        if t is not None and t.revisit_of_task_id == "T-ROOT"
    ]
    assert len(revisits) == 0, (
        f"expected 0 auto-revisit twins (parked ancestor); got {len(revisits)}"
    )
    # notify_failed is suppressed because the work is being retried via parent wake.
    assert notify_calls == []


def test_sweep_per_root_dedup(tmp_path):
    """THR-064: two in-flight children of the same parked root at restart
    correctly skip auto-revisit (parked ancestor). No twin roots at all."""
    db, orch, queue = _seed_org_with_orch(tmp_path)
    db.insert_task(TaskRecord(
        id="T-ROOT", brief="root", team="engineering",
        assigned_agent="engineering_head",
        status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED,
    ))
    db.insert_task(TaskRecord(
        id="T-CHD-A", brief="a", team="engineering",
        assigned_agent="dev_agent", parent_task_id="T-ROOT",
        status=TaskStatus.IN_PROGRESS,
    ))
    db.insert_task(TaskRecord(
        id="T-CHD-B", brief="b", team="engineering",
        assigned_agent="dev_agent", parent_task_id="T-ROOT",
        status=TaskStatus.IN_PROGRESS,
    ))

    notify_calls: list[dict] = []
    orch.notify_failed = lambda **kw: notify_calls.append(kw)  # type: ignore[assignment]

    _sweep_on_startup(db, queue, "test", orch)

    revisits = [
        t for t in (db.get_task(tid)
                    for tid in db.get_nonterminal_task_ids())
        if t is not None and t.revisit_of_task_id == "T-ROOT"
    ]
    assert len(revisits) == 0, (
        f"expected 0 auto-revisit twins (parked ancestor); got {len(revisits)}"
    )
    # Both siblings still cascade-suppressed: zero founder pings.
    assert notify_calls == []


def test_lifespan_recovers_orphaned_running_jobs(tmp_home, daemon_state):
    """Job rows left in 'running' state on daemon startup are force-failed."""
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from runtime.daemon.app import create_app
    from runtime.models import JobInterpreter, JobRecord, JobStatus

    org = daemon_state.orgs["alpha"]
    # Seed: insert a pending job then mark it running manually.
    job = JobRecord(
        id="JOB-001",
        task_id="TASK-001",
        agent_name="engineering_head",
        title="t",
        rationale="r",
        script_text="echo x",
        interpreter=JobInterpreter.BASH,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    org.db.insert_job(job)
    org.db._conn.execute(
        "UPDATE jobs SET status='running', started_at='2026-05-23T00:00:00Z' WHERE id='JOB-001'"
    )
    org.db._conn.commit()

    # Boot lifespan via TestClient context manager — startup hook fires.
    app = create_app(daemon_state)
    with TestClient(app):
        # Query inside the context so the DB is still open (lifespan teardown
        # calls close_all() on __exit__, after which the connection is gone).
        fetched = org.db.get_job("JOB-001")

    assert fetched is not None
    assert fetched.status == JobStatus.FAILED
    assert fetched.finished_at is not None
    # Recovery must distinguish a crash-orphan from a normal failure so the
    # founder UX and audit story preserve the cause.
    assert fetched.reason == "daemon_crash"


def test_terminate_all_inflight_awaits_runner_tasks(tmp_home, daemon_state):
    """Regression: clean shutdown must let in-flight runner tasks persist
    terminal state BEFORE the per-org DB is closed. Without this, a job sits
    in `running` until the next startup recovery scan."""
    import asyncio
    from datetime import datetime, timezone

    from runtime.daemon import jobs_runner
    from runtime.models import JobInterpreter, JobRecord, JobStatus

    org = daemon_state.orgs["alpha"]
    job = JobRecord(
        id="JOB-100",
        task_id="TASK-100",
        agent_name="engineering_head",
        title="t",
        rationale="r",
        script_text="x",
        interpreter=JobInterpreter.BASH,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    org.db.insert_job(job)
    org.db._conn.execute(
        "UPDATE jobs SET status='running' WHERE id='JOB-100'"
    )
    org.db._conn.commit()

    # Simulate a runner task that's still mid-flight: it sleeps briefly, then
    # transitions the row to FAILED. terminate_all_inflight must await this.
    async def fake_runner() -> None:
        await asyncio.sleep(0.05)
        org.db.transition_job_to_terminal(
            "JOB-100",
            status=JobStatus.FAILED,
            exit_code=-15,
            finished_at="2026-05-23T00:00:01Z",
            duration_ms=50,
            stdout_head="",
            stderr_head="killed by shutdown",
        )

    async def run_test() -> None:
        task = asyncio.create_task(fake_runner())
        jobs_runner.register_runner_task("JOB-100", task)
        # No subprocesses to kill — just await the runner task.
        await jobs_runner.terminate_all_inflight(
            grace_seconds=0, persist_timeout_seconds=2.0,
        )

    asyncio.run(run_test())

    fetched = org.db.get_job("JOB-100")
    assert fetched.status == JobStatus.FAILED, (
        "shutdown returned before the runner task persisted terminal state — "
        "row would have stayed `running` until next startup"
    )


# ── Thread invocation sweep (THR-046 message-112) ────────────────────────

def test_sweep_reconciles_pending_invocation_to_failed(tmp_path):
    """Branch 6: orphaned pending thread invocations are reaped to failed on
    daemon restart so the UI reply box (queued/working render) clears."""
    from runtime.daemon.routes.threads import _responder_entry

    db = _seed_org(tmp_path)
    db.insert_thread(ThreadRecord(id="THR-001", subject="x"))
    inv = db.mint_thread_invocation(
        thread_id="THR-001", agent_name="alpha", triggering_seq=1,
        purpose=ThreadInvocationPurpose.REPLY,
    )
    assert inv.status.value == "pending"
    # Verify wire render is 'queued' (no started_at)
    wire_entry = _responder_entry({
        "agent_name": "alpha", "purpose": "reply", "status": "pending",
        "consumed_at": None, "started_at": None,
    })
    assert wire_entry.status == "queued"

    _sweep_on_startup(db, TaskQueue(), "test")

    # DB row is now terminal.
    reel = db.get_invocation_any_status(inv.invocation_token)
    assert reel is not None
    assert reel.status.value == "failed"
    assert reel.decline_reason == "daemon_restart"
    assert reel.consumed_at is not None

    # Wire render is now 'failed' (box clears).
    wire_after = _responder_entry({
        "agent_name": "alpha", "purpose": "reply", "status": reel.status.value,
        "consumed_at": reel.consumed_at.isoformat() if reel.consumed_at else None,
        "started_at": reel.started_at.isoformat() if reel.started_at else None,
    })
    assert wire_after.status == "failed", (
        f"expected wire status 'failed' after sweep; got '{wire_after.status}'"
    )


def test_sweep_reconciles_working_invocation_to_failed(tmp_path):
    """Branch 6: a started (working) pending invocation is also reaped to
    failed. The wire render flips from 'working' to 'failed'."""
    from datetime import datetime, timezone

    from runtime.daemon.routes.threads import _responder_entry

    db = _seed_org(tmp_path)
    db.insert_thread(ThreadRecord(id="THR-001", subject="x"))
    inv = db.mint_thread_invocation(
        thread_id="THR-001", agent_name="alpha", triggering_seq=1,
        purpose=ThreadInvocationPurpose.REPLY,
    )
    # Simulate a subprocess that started before the daemon was killed.
    started_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    db._conn.execute(
        "UPDATE thread_invocations SET started_at = ? WHERE invocation_token = ?",
        (started_ts, inv.invocation_token),
    )
    db._conn.commit()

    # Wire renders 'working' because started_at is set.
    wire_entry = _responder_entry({
        "agent_name": "alpha", "purpose": "reply", "status": "pending",
        "consumed_at": None, "started_at": started_ts,
    })
    assert wire_entry.status == "working"

    _sweep_on_startup(db, TaskQueue(), "test")

    reel = db.get_invocation_any_status(inv.invocation_token)
    assert reel is not None
    assert reel.status.value == "failed"
    assert reel.decline_reason == "daemon_restart"

    wire_after = _responder_entry({
        "agent_name": "alpha", "purpose": "reply", "status": reel.status.value,
        "consumed_at": reel.consumed_at.isoformat() if reel.consumed_at else None,
        "started_at": reel.started_at.isoformat() if reel.started_at else None,
    })
    assert wire_after.status == "failed"


def test_sweep_reconciles_all_threads_pending_invocations(tmp_path):
    """Branch 6: reaps pending invocations across ALL threads, not just open
    ones. A pending invocation is orphaned regardless of thread status."""
    from runtime.daemon.routes.threads import _responder_entry

    db = _seed_org(tmp_path)
    # Thread 1 — has pending invocation
    db.insert_thread(ThreadRecord(id="THR-001", subject="open"))
    inv1 = db.mint_thread_invocation(
        thread_id="THR-001", agent_name="alpha", triggering_seq=1,
        purpose=ThreadInvocationPurpose.REPLY,
    )
    # Thread 2 — archived thread (past conversation), also has pending
    db.insert_thread(ThreadRecord(id="THR-002", subject="archived"))
    db.set_thread_status("THR-002", status=ThreadStatus.ARCHIVED)
    inv2 = db.mint_thread_invocation(
        thread_id="THR-002", agent_name="bravo", triggering_seq=1,
        purpose=ThreadInvocationPurpose.REPLY,
    )

    _sweep_on_startup(db, TaskQueue(), "test")

    for token in (inv1.invocation_token, inv2.invocation_token):
        reel = db.get_invocation_any_status(token)
        assert reel is not None
        assert reel.status.value == "failed", (
            f"invocation {token} should be failed after sweep, got "
            f"{reel.status.value}"
        )
        assert reel.decline_reason == "daemon_restart"


def test_sweep_leaves_already_terminal_invocations_alone(tmp_path):
    """Branch 6: terminal invocations (consumed, declined, timeout) are NOT
    touched — only genuinely pending rows are reaped."""
    from runtime.daemon.routes.threads import _responder_entry

    db = _seed_org(tmp_path)
    db.insert_thread(ThreadRecord(id="THR-001", subject="x"))

    # Already-consumed invocation.
    consumed = db.mint_thread_invocation(
        thread_id="THR-001", agent_name="alpha", triggering_seq=1,
        purpose=ThreadInvocationPurpose.REPLY,
    )
    db.consume_invocation(consumed.invocation_token)

    # Already-declined.
    declined = db.mint_thread_invocation(
        thread_id="THR-001", agent_name="bravo", triggering_seq=1,
        purpose=ThreadInvocationPurpose.REPLY,
    )
    db.mark_invocation_declined(declined.invocation_token, decline_reason="agent_declined")

    _sweep_on_startup(db, TaskQueue(), "test")

    # Consumed stays consumed.
    c = db.get_invocation_any_status(consumed.invocation_token)
    assert c is not None and c.status.value == "consumed", \
        f"consumed invocation was altered to {c.status.value if c else 'None'}"

    # Declined stays declined.
    d = db.get_invocation_any_status(declined.invocation_token)
    assert d is not None and d.status.value == "declined", \
        f"declined invocation was altered to {d.status.value if d else 'None'}"


# ── THR-064: daemon-restart double-recovery (TASK-1855) ──────────────────

def test_thr064_parked_ancestor_bounded_wake(tmp_path):
    """THR-064: A parked manager root (in_progress/DELEGATED) with ONE running
    child at restart -> EXACTLY ONE woken root (parent bounded-wake), NO twin
    successor (auto-revisit removed in TASK-3604), killed child FAILED with
    the restart marker note."""
    db, orch, queue = _seed_org_with_orch(tmp_path)
    db.insert_task(TaskRecord(
        id="T-ROOT", brief="root", team="engineering",
        assigned_agent="engineering_head",
        status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED,
        task_type="task",
    ))
    db.insert_task(TaskRecord(
        id="T-CHD", brief="child", team="engineering",
        assigned_agent="dev_agent", parent_task_id="T-ROOT",
        status=TaskStatus.IN_PROGRESS,
        task_type="subtask",
    ))

    orch.notify_failed = lambda **kw: None  # type: ignore[assignment]

    _sweep_on_startup(db, queue, "test", orch)

    # Child force-failed (null executor_pid → fail-closed).
    chd = db.get_task("T-CHD")
    assert chd.status == TaskStatus.FAILED
    assert "liveness undeterminable" in (chd.note or "")

    # Parent stays parked (bounded-wake), NOT cascade-failed.
    par = db.get_task("T-ROOT")
    assert par.status == TaskStatus.IN_PROGRESS
    assert par.block_kind == BlockKind.DELEGATED

    # NO auto-revisit twin — parked ancestor recovery via bounded-wake instead.
    revisits = [
        t for t in (db.get_task(tid)
                    for tid in db.get_nonterminal_task_ids())
        if t is not None and t.revisit_of_task_id == "T-ROOT"
    ]
    assert len(revisits) == 0, (
        f"expected 0 auto-revisit twins; got {len(revisits)}"
    )

    # Parent is enqueued (bounded-wake recovery).
    enqueued = []
    while not queue._queue.empty():
        enqueued.append(queue._queue.get_nowait())
    enqueued_ids = [tid for (_slug, tid, _md) in enqueued]
    assert "T-ROOT" in enqueued_ids, (
        f"expected parent T-ROOT to be enqueued for bounded-wake; "
        f"got {enqueued_ids}"
    )


def test_thr064_guardrail1_worker_root_terminal_failed(tmp_path):
    """THR-079 / TASK-3604: a genuine parentless worker root subprocess death
    (no parked non-terminal ancestor) is terminal FAILED with no daemon
    successor. The THR-079 pid-liveness probe plus TASK-3604's removal of
    auto-revisit means only a daemon_restart_failure audit row is written;
    recovery is explicit founder action."""
    db, orch, queue = _seed_org_with_orch(tmp_path)
    # A root task with NO parent and NO block_kind — a genuine worker root.
    db.insert_task(TaskRecord(
        id="T-WORKER", brief="worker root", team="engineering",
        assigned_agent="dev_agent",
        status=TaskStatus.IN_PROGRESS,
        task_type="task",
    ))

    orch.notify_failed = lambda **kw: None  # type: ignore[assignment]

    _sweep_on_startup(db, queue, "test", orch)

    # Killed task is failed (null executor_pid → fail-closed).
    t = db.get_task("T-WORKER")
    assert t.status == TaskStatus.FAILED

    # THR-079: NO auto-revisit twin — pid-liveness probe supersedes auto-revisit.
    revisits = [
        t for t in (db.get_task(tid)
                    for tid in db.get_nonterminal_task_ids())
        if t is not None and t.revisit_of_task_id == "T-WORKER"
    ]
    assert len(revisits) == 0, (
        f"THR-079: expected 0 auto-revisit twins (pid-liveness probe supersedes auto-revisit); "
        f"got {len(revisits)}"
    )


def test_thr064_fanout_killed_child_does_not_wake_parent_early(tmp_path):
    """THR-064 RED-FIRST (fails on merge-base b96e105, passes on branch head).

    Fan-out barrier: a restart-killed child among still-live siblings MUST
    NOT wake the parked root early. Only when all children terminal does the
    parent wake."""
    db, orch, queue = _seed_org_with_orch(tmp_path)
    db.insert_task(TaskRecord(
        id="T-ROOT", brief="root", team="engineering",
        assigned_agent="engineering_head",
        status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED,
        task_type="task",
    ))
    # Two children — one killed by restart, one blocked_on_job (survives sweep).
    _seed_job(db, "JOB-SIB", "T-CHD-B", status="running")
    db.insert_task(TaskRecord(
        id="T-CHD-A", brief="killed child", team="engineering",
        assigned_agent="dev_agent", parent_task_id="T-ROOT",
        status=TaskStatus.IN_PROGRESS,
        task_type="subtask",
    ))
    db.insert_task(TaskRecord(
        id="T-CHD-B", brief="job-blocked sibling", team="engineering",
        assigned_agent="dev_agent", parent_task_id="T-ROOT",
        status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.BLOCKED_ON_JOB,
        blocked_on_job_ids='["JOB-SIB"]',
        task_type="subtask",
    ))

    orch.notify_failed = lambda **kw: None  # type: ignore[assignment]

    _sweep_on_startup(db, queue, "test", orch)

    # Killed child is failed.
    assert db.get_task("T-CHD-A").status == TaskStatus.FAILED
    # Job-blocked sibling survives the sweep (BLOCKED_ON_JOB, Branch 3 guard).
    assert db.get_task("T-CHD-B").status == TaskStatus.IN_PROGRESS, (
        f"job-blocked sibling should survive; got {db.get_task('T-CHD-B').status}"
    )

    # Parent is NOT enqueued early — sibling still in_progress blocks the wake.
    assert queue._queue.empty(), (
        "fan-out barrier violated: parent was woken before all siblings terminal"
    )

    # NO auto-revisit twin (parked ancestor exists → skip).
    revisits = [
        t for t in (db.get_task(tid)
                    for tid in db.get_nonterminal_task_ids())
        if t is not None and t.revisit_of_task_id == "T-ROOT"
    ]
    assert len(revisits) == 0, (
        f"expected 0 auto-revisit twins; got {len(revisits)}"
    )


def test_thread_queue_wiring_before_task_workers_prevents_enqueue_unavailable(
    tmp_home, daemon_state,
):
    """Regression THR-109: daemon lifespan MUST wire thread queues + main loop
    BEFORE starting task workers.

    This test exercises two complementary verification surfaces:

    **A. Source-order guard (deterministic).** Uses ``inspect.getsource``
    to verify the two statements inside ``_wire_then_start_workers``
    appear in the correct order: ``_attach_thread_queue_wiring`` before
    ``ensure_workers_started``.  This is a pure code-level check that
    fails immediately if the helper's internal ordering is ever reversed —
    no runtime race, no sleep, no non-determinism.

    **B. Behavioural regression (runtime, deterministic sync).** Calls the SAME
    production helper that ``_lifespan`` invokes via a background event loop.
    A test-local wrapper around the real ``ThreadQueue.put`` records every
    ``ThreadJob`` and signals a ``threading.Event`` after its ``await``
    completes, so the test waits on explicit delivery proof — never DB polling
    or arbitrary sleep.  Real queue workers pick up a pre-seeded terminal task,
    exercise the exact ``_maybe_post_thread_escalation`` →
    ``_append_followup_system_and_reinvoke`` code path, and the test asserts
    the post-fix outcomes.

    With the post-fix helper ordering (wiring before workers):
    - (1) a TASK_FOLLOWUP invocation is minted and its ``ThreadJob`` is
      delivered through the real ``ThreadQueue.put`` (deterministic signal)
    - (2) no thread_followup_skipped(enqueue_unavailable) audit is written
    - (3) the task reaches the expected escalation terminal state

    The unchanged test must fail if ONLY ``_wire_then_start_workers`` call
    order in app.py is temporarily reversed (red-side proof, not committed).
    """
    import asyncio
    import inspect
    import threading

    from runtime.config import Settings as _Settings
    from runtime.daemon.app import _wire_then_start_workers
    from runtime.models import (
        TaskRecord, TaskStatus, ThreadInvocationPurpose, ThreadRecord,
    )

    # ── A. Source-order guard (deterministic) ──
    src = inspect.getsource(_wire_then_start_workers)
    wire_idx = src.index("_attach_thread_queue_wiring")
    workers_idx = src.index("ensure_workers_started")
    assert wire_idx < workers_idx, (
        f"_wire_then_start_workers: _attach_thread_queue_wiring "
        f"(idx {wire_idx}) must precede ensure_workers_started "
        f"(idx {workers_idx}); ordering is reversed — TASK_FOLLOWUP "
        f"invocations will strand with enqueue_unavailable"
    )

    # ── B. Behavioural regression (runtime, deterministic sync) ──

    # Max orchestration steps of 0 ensures the thread-dispatched root task
    # hits the budget guard immediately on first pickup — no executor needed.
    org = daemon_state.orgs["alpha"]
    org.orchestrator._settings = _Settings(max_orchestration_steps=0)
    db = org.db
    orch = org.orchestrator
    audit = orch._audit
    thread_queue = org.thread_queue

    # ── Deterministic delivery signal: wrap ThreadQueue.put ──
    # A test-local async wrapper around the real ThreadQueue.put that records
    # the ThreadJob and signals a threading.Event *after* the await completes.
    # This guarantees the job is actually enqueued in the asyncio.Queue before
    # the test proceeds — no DB polling, no sleep, no race window.
    delivery_event = threading.Event()
    delivered_job = None
    _original_put = thread_queue.put

    async def _wrapped_put(job):
        nonlocal delivered_job
        await _original_put(job)
        delivered_job = job
        delivery_event.set()

    thread_queue.put = _wrapped_put  # type: ignore[method-assign]

    # Seed an OPEN thread + dispatched PENDING root task.
    db.insert_thread(ThreadRecord(id="THR-STRT", subject="startup ordering"))
    db.add_thread_participant("THR-STRT", "engineering_head", added_by="founder")
    db.insert_task(TaskRecord(
        id="TASK-STRT", brief="startup test", team="engineering",
        assigned_agent="engineering_head",
        dispatched_from_thread_id="THR-STRT",
    ))
    audit.log_thread_dispatch(
        "THR-STRT", task_id="TASK-STRT", dispatcher="engineering_head",
        target_agent="engineering_head", team="engineering",
    )

    # Enqueue the PENDING task so workers pick it up immediately on start.
    daemon_state.queue.put_nowait(org.slug, "TASK-STRT")

    # Start a real event loop in a background daemon thread.
    # Mirrors the daemon lifespan where FastAPI/uvicorn runs the loop.
    loop = asyncio.new_event_loop()
    bg_thread = threading.Thread(target=loop.run_forever, daemon=True)
    bg_thread.start()

    try:
        # Call the same production helper that _lifespan uses.
        async def _start():
            _wire_then_start_workers(daemon_state, loop)
        asyncio.run_coroutine_threadsafe(_start(), loop).result(timeout=5.0)

        # Wait for deterministic delivery proof — the wrapped put signals
        # delivery_event AFTER the real asyncio.Queue.put await completes.
        # This replaces the flaky DB-polling loop.
        assert delivery_event.wait(timeout=10.0), (
            "ThreadJob was never delivered via ThreadQueue.put within 10s; "
            "thread queue wiring is not in place before workers started — "
            "TASK_FOLLOWUP invocations will strand with enqueue_unavailable"
        )

        # (1) TASK_FOLLOWUP invocation is minted and the delivered ThreadJob
        #     matches the minted invocation token.
        invs = db.list_thread_invocations("THR-STRT")
        followups = [
            i for i in invs
            if i.purpose == ThreadInvocationPurpose.TASK_FOLLOWUP
        ]
        assert len(followups) >= 1, (
            f"expected \u22651 TASK_FOLLOWUP invocation, got {len(followups)}"
        )
        assert delivered_job.org_slug == org.slug
        assert delivered_job.invocation_token == followups[0].invocation_token, (
            f"delivered job token {delivered_job.invocation_token} "
            f"!= minted token {followups[0].invocation_token}"
        )

        # (2) No enqueue_unavailable audit row was written.
        audit_rows = db.get_audit_logs("TASK-STRT")
        skipped = [
            r for r in audit_rows
            if r.get("action") == "thread_followup_skipped"
            and "enqueue_unavailable" in str(r.get("payload", {}))
        ]
        assert not skipped, (
            f"found enqueue_unavailable audit -- thread queue wiring was "
            f"not in place before escalation fired: {skipped}"
        )

        # (3) Normal nearby lifecycle: task reached the expected terminal state.
        t = db.get_task("TASK-STRT")
        assert t.status == TaskStatus.ESCALATED
        assert "max steps" in (t.note or "")

        # Consume the delivered job from the real queue (do not start the
        # daemon ThreadInvocationRunner — consuming the observed job is
        # sufficient and prevents an unrelated agent execution).
        fut = asyncio.run_coroutine_threadsafe(thread_queue.get(), loop)
        job = fut.result(timeout=2.0)
        assert job.invocation_token == delivered_job.invocation_token
    finally:
        # Restore the original put before cleanup.
        thread_queue.put = _original_put  # type: ignore[method-assign]
        # Stop workers gracefully before tearing down the loop.
        async def _stop():
            await daemon_state.queue.stop(timeout=2.0)
        try:
            asyncio.run_coroutine_threadsafe(_stop(), loop).result(timeout=3.0)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        bg_thread.join(timeout=2.0)


# ── Orphaned result local_ci reconstruction ──────────────────────────────

def test_sweep_orphaned_result_preserves_local_ci_in_audit_and_consumption(tmp_path):
    """An orphaned task_result with valid local_ci must be carried into
    the CompletionReport consumed by the sweep. The audit event must contain
    the exact local_ci object, and the task must reach COMPLETED."""
    from runtime.models import LocalCiEvidence

    db, orch, queue = _seed_org_with_orch(tmp_path)

    db.insert_task(TaskRecord(
        id="TASK-ORPH-LC", brief="x", team="engineering",
        assigned_agent="dev_agent", status=TaskStatus.IN_PROGRESS,
        task_type="task",
    ))
    db.update_task("TASK-ORPH-LC", current_session_id="sess-orph-lc")

    db.insert_task_result(
        task_id="TASK-ORPH-LC",
        agent="dev_agent",
        session_id="sess-orph-lc",
        status="completed",
        output_summary="Done with CI evidence",
        confidence_score=95,
        decision_json='{"action":"done","summary":"Done with CI evidence"}',
        local_ci_json='{"command":"scripts/local_ci.sh all","exit_code":0}',
    )

    _sweep_on_startup(db, queue, "test", orch)

    t = db.get_task("TASK-ORPH-LC")
    assert t.status == TaskStatus.COMPLETED, (
        f"expected COMPLETED, got {t.status}"
    )

    # Verify the audit payload contains the exact local_ci.
    logs = db.get_audit_logs("TASK-ORPH-LC")
    completion_audits = [r for r in logs if r["action"] == "completion_report"]
    assert len(completion_audits) >= 1, (
        f"expected at least one completion_report audit; got {logs}"
    )
    payload = completion_audits[0]["payload"]
    assert payload.get("local_ci") == {
        "command": "scripts/local_ci.sh all",
        "exit_code": 0,
    }, f"audit payload missing local_ci: {payload}"


def test_sweep_orphaned_thread_originated_supersede_escalates_once(tmp_path, monkeypatch):
    """A rejected thread-origin decision is durable and cannot replay."""
    import json

    db, orch, queue = _seed_org_with_orch(tmp_path)
    db.insert_task(TaskRecord(
        id="T-STARTUP-SUP", brief="original", team="engineering",
        assigned_agent="engineering_head", status=TaskStatus.IN_PROGRESS,
        current_session_id="sess-startup",
    ))
    db.execute(
        "UPDATE tasks SET dispatched_from_thread_id = 'THR-152' WHERE id = 'T-STARTUP-SUP'"
    )
    db._conn.commit()
    db.insert_task_result(
        task_id="T-STARTUP-SUP", agent="engineering_head", session_id="sess-startup",
        status="completed", confidence_score=90, output_summary="replace",
        decision_json=json.dumps({
            "action": "supersede", "successor_brief": "replacement",
            "rationale": "new evidence",
            "attestation": {
                "recovery_reason": "Evidence invalidated the old plan.",
                "policy_product_intent_unchanged": True,
                "no_budget_or_external_commitment": True,
                "no_permission_or_cross_team_change": True,
                "no_schema_auth_security_privacy_or_data_access_change": True,
                "no_unresolved_founder_gate": True,
            },
        }),
    )
    monkeypatch.setenv("HAPPYRANCH_MANAGER_SUPERSESSION_ENABLED", "1")
    monkeypatch.setenv("HAPPYRANCH_MANAGER_SUPERSESSION_PILOT_TEAM", "engineering")

    _sweep_on_startup(db, queue, "test", orch)

    task = db.get_task("T-STARTUP-SUP")
    assert task.status is TaskStatus.ESCALATED
    assert task.note == (
        "manager supersession rejected: thread-origin roots are not eligible "
        "for supersession; founder action required"
    )
    assert db.execute("SELECT COUNT(*) FROM manager_supersessions").fetchone()[0] == 0
    assert queue._queue.empty()
    logs = db.get_audit_logs("T-STARTUP-SUP")
    assert [row["action"] for row in logs].count("orchestration_step") == 1
    escalation = next(row for row in logs if row["action"] == "escalation")
    assert escalation["payload"] == {"reason": task.note}
    authority = next(row for row in logs if row["action"] == "authority_hook")
    assert authority["payload"] == {
        "outcome": "not_applicable",
        "reason_code": "runtime_manager_supersession_thread_origin_ineligible",
        "reason": "runtime-raised escalation is not an authority decision",
        "causal_escalation_audit_id": escalation["id"],
    }

    # Escalated rows are outside both startup and zombie recovery allowlists.
    # A replay pass therefore cannot consume the same decision a second time.
    _sweep_on_startup(db, queue, "test", orch)
    replay_logs = db.get_audit_logs("T-STARTUP-SUP")
    assert [row["action"] for row in replay_logs].count("orchestration_step") == 1
    assert [row["action"] for row in replay_logs].count("escalation") == 1


def test_sweep_orphaned_non_thread_supersede_still_commits_atomically(tmp_path, monkeypatch):
    """The rejection correction does not narrow ordinary eligible supersession."""
    db, orch, queue = _seed_org_with_orch(tmp_path)
    db.insert_task(TaskRecord(
        id="T-STARTUP-ELIGIBLE", brief="original", team="engineering",
        assigned_agent="engineering_head", status=TaskStatus.IN_PROGRESS,
        current_session_id="sess-eligible",
    ))
    db.insert_task_result(
        task_id="T-STARTUP-ELIGIBLE", agent="engineering_head",
        session_id="sess-eligible", status="completed", confidence_score=90,
        output_summary="replace", decision_json=json.dumps({
            "action": "supersede", "successor_brief": "replacement",
            "rationale": "new evidence", "attestation": {
                "recovery_reason": "Evidence invalidated the old plan.",
                "policy_product_intent_unchanged": True,
                "no_budget_or_external_commitment": True,
                "no_permission_or_cross_team_change": True,
                "no_schema_auth_security_privacy_or_data_access_change": True,
                "no_unresolved_founder_gate": True,
            },
        }),
    )
    monkeypatch.setenv("HAPPYRANCH_MANAGER_SUPERSESSION_ENABLED", "1")
    monkeypatch.setenv("HAPPYRANCH_MANAGER_SUPERSESSION_PILOT_TEAM", "engineering")

    _sweep_on_startup(db, queue, "test", orch)

    predecessor = db.get_task("T-STARTUP-ELIGIBLE")
    assert predecessor.status is TaskStatus.SUPERSEDED
    relation = db.execute("SELECT * FROM manager_supersessions").fetchone()
    successor = db.get_task(relation["successor_task_id"])
    assert successor.status is TaskStatus.PENDING
    assert queue._queue.get_nowait() == ("test", successor.id, None)


def test_sweep_supersede_competing_cancellation_remains_silent_race(tmp_path, monkeypatch):
    """A cancellation winning the supersession CAS retains its terminal state."""
    db, orch, queue = _seed_org_with_orch(tmp_path)
    task_id = "T-STARTUP-RACE"
    db.insert_task(TaskRecord(
        id=task_id, brief="original", team="engineering",
        assigned_agent="engineering_head", status=TaskStatus.IN_PROGRESS,
        current_session_id="sess-race",
    ))
    db.insert_task_result(
        task_id=task_id, agent="engineering_head", session_id="sess-race",
        status="completed", confidence_score=90, output_summary="replace",
        decision_json=json.dumps({
            "action": "supersede", "successor_brief": "replacement",
            "rationale": "new evidence", "attestation": {
                "recovery_reason": "Evidence invalidated the old plan.",
                "policy_product_intent_unchanged": True,
                "no_budget_or_external_commitment": True,
                "no_permission_or_cross_team_change": True,
                "no_schema_auth_security_privacy_or_data_access_change": True,
                "no_unresolved_founder_gate": True,
            },
        }),
    )

    def cancellation_wins(*args, **kwargs):
        db.update_task(
            task_id, status=TaskStatus.CANCELLED,
            cancelled_at="2026-09-06T00:00:00+00:00",
            completed_at="2026-09-06T00:00:00+00:00",
            note="cancelled by founder",
        )
        return None

    monkeypatch.setattr(db, "try_manager_supersede", cancellation_wins)
    monkeypatch.setenv("HAPPYRANCH_MANAGER_SUPERSESSION_ENABLED", "1")
    monkeypatch.setenv("HAPPYRANCH_MANAGER_SUPERSESSION_PILOT_TEAM", "engineering")

    _sweep_on_startup(db, queue, "test", orch)

    assert db.get_task(task_id).status is TaskStatus.CANCELLED
    assert db.execute("SELECT COUNT(*) FROM manager_supersessions").fetchone()[0] == 0
    assert not any(
        row["action"] == "escalation" for row in db.get_audit_logs(task_id)
    )
    assert queue._queue.empty()


def test_sweep_orphaned_result_malformed_local_ci_does_not_crash(tmp_path):
    """An orphaned task_result with malformed local_ci JSON must not crash
    the sweep or alter its preexisting restart behavior. The task is still
    consumed; local_ci is None."""
    import json as _json

    db, orch, queue = _seed_org_with_orch(tmp_path)

    db.insert_task(TaskRecord(
        id="TASK-ORPH-MAL", brief="x", team="engineering",
        assigned_agent="dev_agent", status=TaskStatus.IN_PROGRESS,
        task_type="task",
    ))
    db.update_task("TASK-ORPH-MAL", current_session_id="sess-orph-mal")

    # Use insert_task_result without local_ci_json, then directly set
    # malformed JSON via the connection to bypass normal serialization.
    db.insert_task_result(
        task_id="TASK-ORPH-MAL",
        agent="dev_agent",
        session_id="sess-orph-mal",
        status="completed",
        output_summary="Done with bad CI evidence",
        confidence_score=95,
        decision_json='{"action":"done","summary":"Done with bad CI evidence"}',
    )
    db._conn.execute(
        "UPDATE task_results SET local_ci = ? "
        "WHERE task_id = ? AND session_id = ?",
        ("NOT VALID JSON", "TASK-ORPH-MAL", "sess-orph-mal"),
    )
    db._conn.commit()

    # Must not crash.
    _sweep_on_startup(db, queue, "test", orch)

    t = db.get_task("TASK-ORPH-MAL")
    assert t.status == TaskStatus.COMPLETED, (
        f"expected COMPLETED (malformed local_ci should not block consumption), "
        f"got {t.status}"
    )

    # The audit payload must still exist but with local_ci as null/absent.
    logs = db.get_audit_logs("TASK-ORPH-MAL")
    completion_audits = [r for r in logs if r["action"] == "completion_report"]
    assert len(completion_audits) >= 1
    payload = completion_audits[0]["payload"]
    assert payload.get("local_ci") is None, (
        f"malformed local_ci should degrade to None; got {payload.get('local_ci')}"
    )
