"""Tests for the ongoing zombie-task reaper (THR-090 Track B).

Covers the state machine: predicate, allowlist, warm-up grace,
fingerprint-tiered confidence, flag-then-cancel-on-TTL, and never-false-reap
guards.
"""
from __future__ import annotations

import os
import time as _time
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import MagicMock, patch

from runtime.daemon.zombie_reaper import (
    FLAG_TTL_FINGERPRINT_SECONDS,
    FLAG_TTL_NO_FINGERPRINT_SECONDS,
    STALE_HEARTBEAT_SECONDS,
    _consume_zombie_fingerprint,
    _sweep_org_zombies,
)
from runtime.infrastructure.database import Database
from runtime.models import BlockKind, TaskRecord, TaskStatus, ThreadRecord

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(seconds: int) -> datetime:
    return _now() - timedelta(seconds=seconds)


ZOMBIE_PID = 99999  # guaranteed-non-existent pid


def _seed_zombie_authority_result(tmp_path, *, self_evaluation="valid"):
    import json

    from runtime.config import Settings
    from runtime.daemon.queue import TaskQueue
    from runtime.orchestrator._paths import OrgPaths
    from runtime.orchestrator.active_authority_policy import (
        SELF_EVALUATION_CONTRACT_DIGEST,
        SELF_EVALUATION_CONTRACT_ID,
        SELF_EVALUATION_CONTRACT_VERSION,
        ActivePolicySnapshot,
        persist_session_policy_binding,
    )
    from runtime.orchestrator.authority_policy import CONTINUE_ROUTINE_PHRASE
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.teams import TeamsRegistry
    from runtime.runtime import RuntimeDir
    from tests.authority_policy_test_factory import activate_test_policy

    paths = OrgPaths(root=RuntimeDir.init(tmp_path / "rt").orgs_dir / "test")
    paths.teams_config_path.parent.mkdir(parents=True, exist_ok=True)
    paths.teams_config_path.write_text(
        "teams:\n  engineering:\n    manager: engineering_manager\n"
        "    workers: [dev_agent]\n"
    )
    db = Database(paths.db_path)
    orch = Orchestrator(
        db=db, settings=Settings(), paths=paths, slug="test",
        teams=TeamsRegistry.load(paths.root),
    )
    orch._queue = TaskQueue()
    task_id = "T-ZOMBIE-SELF-EVAL"
    manager = "engineering_manager"
    session_id = "sess-zombie-self-eval"
    db.insert_thread(ThreadRecord(id="THR-ZOMBIE-RECOVERY", subject="recovery"))
    db.insert_task(TaskRecord(
        id=task_id, brief="zombie test", team="engineering",
        assigned_agent=manager, status=TaskStatus.IN_PROGRESS,
        dispatched_from_thread_id="THR-ZOMBIE-RECOVERY",
    ))
    from runtime.infrastructure.audit_logger import AuditLogger
    AuditLogger(db).log_thread_dispatch(
        "THR-ZOMBIE-RECOVERY", task_id=task_id, dispatcher=manager,
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
        evaluation["policy_digest"] = "0" * 64
    decision = {"action": "escalate", "reason": CONTINUE_ROUTINE_PHRASE}
    if self_evaluation in {"valid", "mismatch"}:
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
    return db, orch, task_id, row


def _zombie_outcome(db, task_id):
    candidates = db.list_authority_candidates_for_root(task_id)
    assert len(candidates) == 1
    candidate = candidates[0]
    evaluation = db.get_authority_evaluation(candidate.id)
    assert evaluation is not None
    rows = [r for r in db.get_audit_logs(task_id) if r["action"] == "authority_hook"]
    assert len(rows) == 1
    return candidate, evaluation, rows[0]["payload"]


def test_consume_zombie_fingerprint_runs_real_authority_path(tmp_path):
    db, orch, task_id, fingerprint = _seed_zombie_authority_result(tmp_path)

    _consume_zombie_fingerprint(
        db, task_id, fingerprint, db.get_task(task_id), orch,
    )

    assert db.get_task(task_id).status is TaskStatus.PENDING
    assert orch._queue._queue.get_nowait() == ("test", task_id, None)
    candidate, evaluation, outcome = _zombie_outcome(db, task_id)
    assert outcome["outcome"] == "continued_same_root"
    assert evaluation.disposition.value == "continue_same_root"
    assert candidate.causal_event_id == f"result:{fingerprint['id']}"
    assert [event.event_type for event in db.list_authority_audit(candidate.id)] == [
        "candidate_claimed", "evaluation_recorded", "candidate_consumed",
    ]
    assert not db.list_thread_messages("THR-ZOMBIE-RECOVERY")


@pytest.mark.parametrize("self_evaluation", ["absent", "malformed", "mismatch"])
def test_consume_zombie_fingerprint_invalid_evidence_fails_closed(
    tmp_path, self_evaluation,
):
    db, orch, task_id, fingerprint = _seed_zombie_authority_result(
        tmp_path, self_evaluation=self_evaluation,
    )

    _consume_zombie_fingerprint(
        db, task_id, fingerprint, db.get_task(task_id), orch,
    )

    assert db.get_task(task_id).status is TaskStatus.ESCALATED
    assert orch._queue._queue.empty()
    _, evaluation, outcome = _zombie_outcome(db, task_id)
    assert outcome["outcome"] == "escalated"
    assert outcome["error"]
    assert evaluation.disposition.value == "escalate"
    messages = db.list_thread_messages("THR-ZOMBIE-RECOVERY")
    assert len(messages) == 1
    assert messages[0].system_payload["kind_tag"] == "task_escalated"


def test_consume_zombie_fingerprint_replay_cannot_continue_twice(tmp_path):
    db, orch, task_id, fingerprint = _seed_zombie_authority_result(tmp_path)
    task = db.get_task(task_id)
    _consume_zombie_fingerprint(db, task_id, fingerprint, task, orch)
    assert orch._queue._queue.get_nowait() == ("test", task_id, None)
    db.update_task(task_id, status=TaskStatus.IN_PROGRESS, block_kind=None)

    _consume_zombie_fingerprint(db, task_id, fingerprint, db.get_task(task_id), orch)

    assert db.get_task(task_id).status is TaskStatus.ESCALATED
    assert orch._queue._queue.empty()
    assert len(db.list_authority_candidates_for_root(task_id)) == 1
    assert db.execute("SELECT COUNT(*) FROM authority_evaluations").fetchone()[0] == 1


@pytest.mark.parametrize("agent", ["dev_agent", "engineering_manager"])
def test_consume_zombie_fingerprint_worker_and_legacy_compatibility(tmp_path, agent):
    db, orch, task_id, _ = _seed_zombie_authority_result(tmp_path)
    compat_id = f"T-ZOMBIE-COMPAT-{agent}"
    session_id = f"sess-{agent}"
    db.insert_task(TaskRecord(
        id=compat_id, brief="compat", team="engineering",
        assigned_agent=agent, status=TaskStatus.IN_PROGRESS,
        task_type="subtask",
    ))
    db.update_task(compat_id, current_session_id=session_id)
    db.insert_task_result(
        task_id=compat_id, agent=agent, session_id=session_id,
        status="completed", confidence_score=90, output_summary="done",
    )
    fingerprint = db.get_latest_task_result(compat_id, agent, session_id)

    _consume_zombie_fingerprint(
        db, compat_id, fingerprint, db.get_task(compat_id), orch,
    )

    assert db.get_task(compat_id).status is TaskStatus.COMPLETED
    assert db.list_authority_candidates_for_root(compat_id) == []


def _fresh_hb() -> datetime:
    """A heartbeat that is definitely fresh (just now)."""
    return _now()


def _stale_hb() -> datetime:
    """A heartbeat that is definitely stale (older than threshold)."""
    return _ago(STALE_HEARTBEAT_SECONDS + 10)


def _insert_zombie_candidate(
    db: Database,
    task_id: str = "T-ZOMBIE",
    *,
    status: TaskStatus = TaskStatus.IN_PROGRESS,
    last_heartbeat: datetime | None = None,
    executor_pid: int | None = None,
    current_session_id: str | None = "sess-dead",
    assigned_agent: str = "dev_agent",
    zombie_flagged_at: datetime | None = None,
) -> None:
    db.insert_task(TaskRecord(
        id=task_id, brief="zombie test", team="engineering",
        assigned_agent=assigned_agent, status=status,
    ))
    # last_heartbeat, executor_pid, current_session_id are set via update_task
    # (they are not part of insert_task's INSERT statement).
    update_kwargs: dict = {
        "current_session_id": current_session_id,
    }
    if last_heartbeat is not None:
        update_kwargs["last_heartbeat"] = last_heartbeat.isoformat()
    if executor_pid is not None:
        update_kwargs["executor_pid"] = executor_pid
    db.update_task(task_id, **update_kwargs)
    if zombie_flagged_at is not None:
        db.update_task(task_id, zombie_flagged_at=zombie_flagged_at.isoformat())


def _insert_task_result(db: Database, task_id: str, agent: str,
                        session_id: str, status: str = "completed") -> None:
    db.insert_task_result(
        task_id=task_id, agent=agent, session_id=session_id,
        status=status, confidence_score=90, output_summary="ok",
    )


def test_zombie_consumer_rejects_thread_originated_manager_supersession(
    tmp_path, monkeypatch,
):
    """The zombie path durably escalates the phase-1 rejection once."""
    import json

    from runtime.config import Settings
    from runtime.orchestrator._paths import OrgPaths
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.teams import TeamsRegistry
    from runtime.runtime import RuntimeDir

    rt = RuntimeDir.init(tmp_path / "rt")
    paths = OrgPaths(root=rt.orgs_dir / "test")
    paths.teams_config_path.parent.mkdir(parents=True, exist_ok=True)
    paths.teams_config_path.write_text(
        "teams:\n  engineering:\n    manager: engineering_head\n    workers: [dev_agent]\n"
    )
    db = Database(paths.db_path)
    db.insert_task(TaskRecord(
        id="T-ZOMBIE-SUP", brief="original", team="engineering",
        assigned_agent="engineering_head", status=TaskStatus.IN_PROGRESS,
        current_session_id="sess-zombie",
    ))
    db.execute(
        "UPDATE tasks SET dispatched_from_thread_id = 'THR-152' WHERE id = 'T-ZOMBIE-SUP'"
    )
    db._conn.commit()
    orch = Orchestrator(
        db=db, settings=Settings(), paths=paths, slug="test",
        teams=TeamsRegistry.load(paths.root),
    )
    orch._queue = MagicMock()
    monkeypatch.setenv("HAPPYRANCH_MANAGER_SUPERSESSION_ENABLED", "1")
    monkeypatch.setenv("HAPPYRANCH_MANAGER_SUPERSESSION_PILOT_TEAM", "engineering")

    _consume_zombie_fingerprint(
        db,
        "T-ZOMBIE-SUP",
        {
            "agent": "engineering_head",
            "status": "completed",
            "confidence_score": 90,
            "output_summary": "replace",
            "decision_json": json.dumps({
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
        },
        db.get_task("T-ZOMBIE-SUP"),
        orch,
    )

    task = db.get_task("T-ZOMBIE-SUP")
    assert task.status is TaskStatus.ESCALATED
    assert task.note == (
        "manager supersession rejected: thread-origin roots are not eligible "
        "for supersession; founder action required"
    )
    assert db.execute("SELECT COUNT(*) FROM manager_supersessions").fetchone()[0] == 0
    orch._queue.put_nowait.assert_not_called()
    logs = db.get_audit_logs("T-ZOMBIE-SUP")
    assert [row["action"] for row in logs].count("orchestration_step") == 1
    assert [row["action"] for row in logs].count("escalation") == 1
    authority = next(row for row in logs if row["action"] == "authority_hook")
    assert authority["payload"]["reason_code"] == (
        "runtime_manager_supersession_thread_origin_ineligible"
    )

    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30,
                       orchestrator=orch)
    replay_logs = db.get_audit_logs("T-ZOMBIE-SUP")
    assert [row["action"] for row in replay_logs].count("orchestration_step") == 1
    assert [row["action"] for row in replay_logs].count("escalation") == 1


# ---------------------------------------------------------------------------
# predicate — allowlist + AND-gate
# ---------------------------------------------------------------------------

def test_healthy_in_progress_fresh_hb_untouched(db: Database):
    """A task with fresh heartbeat + live pid is not a zombie."""
    _insert_zombie_candidate(db, "T-1", last_heartbeat=_fresh_hb(),
                             executor_pid=os.getpid())
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-1")
    assert t.status == TaskStatus.IN_PROGRESS
    assert t.zombie_flagged_at is None


def test_dead_pid_stale_hb_fresh_hb_pid_alive_not_flagged(db: Database):
    """A task with a dead pid (not alive) + stale heartbeat is zombie,
    but a task with a live pid + stale heartbeat is NOT (pid-gated)."""
    # Dead pid + stale hb → flagged
    _insert_zombie_candidate(db, "T-DEAD", last_heartbeat=_stale_hb(),
                             executor_pid=ZOMBIE_PID)
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-DEAD")
    assert t.zombie_flagged_at is not None  # flagged

    # Live pid + fresh hb → not flagged
    _insert_zombie_candidate(db, "T-LIVE", last_heartbeat=_fresh_hb(),
                             executor_pid=os.getpid())
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t2 = db.get_task("T-LIVE")
    assert t2.zombie_flagged_at is None


def test_blocked_task_not_touched(db: Database):
    """Tasks with block_kind set (delegated/blocked_on_job) are never zombies."""
    for bk in ["delegated", "blocked_on_job"]:
        tid = f"T-BLOCK-{bk}"
        db.insert_task(TaskRecord(
            id=tid, brief="x", team="engineering",
            assigned_agent="dev_agent", status=TaskStatus.IN_PROGRESS,
            block_kind=bk,  # type: ignore[arg-type]
            last_heartbeat=_stale_hb(), executor_pid=ZOMBIE_PID,
        ))
        _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
        t = db.get_task(tid)
        assert t.zombie_flagged_at is None, f"block_kind={bk} should be excluded"


def test_terminal_task_not_touched(db: Database):
    """Terminal states (completed, failed, cancelled) are never zombies."""
    for st in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
        tid = f"T-TERM-{st.value}"
        db.insert_task(TaskRecord(
            id=tid, brief="x", team="engineering",
            status=st, last_heartbeat=_stale_hb(), executor_pid=ZOMBIE_PID,
        ))
        _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
        t = db.get_task(tid)
        assert t.zombie_flagged_at is None, f"terminal {st} should be excluded"


def test_pending_task_not_touched(db: Database):
    """Pending tasks are never zombies (not yet in_progress)."""
    _insert_zombie_candidate(db, "T-PEND", status=TaskStatus.PENDING,
                             last_heartbeat=_stale_hb(), executor_pid=ZOMBIE_PID)
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-PEND")
    assert t.zombie_flagged_at is None


def test_escalated_task_not_touched(db: Database):
    """Escalated tasks are never zombies."""
    _insert_zombie_candidate(db, "T-ESC", status=TaskStatus.ESCALATED,
                             last_heartbeat=_stale_hb(), executor_pid=ZOMBIE_PID)
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-ESC")
    assert t.zombie_flagged_at is None


# ---------------------------------------------------------------------------
# zombie detection → flag
# ---------------------------------------------------------------------------

def test_zombie_flagged_on_first_detection(db: Database):
    """A zombie (dead pid + stale hb + not flagged yet) gets flagged but NOT cancelled."""
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=_stale_hb(),
                             executor_pid=ZOMBIE_PID)
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-Z")
    assert t.zombie_flagged_at is not None, "should be flagged on first detection"
    assert t.status == TaskStatus.IN_PROGRESS, "should NOT be cancelled on first flag"
    # Audit row emitted
    actions = [r["action"] for r in db.get_audit_logs("T-Z")]
    assert "zombie_flagged" in actions


def test_zombie_not_double_flagged(db: Database):
    """A zombie that was already flagged is not re-flagged (idempotent flag)."""
    flag_time = _ago(10)
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=_stale_hb(),
                             executor_pid=ZOMBIE_PID,
                             zombie_flagged_at=flag_time)
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-Z")
    # Flag time should be unchanged (idempotent)
    assert t.zombie_flagged_at == flag_time


