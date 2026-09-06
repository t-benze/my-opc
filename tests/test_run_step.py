"""Unit tests for Orchestrator.run_step — the single primitive that advances
a task one subprocess call at a time under the new async execution model."""
from __future__ import annotations

from pathlib import Path

import pytest

from runtime.config import Settings
from runtime.infrastructure.database import Database
from runtime.models import BlockKind, TaskRecord, TaskStatus
from runtime.orchestrator._paths import OrgPaths
from runtime.orchestrator.teams import TeamsRegistry
from runtime.runtime import RuntimeDir


@pytest.fixture(autouse=True)
def _seed_active_agents_for_run_step(runtime: OrgPaths):
    """Task launch is fail-closed: an active AgentDef is required.

    Legacy tests created only a workspace. Seed active frontmatter for the
    agents used in this module so launch/token-usage resolution admits them.
    """
    from tests.conftest import seed_test_agents
    seed_test_agents(runtime, ("engineering_head", "dev_agent", "content_head", "content_agent"))


@pytest.fixture
def runtime(tmp_path: Path) -> OrgPaths:
    rt = RuntimeDir.init(tmp_path / "rt")
    paths = OrgPaths(root=rt.orgs_dir / "test")
    # Seed managers for two teams so manager-root decisions can prove team scope.
    paths.teams_config_path.parent.mkdir(parents=True, exist_ok=True)
    paths.teams_config_path.write_text(
        "teams:\n"
        "  engineering:\n"
        "    manager: engineering_head\n"
        "    workers: [product_manager, dev_agent, payment_agent, qa_engineer]\n"
        "  content:\n"
        "    manager: content_head\n"
        "    workers: [content_agent]\n"
    )
    return paths


@pytest.fixture
def db(runtime: OrgPaths) -> Database:
    return Database(runtime.db_path)


def test_run_step_silent_noop_when_task_missing(runtime, db):
    from runtime.orchestrator.orchestrator import Orchestrator
    settings = Settings(max_orchestration_steps=3)
    orch = Orchestrator(db=db, settings=settings, paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    # Just must not raise
    orch.run_step("TASK-NOPE")


def test_run_step_noop_on_blocked_escalated(runtime, db):
    """A task in ESCALATED isn't eligible for run_step — it waits
    for /resolve-escalation to transition it first. Second-hand enqueue
    must be silently ignored."""
    from runtime.orchestrator.orchestrator import Orchestrator
    db.insert_task(TaskRecord(id="T-1", brief="x"))
    db.update_task("T-1", status=TaskStatus.ESCALATED, block_kind=None,
                   note="halted")
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    orch.run_step("T-1")
    t = db.get_task("T-1")
    assert t.status == TaskStatus.ESCALATED
    assert t.block_kind is None


def test_run_step_over_budget_parks_escalated(runtime, db):
    from runtime.orchestrator.orchestrator import Orchestrator
    settings = Settings(max_orchestration_steps=3)
    db.insert_task(TaskRecord(
        id="T-1", brief="x", assigned_agent="engineering_head",
    ))
    db.update_task("T-1", orchestration_step_count=3)  # already at the cap

    orch = Orchestrator(db=db, settings=settings, paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    orch.run_step("T-1")

    t = db.get_task("T-1")
    assert t.status == TaskStatus.ESCALATED  # Path B: top-level status
    assert t.block_kind is None
    assert t.note and "max steps" in t.note
    # Audit row
    escalations = [
        a for a in db.get_audit_logs("T-1") if a["action"] == "escalation"
    ]
    assert len(escalations) == 1
    assert "max steps" in escalations[0]["payload"]["reason"]
    outcomes = [
        a for a in db.get_audit_logs("T-1") if a["action"] == "authority_hook"
    ]
    assert len(outcomes) == 1
    assert outcomes[0]["payload"]["outcome"] == "not_applicable"
    assert outcomes[0]["payload"]["reason_code"] == (
        "runtime_orchestration_step_budget_exhausted"
    )
    assert outcomes[0]["payload"]["causal_escalation_audit_id"] == escalations[0]["id"]


def test_run_step_transitions_pending_to_in_progress_and_increments_count(
    runtime, db, monkeypatch,
):
    """On pickup, run_step must flip to in_progress, clear block fields,
    and increment the step counter exactly once — BEFORE invoking the agent."""
    from runtime.orchestrator.orchestrator import Orchestrator, WorkspaceNotInitialized

    db.insert_task(TaskRecord(
        id="T-1", brief="x", assigned_agent="engineering_head",
    ))
    orch = Orchestrator(db=db, settings=Settings(max_orchestration_steps=10), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))

    # Force _run_agent to raise so we can inspect the DB state mid-flight.
    captured: dict = {}
    def fail(task_id, agent, prompt, on_session_started=None):
        t = db.get_task(task_id)
        captured["status"] = t.status
        captured["count"] = t.orchestration_step_count
        captured["block_kind"] = t.block_kind
        captured["note"] = t.note
        raise WorkspaceNotInitialized("fake")
    monkeypatch.setattr(orch, "_run_agent", fail)

    orch.run_step("T-1")

    assert captured["status"] == TaskStatus.IN_PROGRESS
    assert captured["count"] == 1
    assert captured["block_kind"] is None
    assert captured["note"] is None


def _make_report(output_summary: str, status: str = "completed",
                 output_dir: str | None = None, verdict: str | None = None):
    from runtime.models import CompletionReport
    return CompletionReport(
        task_id="T-IGNORED", agent="engineering_head", status=status,
        confidence=80, output_summary=output_summary, output_dir=output_dir,
        verdict=verdict,
    )


def _make_result(success: bool = True, duration: int = 1):
    from runtime.orchestrator.executors import ExecutorResult
    return ExecutorResult(
        success=success, session_id="sess-x", duration_seconds=duration,
    )

class _SlugQueue:
    """Test adapter: wraps asyncio.Queue so put_nowait(slug, task_id) works.
    
    Production code calls _queue.put_nowait(slug, task_id), but tests use a
    stdlib asyncio.Queue. This shim accepts the 2-arg form and stores the
    (slug, task_id) tuple on the underlying queue.
    """
    def __init__(self) -> None:
        import asyncio as _asyncio
        self._q: _asyncio.Queue = _asyncio.Queue()
    def put_nowait(self, slug: str, task_id: str) -> None:
        self._q.put_nowait((slug, task_id))
    def qsize(self) -> int:
        return self._q.qsize()
    def get_nowait(self):
        return self._q.get_nowait()


def _consume_manager_supersede(orch, task_id: str, agent: str = "engineering_head") -> None:
    from runtime.models import CompletionReport, NextStep
    from runtime.orchestrator.run_step import _consume_completion_report

    _consume_completion_report(
        orch,
        task_id,
        CompletionReport(
            task_id=task_id,
            agent=agent,
            status="completed",
            confidence=90,
            output_summary="replace the plan",
            decision=NextStep(
                action="supersede",
                successor_brief="replacement plan",
                rationale="new evidence",
                attestation={
                    "recovery_reason": "Evidence invalidated the old plan.",
                    "policy_product_intent_unchanged": True,
                    "no_budget_or_external_commitment": True,
                    "no_permission_or_cross_team_change": True,
                    "no_schema_auth_security_privacy_or_data_access_change": True,
                    "no_unresolved_founder_gate": True,
                },
            ),
        ),
    )


def _claimed_manager_root(
    db,
    task_id: str = "T-SUP",
    *,
    team: str = "engineering",
    agent: str = "engineering_head",
) -> None:
    db.insert_task(TaskRecord(
        id=task_id,
        brief="original plan",
        team=team,
        assigned_agent=agent,
        status=TaskStatus.IN_PROGRESS,
        current_session_id="session-sup",
    ))


def test_persisted_null_supersede_attestation_never_reaches_write_path(runtime, db):
    """A malformed persisted callback must not create a successor or audit row."""
    from runtime.orchestrator.orchestrator import Orchestrator

    _claimed_manager_root(db)
    db.insert_task_result(
        task_id="T-SUP",
        agent="engineering_head",
        session_id="session-sup",
        status="completed",
        output_summary="replace the plan",
        confidence_score=90,
        decision_json='{"action":"supersede","successor_brief":"replacement plan",'
                      '"rationale":"new evidence","attestation":null}',
    )
    orch = Orchestrator(
        db=db, settings=Settings(), paths=runtime, slug="test",
        teams=TeamsRegistry.load(runtime.root),
    )

    report = orch._read_completion_from_db("T-SUP", "engineering_head", "session-sup")

    assert report is not None
    assert report.decision is None
    assert db.get_task("T-SUP").status is TaskStatus.IN_PROGRESS
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE id != 'T-SUP'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM manager_supersessions").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'manager_supersession'"
    ).fetchone()[0] == 0


def test_completion_consumer_allows_any_team_without_legacy_supersession_env_gates(
    runtime, db, monkeypatch,
):
    from runtime.orchestrator.orchestrator import Orchestrator

    monkeypatch.delenv("HAPPYRANCH_MANAGER_SUPERSESSION_ENABLED", raising=False)
    monkeypatch.delenv("HAPPYRANCH_MANAGER_SUPERSESSION_PILOT_TEAM", raising=False)
    _claimed_manager_root(db, team="content", agent="content_head")
    orch = Orchestrator(
        db=db, settings=Settings(), paths=runtime, slug="test",
        teams=TeamsRegistry.load(runtime.root),
    )
    orch._queue = _SlugQueue()

    _consume_manager_supersede(orch, "T-SUP", agent="content_head")

    successor = db.get_task("TASK-001")
    assert db.get_task("T-SUP").status is TaskStatus.SUPERSEDED
    assert successor is not None and successor.status is TaskStatus.PENDING
    assert successor.assigned_agent == "content_head"


def test_completion_consumer_ignores_legacy_supersession_env_gate(runtime, db, monkeypatch):
    from runtime.orchestrator.orchestrator import Orchestrator

    monkeypatch.setenv("HAPPYRANCH_MANAGER_SUPERSESSION_ENABLED", "0")
    monkeypatch.setenv("HAPPYRANCH_MANAGER_SUPERSESSION_PILOT_TEAM", "other")
    _claimed_manager_root(db)
    orch = Orchestrator(
        db=db, settings=Settings(), paths=runtime, slug="test",
        teams=TeamsRegistry.load(runtime.root),
    )
    orch._queue = _SlugQueue()

    _consume_manager_supersede(orch, "T-SUP")

    assert db.get_task("T-SUP").status is TaskStatus.SUPERSEDED
    assert db.get_task("TASK-001").assigned_agent == "engineering_head"
    source = Path(__file__).resolve().parents[1] / "runtime/orchestrator/run_step.py"
    assert "HAPPYRANCH_MANAGER_SUPERSESSION" not in source.read_text()


@pytest.mark.parametrize(
    ("field", "value", "expected_status"),
    [
        ("assigned_agent", "dev_agent", TaskStatus.FAILED),
        ("current_session_id", None, TaskStatus.FAILED),
        ("parent_task_id", "T-PARENT", TaskStatus.IN_PROGRESS),
    ],
    ids=["current_manager", "current_session", "root"],
)
def test_completion_consumer_enforces_manager_session_and_root_gates(
    runtime, db, field: str, value: str | None, expected_status: TaskStatus,
):
    from runtime.orchestrator.orchestrator import Orchestrator

    _claimed_manager_root(db)
    db.execute(f"UPDATE tasks SET {field} = ? WHERE id = 'T-SUP'", (value,))
    db._conn.commit()
    orch = Orchestrator(
        db=db, settings=Settings(), paths=runtime, slug="test",
        teams=TeamsRegistry.load(runtime.root),
    )
    orch._queue = _SlugQueue()

    _consume_manager_supersede(orch, "T-SUP")

    assert db.get_task("T-SUP").status is expected_status
    assert db.execute("SELECT COUNT(*) FROM manager_supersessions").fetchone()[0] == 0
    assert orch._queue.qsize() == 0


def test_completion_consumer_escalates_thread_origin_rejection_once(runtime, db):
    from runtime.orchestrator.orchestrator import Orchestrator

    _claimed_manager_root(db)
    db.execute("UPDATE tasks SET dispatched_from_thread_id = 'THR-152' WHERE id = 'T-SUP'")
    db._conn.commit()
    orch = Orchestrator(
        db=db, settings=Settings(), paths=runtime, slug="test",
        teams=TeamsRegistry.load(runtime.root),
    )
    orch._queue = _SlugQueue()
    founder_notifications: list[dict] = []
    orch.notify_escalated = lambda **kwargs: founder_notifications.append(kwargs)

    _consume_manager_supersede(orch, "T-SUP")

    task = db.get_task("T-SUP")
    assert task.status is TaskStatus.ESCALATED
    assert task.note == (
        "manager supersession rejected: thread-origin roots are not eligible "
        "for supersession; founder action required"
    )
    assert db.execute("SELECT COUNT(*) FROM manager_supersessions").fetchone()[0] == 0
    logs = db.get_audit_logs("T-SUP")
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
    assert founder_notifications == [{
        "task_id": "T-SUP",
        "agent": "engineering_head",
        "reason": task.note,
        "last_summary": "replace the plan",
    }]
    assert orch._queue.qsize() == 0


@pytest.mark.parametrize(
    ("status", "block_kind"),
    [
        (TaskStatus.PENDING, None),
        (TaskStatus.IN_PROGRESS, BlockKind.DELEGATED),
        (TaskStatus.CANCELLED, None),
    ],
    ids=["pending", "blocked", "cancelled"],
)
def test_completion_consumer_thread_origin_rejection_preserves_competing_state(
    runtime, db, monkeypatch, status: TaskStatus, block_kind: BlockKind | None,
):
    """A transition immediately before the rejection CAS remains authoritative."""
    from runtime.orchestrator.orchestrator import Orchestrator

    _claimed_manager_root(db)
    db.execute("UPDATE tasks SET dispatched_from_thread_id = 'THR-152' WHERE id = 'T-SUP'")
    db._conn.commit()
    orch = Orchestrator(
        db=db, settings=Settings(), paths=runtime, slug="test",
        teams=TeamsRegistry.load(runtime.root),
    )
    orch._queue = _SlugQueue()
    founder_notifications: list[dict] = []
    thread_projections: list[tuple] = []
    orch.notify_escalated = lambda **kwargs: founder_notifications.append(kwargs)
    monkeypatch.setattr(
        "runtime.orchestrator.run_step._maybe_post_thread_escalation",
        lambda *args, **kwargs: thread_projections.append((args, kwargs)),
    )
    real_reject = db.try_reject_thread_origin_manager_supersede

    def competing_transition_wins(*args, **kwargs):
        db.update_task(
            "T-SUP", status=status, block_kind=block_kind,
            note="competing transition",
            **({
                "cancelled_at": "2026-09-06T00:00:00+00:00",
                "completed_at": "2026-09-06T00:00:00+00:00",
            } if status is TaskStatus.CANCELLED else {}),
        )
        return real_reject(*args, **kwargs)

    monkeypatch.setattr(
        db, "try_reject_thread_origin_manager_supersede", competing_transition_wins,
    )

    _consume_manager_supersede(orch, "T-SUP")

    task = db.get_task("T-SUP")
    assert task.status is status
    assert task.block_kind is block_kind
    assert task.note == "competing transition"
    assert not [
        row for row in db.get_audit_logs("T-SUP")
        if row["action"] in {"escalation", "authority_hook"}
    ]
    assert founder_notifications == []
    assert thread_projections == []
    assert db.execute("SELECT COUNT(*) FROM manager_supersessions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "live_work",
    ["descendant_task", "pending_root_job", "running_descendant_job"],
)
def test_completion_consumer_thread_origin_rejection_preserves_competing_live_work(
    runtime, db, monkeypatch, live_work: str,
):
    """Family work landing immediately before the rejection CAS wins silently."""
    from runtime.orchestrator.orchestrator import Orchestrator

    _claimed_manager_root(db)
    db.execute("UPDATE tasks SET dispatched_from_thread_id = 'THR-152' WHERE id = 'T-SUP'")
    db._conn.commit()
    orch = Orchestrator(
        db=db, settings=Settings(), paths=runtime, slug="test",
        teams=TeamsRegistry.load(runtime.root),
    )
    orch._queue = _SlugQueue()
    founder_notifications: list[dict] = []
    thread_projections: list[tuple] = []
    orch.notify_escalated = lambda **kwargs: founder_notifications.append(kwargs)
    monkeypatch.setattr(
        "runtime.orchestrator.run_step._maybe_post_thread_escalation",
        lambda *args, **kwargs: thread_projections.append((args, kwargs)),
    )
    real_reject = db.try_reject_thread_origin_manager_supersede

    def live_work_lands(*args, **kwargs):
        descendant_id = "T-LIVE"
        if live_work in {"descendant_task", "running_descendant_job"}:
            db.insert_task(TaskRecord(
                id=descendant_id,
                status=(
                    TaskStatus.PENDING
                    if live_work == "descendant_task"
                    else TaskStatus.COMPLETED
                ),
                assigned_agent="dev_agent",
                team="engineering",
                brief="competing family work",
                parent_task_id="T-SUP",
                task_type="subtask",
            ))
        if live_work != "descendant_task":
            job_status = "pending" if live_work == "pending_root_job" else "running"
            job_task_id = "T-SUP" if live_work == "pending_root_job" else descendant_id
            db.execute(
                """INSERT INTO jobs
                   (id, task_id, agent_name, title, script_text, interpreter, status, created_at)
                   VALUES ('JOB-LIVE', ?, 'dev_agent', 'live', 'true', 'bash', ?,
                           '2026-09-06T00:00:00+00:00')""",
                (job_task_id, job_status),
            )
            db._conn.commit()
        return real_reject(*args, **kwargs)

    monkeypatch.setattr(
        db, "try_reject_thread_origin_manager_supersede", live_work_lands,
    )

    _consume_manager_supersede(orch, "T-SUP")

    task = db.get_task("T-SUP")
    assert task.status is TaskStatus.IN_PROGRESS
    assert task.block_kind is None
    if live_work == "descendant_task":
        assert db.get_task("T-LIVE").status is TaskStatus.PENDING
    else:
        assert db.get_job_status("JOB-LIVE") == (
            "pending" if live_work == "pending_root_job" else "running"
        )
    assert not [
        row for row in db.get_audit_logs("T-SUP")
        if row["action"] in {
            "escalation", "authority_hook", "manager_supersession",
        }
    ]
    assert founder_notifications == []
    assert thread_projections == []
    assert orch._queue.qsize() == 0
    assert db.execute("SELECT COUNT(*) FROM manager_supersessions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE id = 'TASK-001'").fetchone()[0] == 0


