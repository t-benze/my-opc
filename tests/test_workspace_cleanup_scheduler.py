"""THR-195 / TASK-6036: daemon-managed workspace cleanup scheduler.

Covers the shipping seams of the founder-resolved system-default design
(THR-195 seq 129/130/131 + TASK-6036 defaults): per-agent weekly trigger
decision (cadence, per-agent dedup/cooldown, at-most-once per window),
per-agent >= 1 GiB trigger/non-trigger, owning-agent routing, exact
first-two report-only behavior, daemon trigger writes (task creation +
enqueue with fresh advisory context through the daemon-composed brief —
never a Schedule brief), the per-agent durable founder-report thread seam
(create-on-first-trigger, participant-authorized task-bound send path, NO
minted token), the enabled-by-default kill switch, mandatory
advisory/stale/non-candidate/re-derive wording, single true wall-clock
deadline across Git collection, every cardinality-cap boundary yielding
unavailable/truncated status, suffixed TASK-id conservative classification,
no candidate/safe-removal semantics, no Schedule or ordinary-session effect,
and SessionTracker live-session aggregation.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from runtime.config import Settings
from runtime.daemon import workspace_cleanup_scheduler as wcs
from runtime.daemon.sessions import SessionTracker
from runtime.infrastructure.database import Database
from runtime.models import (
    ScheduleKind,
    ScheduleRecord,
    ScheduleStatus,
    TaskRecord,
    TaskStatus,
    ThreadRecord,
)
from runtime.orchestrator.orchestrator import Orchestrator
from runtime.orchestrator.teams import TeamsRegistry


def test_fail_open_threshold_contract_is_consistent_across_normative_surfaces():
    """Normative summaries distinguish unavailable from numeric measurements."""
    root = Path(__file__).parents[1]
    surfaces = {
        "CLAUDE.md": (root / "CLAUDE.md").read_text(),
        "scheduler module": (
            root / "runtime/daemon/workspace_cleanup_scheduler.py"
        ).read_text(),
        "agent runtime protocol": (
            root / "protocol/05b-agent-runtime.md"
        ).read_text(),
        "orchestrator protocol": (
            root / "protocol/05c-orchestrator.md"
        ).read_text(),
    }

    normalized = {
        name: " ".join(source.split()) for name, source in surfaces.items()
    }
    for name, source in normalized.items():
        assert "bypasses only numeric threshold evaluation" in source, name
        assert "otherwise-due spawning continues" in source, name
        assert "honest unavailable advisory context" in source, name
        assert (
            "only an available numeric result below 1 GiB skips" in source
        ), name

    obsolete_claims = {
        "CLAUDE.md": "and the agent's workspace totals >= 1 GiB, triggers",
        "scheduler module": (
            "trigger only when that agent's workspace total is >= 1 GiB"
        ),
        "agent runtime protocol": (
            "agent's workspace totals >= 1 GiB "
            "(founder-approved defaults, TASK-6036), triggers"
        ),
        "orchestrator protocol": (
            "founder threshold: trigger only when the agent's workspace totals "
            ">= 1 GiB"
        ),
    }
    for name, claim in obsolete_claims.items():
        assert claim not in normalized[name], name


# ── helpers ──────────────────────────────────────────────────────────────

def _fmt(n: int) -> str:
    """1024-based human size, mirroring the note formatter's units."""
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def _write_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


def _make_teams(root: Path) -> TeamsRegistry:
    registry = TeamsRegistry.load(root)
    registry._teams["engineering"] = type(
        "TM", (), {"name": "engineering_manager", "team": "engineering",
                   "workers": ("dev_agent", "qa_engineer")}
    )()
    return registry


def _make_schedule(db: Database, *, schedule_id: str, spawned: list[str]) -> ScheduleRecord:
    """A user Schedule (used only to prove the daemon never touches it)."""
    record = ScheduleRecord(
        id=schedule_id,
        agent_name="dev_agent",
        team="engineering",
        kind=ScheduleKind.RECURRING,
        status=ScheduleStatus.ARMED,
        fire_at="2026-08-28T00:00:00+00:00",
        timezone="UTC",
        normalized_brief="Unrelated user schedule brief.",
        source_instruction="founder-created user todo",
    )
    db.schedules.insert(record)
    return db.schedules.get(schedule_id)


def _insert_cleanup_task(
    db: Database,
    *,
    task_id: str,
    agent: str = "dev_agent",
    created_at: datetime,
    status: TaskStatus = TaskStatus.COMPLETED,
    brief: str | None = None,
) -> None:
    db.insert_task(TaskRecord(
        id=task_id,
        brief=brief or (wcs._CLEANUP_BRIEF_MARKER + "\nprior cleanup run"),
        team="engineering",
        assigned_agent=agent,
        status=status,
        created_at=created_at,
    ))


class _RecordingGitRun:
    """Fake subprocess.run that answers `git worktree list --porcelain`.

    Keyed by the repo cwd so each repo reports only its own registered
    worktrees, matching real ``git worktree list`` semantics.
    """

    def __init__(self, by_repo: dict[str, list[str]] | None = None, *, fail: bool = False):
        self.calls: list[list[str]] = []
        self._by_repo = by_repo or {}
        self._fail = fail

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self._fail:
            import subprocess
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 5))
        cwd = str(kwargs.get("cwd", ""))
        paths = self._by_repo.get(cwd, [])
        lines = [f"worktree {p}" for p in paths]
        return type("R", (), {
            "returncode": 0,
            "stdout": ("\n".join(lines) + "\n").encode(),
        })()


# ── (a) per-agent trigger decision: cadence, dedup, cooldown ─────────────

def _sunday_0330_utc() -> datetime:
    """Next Sunday 03:30 UTC (deterministic reference for due/not-due)."""
    now = datetime.now(timezone.utc)
    days = (6 - now.weekday()) % 7
    sunday = (now + timedelta(days=days)).replace(
        hour=3, minute=30, second=0, microsecond=0,
    )
    if sunday < now:
        sunday += timedelta(days=7)
    return sunday