# ---------------------------------------------------------------------------
# flag-then-cancel-on-TTL: no fingerprint (long TTL)
# ---------------------------------------------------------------------------

def test_zombie_cancelled_after_ttl_no_fingerprint(db: Database):
    """Zombie without task_result fingerprint → cancelled after longer TTL."""
    flag_time = _ago(FLAG_TTL_NO_FINGERPRINT_SECONDS + 5)
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=_stale_hb(),
                             executor_pid=ZOMBIE_PID,
                             zombie_flagged_at=flag_time)
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-Z")
    assert t.status == TaskStatus.CANCELLED
    assert t.cancelled_at is not None
    actions = [r["action"] for r in db.get_audit_logs("T-Z")]
    assert "zombie_cancelled" in actions


def test_zombie_not_cancelled_before_ttl_no_fingerprint(db: Database):
    """Zombie flagged recently → not yet past TTL → NOT cancelled."""
    flag_time = _ago(FLAG_TTL_NO_FINGERPRINT_SECONDS - 10)  # still within window
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=_stale_hb(),
                             executor_pid=ZOMBIE_PID,
                             zombie_flagged_at=flag_time)
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-Z")
    assert t.status == TaskStatus.IN_PROGRESS, "should NOT be cancelled before TTL"
    assert t.zombie_flagged_at == flag_time