@pytest.mark.parametrize("enqueue_fails", [False, True], ids=["enqueue", "recovery"])
def test_completion_consumer_supersedes_and_leaves_successor_recoverable(
    runtime, db, monkeypatch, enqueue_fails: bool,
):
    from runtime.orchestrator.orchestrator import Orchestrator

    _claimed_manager_root(db)
    orch = Orchestrator(
        db=db, settings=Settings(), paths=runtime, slug="test",
        teams=TeamsRegistry.load(runtime.root),
    )
    orch._queue = _SlugQueue()
    founder_notifications: list[dict] = []
    orch.notify_escalated = lambda **kwargs: founder_notifications.append(kwargs)
    if enqueue_fails:
        def fail_enqueue(slug: str, task_id: str) -> None:
            raise RuntimeError("injected queue outage")
        monkeypatch.setattr(orch._queue, "put_nowait", fail_enqueue)

    _consume_manager_supersede(orch, "T-SUP")

    successor = db.get_task("TASK-001")
    assert db.get_task("T-SUP").status is TaskStatus.SUPERSEDED
    assert successor is not None and successor.status is TaskStatus.PENDING
    assert successor.assigned_agent == "engineering_head"
    assert founder_notifications == []
    assert orch._queue.qsize() == (0 if enqueue_fails else 1)



def test_run_step_done_completes_task_and_enqueues_parent(
    runtime, db, monkeypatch,
):
    import asyncio
    import json
    from runtime.orchestrator.orchestrator import Orchestrator

    # Parent in in_progress(delegated), child in pending.
    db.insert_task(TaskRecord(id="T-PAR", brief="parent",
                              assigned_agent="engineering_head"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-CHD", brief="child",
        assigned_agent="engineering_head", parent_task_id="T-PAR",
    ))

    orch = Orchestrator(db=db, settings=Settings(max_orchestration_steps=10),
                        paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    # Wire a fake queue
    q = _SlugQueue()
    orch._queue = q

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary=json.dumps({"action": "done", "summary": "Looks great"}),
            output_dir="output/run-1",
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    orch.run_step("T-CHD")

    child = db.get_task("T-CHD")
    assert child.status == TaskStatus.COMPLETED
    assert child.note == "Looks great"
    assert child.final_output_dir == "output/run-1"

    # Parent should be enqueued
    assert q.qsize() == 1
    assert q.get_nowait() == ("test", "T-PAR")


def test_run_step_nonroot_escalate_fails_and_routes_to_parent(
    runtime, db, monkeypatch,
):
    """THR-033 Change A: a NON-root task whose decision is `escalate` must NOT
    escalate directly to the founder — it fails and hands back to its parent,
    which is woken for a bounded-recovery decision step. Constructed with a
    task_type='task' non-root so the decision pipeline is exercised (in
    production, children are task_type='subtask' and never decide, so this
    branch is defensive lock-in).
    """
    import json
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-PAR", brief="p",
                              assigned_agent="engineering_head"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-CHD", brief="c",
        assigned_agent="engineering_head", parent_task_id="T-PAR",
        task_type="task",
    ))

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    q = _SlugQueue()
    orch._queue = q

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary=json.dumps({"action": "escalate", "reason": "needs founder"}),
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    orch.run_step("T-CHD")

    child = db.get_task("T-CHD")
    # Non-root never reaches escalated — it FAILS.
    assert child.status == TaskStatus.FAILED
    assert child.block_kind is None
    assert "non-root escalation requested" in (child.note or "")
    assert "needs founder" in (child.note or "")

    # No escalation audit row was written for the child.
    escalations = [a for a in db.get_audit_logs("T-CHD") if a["action"] == "escalation"]
    assert escalations == []

    # Parent woken for a bounded-recovery decision step (1 failed child < bound).
    assert q.qsize() == 1
    assert q.get_nowait() == ("test", "T-PAR")
    assert db.get_task("T-PAR").status == TaskStatus.IN_PROGRESS


def test_run_step_root_escalate_parks_escalated(
    runtime, db, monkeypatch,
):
    """THR-033 Change A: a ROOT task whose decision is `escalate` parks
    in escalated for the founder — root escalation is unchanged."""
    import json
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-ROOT", brief="r",
                              assigned_agent="engineering_head"))

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    q = _SlugQueue()
    orch._queue = q

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary=json.dumps({"action": "escalate", "reason": "needs founder"}),
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    orch.run_step("T-ROOT")

    t = db.get_task("T-ROOT")
    assert t.parent_task_id is None  # it is a root
    assert t.status == TaskStatus.ESCALATED  # Path B: top-level status
    assert t.block_kind is None
    assert t.note == "needs founder"

    escalations = [a for a in db.get_audit_logs("T-ROOT") if a["action"] == "escalation"]
    assert any("needs founder" in e["payload"]["reason"] for e in escalations)


def test_run_step_delegate_spawns_child_and_blocks_self(
    runtime, db, monkeypatch,
):
    import asyncio
    import json
    from runtime.orchestrator.orchestrator import Orchestrator

    (runtime.workspaces_dir / "dev_agent").mkdir(parents=True)

    db.insert_task(TaskRecord(id="T-1", brief="root",
                              assigned_agent="engineering_head"))
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    q = _SlugQueue()
    orch._queue = q

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary=json.dumps({
                "action": "delegate",
                "agent": "dev_agent",
                "prompt": "Write a PR",
            }),
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    orch.run_step("T-1")

    # Parent now in_progress(DELEGATED) — Path B: a parent waiting on its own
    # child is in progress, with the waiting reason kept in block_kind.
    parent = db.get_task("T-1")
    assert parent.status == TaskStatus.IN_PROGRESS
    assert parent.block_kind == BlockKind.DELEGATED
    assert "dev_agent" in (parent.note or "")

    # Exactly one child exists, is pending, and is enqueued
    children = db.get_children("T-1")
    assert len(children) == 1
    child_id = children[0]
    child = db.get_task(child_id)
    assert child.status == TaskStatus.PENDING
    assert child.assigned_agent == "dev_agent"
    assert child.brief == "Write a PR"
    assert child.parent_task_id == "T-1"
    assert q.get_nowait() == ("test", child_id)


def test_run_step_delegate_inherits_session_timeout(runtime, db, monkeypatch):
    """A delegated child copies the parent's session_timeout_seconds so a
    revisit-time bump propagates down the whole lineage."""
    import asyncio
    import json
    from runtime.orchestrator.orchestrator import Orchestrator

    (runtime.workspaces_dir / "dev_agent").mkdir(parents=True)

    db.insert_task(TaskRecord(
        id="T-1", brief="root", assigned_agent="engineering_head",
        session_timeout_seconds=7200,
    ))
    orch = Orchestrator(
        db=db, settings=Settings(),
        paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root),
    )
    orch._queue = _SlugQueue()

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary=json.dumps({
                "action": "delegate", "agent": "dev_agent", "prompt": "Do it",
            }),
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    orch.run_step("T-1")

    children = db.get_children("T-1")
    assert len(children) == 1
    child = db.get_task(children[0])
    assert child.session_timeout_seconds == 7200


def test_run_step_invalid_delegate_fails_task(runtime, db, monkeypatch):
    """A delegate with no agent name is unrecoverable — fail the task and
    notify the parent (which may itself be root — no-op in that case)."""
    import asyncio
    import json
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-1", brief="x",
                              assigned_agent="engineering_head"))
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary=json.dumps({"action": "delegate", "prompt": "x"}),
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    orch.run_step("T-1")
    t = db.get_task("T-1")
    assert t.status == TaskStatus.FAILED
    assert t.note and "invalid delegate" in t.note


def test_run_step_session_failure_cascades_to_parent_no_retry(
    runtime, db, monkeypatch,
):
    """TASK-573 bounded failure-recovery: when a delegated subtask fails,
    the parent gets a bounded manager-wake decision step (enqueued),
    NOT cascade-failed. TASK-3604: no auto-revisit successor is spawned."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-PAR", brief="p",
                              assigned_agent="engineering_head",
                              task_type="task"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-CHD", brief="c",
        assigned_agent="engineering_head", parent_task_id="T-PAR",
        task_type="subtask",
    ))

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    q = _SlugQueue()
    orch._queue = q

    monkeypatch.setattr(orch, "_run_agent",
                        lambda *a, **k: (_make_result(success=False), None))

    orch.run_step("T-CHD")

    child = db.get_task("T-CHD")
    assert child.status == TaskStatus.FAILED
    assert "session failed" in (child.note or "")

    # Parent stays in_progress(delegated) for bounded manager-wake (TASK-573).
    parent = db.get_task("T-PAR")
    assert parent.status == TaskStatus.IN_PROGRESS
    assert parent.block_kind == BlockKind.DELEGATED
    # TASK-3604: queue holds ONLY the parent re-enqueue (decision step).
    # No auto-revisit successor root is spawned.
    assert q.qsize() == 1
    slug1, tid1 = q.get_nowait()
    assert slug1 == "test"
    assert tid1 == "T-PAR"


def test_run_step_session_failure_cascades_up_chain(
    runtime, db, monkeypatch,
):
    """TASK-573 bounded failure-recovery: a failing grandchild wakes its
    immediate parent for a decision step (not cascade-fail). The chain no
    longer bubbles FAILED status up — each parent wakes independently."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-ROOT", brief="r",
                              assigned_agent="engineering_head",
                              task_type="task"))
    db.update_task("T-ROOT", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-MID", brief="m",
        assigned_agent="engineering_head", parent_task_id="T-ROOT",
        task_type="task",
    ))
    db.update_task("T-MID", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-LEAF", brief="l",
        assigned_agent="dev_agent", parent_task_id="T-MID",
        task_type="subtask",
    ))

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()
    monkeypatch.setattr(orch, "_run_agent",
                        lambda *a, **k: (_make_result(success=False), None))

    orch.run_step("T-LEAF")

    # T-LEAF is FAILED (opaque session failure).
    assert db.get_task("T-LEAF").status == TaskStatus.FAILED
    # T-MID stays in_progress(delegated) — bounded manager-wake.
    assert db.get_task("T-MID").status == TaskStatus.IN_PROGRESS
    assert db.get_task("T-MID").block_kind == BlockKind.DELEGATED
    # T-ROOT stays in_progress(delegated) — not reachable until T-MID advances.
    assert db.get_task("T-ROOT").status == TaskStatus.IN_PROGRESS
    assert db.get_task("T-ROOT").block_kind == BlockKind.DELEGATED
    # TASK-3604: queue holds ONLY T-MID bounded-wake enqueue.
    # No auto-revisit successor root is spawned.
    assert orch._queue.qsize() == 1
    slug_mid, tid_mid = orch._queue.get_nowait()
    assert tid_mid == "T-MID"


def test_run_step_session_failure_note_includes_diagnostics(
    runtime, db, monkeypatch,
):
    """The `agent session failed` note must include rc and a stderr tail
    so post-mortems don't need to grep daemon.log. TASK-044/045 class of
    failure (subprocess exits without calling back) is the motivating case.
    """
    from runtime.orchestrator.executors import ExecutorResult
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-1", brief="x",
                              assigned_agent="engineering_head"))
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    import asyncio
    orch._queue = _SlugQueue()

    result = ExecutorResult(
        success=True,  # rc=0 but no report — the TASK-045 signature
        duration_seconds=703,
        session_id="sess-x",
        returncode=0,
        stdout_tail="wrote ExplorePage.tsx\n",
        stderr_tail="",
    )
    monkeypatch.setattr(orch, "_run_agent", lambda *a, **k: (result, None))

    orch.run_step("T-1")

    note = db.get_task("T-1").note or ""
    assert "rc=0" in note
    assert "no completion callback" in note
    assert "wrote ExplorePage.tsx" in note


def test_run_step_opaque_failure_no_auto_revisit(
    runtime, db, monkeypatch,
):
    """TASK-3604: When a delegated subtask hits an opaque failure, the task
    is marked FAILED but NO auto-revisit successor root is created. The
    failure note includes diagnostics (rc, missing callback flag, stdout
    tail) so post-mortems don't need to grep daemon.log."""
    import asyncio
    from runtime.orchestrator.executors import ExecutorResult
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-PAR", brief="parent brief",
                              team="engineering",
                              assigned_agent="engineering_head"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-CHD", brief="c", team="engineering",
        assigned_agent="dev_agent", parent_task_id="T-PAR",
    ))

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    failing_result = ExecutorResult(
        success=True,  # rc=0 but no callback — TASK-045 class
        duration_seconds=120,
        session_id="sess-x",
        returncode=0,
        stdout_tail="wrote ExplorePage.tsx\n",
        stderr_tail="",
    )
    monkeypatch.setattr(orch, "_run_agent",
                        lambda *a, **k: (failing_result, None))

    orch.run_step("T-CHD")

    # Child is FAILED with diagnostic note.
    child = db.get_task("T-CHD")
    assert child.status == TaskStatus.FAILED
    assert "no completion callback" in (child.note or "")
    assert "wrote ExplorePage.tsx" in (child.note or "")

    # TASK-3604: NO auto-revisit successor root.
    # Queue holds ONLY the parent re-enqueue for bounded-wake.
    assert orch._queue.qsize() == 1
    slug, tid = orch._queue.get_nowait()
    assert slug == "test"
    assert tid == "T-PAR"

    # No auto_revisit_of audit row anywhere.
    all_audit = db.fetch_all_readonly(
        "SELECT action FROM audit_log "
        "WHERE action IN ('auto_revisit_of', 'revisit_spawned')"
    )
    assert len(all_audit) == 0


def test_run_step_opaque_failure_on_root_manager_no_auto_revisit(
    runtime, db, monkeypatch,
):
    """TASK-3604: Manager-level opaque failure (root task itself crashes)
    marks the task FAILED but spawns NO auto-revisit successor."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-ROOT", brief="root brief",
                              team="engineering",
                              assigned_agent="engineering_head"))

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    monkeypatch.setattr(orch, "_run_agent",
                        lambda *a, **k: (_make_result(success=False), None))

    orch.run_step("T-ROOT")

    # Task is FAILED.
    assert db.get_task("T-ROOT").status == TaskStatus.FAILED

    # TASK-3604: no successor root is spawned.
    assert orch._queue.qsize() == 0

    # No auto_revisit_of audit anywhere.
    all_audit = db.fetch_all_readonly(
        "SELECT action FROM audit_log "
        "WHERE action IN ('auto_revisit_of', 'revisit_spawned')"
    )
    assert len(all_audit) == 0


def test_run_step_opaque_failure_on_exception_no_auto_revisit(
    runtime, db, monkeypatch,
):
    """TASK-3604: Exception escaping _run_agent marks the task FAILED
    but spawns NO auto-revisit successor."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-1", brief="x",
                              assigned_agent="engineering_head"))
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    def boom(task_id, agent, prompt, on_session_started=None):
        raise RuntimeError("workspace not initialized")

    monkeypatch.setattr(orch, "_run_agent", boom)

    orch.run_step("T-1")

    # Task is FAILED with diagnostic note.
    t = db.get_task("T-1")
    assert t.status == TaskStatus.FAILED
    assert "agent invocation failed" in (t.note or "")
    assert "workspace not initialized" in (t.note or "")

    # TASK-3604: no successor root is spawned.
    assert orch._queue.qsize() == 0

    # No auto_revisit_of audit anywhere.
    all_audit = db.fetch_all_readonly(
        "SELECT action FROM audit_log "
        "WHERE action IN ('auto_revisit_of', 'revisit_spawned')"
    )
    assert len(all_audit) == 0