def test_trigger_decision_not_due_before_occurrence(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    occurrence = _sunday_0330_utc()
    just_before = occurrence - timedelta(minutes=30)  # Sunday 03:00: this week's occurrence is still in the future
    decision = wcs.decide_cleanup_trigger(
        db=db, agent="dev_agent", now_utc=just_before, tz=timezone.utc,
    )
    assert decision.should_trigger is False
    assert decision.reason == "not_due"


def test_trigger_decision_due_with_no_prior_run(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    occurrence = _sunday_0330_utc()
    decision = wcs.decide_cleanup_trigger(
        db=db, agent="dev_agent", now_utc=occurrence, tz=timezone.utc,
    )
    assert decision.should_trigger is True
    assert decision.reason is None


def test_trigger_decision_dedup_prior_run_in_flight(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    occurrence = _sunday_0330_utc()
    _insert_cleanup_task(
        db, task_id="TASK-100", created_at=occurrence - timedelta(days=7),
        status=TaskStatus.IN_PROGRESS,
    )
    decision = wcs.decide_cleanup_trigger(
        db=db, agent="dev_agent", now_utc=occurrence, tz=timezone.utc,
    )
    assert decision.should_trigger is False
    assert decision.reason == "prior_run_in_flight"


def test_trigger_decision_suppresses_other_nonterminal_statuses(tmp_path):
    """Any non-terminal status (e.g. ESCALATED) suppresses; terminal set is
    exactly COMPLETED/FAILED/SUPERSEDED/CANCELLED."""
    db = Database(tmp_path / "db.sqlite")
    occurrence = _sunday_0330_utc()
    _insert_cleanup_task(
        db, task_id="TASK-100", created_at=occurrence - timedelta(days=7),
        status=TaskStatus.ESCALATED,
    )
    decision = wcs.decide_cleanup_trigger(
        db=db, agent="dev_agent", now_utc=occurrence, tz=timezone.utc,
    )
    assert decision.should_trigger is False
    assert decision.reason == "prior_run_in_flight"


def test_trigger_decision_at_most_once_per_window(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    occurrence = _sunday_0330_utc()
    _insert_cleanup_task(
        db, task_id="TASK-100", created_at=occurrence + timedelta(seconds=1),
    )
    decision = wcs.decide_cleanup_trigger(
        db=db, agent="dev_agent",
        now_utc=occurrence + timedelta(seconds=5), tz=timezone.utc,
    )
    assert decision.should_trigger is False
    assert decision.reason == "already_triggered_this_window"


def test_trigger_decision_terminal_prior_run_before_window_triggers(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    occurrence = _sunday_0330_utc()
    _insert_cleanup_task(
        db, task_id="TASK-100", created_at=occurrence - timedelta(days=8),
        status=TaskStatus.COMPLETED,
    )
    decision = wcs.decide_cleanup_trigger(
        db=db, agent="dev_agent", now_utc=occurrence, tz=timezone.utc,
    )
    assert decision.should_trigger is True


def test_trigger_decision_ignores_unrelated_tasks(tmp_path):
    """A non-cleanup task (no marker) never counts for dedup/cooldown."""
    db = Database(tmp_path / "db.sqlite")
    occurrence = _sunday_0330_utc()
    _insert_cleanup_task(
        db, task_id="TASK-100", created_at=occurrence + timedelta(seconds=1),
        brief="Ordinary dev_agent work, no cleanup marker.",
    )
    decision = wcs.decide_cleanup_trigger(
        db=db, agent="dev_agent",
        now_utc=occurrence + timedelta(seconds=2), tz=timezone.utc,
    )
    assert decision.should_trigger is True


def test_trigger_decision_seven_day_cooldown_suppresses(tmp_path):
    """A terminal prior cleanup task younger than 7 days suppresses even when
    the weekly window is unserviced (rolling per-agent cooldown)."""
    db = Database(tmp_path / "db.sqlite")
    occurrence = _sunday_0330_utc()
    # Terminal run from ~6 days ago: older than the last occurrence but
    # younger than the seven-day cooldown.
    _insert_cleanup_task(
        db, task_id="TASK-100",
        created_at=occurrence - timedelta(days=6) - timedelta(hours=23),
        status=TaskStatus.COMPLETED,
    )
    decision = wcs.decide_cleanup_trigger(
        db=db, agent="dev_agent", now_utc=occurrence, tz=timezone.utc,
    )
    assert decision.should_trigger is False
    assert decision.reason == "cooldown"


def test_trigger_decision_cooldown_expired_triggers(tmp_path):
    """A terminal prior cleanup task older than 7 days with an unserviced
    window triggers."""
    db = Database(tmp_path / "db.sqlite")
    occurrence = _sunday_0330_utc()
    _insert_cleanup_task(
        db, task_id="TASK-100",
        created_at=occurrence - timedelta(days=8),
        status=TaskStatus.COMPLETED,
    )
    decision = wcs.decide_cleanup_trigger(
        db=db, agent="dev_agent", now_utc=occurrence, tz=timezone.utc,
    )
    assert decision.should_trigger is True


def test_trigger_decision_is_per_agent(tmp_path):
    """Agent A's in-flight cleanup task never suppresses agent B's trigger."""
    db = Database(tmp_path / "db.sqlite")
    occurrence = _sunday_0330_utc()
    _insert_cleanup_task(
        db, task_id="TASK-100", agent="dev_agent",
        created_at=occurrence - timedelta(days=7),
        status=TaskStatus.IN_PROGRESS,
    )
    decision = wcs.decide_cleanup_trigger(
        db=db, agent="qa_engineer", now_utc=occurrence, tz=timezone.utc,
    )
    assert decision.should_trigger is True


# ── (b) daemon trigger: per-agent threshold, owning-agent routing ─────────

class _FakeQueue:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def enqueue(self, slug: str, task_id: str) -> None:
        self.items.append((slug, task_id))


class _FakeDaemonState:
    def __init__(self) -> None:
        self.is_idle = False
        self.queue = _FakeQueue()
        self.orgs: dict[str, object] = {}


def _make_org(tmp_path: Path, db: Database, settings: Settings):
    from runtime.daemon.org_state import OrgState
    from runtime.orchestrator._paths import OrgPaths

    org_root = OrgPaths(root=tmp_path).org_dir
    org_root.mkdir(parents=True, exist_ok=True)
    return OrgState(
        slug="test", root=tmp_path, db=db, teams=_make_teams(tmp_path),
        settings=settings, orchestrator=None, sessions=SessionTracker(),
    )


def _org_with_workspaces(tmp_path: Path, db: Database, settings: Settings):
    org_root = tmp_path / "orgs" / "test"
    org_root.mkdir(parents=True, exist_ok=True)
    org = _make_org(org_root, db, settings)
    org.root = org_root
    (org_root / "workspaces" / "dev_agent").mkdir(parents=True, exist_ok=True)
    (org_root / "workspaces" / "qa_engineer").mkdir(parents=True, exist_ok=True)
    return org


@pytest.mark.asyncio
async def test_trigger_creates_task_for_owning_agent_with_fresh_advisory(
    tmp_path, test_settings, monkeypatch,
):
    """A due agent with a >= 1 GiB workspace (threshold monkeypatched small
    for determinism) gets an ordinary task assigned to ITSELF, carrying the
    fresh advisory snapshot in the daemon-composed brief."""
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(
        org.root / "workspaces" / "dev_agent" / "repos" / "r1" / "file.txt",
        1024 * 3,
    )
    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    assert task_id is not None
    task = db.get_task(task_id)
    assert task is not None
    assert task.assigned_agent == "dev_agent"
    assert task.team == "engineering"
    assert task.brief.startswith(wcs._CLEANUP_BRIEF_MARKER)
    assert state.queue.items == [("test", task_id)]

    # Fresh advisory context packed into the daemon-composed brief.
    assert "Workspace disk context" in task.brief
    assert "measured_at" in task.brief
    assert "3.0 KiB" in task.brief or "3 KiB" in task.brief

    # First run: strictly report-only.
    assert "THIS RUN IS STRICTLY REPORT-ONLY" in task.brief
    assert "Do NOT delete" in task.brief


@pytest.mark.asyncio
async def test_trigger_skips_below_1gib_threshold(tmp_path, test_settings):
    """An agent whose workspace measures below the >= 1 GiB founder threshold
    is NOT triggered; the skip is audited and no task/thread is created."""
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(
        org.root / "workspaces" / "dev_agent" / "repos" / "r1" / "file.txt",
        1024 * 3,  # 3 KiB << 1 GiB
    )
    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    assert task_id is None
    assert state.queue.items == []
    audits = db.get_audit_logs("workspace-cleanup:skipped")
    assert any(
        r["action"] == "workspace_cleanup_skipped"
        and r["payload"].get("reason") == "workspace_below_threshold"
        for r in audits
    )


@pytest.mark.asyncio
async def test_trigger_routes_to_owning_agent(tmp_path, test_settings, monkeypatch):
    """Owning-agent routing: qa_engineer's triggered task is assigned to
    qa_engineer, not a fixed dev_agent."""
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(
        org.root / "workspaces" / "qa_engineer" / "repos" / "r1" / "file.txt",
        1024,
    )
    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="qa_engineer",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    assert task_id is not None
    task = db.get_task(task_id)
    assert task.assigned_agent == "qa_engineer"
    assert f"agent qa_engineer" in task.brief


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "truncated", "expected_note"),
    [
        (
            "measurement deadline exceeded",
            True,
            "measurement truncated/unavailable",
        ),
        (
            "workspace could not be measured (unreadable)",
            False,
            "measurement unavailable",
        ),
        (
            "workspace cardinality cap exceeded",
            True,
            "measurement truncated/unavailable",
        ),
    ],
)
async def test_trigger_fail_open_when_measurement_unavailable(
    tmp_path, test_settings, monkeypatch, reason, truncated, expected_note,
):
    """Every bounded-unavailable class still creates a task whose advisory
    note honestly carries the unavailable/truncated reason."""
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)

    monkeypatch.setattr(
        wcs, "measure_workspace_context",
        lambda *a, **kw: wcs.WorkspaceContextSnapshot(
            available=False, reason=reason, truncated=truncated,
        ),
    )
    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    assert task_id is not None
    assert state.queue.items == [("test", task_id)]
    task = db.get_task(task_id)
    assert expected_note in task.brief
    assert reason in task.brief
    assert "No sizing data was packed" in task.brief
    assert "workspace total:" not in task.brief
    audits = db.get_audit_logs(task_id)
    assert any(
        r["action"] == "workspace_cleanup_triggered"
        and r["payload"].get("measurement_available") is False
        and r["payload"].get("measurement_reason") == reason
        and r["payload"].get("measurement_truncated") is truncated
        for r in audits
    )


@pytest.mark.asyncio
async def test_due_scheduler_tick_spawns_when_measurement_is_unavailable(
    tmp_path, test_settings, monkeypatch,
):
    """The shipping decision→tick→trigger path must not reinterpret an
    unavailable size as a failed numeric threshold comparison."""
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    cfg_path = org.root / "org" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("timezone: UTC\n")
    monkeypatch.setattr(
        wcs, "measure_workspace_context",
        lambda *a, **kw: wcs.WorkspaceContextSnapshot(
            available=False,
            reason="workspace measurement did not complete within bounded limits "
            "(deadline exceeded)",
            truncated=True,
        ),
    )
    state = _FakeDaemonState()
    state.orgs = {"test": org}
    occurrence = _sunday_0330_utc()

    await wcs._tick_org(
        org,
        state,
        now_utc=occurrence,
        previous_scan_utc=occurrence - timedelta(seconds=1),
    )

    assert len(state.queue.items) == 2
    tasks = [db.get_task(task_id) for _, task_id in state.queue.items]
    assert {task.assigned_agent for task in tasks} == {
        "dev_agent", "qa_engineer",
    }
    assert all(
        "measurement truncated/unavailable" in task.brief for task in tasks
    )
    assert all("deadline exceeded" in task.brief for task in tasks)


@pytest.mark.asyncio
async def test_due_tick_spawns_when_partial_traversal_is_unreadable(
    tmp_path, test_settings, monkeypatch,
):
    """A traversal error invalidates already-counted totals at the shipping
    tick seam, so a partial size can never suppress an otherwise-due run."""
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    cfg_path = org.root / "org" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("timezone: UTC\n")
    workspace = org.root / "workspaces" / "dev_agent"
    _write_file(workspace / "readable.txt", 100)
    unreadable = workspace / "unreadable"
    unreadable.mkdir()

    real_scandir = wcs.os.scandir

    def partly_unreadable_scandir(path):
        if Path(path) == unreadable:
            raise PermissionError("permission denied")
        return real_scandir(path)

    monkeypatch.setattr(wcs.os, "scandir", partly_unreadable_scandir)
    state = _FakeDaemonState()
    occurrence = _sunday_0330_utc()

    await wcs._tick_org(
        org,
        state,
        now_utc=occurrence,
        previous_scan_utc=occurrence - timedelta(seconds=1),
    )

    assert len(state.queue.items) == 1
    task = db.get_task(state.queue.items[0][1])
    assert task is not None
    assert task.assigned_agent == "dev_agent"
    assert "measurement unavailable" in task.brief
    assert "workspace could not be measured (unreadable)" in task.brief
    assert "No sizing data was packed" in task.brief
    assert "workspace total:" not in task.brief
    skipped = db.get_audit_logs("workspace-cleanup:skipped")
    assert not any(
        row["payload"].get("reason") == "workspace_below_threshold"
        and row["agent"] == "dev_agent"
        for row in skipped
    )
    triggered = db.get_audit_logs(task.id)
    assert any(
        row["action"] == "workspace_cleanup_triggered"
        and row["payload"].get("measurement_available") is False
        and row["payload"].get("measurement_reason")
        == "workspace could not be measured (unreadable)"
        and row["payload"].get("measurement_truncated") is False
        for row in triggered
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_method", ["is_symlink", "is_dir", "stat"])
@pytest.mark.parametrize("partial_count", [False, True])
async def test_due_tick_spawns_when_entry_metadata_is_unreadable(
    tmp_path, test_settings, monkeypatch, failing_method, partial_count,
):
    """Every caught DirEntry metadata failure invalidates zero/partial totals
    through the real walk, measurement, trigger, and due-tick shipping seams.
    """
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    cfg_path = org.root / "org" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("timezone: UTC\n")
    workspace = org.root / "workspaces" / "dev_agent"
    if partial_count:
        _write_file(workspace / "a-readable.txt", 100)
    _write_file(workspace / "z-failure-target", 200)

    real_scandir = wcs.os.scandir

    class FailingEntry:
        def __init__(self, entry):
            self._entry = entry

        def __getattr__(self, name):
            return getattr(self._entry, name)

        def is_symlink(self):
            if failing_method == "is_symlink":
                raise PermissionError("metadata permission denied")
            return self._entry.is_symlink()

        def is_dir(self, *, follow_symlinks=True):
            if failing_method == "is_dir":
                raise PermissionError("metadata permission denied")
            return self._entry.is_dir(follow_symlinks=follow_symlinks)

        def stat(self, *, follow_symlinks=True):
            if failing_method == "stat":
                raise PermissionError("metadata permission denied")
            return self._entry.stat(follow_symlinks=follow_symlinks)

    class OrderedScandir:
        def __init__(self, path):
            self._context = real_scandir(path)

        def __enter__(self):
            entries = sorted(self._context.__enter__(), key=lambda entry: entry.name)
            return iter([
                FailingEntry(entry) if entry.name == "z-failure-target" else entry
                for entry in entries
            ])

        def __exit__(self, *args):
            return self._context.__exit__(*args)

    monkeypatch.setattr(wcs.os, "scandir", OrderedScandir)
    state = _FakeDaemonState()
    occurrence = _sunday_0330_utc()

    await wcs._tick_org(
        org,
        state,
        now_utc=occurrence,
        previous_scan_utc=occurrence - timedelta(seconds=1),
    )

    assert len(state.queue.items) == 1
    task = db.get_task(state.queue.items[0][1])
    assert task is not None
    assert "measurement unavailable" in task.brief
    assert "workspace could not be measured (unreadable)" in task.brief
    assert "No sizing data was packed" in task.brief
    assert "workspace total:" not in task.brief
    skipped = db.get_audit_logs("workspace-cleanup:skipped")
    assert not any(
        row["payload"].get("reason") == "workspace_below_threshold"
        and row["agent"] == "dev_agent"
        for row in skipped
    )
    triggered = db.get_audit_logs(task.id)
    assert any(
        row["action"] == "workspace_cleanup_triggered"
        and row["payload"].get("measurement_available") is False
        and row["payload"].get("measurement_reason")
        == "workspace could not be measured (unreadable)"
        and "workspaces_bytes" not in row["payload"]
        for row in triggered
    )


@pytest.mark.asyncio
async def test_trigger_skips_when_agent_team_unresolved(tmp_path, test_settings):
    db = Database(tmp_path / "db.sqlite")
    org_root = tmp_path / "orgs" / "test"
    org_root.mkdir(parents=True, exist_ok=True)
    # Empty TeamsRegistry: no agent has a team → fail-closed skip.
    org = _make_org(org_root, db, test_settings)
    org.root = org_root
    org.teams = TeamsRegistry.load(org_root)
    (org_root / "workspaces" / "dev_agent").mkdir(parents=True)

    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    assert task_id is None
    assert state.queue.items == []
    audits = db.get_audit_logs("workspace-cleanup:skipped")
    assert any(
        r["action"] == "workspace_cleanup_skipped"
        and r["payload"].get("reason") == "agent_team_unresolved"
        for r in audits
    )


@pytest.mark.asyncio
async def test_trigger_audits_trigger_and_never_touches_schedules(
    tmp_path, test_settings, monkeypatch,
):
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)
    _make_schedule(db, schedule_id="SCHEDULE-001", spawned=[])

    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    audits = db.get_audit_logs(task_id)
    assert any(r["action"] == "workspace_cleanup_triggered" for r in audits)
    # The user Schedule row is byte-identical: no spawned_task_ids appended,
    # no status change, no purpose marker.
    schedule = db.schedules.get("SCHEDULE-001")
    assert schedule.spawned_task_ids == []
    assert schedule.status == ScheduleStatus.ARMED
    assert schedule.normalized_brief == "Unrelated user schedule brief."


# ── (c) exact first-two report-only behavior ──────────────────────────────

@pytest.mark.asyncio
async def _trigger_with_seeded_runs(
    tmp_path, test_settings, monkeypatch, *, seeded: int,
) -> str:
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)
    now = datetime.now(timezone.utc)
    for i in range(seeded):
        _insert_cleanup_task(
            db, task_id=f"TASK-{100 + i}", agent="dev_agent",
            created_at=now - timedelta(days=30 + i),
            status=TaskStatus.COMPLETED,
        )
    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    assert task_id is not None
    return db.get_task(task_id).brief


@pytest.mark.asyncio
async def test_first_two_runs_are_strictly_report_only(tmp_path, test_settings, monkeypatch):
    """Runs #1 and #2 per agent carry the STRICT report-only brief; run #3
    carries the approved TASK-5552 §4 cleanup brief (first-two boundary)."""
    brief1 = await _trigger_with_seeded_runs(
        tmp_path / "case1", test_settings, monkeypatch, seeded=0,
    )
    assert "THIS RUN IS STRICTLY REPORT-ONLY" in brief1
    assert "Allowed cache action" not in brief1
    assert "git -C <primary> worktree remove" not in brief1

    brief2 = await _trigger_with_seeded_runs(
        tmp_path / "case2", test_settings, monkeypatch, seeded=1,
    )
    assert "THIS RUN IS STRICTLY REPORT-ONLY" in brief2
    assert "Allowed cache action" not in brief2

    brief3 = await _trigger_with_seeded_runs(
        tmp_path / "case3", test_settings, monkeypatch, seeded=2,
    )
    assert "THIS RUN IS STRICTLY REPORT-ONLY" not in brief3
    assert "Allowed cache action" in brief3
    assert "git -C <primary> worktree remove" in brief3
    assert "blocked_on_job_ids" in brief3  # §4: jobs are diagnostics only
    assert "report-only" not in brief3.lower().replace(
        "none in report-only", "",
    ) or "STRICTLY REPORT-ONLY" not in brief3


@pytest.mark.asyncio
async def test_report_only_brief_has_no_candidate_or_path_enumeration(
    tmp_path, test_settings, monkeypatch,
):
    """The report-only brief never enumerates executable candidates or
    concrete paths; it is aggregate-only."""
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)

    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    brief = db.get_task(task_id).brief
    assert "safe to remove" not in brief.lower()
    assert "rm -rf" not in brief
    assert "git worktree remove" not in brief
    assert "delete" not in brief.lower().replace("do not delete", "")
    # The brief names no concrete path under the org root.
    assert "workspaces/dev_agent" not in brief
    assert "node_modules" not in brief


