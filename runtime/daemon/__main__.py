"""HappyRanch daemon entry point.

Bootstraps from ~/.happyranch/runtimes.yaml, binds an ephemeral local port,
writes pid/port files, and runs the FastAPI app under uvicorn.

Offline maintenance mode (TASK-5443 replacement, TASK-5505):
``python -m runtime.daemon --maintenance`` is an explicit STARTUP-ONLY
one-shot that runs the MetricsStore maintenance sequence and exits — it
never binds an HTTP listener, never runs the FastAPI lifespan, and never
starts a scheduler/worker.  Run it while the daemon is stopped; the SQLite
layer fail-closes (checkpoint busy / VACUUM locked) if a live daemon holds
the store.  Every failure path (including daemon-home initialization and
stale/malformed runtime state) returns 1 with a stable bounded classification
and fixed recovery guidance — never raw exception text, tracebacks, paths, or
injected content.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
from types import FrameType

import uvicorn

from runtime.config import Settings
from runtime.daemon import paths, runtimes
from runtime.daemon.app import create_app
from runtime.daemon.queue import TaskQueue
from runtime.daemon.state import DaemonState
from runtime.infrastructure.audit_logger import AuditLogger
from runtime.infrastructure.database import Database
from runtime.models import BlockKind, TaskStatus
from runtime.orchestrator.orchestrator import (
    Orchestrator,
    completion_report_from_result_row,
)
from runtime.runtime import RuntimeDir

logger = logging.getLogger("happyranch.daemon")


def _sweep_on_startup(
    db: Database, queue: TaskQueue, slug: str,
    orchestrator: Orchestrator | None = None,
) -> list[str]:
    """Post-restart recovery for a single org (Path B — THR-037 Change B).

    Under Path B ``in_progress`` is two-valued, discriminated by ``block_kind``:
    a NULL discriminant means a subprocess was running (killed by the restart);
    a non-NULL discriminant (delegated/blocked_on_job) means the task was
    *parked* with no subprocess. Branching on ``status`` alone — as the pre-Path-B
    sweep did — would force-fail every parked parent and every blocked-on-job
    task on each restart (silent cascade corruption). The discriminant is what
    saves it:

      - Branch 1 — in_progress + block_kind IS NULL → pid-liveness probe
        (THR-079). Instead of assuming the subprocess died, the sweep reads
        the persisted ``executor_pid`` and probes with ``os.kill(pid, 0)``:
        * pid ALIVE → leave alone (session survived the daemon restart).
        * pid DEAD (ProcessLookupError) → ``FAILED`` with reason
          "session died on daemon restart — executor pid not alive".
        * pid NULL/undeterminable (PermissionError, etc.) → ``FAILED``
          with reason "session liveness undeterminable on daemon restart".
        No auto-revisit is spawned — the founder receives a
        ``daemon_restart_failure`` audit row and decides whether to
        re-dispatch. NOTE: a recycled pid could read as falsely-alive;
        the probe is the ratified THR-079 approach.
      - Branch 2 — in_progress + block_kind=DELEGATED with all children terminal
        → re-enqueue parent (orphaned wake-up: the daemon died after a child
        terminated but before the parent saw the signal). Else leave (children
        still live).
      - Branch 3 — in_progress + block_kind=BLOCKED_ON_JOB with all jobs terminal
        → re-enqueue (orphaned wake-up: jobs finished while the daemon was down).
        Else leave alone. This branch MUST exist: without it a parked-on-job
        task falls into Branch 1 and is wrongly failed on every restart.
      - Branch 4 — pending rows → re-enqueue (lost the original POST enqueue).
      - Branch 5 — escalated → leave alone (founder owns these); mirrors the
        pre-Path-B blocked(ESCALATED) fall-through.

    Phase 3: only in_progress(...) shapes are accepted; the boot migration
    flips any legacy blocked(...) rows before the sweep runs.

    When ``orchestrator`` is None (test harnesses that don't construct one),
    Branch 1 degrades to liveness-probe-and-mark-failed only; no cascade
    notification to parent. Production always passes an orchestrator.

    Returns the list of reply-delivery invocation tokens that must be
    re-enqueued onto the thread queue after commit (retained valid queued
    wakes and daemon_restart replacements from interrupted running replies);
    the caller enqueues them once the event loop is live.
    """
    # Imported lazily to avoid a startup-time cycle (run_step → daemon types).
    from runtime.orchestrator.run_step import (
        TERMINAL_STATES,
        _enqueue_parent_if_waiting,
    )

    audit = AuditLogger(db)

    import json as _json
    # Path B: parked carriers are in_progress(...) with block_kind set.
    # A live subprocess is in_progress + block_kind IS NULL (Branch 1).
    _PARKED = {TaskStatus.IN_PROGRESS}
    _TERMINAL_JOB_STATES = {"completed", "failed", "rejected"}

    for task_id in db.get_nonterminal_task_ids():
        t = db.get_task(task_id)
        if t is None:
            continue

        # Branch 1 — genuinely running, killed by the restart.
        if t.status == TaskStatus.IN_PROGRESS and t.block_kind is None:
            # THR-079: use the persisted executor OS pid as the liveness
            # signal instead of assuming the subprocess is dead. A running
            # session survives a daemon restart; only genuinely dead pids
            # (or undeterminable ones, per fail-closed default) are failed.
            # NOTE: os.kill(pid, 0) carries a pid-recycle caveat — a recycled
            # pid could read as falsely-alive. The probe is the ratified
            # THR-079 approach; a falsely-alive false-positive is acceptable
            # relative to the risk of duplicate runs from false-negative.
            pid = t.executor_pid
            alive = False
            if pid is not None:
                try:
                    os.kill(pid, 0)  # signal 0 = existence check, no signal sent
                except ProcessLookupError:
                    alive = False
                except Exception:
                    # PermissionError, recycled-pid uncertainty, any
                    # non-clean answer → founder fail-closed default.
                    # Do NOT leave-alone and do NOT auto-resume on ambiguity.
                    pid = None  # treat as undeterminable
                else:
                    alive = True

            if alive:
                # Session still running — leave alone. No reconcile, no
                # re-enqueue, no auto-revisit. The live session will complete
                # its work and report back normally.
                continue

            # THR-090 Track A: before failing a dead-pid task, check for an
            # unconsumed task_result row from the CURRENT session (the
            # definitive TASK-2625 fingerprint: a completion callback that
            # landed after the daemon died). Session-scoping is mandatory:
            # a prior-step result row carries a different session uuid and
            # must never match — the task falls through to the dead-pid FAIL
            # path instead. Governing invariant: err toward a MISS
            # (fail-closed), NEVER replay an already-consumed decision.
            # Only act if current_session_id is not None AND the row is found;
            # otherwise fall through unchanged to the dead-pid FAIL path.
            orphaned_result_row = None
            if t.current_session_id is not None and t.assigned_agent is not None:
                orphaned_result_row = db.get_latest_task_result(
                    task_id, t.assigned_agent, t.current_session_id,
                )
            if orphaned_result_row is not None and orchestrator is not None:
                orphaned_report = completion_report_from_result_row(
                    task_id, orphaned_result_row,
                    fallback_agent=t.assigned_agent or "unknown",
                )
                # Audit: log the completion report so the consumed result is
                # visible — the original session's log_completion_report call
                # never ran (the daemon died before that point).
                orchestrator._audit.log_completion_report(report=orphaned_report)
                from runtime.orchestrator.run_step import _consume_completion_report
                _consume_completion_report(
                    orchestrator, task_id, orphaned_report,
                    result_row_id=orphaned_result_row.get("id"),
                )
                continue

            # Dead or undeterminable (no orphaned result to consume):
            # fail-closed. No auto-revisit spawn — the THR-079 ruling
            # supersedes the earlier heartbeat/revisit approach. The founder
            # receives a daemon_restart_failure audit row and decides whether
            # to re-dispatch.
            if pid is None:
                reason = (
                    "session liveness undeterminable on daemon restart -- "
                    "executor pid null or probe inconclusive"
                )
            else:
                reason = (
                    "session died on daemon restart -- executor pid not alive"
                )
            db.update_task(task_id, status=TaskStatus.FAILED, note=reason)
            audit.log_daemon_restart_failure(task_id, t.assigned_agent or "daemon")
            if orchestrator is not None:
                _enqueue_parent_if_waiting(
                    orchestrator, task_id,
                    root_auto_revisit_spawned=False,
                )

        # Branch 2 — parked on children (delegated). Re-enqueue only when all
        # children are terminal (orphaned wake-up); else leave it parked.
        elif t.status in _PARKED and t.block_kind == BlockKind.DELEGATED:
            children = [db.get_task(cid) for cid in db.get_children(task_id)]
            if all(c is not None and c.status in TERMINAL_STATES
                   for c in children):
                queue.enqueue(slug, task_id)

        # Branch 3 — parked on jobs (blocked_on_job). Re-enqueue only when all
        # blocking jobs are terminal (jobs finished while the daemon was down);
        # else leave alone. MUST exist or these fall into Branch 1 and get
        # wrongly failed on every restart (the #1 reviewer-focus item).
        elif t.status in _PARKED and t.block_kind == BlockKind.BLOCKED_ON_JOB:
            try:
                job_ids = _json.loads(t.blocked_on_job_ids or "[]")
            except _json.JSONDecodeError:
                job_ids = []
            if job_ids and all(
                db.get_job_status(j) in _TERMINAL_JOB_STATES for j in job_ids
            ):
                queue.enqueue(slug, task_id)

        # Branch 4 — pending: re-enqueue (lost the original POST enqueue).
        elif t.status == TaskStatus.PENDING:
            queue.enqueue(slug, task_id)

        # Branch 5 — escalated: leave alone (founder owns the transition).
        # Reached only because get_nonterminal_task_ids now yields escalated.
        elif t.status == TaskStatus.ESCALATED:
            pass
        # The boot-time migration flips any legacy blocked(escalated) row
        # before startup; this branch is reached only via new escalated rows.

    # Branch 6 — thread reply delivery recovery (GitHub #688 Slice B).
    # Replace ONLY the conversational REPLY portion of the generic reaper with
    # the durable store-owned recovery primitives. BOOTSTRAP and TASK_FOLLOWUP
    # keep the generic daemon_restart reaping below.
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc).isoformat()

    # 6a. Activate per-pair reply delivery state for every OPEN thread
    # (idempotent cutover: seeds/coalesces legacy pending REPLYs).
    recovered_tokens: list[str] = []
    for thread_id in db.list_open_thread_ids():
        db.cutover_thread_reply_delivery_state(thread_id)

    # 6b. Durable reply-delivery recovery: retain valid queued wakes, replace
    # interrupted running attempts with exactly one daemon_restart replacement.
    for entry in db.recover_reply_delivery_state():
        recovered_tokens.append(entry.invocation_token)

    # 6e. TASK-5966 exchange reconcile: corruption sweep (fail-closed) then
    # idempotent closure evaluation for every open exchange. daemon_restart is
    # an INTERRUPTION, never terminal, while a recovery replacement covering
    # the exchange is queued/running (6b) — the quiescence predicate keeps the
    # cohort non-quiescent until that replacement settles. Catch-up tokens
    # minted by a stale-close are enqueued after commit like every other
    # startup token.
    for entry in db.reconcile_reply_exchanges():
        recovered_tokens.append(entry.invocation_token)

    # 6c. Reap the remaining conversational REPLY rows not governed by any
    # delivery-state ownership slot (orphan legacy receipts — e.g. archived
    # threads cutover does not reach). Never touch the governed queued wakes
    # retained above.
    db._conn.execute(
        "UPDATE thread_invocations SET status = 'failed', "
        "decline_reason = ?, consumed_at = ? "
        "WHERE status = 'pending' AND purpose = 'reply' "
        "AND invocation_token NOT IN "
        "(SELECT queued_invocation_token FROM thread_reply_delivery_state "
        " WHERE queued_invocation_token IS NOT NULL) "
        "AND invocation_token NOT IN "
        "(SELECT running_invocation_token FROM thread_reply_delivery_state "
        " WHERE running_invocation_token IS NOT NULL)",
        ("daemon_restart", _now),
    )

    # 6d. Preserve the generic reaper for BOOTSTRAP and TASK_FOLLOWUP exactly.
    cursor = db._conn.execute(
        "UPDATE thread_invocations SET status = 'failed', "
        "decline_reason = ?, consumed_at = ? "
        "WHERE status = 'pending' AND purpose IN ('bootstrap', 'task_followup')",
        ("daemon_restart", _now),
    )
    db._conn.commit()
    logger.debug(
        "startup sweep: reaped %d orphaned pending BOOTSTRAP/TASK_FOLLOWUP "
        "invocations; recovered %d reply-delivery tokens",
        cursor.rowcount, len(recovered_tokens),
    )
    return recovered_tokens


def _build_state(settings: Settings) -> DaemonState:
    reg = runtimes.load()
    if reg.active is None:
        # Auto-provision a default runtime on first launch so the daemon
        # never starts idle unless the provisioning itself fails.
        # Precedence:
        #   1. Registered runtimes exist but none active → activate the first.
        #   2. Registry empty → create a default runtime at daemon_home/runtime.
        if reg.registered:
            target = reg.registered[0]
            logger.info("no active runtime — activating existing registered runtime: %s", target)
            runtimes.activate(target)
        else:
            default_path = paths.daemon_home() / "runtime"
            logger.info("no active runtime — auto-provisioning default runtime at %s", default_path)
            RuntimeDir.init(default_path)
            runtimes.register(default_path)
        reg = runtimes.load()
        if reg.active is None:
            # Defensive: if provisioning still yields no active runtime,
            # fall back to idle rather than raising. This path should be
            # unreachable in practice.
            logger.error("runtime auto-provision failed — starting in idle mode")
            return DaemonState.idle(settings)
    runtime = RuntimeDir.load(reg.active)
    state = DaemonState.from_runtime(runtime, settings)
    for org in state.orgs.values():
        recovered_tokens = _sweep_on_startup(
            org.db, state.queue, org.slug, org.orchestrator,
        )
        # GitHub #688 Slice B: startup reply-delivery recovery returns the
        # queued/replacement tokens to re-enqueue once the event loop is live
        # (the lifespan enqueues them before thread workers start).
        org._startup_recovered_thread_tokens = recovered_tokens
    # Worker-pool bootstrap is deferred to the FastAPI lifespan startup
    # event because we need a running event loop. See `create_app` →
    # lifespan.
    return state


def _bind_port(host: str, port: int = 0) -> tuple[socket.socket, int]:
    """Bind to `port` (0 = ephemeral) and return (socket, actual_port)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock, sock.getsockname()[1]


def _install_signal_handlers(state: DaemonState) -> None:
    def _handle(signum: int, _frame: FrameType | None) -> None:
        logger.info("received signal %s — shutting down", signum)
        # uvicorn handles its own SIGTERM/SIGINT to drain workers; here
        # we just make sure the lifecycle files get cleaned up.
        for f in (paths.pid_file(), paths.port_file()):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        for org in state.orgs.values():
            try:
                org.db.close()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def _daemon_pid_alive() -> bool:
    """True when the daemon pid file names a live process (fail-closed).

    A pid file naming a live process means a normal daemon is (or may be)
    serving traffic — offline maintenance must refuse.  A missing or
    dead/stale pid is fine; an ambiguous probe (e.g. PermissionError) is
    treated as alive so destructive compaction is never attempted when the
    daemon state cannot be verified.
    """
    try:
        raw = paths.pid_file().read_text().strip()
        pid = int(raw)
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)  # signal 0 = existence check, no signal sent
    except ProcessLookupError:
        return False
    except OSError:
        # PermissionError / undeterminable → fail closed (don't run
        # destructive compaction when we cannot verify the daemon is stopped).
        return True
    return True