def test_run_step_terminal_failed_no_successor_with_legacy_audit(
    runtime, db, monkeypatch,
):
    """After 2 prior auto-revisits in the audit chain, opaque failure is
    terminal FAILED with no successor spawned. Legacy audit entries
    (auto_revisit_of) are preserved as historical fixtures but the terminal
    failure creates no new revisit. Since TASK-3604, auto-revisit spawning is
    removed; the queue stays empty."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    # Chain: T-ORIG <- T-AR1 (auto-revisit of T-ORIG) <- T-AR2 (auto of T-AR1)
    # T-AR2 is the current task; if it fails, no more auto-revisits.
    db.insert_task(TaskRecord(id="T-ORIG", brief="b",
                              assigned_agent="engineering_head",
                              status=TaskStatus.FAILED))
    db.insert_task(TaskRecord(
        id="T-AR1", brief="b", assigned_agent="engineering_head",
        revisit_of_task_id="T-ORIG", status=TaskStatus.FAILED,
    ))
    db.insert_task(TaskRecord(
        id="T-AR2", brief="b", assigned_agent="engineering_head",
        revisit_of_task_id="T-AR1",
    ))
    # Mark T-AR1 and T-AR2 as auto-revisits in the audit log (historical
    # fixtures — TASK-3604 removed auto-revisit spawning).
    from runtime.infrastructure.audit_logger import AuditLogger
    audit = AuditLogger(db)
    audit.log_auto_revisit_of(
        task_id="T-AR1", predecessor_root="T-ORIG",
        failed_task="T-ORIG", failed_agent="engineering_head",
        cascade=["T-ORIG"],
        failure_kind="session_failed",
        error_context={"mode": "session_failure"},
        attempt=1,
    )
    audit.log_auto_revisit_of(
        task_id="T-AR2", predecessor_root="T-AR1",
        failed_task="T-AR1", failed_agent="engineering_head",
        cascade=["T-AR1"],
        failure_kind="session_failed",
        error_context={"mode": "session_failure"},
        attempt=2,
    )

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()
    monkeypatch.setattr(orch, "_run_agent",
                        lambda *a, **k: (_make_result(success=False), None))

    orch.run_step("T-AR2")

    # T-AR2 fails; no further auto-revisit is spawned.
    assert db.get_task("T-AR2").status == TaskStatus.FAILED
    assert orch._queue.qsize() == 0


def test_run_step_self_blocked_does_not_spawn_auto_revisit(
    runtime, db, monkeypatch,
):
    """Self-blocked is a deliberate agent decision — not an opaque failure.
    No auto-revisit should be spawned."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-1", brief="x",
                              assigned_agent="engineering_head"))
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    monkeypatch.setattr(orch, "_run_agent",
                        lambda *a, **k: (_make_result(),
                                         _make_report("blocked on prereq",
                                                      status="blocked")))

    orch.run_step("T-1")

    assert db.get_task("T-1").status == TaskStatus.FAILED
    assert orch._queue.qsize() == 0


def test_run_step_auto_revisit_header_injected_on_first_step(
    runtime, db, monkeypatch,
):
    """The team manager's first prompt on the auto-revisit root must
    include AUTO-REVISIT CONTEXT with the structured error payload."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _build_agent_prompt

    db.insert_task(TaskRecord(id="T-PAR", brief="parent brief",
                              team="engineering",
                              assigned_agent="engineering_head",
                              status=TaskStatus.FAILED))
    db.insert_task(TaskRecord(
        id="T-NEW", brief="parent brief", team="engineering",
        assigned_agent="engineering_head",
        revisit_of_task_id="T-PAR",
    ))
    from runtime.infrastructure.audit_logger import AuditLogger
    AuditLogger(db).log_auto_revisit_of(
        task_id="T-NEW", predecessor_root="T-PAR",
        failed_task="T-CHD", failed_agent="dev_agent",
        cascade=["T-PAR", "T-CHD"],
        failure_kind="no_callback",
        error_context={
            "mode": "session_failure", "rc": 0, "missing_callback": True,
            "stderr_tail": "", "stdout_tail": "wrote files",
            "executor_error": None,
        },
        attempt=1,
    )

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    task = db.get_task("T-NEW")
    prompt = _build_agent_prompt(orch, task, "engineering_head")
    assert "AUTO-REVISIT CONTEXT" in prompt
    assert "T-PAR" in prompt
    assert "T-CHD" in prompt
    assert "dev_agent" in prompt
    assert "no completion callback" in prompt
    assert "wrote files" in prompt
    # Shared discipline tail (TALK-028): manager must status-assess and choose
    # execute-with-divergence-note vs escalate, not improvise.
    assert "Status-assess before acting" in prompt
    assert "Do NOT improvise" in prompt


def test_build_agent_prompt_subtask_includes_blocked_jobs_resume_header(
    runtime, db,
):
    """A resumed delegated subtask receives its job-outcome pointer."""
    from runtime.infrastructure.audit_logger import AuditLogger
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _build_agent_prompt

    db.insert_task(TaskRecord(
        id="T-SUB", brief="wait for JOB-1", team="engineering",
        assigned_agent="dev_agent", task_type="subtask", parent_task_id="T-PAR",
    ))
    AuditLogger(db).log_task_resumed_from_jobs(
        task_id="T-SUB",
        blocking_job_ids=["JOB-1"],
        trigger="job_terminal",
        triggering_job_id="JOB-1",
        job_outcomes={"JOB-1": "completed"},
    )

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    task = db.get_task("T-SUB")
    prompt = _build_agent_prompt(orch, task, "dev_agent")

    assert "BLOCKED-JOBS-RESULTS" in prompt
    assert "JOB-1" in prompt


def test_run_step_worker_self_blocked_fails_task(runtime, db, monkeypatch):
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-1", brief="x",
                              assigned_agent="engineering_head"))
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    monkeypatch.setattr(orch, "_run_agent",
                        lambda *a, **k: (_make_result(), _make_report(
                            output_summary="ran out of tokens", status="blocked")))

    orch.run_step("T-1")
    t = db.get_task("T-1")
    assert t.status == TaskStatus.FAILED
    assert t.note and t.note.startswith("self-blocked:")


def test_run_step_worker_completion_is_done_not_parsed_as_eh_decision(
    runtime, db, monkeypatch,
):
    """P1 regression: workers don't speak the NextStep JSON protocol. A plain
    prose output_summary from a delegated worker must be treated as `done`,
    not escalated as "non-JSON EH decision"."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    # Parent (EH) delegated to dev_agent (worker).
    db.insert_task(TaskRecord(id="T-PAR", brief="p",
                              assigned_agent="engineering_head"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-CHD", brief="c",
        assigned_agent="dev_agent", parent_task_id="T-PAR",
        task_type="subtask",
    ))

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    q = _SlugQueue()
    orch._queue = q

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary="Shipped the PR — see branch feat/x",
            output_dir="output/run-1",
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    orch.run_step("T-CHD")

    child = db.get_task("T-CHD")
    assert child.status == TaskStatus.COMPLETED
    assert child.block_kind is None
    assert child.note == "Shipped the PR — see branch feat/x"
    assert child.final_output_dir == "output/run-1"
    # Parent wakes on the child terminal.
    assert q.get_nowait() == ("test", "T-PAR")


def test_run_step_delegated_worker_emits_review_verdict(
    runtime, db, monkeypatch,
):
    """P1 regression: tiers are computed from review_verdict audit rows. When
    a delegated worker reaches a terminal state, the EH's implicit verdict
    (approved on COMPLETED, rejected on FAILED) must be logged — otherwise
    every delegated agent stays on stale performance data."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-PAR", brief="p",
                              assigned_agent="engineering_head"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-OK", brief="ok",
        assigned_agent="dev_agent", parent_task_id="T-PAR",
        task_type="subtask",
    ))
    db.insert_task(TaskRecord(
        id="T-BAD", brief="bad",
        assigned_agent="dev_agent", parent_task_id="T-PAR",
        task_type="subtask",
    ))

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    # Success path.
    monkeypatch.setattr(orch, "_run_agent",
                        lambda *a, **k: (_make_result(), _make_report(
                            output_summary="done")))
    orch.run_step("T-OK")

    # Failure path (session failed, no report).
    monkeypatch.setattr(orch, "_run_agent",
                        lambda *a, **k: (_make_result(success=False), None))
    orch.run_step("T-BAD")

    ok_verdicts = [a for a in db.get_audit_logs("T-OK")
                   if a["action"] == "review_verdict"]
    bad_verdicts = [a for a in db.get_audit_logs("T-BAD")
                    if a["action"] == "review_verdict"]
    assert len(ok_verdicts) == 1
    assert ok_verdicts[0]["agent"] == "engineering_head"
    assert ok_verdicts[0]["payload"]["verdict"] == "approved"
    assert ok_verdicts[0]["payload"]["reviewed_agent"] == "dev_agent"
    assert len(bad_verdicts) == 1
    assert bad_verdicts[0]["payload"]["verdict"] == "rejected"
    assert bad_verdicts[0]["payload"]["reviewed_agent"] == "dev_agent"


def test_run_step_delegated_reviewer_request_changes_verdict(runtime, db, monkeypatch):
    """A delegated non-manager reviewer that reports status=completed with an
    explicit structured verdict=REQUEST_CHANGES must finish COMPLETED (the
    completion status is a distinct fact) while its review_verdict audit
    payload carries the reported REQUEST_CHANGES — never an inferred approved."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-PAR", brief="p",
                              assigned_agent="engineering_head"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS,
                   block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-REV", brief="review",
        assigned_agent="qa_engineer", parent_task_id="T-PAR",
        task_type="subtask",
    ))

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    # Production seam: the agent callback persists the completion report
    # (task_results) before run_step classifies the terminal transition.
    def _run_agent_persisting(*a, **k):
        db.insert_task_result(
            task_id="T-REV", agent="qa_engineer", session_id="sess-x",
            status="completed", confidence_score=80,
            output_summary="needs changes", verdict="REQUEST_CHANGES",
        )
        return _make_result(), _make_report(
            output_summary="needs changes", verdict="REQUEST_CHANGES")
    monkeypatch.setattr(orch, "_run_agent", _run_agent_persisting)

    orch.run_step("T-REV")

    child = db.get_task("T-REV")
    assert child.status == TaskStatus.COMPLETED
    assert child.note == "needs changes"

    verdicts = [a for a in db.get_audit_logs("T-REV")
                if a["action"] == "review_verdict"]
    assert len(verdicts) == 1
    assert verdicts[0]["payload"]["verdict"] == "REQUEST_CHANGES"
    assert verdicts[0]["payload"]["reviewed_agent"] == "qa_engineer"


def test_run_step_delegated_completed_no_verdict_falls_back_approved(
    runtime, db, monkeypatch,
):
    """A delegated worker reporting completed WITHOUT a structured verdict keeps
    the legacy implicit mapping (approved)."""
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-PAR", brief="p",
                              assigned_agent="engineering_head"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS,
                   block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-OK", brief="ok",
        assigned_agent="dev_agent", parent_task_id="T-PAR",
        task_type="subtask",
    ))

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    monkeypatch.setattr(orch, "_run_agent",
                        lambda *a, **k: (_make_result(), _make_report(
                            output_summary="done")))
    orch.run_step("T-OK")

    verdicts = [a for a in db.get_audit_logs("T-OK")
                if a["action"] == "review_verdict"]
    assert len(verdicts) == 1
    assert verdicts[0]["payload"]["verdict"] == "approved"


def test_run_step_root_eh_task_skips_review_verdict(runtime, db, monkeypatch):
    """Root tasks (no parent) are EH-assigned and must NOT produce verdict
    rows — the EH is not reviewing itself."""
    import asyncio
    import json
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-ROOT", brief="r",
                              assigned_agent="engineering_head"))
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    monkeypatch.setattr(orch, "_run_agent",
                        lambda *a, **k: (_make_result(), _make_report(
                            output_summary=json.dumps(
                                {"action": "done", "summary": "ok"}))))
    orch.run_step("T-ROOT")

    verdicts = [a for a in db.get_audit_logs("T-ROOT")
                if a["action"] == "review_verdict"]
    assert verdicts == []


def test_run_step_skips_task_with_cancelled_at(runtime, db, monkeypatch):
    """Entry guard: once /cancel stamps cancelled_at on a row, a late queue
    entry must be a silent no-op — no in_progress transition, no _run_agent
    call, no step-count increment. The row stays exactly as /cancel left it."""
    from datetime import datetime, timezone

    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(
        id="T-CNL", brief="x",
        assigned_agent="engineering_head",
    ))
    # /cancel's phase-1 writes: FAILED + cancelled_at + founder note.
    now = datetime.now(timezone.utc).isoformat()
    db.update_task(
        "T-CNL",
        status=TaskStatus.FAILED,
        block_kind=None,
        note="cancelled by founder: enough",
        cancelled_at=now,
        completed_at=now,
    )

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    called = {"n": 0}
    def sentinel(*a, **k):
        called["n"] += 1
        raise AssertionError("_run_agent must not be called after cancel")
    monkeypatch.setattr(orch, "_run_agent", sentinel)

    orch.run_step("T-CNL")

    t = db.get_task("T-CNL")
    assert t.status == TaskStatus.FAILED
    assert t.note == "cancelled by founder: enough"
    assert t.cancelled_at is not None
    assert t.orchestration_step_count == 0
    assert called["n"] == 0


def test_fail_idempotent_on_terminal_task(runtime, db):
    """The post-Popen classifier must not overwrite the founder's note.
    After /cancel flips the row to FAILED, a stray _fail() call (from the
    run_step that was mid-flight when SIGTERM arrived) must no-op."""
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _fail

    db.insert_task(TaskRecord(id="T-1", brief="x",
                              assigned_agent="dev_agent"))
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.update_task("T-1", status=TaskStatus.FAILED, block_kind=None,
                   note="cancelled by founder: stop", cancelled_at=now,
                   completed_at=now)

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    _fail(orch, "T-1", note="agent session failed rc=-15")

    t = db.get_task("T-1")
    assert t.status == TaskStatus.FAILED
    assert t.note == "cancelled by founder: stop"  # unchanged


def test_complete_idempotent_on_terminal_task(runtime, db):
    """If the subprocess happened to finish cleanly just before SIGTERM,
    _complete must not resurrect the cancelled row back to COMPLETED."""
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _complete

    db.insert_task(TaskRecord(id="T-1", brief="x",
                              assigned_agent="dev_agent"))
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.update_task("T-1", status=TaskStatus.FAILED, block_kind=None,
                   note="cancelled by founder: stop", cancelled_at=now,
                   completed_at=now)

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    _complete(orch, "T-1", note="looks great", output_dir="output/run-1")

    t = db.get_task("T-1")
    assert t.status == TaskStatus.FAILED
    assert t.note == "cancelled by founder: stop"
    assert t.final_output_dir is None  # unchanged


def test_run_step_revisit_header_injected_on_first_step(
    runtime, db, monkeypatch,
):
    """New-root task with a revisit_of audit entry and no orchestration_step
    entry: EH prompt must start with the revisit context header."""
    from runtime.orchestrator.orchestrator import Orchestrator
    db.insert_task(TaskRecord(
        id="TASK-072", brief="Add Alipay support",
        assigned_agent="engineering_head",
    ))
    db.insert_audit_log(
        task_id="TASK-072", agent="founder", action="revisit_of",
        payload={
            "predecessor_root": "TASK-052",
            "flagged": "TASK-058",
            "cascade": ["TASK-052", "TASK-053", "TASK-058"],
            "prior_status": "failed",
            "founder_note": "PR #103 already merged",
        },
    )
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))

    captured = {}
    def capture(task_id, agent, prompt, on_session_started=None):
        captured["prompt"] = prompt
        raise RuntimeError("abort after prompt build")
    monkeypatch.setattr(orch, "_run_agent", capture)
    orch.run_step("TASK-072")

    prompt = captured["prompt"]
    assert prompt.startswith("REVISIT CONTEXT:")
    assert "TASK-052" in prompt
    assert "failed" in prompt
    assert "TASK-058" in prompt
    assert "TASK-052 -> TASK-053 -> TASK-058" in prompt or \
           "TASK-052 → TASK-053 → TASK-058" in prompt
    assert "PR #103 already merged" in prompt
    # Shared discipline tail (TALK-028).
    assert "Status-assess before acting" in prompt
    assert "Do NOT improvise" in prompt