# ── (d) advisory wording + no candidate/safe-removal semantics ────────────

def test_note_wording_available():
    snap = wcs.WorkspaceContextSnapshot(
        measured_at="2026-08-28T09:32:00+00:00",
        workspaces_count=1,
        workspaces_bytes=4 * 1024 * 1024 * 1024,
        largest=[("dev_agent", 4 * 1024 * 1024 * 1024)],
        worktrees_registered=3,
        worktrees_terminal=2,
        worktrees_non_terminal=1,
        worktrees_unclassified=0,
        dep_dirs=2,
        dep_bytes=1024 * 1024,
        dep_dirs_in_worktrees=1,
        dep_bytes_in_worktrees=512 * 1024,
        live_sessions_count=1,
        live_sessions_agents=["dev_agent"],
    )
    note = wcs.format_workspace_context_note(snap)
    for required in (
        "ADVISORY ONLY", "STALE ON ARRIVAL", "NOT an eligibility list",
        "NOT a candidate list", "Re-derive every path and every fact",
        "measured_at", "dev_agent (4.0 GiB)", "2 terminal-task",
        "2 / 1.0 MiB", "1 (dev_agent)",
    ):
        assert required in note
    # No candidate/safe-removal semantics anywhere in the note.
    assert "safe to remove" not in note.lower()
    assert "candidate" not in note.lower().replace("not a candidate list", "")
    assert "remove" not in note.lower().replace("recommends or authorizes removal", "")


def test_note_wording_unavailable():
    snap = wcs.WorkspaceContextSnapshot(
        available=False, reason="measurement deadline exceeded",
    )
    note = wcs.format_workspace_context_note(snap)
    assert "measurement unavailable" in note
    assert "does not affect this run" in note
    assert "ADVISORY ONLY" in note


def test_inode_observation_is_fail_open_and_actionable(monkeypatch):
    class Stat:
        f_files = 100
        f_favail = 5

    snap = wcs.WorkspaceContextSnapshot()
    monkeypatch.setattr(wcs.os, "statvfs", lambda _path: Stat())
    wcs._observe_inodes(snap)
    assert (snap.inode_used, snap.inode_free, snap.inode_total) == (95, 5, 100)
    assert snap.inode_percent == 95.0
    assert snap.inode_threshold_state == "alert"
    note = wcs.format_workspace_context_note(snap)
    assert "temporary-file producers and filesystem usage" in note
    assert "advisory and not cleanup authority" in note

    monkeypatch.setattr(wcs.os, "statvfs", lambda _path: (_ for _ in ()).throw(OSError("down")))
    failed = wcs.WorkspaceContextSnapshot()
    wcs._observe_inodes(failed)
    assert failed.available is True
    assert failed.inode_available is False
    assert "down" in wcs.format_workspace_context_note(failed)


def test_scheduler_has_no_temporary_filesystem_mutation_surface():
    source = inspect.getsource(wcs)
    for forbidden in (
        "runtime.daemon.managed_temp",
        "os.rename(",
        "os.replace(",
        "os.unlink(",
        "shutil.rmtree(",
        ".unlink(",
    ):
        assert forbidden not in source


# ── (e) measurement: per-agent aggregates, symlink safety, fail-open ──────