# ---------------------------------------------------------------------------
# flag-then-cancel-on-TTL: WITH fingerprint (short TTL → consume)
# ---------------------------------------------------------------------------

def test_zombie_with_fingerprint_consumed_after_short_ttl(db: Database):
    """Zombie with task_result fingerprint → consumed (not cancelled) after short TTL."""
    flag_time = _ago(FLAG_TTL_FINGERPRINT_SECONDS + 5)
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=_stale_hb(),
                             executor_pid=ZOMBIE_PID,
                             zombie_flagged_at=flag_time,
                             current_session_id="sess-fp")
    _insert_task_result(db, "T-Z", "dev_agent", "sess-fp", status="completed")
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-Z")
    # The sweep matched the fingerprint → should NOT cancel; the result
    # should be consumed (honored). Since we don't have an orchestrator
    # in unit tests, the completion report consumption path won't fully
    # run but the fingerprint tier should still be detected and the
    # task should NOT be in cancelled.
    assert t.status != TaskStatus.CANCELLED, (
        "fingerprint present → should consume/honor, not cancel"
    )


def test_zombie_with_fingerprint_not_cancelled_even_after_long_ttl(db: Database):
    """Even after long TTL, fingerprint-present zombie should not be cancelled
    — it should be consumed/honored."""
    flag_time = _ago(FLAG_TTL_NO_FINGERPRINT_SECONDS + 60)
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=_stale_hb(),
                             executor_pid=ZOMBIE_PID,
                             zombie_flagged_at=flag_time,
                             current_session_id="sess-fp")
    _insert_task_result(db, "T-Z", "dev_agent", "sess-fp", status="completed")
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-Z")
    assert t.status != TaskStatus.CANCELLED, (
        "fingerprint present → should never cancel, always honor"
    )