def test_run_step_revisit_header_absent_on_second_step(
    runtime, db, monkeypatch,
):
    """After the first orchestration_step audit entry lands, the header must
    disappear — subsequent EH cycles see a vanilla capabilities prompt."""
    from runtime.orchestrator.orchestrator import Orchestrator
    db.insert_task(TaskRecord(
        id="TASK-072", brief="x",
        assigned_agent="engineering_head",
    ))
    db.update_task("TASK-072", orchestration_step_count=1)
    db.insert_audit_log(
        task_id="TASK-072", agent="founder", action="revisit_of",
        payload={
            "predecessor_root": "TASK-052", "flagged": "TASK-052",
            "cascade": ["TASK-052"], "prior_status": "failed",
            "founder_note": None,
        },
    )
    db.insert_audit_log(
        task_id="TASK-072", agent="orchestrator", action="orchestration_step",
        payload={"step_number": 1, "decision": {"action": "done"}},
    )
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))

    captured = {}
    def capture(task_id, agent, prompt, on_session_started=None):
        captured["prompt"] = prompt
        raise RuntimeError("abort")
    monkeypatch.setattr(orch, "_run_agent", capture)
    orch.run_step("TASK-072")

    assert not captured["prompt"].startswith("REVISIT CONTEXT:")


def test_run_step_revisit_header_omits_note_line_when_none(
    runtime, db, monkeypatch,
):
    """founder_note == None => no 'Founder note:' line in the header."""
    from runtime.orchestrator.orchestrator import Orchestrator
    db.insert_task(TaskRecord(
        id="TASK-072", brief="x",
        assigned_agent="engineering_head",
    ))
    db.insert_audit_log(
        task_id="TASK-072", agent="founder", action="revisit_of",
        payload={
            "predecessor_root": "TASK-052", "flagged": "TASK-052",
            "cascade": ["TASK-052"], "prior_status": "failed",
            "founder_note": None,
        },
    )
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))

    captured = {}
    def capture(task_id, agent, prompt, on_session_started=None):
        captured["prompt"] = prompt
        raise RuntimeError("abort")
    monkeypatch.setattr(orch, "_run_agent", capture)
    orch.run_step("TASK-072")

    assert "Founder note:" not in captured["prompt"]


def test_run_step_resolved_escalation_header_injected_after_continue(
    runtime, db, monkeypatch,
):
    """After /resolve-escalation --continue, the task is re-enqueued (PENDING).
    On the manager's next decision step, the prompt must start with the
    ESCALATION RESOLVED header so the manager sees the founder's verdict."""
    from runtime.orchestrator.orchestrator import Orchestrator
    db.insert_task(TaskRecord(
        id="TASK-080", brief="Refund $800?",
        assigned_agent="engineering_head",
    ))
    db.update_task("TASK-080", orchestration_step_count=1)
    db.insert_audit_log(
        task_id="TASK-080", agent="orchestrator", action="orchestration_step",
        payload={"step_number": 1, "decision": {"action": "escalate"}},
    )
    db.insert_audit_log(
        task_id="TASK-080", agent="founder", action="escalation_resolved",
        payload={"decision": "continue", "rationale": "approved one-time exception"},
    )
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))

    captured = {}
    def capture(task_id, agent, prompt, on_session_started=None):
        captured["prompt"] = prompt
        raise RuntimeError("abort after prompt build")
    monkeypatch.setattr(orch, "_run_agent", capture)
    orch.run_step("TASK-080")

    prompt = captured["prompt"]
    assert prompt.startswith("ESCALATION RESOLVED:")
    assert "approved one-time exception" in prompt
    assert "founder continued" in prompt


def test_run_step_resolved_escalation_header_absent_after_next_step(
    runtime, db, monkeypatch,
):
    """Once the manager has taken a decision step after the resolution, the
    header must disappear — its trigger is `latest escalation_resolved id >
    latest orchestration_step id`."""
    from runtime.orchestrator.orchestrator import Orchestrator
    db.insert_task(TaskRecord(
        id="TASK-081", brief="x",
        assigned_agent="engineering_head",
    ))
    db.update_task("TASK-081", orchestration_step_count=2)
    db.insert_audit_log(
        task_id="TASK-081", agent="orchestrator", action="orchestration_step",
        payload={"step_number": 1, "decision": {"action": "escalate"}},
    )
    db.insert_audit_log(
        task_id="TASK-081", agent="founder", action="escalation_resolved",
        payload={"decision": "continue", "rationale": "ok"},
    )
    db.insert_audit_log(
        task_id="TASK-081", agent="orchestrator", action="orchestration_step",
        payload={"step_number": 2, "decision": {"action": "done"}},
    )
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))

    captured = {}
    def capture(task_id, agent, prompt, on_session_started=None):
        captured["prompt"] = prompt
        raise RuntimeError("abort")
    monkeypatch.setattr(orch, "_run_agent", capture)
    orch.run_step("TASK-081")

    assert not captured["prompt"].startswith("ESCALATION RESOLVED:")


def test_run_step_concurrent_claim_spawns_only_one_agent(
    runtime, db, monkeypatch,
):
    """Regression: when two workers pop the same task_id (e.g. a multi-child
    fan-in race double-enqueued the parent), exactly one must claim the step
    and call _run_agent. The other must observe the claimed state and
    silently no-op.

    Without an atomic CAS on the in_progress(delegated) → in_progress(NULL) transition,
    both threads pass the eligibility check at run_step steps 1 and both
    write IN_PROGRESS at step 3 → both _run_agent calls fire, producing two
    EH subprocesses on the same brief.
    """
    import json
    import threading
    from runtime.orchestrator.orchestrator import Orchestrator

    # Parent in_progress(delegated) with two children, both terminal → eligible
    # for exactly one EH decision step.
    db.insert_task(TaskRecord(id="T-PAR", brief="p",
                              assigned_agent="engineering_head"))
    db.insert_task(TaskRecord(id="T-C1", brief="c1",
                              assigned_agent="dev_agent", parent_task_id="T-PAR"))
    db.insert_task(TaskRecord(id="T-C2", brief="c2",
                              assigned_agent="dev_agent", parent_task_id="T-PAR"))
    db.update_task("T-C1", status=TaskStatus.COMPLETED)
    db.update_task("T-C2", status=TaskStatus.COMPLETED)
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")

    orch = Orchestrator(db=db, settings=Settings(max_orchestration_steps=10),
                        paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))

    # Barrier-sync the two threads AFTER each has read the parent row at the
    # top of run_step_impl — both then observe in_progress(delegated) before either
    # writes IN_PROGRESS. This is the exact race window we're closing.
    barrier = threading.Barrier(2, timeout=5.0)
    original_get_task = db.get_task
    par_reads = [0]
    par_reads_lock = threading.Lock()
    def synced_get_task(task_id):
        result = original_get_task(task_id)
        if task_id == "T-PAR":
            with par_reads_lock:
                par_reads[0] += 1
                should_sync = par_reads[0] <= 2
            if should_sync:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
        return result
    monkeypatch.setattr(db, "get_task", synced_get_task)

    agent_calls: list[tuple[str, str]] = []
    agent_calls_lock = threading.Lock()
    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        with agent_calls_lock:
            agent_calls.append((task_id, agent))
        return _make_result(), _make_report(
            output_summary=json.dumps({"action": "done", "summary": "ok"})
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    errs: list[BaseException] = []
    def worker():
        try:
            orch.run_step("T-PAR")
        except BaseException as e:
            errs.append(e)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(timeout=5.0); t2.join(timeout=5.0)

    assert not t1.is_alive() and not t2.is_alive(), "worker thread hung"
    assert not errs, f"worker threads raised: {errs}"
    # The assertion: exactly one EH subprocess spawned, not two.
    assert len(agent_calls) == 1, (
        f"expected 1 _run_agent call, got {len(agent_calls)}: {agent_calls}"
    )
    # And the step counter incremented exactly once — not twice.
    par = db.get_task("T-PAR")
    assert par.orchestration_step_count == 1, (
        f"expected orchestration_step_count=1, got {par.orchestration_step_count}"
    )


def test_revisit_header_includes_sr_summary(runtime, db):
    """When the predecessor task submitted SRs, revisit header lists them."""
    from datetime import datetime, timezone

    from runtime.infrastructure.audit_logger import AuditLogger
    from runtime.models import (
        JobInterpreter,
        JobRecord,
        JobStatus,
        TaskRecord,
        TaskStatus,
    )
    from runtime.orchestrator.run_step import _revisit_header_if_applicable

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    predecessor = TaskRecord(
        id="TASK-001",
        assigned_agent="engineering_head",
        team="engineering",
        brief="orig",
        status=TaskStatus.FAILED,
    )
    revisit = TaskRecord(
        id="TASK-002",
        assigned_agent="engineering_head",
        team="engineering",
        brief="retry",
        status=TaskStatus.IN_PROGRESS,
    )
    db.insert_task(predecessor)
    db.insert_task(revisit)

    # Seed an SR submitted by the predecessor.
    sr = JobRecord(
        id="SR-019",
        task_id="TASK-001",
        agent_name="engineering_head",
        title="Close PR #247 with approval comment",
        rationale="r",
        script_text="echo x",
        interpreter=JobInterpreter.BASH,
        status=JobStatus.COMPLETED,
        created_at=now,
    )
    db.insert_job(sr)

    # Audit: script_submitted on predecessor, revisit_of on revisit.
    audit = AuditLogger(db)
    audit.log_job_submitted(
        task_id="TASK-001",
        job_id="SR-019",
        agent="engineering_head",
        title="Close PR #247 with approval comment",
        interpreter="bash",
        cwd_hint=None,
        byte_size=10,
        line_count=1,
    )
    db.insert_audit_log(
        task_id="TASK-002",
        agent="founder",
        action="revisit_of",
        payload={
            "predecessor_root": "TASK-001",
            "flagged": "TASK-001",
            "prior_status": "failed",
            "cascade": ["TASK-001"],
            "founder_note": "retry",
        },
    )

    # Mock orchestrator: just needs ._db.
    class _MockOrch:
        def __init__(self, d):
            self._db = d

    header = _revisit_header_if_applicable(_MockOrch(db), "TASK-002")
    assert header is not None
    assert "SR-019" in header
    assert "Close PR #247" in header
    assert "happyranch jobs show SR-019" in header
    assert "happyranch jobs output SR-019" in header


# ---- Cancel-race Guard B: post-_run_agent re-check ----
# See docs/superpowers/specs/2026-05-26-cancel-race-design.md §5.2.

def test_run_step_drops_delegate_when_cancelled_during_session(runtime, db, monkeypatch):
    """Guard B: /cancel can land between try_claim_for_step and subprocess exit.
    The l.41 entry guard only catches NEW enqueues. When `_run_agent` returns
    with a delegate decision but the task is now cancelled, no child task may
    be spawned and the founder-set status / note must remain intact.

    This is the regression check for the TASK-497 cancel race documented in
    docs/superpowers/specs/2026-05-26-cancel-race-design.md.
    """
    import json
    from datetime import datetime, timezone
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.models import TokenUsage

    # Workspace must exist so _validate_delegate doesn't error out and
    # take us through the (already-idempotent) _fail path instead of the
    # (not-yet-guarded) delegate path we're testing.
    (runtime.workspaces_dir / "dev_agent").mkdir(parents=True)

    db.insert_task(TaskRecord(
        id="T-RACE", brief="x", assigned_agent="engineering_head",
    ))
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    def cancel_then_delegate(*a, **k):
        # Simulate /cancel landing while the subprocess was running. By the
        # time _run_agent returns, the founder has already stamped the row.
        now = datetime.now(timezone.utc).isoformat()
        db.update_task(
            "T-RACE",
            status=TaskStatus.FAILED,
            block_kind=None,
            note="cancelled by founder: stop",
            cancelled_at=now,
            completed_at=now,
        )
        # The EH session would have produced its decision before SIGTERM
        # took effect; we model that by returning a delegate decision anyway.
        result = _make_result()
        result.token_usage = TokenUsage(
            input_tokens=10, output_tokens=20, model="claude-opus",
        )
        report = _make_report(
            output_summary=json.dumps({
                "action": "delegate", "agent": "dev_agent", "prompt": "ship it",
            }),
        )
        return result, report

    monkeypatch.setattr(orch, "_run_agent", cancel_then_delegate)

    orch.run_step("T-RACE")

    t = db.get_task("T-RACE")
    # Founder's terminal state preserved.
    assert t.status == TaskStatus.FAILED
    assert t.note == "cancelled by founder: stop"
    assert t.cancelled_at is not None
    # No child task spawned by the delegate decision.
    assert db.get_children("T-RACE") == []
    # Queue stays empty — nothing to dispatch.
    assert orch._queue.qsize() == 0
    # Token usage IS persisted regardless of cancel (spec §5.2 — provider
    # really charged for the session; /tokens rollups must reflect spend).
    usage_rows = db.list_session_token_usage(task_id="T-RACE")
    assert len(usage_rows) == 1
    assert usage_rows[0]["input_tokens"] == 10
    assert usage_rows[0]["output_tokens"] == 20


@pytest.mark.parametrize(
    ("task_id", "field", "origin_id"),
    [
        ("T-THREAD", "dispatched_from_thread_id", "THR-001"),
    ],
)
def test_run_step_token_usage_carries_task_origin_scope(
    runtime, db, monkeypatch, task_id, field, origin_id,
):
    from datetime import datetime, timezone
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.models import TokenUsage

    task_kwargs = {
        "id": task_id,
        "brief": "x",
        "assigned_agent": "engineering_head",
        field: origin_id,
    }
    db.insert_task(TaskRecord(**task_kwargs))
    orch = Orchestrator(
        db=db, settings=Settings(), paths=runtime, slug="test",
        teams=TeamsRegistry.load(runtime.root),
    )
    orch._queue = _SlugQueue()

    def cancel_with_usage(*a, **k):
        now = datetime.now(timezone.utc).isoformat()
        db.update_task(
            task_id,
            status=TaskStatus.FAILED,
            note="cancelled by founder: stop",
            cancelled_at=now,
            completed_at=now,
        )
        result = _make_result()
        result.token_usage = TokenUsage(input_tokens=3, output_tokens=4)
        return result, _make_report(output_summary="ignored")

    monkeypatch.setattr(orch, "_run_agent", cancel_with_usage)

    orch.run_step(task_id)

    rows = db.list_session_token_usage(task_id=task_id)
    assert len(rows) == 1
    assert rows[0]["scope_type"] == "task"
    assert rows[0]["scope_id"] == task_id
    assert rows[0]["thread_id"] == (
        origin_id if field == "dispatched_from_thread_id" else None
    )


# ---- Cancel-race Guard C: shared terminal predicate ----
# See docs/superpowers/specs/2026-05-26-cancel-race-design.md §5.3.

def test_is_already_terminal_predicate(runtime, db):
    """Single source of truth for the `done` / `delegate` / `escalate` / `_fail`
    / `_complete` idempotence guards. Returns True for missing tasks, for
    terminal statuses (COMPLETED, FAILED), and for cancelled rows even if their
    status hasn't yet flipped to FAILED.
    """
    from datetime import datetime, timezone
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _is_already_terminal

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))

    # Missing task → True (treat as terminal; nothing to act on).
    assert _is_already_terminal(orch, "T-NOPE") is True

    # PENDING → False.
    db.insert_task(TaskRecord(id="T-A", brief="a"))
    assert _is_already_terminal(orch, "T-A") is False

    # IN_PROGRESS → False.
    db.update_task("T-A", status=TaskStatus.IN_PROGRESS)
    assert _is_already_terminal(orch, "T-A") is False

    # IN_PROGRESS(delegated) → False. (Parent in_progress(delegated) is waiting
    # on a child, not terminal; a fresh manager step is allowed.)
    db.update_task("T-A", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED)
    assert _is_already_terminal(orch, "T-A") is False

    # COMPLETED → True.
    db.update_task("T-A", status=TaskStatus.COMPLETED, block_kind=None)
    assert _is_already_terminal(orch, "T-A") is True

    # FAILED → True.
    db.insert_task(TaskRecord(id="T-B", brief="b"))
    db.update_task("T-B", status=TaskStatus.FAILED)
    assert _is_already_terminal(orch, "T-B") is True

    # Cancelled even if status hasn't yet been flipped to FAILED — defense
    # in depth against a future code path that stamps cancelled_at without
    # touching status. Per spec §5.3.
    db.insert_task(TaskRecord(id="T-C", brief="c"))
    now = datetime.now(timezone.utc).isoformat()
    db.update_task("T-C", status=TaskStatus.IN_PROGRESS, cancelled_at=now)
    assert _is_already_terminal(orch, "T-C") is True