def test_measure_aggregates_sizes_deps_and_worktree_status(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    _insert_cleanup_task(
        db, task_id="TASK-1", created_at=datetime.now(timezone.utc),
        status=TaskStatus.COMPLETED,
    )
    ws = tmp_path / "ws"
    _write_file(ws / "repos" / "r" / ".git" / "HEAD", 10)
    _write_file(ws / "repos" / "r" / "file.txt", 1000)
    _write_file(ws / "repos" / "r" / "node_modules" / "pkg" / "index.js", 2000)
    monkeypatch.setattr(
        wcs, "_git_worktree_paths",
        lambda repo_dir, timeout: ([ws / ".claude" / "worktrees" / "TASK-1-wt"], False),
    )
    snap = wcs.measure_workspace_context(ws, db=db, sessions=None)
    assert snap.available is True
    assert snap.workspaces_count == 1
    assert snap.workspaces_bytes == 10 + 1000 + 2000
    assert snap.largest == [("ws", 10 + 1000 + 2000)]
    assert snap.dep_dirs == 1
    assert snap.dep_bytes == 2000
    assert snap.worktrees_registered == 1
    assert snap.worktrees_terminal == 1
    assert snap.worktrees_unclassified == 0


def test_measure_skips_symlinks_and_bounds_walk(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    ws = tmp_path / "ws"
    _write_file(ws / "real.txt", 100)
    (ws / "link").symlink_to(ws)
    monkeypatch.setattr(wcs, "_git_worktree_paths", lambda repo_dir, timeout: ([], False))
    snap = wcs.measure_workspace_context(ws, db=db, sessions=None)
    assert snap.available is True
    assert snap.workspaces_bytes == 100  # the symlink target is never walked


def test_measure_missing_workspace_dir_is_empty_available(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    snap = wcs.measure_workspace_context(
        tmp_path / "nope", db=db, sessions=None,
    )
    assert snap.available is True
    assert snap.workspaces_bytes == 0


def test_measure_deadline_produces_unavailable_note(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    ws = tmp_path / "ws"
    _write_file(ws / "file.txt", 100)

    class _Clock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            self.now += 100.0  # each read is already past any deadline
            return self.now

    monkeypatch.setattr(wcs.time, "monotonic", _Clock())
    snap = wcs.measure_workspace_context(ws, db=db, sessions=None)
    assert snap.available is False
    assert snap.truncated is True
    assert snap.reason is not None


def test_measure_never_raises_on_unexpected_error(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    ws = tmp_path / "ws"

    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(wcs, "_measure", boom)
    snap = wcs.measure_workspace_context(ws, db=db, sessions=None)
    assert snap.available is False
    assert "measurement error" in (snap.reason or "")


# ── (f) deadline: one true wall-clock deadline across Git collection ──────

def test_measure_bounds_git_timeout_by_remaining_deadline_and_marks_unavailable(
    tmp_path, monkeypatch,
):
    """Each git subprocess gets min(per-call cap, remaining deadline); a
    subprocess that consumes the deadline marks the snapshot unavailable.
    """
    db = Database(tmp_path / "db.sqlite")
    ws = tmp_path / "ws"
    for repo in ("r1", "r2"):
        (ws / "repos" / repo / ".git").mkdir(parents=True)

    class _FakeClock:
        def __init__(self):
            self.now = 1000.0

        def __call__(self):
            return self.now

    clock = _FakeClock()
    monkeypatch.setattr(wcs.time, "monotonic", clock)

    git_timeouts: list[float] = []

    def fake_git(repo_dir, timeout):
        git_timeouts.append(timeout)
        clock.now += 9.0  # this git call consumes 9s of wall clock
        return [], False

    monkeypatch.setattr(wcs, "_git_worktree_paths", fake_git)

    snap = wcs.measure_workspace_context(
        ws, db=db, sessions=None, deadline_seconds=10.0,
    )
    # First call got the full per-call cap; the second was bounded to the
    # remaining budget (deadline = 1000 + 10 = 1010; after the first call the
    # clock is 1009, so remaining = 1.0 → min(5.0, 1.0) = 1.0).
    assert git_timeouts == [5.0, 1.0]
    assert snap.available is False
    assert snap.truncated is True
    assert "bounded limits" in (snap.reason or "")


def test_measure_expiry_after_last_repo_marks_unavailable(tmp_path, monkeypatch):
    """A git subprocess that finishes at/after the deadline on the LAST
    repository still flips the snapshot to unavailable."""
    db = Database(tmp_path / "db.sqlite")
    ws = tmp_path / "ws"
    (ws / "repos" / "r1" / ".git").mkdir(parents=True)

    class _FakeClock:
        def __init__(self):
            self.now = 1000.0

        def __call__(self):
            return self.now

    clock = _FakeClock()
    monkeypatch.setattr(wcs.time, "monotonic", clock)

    def fake_git(repo_dir, timeout):
        clock.now += 12.0  # single repo call overruns the whole 10s deadline
        return [], False

    monkeypatch.setattr(wcs, "_git_worktree_paths", fake_git)

    snap = wcs.measure_workspace_context(
        ws, db=db, sessions=None, deadline_seconds=10.0,
    )
    assert snap.available is False
    assert snap.truncated is True
    assert "bounded limits" in (snap.reason or "")


# ── (g) cardinality caps → truncated/unavailable, boundary tests ─────────

def test_measure_repo_cap_hit_marks_unavailable(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    ws = tmp_path / "ws"
    repos = ws / "repos"
    for i in range(wcs._MAX_REPOS_PER_WORKSPACE + 1):
        (repos / f"r{i}" / ".git").mkdir(parents=True)
    monkeypatch.setattr(wcs, "_git_worktree_paths", lambda repo_dir, timeout: ([], False))
    snap = wcs.measure_workspace_context(ws, db=db, sessions=None)
    assert snap.available is False
    assert snap.truncated is True
    assert "bounded limits" in (snap.reason or "")


def test_measure_worktree_cap_hit_marks_unavailable(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    ws = tmp_path / "ws"
    (ws / "repos" / "r1" / ".git").mkdir(parents=True)
    many = [str(tmp_path / "wt" / str(i)) for i in range(wcs._MAX_WORKTREES_PER_REPO + 1)]
    runner = _RecordingGitRun(by_repo={
        str(ws / "repos" / "r1"): many,
    })
    monkeypatch.setattr(wcs.subprocess, "run", runner)
    snap = wcs.measure_workspace_context(ws, db=db, sessions=None)
    assert snap.available is False
    assert snap.truncated is True
    assert "bounded limits" in (snap.reason or "")


def test_git_worktree_paths_cap_sets_truncated(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    many = [str(tmp_path / "wt" / str(i)) for i in range(wcs._MAX_WORKTREES_PER_REPO + 2)]
    runner = _RecordingGitRun(by_repo={str(repo): many})
    monkeypatch.setattr(wcs.subprocess, "run", runner)
    paths, truncated = wcs._git_worktree_paths(repo, timeout=5.0)
    assert truncated is True
    assert len(paths) == wcs._MAX_WORKTREES_PER_REPO


def test_git_worktree_paths_no_cap_no_truncation(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    three = [str(tmp_path / "wt" / str(i)) for i in range(3)]
    runner = _RecordingGitRun(by_repo={str(repo): three})
    monkeypatch.setattr(wcs.subprocess, "run", runner)
    paths, truncated = wcs._git_worktree_paths(repo, timeout=5.0)
    assert truncated is False
    assert len(paths) == 3


def test_iter_workspaces_cap_sets_truncated(tmp_path):
    ws = tmp_path / "ws"
    for i in range(wcs._MAX_WORKSPACES + 1):
        (ws / f"agent{i}").mkdir(parents=True)
    paths = type("P", (), {"workspaces_dir": ws})()
    dirs, truncated = wcs._iter_workspaces(paths)
    assert truncated is True
    assert len(dirs) == wcs._MAX_WORKSPACES


# ── (h) suffixed TASK-id conservative classification ─────────────────────

def test_task_id_from_worktree_name_exact_and_suffixed():
    assert wcs._task_id_from_worktree_name("TASK-5567") == "TASK-5567"
    assert wcs._task_id_from_worktree_name("TASK-5567-base691") == "TASK-5567"
    assert wcs._task_id_from_worktree_name("TASK-5829-base") == "TASK-5829"
    assert wcs._task_id_from_worktree_name("TASK-5603-baseline") == "TASK-5603"
    assert wcs._task_id_from_worktree_name("not-a-task") is None
    assert wcs._task_id_from_worktree_name("") is None


def test_measure_classifies_unknown_worktree_task_as_unclassified(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    ws = tmp_path / "ws"
    (ws / "repos" / "r1" / ".git").mkdir(parents=True)
    unknown_wt = str(tmp_path / "wt-unknown")
    runner = _RecordingGitRun(by_repo={
        str(ws / "repos" / "r1"): [unknown_wt],
    })
    monkeypatch.setattr(wcs.subprocess, "run", runner)
    snap = wcs.measure_workspace_context(ws, db=db, sessions=None)
    assert snap.worktrees_registered == 1
    assert snap.worktrees_unclassified == 1
    assert snap.worktrees_terminal == 0
    assert snap.worktrees_non_terminal == 0


def test_terminal_statuses_parity():
    from runtime.orchestrator import run_step
    assert wcs._TERMINAL_TASK_STATUSES == frozenset(run_step.TERMINAL_STATES)


# ── (i) SessionTracker live sessions ──────────────────────────────────────

def test_session_tracker_iter_active_snapshot():
    tracker = SessionTracker()
    tracker.set_active("TASK-1", "dev_agent", "sess-1")
    tracker.set_active("TASK-2", "qa_engineer", "sess-2")
    assert sorted(tracker.iter_active()) == [
        ("TASK-1", "dev_agent", "sess-1"),
        ("TASK-2", "qa_engineer", "sess-2"),
    ]
    count, agents = wcs._live_sessions(tracker)
    assert count == 2
    assert agents == ["dev_agent", "qa_engineer"]


def test_live_sessions_fail_open_on_none_or_error():
    assert wcs._live_sessions(None) == (0, [])

    class _BoomTracker:
        def iter_active(self):
            raise RuntimeError("boom")

    assert wcs._live_sessions(_BoomTracker()) == (0, [])


# ── (j) per-agent durable report thread: no minted token ──────────────────

@pytest.mark.asyncio
async def test_trigger_creates_per_agent_report_thread_without_minted_token(
    tmp_path, test_settings, monkeypatch,
):
    """First trigger creates ONE durable thread per agent (per-agent subject,
    owning agent as participant) and the brief instructs the participant-
    authorized task-bound send path — NO minted invocation token."""
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)

    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    task = db.get_task(task_id)
    subject = wcs.report_thread_subject("dev_agent")
    threads = db.list_threads(limit=50)
    matching = [t for t in threads if t.subject == subject]
    assert len(matching) == 1
    thread_id = matching[0].id
    # Owning agent is a participant (participant-authorized send path).
    assert db.is_thread_participant(thread_id, "dev_agent")
    # Brief carries the thread id + the task-bound send instruction.
    assert f"--thread-id {thread_id}" in task.brief
    assert "happyranch threads send --org" in task.brief
    assert "--task-id" in task.brief and "--session-id" in task.brief
    assert "no invocation token is needed" in task.brief
    # NO minted token anywhere in the brief.
    assert "invocation_token" not in task.brief
    assert "BOOTSTRAP" not in task.brief


def test_cleanup_report_thread_inserts_without_mention_routing_enabled_field(
    tmp_path,
):
    """TASK-6082 founder ruling: the THR-195 workspace-cleanup seam
    (``insert_cleanup_report_thread_and_task`` -> ``_insert_thread_uncommitted``)
    must insert a ThreadRecord that has NO ``mention_routing_enabled`` field
    (TASK-6027 unconditional routing) without AttributeError, and the
    persisted row keeps the inert legacy column at its shipped SQLite
    DEFAULT with every adjacent field at its own position."""
    db = Database(tmp_path / "db.sqlite")
    task = TaskRecord(
        id="T-CLEAN-1",
        brief="cleanup brief",
        team="engineering",
        assigned_agent="dev_agent",
    )
    thread_id = db.insert_cleanup_report_thread_and_task(
        thread_id="THR-CLEAN-1",
        subject="Cleanup report",
        composer="dev_agent",
        opening_body="opening body",
        initial_recipients=[],
        turn_cap=500,
        task=task,
    )
    assert thread_id == "THR-CLEAN-1"
    t = db.get_thread(thread_id)
    assert t.subject == "Cleanup report"
    assert t.turn_cap == 500
    assert t.composed_by == "dev_agent"
    assert t.composed_from_task_id == "T-CLEAN-1"
    assert not hasattr(t, "mention_routing_enabled")
    # The inert legacy column receives its shipped DEFAULT, not a shift.
    row = db._conn.execute(
        "SELECT mention_routing_enabled FROM threads WHERE id='THR-CLEAN-1'"
    ).fetchone()
    assert row["mention_routing_enabled"] == 1
    # The atomic producer also inserted the task and the composer participant.
    assert db.get_task("T-CLEAN-1") is not None
    assert db.is_thread_participant("THR-CLEAN-1", "dev_agent")


@pytest.mark.asyncio
async def test_trigger_reuses_same_thread_on_next_run(tmp_path, test_settings, monkeypatch):
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)

    state = _FakeDaemonState()
    task1_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    task1 = db.get_task(task1_id)
    thread_id = wcs._find_report_thread(db, "dev_agent").thread_id
    assert thread_id is not None
    assert f"--thread-id {thread_id}" in task1.brief

    task2_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    task2 = db.get_task(task2_id)
    assert f"--thread-id {thread_id}" in task2.brief
    subject = wcs.report_thread_subject("dev_agent")
    matching = [t for t in db.list_threads(limit=50) if t.subject == subject]
    assert len(matching) == 1
    assert matching[0].id == thread_id


@pytest.mark.asyncio
async def test_two_agents_get_distinct_report_threads(tmp_path, test_settings, monkeypatch):
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)
    _write_file(org.root / "workspaces" / "qa_engineer" / "f.txt", 1024)

    state = _FakeDaemonState()
    await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    await wcs.trigger_cleanup(
        org, agent="qa_engineer",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    dev_thread = wcs._find_report_thread(db, "dev_agent").thread_id
    qa_thread = wcs._find_report_thread(db, "qa_engineer").thread_id
    assert dev_thread is not None and qa_thread is not None
    assert dev_thread != qa_thread
    assert db.is_thread_participant(dev_thread, "dev_agent")
    assert not db.is_thread_participant(dev_thread, "qa_engineer")
    assert db.is_thread_participant(qa_thread, "qa_engineer")


@pytest.mark.asyncio
async def test_trigger_fails_closed_when_thread_creation_fails(
    tmp_path, test_settings, monkeypatch,
):
    """TASK-6046 finding 1: a thread-creation failure inside the atomic
    producer rolls back EVERYTHING — no task, no thread residue, no enqueue —
    and is audited as a skipped trigger (fail closed; a later tick retries
    cleanly with zero residue to resolve)."""
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)

    def boom(**kw):
        raise RuntimeError("thread create boom")

    monkeypatch.setattr(
        org.db, "insert_cleanup_report_thread_and_task", boom,
    )
    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    assert task_id is None
    assert state.queue.items == []
    assert db.list_tasks_by_brief_prefix(
        wcs._CLEANUP_BRIEF_MARKER, assigned_agent="dev_agent",
    ) == []
    assert db.list_threads(limit=1000) == []
    audits = db.get_audit_logs("workspace-cleanup:skipped")
    assert any(
        r["action"] == "workspace_cleanup_skipped"
        and r["payload"].get("reason") == "task_insert_failed"
        for r in audits
    )


# ── (k) kill switch (enabled-by-default org config flag) ─────────────────

@pytest.mark.asyncio
async def test_kill_switch_default_enabled_triggers(tmp_path, test_settings, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)

    state = _FakeDaemonState()
    state.orgs = {"test": org}
    state.metrics_registry = _FakeMetricsRegistry()

    triggered: list[str] = []

    async def fake_trigger(org, *, agent, enqueue, now_utc=None):
        triggered.append(agent)
        return "TASK-1"

    monkeypatch.setattr(wcs, "trigger_cleanup", fake_trigger)
    monkeypatch.setattr(
        wcs, "decide_cleanup_trigger",
        lambda **kw: wcs.CleanupTriggerDecision(True, None),
    )
    await wcs._tick_org(org, state, now_utc=_sunday_0330_utc())
    assert triggered == ["dev_agent", "qa_engineer"]


@pytest.mark.asyncio
async def test_kill_switch_disabled_skips_org(tmp_path, test_settings, monkeypatch):
    """workspace_cleanup.enabled: false in the org config.yaml disables the
    whole capability for the org (existing daemon/org config mechanism)."""
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    cfg_path = org.root / "org" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("workspace_cleanup:\n  enabled: false\n")

    state = _FakeDaemonState()
    state.orgs = {"test": org}
    state.metrics_registry = _FakeMetricsRegistry()

    triggered: list[str] = []

    async def fake_trigger(org, *, agent, enqueue, now_utc=None):
        triggered.append(agent)
        return "TASK-1"

    monkeypatch.setattr(wcs, "trigger_cleanup", fake_trigger)
    monkeypatch.setattr(
        wcs, "decide_cleanup_trigger",
        lambda **kw: wcs.CleanupTriggerDecision(True, None),
    )
    await wcs._tick_org(org, state, now_utc=_sunday_0330_utc())
    assert triggered == []


def test_org_config_kill_switch_parse():
    from runtime.orchestrator.org_config import OrgConfig, OrgConfigError

    assert OrgConfig().workspace_cleanup_enabled is True
    assert OrgConfig.load_from_text("").workspace_cleanup_enabled is True
    assert OrgConfig.load_from_text(
        "workspace_cleanup:\n  enabled: false\n"
    ).workspace_cleanup_enabled is False
    assert OrgConfig.load_from_text(
        "workspace_cleanup:\n  enabled: true\n"
    ).workspace_cleanup_enabled is True
    with pytest.raises(OrgConfigError):
        OrgConfig.load_from_text("workspace_cleanup:\n  enabled: nope\n")
    with pytest.raises(OrgConfigError):
        OrgConfig.load_from_text("workspace_cleanup: 42\n")


# ── (l) no Schedule / ordinary-session effect ────────────────────────────

def test_orchestrator_has_no_cleanup_seam():
    """The superseded prompt-seam is gone: the orchestrator no longer imports
    or calls any workspace-cleanup context builder, so ordinary and unrelated
    Schedule-spawned sessions are byte-identical BY CONSTRUCTION."""
    import runtime.orchestrator.orchestrator as orch_module
    source = inspect.getsource(orch_module)
    assert "workspace_context" not in source
    assert "maybe_build_cleanup_context_note" not in source


def test_no_schedule_store_reverse_lookup_survives():
    """find_by_spawned_task_id (the rejected Schedule discriminator) is gone."""
    import runtime.infrastructure.schedule_store as store_module
    assert not hasattr(store_module, "find_by_spawned_task_id")
    source = inspect.getsource(store_module)
    assert "find_by_spawned_task_id" not in source


# ── (m) full _run_agent shipping seam: no advisory note in ANY session ───

_TASK_CONTEXT_CONTRACT_IDS = (
    "start-task",
    "jobs",
    "make-worktree",
    "thread",
    "dream",
    "todos",
    "create-skill",
)


def _setup_protocol_skills(settings: Settings) -> None:
    for sid in _TASK_CONTEXT_CONTRACT_IDS:
        src = settings.get_protocol_dir() / "skills" / sid
        src.mkdir(parents=True, exist_ok=True)
        (src / "SKILL.md").write_text(f"# {sid}\n\nSkill body for {sid}.\n")


def _setup_agent_workspace(runtime, agent: str, provider: str) -> None:
    from runtime.orchestrator.agent_def import AgentDef, render_agent_text

    ws = runtime.workspaces_dir / agent
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "task_history.md").write_text(f"# Task History: {agent}\n\n")
    (ws / "AGENTS.md").write_text(f"# Agent: {agent}\n")
    ad = AgentDef(
        name=agent, team="engineering", role="worker",
        executor=provider, allow_rules=(), repos={},
        enrolled_by=None, enrolled_at_task=None, enrolled_at=None,
        system_prompt=f"You are {agent}.", description="", model=None,
    )
    runtime.agents_dir.mkdir(parents=True, exist_ok=True)
    (runtime.agents_dir / f"{agent}.md").write_text(render_agent_text(ad))


def _run_task_session(orch: Orchestrator, task_id: str, mock_executor) -> str:
    """Run one task session, capturing the composed executor prompt."""
    captured: dict[str, str] = {}

    def fake_executor_run(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return __import__(
            "runtime.orchestrator.executors", fromlist=["ExecutorResult"],
        ).ExecutorResult(
            success=True, duration_seconds=1, session_id=kwargs["session_id"],
        )

    mock_executor.run.side_effect = fake_executor_run
    with patch.object(orch, "_build_executor", return_value=mock_executor):
        orch._run_agent(task_id, "dev_agent", "")
    return captured["prompt"]


def test_no_session_prompt_contains_cleanup_note(
    test_settings, test_runtime, monkeypatch,
):
    """Full _run_agent shipping seam: no session — ordinary OR
    schedule-spawned — receives any workspace-cleanup advisory note, so every
    session prompt stays byte-identical to pre-feature main."""
    _setup_protocol_skills(test_settings)
    test_runtime.root.mkdir(parents=True, exist_ok=True)
    _setup_agent_workspace(test_runtime, "dev_agent", "claude")

    db = Database(test_runtime.db_path)
    teams = TeamsRegistry.load(test_runtime.root)
    orch = Orchestrator(
        db=db, settings=test_settings,
        paths=test_runtime, slug="test", teams=teams,
    )
    ordinary_task_id = orch.create_task("Ordinary work")
    spawned_task_id = orch.create_task("Spawned by a Schedule")
    _make_schedule(db, schedule_id="SCHEDULE-001", spawned=[spawned_task_id])

    mock_executor = MagicMock()

    ordinary_prompt = _run_task_session(orch, ordinary_task_id, mock_executor)
    spawned_prompt = _run_task_session(orch, spawned_task_id, mock_executor)

    assert "Workspace disk context" not in ordinary_prompt
    assert "Workspace disk context" not in spawned_prompt
    assert "ADVISORY ONLY" not in spawned_prompt
    # The shared repo-freshness note is still present for every session.
    assert "Repository freshness" in ordinary_prompt
    assert "Repository freshness" in spawned_prompt


# ── (n) daemon loop: trigger/non-trigger through the async loop ──────────

class _FakeMetricsRegistry:
    def record_loop_tick(self, *args, **kwargs) -> None:
        pass


@pytest.mark.asyncio
async def test_loop_ticks_and_triggers_when_due(tmp_path, test_settings, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)

    state = _FakeDaemonState()
    state.orgs = {"test": org}
    state.metrics_registry = _FakeMetricsRegistry()

    due = _sunday_0330_utc()
    triggered: list[str] = []

    async def fake_trigger(org, *, agent, enqueue, now_utc=None):
        triggered.append(agent)
        return "TASK-1"

    monkeypatch.setattr(wcs, "trigger_cleanup", fake_trigger)
    monkeypatch.setattr(
        wcs, "decide_cleanup_trigger",
        lambda **kw: wcs.CleanupTriggerDecision(True, None),
    )
    await wcs._tick_org(org, state, now_utc=due)
    assert triggered == ["dev_agent", "qa_engineer"]


@pytest.mark.asyncio
async def test_loop_ticks_and_skips_when_not_due(tmp_path, test_settings, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)

    state = _FakeDaemonState()
    state.orgs = {"test": org}
    state.metrics_registry = _FakeMetricsRegistry()

    triggered: list[str] = []

    async def fake_trigger(org, *, agent, enqueue, now_utc=None):
        triggered.append(agent)
        return "TASK-1"

    monkeypatch.setattr(wcs, "trigger_cleanup", fake_trigger)
    monkeypatch.setattr(
        wcs, "decide_cleanup_trigger",
        lambda **kw: wcs.CleanupTriggerDecision(False, "not_due"),
    )
    await wcs._tick_org(org, state, now_utc=_sunday_0330_utc())
    assert triggered == []


@pytest.mark.asyncio
async def test_tick_org_below_threshold_audits_once_at_weekly_boundary(
    tmp_path, test_settings, monkeypatch,
):
    """An unserviced below-threshold agent is observed once at the weekly
    decision boundary, not once per minute for the rest of the week."""
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    cfg_path = org.root / "org" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("timezone: UTC\n")
    state = _FakeDaemonState()

    monkeypatch.setattr(
        wcs, "measure_workspace_context",
        lambda *a, **kw: wcs.WorkspaceContextSnapshot(
            available=True, workspaces_bytes=0,
        ),
    )
    occurrence = _sunday_0330_utc()
    for minute in range(3):
        await wcs._tick_org(
            org, state, now_utc=occurrence + timedelta(minutes=minute),
        )

    audits = db.get_audit_logs("workspace-cleanup:skipped")
    below_threshold = [
        row for row in audits
        if row["action"] == "workspace_cleanup_skipped"
        and row["agent"] == "dev_agent"
        and row["payload"].get("reason") == "workspace_below_threshold"
    ]
    assert len(below_threshold) == 1


@pytest.mark.asyncio
async def test_tick_org_retries_below_threshold_at_later_cooldown_boundary(
    tmp_path, test_settings, monkeypatch,
):
    """A rolling cooldown can suppress the first cadence boundary; the next
    weekly boundary observes below-threshold state once after it expires."""
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    cfg_path = org.root / "org" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("timezone: UTC\n")
    state = _FakeDaemonState()

    monkeypatch.setattr(
        wcs, "measure_workspace_context",
        lambda *a, **kw: wcs.WorkspaceContextSnapshot(
            available=True, workspaces_bytes=0,
        ),
    )
    occurrence = _sunday_0330_utc()
    _insert_cleanup_task(
        db,
        task_id="TASK-100",
        agent="dev_agent",
        created_at=occurrence - timedelta(hours=15),
        status=TaskStatus.COMPLETED,
    )

    await wcs._tick_org(org, state, now_utc=occurrence)
    await wcs._tick_org(org, state, now_utc=occurrence + timedelta(minutes=1))
    later_boundary = occurrence + timedelta(days=7)
    await wcs._tick_org(org, state, now_utc=later_boundary)
    await wcs._tick_org(
        org, state, now_utc=later_boundary + timedelta(minutes=1),
    )

    audits = db.get_audit_logs("workspace-cleanup:skipped")
    below_threshold = [
        row for row in audits
        if row["action"] == "workspace_cleanup_skipped"
        and row["agent"] == "dev_agent"
        and row["payload"].get("reason") == "workspace_below_threshold"
    ]
    assert len(below_threshold) == 1


@pytest.mark.asyncio
async def test_shipping_loop_reaches_occurrence_once_across_phase_and_processing_drift(
    tmp_path, test_settings, monkeypatch,
):
    """The shipping loop cannot skip 03:30 when scan work plus the full sleep
    advances the next scan from just before the boundary to after 03:31."""
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    cfg_path = org.root / "org" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("timezone: UTC\n")
    state = _FakeDaemonState()
    state.orgs = {"test": org}
    state.metrics_registry = _FakeMetricsRegistry()

    monkeypatch.setattr(
        wcs, "measure_workspace_context",
        lambda *a, **kw: wcs.WorkspaceContextSnapshot(
            available=True,
            workspaces_bytes=0,
            measured_at="2026-08-30T03:30:00+00:00",
        ),
    )
    occurrence = _sunday_0330_utc()
    scan_times = iter([
        occurrence - timedelta(microseconds=500_000),  # cursor initialization
        occurrence - timedelta(microseconds=500_000),  # first scan
        occurrence + timedelta(minutes=1, microseconds=500_000),
        occurrence + timedelta(minutes=2, seconds=2),
    ])

    class _DriftingDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(scan_times)

    sleeps = 0

    async def fake_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(wcs, "datetime", _DriftingDateTime)
    monkeypatch.setattr(wcs.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await wcs.workspace_cleanup_scheduler_loop(
            state, interval_seconds=60, warm_up_seconds=0,
        )

    below_threshold = [
        row for row in db.get_audit_logs("workspace-cleanup:skipped")
        if row["action"] == "workspace_cleanup_skipped"
        and row["agent"] == "dev_agent"
        and row["payload"].get("reason") == "workspace_below_threshold"
    ]
    assert len(below_threshold) == 1


@pytest.mark.asyncio
async def test_shipping_loop_catches_up_current_window_once_on_startup(
    tmp_path, test_settings, monkeypatch,
):
    """A daemon starting after 03:30 evaluates the current weekly occurrence
    once, without replaying it on later loop ticks."""
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    cfg_path = org.root / "org" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("timezone: UTC\n")
    state = _FakeDaemonState()
    state.orgs = {"test": org}
    state.metrics_registry = _FakeMetricsRegistry()

    monkeypatch.setattr(
        wcs, "measure_workspace_context",
        lambda *a, **kw: wcs.WorkspaceContextSnapshot(
            available=True, workspaces_bytes=0,
            measured_at="2026-08-30T07:30:00+00:00",
        ),
    )
    occurrence = _sunday_0330_utc()
    scan_times = iter([
        occurrence + timedelta(hours=4),
        occurrence + timedelta(hours=4, seconds=1),
        occurrence + timedelta(hours=4, minutes=1),
    ])

    class _StartupDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(scan_times)

    sleeps = 0

    async def fake_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(wcs, "datetime", _StartupDateTime)
    monkeypatch.setattr(wcs.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await wcs.workspace_cleanup_scheduler_loop(
            state, interval_seconds=60, warm_up_seconds=0,
        )

    below_threshold = [
        row for row in db.get_audit_logs("workspace-cleanup:skipped")
        if row["action"] == "workspace_cleanup_skipped"
        and row["agent"] == "dev_agent"
        and row["payload"].get("reason") == "workspace_below_threshold"
    ]
    assert len(below_threshold) == 1


@pytest.mark.asyncio
async def test_loop_waits_out_boot_warm_up_before_any_tick(
    tmp_path, test_settings, monkeypatch,
):
    """The daemon loop honours a boot warm-up grace: no trigger scan runs
    before ``warm_up_seconds`` elapse (short-lived lifespan contexts — e.g.
    the dashboard lifespan test — never see trigger side effects)."""
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)

    state = _FakeDaemonState()
    state.orgs = {"test": org}
    state.metrics_registry = _FakeMetricsRegistry()

    ticks: list[str] = []

    async def fake_tick(org, state, now_utc, previous_scan_utc=None):
        ticks.append(org.slug)

    monkeypatch.setattr(wcs, "_tick_org", fake_tick)

    task = asyncio.ensure_future(
        wcs.workspace_cleanup_scheduler_loop(
            state, interval_seconds=0.01, warm_up_seconds=5.0,
        )
    )
    await asyncio.sleep(0.05)  # well inside the 5s warm-up
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ticks == []

    # With a zero warm-up the scan runs on the first tick.
    task2 = asyncio.ensure_future(
        wcs.workspace_cleanup_scheduler_loop(
            state, interval_seconds=0.01, warm_up_seconds=0.0,
        )
    )
    await asyncio.sleep(0.05)
    task2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task2
    assert ticks  # the scan ran (at least once) without a warm-up


# ── (o) TASK-6043 finding 1: history lookup failure/indeterminacy fails closed ──

def test_trigger_decision_suppresses_on_history_lookup_error(tmp_path, monkeypatch):
    """A task-history lookup error must NEVER read as 'no prior run → trigger'.
    The decision seam represents the error as indeterminate and suppresses."""
    db = Database(tmp_path / "db.sqlite")

    def boom(*a, **kw):
        raise RuntimeError("db read failure")

    monkeypatch.setattr(db, "list_tasks_by_brief_prefix", boom)
    decision = wcs.decide_cleanup_trigger(
        db=db, agent="dev_agent",
        now_utc=_sunday_0330_utc(), tz=timezone.utc,
    )
    assert decision.should_trigger is False
    assert decision.reason == "history_indeterminate"


@pytest.mark.asyncio
async def test_tick_org_audits_boundary_history_failure_once_without_task(
    tmp_path, test_settings, monkeypatch,
):
    """The shipping tick records one fail-closed boundary decision while
    adjacent scans remain quiet and no cleanup task is produced."""
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    cfg_path = org.root / "org" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("timezone: UTC\n")
    state = _FakeDaemonState()

    def boom(*a, **kw):
        raise RuntimeError("db read failure")

    monkeypatch.setattr(db, "list_tasks_by_brief_prefix", boom)
    occurrence = _sunday_0330_utc()
    await wcs._tick_org(
        org, state,
        now_utc=occurrence - timedelta(seconds=1),
        previous_scan_utc=occurrence - timedelta(minutes=1),
    )
    await wcs._tick_org(
        org, state,
        now_utc=occurrence,
        previous_scan_utc=occurrence - timedelta(seconds=1),
    )
    await wcs._tick_org(
        org, state,
        now_utc=occurrence + timedelta(minutes=1),
        previous_scan_utc=occurrence,
    )

    assert state.queue.items == []
    assert db.list_tasks() == []
    history_skips = [
        row for row in db.get_audit_logs("workspace-cleanup:skipped")
        if row["action"] == "workspace_cleanup_skipped"
        and row["agent"] == "dev_agent"
        and row["payload"].get("reason") == "history_indeterminate"
    ]
    assert len(history_skips) == 1


@pytest.mark.asyncio
async def test_trigger_audits_skip_on_history_indeterminate(
    tmp_path, test_settings, monkeypatch,
):
    """The public trigger seam fails closed on an indeterminate history: no
    task, no enqueue, explicit audit — even when the workspace is huge."""
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)

    def boom(*a, **kw):
        raise RuntimeError("db read failure")

    monkeypatch.setattr(db, "list_tasks_by_brief_prefix", boom)
    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    assert task_id is None
    assert state.queue.items == []
    audits = db.get_audit_logs("workspace-cleanup:skipped")
    assert any(
        r["action"] == "workspace_cleanup_skipped"
        and r["payload"].get("reason") == "history_indeterminate"
        for r in audits
    )


# ── (p) TASK-6043 finding 2: authoritative marker filter (no bounded-scan exhaustion) ──

def test_cleanup_task_history_orders_newest_first(tmp_path):
    """The marker-filtered history returns the newest cleanup task first and
    an exact count — the inputs of weekly dedup, cooldown, and run number."""
    db = Database(tmp_path / "db.sqlite")
    now = datetime.now(timezone.utc)
    for i, days_ago in enumerate((60, 30, 7)):
        _insert_cleanup_task(
            db, task_id=f"TASK-{100 + i}", agent="dev_agent",
            created_at=now - timedelta(days=days_ago),
            status=TaskStatus.COMPLETED,
        )
    history = wcs._cleanup_task_history(db, "dev_agent")
    assert history.indeterminate is False
    assert history.count == 3
    assert history.latest is not None
    assert history.latest.id == "TASK-102"  # newest (7 days ago)


def test_cleanup_history_finds_marker_row_beyond_former_scan_bound(tmp_path):
    """An old cleanup row buried under >1000 newer ordinary tasks is still
    found by the SQL-side marker filter — the former bounded scan (1000 rows)
    would have hidden it and let the daemon double-trigger."""
    db = Database(tmp_path / "db.sqlite")
    now = datetime.now(timezone.utc)
    # Non-terminal cleanup run 8 days ago (younger than the 7-day cooldown is
    # irrelevant: non-terminal suppression applies regardless of age).
    _insert_cleanup_task(
        db, task_id="TASK-100", agent="dev_agent",
        created_at=now - timedelta(days=8), status=TaskStatus.IN_PROGRESS,
    )
    # 1100 NEWER ordinary tasks (no marker) bury it beyond the old 1000-row
    # newest-first scan bound.
    for i in range(1100):
        db.insert_task(TaskRecord(
            id=f"TASK-{2000 + i}",
            brief=f"ordinary work {i}",
            team="engineering",
            assigned_agent="dev_agent",
            created_at=now - timedelta(days=7) + timedelta(
                seconds=i,
            ),
        ))
    history = wcs._cleanup_task_history(db, "dev_agent")
    assert history.indeterminate is False
    assert history.count == 1
    assert history.latest is not None
    assert history.latest.id == "TASK-100"

    # The decision seam honors the found non-terminal row: no double trigger.
    decision = wcs.decide_cleanup_trigger(
        db=db, agent="dev_agent",
        now_utc=_sunday_0330_utc(), tz=timezone.utc,
    )
    assert decision.should_trigger is False
    assert decision.reason == "prior_run_in_flight"


def test_cleanup_history_is_per_agent_and_prefix_exact(tmp_path):
    """The marker filter is per agent and matches the daemon marker PREFIX
    exactly (brief.startswith semantics — identical to the pre-change
    in-memory check): a brief that merely CONTAINS the marker mid-text is not
    a daemon-marked cleanup run, while any brief starting with the marker
    is."""
    db = Database(tmp_path / "db.sqlite")
    now = datetime.now(timezone.utc)
    _insert_cleanup_task(
        db, task_id="TASK-100", agent="dev_agent",
        created_at=now - timedelta(days=1), status=TaskStatus.COMPLETED,
    )
    _insert_cleanup_task(
        db, task_id="TASK-101", agent="qa_engineer",
        created_at=now - timedelta(days=1), status=TaskStatus.COMPLETED,
    )
    # Contains the marker mid-text but does not START with it → not matched.
    db.insert_task(TaskRecord(
        id="TASK-102",
        brief="user-written: " + wcs._CLEANUP_BRIEF_MARKER + " (not a daemon run)",
        team="engineering",
        assigned_agent="dev_agent",
        created_at=now,
    ))
    # Starts with the marker (suffixed content) → matched, exactly like the
    # former brief.startswith(marker) check.
    db.insert_task(TaskRecord(
        id="TASK-103",
        brief=wcs._CLEANUP_BRIEF_MARKER + " (suffixed daemon run)",
        team="engineering",
        assigned_agent="dev_agent",
        created_at=now,
    ))
    dev = wcs._cleanup_task_history(db, "dev_agent")
    assert dev.count == 2
    assert dev.latest.id == "TASK-103"  # newest first
    qa = wcs._cleanup_task_history(db, "qa_engineer")
    assert qa.count == 1


# ── (q) TASK-6043 finding 3: atomic task-producer seam ───────────────────

@pytest.mark.asyncio
async def test_trigger_allocates_task_id_after_awaited_measurement(
    tmp_path, test_settings, monkeypatch,
):
    """The task id is allocated only AFTER the awaited measurement. A foreign
    producer inserting a task DURING the measurement cannot claim the id the
    cleanup trigger will use, and the report thread is linked only to the
    real cleanup task — never a falsely linked thread."""
    import threading

    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)

    entered = threading.Event()
    release = threading.Event()

    def gated_measure(*a, **kw):
        entered.set()
        assert release.wait(10)
        return wcs.WorkspaceContextSnapshot(
            available=True,
            workspaces_bytes=2 ** 30,
            workspaces_count=1,
            largest=[("dev_agent", 2 ** 30)],
        )

    monkeypatch.setattr(wcs, "measure_workspace_context", gated_measure)

    state = _FakeDaemonState()
    trigger_task = asyncio.create_task(
        wcs.trigger_cleanup(
            org, agent="dev_agent",
            enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
        )
    )

    # Wait until the awaited measurement is in flight, then have a foreign
    # producer allocate+insert the next task id — the exact interleaving that
    # used to race the pre-selected id (TASK-6043 finding 3).
    while not entered.is_set():
        await asyncio.sleep(0.001)
    foreign_id = org.db.next_task_id()
    org.db.insert_task(TaskRecord(
        id=foreign_id, brief="foreign ordinary work",
        team="engineering", assigned_agent="dev_agent",
    ))
    release.set()

    task_id = await trigger_task
    assert task_id is not None
    # The cleanup id was allocated AFTER the foreign insert — never the same,
    # never a collision.
    assert task_id != foreign_id
    assert int(task_id.split("-")[-1]) > int(foreign_id.split("-")[-1])

    # The inserted cleanup task is a clean root: no parent, no thread dispatch.
    task = db.get_task(task_id)
    assert task.parent_task_id is None
    assert task.dispatched_from_thread_id is None

    # The report thread is linked ONLY to the real cleanup task.
    subject = wcs.report_thread_subject("dev_agent")
    matching = [t for t in db.list_threads(limit=50) if t.subject == subject]
    assert len(matching) == 1
    assert matching[0].composed_from_task_id == task_id


@pytest.mark.asyncio
async def test_trigger_insert_failure_leaves_zero_residue_then_retry_succeeds(
    tmp_path, test_settings, monkeypatch,
):
    """TASK-6046 finding 1 probe: an insertion failure at the atomic producer
    leaves ZERO durable residue (no thread row, participant, message, turn,
    or thread/task audit rows; no task; no enqueue) and a later retry
    succeeds exactly once — one task, one thread, one enqueue."""
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)

    real_producer = org.db.insert_cleanup_report_thread_and_task
    calls = {"n": 0}

    def flaky_producer(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("simulated mid-transaction producer failure")
        return real_producer(**kw)

    monkeypatch.setattr(
        org.db, "insert_cleanup_report_thread_and_task", flaky_producer,
    )

    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    assert task_id is None
    assert state.queue.items == []

    # ZERO residue across every affected durable table/audit.
    assert db.list_threads(limit=1000) == []
    assert db._conn.execute(
        "SELECT COUNT(*) FROM thread_participants"
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM thread_messages"
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action IN "
        "('thread_started', 'thread_message_sent')"
    ).fetchone()[0] == 0
    assert db.list_tasks_by_brief_prefix(
        wcs._CLEANUP_BRIEF_MARKER, assigned_agent="dev_agent",
    ) == []
    audits = db.get_audit_logs("workspace-cleanup:skipped")
    assert any(
        r["action"] == "workspace_cleanup_skipped"
        and r["payload"].get("reason") == "task_insert_failed"
        for r in audits
    )

    # A later retry succeeds exactly once: one task, one thread, one enqueue.
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    assert task_id is not None
    assert state.queue.items == [(org.slug, task_id)]
    cleanup = db.list_tasks_by_brief_prefix(
        wcs._CLEANUP_BRIEF_MARKER, assigned_agent="dev_agent",
    )
    assert [t.id for t in cleanup] == [task_id]
    subject = wcs.report_thread_subject("dev_agent")
    matching = [t for t in db.list_threads(limit=1000) if t.subject == subject]
    assert len(matching) == 1
    assert matching[0].composed_from_task_id == task_id
    assert db.is_thread_participant(matching[0].id, "dev_agent")
    opening = db.get_thread_message_by_seq(matching[0].id, 1)
    assert opening is not None
    assert opening.body_markdown.startswith(wcs._REPORT_THREAD_OPENING_PREFIX)
    assert db.get_thread(matching[0].id).turns_used == 1


@pytest.mark.asyncio
async def test_trigger_insert_failure_on_existing_thread_touches_no_thread_rows(
    tmp_path, test_settings, monkeypatch,
):
    """When the durable thread already exists, an insert failure happens on
    the task-only path — there is no thread work that could leave residue:
    the existing thread (row/participant/message/turns) is untouched."""
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)

    state = _FakeDaemonState()
    first_task = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    thread_id = wcs._find_report_thread(db, "dev_agent").thread_id
    assert thread_id is not None

    def flaky_insert(task):
        raise Exception("simulated concurrent id collision")

    monkeypatch.setattr(org.db, "insert_task", flaky_insert)
    second = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    assert second is None
    assert state.queue.items == [(org.slug, first_task)]
    # The existing thread is untouched: exactly one thread, one participant,
    # one opening message, one turn, no extra audits.
    subject = wcs.report_thread_subject("dev_agent")
    matching = [t for t in db.list_threads(limit=1000) if t.subject == subject]
    assert len(matching) == 1
    assert matching[0].id == thread_id
    assert db.get_thread(thread_id).turns_used == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM thread_messages WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()[0] == 1
    audits = db.get_audit_logs("workspace-cleanup:skipped")
    assert any(
        r["action"] == "workspace_cleanup_skipped"
        and r["payload"].get("reason") == "task_insert_failed"
        for r in audits
    )


@pytest.mark.asyncio
async def test_trigger_fails_closed_on_intermittent_identity_read_then_recovers(
    tmp_path, test_settings, monkeypatch,
):
    """TASK-6046 finding 2 probe: a transient report-thread IDENTITY read
    failure with an existing valid thread must NOT create a duplicate. The
    history read succeeds but the subsequent identity read fails once: the
    trigger fails closed (no task, no enqueue, audited reason), the existing
    open thread is untouched, and the next attempt recovers — one open
    thread, no duplicate, the task references the existing thread."""
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)

    # A valid existing report thread behind a terminal cleanup task — the
    # state after an earlier successful trigger + completed run.
    prior_task_id = "TASK-900"
    _insert_cleanup_task(
        db, task_id=prior_task_id, agent="dev_agent",
        created_at=datetime.now(timezone.utc) - timedelta(days=30),
        status=TaskStatus.COMPLETED,
    )
    subject = wcs.report_thread_subject("dev_agent")
    existing_tid = _insert_thread_via_shared_helper(
        org, agent="dev_agent", subject=subject,
        body_text=wcs._REPORT_THREAD_OPENING_PREFIX + " daemon opening",
        task_id=prior_task_id,
    )
    assert wcs._find_report_thread(db, "dev_agent").state == "found"

    # Intermittent identity read: history succeeds, provenance read fails
    # exactly once.
    real_identity_read = org.db.list_threads_by_composed_from_task_id
    calls = {"n": 0}

    def flaky_identity_read(task_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("simulated transient identity-read failure")
        return real_identity_read(task_id)

    monkeypatch.setattr(
        org.db, "list_threads_by_composed_from_task_id", flaky_identity_read,
    )

    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    assert task_id is None
    assert state.queue.items == []
    audits = db.get_audit_logs("workspace-cleanup:skipped")
    assert any(
        r["action"] == "workspace_cleanup_skipped"
        and r["payload"].get("reason") == "report_thread_indeterminate"
        and r["payload"].get("detail") == "provenance_lookup_failed"
        for r in audits
    )
    # Exactly ONE open report thread — no duplicate was created.
    matching = [t for t in db.list_threads(limit=1000) if t.subject == subject]
    assert len(matching) == 1
    assert matching[0].id == existing_tid
    assert matching[0].status.value == "open"

    # Successful recovery: the next trigger resolves the existing thread and
    # creates the task referencing it — still one open thread.
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    assert task_id is not None
    assert state.queue.items == [(org.slug, task_id)]
    task = db.get_task(task_id)
    assert f"--thread-id {existing_tid}" in task.brief
    matching = [t for t in db.list_threads(limit=1000) if t.subject == subject]
    assert len(matching) == 1
    assert matching[0].id == existing_tid
    assert wcs._find_report_thread(db, "dev_agent").thread_id == existing_tid


# ── (r) TASK-6043 finding 4: authoritative durable thread identity ───────

def _insert_thread_via_shared_helper(
    org, *, agent, subject, body_text, task_id,
) -> str:
    """Create a thread through the exact shared compose helper the daemon
    uses (participant/turn/audit semantics), with arbitrary provenance — the
    shape a user-created subject collision would take."""
    from runtime.daemon.routes.threads import _create_agent_thread_locked
    from runtime.orchestrator.org_config import (
        OrgConfig,
        resolve_org_setting_threads,
    )

    turn_cap = resolve_org_setting_threads(
        org.db, code_default=OrgConfig(),
    )["default_turn_cap"]
    thread_id, _seq, _tokens, _addr = _create_agent_thread_locked(
        org,
        composer=agent,
        subject=subject,
        body_text=body_text,
        recipients=["@founder"],
        turn_cap=turn_cap,
        composed_from_task_id=task_id,
    )
    return thread_id


@pytest.mark.asyncio
async def test_find_report_thread_rejects_user_subject_collisions(
    tmp_path, test_settings, monkeypatch,
):
    """A user-created thread with the fixed subject is NEVER selected as the
    durable report thread — whether its provenance is an ordinary task or
    even a cleanup task (the daemon's opening message is the tiebreaker)."""
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)
    subject = wcs.report_thread_subject("dev_agent")

    # Collision A: ordinary-task provenance, same subject.
    ordinary_task_id = "TASK-900"
    db.insert_task(TaskRecord(
        id=ordinary_task_id, brief="ordinary work",
        team="engineering", assigned_agent="dev_agent",
    ))
    user_tid_a = _insert_thread_via_shared_helper(
        org, agent="dev_agent", subject=subject,
        body_text="a user's own thread, not the daemon's",
        task_id=ordinary_task_id,
    )

    # Collision B: cleanup-task provenance but a non-daemon opening message.
    _insert_cleanup_task(
        db, task_id="TASK-901", agent="dev_agent",
        created_at=datetime.now(timezone.utc) - timedelta(days=30),
        status=TaskStatus.COMPLETED,
    )
    user_tid_b = _insert_thread_via_shared_helper(
        org, agent="dev_agent", subject=subject,
        body_text="user content that happens to match the subject",
        task_id="TASK-901",
    )

    # Neither collision resolves to the durable report thread.
    assert wcs._find_report_thread(db, "dev_agent").state == "absent"

    # The daemon's own trigger creates the real thread and resolves to it.
    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    found = wcs._find_report_thread(db, "dev_agent")
    assert found.state == "found"
    assert found.thread_id not in (user_tid_a, user_tid_b)
    assert db.get_thread(found.thread_id).composed_from_task_id == task_id


@pytest.mark.asyncio
async def test_find_report_thread_requires_participant_membership(
    tmp_path, test_settings, monkeypatch,
):
    """A thread the owning agent is not a participant of can never be
    selected — the participant-authorized send path would otherwise fail."""
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)

    state = _FakeDaemonState()
    task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    daemon_tid = wcs._find_report_thread(db, "dev_agent").thread_id
    assert daemon_tid is not None
    assert db.is_thread_participant(daemon_tid, "dev_agent")

    # Remove the owning agent from the thread → it is no longer a valid
    # identity and must NOT be selected.
    db.remove_thread_participant(daemon_tid, "dev_agent")
    assert wcs._find_report_thread(db, "dev_agent").state == "absent"


@pytest.mark.asyncio
async def test_find_report_thread_located_beyond_open_presentation_limit(
    tmp_path, test_settings, monkeypatch,
):
    """The durable thread is found even when buried under >500 newer open
    threads — the old 500-open-row presentation scan would have missed it and
    duplicated it on the next trigger."""
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)

    state = _FakeDaemonState()
    first_task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    daemon_tid = wcs._find_report_thread(db, "dev_agent").thread_id
    assert daemon_tid is not None

    # 549 newer unrelated open threads push the daemon thread beyond any
    # 500-row open presentation page.
    for i in range(549):
        db.insert_thread(ThreadRecord(
            id=db.next_thread_id(),
            subject=f"unrelated thread {i}",
            turn_cap=500,
            composed_by="someone_else",
        ))

    assert wcs._find_report_thread(db, "dev_agent").thread_id == daemon_tid

    # The next trigger reuses the SAME thread — no duplicate is created.
    second_task_id = await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    subject = wcs.report_thread_subject("dev_agent")
    matching = [t for t in db.list_threads(limit=1000) if t.subject == subject]
    assert len(matching) == 1
    assert matching[0].id == daemon_tid
    assert first_task_id != second_task_id


@pytest.mark.asyncio
async def test_find_report_thread_does_not_reuse_closed_thread(
    tmp_path, test_settings, monkeypatch,
):
    """A closed (archived) report thread is never reused: the daemon creates
    a fresh open thread on the next trigger."""
    monkeypatch.setattr(wcs, "_MIN_WORKSPACE_TRIGGER_BYTES", 1)
    db = Database(tmp_path / "db.sqlite")
    org = _org_with_workspaces(tmp_path, db, test_settings)
    _write_file(org.root / "workspaces" / "dev_agent" / "f.txt", 1024)

    state = _FakeDaemonState()
    await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    daemon_tid = wcs._find_report_thread(db, "dev_agent").thread_id
    assert daemon_tid is not None
    db.archive_thread_and_reset_sessions(
        daemon_tid, summary="rollup complete",
        audit_scope_id="workspace-cleanup:test", audit_agent="dev_agent",
    )
    assert wcs._find_report_thread(db, "dev_agent").state == "absent"

    # A new trigger creates a fresh open thread (the closed one stays).
    await wcs.trigger_cleanup(
        org, agent="dev_agent",
        enqueue=lambda slug, tid: state.queue.enqueue(slug, tid),
    )
    new_tid = wcs._find_report_thread(db, "dev_agent").thread_id
    assert new_tid is not None
    assert new_tid != daemon_tid
    subject = wcs.report_thread_subject("dev_agent")
    matching = [t for t in db.list_threads(limit=100) if t.subject == subject]
    assert len(matching) == 2
    assert db.get_thread(new_tid).status.value == "open"


# ── (s) TASK-6043 finding 5: all agents handled beyond the 64 cap ────────

@pytest.mark.asyncio
async def test_tick_processes_all_agents_beyond_cap(
    tmp_path, test_settings, monkeypatch,
):
    """An org with more than _MAX_WORKSPACES agents: the tick pages through
    every registered workspace — no agent is alphabetically starved."""
    db = Database(tmp_path / "db.sqlite")
    org_root = tmp_path / "orgs" / "test"
    org_root.mkdir(parents=True, exist_ok=True)
    org = _make_org(org_root, db, test_settings)
    org.root = org_root

    agents = [f"agent{i:03d}" for i in range(wcs._MAX_WORKSPACES + 6)]
    teams = TeamsRegistry.load(org_root)
    teams._teams["engineering"] = type(
        "TM", (), {"name": "engineering_manager", "team": "engineering",
                   "workers": tuple(agents)}
    )()
    org.teams = teams
    for agent in agents:
        (org_root / "workspaces" / agent).mkdir(parents=True)

    state = _FakeDaemonState()
    state.orgs = {"test": org}
    state.metrics_registry = _FakeMetricsRegistry()

    triggered: list[str] = []

    async def fake_trigger(org, *, agent, enqueue, now_utc=None):
        triggered.append(agent)
        return "TASK-1"

    monkeypatch.setattr(wcs, "trigger_cleanup", fake_trigger)
    monkeypatch.setattr(
        wcs, "decide_cleanup_trigger",
        lambda **kw: wcs.CleanupTriggerDecision(True, None),
    )
    await wcs._tick_org(org, state, now_utc=_sunday_0330_utc())
    assert len(triggered) == len(agents)
    assert set(triggered) == set(agents)


def test_iter_workspaces_pages_across_batches(tmp_path):
    """The workspace iterator pages deterministically: batch N+1 starts where
    batch N ended, and only the last batch reports no truncation."""
    ws = tmp_path / "ws"
    total = wcs._MAX_WORKSPACES * 2 + 3
    for i in range(total):
        (ws / f"agent{i:04d}").mkdir(parents=True)
    paths = type("P", (), {"workspaces_dir": ws})()
    seen: list[str] = []
    offset = 0
    while True:
        batch, truncated = wcs._iter_workspaces(paths, offset=offset)
        if not batch:
            break
        seen.extend(p.name for p in batch)
        if not truncated:
            break
        offset += wcs._MAX_WORKSPACES
    assert len(seen) == total
    assert seen == sorted(f"agent{i:04d}" for i in range(total))