# ---------------------------------------------------------------------------
# recovery → clear flag
# ---------------------------------------------------------------------------

def test_flag_cleared_on_heartbeat_recovery(db: Database):
    """If a flagged zombie gets a fresh heartbeat → clear the flag."""
    flag_time = _ago(60)
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=_fresh_hb(),  # now fresh!
                             executor_pid=ZOMBIE_PID,
                             zombie_flagged_at=flag_time)
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-Z")
    assert t.zombie_flagged_at is None, "flag should be cleared on recovery"
    actions = [r["action"] for r in db.get_audit_logs("T-Z")]
    assert "zombie_cleared" in actions


def test_flag_cleared_on_pid_recovery(db: Database):
    """If a flagged zombie's pid becomes alive → clear the flag."""
    flag_time = _ago(60)
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=_stale_hb(),
                             executor_pid=os.getpid(),  # alive!
                             zombie_flagged_at=flag_time)
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-Z")
    assert t.zombie_flagged_at is None, "flag should be cleared when pid is alive"


# ---------------------------------------------------------------------------
# warm-up grace
# ---------------------------------------------------------------------------

def test_warm_up_window_exempt(db: Database):
    """During warm-up (uptime < 1 heartbeat interval), no tasks are flagged."""
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=_stale_hb(),
                             executor_pid=ZOMBIE_PID)
    _sweep_org_zombies(db, now=_now(), uptime=10, warm_up_seconds=30)
    t = db.get_task("T-Z")
    assert t.zombie_flagged_at is None, "warm-up window should exempt"