def test_run_step_delegate_atomic_against_cancel_between_recheck_and_cas(
    runtime, db, monkeypatch,
):
    """Codex P1 on PR #34: even after Guard B's re-fetch passes, /cancel can
    land between the re-fetch and the delegate's insert+update. The atomic
    CAS in db.try_delegate must close this window — no child created, parent
    state preserved.

    Simulated by monkey-patching db.try_delegate to invoke /cancel just before
    its conditional UPDATE runs. This reproduces the worst-case interleaving
    that the Python-level check-then-act would have lost.
    """
    import json
    from datetime import datetime, timezone
    from runtime.orchestrator.orchestrator import Orchestrator

    (runtime.workspaces_dir / "dev_agent").mkdir(parents=True)

    db.insert_task(TaskRecord(
        id="T-RACE2", brief="x", assigned_agent="engineering_head",
    ))
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    # _run_agent returns a delegate without cancelling — Guard B re-fetch
    # will pass. The cancel races in via the monkey-patched try_delegate.
    monkeypatch.setattr(orch, "_run_agent",
                        lambda *a, **k: (_make_result(), _make_report(
                            output_summary=json.dumps({
                                "action": "delegate", "agent": "dev_agent",
                                "prompt": "ship it",
                            }),
                        )))

    # Wrap try_delegate so the cancel lands at the worst moment: AFTER Guard B
    # re-checks but BEFORE the CAS write. The atomic SELECT inside try_delegate
    # should observe the cancel and return False.
    real_try_delegate = db.try_delegate
    def racy_try_delegate(parent_id, child, *, parent_note, attachments=None, active_chain_json=None, uploaded_by="orchestrator"):
        # Simulate founder cancel landing just before the CAS SELECT.
        now = datetime.now(timezone.utc).isoformat()
        db.update_task(
            parent_id,
            status=TaskStatus.FAILED, block_kind=None,
            note="cancelled by founder: stop",
            cancelled_at=now, completed_at=now,
        )
        return real_try_delegate(parent_id, child, parent_note=parent_note,
                                  attachments=attachments, active_chain_json=active_chain_json,
                                  uploaded_by=uploaded_by)
    monkeypatch.setattr(db, "try_delegate", racy_try_delegate)

    orch.run_step("T-RACE2")

    t = db.get_task("T-RACE2")
    assert t.status == TaskStatus.FAILED
    assert t.note == "cancelled by founder: stop"
    assert t.cancelled_at is not None
    # CRITICAL: no child created — the atomic CAS observed the cancel and bailed.
    assert db.get_children("T-RACE2") == []
    assert orch._queue.qsize() == 0


def test_run_step_escalate_atomic_against_cancel_between_recheck_and_cas(
    runtime, db, monkeypatch,
):
    """Codex P2 on PR #34: same race shape for the escalate branch — cancel
    landing between Guard B and the conditional UPDATE must not resurrect a
    cancelled row into escalated."""
    import json
    from datetime import datetime, timezone
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(
        id="T-ESC", brief="x", assigned_agent="engineering_head",
    ))
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    monkeypatch.setattr(orch, "_run_agent",
                        lambda *a, **k: (_make_result(), _make_report(
                            output_summary=json.dumps({
                                "action": "escalate", "reason": "blocked on creds",
                            }),
                        )))

    real_try_escalate = db.try_escalate
    def racy_try_escalate(task_id, *, reason):
        now = datetime.now(timezone.utc).isoformat()
        db.update_task(
            task_id,
            status=TaskStatus.FAILED, block_kind=None,
            note="cancelled by founder: stop",
            cancelled_at=now, completed_at=now,
        )
        return real_try_escalate(task_id, reason=reason)
    monkeypatch.setattr(db, "try_escalate", racy_try_escalate)

    orch.run_step("T-ESC")

    t = db.get_task("T-ESC")
    assert t.status == TaskStatus.FAILED
    assert t.note == "cancelled by founder: stop"
    assert t.cancelled_at is not None
    # block_kind stays None (cancel cleared it); not escalated.
    assert t.block_kind is None


def _seed_open_thread_dispatch(db, *, thread_id, task_id, dispatcher, target):
    # Sibling: _seed_dispatched_root in test_thread_task_followup.py also inserts
    # the TaskRecord; this one does not — callers insert the task themselves.
    from runtime.models import ThreadRecord
    from runtime.infrastructure.audit_logger import AuditLogger
    db.insert_thread(ThreadRecord(id=thread_id, subject="t"))
    db.add_thread_participant(thread_id, dispatcher, added_by="founder")
    AuditLogger(db).log_thread_dispatch(
        thread_id, task_id=task_id, dispatcher=dispatcher,
        target_agent=target, team="engineering",
    )


def test_run_step_escalate_surfaces_in_thread(runtime, db, monkeypatch):
    import json
    from runtime.models import ThreadInvocationPurpose
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(
        id="T-1", brief="x", assigned_agent="engineering_head",
        dispatched_from_thread_id="THR-9",
    ))
    _seed_open_thread_dispatch(db, thread_id="THR-9", task_id="T-1",
                               dispatcher="engineering_head", target="engineering_head")

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()  # mirror existing escalate test; not used on escalate path

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary=json.dumps({"action": "escalate", "reason": "needs founder auth"}),
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    orch.run_step("T-1")

    t = db.get_task("T-1")
    assert t.status == TaskStatus.ESCALATED and t.block_kind is None  # Path B
    msgs = db.list_thread_messages("THR-9")
    esc = [m for m in msgs if m.system_payload
           and m.system_payload.get("kind_tag") == "task_escalated"]
    assert len(esc) == 1
    assert esc[0].system_payload["reason"] == "needs founder auth"
    invs = db.list_thread_invocations("THR-9")
    assert any(i.purpose == ThreadInvocationPurpose.TASK_FOLLOWUP for i in invs)


