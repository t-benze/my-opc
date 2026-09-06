"""Ongoing zombie-task reaper (THR-090 Track B).

Periodically scans in_progress tasks with no block_kind for zombie signatures
(dead process + stale heartbeat, with optional task_result fingerprint) and
applies flag-then-cancel-on-TTL discipline.

Design authority: THR-090 seq10 (consultant_head umbrella) + seq12 (founder
approval). This is a NARROW periodic reaper, NOT a general health monitor.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from runtime.daemon.queue import HEARTBEAT_INTERVAL_SECONDS
from runtime.infrastructure.audit_logger import AuditLogger
from runtime.models import TaskStatus

if TYPE_CHECKING:
    from runtime.daemon.state import DaemonState
    from runtime.infrastructure.database import Database
    from runtime.orchestrator.orchestrator import Orchestrator

logger = logging.getLogger("happyranch.daemon.zombie_reaper")

# ---------------------------------------------------------------------------
# Tuning constants (founder-approved design — THR-090 seq10/seq12)
# ---------------------------------------------------------------------------

# Staleness threshold: ≥ 2 missed heartbeat intervals.
# A heartbeat that hasn't been stamped for this long is definitely stale.
STALE_HEARTBEAT_SECONDS = 2 * HEARTBEAT_INTERVAL_SECONDS  # 60s

# Tier 1 (WITH task_result fingerprint): shorter TTL — high confidence the
# task actually completed, so we can consume the result sooner.
FLAG_TTL_FINGERPRINT_SECONDS = HEARTBEAT_INTERVAL_SECONDS  # 30s

# Tier 2 (WITHOUT fingerprint): longer TTL — cancel-on-TTL is an inference
# that work didn't complete; stay flag-heavy and conservative.
FLAG_TTL_NO_FINGERPRINT_SECONDS = 5 * HEARTBEAT_INTERVAL_SECONDS  # 150s

# Interval between reaper sweeps. Aligned with the heartbeat cadence.
REAPER_INTERVAL_SECONDS = 30


# ---------------------------------------------------------------------------
# PID liveness probe
# ---------------------------------------------------------------------------

def _pid_is_dead(pid: int) -> bool:
    """Return True if the given OS pid is definitively dead (ProcessLookupError).

    False for pid alive, None pid, or indeterminate (PermissionError, etc.).
    Err toward a miss: indeterminate → treat as alive (don't reap).
    """
    try:
        os.kill(pid, 0)  # signal 0 = existence check, no signal sent
    except ProcessLookupError:
        return True  # definitively dead
    except Exception:
        # PermissionError, recycled-pid uncertainty, etc. → indeterminate.
        # Err toward a miss: treat as alive.
        return False
    else:
        return False  # pid exists → alive


# ---------------------------------------------------------------------------
# Per-org sweep
# ---------------------------------------------------------------------------

def _sweep_org_zombies(
    db: Database,
    *,
    now: datetime,
    uptime: float,
    warm_up_seconds: float,
    orchestrator: Orchestrator | None = None,
) -> None:
    """Sweep one org for zombie tasks.

    This is the pure logic entry point — callable from the periodic loop
    (with orchestrator) or unit tests (without).
    """
    # Warm-up grace: don't trust staleness until daemon has been up >= 1
    # heartbeat interval. Prevents false-reaping freshly-spawned sessions
    # whose heartbeat hasn't been stamped yet after a boot.
    if uptime < warm_up_seconds:
        return

    audit = AuditLogger(db)

    for task_id in db.get_nonterminal_task_ids():
        t = db.get_task(task_id)
        if t is None:
            continue

        # ── STATE ALLOWLIST (requirement 3) ──
        # Explicit allowlist: in_progress + block_kind NULL. Never touch a
        # healthy in_progress (fresh heartbeat), nor any blocked/terminal task.
        if t.status != TaskStatus.IN_PROGRESS or t.block_kind is not None:
            continue

        # ── PREDICATE (AND-gate) ──
        # All must hold to even consider a task as zombie.

        # 1. Stale heartbeat — must be older than STALE_HEARTBEAT_SECONDS.
        if t.last_heartbeat is None:
            continue  # err toward miss — can't determine staleness
        hb_age = (now - t.last_heartbeat).total_seconds()
        if hb_age < STALE_HEARTBEAT_SECONDS:
            # Heartbeat is fresh → healthy. If previously flagged, clear.
            if t.zombie_flagged_at is not None:
                db.update_task(task_id, zombie_flagged_at=None)
                audit.log_zombie_cleared(
                    task_id, t.assigned_agent or "unknown",
                )
            continue

        # 2. Dead executor pid — must be definitively not alive.
        if t.executor_pid is None:
            continue  # err toward miss — can't probe
        if not _pid_is_dead(t.executor_pid):
            # Pid is alive (or indeterminate). If previously flagged, clear.
            if t.zombie_flagged_at is not None:
                db.update_task(task_id, zombie_flagged_at=None)
                audit.log_zombie_cleared(
                    task_id, t.assigned_agent or "unknown",
                )
            continue

        # ── PREDICATE MATCHED — task is a zombie candidate ──

        # Determine fingerprint tier: is there an unconsumed task_result
        # from the current session?
        fingerprint = None
        if t.current_session_id is not None and t.assigned_agent is not None:
            fingerprint = db.get_latest_task_result(
                task_id, t.assigned_agent, t.current_session_id,
            )

        # Tier-based TTL selection (requirement 2: fingerprint-tiered confidence)
        if fingerprint is not None:
            ttl = FLAG_TTL_FINGERPRINT_SECONDS  # shorter — high confidence
        else:
            ttl = FLAG_TTL_NO_FINGERPRINT_SECONDS  # longer — conservative

        agent = t.assigned_agent or "unknown"

        if t.zombie_flagged_at is None:
            # ── FIRST DETECTION: FLAG only, do NOT cancel ──
            db.update_task(task_id, zombie_flagged_at=now.isoformat())
            audit.log_zombie_flagged(task_id, agent)
        else:
            # ── ALREADY FLAGGED ──
            # If a task_result fingerprint appears on an already-flagged zombie,
            # treat it as recovery IMMEDIATELY (protocol/05c recovery clause).
            # A real result is never a false-reap — honoring it immediately is
            # the safe direction (founder-approved loss function, THR-090 seq12).
            # Design note: merely clearing zombie_flagged_at without consuming
            # would re-flag next tick (task still in_progress + stale-hb +
            # dead-pid), so clear MUST be paired with consumption.
            if fingerprint is not None:
                if orchestrator is not None:
                    _consume_zombie_fingerprint(
                        db, task_id, fingerprint, t, orchestrator,
                    )
                    # Consumption moves the task terminal; clear the flag.
                    db.update_task(task_id, zombie_flagged_at=None)
                    audit.log_zombie_cleared(task_id, agent)
                # No orchestrator (unit-test context): leave flagged, retry
                # next sweep when orchestrator is present.
                continue
            # No fingerprint — check TTL for cancel.
            # zombie_flagged_at is parsed from TEXT column by Pydantic.
            # It may be a datetime object or a string.
            flag_time: datetime
            if isinstance(t.zombie_flagged_at, datetime):
                flag_time = t.zombie_flagged_at
            else:
                flag_time = datetime.fromisoformat(t.zombie_flagged_at)
            if (now - flag_time).total_seconds() >= ttl:
                # TTL expired — cancel.
                # THR-079 ruling: no auto-revisit. Cancel via the existing
                # cancelled status transition, routed through shared
                # terminal side-effects so a delegated parent parked on
                # this child is woken (code_reviewer FIX 2).
                db.update_task(
                    task_id,
                    status=TaskStatus.CANCELLED,
                    cancelled_at=now.isoformat(),
                    completed_at=now.isoformat(),
                    block_kind=None,
                    note="zombie reaped: session died without completing",
                )
                audit.log_zombie_cancelled(task_id, agent)
                if orchestrator is not None:
                    from runtime.orchestrator.run_step import _enqueue_parent_if_waiting
                    _enqueue_parent_if_waiting(orchestrator, task_id)


# ---------------------------------------------------------------------------
# Fingerprint consumption
# ---------------------------------------------------------------------------

def _consume_zombie_fingerprint(
    db: Database,
    task_id: str,
    fingerprint: dict,
    task,
    orchestrator: Orchestrator,
) -> None:
    """Consume an orphaned task_result discovered by the ongoing zombie reaper.

    Mirrors the Track A _sweep_on_startup orphaned-result consumption path:
    build a CompletionReport from the task_result row and feed it to
    _consume_completion_report (the same tail that run_step_impl uses for
    normal completions).
    """
    from runtime.orchestrator.orchestrator import completion_report_from_result_row

    orphaned_report = completion_report_from_result_row(
        task_id, fingerprint,
        fallback_agent=task.assigned_agent or "unknown",
    )
    from runtime.orchestrator.run_step import _consume_completion_report
    _consume_completion_report(
        orchestrator, task_id, orphaned_report,
        result_row_id=fingerprint.get("id"),
    )


# ---------------------------------------------------------------------------
# Periodic loop
# ---------------------------------------------------------------------------

async def zombie_reaper_loop(
    state: DaemonState,
    *,
    interval_seconds: int = REAPER_INTERVAL_SECONDS,
) -> None:
    """Periodic zombie-task reaper loop (THR-090 Track B).

    Runs per-org sweep on every tick. Mirrors the work_hours_scheduler_loop /
    dream_scheduler_loop pattern: asyncio.sleep interval, per-org iteration,
    metrics recording. Registered in runtime/daemon/app.py _lifespan alongside
    the existing scheduler tasks.
    """
    boot_time = _time.monotonic()
    warm_up_seconds = HEARTBEAT_INTERVAL_SECONDS  # 30s post-boot grace

    while True:
        t0 = _time.monotonic()
        now = datetime.now(timezone.utc)
        uptime = t0 - boot_time

        for org in list(state.orgs.values()):
            try:
                _sweep_org_zombies(
                    org.db,
                    now=now,
                    uptime=uptime,
                    warm_up_seconds=warm_up_seconds,
                    orchestrator=org.orchestrator,
                )
            except Exception:
                logger.exception(
                    "zombie reaper sweep failed for org %s", org.slug,
                )

        duration = _time.monotonic() - t0
        state.metrics_registry.record_loop_tick(
            "zombie_reaper", interval_seconds, duration,
        )
        await asyncio.sleep(interval_seconds)