def test_after_warm_up_detects(db: Database):
    """After warm-up (uptime >= 1 heartbeat interval), zombies are detected."""
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=_stale_hb(),
                             executor_pid=ZOMBIE_PID)
    _sweep_org_zombies(db, now=_now(), uptime=31, warm_up_seconds=30)
    t = db.get_task("T-Z")
    assert t.zombie_flagged_at is not None, "should detect after warm-up"


# ---------------------------------------------------------------------------
# never-false-reap: no executor_pid → not flagged (err toward miss)
# ---------------------------------------------------------------------------

def test_no_executor_pid_not_flagged(db: Database):
    """Without an executor_pid, we can't probe → err toward miss, don't flag."""
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=_stale_hb(),
                             executor_pid=None)
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-Z")
    assert t.zombie_flagged_at is None, "NULL pid → err toward miss"


def test_no_last_heartbeat_not_flagged(db: Database):
    """Without a last_heartbeat, we can't determine staleness → don't flag."""
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=None,
                             executor_pid=ZOMBIE_PID)
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-Z")
    assert t.zombie_flagged_at is None, "NULL heartbeat → err toward miss"


def test_stale_but_alive_pid_not_flagged(db: Database):
    """Stale heartbeat + ALIVE pid → NOT flagged (pid probe is definitive)."""
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=_stale_hb(),
                             executor_pid=os.getpid())
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-Z")
    assert t.zombie_flagged_at is None, "live pid should never be flagged"