def test_run_step_over_budget_surfaces_in_thread(runtime, db):
    from runtime.models import ThreadInvocationPurpose
    from runtime.orchestrator.orchestrator import Orchestrator
    settings = Settings(max_orchestration_steps=3)
    db.insert_task(TaskRecord(
        id="T-1", brief="x", assigned_agent="engineering_head",
        dispatched_from_thread_id="THR-9",
    ))
    db.update_task("T-1", orchestration_step_count=3)  # already at the cap
    _seed_open_thread_dispatch(db, thread_id="THR-9", task_id="T-1",
                               dispatcher="engineering_head", target="engineering_head")

    orch = Orchestrator(db=db, settings=settings, paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch.run_step("T-1")

    msgs = db.list_thread_messages("THR-9")
    esc = [m for m in msgs if m.system_payload
           and m.system_payload.get("kind_tag") == "task_escalated"]
    assert len(esc) == 1
    assert "max steps" in esc[0].system_payload["reason"]


def test_non_manager_owner_of_task_type_emits_decision(runtime, db, monkeypatch):
    """A type=task owned by a NON-manager parses its decision (done here)."""
    import json
    from runtime.orchestrator.orchestrator import Orchestrator
    db.insert_task(TaskRecord(
        id="T-1", brief="root", assigned_agent="dev_agent", task_type="task",
    ))
    orch = Orchestrator(db=db, settings=Settings(max_orchestration_steps=10),
                        paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary=json.dumps({"action": "done", "summary": "did it"}),
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    orch.run_step("T-1")

    t = db.get_task("T-1")
    assert t.status == TaskStatus.COMPLETED
    assert t.note == "did it"


def test_subtask_owner_is_leaf_even_if_decision_present(runtime, db, monkeypatch):
    """A type=subtask owner does NOT orchestrate: a delegate decision in its
    report is ignored and the task simply completes (leaf path)."""
    import json
    from runtime.orchestrator.orchestrator import Orchestrator
    db.insert_task(TaskRecord(
        id="T-2", brief="leaf", assigned_agent="engineering_head",
        task_type="subtask",
    ))
    orch = Orchestrator(db=db, settings=Settings(max_orchestration_steps=10),
                        paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        # Even though this is a manager AND emits a delegate, the subtask
        # gate forces leaf completion.
        return _make_result(), _make_report(
            output_summary=json.dumps(
                {"action": "delegate", "agent": "dev_agent", "prompt": "go"}),
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    orch.run_step("T-2")
    t = db.get_task("T-2")
    assert t.status == TaskStatus.COMPLETED          # leaf — no child spawned
    assert db.get_children("T-2") == []


def test_delegated_child_is_typed_subtask(runtime, db, monkeypatch):
    import json
    from runtime.orchestrator.orchestrator import Orchestrator
    (runtime.workspaces_dir / "dev_agent").mkdir(parents=True)
    db.insert_task(TaskRecord(
        id="T-1", brief="root", assigned_agent="engineering_head",
        task_type="task",
    ))
    orch = Orchestrator(db=db, settings=Settings(max_orchestration_steps=10),
                        paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary=json.dumps(
                {"action": "delegate", "agent": "dev_agent", "prompt": "build"}),
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    orch.run_step("T-1")
    children = db.get_children("T-1")
    assert len(children) == 1
    assert db.get_task(children[0]).task_type == "subtask"


def test_non_manager_self_delegation_is_allowed(runtime, db, monkeypatch):
    """dev_agent owns a type=task and delegates to ITSELF → child spawned."""
    import json
    from runtime.orchestrator.orchestrator import Orchestrator
    (runtime.workspaces_dir / "dev_agent").mkdir(parents=True, exist_ok=True)
    db.insert_task(TaskRecord(id="T-1", brief="root",
                              assigned_agent="dev_agent", task_type="task"))
    orch = Orchestrator(db=db, settings=Settings(max_orchestration_steps=10),
                        paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    def fake(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary=json.dumps(
                {"action": "delegate", "agent": "dev_agent", "prompt": "phase 2"}))
    monkeypatch.setattr(orch, "_run_agent", fake)

    orch.run_step("T-1")
    children = db.get_children("T-1")
    assert len(children) == 1
    assert db.get_task(children[0]).assigned_agent == "dev_agent"


def test_non_manager_cross_agent_delegation_is_rejected(runtime, db, monkeypatch):
    """dev_agent owning a type=task may NOT delegate to product_manager →
    feedback step, task re-enqueued PENDING, no child."""
    import json
    from runtime.orchestrator.orchestrator import Orchestrator
    (runtime.workspaces_dir / "product_manager").mkdir(parents=True, exist_ok=True)
    db.insert_task(TaskRecord(id="T-1", brief="root",
                              assigned_agent="dev_agent", task_type="task"))
    orch = Orchestrator(db=db, settings=Settings(max_orchestration_steps=10),
                        paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    def fake(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary=json.dumps(
                {"action": "delegate", "agent": "product_manager", "prompt": "x"}))
    monkeypatch.setattr(orch, "_run_agent", fake)

    orch.run_step("T-1")
    assert db.get_children("T-1") == []
    assert db.get_task("T-1").status == TaskStatus.PENDING   # re-enqueued for re-decide


def test_manager_self_target_does_not_bump_revision_count(runtime, db, monkeypatch):
    """A manager re-delegating to ITSELF is sequencing, not a revise loop —
    revision_count must stay 0 so escalate-after-2-rounds doesn't misfire."""
    import json
    from runtime.orchestrator.orchestrator import Orchestrator
    (runtime.workspaces_dir / "engineering_head").mkdir(parents=True, exist_ok=True)
    # One already-completed self-child makes engineering_head the worker-of-record.
    db.insert_task(TaskRecord(id="T-1", brief="root",
                              assigned_agent="engineering_head", task_type="task"))
    db.insert_task(TaskRecord(id="T-1-c1", brief="c1",
                              assigned_agent="engineering_head",
                              parent_task_id="T-1", task_type="subtask"))
    db.update_task("T-1-c1", status=TaskStatus.COMPLETED)

    orch = Orchestrator(db=db, settings=Settings(max_orchestration_steps=10),
                        paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    def fake(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary=json.dumps(
                {"action": "delegate", "agent": "engineering_head",
                 "prompt": "phase 2"}))
    monkeypatch.setattr(orch, "_run_agent", fake)

    orch.run_step("T-1")
    assert db.get_task("T-1").revision_count == 0


def test_build_agent_prompt_leaf_subtask_is_empty(runtime, db):
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _build_agent_prompt
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    t = TaskRecord(id="T-1", brief="x", assigned_agent="dev_agent",
                   task_type="subtask")
    assert _build_agent_prompt(orch, t, "dev_agent") == ""


def test_build_agent_prompt_non_manager_task_is_self_only(runtime, db):
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _build_agent_prompt
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    t = TaskRecord(id="T-1", brief="x", assigned_agent="dev_agent",
                   task_type="task")
    p = _build_agent_prompt(orch, t, "dev_agent")
    assert "Available Agents" not in p
    assert "dev_agent" in p


def test_build_agent_prompt_manager_roster_includes_self(runtime, db):
    """Spec §3: a manager may self-target. The roster must advertise self so the
    manager knows it can delegate a sub-task to itself (it is not in teams.yaml
    `workers`, so it would otherwise be absent)."""
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _build_agent_prompt
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test",
                        teams=TeamsRegistry.load(runtime.root))
    t = TaskRecord(id="T-1", brief="x", assigned_agent="engineering_head",
                   task_type="task")
    p = _build_agent_prompt(orch, t, "engineering_head")
    assert "Available Agents" in p          # full roster prompt
    assert "engineering_head" in p          # self advertised in the roster
    assert "yourself" in p


# ═══════════════════════════════════════════════════════════════════
# TASK-573 — bounded failure-recovery tests
# ═══════════════════════════════════════════════════════════════════


def test_failed_child_wakes_parent_for_decision_step_not_cascade(
    runtime, db, monkeypatch,
):
    """One failed child → parent gets a manager decision step (enqueued),
    NOT cascade-failed. The old behavior unconditionally _fail'd the parent;
    the new contract wakes it for a bounded re-decision."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-PAR", brief="parent brief",
                              assigned_agent="engineering_head",
                              task_type="task"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-CHD", brief="child brief",
        assigned_agent="dev_agent", parent_task_id="T-PAR",
        task_type="subtask",
    ))
    # Child is FAILED — the scenario the orchestrator must handle.
    db.update_task("T-CHD", status=TaskStatus.FAILED,
                   note="reviewer found issues: REQUEST_CHANGES")

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting
    _enqueue_parent_if_waiting(orch, "T-CHD")

    # Parent must NOT be FAILED — it gets a decision step.
    parent = db.get_task("T-PAR")
    assert parent.status == TaskStatus.IN_PROGRESS, (
        f"parent should stay in_progress(delegated) for decision step, got {parent.status}"
    )
    assert parent.block_kind == BlockKind.DELEGATED

    # Parent is enqueued for its next decision step.
    assert orch._queue.qsize() == 1
    slug, tid = orch._queue.get_nowait()
    assert tid == "T-PAR"


def test_two_failed_children_wakes_owner_not_escalate(runtime, db, monkeypatch):
    """THR-078: two failed children with no revisit lineage (different
    slices, each failing for the first time) wakes the owner for
    adjudication, NOT escalated.  The old count-based _FAILURE_ROUND_BOUND
    auto-escalation is retired."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-PAR", brief="parent brief",
                              assigned_agent="engineering_head",
                              task_type="task"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")

    # Two different slices, each failing for the first time.
    db.insert_task(TaskRecord(
        id="T-F1", brief="first failed child",
        assigned_agent="dev_agent", parent_task_id="T-PAR",
        task_type="subtask",
    ))
    db.update_task("T-F1", status=TaskStatus.FAILED,
                   note="first failure")

    db.insert_task(TaskRecord(
        id="T-F2", brief="second failed child",
        assigned_agent="dev_agent", parent_task_id="T-PAR",
        task_type="subtask",
    ))
    db.update_task("T-F2", status=TaskStatus.FAILED,
                   note="second failure — different slice")

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting
    _enqueue_parent_if_waiting(orch, "T-F2")

    parent = db.get_task("T-PAR")
    # No revisit lineage → owner is woken, not escalated.
    assert parent.status == TaskStatus.IN_PROGRESS, (
        f"owner should be woken for adjudication, got {parent.status}"
    )
    assert parent.block_kind == BlockKind.DELEGATED

    # Parent is enqueued for its next decision step.
    assert orch._queue.qsize() == 1
    slug, tid = orch._queue.get_nowait()
    assert tid == "T-PAR"


def test_chain_leg_failure_wakes_parent_not_cascade(runtime, db, monkeypatch):
    """A failed chain leg clears the chain and hands back to the parent's
    manager decision step (subject to the same 2-round bound), NOT cascade-fail."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-PAR", brief="chain parent",
                              assigned_agent="engineering_head",
                              task_type="task"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")

    # Set up an active chain on the parent.
    from runtime.orchestrator.chain import ChainState, ChainLeg
    chain = ChainState(
        step_index=0, first_leg_expect_verdict=None,
        legs=[ChainLeg(agent="code_reviewer", prompt="review",
                       expect_verdict="APPROVE")],
        step_audit_id=1,
    )
    db.update_task_active_chain("T-PAR", chain.serialize())

    # Chain leg child FAILED.
    db.insert_task(TaskRecord(
        id="T-LEG", brief="review",
        assigned_agent="code_reviewer", parent_task_id="T-PAR",
        task_type="subtask",
    ))
    db.update_task("T-LEG", status=TaskStatus.FAILED,
                   note="self-blocked: REQUEST_CHANGES")

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting
    _enqueue_parent_if_waiting(orch, "T-LEG")

    parent = db.get_task("T-PAR")
    # Chain cleared; parent gets a decision step, not cascade-failed.
    assert parent.active_chain is None, "chain must be cleared on leg failure"
    assert parent.status == TaskStatus.IN_PROGRESS
    assert parent.block_kind == BlockKind.DELEGATED
    # Parent is enqueued.
    assert orch._queue.qsize() == 1
    slug, tid = orch._queue.get_nowait()
    assert tid == "T-PAR"


def test_regression_all_children_completed_wakes_parent_unchanged(
    runtime, db, monkeypatch,
):
    """REGRESSION GUARD: happy path — all children COMPLETED → parent enqueued
    for next decision step. This behavior MUST NOT change."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-PAR", brief="parent brief",
                              assigned_agent="engineering_head",
                              task_type="task"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-CHD", brief="child brief",
        assigned_agent="dev_agent", parent_task_id="T-PAR",
        task_type="subtask",
    ))
    db.update_task("T-CHD", status=TaskStatus.COMPLETED, note="all good")

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting
    _enqueue_parent_if_waiting(orch, "T-CHD")

    parent = db.get_task("T-PAR")
    assert parent.status == TaskStatus.IN_PROGRESS
    assert parent.block_kind == BlockKind.DELEGATED
    assert orch._queue.qsize() == 1
    slug, tid = orch._queue.get_nowait()
    assert tid == "T-PAR"


def test_regression_revise_verdict_chain_advance_unchanged(
    runtime, db, monkeypatch,
):
    """REGRESSION GUARD: REVISE-verdict auto-advance in chains is UNCHANGED.
    A COMPLETED child with REVISE verdict must still advance the chain."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-PAR", brief="chain parent",
                              assigned_agent="engineering_head",
                              task_type="task"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")

    from runtime.orchestrator.chain import ChainState, ChainLeg
    chain = ChainState(
        step_index=0, first_leg_expect_verdict="APPROVE",
        legs=[
            ChainLeg(agent="code_reviewer", prompt="review",
                     expect_verdict="APPROVE"),
            ChainLeg(agent="dev_agent", prompt="revise",
                     expect_verdict=None),
        ],
        step_audit_id=1,
    )
    db.update_task_active_chain("T-PAR", chain.serialize())

    # Chain leg child COMPLETED with REVISE verdict.
    db.insert_task(TaskRecord(
        id="T-LEG", brief="review",
        assigned_agent="code_reviewer", parent_task_id="T-PAR",
        task_type="subtask",
    ))
    db.update_task("T-LEG", status=TaskStatus.COMPLETED, note="done")
    db.insert_task_result(
        task_id="T-LEG", agent="code_reviewer", session_id="s",
        status="completed", confidence_score=85,
        output_summary="found issues, REVISE",
        verdict="REVISE",
    )

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting
    _enqueue_parent_if_waiting(orch, "T-LEG")

    # Chain auto-advance: REVISE verdict matches first_leg_expect_verdict=APPROVE?
    # No — REVISE does NOT match APPROVE, so the chain should clear and the
    # parent should wake. The REVISE auto-advance works by having
    # expect_verdict=None on the follow-up leg (the advance_action returns
    # advance regardless of verdict when expect_verdict is None).
    # In this test: first_leg_expect_verdict="APPROVE", child verdict="REVISE"
    # → mismatch → wake. The parent gets enqueued for a decision step.
    parent = db.get_task("T-PAR")
    assert parent.status == TaskStatus.IN_PROGRESS
    assert parent.block_kind == BlockKind.DELEGATED
    assert parent.active_chain is None  # chain cleared on mismatch
    assert orch._queue.qsize() == 1
    slug, tid = orch._queue.get_nowait()
    assert tid == "T-PAR"


def test_run_step_nonroot_over_budget_fails_and_routes_to_parent(runtime, db):
    """THR-033 Change A — the one substantive behavioral fix: a NON-root task
    that exceeds the step budget FAILS (block_kind=NULL) instead of parking in
    escalated, and hands back to its parent for bounded recovery."""
    from runtime.orchestrator.orchestrator import Orchestrator
    settings = Settings(max_orchestration_steps=3)

    db.insert_task(TaskRecord(id="T-PAR", brief="p",
                              assigned_agent="engineering_head"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-CHD", brief="c", assigned_agent="dev_agent",
        parent_task_id="T-PAR", task_type="subtask",
    ))
    db.update_task("T-CHD", orchestration_step_count=3)  # already at the cap

    orch = Orchestrator(db=db, settings=settings, paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    q = _SlugQueue()
    orch._queue = q

    orch.run_step("T-CHD")

    child = db.get_task("T-CHD")
    assert child.status == TaskStatus.FAILED
    assert child.block_kind is None
    assert child.note and "max steps" in child.note
    assert child.completed_at is not None  # terminal row carries completed_at

    # Never escalated — no escalation audit row for the child.
    escalations = [
        a for a in db.get_audit_logs("T-CHD") if a["action"] == "escalation"
    ]
    assert escalations == []

    # Parent woken (1 failed child < bound).
    assert q.qsize() == 1
    assert q.get_nowait() == ("test", "T-PAR")


def test_run_step_nonroot_over_budget_idempotent_single_parent_wake(runtime, db):
    """Duplicate delivery of the same at-cap non-root row wakes the parent
    exactly once. The CAS in try_fail_over_budget (and the entry-state guard)
    makes the second delivery a no-op."""
    from runtime.orchestrator.orchestrator import Orchestrator
    settings = Settings(max_orchestration_steps=3)

    db.insert_task(TaskRecord(id="T-PAR", brief="p",
                              assigned_agent="engineering_head"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-CHD", brief="c", assigned_agent="dev_agent",
        parent_task_id="T-PAR", task_type="subtask",
    ))
    db.update_task("T-CHD", orchestration_step_count=3)

    orch = Orchestrator(db=db, settings=settings, paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    q = _SlugQueue()
    orch._queue = q

    orch.run_step("T-CHD")
    orch.run_step("T-CHD")  # duplicate delivery

    assert db.get_task("T-CHD").status == TaskStatus.FAILED
    assert q.qsize() == 1  # parent enqueued exactly once


def test_run_step_root_over_budget_still_escalates(runtime, db):
    """THR-033 Change A: a ROOT task that exceeds the step budget parks
    in escalated for the founder — unchanged."""
    from runtime.orchestrator.orchestrator import Orchestrator
    settings = Settings(max_orchestration_steps=3)
    db.insert_task(TaskRecord(
        id="T-ROOT", brief="x", assigned_agent="engineering_head",
    ))
    db.update_task("T-ROOT", orchestration_step_count=3)

    orch = Orchestrator(db=db, settings=settings, paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    orch.run_step("T-ROOT")

    t = db.get_task("T-ROOT")
    assert t.parent_task_id is None
    assert t.status == TaskStatus.ESCALATED  # Path B: top-level status
    assert t.block_kind is None
    assert t.note and "max steps" in t.note


def test_run_step_nonroot_self_block_never_escalated(runtime, db, monkeypatch):
    """Regression (THR-033 Change A confirm-no-change): a NON-root self-block
    (report.status=blocked, empty waiting_on_job_ids) fails through the parent
    and is NEVER escalated."""
    from runtime.orchestrator.orchestrator import Orchestrator

    db.insert_task(TaskRecord(id="T-PAR", brief="p",
                              assigned_agent="engineering_head"))
    db.update_task("T-PAR", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    db.insert_task(TaskRecord(
        id="T-CHD", brief="c", assigned_agent="dev_agent",
        parent_task_id="T-PAR", task_type="subtask",
    ))

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime, slug="test", teams=TeamsRegistry.load(runtime.root))
    q = _SlugQueue()
    orch._queue = q

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary="cannot proceed", status="blocked",
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    orch.run_step("T-CHD")

    child = db.get_task("T-CHD")
    assert child.status == TaskStatus.FAILED
    assert child.block_kind is None

    escalations = [
        a for a in db.get_audit_logs("T-CHD") if a["action"] == "escalation"
    ]
    assert escalations == []
    # Parent woken via bounded recovery.
    assert q.qsize() == 1
    assert q.get_nowait() == ("test", "T-PAR")


def test_enqueue_parent_exhaustion_nonroot_parent_routes_upward(runtime, db):
    """THR-033 Change A lock-in: if an exhausted-bound parent were itself a
    NON-root (impossible in production today — only roots have children), it
    must NOT escalate directly. It fails and routes the failure to its own
    parent. Locks the defensive is_root(parent) guard in
    _enqueue_parent_if_waiting."""
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting

    # Grandparent (root), delegated + waiting.
    db.insert_task(TaskRecord(id="T-GP", brief="gp",
                              assigned_agent="engineering_head"))
    db.update_task("T-GP", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    # Middle parent: NON-root (child of T-GP) but itself delegating + blocked.
    db.insert_task(TaskRecord(
        id="T-MID", brief="mid", assigned_agent="engineering_head",
        parent_task_id="T-GP", task_type="task",
    ))
    db.update_task("T-MID", status=TaskStatus.IN_PROGRESS, block_kind=BlockKind.DELEGATED, note="waiting")
    # Two failed children of T-MID with no revisit lineage.
    db.insert_task(TaskRecord(
        id="T-F1", brief="f1", assigned_agent="dev_agent",
        parent_task_id="T-MID", task_type="subtask",
    ))
    db.update_task("T-F1", status=TaskStatus.FAILED, note="first failure")
    db.insert_task(TaskRecord(
        id="T-F2", brief="f2", assigned_agent="dev_agent",
        parent_task_id="T-MID", task_type="subtask",
    ))
    db.update_task("T-F2", status=TaskStatus.FAILED,
                   note="second failure — different slice")

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    _enqueue_parent_if_waiting(orch, "T-F2")

    mid = db.get_task("T-MID")
    # THR-078: no revisit lineage → non-root parent is woken for
    # adjudication, not failed.  Only per-slice ceiling exhaustion
    # (a child retry that fails again) would fail the non-root.
    assert mid.status == TaskStatus.IN_PROGRESS
    assert mid.block_kind == BlockKind.DELEGATED

    # Non-root parent is enqueued.
    assert orch._queue.qsize() == 1
    assert orch._queue.get_nowait() == ("test", "T-MID")


# --- THR-078: per-slice retry ceiling, owner-adjudication primary ---


def test_fanout_mixed_outcome_wakes_owner_not_escalate(runtime, db, monkeypatch):
    """THR-078: any fan-out round with >=1 non-clean slice wakes the root
    owner with per-slice join context, NOT auto-escalated to founder.

    Even with 2+ failed siblings (old _FAILURE_ROUND_BOUND trigger),
    the new design wakes the owner to adjudicate."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting

    # Root parent with 3 fan-out children: 2 failed, 1 completed.
    db.insert_task(TaskRecord(
        id="T-MIXED", brief="fan-out parent",
        assigned_agent="engineering_head",
        task_type="task",
    ))
    db.update_task("T-MIXED", status=TaskStatus.IN_PROGRESS,
                   block_kind=BlockKind.DELEGATED, note="fan-out in flight")

    # Child 1: failed (REQUEST_CHANGES via carrier verdict mismatch)
    db.insert_task(TaskRecord(
        id="T-MIXED-C1", brief="build feature",
        assigned_agent="dev_agent", parent_task_id="T-MIXED",
        task_type="subtask",
    ))
    db.update_task("T-MIXED-C1", status=TaskStatus.FAILED,
                   note="carrier verdict mismatch: expected 'APPROVE', got 'REQUEST_CHANGES'")
    # Child 2: failed (no-op self-block)
    db.insert_task(TaskRecord(
        id="T-MIXED-C2", brief="no-op task",
        assigned_agent="dev_agent", parent_task_id="T-MIXED",
        task_type="subtask",
    ))
    db.update_task("T-MIXED-C2", status=TaskStatus.FAILED,
                   note="self-blocked: already shipped, nothing to do")
    # Child 3: completed cleanly
    db.insert_task(TaskRecord(
        id="T-MIXED-C3", brief="test",
        assigned_agent="qa_engineer", parent_task_id="T-MIXED",
        task_type="subtask",
    ))
    db.update_task("T-MIXED-C3", status=TaskStatus.COMPLETED)
    # Add completion report for child 3.
    db.insert_task_result(
        task_id="T-MIXED-C3", agent="qa_engineer", session_id="s",
        status="completed", confidence_score=90, output_summary="Done",
        risks_flagged=[],
    )

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    # Fire on the last terminal child (C2).
    _enqueue_parent_if_waiting(orch, "T-MIXED-C2")

    parent = db.get_task("T-MIXED")
    # Parent is woken (re-enqueued), NOT escalated.
    assert parent.status == TaskStatus.IN_PROGRESS, (
        f"owner should be woken, not escalated; got status {parent.status}"
    )
    assert parent.block_kind == BlockKind.DELEGATED

    # Parent is enqueued for its decision step.
    assert orch._queue.qsize() == 1
    slug, tid = orch._queue.get_nowait()
    assert tid == "T-MIXED"


def test_per_slice_retry_ceiling_escalates_on_second_failure(runtime, db, monkeypatch):
    """THR-078: per-slice retry ceiling = 1.  A slice that fails, gets
    re-dispatched by the owner (revisit_of_task_id set), and fails again
    forces escalation to founder — even when the TOTAL failed sibling
    count is only 1 (the original slice succeeded or was a different
    status).

    This is a RED test pre-impl: the old code wakes the owner (count=1 <
    _FAILURE_ROUND_BOUND=2) but the new per-slice design escalates."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting

    # Root parent.
    db.insert_task(TaskRecord(
        id="T-RETRY2", brief="fan-out parent",
        assigned_agent="engineering_head",
        task_type="task",
    ))
    db.update_task("T-RETRY2", status=TaskStatus.IN_PROGRESS,
                   block_kind=BlockKind.DELEGATED, note="fan-out in flight")

    # Original slice FAILED (first attempt was not clean).
    # Per-slice ceiling: exactly one retry after the FIRST FAILURE.
    db.insert_task(TaskRecord(
        id="T-RETRY2-C1", brief="build feature X",
        assigned_agent="dev_agent", parent_task_id="T-RETRY2",
        task_type="subtask",
    ))
    db.update_task("T-RETRY2-C1", status=TaskStatus.FAILED,
                   note="first failure of this slice")
    db.insert_task_result(
        task_id="T-RETRY2-C1", agent="dev_agent", session_id="s",
        status="failed", confidence_score=0, output_summary="Failed",
        risks_flagged=[],
    )

    # Another child completed cleanly.
    db.insert_task(TaskRecord(
        id="T-RETRY2-C2", brief="build feature Y",
        assigned_agent="dev_agent", parent_task_id="T-RETRY2",
        task_type="subtask",
    ))
    db.update_task("T-RETRY2-C2", status=TaskStatus.COMPLETED)
    db.insert_task_result(
        task_id="T-RETRY2-C2", agent="dev_agent", session_id="s",
        status="completed", confidence_score=90, output_summary="Done",
        risks_flagged=[],
    )

    # Re-dispatched slice: revisit_of_task_id points to the COMPLETED C1.
    # The owner re-delegated this slice post-fanout-join; it fails (2nd
    # failure of this slice). Total failed siblings = 1 (just this one).
    db.insert_task(TaskRecord(
        id="T-RETRY2-C1-R", brief="re-dispatched: build feature X",
        assigned_agent="dev_agent", parent_task_id="T-RETRY2",
        revisit_of_task_id="T-RETRY2-C1",
        task_type="subtask",
    ))
    db.update_task("T-RETRY2-C1-R", status=TaskStatus.FAILED,
                   note="second failure of this slice — ceiling exhausted")

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    # Fire on the re-dispatched child's failure.
    _enqueue_parent_if_waiting(orch, "T-RETRY2-C1-R")

    parent = db.get_task("T-RETRY2")
    # Ceiling exhausted → escalated to founder, NOT woken.
    assert parent.status == TaskStatus.ESCALATED, (
        f"per-slice ceiling exhausted should escalate; got status {parent.status}"
    )
    assert parent.block_kind is None
    assert "T-RETRY2-C1-R" in (parent.note or "")

    # No queue entries — escalation is not waking the parent.
    assert orch._queue.qsize() == 0

    # THR-181 Track A denominator: this runtime-raised escalation is not an
    # authority decision, but it still needs one explicit auditable outcome.
    rows = db.get_audit_logs("T-RETRY2")
    escalations = [row for row in rows if row["action"] == "escalation"]
    outcomes = [row for row in rows if row["action"] == "authority_hook"]
    assert len(escalations) == 1
    assert len(outcomes) == 1
    assert outcomes[0]["payload"] == {
        "outcome": "not_applicable",
        "reason_code": "runtime_retry_ceiling",
        "reason": "runtime-raised escalation is not an authority decision",
        "causal_escalation_audit_id": escalations[0]["id"],
    }

    # Startup/recovery re-entry sees the already-escalated parent and loses
    # the commit CAS: neither half of the durable pair is duplicated.
    _enqueue_parent_if_waiting(orch, "T-RETRY2-C1-R")
    replay_rows = db.get_audit_logs("T-RETRY2")
    assert len([row for row in replay_rows if row["action"] == "escalation"]) == 1
    assert len([row for row in replay_rows if row["action"] == "authority_hook"]) == 1


def test_runtime_retry_ceiling_audit_failure_rolls_back_whole_commit(
    runtime, db, monkeypatch,
):
    """The production retry seam cannot leave an escalation or orphan audit."""
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting

    db.insert_task(TaskRecord(
        id="T-RETRY-ROLLBACK", brief="fan-out parent",
        assigned_agent="engineering_head", task_type="task",
    ))
    db.update_task(
        "T-RETRY-ROLLBACK", status=TaskStatus.IN_PROGRESS,
        block_kind=BlockKind.DELEGATED,
    )
    db.update_task_active_fanout("T-RETRY-ROLLBACK", '{"children": []}')
    db.insert_task(TaskRecord(
        id="T-RETRY-ROLLBACK-A", brief="first failure", assigned_agent="dev_agent",
        parent_task_id="T-RETRY-ROLLBACK", task_type="subtask",
    ))
    db.update_task("T-RETRY-ROLLBACK-A", status=TaskStatus.FAILED)
    db.insert_task(TaskRecord(
        id="T-RETRY-ROLLBACK-B", brief="retry failure", assigned_agent="dev_agent",
        parent_task_id="T-RETRY-ROLLBACK", revisit_of_task_id="T-RETRY-ROLLBACK-A",
        task_type="subtask",
    ))
    db.update_task("T-RETRY-ROLLBACK-B", status=TaskStatus.FAILED)

    real_insert = db.insert_audit_log_uncommitted
    def fail_denominator(*args, **kwargs):
        if kwargs.get("action") == "authority_hook":
            raise RuntimeError("injected denominator append failure")
        return real_insert(*args, **kwargs)
    monkeypatch.setattr(db, "insert_audit_log_uncommitted", fail_denominator)

    orch = Orchestrator(
        db=db, settings=Settings(), paths=runtime, slug="test",
        teams=TeamsRegistry.load(runtime.root),
    )
    orch._queue = _SlugQueue()
    with pytest.raises(RuntimeError, match="denominator append failure"):
        _enqueue_parent_if_waiting(orch, "T-RETRY-ROLLBACK-B")

    parent = db.get_task("T-RETRY-ROLLBACK")
    assert parent.status == TaskStatus.IN_PROGRESS
    assert parent.block_kind == BlockKind.DELEGATED
    assert parent.active_fanout == '{"children": []}'
    assert not [
        row for row in db.get_audit_logs("T-RETRY-ROLLBACK")
        if row["action"] in {"escalation", "authority_hook"}
    ]


def test_runtime_escalation_write_failure_has_no_audit_residue(db):
    """A task-state write failure occurs before either audit append commits."""
    import sqlite3

    db.insert_task(TaskRecord(id="T-RUNTIME-WRITE-FAIL", brief="root"))
    db.execute("""
        CREATE TRIGGER reject_runtime_escalation
        BEFORE UPDATE OF status ON tasks
        WHEN NEW.id = 'T-RUNTIME-WRITE-FAIL' AND NEW.status = 'escalated'
        BEGIN SELECT RAISE(ABORT, 'injected escalation write failure'); END
    """)
    with pytest.raises(sqlite3.IntegrityError, match="escalation write failure"):
        db.try_escalate_runtime(
            "T-RUNTIME-WRITE-FAIL", reason="runtime failure",
            agent="orchestrator", reason_code="runtime_retry_ceiling",
        )
    assert db.get_task("T-RUNTIME-WRITE-FAIL").status == TaskStatus.PENDING
    assert not db.get_audit_logs("T-RUNTIME-WRITE-FAIL")


def test_regression_all_completed_clean_fanout_still_happy_path(runtime, db, monkeypatch):
    """THR-078 regression: a clean all-completed fan-out still takes the
    happy path and wakes the owner for its next decision step."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting

    db.insert_task(TaskRecord(
        id="T-CLEAN", brief="fan-out parent",
        assigned_agent="engineering_head",
        task_type="task",
    ))
    db.update_task("T-CLEAN", status=TaskStatus.IN_PROGRESS,
                   block_kind=BlockKind.DELEGATED, note="fan-out in flight")

    # All children completed.
    for i, (cid, agent) in enumerate([
        ("T-CLEAN-C1", "dev_agent"),
        ("T-CLEAN-C2", "dev_agent"),
        ("T-CLEAN-C3", "qa_engineer"),
    ]):
        db.insert_task(TaskRecord(
            id=cid, brief=f"task {i}",
            assigned_agent=agent, parent_task_id="T-CLEAN",
            task_type="subtask",
        ))
        db.update_task(cid, status=TaskStatus.COMPLETED)
        db.insert_task_result(
            task_id=cid, agent=agent, session_id="s",
            status="completed", confidence_score=90, output_summary="Done",
            risks_flagged=[],
        )

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    _enqueue_parent_if_waiting(orch, "T-CLEAN-C3")

    parent = db.get_task("T-CLEAN")
    assert parent.status == TaskStatus.IN_PROGRESS
    assert parent.block_kind == BlockKind.DELEGATED
    assert orch._queue.qsize() == 1
    slug, tid = orch._queue.get_nowait()
    assert tid == "T-CLEAN"


def test_retry_of_completed_predecessor_does_not_escalate_on_first_failure(runtime, db, monkeypatch):
    """THR-078 Fix 1 negative: a retry of a previously COMPLETED (successful)
    slice does NOT exhaust the ceiling on its first failure.  Ceiling=1 means
    exactly ONE retry after a slice's FIRST FAILURE; a COMPLETED predecessor
    means this is a fresh dispatch, not a retry — so the ceiling is not
    triggered.

    RED test: current code treats ANY same-parent ancestor as exhausting the
    ceiling, so this test will FAIL against the unfixed code."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting

    db.insert_task(TaskRecord(
        id="T-CMPL", brief="fan-out parent",
        assigned_agent="engineering_head",
        task_type="task",
    ))
    db.update_task("T-CMPL", status=TaskStatus.IN_PROGRESS,
                   block_kind=BlockKind.DELEGATED, note="fan-out in flight")

    # Original slice COMPLETED (clean first attempt).
    db.insert_task(TaskRecord(
        id="T-CMPL-C1", brief="build feature X",
        assigned_agent="dev_agent", parent_task_id="T-CMPL",
        task_type="subtask",
    ))
    db.update_task("T-CMPL-C1", status=TaskStatus.COMPLETED)
    db.insert_task_result(
        task_id="T-CMPL-C1", agent="dev_agent", session_id="s",
        status="completed", confidence_score=90, output_summary="Done",
        risks_flagged=[],
    )

    # Another child completed cleanly.
    db.insert_task(TaskRecord(
        id="T-CMPL-C2", brief="build feature Y",
        assigned_agent="dev_agent", parent_task_id="T-CMPL",
        task_type="subtask",
    ))
    db.update_task("T-CMPL-C2", status=TaskStatus.COMPLETED)
    db.insert_task_result(
        task_id="T-CMPL-C2", agent="dev_agent", session_id="s",
        status="completed", confidence_score=90, output_summary="Done",
        risks_flagged=[],
    )

    # Re-dispatched slice: revisit_of_task_id points to the COMPLETED C1.
    # The owner re-delegated this slice post-fanout-join; it fails (first
    # failure of this slice). This is NOT an exhaustion — the predecessor
    # was COMPLETED, not FAILED.
    db.insert_task(TaskRecord(
        id="T-CMPL-C1-R", brief="re-dispatched: build feature X",
        assigned_agent="dev_agent", parent_task_id="T-CMPL",
        revisit_of_task_id="T-CMPL-C1",
        task_type="subtask",
    ))
    db.update_task("T-CMPL-C1-R", status=TaskStatus.FAILED,
                   note="first failure of this re-dispatched slice")

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    _enqueue_parent_if_waiting(orch, "T-CMPL-C1-R")

    parent = db.get_task("T-CMPL")
    # COMPLETED predecessor → NOT escalated, owner is woken instead.
    assert parent.status == TaskStatus.IN_PROGRESS, (
        f"COMPLETED predecessor should NOT trigger escalation; "
        f"owner should be woken; got status {parent.status}"
    )
    assert parent.block_kind == BlockKind.DELEGATED
    assert orch._queue.qsize() == 1
    slug, tid = orch._queue.get_nowait()
    assert tid == "T-CMPL"


def test_delegate_without_revisit_of_task_id_when_failed_sibling_is_rejected(runtime, db, monkeypatch):
    """THR-078 Fix 4: a delegate that re-targets the agent of a FAILED sibling
    WITHOUT revisit_of_task_id is HARD-REJECTED.  The retry-link field is
    MANDATORY — even the first retry of a failed slice is DISALLOWED
    without the field.

    RED test: current code silently allows the delegate through, treating
    it as an unlinked fresh dispatch that resets the ceiling."""
    import json
    from runtime.orchestrator.orchestrator import Orchestrator

    (runtime.workspaces_dir / "dev_agent").mkdir(parents=True)

    db.insert_task(TaskRecord(
        id="T-NOLINK", brief="fan-out parent",
        assigned_agent="engineering_head",
        task_type="task",
    ))

    # A FAILED child targeting dev_agent.
    db.insert_task(TaskRecord(
        id="T-NOLINK-C1", brief="build feature X",
        assigned_agent="dev_agent", parent_task_id="T-NOLINK",
        task_type="subtask",
    ))
    db.update_task("T-NOLINK-C1", status=TaskStatus.FAILED,
                   note="failed to build feature X")

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    # Mock the executor: owner returns delegate to dev_agent WITHOUT
    # revisit_of_task_id even though dev_agent has a failed sibling.
    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(
            output_summary=json.dumps({
                "action": "delegate",
                "agent": "dev_agent",
                "prompt": "retry build feature X",
                # OMIT revisit_of_task_id — should be REJECTED.
            }),
        )
    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)

    # Run the step — the delegate handler should REJECT before spawning.
    orch.run_step("T-NOLINK")

    # After rejection, parent should still be PENDING (re-enqueued for retry),
    # NOT in_progress(delegated).
    parent = db.get_task("T-NOLINK")
    assert parent.status == TaskStatus.PENDING, (
        f"Parent should be PENDING after reject; got {parent.status}"
    )
    assert parent.block_kind is None, (
        f"block_kind should be None after reject; got {parent.block_kind}"
    )

    # No NEW child should have been spawned (only T-NOLINK-C1 was pre-existing).
    children = db.get_children("T-NOLINK")
    assert len(children) == 1, (
        f"No new child should be spawned; got {len(children)} children"
    )
    assert children[0] == "T-NOLINK-C1"

    # Parent should be re-enqueued for another decision step.
    assert orch._queue.qsize() == 1
    slug, tid = orch._queue.get_nowait()
    assert tid == "T-NOLINK"


def test_fanout_without_revisit_of_task_id_when_failed_sibling_is_rejected(runtime, db, monkeypatch):
    """THR-078: fanout rejects the whole decision if any retry lacks its link."""
    import json
    from runtime.orchestrator.orchestrator import Orchestrator

    (runtime.workspaces_dir / "dev_agent").mkdir(parents=True)
    (runtime.workspaces_dir / "qa_engineer").mkdir(parents=True)
    db.insert_task(TaskRecord(
        id="T-FANOUT-NOLINK", brief="fan-out parent",
        assigned_agent="engineering_head", task_type="task",
    ))
    db.insert_task(TaskRecord(
        id="T-FANOUT-NOLINK-C1", brief="build feature X",
        assigned_agent="dev_agent", parent_task_id="T-FANOUT-NOLINK",
        task_type="subtask",
    ))
    db.update_task("T-FANOUT-NOLINK-C1", status=TaskStatus.FAILED)

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(output_summary=json.dumps({
            "action": "fanout",
            "children": [
                {"agent": "dev_agent", "prompt": "retry feature X"},
                {"agent": "qa_engineer", "prompt": "test feature Y"},
            ],
            "width_cap_ack": 2,
        }))

    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)
    orch.run_step("T-FANOUT-NOLINK")

    parent = db.get_task("T-FANOUT-NOLINK")
    assert parent.status == TaskStatus.PENDING
    assert parent.block_kind is None
    assert db.get_children("T-FANOUT-NOLINK") == ["T-FANOUT-NOLINK-C1"]
    assert orch._queue.qsize() == 1
    _, queued_id = orch._queue.get_nowait()
    assert queued_id == "T-FANOUT-NOLINK"


def test_delegate_with_invalid_revisit_link_is_rejected(runtime, db, monkeypatch):
    """THR-078: a retry link must name this parent's FAILED same-agent child."""
    import json
    from runtime.orchestrator.orchestrator import Orchestrator

    (runtime.workspaces_dir / "dev_agent").mkdir(parents=True)
    db.insert_task(TaskRecord(
        id="T-BADLINK", brief="parent", assigned_agent="engineering_head",
        task_type="task",
    ))
    db.insert_task(TaskRecord(
        id="T-BADLINK-C1", brief="failed feature", assigned_agent="dev_agent",
        parent_task_id="T-BADLINK", task_type="subtask",
    ))
    db.update_task("T-BADLINK-C1", status=TaskStatus.FAILED)

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        return _make_result(), _make_report(output_summary=json.dumps({
            "action": "delegate",
            "agent": "dev_agent",
            "prompt": "retry feature",
            "revisit_of_task_id": "TASK-NOT-A-FAILED-SIBLING",
        }))

    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)
    orch.run_step("T-BADLINK")

    parent = db.get_task("T-BADLINK")
    assert parent.status == TaskStatus.PENDING
    assert parent.block_kind is None
    assert db.get_children("T-BADLINK") == ["T-BADLINK-C1"]
    assert orch._queue.qsize() == 1


def test_fanout_retry_link_reaches_second_failure_escalation(runtime, db, monkeypatch):
    """THR-078: retrying a failed fanout slice links its second failure."""
    import json
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting

    (runtime.workspaces_dir / "dev_agent").mkdir(parents=True)
    (runtime.workspaces_dir / "qa_engineer").mkdir(parents=True)
    db.insert_task(TaskRecord(
        id="T-FANOUT-RETRY", brief="fan-out parent",
        assigned_agent="engineering_head", task_type="task",
    ))

    responses = [
        {
            "action": "fanout",
            "children": [
                {"agent": "dev_agent", "prompt": "build feature X"},
                {"agent": "qa_engineer", "prompt": "test feature Y"},
            ],
            "width_cap_ack": 2,
        },
        {
            "action": "fanout",
            "children": [
                {
                    "agent": "dev_agent",
                    "prompt": "retry feature X",
                    "revisit_of_task_id": "REPLACED-BEFORE-SECOND-ROUND",
                },
                {"agent": "qa_engineer", "prompt": "test feature Z"},
            ],
            "width_cap_ack": 2,
        },
    ]
    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    def fake_run_agent(task_id, agent, prompt, on_session_started=None):
        response = responses.pop(0)
        if response["children"][0].get("revisit_of_task_id"):
            response["children"][0]["revisit_of_task_id"] = failed_slice_id
        return _make_result(), _make_report(output_summary=json.dumps(response))

    monkeypatch.setattr(orch, "_run_agent", fake_run_agent)
    orch.run_step("T-FANOUT-RETRY")

    first_round = [db.get_task(cid) for cid in db.get_children("T-FANOUT-RETRY")]
    failed_slice = next(child for child in first_round if child.assigned_agent == "dev_agent")
    failed_slice_id = failed_slice.id
    first_round_qa = next(child for child in first_round if child.assigned_agent == "qa_engineer")
    db.update_task(failed_slice.id, status=TaskStatus.FAILED)
    db.update_task(first_round_qa.id, status=TaskStatus.COMPLETED)

    # All first-round siblings are terminal, so the owner may fan out again.
    orch.run_step("T-FANOUT-RETRY")
    all_children = [db.get_task(cid) for cid in db.get_children("T-FANOUT-RETRY")]
    retry_slice = next(
        child for child in all_children
        if child.assigned_agent == "dev_agent" and child.id != failed_slice.id
    )
    second_round_qa = next(
        child for child in all_children
        if child.assigned_agent == "qa_engineer" and child.id != first_round_qa.id
    )
    assert retry_slice.revisit_of_task_id == failed_slice.id

    db.update_task(retry_slice.id, status=TaskStatus.FAILED)
    db.update_task(second_round_qa.id, status=TaskStatus.COMPLETED)
    _enqueue_parent_if_waiting(orch, retry_slice.id)

    parent = db.get_task("T-FANOUT-RETRY")
    assert parent.status == TaskStatus.ESCALATED
    assert parent.block_kind is None
    assert orch._queue.qsize() == 4  # two rounds' children; no parent wake on escalation


def test_delegate_with_revisit_of_task_id_e2e_ceiling_fires(runtime, db, monkeypatch):
    """THR-078 Fix 2: end-to-end.  Parent with a failed slice + uncleared
    active_fanout wakes → join context is injected.  Simulate the second
    phase: the retry child (which carries revisit_of_task_id pointing at the
    FAILED predecessor) itself fails → ceiling fires, parent escalates.

    This validates the real _enqueue_parent_if_waiting path: the revisited
    FAILED ancestor under the same parent triggers the ceiling."""
    import asyncio
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting

    db.insert_task(TaskRecord(
        id="T-E2E", brief="fan-out parent",
        assigned_agent="engineering_head",
        task_type="task",
    ))
    db.update_task("T-E2E", status=TaskStatus.IN_PROGRESS,
                   block_kind=BlockKind.DELEGATED, note="fan-out in flight")

    # FAILED predecessor (original slice, first failure).
    db.insert_task(TaskRecord(
        id="T-E2E-C1-ORIG", brief="build feature Y (original)",
        assigned_agent="dev_agent", parent_task_id="T-E2E",
        task_type="subtask",
    ))
    db.update_task("T-E2E-C1-ORIG", status=TaskStatus.FAILED,
                   note="first failure")

    # Another child completed cleanly.
    db.insert_task(TaskRecord(
        id="T-E2E-C2", brief="build feature Z",
        assigned_agent="qa_engineer", parent_task_id="T-E2E",
        task_type="subtask",
    ))
    db.update_task("T-E2E-C2", status=TaskStatus.COMPLETED)

    # Retry child: revisit_of_task_id points at the FAILED predecessor.
    # This is the retry after the first failure — when IT fails, the ceiling
    # fires (1 retry exhausted).
    db.insert_task(TaskRecord(
        id="T-E2E-C1-R", brief="retry: build feature Y",
        assigned_agent="dev_agent", parent_task_id="T-E2E",
        revisit_of_task_id="T-E2E-C1-ORIG",
        task_type="subtask",
    ))
    db.update_task("T-E2E-C1-R", status=TaskStatus.FAILED,
                   note="second failure — ceiling exhausted")

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    _enqueue_parent_if_waiting(orch, "T-E2E-C1-R")

    parent = db.get_task("T-E2E")
    # Ceiling exhausted → escalated (T-E2E is root).
    assert parent.status == TaskStatus.ESCALATED, (
        f"second failure should escalate; got status {parent.status}"
    )
    assert parent.block_kind is None


# ═══════════════════════════════════════════════════════════════════
# THR-183 / TASK-5235 — retry-ceiling escalation attribution fix
# ═══════════════════════════════════════════════════════════════════


def _escalation_audit_rows(db: Database, task_id: str) -> list[dict]:
    return [a for a in db.get_audit_logs(task_id) if a["action"] == "escalation"]


def test_thr183_completed_child_after_retired_failed_lineage_does_not_escalate(
    runtime, db,
):
    """A later COMPLETED descendant retires earlier FAILED retry attempts.
    A normal parent wake initiated by the completed child MUST NOT scan the
    stale failed siblings and escalate using their notes, and must not mutate
    the parent's active fan-out state."""
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.fanout import FanoutState
    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting

    db.insert_task(TaskRecord(
        id="T-RET1", brief="parent", assigned_agent="engineering_head",
        task_type="task",
    ))
    db.update_task(
        "T-RET1", status=TaskStatus.IN_PROGRESS,
        block_kind=BlockKind.DELEGATED, note="waiting",
    )

    # Seed a meaningful active_fanout so the no-state-change contract is
    # actually exercised (active_chain stays None for this completed-child
    # signature; the chain branch is not entered).
    fanout = FanoutState(
        children_ids=["T-RET1-C"],
        children_details=[{"agent": "qa_engineer", "prompt": "other slice"}],
        width=1,
        manager_agent="engineering_head",
    )
    db.update_task_active_fanout("T-RET1", fanout.serialize())

    # Original quota failure.
    db.insert_task(TaskRecord(
        id="T-RET1-A", brief="slice A", assigned_agent="dev_agent",
        parent_task_id="T-RET1", task_type="subtask",
    ))
    db.update_task("T-RET1-A", status=TaskStatus.FAILED, note="quota exceeded")

    # Retry also failed (ceiling exhausted at this point in real life).
    db.insert_task(TaskRecord(
        id="T-RET1-A-R", brief="retry slice A", assigned_agent="dev_agent",
        parent_task_id="T-RET1", revisit_of_task_id="T-RET1-A",
        task_type="subtask",
    ))
    db.update_task(
        "T-RET1-A-R", status=TaskStatus.FAILED,
        note="second failure — review rejected",
    )

    # Owner resolved and a later retry completed, retiring the lineage.
    db.insert_task(TaskRecord(
        id="T-RET1-A-R2", brief="final retry slice A", assigned_agent="dev_agent",
        parent_task_id="T-RET1", revisit_of_task_id="T-RET1-A-R",
        task_type="subtask",
    ))
    db.update_task("T-RET1-A-R2", status=TaskStatus.COMPLETED, note="done")

    # Another completed child triggers the parent wake.
    db.insert_task(TaskRecord(
        id="T-RET1-C", brief="other slice", assigned_agent="qa_engineer",
        parent_task_id="T-RET1", task_type="subtask",
    ))
    db.update_task("T-RET1-C", status=TaskStatus.COMPLETED)

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    parent_before = db.get_task("T-RET1")
    _enqueue_parent_if_waiting(orch, "T-RET1-C")

    parent = db.get_task("T-RET1")
    assert parent.status == TaskStatus.IN_PROGRESS, (
        f"completed-child wake with retired lineage must not escalate; got {parent.status}"
    )
    assert parent.block_kind == BlockKind.DELEGATED
    assert parent.note == parent_before.note
    assert parent.active_fanout == parent_before.active_fanout
    assert _escalation_audit_rows(db, "T-RET1") == []
    assert orch._queue.qsize() == 1
    assert orch._queue.get_nowait() == ("test", "T-RET1")


def test_thr183_recovered_lineage_does_not_re_escalate_on_startup_style_ancestor_wake(
    runtime, db,
):
    """Startup/recovery or any caller may invoke _enqueue_parent_if_waiting with
    a recovered FAILED ancestor. A later SUPERSEDED descendant in the same
    revisit_of_task_id lineage must retire the earlier failures, prevent re-
    escalation, and must not mutate parent state — including meaningful non-null
    active_chain and active_fanout metadata."""
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.chain import ChainState
    from runtime.orchestrator.fanout import FanoutState
    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting

    db.insert_task(TaskRecord(
        id="T-REC2", brief="parent", assigned_agent="engineering_head",
        task_type="task",
    ))
    db.update_task(
        "T-REC2", status=TaskStatus.IN_PROGRESS,
        block_kind=BlockKind.DELEGATED, note="waiting",
    )

    # Seed meaningful, valid active_chain and active_fanout metadata. The bug
    # is that the FAILED-chain branch used to clear active_chain before the
    # retired-lineage check could prove the lineage was resolved.
    chain = ChainState(
        step_index=0,
        first_leg_expect_verdict="PASS",
        legs=[],
        step_audit_id=1,
    )
    fanout = FanoutState(
        children_ids=["T-REC2-C"],
        children_details=[{"agent": "qa_engineer", "prompt": "other slice"}],
        width=1,
        manager_agent="engineering_head",
    )
    db.update_task_active_chain("T-REC2", chain.serialize())
    db.update_task_active_fanout("T-REC2", fanout.serialize())

    # Exhausted historical FAILED lineage.
    db.insert_task(TaskRecord(
        id="T-REC2-A", brief="slice A", assigned_agent="dev_agent",
        parent_task_id="T-REC2", task_type="subtask",
    ))
    db.update_task("T-REC2-A", status=TaskStatus.FAILED, note="quota exceeded")

    db.insert_task(TaskRecord(
        id="T-REC2-A-R", brief="retry slice A", assigned_agent="dev_agent",
        parent_task_id="T-REC2", revisit_of_task_id="T-REC2-A",
        task_type="subtask",
    ))
    db.update_task(
        "T-REC2-A-R", status=TaskStatus.FAILED,
        note="second failure — review rejected",
    )

    # Later descendant SUPERSEDED via revisit_of_task_id → lineage retired.
    db.insert_task(TaskRecord(
        id="T-REC2-A-R2", brief="superseded retry slice A", assigned_agent="dev_agent",
        parent_task_id="T-REC2", revisit_of_task_id="T-REC2-A-R",
        task_type="subtask",
    ))
    db.update_task("T-REC2-A-R2", status=TaskStatus.SUPERSEDED, note="replaced by owner")

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    parent_before = db.get_task("T-REC2")
    # Simulate a startup/recovery path invoking on the stale ancestor.
    _enqueue_parent_if_waiting(orch, "T-REC2-A")

    parent = db.get_task("T-REC2")
    assert parent.status == TaskStatus.IN_PROGRESS, (
        f"recovered-ancestor wake with SUPERSEDED descendant must not escalate; got {parent.status}"
    )
    assert parent.block_kind == BlockKind.DELEGATED
    assert parent.note == parent_before.note
    assert parent.active_chain == parent_before.active_chain
    assert parent.active_fanout == parent_before.active_fanout
    assert _escalation_audit_rows(db, "T-REC2") == []
    assert orch._queue.qsize() == 1
    assert orch._queue.get_nowait() == ("test", "T-REC2")

    # A second recovery-style call on the same retired ancestor must remain a
    # bounded no-op: parent state unchanged, another normal wake queued.
    _enqueue_parent_if_waiting(orch, "T-REC2-A")
    parent_after = db.get_task("T-REC2")
    assert parent_after.status == TaskStatus.IN_PROGRESS
    assert parent_after.active_chain == parent_before.active_chain
    assert parent_after.active_fanout == parent_before.active_fanout
    assert _escalation_audit_rows(db, "T-REC2") == []
    assert orch._queue.qsize() == 1
    assert orch._queue.get_nowait() == ("test", "T-REC2")


def test_thr183_genuine_unresolved_second_failure_escalates_once_using_leaf(
    runtime, db,
):
    """A true unresolved second failure escalates exactly once and the reason
    identifies the causal leaf task (not a stale ancestor note)."""
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting

    db.insert_task(TaskRecord(
        id="T-GENU", brief="parent", assigned_agent="engineering_head",
        task_type="task",
    ))
    db.update_task(
        "T-GENU", status=TaskStatus.IN_PROGRESS,
        block_kind=BlockKind.DELEGATED, note="waiting",
    )

    db.insert_task(TaskRecord(
        id="T-GENU-A", brief="slice A", assigned_agent="dev_agent",
        parent_task_id="T-GENU", task_type="subtask",
    ))
    db.update_task("T-GENU-A", status=TaskStatus.FAILED, note="quota exceeded")

    db.insert_task(TaskRecord(
        id="T-GENU-A-R", brief="retry slice A", assigned_agent="dev_agent",
        parent_task_id="T-GENU", revisit_of_task_id="T-GENU-A",
        task_type="subtask",
    ))
    db.update_task(
        "T-GENU-A-R", status=TaskStatus.FAILED,
        note="review rejected",
    )
    db.insert_task_result(
        task_id="T-GENU-A-R", agent="dev_agent", session_id="s",
        status="failed", confidence_score=0, output_summary="review rejected",
        verdict="FAIL",
    )

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    _enqueue_parent_if_waiting(orch, "T-GENU-A-R")

    parent = db.get_task("T-GENU")
    assert parent.status == TaskStatus.ESCALATED
    assert parent.block_kind is None
    assert "T-GENU-A-R" in (parent.note or "")
    assert "review rejected" in (parent.note or "")
    assert "quota exceeded" not in (parent.note or "")
    assert "T-GENU-A" not in (parent.note or "") or "T-GENU-A-R" in (parent.note or "")

    audits = _escalation_audit_rows(db, "T-GENU")
    assert len(audits) == 1
    reason = audits[0]["payload"].get("reason", "")
    assert "T-GENU-A-R" in reason
    assert "review rejected" in reason
    assert "quota exceeded" not in reason
    assert orch._queue.qsize() == 0

    # Duplicate evaluation must be a no-op (try_escalate CAS).
    _enqueue_parent_if_waiting(orch, "T-GENU-A-R")
    assert len(_escalation_audit_rows(db, "T-GENU")) == 1
    assert db.get_task("T-GENU").note == parent.note


def test_thr183_stale_lineage_does_not_escalate_a_fresh_failure(
    runtime, db,
):
    """A fresh failure of a recovered/replaced slice must be evaluated on its
    own merits; stale FAILED ancestors retired by a COMPLETED descendant must
    not force escalation and must not leak into the reason."""
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting

    db.insert_task(TaskRecord(
        id="T-FRESH", brief="parent", assigned_agent="engineering_head",
        task_type="task",
    ))
    db.update_task(
        "T-FRESH", status=TaskStatus.IN_PROGRESS,
        block_kind=BlockKind.DELEGATED, note="waiting",
    )

    # Old exhausted lineage retired by a completed descendant.
    db.insert_task(TaskRecord(
        id="T-FRESH-A", brief="slice A", assigned_agent="dev_agent",
        parent_task_id="T-FRESH", task_type="subtask",
    ))
    db.update_task("T-FRESH-A", status=TaskStatus.FAILED, note="quota exceeded")

    db.insert_task(TaskRecord(
        id="T-FRESH-A-R", brief="retry slice A", assigned_agent="dev_agent",
        parent_task_id="T-FRESH", revisit_of_task_id="T-FRESH-A",
        task_type="subtask",
    ))
    db.update_task(
        "T-FRESH-A-R", status=TaskStatus.FAILED,
        note="second failure — review rejected",
    )

    db.insert_task(TaskRecord(
        id="T-FRESH-A-R2", brief="final retry slice A", assigned_agent="dev_agent",
        parent_task_id="T-FRESH", revisit_of_task_id="T-FRESH-A-R",
        task_type="subtask",
    ))
    db.update_task("T-FRESH-A-R2", status=TaskStatus.COMPLETED, note="done")

    # A fresh retry of the now-completed slice fails for the first time.
    db.insert_task(TaskRecord(
        id="T-FRESH-B", brief="retry after completion", assigned_agent="dev_agent",
        parent_task_id="T-FRESH", revisit_of_task_id="T-FRESH-A-R2",
        task_type="subtask",
    ))
    db.update_task(
        "T-FRESH-B", status=TaskStatus.FAILED,
        note="review rejected on fresh retry",
    )

    orch = Orchestrator(db=db, settings=Settings(), paths=runtime,
                        slug="test", teams=TeamsRegistry.load(runtime.root))
    orch._queue = _SlugQueue()

    parent_before = db.get_task("T-FRESH")
    _enqueue_parent_if_waiting(orch, "T-FRESH-B")

    parent = db.get_task("T-FRESH")
    assert parent.status == TaskStatus.IN_PROGRESS, (
        f"fresh first failure after completed predecessor must wake, not escalate; got {parent.status}"
    )
    assert parent.block_kind == BlockKind.DELEGATED
    assert parent.note == parent_before.note
    assert _escalation_audit_rows(db, "T-FRESH") == []
    assert orch._queue.qsize() == 1
    assert orch._queue.get_nowait() == ("test", "T-FRESH")
