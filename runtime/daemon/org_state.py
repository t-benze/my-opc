"""Per-org runtime state: DB, queue events, sessions, teams, locks.

One ``OrgState`` per active org under ``<runtime>/orgs/<slug>/``. Constructed
once at daemon startup (via ``DaemonState.from_runtime``) or lazily on
``happyranch orgs init <slug>``. Each instance is fully self-contained — no
cross-references to other orgs.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from runtime.config import Settings
from runtime.daemon.dream_queue import DreamQueue
from runtime.daemon.event_bus import EventBus
from runtime.daemon.schedule_queue import ScheduleQueue
from runtime.daemon.wake_queue import WakeQueue
from runtime.daemon.sessions import SessionTracker
from runtime.daemon.thread_queue import ThreadQueue
from runtime.infrastructure.database import Database
from runtime.infrastructure.thread_store import ThreadStore
from runtime.models import BlockKind, TaskStatus
from runtime.orchestrator._paths import OrgPaths
from runtime.orchestrator.dashboard_projection import DashboardProjectionManager
from runtime.orchestrator.orchestrator import Orchestrator
from runtime.orchestrator.org_validation import validate_team_membership
from runtime.orchestrator.teams import TeamsRegistry

logger = logging.getLogger(__name__)


def _build_authority_evaluator():
    """S6a: production semantics come from the authenticated manager result.

    The injectable evaluator seam remains available to strict unit fakes and
    legacy static-policy tests; production never launches a second LLM.
    """
    return None


@dataclass
class OrgState:
    slug: str
    root: Path                        # <runtime>/orgs/<slug>
    db: Database
    teams: TeamsRegistry
    settings: Settings
    orchestrator: Orchestrator
    sessions: SessionTracker = field(default_factory=SessionTracker)
    db_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    kb_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    teams_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    thread_queue: ThreadQueue = field(default_factory=ThreadQueue)
    dream_queue: DreamQueue = field(default_factory=DreamQueue)
    wake_queue: WakeQueue = field(default_factory=WakeQueue)
    schedule_queue: ScheduleQueue = field(default_factory=ScheduleQueue)
    event_bus: EventBus = field(init=False)
    thread_store: ThreadStore = field(init=False)
    # Dashboard projection: per-org durable last-known-good cache, refreshed
    # every 10s by a coalesced asyncio scheduler. The HTTP route reads ONLY
    # from this projection; it never calls compose_dashboard_summary directly.
    dashboard_projection: DashboardProjectionManager = field(init=False)

    _TERMINAL_STATUS_TO_EVENT = {
        TaskStatus.COMPLETED: "task_complete",
        TaskStatus.FAILED: "task_failed",
        # A superseded-resolution is a non-failure terminal, so it replays as a
        # completion-class event; `_synthesize_terminal_event` carries the
        # precise label in `outcome` ("superseded").
        TaskStatus.SUPERSEDED: "task_complete",
        # Path B: cancellation is a non-success terminal, so it replays as a
        # failure-class event — mirroring how SUPERSEDED rides the
        # completion class — with the precise label in `outcome` ("cancelled").
        # Avoids inventing a new EventBus event type. (The founder-facing record
        # is the distinct `log_task_cancelled` audit row; this map only governs
        # terminal-event replay synthesis.)
        TaskStatus.CANCELLED: "task_failed",
    }

    def __post_init__(self) -> None:
        self.dashboard_projection = DashboardProjectionManager(
            org_slug=self.slug, org_root=self.root,
        )
        def loader(task_id: str) -> list[dict]:
            task = self.db.get_task(task_id)
            if task is not None:
                # Task Activity: gather audit logs for the root task and all
                # descendants (children, grandchildren, etc.), then merge and
                # deduplicate in chronological order (by audit-log id).
                task_ids = [task_id]
                task_ids.extend(self.db.get_descendant_task_ids(task_id))
                all_logs: list[dict] = []
                for tid in task_ids:
                    all_logs.extend(self.db.get_audit_logs(tid))
                # Deduplicate by id, sort chronologically.
                seen: set[int] = set()
                history: list[dict] = []
                for log in sorted(all_logs, key=lambda x: x["id"]):
                    if log["id"] not in seen:
                        seen.add(log["id"])
                        history.append({"type": "audit", **log})
            else:
                # Non-task topic (thread:*, job:*) or unknown task id:
                # preserve existing behavior — try audit logs for the raw
                # topic string (always empty for non-task topics).
                history = [
                    {"type": "audit", **log}
                    for log in self.db.get_audit_logs(task_id)
                ]
            terminal = self._synthesize_terminal_event(task) if task else None
            if terminal is not None:
                history.append(terminal)
            return history
        self.event_bus = EventBus(history_loader=loader)
        self.thread_store = ThreadStore(self.root / "threads")

    def _synthesize_terminal_event(self, task) -> dict | None:
        if task.status in self._TERMINAL_STATUS_TO_EVENT:
            return {
                "type": self._TERMINAL_STATUS_TO_EVENT[task.status],
                "outcome": task.status.value,
                "synthesized": True,
                "timestamp": self._terminal_event_timestamp(task),
            }
        # Path B: escalated is a top-level non-terminal status. Late
        # subscribers still get the right synthesized event.
        if task.status == TaskStatus.ESCALATED:
            return {
                "type": "task_blocked",
                "outcome": "escalated",
                "synthesized": True,
                "timestamp": self._terminal_event_timestamp(task),
            }
        return None

    @staticmethod
    def _terminal_event_timestamp(task) -> str:
        """Durable persisted timestamp for a synthesized terminal replay event.

        ``completed_at`` is authoritative when the task actually reached a
        terminal state (COMPLETED/FAILED/SUPERSEDED/CANCELLED all write it).
        Legacy rows with a NULL ``completed_at`` fall back to ``updated_at`` —
        the last durable write — mirroring ``list_agent_tasks``'s
        ``COALESCE(completed_at, updated_at, created_at)`` ordering. We never
        manufacture wall-clock time at replay.

        ESCALATED is non-terminal by design: ``try_escalate`` and
        ``try_escalate_over_budget`` write only ``updated_at`` (the transition
        timestamp), never ``completed_at`` — so the fallback naturally yields
        the escalation transition without reinterpreting ``completed_at``.
        """
        ts = task.completed_at or task.updated_at
        return ts.isoformat()

    @classmethod
    def load(cls, *, slug: str, root: Path, settings: Settings) -> "OrgState":
        paths = OrgPaths(root=root)
        db = Database(paths.db_path)
        teams = TeamsRegistry.load(root)
        # THR-095: one-shot seed — copy the 4 web-writable knobs from
        # config.yaml into the org_settings DB table exactly once per org.
        # Idempotent (sentinel); runs on every daemon startup but is a no-op
        # after the first run.
        try:
            from runtime.orchestrator.org_config import (
                backfill_reviewer_agents_setting,
                seed_org_settings_from_config,
            )
            seed_org_settings_from_config(paths, db)
            # THR-175: reviewer_agents is a 5th knob orgs seeded before this
            # change never received.  Backfill is idempotent (row-absent) and
            # never overwrites an explicit setting.
            backfill_reviewer_agents_setting(paths, db)
        except Exception as exc:
            logger.warning(
                "org %r: org_settings seed skipped (non-fatal): %s", slug, exc
            )

        # Refuse to attach if agent files and teams.yaml disagree. Raises
        # OrgConsistencyError on drift; DaemonState.from_runtime catches
        # per-org so one broken org cannot crash daemon startup, while
        # add_org propagates so explicit founder actions fail loudly.
        validate_team_membership(paths, teams)
        orchestrator = Orchestrator(
            db=db,
            settings=settings,
            paths=paths,
            slug=slug,
            teams=teams,
            authority_evaluator=_build_authority_evaluator(),
        )
        return cls(
            slug=slug,
            root=root,
            db=db,
            teams=teams,
            settings=settings,
            orchestrator=orchestrator,
        )

    def close(self) -> None:
        self.db.close()