# ---------------------------------------------------------------------------
# probe edge cases
# ---------------------------------------------------------------------------

def test_permission_error_pid_probe_not_flagged(db: Database):
    """If os.kill raises PermissionError → indeterminate → err toward miss."""
    flag_time = _ago(FLAG_TTL_NO_FINGERPRINT_SECONDS + 60)
    _insert_zombie_candidate(db, "T-Z", last_heartbeat=_stale_hb(),
                             executor_pid=1,  # pid 1 usually needs root
                             zombie_flagged_at=flag_time)
    _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30)
    t = db.get_task("T-Z")
    # PermissionError → not ProcessLookupError → indeterminate → err toward miss
    # Should NOT cancel; flag may remain
    assert t.status != TaskStatus.CANCELLED, (
        "PermissionError on pid probe → indeterminate, should not cancel"
    )


# ---------------------------------------------------------------------------
# FIX 1 (Finding 1 HIGH): flagged zombie + fingerprint before TTL → cleared immediately
# ---------------------------------------------------------------------------

def test_flagged_zombie_with_fingerprint_cleared_immediately_before_ttl(db: Database):
    """Finding 1 (HIGH): flagged task + fingerprint appearing before TTL →
    consumed immediately (no TTL wait), flag cleared, zombie_cleared audit emitted.

    Per protocol/05c recovery clause: 'or a result appears' triggers immediate
    flag clearing. Merely NULL-ing zombie_flagged_at without consuming would
    re-flag next tick, so the clear is paired with fingerprint consumption.
    """
    task_id = "T-FP-IMMEDIATE"
    agent = "dev_agent"
    session_id = "sess-fp-imm"

    # Flag the zombie RECENTLY — well within the 30s fingerprint TTL.
    flag_time = _ago(10)
    _insert_zombie_candidate(
        db, task_id, last_heartbeat=_stale_hb(),
        executor_pid=ZOMBIE_PID,
        zombie_flagged_at=flag_time,
        current_session_id=session_id,
        assigned_agent=agent,
    )
    # Insert a task_result fingerprint.
    _insert_task_result(db, task_id, agent, session_id, status="completed")

    # Mock orchestrator with real db.
    mock_orch = MagicMock()
    mock_orch._db = db

    with patch(
        "runtime.orchestrator.run_step._consume_completion_report"
    ) as mock_consume:
        _sweep_org_zombies(
            db, now=_now(), uptime=999, warm_up_seconds=30,
            orchestrator=mock_orch,
        )
        # The fingerprint should be consumed immediately (no TTL wait).
        mock_consume.assert_called_once()

    # Flag must be cleared.
    t = db.get_task(task_id)
    assert t.zombie_flagged_at is None, (
        "flag should be cleared on fingerprint recovery"
    )

    # zombie_cleared audit must be emitted.
    actions = [r["action"] for r in db.get_audit_logs(task_id)]
    assert "zombie_cleared" in actions, (
        "zombie_cleared audit row must be present"
    )


# ---------------------------------------------------------------------------
# FIX 2 (was FIX 1): parent-wake on Tier-2 zombie cancel
# ---------------------------------------------------------------------------