def run_maintenance() -> int:
    """Run the offline/startup-only metrics maintenance one-shot and exit.

    This function IS the entire process when ``--maintenance`` is passed:
    it runs BEFORE ``_build_state`` (no org load, no startup sweep, no
    task-state mutation), before ``create_app``, before ``_bind_port`` and
    before uvicorn — so no HTTP listener, lifespan, scheduler loop, worker,
    or task/job/session producer ever starts.  It opens the active runtime's
    ``metrics.db`` through ``MetricsStore``, runs the ordered
    prune → WAL checkpoint → integrity check → controlled VACUUM →
    post-VACUUM WAL checkpoint → final integrity check sequence, logs the
    bounded telemetry report, and returns a process exit code.

    Fail-closed and bounded: every preflight step that can throw — daemon-home
    initialization, the pid probe, the runtime registry load, ``RuntimeDir.load``
    (stale/malformed runtime state), the store open, and the ordered maintenance
    sequence — sits inside ONE controlled failure boundary.  Any failure returns 1
    with a stable, non-sensitive classification (``MetricsMaintenanceError.code``
    or ``operational-error``) plus fixed recovery guidance — never raw
    exception text, tracebacks, filesystem paths, IDs, or injected secrets,
    and never a success claim or an automatic retry.  A fresh explicit
    invocation is required for retry.
    """
    from datetime import datetime, timedelta, timezone

    from runtime.daemon.metrics_store import (
        MetricsMaintenanceError,
        MetricsStore,
        _RETENTION_DAYS,
    )

    # The ENTIRE maintenance preflight + run sits inside the bounded
    # failure boundary: daemon-home initialization, pid probe, registry load,
    # RuntimeDir.load (which raises on stale/malformed runtime state), store
    # open, and the ordered maintenance sequence.  Any throw converts to exit
    # 1 with bounded recovery guidance — never a raw traceback out of
    # ``main()``.
    try:
        # Daemon-home initialization first — inside the same controlled
        # boundary as every other maintenance preflight, so a hostile or
        # malformed home (unwritable parent, path-as-file, injected marker)
        # returns exit 1 with fixed redacted guidance instead of an uncaught
        # traceback out of ``main()``.
        paths.ensure_daemon_home()

        # Offline guard (belt-and-suspenders): SQLite fail-closes on a
        # concurrent holder anyway (checkpoint busy / VACUUM locked), but a
        # live-daemon pid probe fails fast with an actionable message.
        if _daemon_pid_alive():
            logger.error(
                "metrics maintenance aborted: a daemon process appears to be "
                "running (the daemon pid file in the daemon home names a "
                "live process). Maintenance is OFFLINE/STARTUP-ONLY — stop "
                "the daemon first, then re-run "
                "'python -m runtime.daemon --maintenance'."
            )
            return 1

        reg = runtimes.load()
        if reg.active is None:
            logger.error(
                "metrics maintenance aborted: no active runtime is registered "
                "(runtimes.yaml has no active runtime); nothing to maintain. "
                "Register/activate a runtime, then re-run "
                "'python -m runtime.daemon --maintenance'."
            )
            return 1

        runtime = RuntimeDir.load(reg.active)
        store = MetricsStore(str(runtime.root / "metrics.db"))
        try:
            now = datetime.now(timezone.utc)
            cutoff = (now - timedelta(days=_RETENTION_DAYS)).isoformat()
            report = store.maintenance(cutoff)
        finally:
            try:
                store.close()
            except Exception:
                pass
    except MetricsMaintenanceError as exc:
        logger.error(
            "metrics maintenance FAILED (%s). No automatic retry — re-run "
            "'python -m runtime.daemon --maintenance' after resolving the "
            "cause. The store was left queryable where SQLite guarantees it; "
            "metrics.db/-wal/-shm were never deleted or hand-edited.",
            exc.code,
        )
        return 1
    except Exception:
        logger.error(
            "metrics maintenance FAILED (operational-error). No automatic "
            "retry — re-run 'python -m runtime.daemon --maintenance' after "
            "resolving the cause. The store was left queryable where SQLite "
            "guarantees it; metrics.db/-wal/-shm were never deleted or "
            "hand-edited."
        )
        return 1

    logger.info("metrics maintenance complete: %s", report)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m runtime.daemon",
        description="HappyRanch daemon (or offline metrics maintenance one-shot).",
    )
    parser.add_argument(
        "--maintenance",
        action="store_true",
        help=(
            "Run the OFFLINE/STARTUP-ONLY metrics maintenance sequence "
            "(strict-before prune at the 30-day cutoff, WAL checkpoint, "
            "integrity check, controlled VACUUM, post-VACUUM WAL "
            "checkpoint, final integrity check) against the active "
            "runtime's metrics.db, log the bounded telemetry report, and "
            "exit. Never starts the normal daemon; failure returns a "
            "bounded, redacted classification plus fixed recovery guidance "
            "and requires a fresh explicit invocation for retry."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Parse the command line BEFORE any daemon-home initialization that can
    # throw: a hostile/malformed daemon home must never leak an uncaught
    # traceback out of an otherwise-controlled invocation.
    args = _build_parser().parse_args(argv)

    # Offline maintenance is the ENTIRE process: it must run before any
    # HTTP listener binds, before the FastAPI lifespan, and before any
    # scheduler/worker starts — then exit.  It can never coexist with
    # serving normal traffic.  Daemon-home initialization for the
    # maintenance path happens INSIDE run_maintenance()'s single bounded
    # failure boundary.
    if args.maintenance:
        return run_maintenance()

    paths.ensure_daemon_home()

    paths.ensure_token()

    settings = Settings()
    # Normalise PATH so the bundled happyranch CLI and standard tool
    # directories are available even when the daemon is launched by
    # Finder/launchd with a stripped PATH (/usr/bin:/bin).  Executor
    # binary resolution is handled separately via the machine-local
    # executors.json registry (THR-107 seq155).  Must happen before any
    # executor is constructed (issue #254).
    from runtime.orchestrator.executors import _normalize_path
    _normalize_path()
    state = _build_state(settings)
    app = create_app(state)

    sock, port = _bind_port(settings.daemon_bind_host, settings.daemon_port)
    paths.port_file().write_text(str(port))
    paths.pid_file().write_text(str(os.getpid()))
    _install_signal_handlers(state)

    logger.info("HappyRanch daemon listening on %s:%d", settings.daemon_bind_host, port)
    config = uvicorn.Config(app, log_level="info", lifespan="on")
    server = uvicorn.Server(config)
    # Hand the bound socket to uvicorn so we don't race the port number.
    server.run(sockets=[sock])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