def test_parent_woken_when_zombie_child_cancelled(db: Database):
    """A delegated parent parked on a zombie child is enqueued/woken when the
    reaper cancels the child (FIX 1 — code_reviewer HIGH)."""
    parent_id = "T-PARENT"
    child_id = "T-CHILD"

    # Create parent in in_progress(delegated), waiting on child.
    db.insert_task(TaskRecord(
        id=parent_id, brief="parent", team="engineering",
        assigned_agent="dev_agent", status=TaskStatus.IN_PROGRESS,
    ))
    db.update_task(parent_id, block_kind=BlockKind.DELEGATED)

    # Create zombie child with parent_task_id at insert time.
    flag_time = _ago(FLAG_TTL_NO_FINGERPRINT_SECONDS + 5)
    db.insert_task(TaskRecord(
        id=child_id, brief="zombie child", team="engineering",
        assigned_agent="dev_agent", status=TaskStatus.IN_PROGRESS,
        parent_task_id=parent_id,
    ))
    db.update_task(
        child_id,
        current_session_id="sess-dead",
        last_heartbeat=_stale_hb().isoformat(),
        executor_pid=ZOMBIE_PID,
    )
    db.update_task(child_id, zombie_flagged_at=flag_time.isoformat())

    # Create a mock orchestrator with access to the real DB.
    mock_orch = MagicMock()
    mock_orch._db = db

    with patch(
        "runtime.orchestrator.run_step._enqueue_parent_if_waiting"
    ) as mock_enqueue:
        _sweep_org_zombies(db, now=_now(), uptime=999, warm_up_seconds=30,
                           orchestrator=mock_orch)
        # _enqueue_parent_if_waiting should have been called for the child.
        mock_enqueue.assert_called_once_with(mock_orch, child_id)

    # Child should be cancelled.
    t = db.get_task(child_id)
    assert t.status == TaskStatus.CANCELLED
    assert t.cancelled_at is not None
    assert t.completed_at is not None
    assert t.block_kind is None
    assert t.note == "zombie reaped: session died without completing"


# ---------------------------------------------------------------------------
# FIX 2: double-deserialize of waiting_on_job_ids
# ---------------------------------------------------------------------------

def test_fingerprint_with_waiting_on_job_ids_no_typeerror(db: Database):
    """A fingerprinted report with non-empty waiting_on_job_ids is consumed
    via _consume_completion_report without raising TypeError (FIX 2 —
    code_reviewer MEDIUM: get_latest_task_result already deserializes)."""
    task_id = "T-FP"
    agent = "dev_agent"
    session_id = "sess-fp"
    job_ids = ["job-1", "job-2"]

    # Insert a zombie candidate.
    _insert_zombie_candidate(db, task_id,
                             last_heartbeat=_stale_hb(),
                             executor_pid=ZOMBIE_PID,
                             current_session_id=session_id,
                             assigned_agent=agent)

    # Insert a task_result with non-empty waiting_on_job_ids.
    db.insert_task_result(
        task_id=task_id, agent=agent, session_id=session_id,
        status="blocked", confidence_score=90,
        output_summary="waiting on jobs",
        waiting_on_job_ids=job_ids,
    )

    # Fetch the fingerprint — get_latest_task_result already deserializes.
    fingerprint = db.get_latest_task_result(task_id, agent, session_id)
    assert fingerprint is not None
    assert fingerprint["waiting_on_job_ids"] == job_ids  # already a list

    # Create a mock orchestrator and mock _consume_completion_report.
    mock_orch = MagicMock()
    mock_orch._db = db
    task = db.get_task(task_id)

    with patch(
        "runtime.orchestrator.run_step._consume_completion_report"
    ) as mock_consume:
        # This must NOT raise TypeError.
        _consume_zombie_fingerprint(db, task_id, fingerprint, task, mock_orch)
        mock_consume.assert_called_once()
        # Verify the CompletionReport has the correct waiting_on_job_ids.
        called_report = mock_consume.call_args[0][2]
        assert called_report.waiting_on_job_ids == job_ids


# ── local_ci reconstruction in zombie fingerprint consumption ────────────

def test_consume_zombie_fingerprint_local_ci_reaches_report(db: Database):
    """When a fingerprinted task_result contains valid local_ci, the
    consumed CompletionReport must carry that exact LocalCiEvidence."""
    from runtime.models import LocalCiEvidence

    task_id = "T-ZLC"
    agent = "dev_agent"
    session_id = "sess-zlc"

    _insert_zombie_candidate(db, task_id,
                             last_heartbeat=_stale_hb(),
                             executor_pid=ZOMBIE_PID,
                             current_session_id=session_id,
                             assigned_agent=agent)

    db.insert_task_result(
        task_id=task_id, agent=agent, session_id=session_id,
        status="completed", confidence_score=90,
        output_summary="done with local_ci",
        local_ci_json='{"command":"scripts/local_ci.sh all","exit_code":0}',
    )

    fingerprint = db.get_latest_task_result(task_id, agent, session_id)
    assert fingerprint is not None
    assert fingerprint["local_ci"] == '{"command":"scripts/local_ci.sh all","exit_code":0}'

    mock_orch = MagicMock()
    mock_orch._db = db
    task = db.get_task(task_id)

    with patch(
        "runtime.orchestrator.run_step._consume_completion_report"
    ) as mock_consume:
        _consume_zombie_fingerprint(db, task_id, fingerprint, task, mock_orch)
        mock_consume.assert_called_once()
        called_report = mock_consume.call_args[0][2]
        assert called_report.local_ci is not None
        assert called_report.local_ci.command == "scripts/local_ci.sh all"
        assert called_report.local_ci.exit_code == 0


def test_consume_zombie_fingerprint_malformed_local_ci_returns_none(db: Database):
    """Malformed local_ci in a zombie fingerprint degrades to None without
    crashing or changing the existing recovery/flag semantics."""
    task_id = "T-ZMAL"
    agent = "dev_agent"
    session_id = "sess-zmal"

    _insert_zombie_candidate(db, task_id,
                             last_heartbeat=_stale_hb(),
                             executor_pid=ZOMBIE_PID,
                             current_session_id=session_id,
                             assigned_agent=agent)

    # Insert a task_result, then corrupt the local_ci column directly.
    db.insert_task_result(
        task_id=task_id, agent=agent, session_id=session_id,
        status="completed", confidence_score=90,
        output_summary="done with bad ci",
    )
    db._conn.execute(
        "UPDATE task_results SET local_ci = ? "
        "WHERE task_id = ? AND session_id = ?",
        ("NOT VALID JSON", task_id, session_id),
    )
    db._conn.commit()

    fingerprint = db.get_latest_task_result(task_id, agent, session_id)
    assert fingerprint is not None
    assert fingerprint["local_ci"] == "NOT VALID JSON"

    mock_orch = MagicMock()
    mock_orch._db = db
    task = db.get_task(task_id)

    with patch(
        "runtime.orchestrator.run_step._consume_completion_report"
    ) as mock_consume:
        _consume_zombie_fingerprint(db, task_id, fingerprint, task, mock_orch)
        mock_consume.assert_called_once()
        called_report = mock_consume.call_args[0][2]
        assert called_report.local_ci is None, (
            f"malformed local_ci should be None; got {called_report.local_ci}"
        )


def test_consume_zombie_fingerprint_absent_local_ci_returns_none(db: Database):
    """A fingerprint without local_ci (NULL/unset) safely returns None."""
    task_id = "T-ZNUL"
    agent = "dev_agent"
    session_id = "sess-znul"

    _insert_zombie_candidate(db, task_id,
                             last_heartbeat=_stale_hb(),
                             executor_pid=ZOMBIE_PID,
                             current_session_id=session_id,
                             assigned_agent=agent)

    db.insert_task_result(
        task_id=task_id, agent=agent, session_id=session_id,
        status="completed", confidence_score=90,
        output_summary="done without local_ci",
    )

    fingerprint = db.get_latest_task_result(task_id, agent, session_id)
    assert fingerprint is not None
    assert fingerprint.get("local_ci") is None

    mock_orch = MagicMock()
    mock_orch._db = db
    task = db.get_task(task_id)

    with patch(
        "runtime.orchestrator.run_step._consume_completion_report"
    ) as mock_consume:
        _consume_zombie_fingerprint(db, task_id, fingerprint, task, mock_orch)
        mock_consume.assert_called_once()
        called_report = mock_consume.call_args[0][2]
        assert called_report.local_ci is None
