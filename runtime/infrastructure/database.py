from __future__ import annotations

import functools
import hashlib
import json
import logging
import sqlite3
import threading
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime.models import (
    AuthorityAuditEvent,
    AuthorityAuditEventType,
    AuthorityAuditPayload,
    AuthorityCandidate,
    AuthorityCandidatePolicyPin,
    AuthorityEvaluation,
    AuthorityPolicyActivation,
    AuthorityPolicyRelease,
    AuthorityFenceResult,
    AuthorityRedactionClass,
    AuthorityRetentionClass,
    BlockKind,
    DreamKbCandidate,
    DreamRecord,
    DreamStatus,
    ScheduleStatus,
    TaskAttachmentRecord,
    TaskRecord,
    TaskStatus,
    ThreadAttachment,
    ThreadInvocation,
    ThreadInvocationPurpose,
    ThreadInvocationStatus,
    ThreadMessage,
    ThreadMessageKind,
    ThreadParticipant,
    ThreadRecord,
    ThreadReplyArrival,
    ThreadReplyClaim,
    ThreadReplyDeliveryState,
    ThreadReplyBreakerEpisode,
    ThreadReplyExchangeProjection,
    ThreadReplyRecoveryEntry,
    ThreadReplySettlement,
    ReplyDeliveryProjection,
    ThreadScopedAttachment,
    ThreadStatus,
    TokenUsage,
    WorkHourStatus,
    validate_authority_digest,
    validate_authority_version,
)
from runtime.reply_delivery import reply_failure_category
from runtime.infrastructure.work_hours_store import WorkHoursStore
from runtime.infrastructure.schedule_store import ScheduleStore
from runtime.daemon.thread_mentions import (
    parse_mentions,
    resolve_wake_set,
    valid_mentions,
)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ── TASK-5966 strict mention-led exchange bounds (founder-ratified) ──────
# EXCHANGE_GRACE: idle-closure bound — an exchange closes when the cohort has
# no live covering wake AND no conversational activity for this long
# (founder verdict: FIVE minutes, not the design artifact's 15-minute
# default).
EXCHANGE_GRACE_SECONDS = 5 * 60
# MAX_PRIORITY_WAIT: absolute fail-open bound from exchange open — the reaper
# closes any exchange older than this (retained approved G1 = 4 hours).
MAX_PRIORITY_WAIT_SECONDS = 4 * 60 * 60


class LineageTooDeep(Exception):
    """Ancestor walk exceeded the safety bound; indicates data corruption."""


class AuthorityAuditMigrationRefusal(Exception):
    """Legacy ``authority_audit`` rows reference candidates that do not exist,
    so the candidate-FK retrofit was refused atomically. The legacy schema and
    data are left intact for inspection."""


# Corrected DB-level lifecycle-guard trigger body. Requires the referenced
# ``authority_evaluations`` row's disposition to exactly mirror the candidate's
# frozen disposition (created -> evaluated: NEW.disposition; evaluated ->
# consumed: OLD.disposition), so an append-only escalate evaluation can never
# durably freeze a continue_same_root candidate. Used by BOTH the fresh
# ``_create_authority_tables`` script and the legacy retrofit, so the two
# surfaces can never drift.
_AUTHORITY_LIFECYCLE_GUARD_TRIGGER_SQL = """
            CREATE TRIGGER IF NOT EXISTS authority_candidates_lifecycle_guard
                BEFORE UPDATE OF lifecycle_state, disposition, consumed_at
                ON authority_candidates
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, 'invalid authority candidate lifecycle transition')
                    WHERE NOT (
                        -- created -> evaluated: requires a committed evaluation row
                        -- whose disposition exactly equals the freshly set
                        -- disposition (NULL -> value), so an append-only escalate
                        -- evaluation can never freeze a continue_same_root candidate;
                        -- consumed_at is still NULL.
                        (OLD.lifecycle_state = 'created' AND OLD.disposition IS NULL
                         AND NEW.lifecycle_state = 'evaluated'
                         AND NEW.disposition IS NOT NULL
                         AND NEW.consumed_at IS NULL
                         AND EXISTS (SELECT 1 FROM authority_evaluations e
                                     WHERE e.candidate_id = NEW.id
                                       AND e.disposition = NEW.disposition))
                        OR
                        -- evaluated -> consumed: exactly-once; disposition frozen;
                        -- a committed evaluation row with the SAME frozen
                        -- disposition must exist; consumed_at is stamped exactly
                        -- once (NULL -> value).
                        (OLD.lifecycle_state = 'evaluated'
                         AND NEW.lifecycle_state = 'consumed'
                         AND NEW.disposition IS OLD.disposition
                         AND OLD.consumed_at IS NULL
                         AND NEW.consumed_at IS NOT NULL
                         AND EXISTS (SELECT 1 FROM authority_evaluations e
                                     WHERE e.candidate_id = NEW.id
                                       AND e.disposition = OLD.disposition))
                        OR
                        -- no-op on the guarded columns (e.g. updated_at-only writes).
                        (NEW.lifecycle_state = OLD.lifecycle_state
                         AND NEW.disposition IS OLD.disposition
                         AND NEW.consumed_at IS OLD.consumed_at)
                    );
                END;
"""


_AUTHORITY_POLICY_ACTIVATIONS_VALIDATE_INSERT_SQL = """
CREATE TRIGGER authority_policy_activations_validate_insert
BEFORE INSERT ON authority_policy_activations
BEGIN
    SELECT RAISE(ABORT, 'authority activation release/team mismatch')
    WHERE NOT EXISTS (SELECT 1 FROM authority_policy_releases r
                      WHERE r.id=NEW.release_id AND r.team=NEW.team);
    SELECT RAISE(ABORT, 'authority activation predecessor mismatch')
    WHERE (
        (SELECT COUNT(*) FROM authority_policy_activations a
         WHERE a.team=NEW.team)=0
        AND (NEW.epoch!=1 OR NEW.previous_activation_id IS NOT NULL
             OR NEW.expected_previous_epoch NOT IN (0))
    ) OR (
        (SELECT COUNT(*) FROM authority_policy_activations a
         WHERE a.team=NEW.team)>0
        AND NOT EXISTS (
            SELECT 1 FROM authority_policy_activations a
            WHERE a.id=NEW.previous_activation_id AND a.team=NEW.team
              AND a.epoch=NEW.expected_previous_epoch
              AND NEW.epoch=a.epoch+1
              AND a.epoch=(SELECT MAX(last.epoch)
                           FROM authority_policy_activations last
                           WHERE last.team=NEW.team))
    );
    SELECT RAISE(ABORT, 'authority activation action/history mismatch')
    WHERE (
        (SELECT COUNT(*) FROM authority_policy_activations a
         WHERE a.team=NEW.team)=0
        AND NEW.action!='bootstrap'
    ) OR (
        (SELECT COUNT(*) FROM authority_policy_activations a
         WHERE a.team=NEW.team)>0
        AND (
            NEW.action='bootstrap'
            OR (NEW.action='activate' AND EXISTS (
                SELECT 1 FROM authority_policy_activations a
                WHERE a.team=NEW.team AND a.release_id=NEW.release_id
            ))
            OR (NEW.action='reactivate_rollback' AND (
                NOT EXISTS (
                    SELECT 1 FROM authority_policy_activations a
                    WHERE a.team=NEW.team AND a.release_id=NEW.release_id
                )
                OR NEW.release_id=(
                    SELECT a.release_id FROM authority_policy_activations a
                    WHERE a.team=NEW.team ORDER BY a.epoch DESC LIMIT 1
                )
                OR NOT EXISTS (
                    SELECT 1
                    FROM authority_policy_releases target
                    JOIN authority_policy_activations current_activation
                      ON current_activation.team=NEW.team
                     AND current_activation.epoch=(
                         SELECT MAX(last.epoch)
                         FROM authority_policy_activations last
                         WHERE last.team=NEW.team
                     )
                    JOIN authority_policy_releases current
                      ON current.id=current_activation.release_id
                    WHERE target.id=NEW.release_id
                      AND target.policy_id=current.policy_id
                      AND target.version<current.version
                )
            ))
        )
    );
END;
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Terminal statuses that must never be continued by the authority hook
# (mirrors ``authority._TERMINAL_STATUSES``; kept here so the DB-level
# consumption recheck needs no orchestrator import).
_AUTHORITY_TERMINAL_STATUSES = frozenset({
    "completed", "failed", "superseded", "cancelled",
})

# Child task-result verdicts that do NOT block a same-root continuation.
_AUTHORITY_APPROVED_VERDICTS = frozenset({"APPROVE", "PASS"})


def _authority_claim_key(
    root_task_id: str,
    manager_session_id: str,
    causal_event_id: str,
    policy_digest: str,
    prompt_digest: str,
    model_digest: str,
) -> str:
    """Deterministic CAS key for the authority candidate claim tuple.

    One durable candidate wins the
    root/session/causal-event/policy-prompt-model tuple. The key is a sha256
    digest of the tuple joined with unit-separator bytes so distinct inputs
    cannot collide across field boundaries. It is the ``claim_key`` UNIQUE
    column on ``authority_candidates`` — the database-level exactly-one
    arbiter (not merely the in-process lock).
    """
    material = "\x1f".join(
        (
            root_task_id,
            manager_session_id,
            causal_event_id,
            policy_digest,
            prompt_digest,
            model_digest,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _parse_authority_fence_results(raw: str | None) -> dict[str, AuthorityFenceResult] | None:
    """Parse a persisted fence-results JSON column back into typed results."""
    if raw is None:
        return None
    data = json.loads(raw)
    return {name: AuthorityFenceResult.model_validate(value) for name, value in data.items()}


def _validate_authority_class(value: str, field: str, enum_cls) -> str:
    """Validate a controlled retention/redaction classification value.

    Raises ValueError for any value outside the closed vocabulary — the caller
    must fail loudly BEFORE any durable write, so a bad classification can
    never be swallowed by conflict handling into a phantom CAS loser.
    """
    allowed = {member.value for member in enum_cls}
    if value not in allowed:
        raise ValueError(
            f"{field} must be one of: {', '.join(sorted(allowed))}; got {value!r}"
        )
    return value


def _serialize_authority_fence_results(
    fence_results: dict | None,
) -> str | None:
    """Strictly validate and serialize a fence-results mapping.

    Each value must be a closed ``AuthorityFenceResult`` (extra keys and
    unknown codes are rejected); fence names must be non-empty strings. Returns
    the JSON column value, or None for an absent mapping. Raises ValueError
    (via Pydantic) rather than silently storing or redacting anything.
    """
    if fence_results is None:
        return None
    if not isinstance(fence_results, dict):
        raise ValueError("fence_results must be a dict mapping fence name -> AuthorityFenceResult")
    normalized: dict[str, AuthorityFenceResult] = {}
    for name, value in fence_results.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("fence result names must be non-empty strings")
        normalized[name] = AuthorityFenceResult.model_validate(value)
    return json.dumps(
        {name: result.model_dump(mode="json") for name, result in normalized.items()}
    )


def _serialize_authority_audit_payload(payload: object | None) -> str | None:
    """Strictly validate and serialize an authority audit payload.

    The payload must be a closed ``AuthorityAuditPayload`` — unknown keys,
    nested arbitrary JSON, prose, credentials, and raw model exchanges are
    rejected. Returns the JSON column value, or None for an absent payload.
    """
    if payload is None:
        return None
    model = AuthorityAuditPayload.model_validate(payload)
    return json.dumps(model.model_dump(mode="json", exclude_none=True))


def _synchronized(method):
    """Serialize every public ``Database`` call through ``self._lock``.

    Why: the daemon shares ONE sqlite3 connection across the event-loop thread
    (async routes) and the threadpool thread running ``Orchestrator.run_step``
    (see ``src/daemon/queue.py``). ``DaemonState.db_lock`` is an ``asyncio.Lock``
    and can't serialize against threads; ``check_same_thread=False`` on the
    connection allows cross-thread access but not concurrent cursor/exec ops —
    overlap raises ``sqlite3.InterfaceError`` or hands back rows with None-valued
    columns. A ``threading.RLock`` inside ``Database`` closes that gap without
    per-thread connections or a migration.

    Lock instrumentation (THR-129): times wait duration (acquire) and hold
    duration (method body). Warns when either exceeds the instance's
    ``_lock_warn_threshold_seconds`` (default 1.0 s). RLock reentrancy is
    respected — nested acquires show near-zero wait time.
    """
    _db_logger = logging.getLogger("happyranch.database.lock")

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        threshold = getattr(self, '_lock_warn_threshold_seconds', 1.0)
        t_wait_start = _time.monotonic()
        self._lock.acquire(blocking=True)
        wait_sec = _time.monotonic() - t_wait_start
        if wait_sec > threshold:
            _db_logger.warning(
                "Database._lock wait %.3fs > threshold %.3fs "
                "for %s.%s (lock convoy may stall other routes)",
                wait_sec, threshold,
                type(self).__name__, method.__name__,
            )
        try:
            t_hold_start = _time.monotonic()
            result = method(self, *args, **kwargs)
            hold_sec = _time.monotonic() - t_hold_start
            if hold_sec > threshold:
                _db_logger.warning(
                    "Database._lock hold %.3fs > threshold %.3fs "
                    "for %s.%s",
                    hold_sec, threshold,
                    type(self).__name__, method.__name__,
                )
            return result
        finally:
            self._lock.release()
    return wrapper


def _rebuild_indexes_for(
    table: str,
    conn: sqlite3.Connection,
    statements: list[tuple[str, list]],
    dropped_col: str | None = None,
) -> None:
    """Append CREATE INDEX statements for *table* after a table-rebuild.

    Called during the old-SQLite fallback path of the talk-removal migration.
    The rebuild drops all indexes on the original table; this helper re-creates
    them by reading sqlite_master. When *dropped_col* is set, skip any index
    whose SQL references it (the index is already dropped in step 1 of the
    migration).
    """
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    for (sql,) in rows:
        # Keep CREATE UNIQUE INDEX / CREATE INDEX as-is.
        if not sql.upper().startswith("CREATE "):
            continue
        if dropped_col and dropped_col in sql:
            continue
        statements.append((sql, []))


# ── Keyset cursor helpers for audit-log pagination ────────────────────────

import base64 as _base64


def _encode_cursor(timestamp: str, row_id: int) -> str:
    """Encode (timestamp, id) into an opaque base64 cursor string."""
    raw = f"{timestamp}|{row_id}"
    return _base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, int]:
    """Decode an opaque cursor string back to (timestamp, id).

    Raises ``ValueError`` on malformed cursors so callers can reject them
    cleanly (422 at the HTTP layer).
    """
    try:
        raw = _base64.urlsafe_b64decode(cursor.encode()).decode()
        ts, id_str = raw.rsplit("|", 1)
        return ts, int(id_str)
    except Exception:
        raise ValueError(f"Invalid cursor: {cursor!r}")


# Shared SELECT for the stale never-started pending observation (THR-195): the
# single source of truth for BOTH the managed-store scan
# (``Database.list_stale_pending_jobs``) and the read-only registry scan
# (``scan_stale_pending_jobs_readonly``), so the observation predicate
# (``status='pending' AND started_at IS NULL AND created_at <= cutoff``) can
# never drift between the two paths.
_STALE_PENDING_JOBS_SCAN_SQL = (
    "SELECT id, task_id, agent_name, title, review_required, created_at "
    "FROM jobs WHERE status='pending' AND started_at IS NULL "
    "AND created_at <= ? ORDER BY created_at, id"
)

# Active-WAL observation is a DIRECT read of the source store (founder
# ruling TASK-5542/TASK-5544): the temporary snapshot/copy machinery is
# retired entirely. An active-WAL source is opened in place with a genuine
# SQLite read-only connection (``mode=ro``) which consults the ``-wal`` so
# WAL-only committed candidates are observed. The FOUNDER CONTRACT protects
# the durable source ``happyranch.db`` and ``happyranch.db-wal`` BYTES ONLY:
# SQLite's WAL reader — even ``mode=ro`` — initializes WAL shared memory and
# may CREATE, MODIFY, or REMOVE the WAL-index ``happyranch.db-shm`` as
# transient reader/lock/index behavior, and that is explicitly permitted
# (TASK-5544 ruling; creation/modification/removal are all allowed, and no
# ``-shm`` existence/hash/mtime identity is ever asserted). The source main
# DB and ``-wal`` are NEVER written: a read-only connection cannot append WAL
# frames, checkpoint, recover, or run DDL/DML, so both stay byte-identical
# before/after every observation. No snapshot, no copy, no temp directory
# anywhere.


def _scan_stale_pending_jobs_direct_wal(
    db_path: Path, cutoff_iso: str,
) -> list[dict]:
    """Read-only WAL-aware observation directly on the SOURCE store.

    Founder-authorized fourth-round correction (TASK-5542): the active-WAL
    source is opened in place with a genuine SQLite read-only connection
    (``file:...?mode=ro``) for the duration of one query and closed. The
    reader consults the ``-wal`` so candidates committed only to the WAL are
    observed; SQLite's own WAL-reader protocol gives every reader a coherent
    committed view without any copy or stat-guard. The source main DB and
    ``-wal`` are never written (a read-only connection cannot append WAL
    frames, checkpoint, or recover), so both files stay byte-identical
    before/after every observation and no row/schema/audit state can change.

    SQLite's WAL reader may CREATE, MODIFY, or REMOVE the source
    ``-shm`` (WAL-index shared memory) as transient reader/lock/index
    behavior — explicitly permitted by the founder contract (TASK-5544); no
    ``-shm`` existence/hash/mtime identity is asserted. Only the durable
    source ``happyranch.db`` and ``happyranch.db-wal`` bytes are protected
    (byte-identical before/after).

    Fail closed: a missing main DB is handled by the caller (``[]``); a
    malformed main file raises ``sqlite3.DatabaseError`` and a schema without
    a ``jobs`` table raises ``sqlite3.OperationalError`` — observation never
    fabricates candidates and never mutates the source.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            _STALE_PENDING_JOBS_SCAN_SQL, (cutoff_iso,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def scan_stale_pending_jobs_readonly(
    db_path: Path, cutoff_iso: str,
) -> list[dict]:
    """Read-only stale-pending observation over one org store.

    THR-195 observation MUST NOT durably mutate any store: it never creates a
    missing DB, never enables WAL, never runs the schema migration guards, and
    never writes the source ``-wal``. The founder contract (TASK-5544)
    protects the durable source ``happyranch.db`` and ``happyranch.db-wal``
    BYTES ONLY — the SQLite WAL-index ``happyranch.db-shm`` may be created,
    modified, or removed by read-side WAL access and that is explicitly
    permitted, so no ``-shm`` identity is ever asserted. This helper
    therefore never opens the source with ``Database(db_path)`` (whose
    ``__init__`` creates the file, enables WAL, and runs migrations).

    Route selection: a cleanly-closed store has no ``-wal``/``-shm`` (SQLite
    checkpoints and removes them on the last close), so the main file holds
    every committed row — ``immutable=1`` reads it fully and provably cannot
    create sidecars. When sidecars exist (store open in this process — e.g. a
    loaded org — or crash leftovers), the scan opens the SOURCE directly with
    a genuine read-only WAL-aware connection (``mode=ro``): WAL-only
    committed candidates are observed, the source main DB and ``-wal`` stay
    byte-identical before/after, and the source ``-shm`` is the explicitly
    permitted shared-memory surface (creation/modification/removal by the
    WAL reader allowed; founder ruling TASK-5544; no snapshot/copy/temp
    directory is used). Either way the scan sees every committed candidate
    row and durably writes only the permitted ``-shm`` shared-memory surface
    (which it may create or remove) — never the source ``.db``/``-wal``.

    A missing DB file returns ``[]`` — nothing to observe, nothing created.
    A store that cannot be read (malformed file, or a pre-migration/
    irrelevant schema without a ``jobs`` table) raises
    ``sqlite3.DatabaseError``/``OperationalError``: fail closed at this leaf —
    never mutate, never fabricate candidates; the all-org coordinator
    (``scan_all_org_stale_pending``) isolates and logs such a failure so it
    cannot abort daemon startup or suppress other org roots.
    """
    if not db_path.is_file():
        return []
    wal = Path(f"{db_path}-wal")
    shm = Path(f"{db_path}-shm")
    if wal.exists() or shm.exists():
        return _scan_stale_pending_jobs_direct_wal(db_path, cutoff_iso)
    conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_STALE_PENDING_JOBS_SCAN_SQL, (cutoff_iso,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # See `_synchronized` for the threading model. RLock (not Lock) because
        # e.g. `walk_ancestors` → `get_task` and `get_recall_payload` → `get_task`
        # both re-enter public methods while already holding the lock.
        self._lock = threading.RLock()
        # THR-129 lock instrumentation: configurable warning threshold for
        # lock wait/hold times (seconds). Test seam — tests set this to a low
        # value to verify instrumentation fires.
        self._lock_warn_threshold_seconds = 1.0
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        from runtime.infrastructure.remote_job_schema import (
            migrate_identity_enrollment_schema,
            validate_identity_enrollment_schema_preflight,
        )

        # TASK-6611: this fail-closed guard and six-stage convergence are the
        # first database-open schema/data operation.  In particular they run
        # before WAL selection and every legacy/jobs/S2/open-path mutator.
        validate_identity_enrollment_schema_preflight(self._conn)
        migrate_identity_enrollment_schema(
            self._conn,
            stage_hook=getattr(self, "_remote_identity_schema_stage_hook", None),
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate_jobs_table_if_needed()
        self._migrate_drop_talk_surface_if_needed()
        self._retire_skill_lifecycle_if_present()
        self._create_tables()
        self._migrate_remote_job_schema()
        self._migrate_dark_authority_activation_seal_if_needed()
        self._create_authority_tables()
        self._retrofit_authority_policy_activation_trigger_if_needed()
        self._retrofit_authority_audit_fk_if_needed()
        self._retrofit_authority_lifecycle_trigger_if_needed()
        self._ensure_task_attachments_storage_key_unique()
        # Working-hours CRUD lives in its own module but shares THIS connection
        # and lock so the single-connection serialization invariant (see
        # `_synchronized`) is preserved across both surfaces.
        self.work_hours = WorkHoursStore(self._conn, self._lock)
        self.schedules = ScheduleStore(self._conn, self._lock)

    def _migrate_remote_job_schema(self) -> None:
        """Install the exact additive generic remote-job S2 schema."""
        from runtime.infrastructure.remote_job_schema import migrate_remote_job_schema

        migrate_remote_job_schema(
            self._conn,
            stage_hook=getattr(self, "_remote_schema_stage_hook", None),
        )

    @_synchronized
    def execute(self, sql: str, parameters=()):
        """Passthrough to the underlying sqlite3 connection's execute().

        Enables lifecycle stores that accept either raw connections (tests)
        or Database wrappers (production) to call ``db.execute()`` uniformly.

        Lock acquisition is centralized through ``_synchronized`` (same as all
        other public methods) so lock wait/hold instrumentation covers every
        shared-connection path uniformly. RLock reentrancy is preserved —
        ``execute`` called from within another ``_synchronized`` method
        re-acquires with near-zero wait.
        """
        return self._conn.execute(sql, parameters)

    @property
    def path(self) -> Path:
        """Alias for ``db_path``. Convenience for callers that prefer ``.path``."""
        return self.db_path

    def _retire_skill_lifecycle_if_present(self) -> None:
        """Permanently remove legacy lifecycle tables and their content blobs."""
        tables = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'skill_lifecycle_%'"
            )
        }
        if not tables:
            return
        artifact_keys: list[str] = []
        if "skill_lifecycle_packages" in tables:
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(skill_lifecycle_packages)")}
            if "content_artifact_key" in columns:
                artifact_keys = [
                    row[0]
                    for row in self._conn.execute(
                        "SELECT content_artifact_key FROM skill_lifecycle_packages WHERE content_artifact_key IS NOT NULL"
                    )
                ]
        try:
            self._conn.execute("BEGIN")
            ordered = (
                "skill_lifecycle_materializations",
                "skill_lifecycle_assignments",
                "skill_lifecycle_events",
                "skill_lifecycle_packages",
            )
            for table in (*[name for name in ordered if name in tables], *sorted(tables - set(ordered))):
                self._conn.execute(f"DROP TABLE IF EXISTS {table}")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        from runtime.infrastructure.artifact_store import ArtifactStore, ArtifactNotFound

        store = ArtifactStore(self.db_path.parent / "artifacts")
        for artifact_key in artifact_keys:
            try:
                store.delete(artifact_key)
            except ArtifactNotFound:
                pass

    def _migrate_jobs_table_if_needed(self) -> None:
        """Rename legacy ``script_requests`` table to ``jobs`` and ripple the
        rename through audit_log + escalation_notifications.

        Idempotent: if ``jobs`` already exists OR ``script_requests`` does not
        exist, this is a no-op. Must run BEFORE ``_create_tables`` so the
        ``CREATE TABLE IF NOT EXISTS jobs`` below becomes a no-op on an
        already-migrated DB.

        See spec docs/superpowers/specs/2026-05-26-jobs-design.md §6.2.
        """
        # `executescript` does not return rows, so use a plain execute+fetchall
        # to inspect the schema first.
        existing = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name IN ('script_requests', 'jobs')"
            ).fetchall()
        }
        if "script_requests" not in existing or "jobs" in existing:
            return

        # Drive the migration as one explicit transaction. `executescript`
        # would issue an implicit COMMIT at start AND swallow rollback on
        # mid-script failure — leaving the DB half-migrated and the
        # idempotency check above tripping on the next startup (jobs table
        # exists but audit/notifications still reference SR-NNN). Each
        # statement goes through `execute` so any failure raises with the
        # full transaction rolled back.
        # SQLite 3.35+ supports DROP COLUMN; we rely on that for
        # `timeout_seconds`.
        migration_statements = [
            "ALTER TABLE script_requests RENAME TO jobs",

            "ALTER TABLE jobs ADD COLUMN review_required INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE jobs ADD COLUMN persistent INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE jobs ADD COLUMN max_output_bytes INTEGER NOT NULL DEFAULT 52428800",
            "ALTER TABLE jobs ADD COLUMN stdout_bytes INTEGER",
            "ALTER TABLE jobs ADD COLUMN stderr_bytes INTEGER",
            "ALTER TABLE jobs ADD COLUMN reason TEXT",
            "ALTER TABLE jobs ADD COLUMN max_runtime_seconds INTEGER",

            "UPDATE jobs SET max_runtime_seconds = timeout_seconds"
            " WHERE timeout_seconds IS NOT NULL",
            "ALTER TABLE jobs DROP COLUMN timeout_seconds",

            # Backfill: every legacy script_request was a founder-approved one-shot.
            "UPDATE jobs SET review_required = 1 WHERE review_required = 0",

            # Force-fail orphaned 'running' rows (daemon has clearly exited by now).
            "UPDATE jobs"
            "   SET status = 'failed',"
            "       reason = 'daemon_crash',"
            "       finished_at = COALESCE(finished_at, started_at, created_at)"
            " WHERE status = 'running'",

            # ID rewrite SR-NNN -> JOB-NNN.
            "UPDATE jobs SET id = 'JOB-' || SUBSTR(id, 4) WHERE id LIKE 'SR-%'",

            # File-path rewrite scripts/SR- -> jobs/JOB-.
            "UPDATE jobs"
            "   SET stdout_path = REPLACE(REPLACE(stdout_path, '/scripts/SR-', '/jobs/JOB-'),"
            "                             '/scripts/', '/jobs/')"
            " WHERE stdout_path IS NOT NULL",
            "UPDATE jobs"
            "   SET stderr_path = REPLACE(REPLACE(stderr_path, '/scripts/SR-', '/jobs/JOB-'),"
            "                             '/scripts/', '/jobs/')"
            " WHERE stderr_path IS NOT NULL",

            # Ripple through cross-referencing tables.
            "UPDATE escalation_notifications"
            "   SET task_id = 'JOB-' || SUBSTR(task_id, 4)"
            " WHERE kind = 'script_request' AND task_id LIKE 'SR-%'",
            "UPDATE escalation_notifications"
            "   SET kind = 'job_request'"
            " WHERE kind = 'script_request'",

            # Audit rewrites. NB: real columns are `action` and `payload`
            # (NOT `kind`/`payload_json` — spec §6.2 corrected).
            # task_id values in audit_log never contain SR-NNN — only audit
            # payloads do (via script_id references), so the broad REPLACE
            # on payload below is safe.
            "UPDATE audit_log"
            "   SET action = 'job_' || SUBSTR(action, 8)"
            " WHERE action LIKE 'script_%'",
            "UPDATE audit_log"
            "   SET payload = REPLACE(payload, '\"script_id\"', '\"job_id\"')"
            " WHERE payload LIKE '%\"script_id\"%'",
            "UPDATE audit_log"
            "   SET payload = REPLACE(payload, '\"SR-', '\"JOB-')"
            " WHERE payload LIKE '%\"SR-%'",

            # Rename indexes.
            "DROP INDEX IF EXISTS idx_script_requests_task",
            "DROP INDEX IF EXISTS idx_script_requests_agent",
            "DROP INDEX IF EXISTS idx_script_requests_status",
            "DROP INDEX IF EXISTS idx_script_requests_created_at",
            "CREATE INDEX IF NOT EXISTS jobs_task_id_idx ON jobs(task_id)",
            "CREATE INDEX IF NOT EXISTS jobs_status_idx  ON jobs(status)",
        ]
        try:
            self._conn.execute("BEGIN")
            for stmt in migration_statements:
                self._conn.execute(stmt)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise


    def _migrate_drop_talk_surface_if_needed(self) -> None:
        """Drop the talks table, four talk-reference columns, and five talk indexes.

        Idempotent: inspects PRAGMA table_info and sqlite_master; if the talk
        columns/table are already absent, returns immediately (no-op).

        Wraps every statement in one explicit BEGIN/COMMIT with rollback on
        exception. Uses the version-guarded DROP COLUMN / table-rebuild hybrid
        from the spec (runtime already hard-requires SQLite >= 3.35, so the
        fallback branch is belt-and-suspenders).

        Must run BEFORE ``_create_tables`` so ``CREATE TABLE IF NOT EXISTS``
        becomes a no-op on the already-dropped table/columns.

        Migration ordering (single transaction):
        1. Drop the 5 talk-related indexes.
        2. Drop the 3 columns (tasks/jobs/threads) + table-rebuild fallback.
        3. Reconcile session_token_usage.talk_id (DROP COLUMN or rebuild).
        4. DROP TABLE IF EXISTS talks.
        5. Leave audit_log untouched (talk_* rows preserved per decision #6).
        """
        # Idempotency guard: verify ALL FOUR targets are already gone
        # (talks table + the 3 talk_id columns on tasks/jobs/threads).
        # session_token_usage.talk_id is checked per-column below.
        existing_tables = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "talks" not in existing_tables:
            all_gone = True
            for table, col in (
                ("tasks", "dispatched_from_talk_id"),
                ("jobs", "submitted_from_talk_id"),
                ("threads", "composed_from_talk_id"),
                ("session_token_usage", "talk_id"),
            ):
                tbl_cols = {
                    row["name"]
                    for row in self._conn.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                if col in tbl_cols:
                    all_gone = False
                    break
            if all_gone:
                return

        sqlite_version = sqlite3.sqlite_version_info
        can_drop_column = sqlite_version >= (3, 35, 0)

        statements: list[tuple[str, list]] = []

        # 1. Drop talk-related indexes.
        for idx in (
            "idx_talks_agent_status",
            "idx_talks_started",
            "idx_tasks_dispatched_from_talk_id",
            "idx_threads_composed_from_talk",
            "idx_session_token_usage_talk",
        ):
            statements.append((f"DROP INDEX IF EXISTS {idx}", []))

        # 2. Drop the three talk-reference columns.
        for table, col in (
            ("tasks", "dispatched_from_talk_id"),
            ("jobs", "submitted_from_talk_id"),
            ("threads", "composed_from_talk_id"),
        ):
            cols = {
                row["name"]
                for row in self._conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            if col not in cols:
                continue
            if can_drop_column:
                statements.append((f"ALTER TABLE {table} DROP COLUMN {col}", []))
            else:
                # Table-rebuild fallback: explicit CREATE TABLE (
                # full DDL minus the talk column), INSERT SELECT explicit
                # cols, DROP old, RENAME new, recreate indexes.
                info_rows = self._conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
                keep_cols = [r["name"] for r in info_rows if r["name"] != col]
                col_list = ", ".join(keep_cols)
                # Build the new-column DDL from PRAGMA info.
                col_defs = []
                for r in info_rows:
                    if r["name"] == col:
                        continue
                    cname = r["name"]
                    ctype = r["type"]
                    notnull = r["notnull"]
                    dflt = r["dflt_value"]
                    pk = r["pk"]
                    parts = [cname, ctype]
                    if notnull:
                        parts.append("NOT NULL")
                    if dflt is not None:
                        parts.append(f"DEFAULT {dflt}")
                    col_def = " ".join(parts)
                    # PRIMARY KEY handled separately in the table DDL.
                    col_defs.append(col_def)
                # Collect PK columns.
                pk_cols = [r["name"] for r in info_rows if r["pk"] and r["name"] != col]
                pk_clause = ""
                if pk_cols:
                    pk_clause = f", PRIMARY KEY ({', '.join(pk_cols)})"
                stmt_create = (
                    f"CREATE TABLE {table}_new (\n  "
                    + ",\n  ".join(col_defs)
                    + f"{pk_clause}\n)"
                )
                stmt_insert = (
                    f"INSERT INTO {table}_new ({col_list}) "
                    f"SELECT {col_list} FROM {table}"
                )
                statements.append((stmt_create, []))
                statements.append((stmt_insert, []))
                statements.append((f"DROP TABLE {table}", []))
                statements.append((f"ALTER TABLE {table}_new RENAME TO {table}", []))
                # Recreate indexes lost by the rebuild, skipping talk-column indexes.
                _rebuild_indexes_for(table, self._conn, statements, dropped_col=col)

        # 3. session_token_usage.talk_id.
        stu_cols = {
            row["name"]
            for row in self._conn.execute(
                "PRAGMA table_info(session_token_usage)"
            ).fetchall()
        }
        if "talk_id" in stu_cols:
            if can_drop_column:
                statements.append(
                    ("ALTER TABLE session_token_usage DROP COLUMN talk_id", [])
                )
            else:
                info_rows = self._conn.execute(
                    "PRAGMA table_info(session_token_usage)"
                ).fetchall()
                keep_cols = [r["name"] for r in info_rows if r["name"] != "talk_id"]
                col_list = ", ".join(keep_cols)
                # Build the new-column DDL from PRAGMA info.
                col_defs = []
                for r in info_rows:
                    if r["name"] == "talk_id":
                        continue
                    cname = r["name"]
                    ctype = r["type"]
                    notnull = r["notnull"]
                    dflt = r["dflt_value"]
                    pk = r["pk"]
                    parts = [cname, ctype]
                    if notnull:
                        parts.append("NOT NULL")
                    if dflt is not None:
                        parts.append(f"DEFAULT {dflt}")
                    col_def = " ".join(parts)
                    col_defs.append(col_def)
                # Collect PK columns.
                pk_cols = [r["name"] for r in info_rows if r["pk"] and r["name"] != "talk_id"]
                pk_clause = ""
                if pk_cols:
                    pk_clause = f", PRIMARY KEY ({', '.join(pk_cols)})"
                stmt_create = (
                    f"CREATE TABLE session_token_usage_new (\n  "
                    + ",\n  ".join(col_defs)
                    + f"{pk_clause}\n)"
                )
                stmt_insert = (
                    f"INSERT INTO session_token_usage_new ({col_list}) "
                    f"SELECT {col_list} FROM session_token_usage"
                )
                statements.append((stmt_create, []))
                statements.append((stmt_insert, []))
                statements.append(("DROP TABLE session_token_usage", []))
                statements.append((
                    "ALTER TABLE session_token_usage_new RENAME TO session_token_usage", []
                ))
                # Recreate indexes lost by the rebuild, skipping talk-column indexes.
                _rebuild_indexes_for(
                    "session_token_usage", self._conn, statements, dropped_col="talk_id"
                )

        # 4. Drop the talks table.
        if "talks" in existing_tables:
            statements.append(("DROP TABLE IF EXISTS talks", []))

        # Execute as one transaction.
        if not statements:
            return
        try:
            self._conn.execute("BEGIN")
            for stmt, params in statements:
                self._conn.execute(stmt, params)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _ensure_task_attachments_storage_key_unique(self) -> None:
        """Idempotent, transactional preflight: enforce storage_key uniqueness.

        Handles legacy v1 task_attachments tables where the pre-constraint
        schema allowed duplicate storage_key rows. Legacy duplicates are
        preserved for readability — marked with legacy_status='duplicate_v1'
        — and their keys cannot be newly claimed (guarded by a pre-insert
        existence check in insert_task_attachment and
        insert_task_with_attachments).

        All preflight steps — additive legacy_status column creation,
        duplicate detection/marking, and named index creation — run inside
        a single SQLite BEGIN IMMEDIATE / COMMIT transaction. On any error
        the entire preflight rolls back, leaving the database schema, data,
        and indexes exactly as they were before this invocation.

        For clean databases a full UNIQUE index is created. For databases
        with legacy duplicates a partial UNIQUE index (WHERE
        legacy_status IS NULL) enforces uniqueness on new non-legacy claims.
        """
        existing_tables = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "task_attachments" not in existing_tables:
            return

        # Idempotence: if our named index already exists, migration is done.
        existing_idx = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='task_attachments'"
            ).fetchall()
        }
        if "idx_task_attachments_storage_key_unique" in existing_idx:
            return

        # Check whether legacy_status column already exists.
        cols_before = {
            row[1]
            for row in self._conn.execute(
                "PRAGMA table_info('task_attachments')"
            ).fetchall()
        }
        has_legacy_col = "legacy_status" in cols_before

        # Single transaction: ALTER TABLE (if needed), duplicate detection,
        # marking, and index creation all inside one BEGIN / COMMIT scope.
        # On any error the entire preflight rolls back, leaving the database
        # schema, data, and indexes exactly as they were before this call.
        try:
            self._conn.execute("BEGIN IMMEDIATE")

            # Additive: ensure legacy_status column exists.
            if not has_legacy_col:
                self._conn.execute(
                    "ALTER TABLE task_attachments "
                    "ADD COLUMN legacy_status TEXT"
                )

            # Detect duplicate storage_key rows among non-legacy rows.
            # These can only exist on databases created before the UNIQUE
            # constraint was introduced (v1 pre-index schema).
            dupes = self._conn.execute(
                "SELECT storage_key FROM task_attachments "
                "WHERE legacy_status IS NULL "
                "GROUP BY storage_key HAVING COUNT(*) > 1"
            ).fetchall()

            if dupes:
                # Mark ALL rows sharing each duplicate key as legacy.
                for (dup_key,) in dupes:
                    self._conn.execute(
                        "UPDATE task_attachments SET legacy_status = ? "
                        "WHERE storage_key = ?",
                        ("duplicate_v1", dup_key),
                    )
                # Partial unique index: only non-legacy rows must be unique.
                # Legacy duplicates are excluded from the unique guard — their
                # keys are protected from new claims by pre-insert existence
                # checks in the insert methods.
                self._conn.execute(
                    "CREATE UNIQUE INDEX "
                    "idx_task_attachments_storage_key_unique "
                    "ON task_attachments(storage_key) "
                    "WHERE legacy_status IS NULL"
                )
            else:
                # Full unique index for clean databases (v0 or clean v1).
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "idx_task_attachments_storage_key_unique "
                    "ON task_attachments(storage_key)"
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise


    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                assigned_agent TEXT,
                team TEXT NOT NULL DEFAULT 'engineering',
                brief TEXT NOT NULL,
                task_type TEXT NOT NULL DEFAULT 'task',
                revision_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                parent_task_id TEXT,
                final_output_summary TEXT,
                final_output_dir TEXT,
                executor_pid INTEGER
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                action TEXT NOT NULL,
                payload TEXT,
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS manager_supersessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                predecessor_task_id TEXT NOT NULL UNIQUE,
                successor_task_id TEXT NOT NULL UNIQUE,
                original_root_task_id TEXT NOT NULL,
                actor_agent TEXT NOT NULL,
                actor_session_id TEXT NOT NULL,
                rationale TEXT NOT NULL,
                attestation_evidence TEXT NOT NULL,
                predecessor_brief TEXT NOT NULL,
                successor_brief TEXT NOT NULL,
                predecessor_brief_sha256 TEXT NOT NULL,
                successor_brief_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(predecessor_task_id) REFERENCES tasks(id),
                FOREIGN KEY(successor_task_id) REFERENCES tasks(id)
            );
            CREATE INDEX IF NOT EXISTS idx_manager_supersessions_original_root
                ON manager_supersessions(original_root_task_id);
            CREATE TRIGGER IF NOT EXISTS manager_supersessions_no_update
                BEFORE UPDATE ON manager_supersessions
                BEGIN SELECT RAISE(ABORT, 'manager supersessions are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS manager_supersessions_no_delete
                BEFORE DELETE ON manager_supersessions
                BEGIN SELECT RAISE(ABORT, 'manager supersessions are append-only'); END;

            CREATE TABLE IF NOT EXISTS task_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'completed',
                output_summary TEXT,
                decision_json TEXT,
                confidence_score INTEGER,
                learnings TEXT,
                risks_flagged TEXT,
                duration_seconds INTEGER,
                token_count INTEGER,
                estimated_cost REAL,
                output_dir TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dreams (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                local_date TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                window_start TEXT,
                window_end TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                summary TEXT,
                transcript_path TEXT,
                new_learnings_count INTEGER NOT NULL DEFAULT 0,
                kb_candidate_count INTEGER NOT NULL DEFAULT 0,
                founder_thread_id TEXT,
                session_id TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(agent_name, local_date)
            );
            CREATE INDEX IF NOT EXISTS idx_dreams_agent_date
                ON dreams(agent_name, local_date);
            CREATE INDEX IF NOT EXISTS idx_dreams_status
                ON dreams(status);

            CREATE TABLE IF NOT EXISTS dream_kb_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dream_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                slug TEXT NOT NULL,
                title TEXT NOT NULL,
                topic TEXT NOT NULL,
                rationale TEXT NOT NULL,
                body_markdown TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                promoted_kb_slug TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(dream_id, slug),
                FOREIGN KEY (dream_id) REFERENCES dreams(id)
            );
            CREATE INDEX IF NOT EXISTS idx_dream_candidates_dream
                ON dream_kb_candidates(dream_id);
            CREATE INDEX IF NOT EXISTS idx_dream_candidates_status
                ON dream_kb_candidates(status);

            CREATE TABLE IF NOT EXISTS work_hours (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                local_date TEXT NOT NULL,
                slot TEXT NOT NULL,
                mode TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                routine_count INTEGER NOT NULL DEFAULT 0,
                dropped_count INTEGER NOT NULL DEFAULT 0,
                spawned_task_ids TEXT,
                spawned_task_count INTEGER NOT NULL DEFAULT 0,
                summary TEXT,
                transcript_path TEXT,
                session_id TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(agent_name, local_date, slot)
            );
            CREATE INDEX IF NOT EXISTS idx_work_hours_agent_date
                ON work_hours(agent_name, local_date);
            CREATE INDEX IF NOT EXISTS idx_work_hours_status
                ON work_hours(status);

            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                team TEXT NOT NULL DEFAULT 'engineering',
                kind TEXT NOT NULL,
                fire_at TEXT NOT NULL,
                recurrence TEXT,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                normalized_brief TEXT NOT NULL,
                source_instruction TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'armed',
                active INTEGER NOT NULL DEFAULT 1,
                expires_at TEXT,
                indefinite INTEGER NOT NULL DEFAULT 0,
                spawned_task_ids TEXT,
                last_fired_at TEXT,
                fire_count INTEGER NOT NULL DEFAULT 0,
                session_id TEXT,
                error TEXT,
                transcript_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_schedules_agent_status
                ON schedules(agent_name, status);
            CREATE INDEX IF NOT EXISTS idx_schedules_status_fire_at
                ON schedules(status, fire_at);

            CREATE TABLE IF NOT EXISTS session_token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    TEXT,
                agent      TEXT NOT NULL,
                session_id TEXT NOT NULL,
                executor   TEXT NOT NULL,
                model      TEXT,
                input_tokens          INTEGER,
                output_tokens         INTEGER,
                cache_read_tokens     INTEGER,
                cache_creation_tokens INTEGER,
                reasoning_tokens      INTEGER,
                usage_raw_json TEXT,
                scope_type TEXT,
                scope_id TEXT,
                thread_id TEXT,
                invocation_purpose TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (task_id, agent, session_id)
            );

            CREATE TABLE IF NOT EXISTS escalation_notifications (
                feishu_message_id TEXT PRIMARY KEY,
                org_slug          TEXT NOT NULL,
                task_id           TEXT NOT NULL,
                chat_id           TEXT NOT NULL,
                created_at        TEXT NOT NULL,
                expires_at        TEXT NOT NULL,
                consumed_at       TEXT,
                consumed_by       TEXT,
                kind              TEXT NOT NULL DEFAULT 'escalation'
            );
            CREATE INDEX IF NOT EXISTS idx_escalation_notifications_task
                ON escalation_notifications (task_id);

            CREATE TABLE IF NOT EXISTS processed_event_ids (
                org_slug          TEXT NOT NULL,
                feishu_event_id   TEXT NOT NULL,
                processed_at      TEXT NOT NULL,
                outcome           TEXT NOT NULL,
                reason            TEXT,
                PRIMARY KEY (org_slug, feishu_event_id)
            );

            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                started_at TEXT NOT NULL,
                archived_at TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                forwarded_from_id TEXT,
                forwarded_from_kind TEXT,
                turn_cap INTEGER NOT NULL DEFAULT 500,
                turns_used INTEGER NOT NULL DEFAULT 0,
                summary TEXT,
                transcript_path TEXT,
                pinned_at TEXT,
                mention_routing_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status);
            CREATE INDEX IF NOT EXISTS idx_threads_started ON threads(started_at);

            CREATE TABLE IF NOT EXISTS thread_participants (
                thread_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                added_at TEXT NOT NULL,
                added_by TEXT NOT NULL,
                agent_session_id TEXT,
                last_resumed_seq INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (thread_id, agent_name),
                FOREIGN KEY (thread_id) REFERENCES threads(id)
            );
            CREATE INDEX IF NOT EXISTS idx_thread_participants_agent
                ON thread_participants(agent_name);

            CREATE TABLE IF NOT EXISTS thread_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                speaker TEXT NOT NULL,
                kind TEXT NOT NULL,
                body_markdown TEXT,
                addressed_to_json TEXT,
                decline_reason TEXT,
                system_payload_json TEXT,
                sent_from_task_id TEXT,
                mentions_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES threads(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_messages_thread_seq
                ON thread_messages(thread_id, seq);

            CREATE TABLE IF NOT EXISTS thread_message_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                message_seq INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                artifact_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                size_bytes INTEGER,
                content_type TEXT,
                uploaded_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES threads(id),
                UNIQUE(thread_id, message_seq, ordinal)
            );
            CREATE INDEX IF NOT EXISTS idx_thread_message_attachments_message
                ON thread_message_attachments(thread_id, message_seq);

            CREATE TABLE IF NOT EXISTS thread_scoped_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attachment_id TEXT NOT NULL UNIQUE,
                thread_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                size_bytes INTEGER,
                content_type TEXT,
                uploaded_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES threads(id)
            );
            CREATE INDEX IF NOT EXISTS idx_thread_scoped_attachments_thread
                ON thread_scoped_attachments(thread_id);

            CREATE TABLE IF NOT EXISTS task_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                storage_key TEXT NOT NULL,
                display_name TEXT NOT NULL,
                size_bytes INTEGER,
                content_type TEXT,
                uploaded_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                legacy_status TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(id),
                UNIQUE(task_id, ordinal),
                UNIQUE(storage_key)
            );
            CREATE INDEX IF NOT EXISTS idx_task_attachments_task
                ON task_attachments(task_id);

            CREATE TABLE IF NOT EXISTS thread_invocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                invocation_token TEXT NOT NULL UNIQUE,
                triggering_seq INTEGER NOT NULL,
                purpose TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                enqueued_at TEXT NOT NULL,
                started_at TEXT,
                consumed_at TEXT,
                session_id TEXT,
                dispatched_task_id TEXT,
                decline_reason TEXT,
                FOREIGN KEY (thread_id) REFERENCES threads(id)
            );
            CREATE INDEX IF NOT EXISTS idx_thread_invocations_token
                ON thread_invocations(invocation_token);
            CREATE INDEX IF NOT EXISTS idx_thread_invocations_thread
                ON thread_invocations(thread_id);
            CREATE INDEX IF NOT EXISTS idx_thread_invocations_pending
                ON thread_invocations(status) WHERE status = 'pending';

            -- GitHub #688 Phase 1 Slice A: additive, provider-neutral
            -- per-(thread_id, agent_name) conversational REPLY delivery state.
            -- Intentionally dark until Slice B wires the route/runner
            -- activation; no existing writer/runner path reads or writes it.
            -- Invocation rows in ``thread_invocations`` remain the immutable
            -- per-attempt authority; this table only records which single
            -- queued/running REPLY token currently owns each pair's delivery
            -- obligation plus the acknowledged/required watermarks.
            CREATE TABLE IF NOT EXISTS thread_reply_delivery_state (
                thread_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                acknowledged_through_seq INTEGER NOT NULL DEFAULT 0,
                required_through_seq INTEGER NOT NULL DEFAULT 0,
                queued_invocation_token TEXT,
                running_invocation_token TEXT,
                running_from_seq INTEGER,
                running_through_seq INTEGER,
                last_terminal_reason TEXT,
                last_terminal_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (thread_id, agent_name),
                FOREIGN KEY (thread_id) REFERENCES threads(id)
            );
            CREATE INDEX IF NOT EXISTS idx_thread_reply_delivery_state_thread
                ON thread_reply_delivery_state(thread_id);

            -- THR-200 PR E: one additive breaker row per continuity identity.
            -- Absence is the authoritative CLOSED representation. Outcome
            -- receipts bind terminal accounting to immutable invocation tokens.
            CREATE TABLE IF NOT EXISTS thread_reply_breaker_episodes (
                thread_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                executor_key TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('closed','open','probe')),
                consecutive_failures INTEGER NOT NULL DEFAULT 0
                    CHECK (consecutive_failures >= 0),
                opened_at TEXT,
                cooldown_until TEXT,
                probe_lease_id TEXT,
                last_failure_category TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (thread_id, agent_name, executor_key),
                UNIQUE (episode_id),
                FOREIGN KEY (thread_id) REFERENCES threads(id)
            );
            CREATE INDEX IF NOT EXISTS idx_thread_reply_breaker_due
                ON thread_reply_breaker_episodes(state, cooldown_until);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_reply_breaker_probe_lease
                ON thread_reply_breaker_episodes(probe_lease_id)
                WHERE probe_lease_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS thread_reply_breaker_receipts (
                invocation_token TEXT PRIMARY KEY,
                episode_id TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (outcome IN ('failure','success')),
                failure_category TEXT,
                recorded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_thread_reply_breaker_receipts_episode
                ON thread_reply_breaker_receipts(episode_id);

            -- TASK-5966 (strict mention-led exchange): additive exchange-epoch
            -- gate. One row per mention-led exchange; rows are append-only and
            -- ``state`` is the only mutable field. The partial unique index
            -- enforces at most one OPEN exchange per thread (write-time
            -- invariant). ``open_seq`` is the opening founder-mention message;
            -- ``close_seq`` is the last conversational message inside E;
            -- ``deferred_count`` = |D(E)| frozen at open (audit/measurement).
            CREATE TABLE IF NOT EXISTS thread_reply_exchange (
                thread_id        TEXT NOT NULL,
                exchange_id      INTEGER NOT NULL,
                state            TEXT NOT NULL CHECK (state IN ('open','released','suppressed')),
                open_seq         INTEGER NOT NULL,
                close_seq        INTEGER NOT NULL,
                opened_at        TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                closed_at        TEXT,
                close_reason     TEXT CHECK (close_reason IN
                    ('quiescence','max_priority_wait','founder_aborted',
                     'thread_archived','participant_removed','corrupt')),
                deferred_count   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (thread_id, exchange_id),
                FOREIGN KEY (thread_id) REFERENCES threads(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trex_open
                ON thread_reply_exchange(thread_id) WHERE state = 'open';
            CREATE INDEX IF NOT EXISTS idx_trex_state
                ON thread_reply_exchange(state);
            CREATE INDEX IF NOT EXISTS idx_trex_thread_seq
                ON thread_reply_exchange(thread_id, open_seq);

            -- Frozen per-(exchange, deferred agent) rows; D(E) is frozen at
            -- open (write-time-frozen doctrine). Rows are audit/sweep
            -- substrate: release is watermark-based (every pair with unread
            -- content gets one slot-checked catch-up at closure, which is a
            -- superset of D(E)).
            CREATE TABLE IF NOT EXISTS thread_exchange_deferrals (
                thread_id        TEXT NOT NULL,
                exchange_id      INTEGER NOT NULL,
                agent_name       TEXT NOT NULL,
                state            TEXT NOT NULL CHECK (state IN ('held','released','suppressed')),
                created_at       TEXT NOT NULL,
                released_at      TEXT,
                mint_token_prefix TEXT,
                catchup_pending  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (thread_id, exchange_id, agent_name),
                FOREIGN KEY (thread_id, exchange_id)
                    REFERENCES thread_reply_exchange(thread_id, exchange_id)
            );
            CREATE INDEX IF NOT EXISTS idx_txed_state
                ON thread_exchange_deferrals(state);
            CREATE INDEX IF NOT EXISTS idx_txed_thread
                ON thread_exchange_deferrals(thread_id, exchange_id);

            CREATE TABLE IF NOT EXISTS jobs (
                id                       TEXT PRIMARY KEY,
                task_id                  TEXT NOT NULL,
                agent_name               TEXT NOT NULL,
                title                    TEXT NOT NULL,
                rationale                TEXT,
                script_text              TEXT NOT NULL,
                interpreter              TEXT NOT NULL,
                cwd_hint                 TEXT,
                review_required          INTEGER NOT NULL DEFAULT 0,
                persistent               INTEGER NOT NULL DEFAULT 0,
                max_runtime_seconds      INTEGER,
                max_output_bytes         INTEGER NOT NULL DEFAULT 52428800,
                status                   TEXT NOT NULL DEFAULT 'pending',
                exit_code                INTEGER,
                reason                   TEXT,
                duration_ms              INTEGER,
                stdout_head              TEXT,
                stderr_head              TEXT,
                stdout_path              TEXT,
                stderr_path              TEXT,
                stdout_bytes             INTEGER,
                stderr_bytes             INTEGER,
                cwd_resolved             TEXT,
                started_at               TEXT,
                finished_at              TEXT,
                reviewed_at              TEXT,
                reviewed_by              TEXT,
                reject_reason            TEXT,
                created_at               TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_task_id_idx ON jobs(task_id);
            CREATE INDEX IF NOT EXISTS jobs_status_idx  ON jobs(status);

            CREATE TABLE IF NOT EXISTS kb_views (
                slug           TEXT PRIMARY KEY,
                view_count     INTEGER NOT NULL DEFAULT 0,
                last_viewed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS org_settings (
                section     TEXT NOT NULL PRIMARY KEY,
                value_json  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                updated_by  TEXT DEFAULT 'founder'
            );
            CREATE TABLE IF NOT EXISTS skill_validation_events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_id       TEXT NOT NULL,
                slug           TEXT NOT NULL,
                agent          TEXT,
                source         TEXT NOT NULL DEFAULT 'user_authored',
                severity       TEXT NOT NULL DEFAULT 'info',
                ok             INTEGER NOT NULL DEFAULT 1,
                version        TEXT,
                findings       TEXT,
                reason_codes   TEXT,
                created_at     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sve_skill_id
                ON skill_validation_events(skill_id);
            CREATE INDEX IF NOT EXISTS idx_sve_agent
                ON skill_validation_events(agent);

            -- THR-055 B2 Slice A1: additive custom-skill schema. These tables
            -- are intentionally dark until the later route/resolver slices.
            CREATE TABLE IF NOT EXISTS custom_skills (
                id                  TEXT PRIMARY KEY,
                org_slug            TEXT NOT NULL,
                slug                TEXT NOT NULL,
                name                TEXT NOT NULL,
                description         TEXT NOT NULL DEFAULT '',
                policy_class        TEXT NOT NULL DEFAULT 'standard_operational' CHECK (policy_class = 'standard_operational'),
                origin_kind         TEXT NOT NULL CHECK (origin_kind IN ('agent', 'human')),
                origin_agent        TEXT,
                created_at          TEXT NOT NULL,
                created_by          TEXT NOT NULL,
                current_version_id  INTEGER,
                retired_at          TEXT,
                retired_by          TEXT,
                retired_reason      TEXT,
                purged_at           TEXT,
                purge_id            TEXT,
                FOREIGN KEY (current_version_id) REFERENCES custom_skill_versions(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_custom_skills_org_slug
                ON custom_skills(org_slug, slug);
            CREATE INDEX IF NOT EXISTS idx_custom_skills_origin_agent
                ON custom_skills(origin_agent) WHERE origin_agent IS NOT NULL;

            CREATE TABLE IF NOT EXISTS custom_skill_versions (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_id               TEXT NOT NULL REFERENCES custom_skills(id),
                parent_version_id      INTEGER REFERENCES custom_skill_versions(id),
                content_hash           TEXT NOT NULL,
                content_artifact_key   TEXT NOT NULL,
                skill_md_cache         TEXT,
                references_manifest    TEXT,
                assets_manifest        TEXT,
                validation_state       TEXT NOT NULL DEFAULT 'validation_required' CHECK (validation_state IN ('valid', 'invalid', 'validation_required')),
                validator_version       TEXT,
                validation_findings     TEXT,
                created_at              TEXT NOT NULL,
                author_kind             TEXT NOT NULL CHECK (author_kind IN ('agent', 'human')),
                author_identity          TEXT NOT NULL,
                source_task_id           TEXT,
                source_session_id        TEXT,
                task_brief_digest         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_csv_skill_id ON custom_skill_versions(skill_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_csv_skill_hash ON custom_skill_versions(skill_id, content_hash);

            CREATE TABLE IF NOT EXISTS custom_skill_eligibility_rules (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_id      TEXT NOT NULL REFERENCES custom_skills(id),
                scope_type    TEXT NOT NULL CHECK (scope_type IN ('org', 'team', 'agent')),
                scope_target  TEXT,
                effect        TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
                created_at    TEXT NOT NULL,
                created_by    TEXT NOT NULL,
                superseded_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cser_skill_current
                ON custom_skill_eligibility_rules(skill_id) WHERE superseded_at IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_cser_current_scope_unique
                ON custom_skill_eligibility_rules(skill_id, scope_type, COALESCE(scope_target, '')) WHERE superseded_at IS NULL;

            CREATE TABLE IF NOT EXISTS custom_skill_eligibility_events (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_id                TEXT NOT NULL REFERENCES custom_skills(id),
                actor                   TEXT NOT NULL,
                preview_revision        INTEGER NOT NULL,
                rule_set_json           TEXT NOT NULL,
                affected_newly_visible  TEXT NOT NULL,
                affected_newly_hidden   TEXT NOT NULL,
                created_at              TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_csee_skill_id ON custom_skill_eligibility_events(skill_id);

            CREATE TABLE IF NOT EXISTS custom_skill_materializations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_id        TEXT NOT NULL REFERENCES custom_skills(id),
                agent_name      TEXT NOT NULL,
                task_id         TEXT,
                session_context TEXT NOT NULL CHECK (session_context IN ('task', 'thread', 'wake', 'dream')),
                session_id      TEXT NOT NULL,
                version_id      INTEGER NOT NULL REFERENCES custom_skill_versions(id),
                content_hash    TEXT NOT NULL,
                success         INTEGER NOT NULL DEFAULT 0,
                error_message   TEXT,
                created_at      TEXT NOT NULL,
                CHECK ((session_context = 'task' AND task_id IS NOT NULL) OR (session_context != 'task'))
            );
            CREATE INDEX IF NOT EXISTS idx_csm_skill_agent ON custom_skill_materializations(skill_id, agent_name);
            CREATE INDEX IF NOT EXISTS idx_csm_session ON custom_skill_materializations(session_id);

            CREATE TABLE IF NOT EXISTS custom_skill_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_id      TEXT NOT NULL REFERENCES custom_skills(id),
                event_type    TEXT NOT NULL CHECK (event_type IN ('created', 'version_saved', 'validated', 'retired', 'restored')),
                actor         TEXT NOT NULL,
                version_id    INTEGER REFERENCES custom_skill_versions(id),
                metadata_json TEXT,
                created_at    TEXT NOT NULL,
                task_id       TEXT,
                session_id    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cse_skill_id ON custom_skill_events(skill_id);

            CREATE TABLE IF NOT EXISTS custom_skill_purge_events (
                purge_id       TEXT PRIMARY KEY,
                skill_id       TEXT NOT NULL UNIQUE REFERENCES custom_skills(id),
                org_slug       TEXT NOT NULL,
                slug           TEXT NOT NULL,
                actor          TEXT NOT NULL,
                purged_at      TEXT NOT NULL,
                physical_erasure INTEGER NOT NULL DEFAULT 0 CHECK (physical_erasure = 0)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_cspe_org_slug
                ON custom_skill_purge_events(org_slug, slug);

            """)
        for ddl in (
            "ALTER TABLE custom_skills ADD COLUMN purged_at TEXT",
            "ALTER TABLE custom_skills ADD COLUMN purge_id TEXT",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        self._migrate_session_token_usage_scope_columns()
        # Best-effort migration for DBs created before `status` existed. SQLite
        # has no IF NOT EXISTS for ADD COLUMN; swallow the duplicate-column
        # error so this is idempotent across restarts.
        try:
            self._conn.execute(
                "ALTER TABLE task_results ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"
            )
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN parent_task_id TEXT")
        except sqlite3.OperationalError:
            pass
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id)"
        )
        # Thread attachment thread_attachment_id column (additive, TASK-1616).
        try:
            self._conn.execute(
                "ALTER TABLE thread_message_attachments ADD COLUMN thread_attachment_id TEXT"
            )
        except sqlite3.OperationalError:
            pass
        # TASK-6057: ``thread_exchange_deferrals.catchup_pending`` (additive on
        # this PR's own unmerged exchange table). Fresh DBs get it from the
        # CREATE TABLE; DBs created by an earlier commit of this branch are
        # upgraded best-effort (idempotent across restarts).
        try:
            self._conn.execute(
                "ALTER TABLE thread_exchange_deferrals ADD COLUMN "
                "catchup_pending INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
        # NOTE: the for-loop below contains DDL (RENAME COLUMN) that has no
        # explicit commit; the commit() following the UPDATE team='engineering'
        # block durably persists those DDLs. Don't insert returning code between.
        for ddl in (
            "ALTER TABLE tasks ADD COLUMN final_output_summary TEXT",
            # Manager-only structured decision payload (serialized NextStep
            # JSON). NULL for worker rows. Replaces the prose-in-output_summary
            # double-encoding contract — see TASK-071 post-mortem.
            "ALTER TABLE task_results ADD COLUMN decision_json TEXT",
            # crew → team rename (SQLite >= 3.25). Idempotent: fails on
            # DBs where the column is already `team` or already renamed.
            "ALTER TABLE tasks RENAME COLUMN crew TO team",
            # Per-agent output-dir rename (2026-06-02). Idempotent: fails on DBs
            # where the column is already `final_output_dir`/`output_dir` (fresh or
            # already-renamed). See docs/superpowers/plans/2026-06-01-rename-assets-to-artifacts.md.
            "ALTER TABLE tasks RENAME COLUMN final_artifact_dir TO final_output_dir",
            "ALTER TABLE task_results RENAME COLUMN artifact_dir TO output_dir",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

        # Remap legacy team value: 'product_engineering' → 'engineering'.
        try:
            self._conn.execute(
                "UPDATE tasks SET team='engineering' WHERE team='product_engineering'"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # Path-string rewrite: stored relative paths under 'artifacts/' point at the
        # pre-rename per-agent dir. Rewrite to 'output/' so recall resolves correctly.
        # Idempotent: re-running matches no rows once paths have been rewritten.
        try:
            self._conn.execute(
                "UPDATE tasks SET final_output_dir = 'output/' || substr(final_output_dir, length('artifacts/') + 1) "
                "WHERE final_output_dir LIKE 'artifacts/%'"
            )
            self._conn.execute(
                "UPDATE task_results SET output_dir = 'output/' || substr(output_dir, length('artifacts/') + 1) "
                "WHERE output_dir LIKE 'artifacts/%'"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # THR-105 recurrence v2: nullable terminal cause for naturally ended
        # schedules. Existing rows intentionally remain NULL.
        try:
            self._conn.execute("ALTER TABLE schedules ADD COLUMN end_reason TEXT")
        except sqlite3.OperationalError:
            pass

        # --- Task-status redesign migration (idempotent) ---
        # Add new columns; swallow duplicate errors on subsequent startups.
        for ddl in (
            "ALTER TABLE tasks ADD COLUMN block_kind TEXT",
            "ALTER TABLE tasks ADD COLUMN note TEXT",
            "ALTER TABLE tasks ADD COLUMN orchestration_step_count INTEGER DEFAULT 0",
            # cancelled_at: founder-initiated cancellation marker. Distinct
            # from completed_at/status=failed so run_step can recognise a
            # SIGTERM'd session as "cancelled" (not a retryable failure) and
            # idempotent _fail calls don't overwrite the founder's note.
            "ALTER TABLE tasks ADD COLUMN cancelled_at TEXT",
            # Revisit link: see docs/superpowers/specs/2026-04-23-revisit-root-link-design.md.
            # Sideways reference to the predecessor root of a revisit; NULL for
            # non-revisit tasks. walk_ancestors MUST NOT follow this column —
            # that's the attempt-isolation invariant from the v2 revisit spec.
            "ALTER TABLE tasks ADD COLUMN revisit_of_task_id TEXT",
            # Liveness heartbeat: queue worker stamps this while a subprocess
            # is alive so `happyranch details` can show progress on long-running
            # tasks. Distinct from updated_at (which advances on any write).
            "ALTER TABLE tasks ADD COLUMN last_heartbeat TEXT",
            # Per-task subprocess timeout override. NULL → resolver falls
            # through to org/config.yaml then Settings default. Founder sets
            # via `happyranch revisit --session-timeout-seconds`; inherited from
            # parent on delegate and from predecessor root on revisit.
            "ALTER TABLE tasks ADD COLUMN session_timeout_seconds INTEGER",
            # Job-blocking link: spec §3.1. JSON array of JOB-NNN IDs that must
            # complete before this task can proceed. NULL means unblocked.
            "ALTER TABLE tasks ADD COLUMN blocked_on_job_ids TEXT",
            # Completion-report job-wait list: JSON array of JOB-NNN IDs the
            # agent asked to block on. Persisted alongside the task_result row
            # so run_step can read it back via _read_completion_from_db.
            "ALTER TABLE task_results ADD COLUMN waiting_on_job_ids TEXT",
            # Worker-reported verdict (free string: APPROVE, PASS, REQUEST_CHANGES,
            # etc.). Used by inline delegation chains to gate auto-advance to the
            # next leg without consuming the manager's orchestration_step_count.
            # NULL for non-chain or non-verdict workers.
            "ALTER TABLE task_results ADD COLUMN verdict TEXT",
            # Push-PR local CI evidence (additive, nullable). A JSON object
            # with command + exit_code persisted losslessly so audit and
            # reconstruction round-trips preserve it.
            "ALTER TABLE task_results ADD COLUMN local_ci TEXT",
            # Thread agent-session resume (issue #53). agent_session_id holds the
            # resumable agent session for this (thread, agent); NULL = none yet /
            # evicted. last_resumed_seq is the highest thread message seq the stored
            # session has been shown — the delta watermark, advanced only on a
            # successful turn.
            "ALTER TABLE thread_participants ADD COLUMN agent_session_id TEXT",
            "ALTER TABLE thread_participants ADD COLUMN last_resumed_seq INTEGER NOT NULL DEFAULT 0",
            # Legacy cleanup: drop the dead `type` column (dropped from the
            # current schema in the Task-4 refactor; never read, only a
            # "general" sentinel was written). Idempotent via the try/except
            # below — DROP of an absent column raises OperationalError.
            "ALTER TABLE tasks DROP COLUMN type",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # task_type column + one-time provenance backfill. Coupled in a single
        # try/except so the backfill UPDATE runs EXACTLY ONCE — when ADD COLUMN
        # succeeds on the first upgrade. On later startups (and on fresh DBs,
        # where CREATE TABLE already defines the column) ADD raises
        # duplicate-column and the whole block is skipped. Existing rows with a
        # parent were spawned from an ongoing task, so under the new model they
        # are subtasks (leaf); roots keep the 'task' default. Without this
        # backfill an in-flight pre-existing child would be mis-typed 'task' and
        # run_step would parse its plain completion as a NextStep decision and
        # escalate. (A task_type='task' row never has a parent, so the predicate
        # is provenance-correct and safe even if it ever re-ran.)
        try:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'task'"
            )
            self._conn.execute(
                "UPDATE tasks SET task_type='subtask' WHERE parent_task_id IS NOT NULL"
            )
        except sqlite3.OperationalError:
            pass
        # Index the reverse lookup (`WHERE revisit_of_task_id = ?`).
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_revisit_of ON tasks(revisit_of_task_id)"
        )
        try:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN dispatched_from_thread_id TEXT"
            )
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN active_chain TEXT"
            )
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN active_fanout TEXT"
            )
        except sqlite3.OperationalError:
            pass
        # THR-079: executor OS pid for daemon-restart liveness probe.
        # Persisted at _on_started (orchestrator.py); read by _sweep_on_startup.
        # NULL for pre-migration rows (fail-closed on first post-deploy restart).
        try:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN executor_pid INTEGER"
            )
        except sqlite3.OperationalError:
            pass
        # THR-090 Track A: current session id, persisted at _on_started
        # alongside executor_pid. Used by the daemon-restart sweep to scope
        # orphaned-result detection to the CURRENT session only. A prior-step
        # result row carries a different session uuid and must never match.
        # NULL for pre-migration rows (fail-closed: no session-scoped match
        # → falls through to dead-pid FAIL path).
        try:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN current_session_id TEXT"
            )
        except sqlite3.OperationalError:
            pass
        # THR-090 Track B: timestamp of first zombie detection for the
        # ongoing zombie reaper. Set on first flag; cleared (NULL) on
        # recovery; used for flag-then-cancel-on-TTL. NULL default —
        # never been flagged. Additive-only.
        try:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN zombie_flagged_at TEXT"
            )
        except sqlite3.OperationalError:
            pass
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_dispatched_from_thread_id "
            "ON tasks(dispatched_from_thread_id) "
            "WHERE dispatched_from_thread_id IS NOT NULL"
        )
        # Agent-initiated threads: composer attribution + session binding.
        # Sideways refs — NOT walked by walk_ancestors. Mutually exclusive at
        # insert time (daemon enforces); default 'founder' preserves all
        # existing rows on first migration.
        for ddl in (
            "ALTER TABLE threads ADD COLUMN composed_by TEXT NOT NULL DEFAULT 'founder'",
            "ALTER TABLE threads ADD COLUMN composed_from_task_id TEXT",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_threads_composed_from_task "
            "ON threads(composed_from_task_id) "
            "WHERE composed_from_task_id IS NOT NULL"
        )
        # Dream-originated threads: dream attribution marker (design-overhaul A4).
        # Additive nullable; existing rows stay NULL.
        try:
            self._conn.execute(
                "ALTER TABLE threads ADD COLUMN composed_from_dream_id TEXT"
            )
        except sqlite3.OperationalError:
            pass
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_threads_composed_from_dream "
            "ON threads(composed_from_dream_id) "
            "WHERE composed_from_dream_id IS NOT NULL"
        )
        # Founder-workspace pin state (THR-209): additive nullable timestamp;
        # non-NULL = pinned. Existing rows (and fresh inserts) stay NULL until
        # the founder pins. Presentation-only — never affects messages,
        # participants, unread, or lifecycle.
        try:
            self._conn.execute(
                "ALTER TABLE threads ADD COLUMN pinned_at TEXT"
            )
        except sqlite3.OperationalError:
            pass
        # Task-session post-to-existing-thread provenance (THR-027): the task id
        # whose live session appended a message via POST /threads/{id}/post-as-agent.
        # Additive nullable; existing rows + founder/compose/reply messages stay
        # NULL. No index — provenance is read by message, never queried by task.
        try:
            self._conn.execute(
                "ALTER TABLE thread_messages ADD COLUMN sent_from_task_id TEXT"
            )
        except sqlite3.OperationalError:
            pass
        # Phase-2 thread mention routing (THR-198, seq 108-110 approval):
        # per-thread default-enabled switch + per-message structured mention
        # signal. Both additive and statement-identical to the fresh CREATE
        # definitions above — existing threads adopt the enabled default,
        # historical messages stay NULL, no replay. Storage only in Slice A;
        # routing behavior lands in Slice B.
        try:
            self._conn.execute(
                "ALTER TABLE threads ADD COLUMN "
                "mention_routing_enabled INTEGER NOT NULL DEFAULT 1"
            )
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute(
                "ALTER TABLE thread_messages ADD COLUMN mentions_json TEXT"
            )
        except sqlite3.OperationalError:
            pass
        # kind column for escalation_notifications: 'escalation' (default) or
        # 'failure'. Additive; existing rows keep the default.
        try:
            self._conn.execute(
                "ALTER TABLE escalation_notifications ADD COLUMN kind "
                "TEXT NOT NULL DEFAULT 'escalation'"
            )
        except sqlite3.OperationalError:
            pass

        # --- Revisit link backfill ---
        # Historical revisit rows (created before revisit_of_task_id existed)
        # have the column but no value; the link lives only in audit_log's
        # revisit_of entry. Populate the column from those entries.
        # IS NULL guard makes this safely idempotent across restarts.
        self._backfill_revisit_of_task_id()

        # One-shot data remap. Guard with a sentinel so re-runs are no-ops.
        applied = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks' "
            "AND sql LIKE '%block_kind%'"
        ).fetchone()
        if applied is not None:
            # Fold final_output_summary → note where not already set.
            self._conn.execute(
                "UPDATE tasks SET note = final_output_summary "
                "WHERE note IS NULL AND final_output_summary IS NOT NULL"
            )
            # Old-world → new-world status mapping. Each UPDATE is narrow so
            # re-running is a no-op (no rows match the WHERE clause the 2nd time).
            self._conn.execute("UPDATE tasks SET status='completed' WHERE status='approved'")
            self._conn.execute("UPDATE tasks SET status='failed'    WHERE status='rejected'")
            self._conn.execute(
                "UPDATE tasks SET status='blocked', block_kind='escalated' "
                "WHERE status='escalated'"
            )
            # Normalize dead legacy values.
            self._conn.execute("UPDATE tasks SET status='failed' WHERE status='in_review'")
            # --- THR-037 Change B (Path B) live-row migration ---
            # Collapse the surfaced `blocked` vocabulary into the stored model:
            #   blocked(escalated)    → escalated (top-level), block_kind cleared
            #   blocked(delegated)    → in_progress, reason kept in block_kind
            #   blocked(blocked_on_job) → in_progress, reason kept in block_kind
            # Idempotent: each UPDATE's WHERE matches zero rows on re-run.
            # LIVE rows only — historical terminal rows (failed + cancelled_at)
            # are LEFT AS-IS; only new cancellations write status='cancelled'
            # (derivations read cancelled_at, not the status label). Forward-only
            # posture; the reverse migration is published in the Path-B spec
            # (docs/superpowers/specs/2026-06-27-task-status-pathB-stored-design.md).
            # No DDL: neither status nor block_kind has a CHECK constraint, so
            # the new values are application-enum-only.
            self._conn.execute(
                "UPDATE tasks SET status='escalated', block_kind=NULL "
                "WHERE status='blocked' AND block_kind='escalated'"
            )
            self._conn.execute(
                "UPDATE tasks SET status='in_progress' "
                "WHERE status='blocked' AND block_kind='delegated'"
            )
            self._conn.execute(
                "UPDATE tasks SET status='in_progress' "
                "WHERE status='blocked' AND block_kind='blocked_on_job'"
            )
            # --- THR-080 Slice A: rename resolved_superseded -> superseded ---
            # One-way DB row rewrite (founder-ratified). No dual-read; code
            # reads only 'superseded' after this. Idempotent across restarts.
            self._conn.execute(
                "UPDATE tasks SET status='superseded' "
                "WHERE status='resolved_superseded'"
            )
            self._conn.commit()

    def _create_authority_tables(self) -> None:
        """THR-181 Track A Slice 1: additive durable authority foundation.

        Creates three dedicated, clearly named ``authority_*`` tables plus
        their indexes and DB-level protections. This is *purely additive* —
        it never alters ``audit_log``, ``tasks``, or any existing column/row
        meaning (``audit_log.task_id`` scope prefixes and
        ``tasks.blocked_on_job_ids`` / revisit/lineage fields are untouched).

        Idempotent: every statement is ``IF NOT EXISTS``, so re-opening the
        same database (or a pre-migration v0 file) is a no-op on the second
        run. Boot ordering: called from ``__init__`` after ``_create_tables``
        so the FK target exists before the tables that reference it.

        Append-only surfaces (``authority_evaluations`` and
        ``authority_audit``) carry BEFORE UPDATE / BEFORE DELETE triggers that
        RAISE(ABORT). ``authority_candidates`` blocks deletion and any change
        to its identity columns, while allowing the narrow lifecycle
        transition (created -> evaluated -> consumed) performed only through
        the persistence API below.
        """
        self._conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS authority_policy_releases (
                id TEXT PRIMARY KEY,
                team TEXT NOT NULL,
                policy_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0),
                title TEXT NOT NULL,
                normative_text TEXT NOT NULL,
                clauses_json TEXT NOT NULL,
                continuation_phrase TEXT NOT NULL,
                canonical_payload_json TEXT NOT NULL,
                policy_digest TEXT NOT NULL UNIQUE,
                based_on_release_id TEXT REFERENCES authority_policy_releases(id) ON DELETE RESTRICT,
                actor_kind TEXT NOT NULL CHECK(actor_kind='shared_local_operator_credential'),
                created_at TEXT NOT NULL,
                UNIQUE(team, policy_id, version)
            );
            CREATE INDEX IF NOT EXISTS idx_authority_policy_releases_team_version
                ON authority_policy_releases(team, version DESC);
            CREATE TRIGGER IF NOT EXISTS authority_policy_releases_validate_insert
                BEFORE INSERT ON authority_policy_releases
                BEGIN
                    SELECT RAISE(ABORT, 'authority policy release base/team mismatch')
                    WHERE NEW.based_on_release_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM authority_policy_releases r
                        WHERE r.id=NEW.based_on_release_id AND r.team=NEW.team);
                END;
            CREATE TRIGGER IF NOT EXISTS authority_policy_releases_no_update
                BEFORE UPDATE ON authority_policy_releases
                BEGIN SELECT RAISE(ABORT, 'authority policy releases are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS authority_policy_releases_no_delete
                BEFORE DELETE ON authority_policy_releases
                BEGIN SELECT RAISE(ABORT, 'authority policy releases cannot be deleted'); END;

            CREATE TABLE IF NOT EXISTS authority_policy_activations (
                id TEXT PRIMARY KEY,
                team TEXT NOT NULL,
                epoch INTEGER NOT NULL CHECK(epoch > 0),
                release_id TEXT NOT NULL REFERENCES authority_policy_releases(id) ON DELETE RESTRICT,
                previous_activation_id TEXT REFERENCES authority_policy_activations(id) ON DELETE RESTRICT,
                expected_previous_epoch INTEGER CHECK(expected_previous_epoch IS NULL OR expected_previous_epoch >= 0),
                action TEXT NOT NULL CHECK(action IN ('activate','reactivate_rollback','bootstrap')),
                actor_kind TEXT NOT NULL CHECK(actor_kind='shared_local_operator_credential'),
                request_id TEXT NOT NULL UNIQUE,
                request_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                activation_digest TEXT NOT NULL,
                UNIQUE(team, epoch)
            );
            CREATE INDEX IF NOT EXISTS idx_authority_policy_activations_team_epoch
                ON authority_policy_activations(team, epoch DESC);
            CREATE TRIGGER IF NOT EXISTS authority_policy_activations_no_update
                BEFORE UPDATE ON authority_policy_activations
                BEGIN SELECT RAISE(ABORT, 'authority policy activations are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS authority_policy_activations_no_delete
                BEFORE DELETE ON authority_policy_activations
                BEGIN SELECT RAISE(ABORT, 'authority policy activations cannot be deleted'); END;

            CREATE TABLE IF NOT EXISTS authority_candidates (
                id                       TEXT PRIMARY KEY,
                claim_key                TEXT NOT NULL UNIQUE,
                root_task_id             TEXT NOT NULL,
                team                     TEXT NOT NULL,
                manager_agent            TEXT NOT NULL,
                manager_session_id       TEXT NOT NULL,
                causal_event_id          TEXT NOT NULL,
                causal_event_digest      TEXT NOT NULL,
                causal_result_id         TEXT,
                policy_id                TEXT NOT NULL,
                policy_version           TEXT NOT NULL,
                policy_digest            TEXT NOT NULL,
                prompt_id                TEXT NOT NULL,
                prompt_version           TEXT NOT NULL,
                prompt_digest            TEXT NOT NULL,
                model_id                 TEXT NOT NULL,
                model_version            TEXT NOT NULL,
                model_digest             TEXT NOT NULL,
                snapshot_digest          TEXT NOT NULL,
                snapshot_retention_class TEXT NOT NULL DEFAULT 'digest_only'
                    CHECK (snapshot_retention_class IN ('digest_only','shadow','indefinite')),
                snapshot_redaction_class TEXT NOT NULL DEFAULT 'redacted'
                    CHECK (snapshot_redaction_class IN ('none','redacted')),
                fence_results_json       TEXT,
                disposition              TEXT
                    CHECK (disposition IS NULL OR disposition IN
                        ('continue_same_root','escalate','not_applicable','evaluator_error')),
                lifecycle_state          TEXT NOT NULL DEFAULT 'created'
                    CHECK (lifecycle_state IN ('created','evaluated','consumed')),
                consumed_at              TEXT,
                created_at               TEXT NOT NULL,
                updated_at               TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_authority_candidates_root
                ON authority_candidates(root_task_id);
            CREATE INDEX IF NOT EXISTS idx_authority_candidates_root_outcome
                ON authority_candidates(root_task_id, disposition);

            CREATE TRIGGER IF NOT EXISTS authority_candidates_no_delete
                BEFORE DELETE ON authority_candidates
                BEGIN SELECT RAISE(ABORT, 'authority candidates cannot be deleted'); END;
            CREATE TRIGGER IF NOT EXISTS authority_candidates_identity_immutable
                BEFORE UPDATE ON authority_candidates
                WHEN OLD.claim_key != NEW.claim_key
                  OR OLD.root_task_id != NEW.root_task_id
                  OR OLD.team != NEW.team
                  OR OLD.manager_agent != NEW.manager_agent
                  OR OLD.manager_session_id != NEW.manager_session_id
                  OR OLD.causal_event_id != NEW.causal_event_id
                  OR OLD.causal_event_digest != NEW.causal_event_digest
                  OR OLD.causal_result_id IS NOT NEW.causal_result_id
                  OR OLD.policy_id != NEW.policy_id
                  OR OLD.policy_version != NEW.policy_version
                  OR OLD.policy_digest != NEW.policy_digest
                  OR OLD.prompt_id != NEW.prompt_id
                  OR OLD.prompt_version != NEW.prompt_version
                  OR OLD.prompt_digest != NEW.prompt_digest
                  OR OLD.model_id != NEW.model_id
                  OR OLD.model_version != NEW.model_version
                  OR OLD.model_digest != NEW.model_digest
                  OR OLD.snapshot_digest != NEW.snapshot_digest
                  OR OLD.snapshot_retention_class != NEW.snapshot_retention_class
                  OR OLD.snapshot_redaction_class != NEW.snapshot_redaction_class
                  OR OLD.fence_results_json IS NOT NEW.fence_results_json
                  OR OLD.created_at != NEW.created_at
                BEGIN
                    SELECT RAISE(ABORT, 'authority candidate identity is immutable');
                END;

            CREATE TABLE IF NOT EXISTS authority_candidate_policy_pins (
                candidate_id TEXT PRIMARY KEY REFERENCES authority_candidates(id) ON DELETE RESTRICT,
                release_id TEXT NOT NULL REFERENCES authority_policy_releases(id) ON DELETE RESTRICT,
                activation_id TEXT NOT NULL REFERENCES authority_policy_activations(id) ON DELETE RESTRICT,
                activation_epoch INTEGER NOT NULL CHECK(activation_epoch > 0),
                provider_id TEXT NOT NULL,
                executor_kind TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS authority_candidate_policy_pins_validate_insert
                BEFORE INSERT ON authority_candidate_policy_pins
                BEGIN
                    SELECT RAISE(ABORT, 'authority candidate pin identity mismatch')
                    WHERE NOT EXISTS (
                        SELECT 1 FROM authority_candidates c
                        JOIN authority_policy_releases r ON r.id=NEW.release_id
                        JOIN authority_policy_activations a ON a.id=NEW.activation_id
                        WHERE c.id=NEW.candidate_id AND c.team=r.team
                          AND c.team=a.team AND a.release_id=r.id
                          AND a.epoch=NEW.activation_epoch
                          AND c.policy_id=r.policy_id
                          AND c.policy_version=CAST(r.version AS TEXT)
                          AND c.policy_digest=r.policy_digest
                    );
                END;
            CREATE TRIGGER IF NOT EXISTS authority_candidate_policy_pins_no_update
                BEFORE UPDATE ON authority_candidate_policy_pins
                BEGIN SELECT RAISE(ABORT, 'authority candidate policy pins are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS authority_candidate_policy_pins_no_delete
                BEFORE DELETE ON authority_candidate_policy_pins
                BEGIN SELECT RAISE(ABORT, 'authority candidate policy pins cannot be deleted'); END;

            CREATE TABLE IF NOT EXISTS authority_evaluations (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id             TEXT NOT NULL UNIQUE REFERENCES authority_candidates(id),
                disposition              TEXT NOT NULL
                    CHECK (disposition IN
                        ('continue_same_root','escalate','not_applicable','evaluator_error')),
                disposition_code         TEXT NOT NULL
                    CHECK (disposition_code IN
                        ('continue_same_root','escalate','not_applicable','evaluator_error',
                         'low_confidence','timeout','malformed_output','injection_guard','audit_failure')),
                response_digest          TEXT NOT NULL,
                response_retention_class TEXT NOT NULL DEFAULT 'digest_only'
                    CHECK (response_retention_class IN ('digest_only','shadow','indefinite')),
                response_redaction_class TEXT NOT NULL DEFAULT 'redacted'
                    CHECK (response_redaction_class IN ('none','redacted')),
                fence_results_json       TEXT,
                created_at               TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_authority_evaluations_candidate
                ON authority_evaluations(candidate_id);

            CREATE TRIGGER IF NOT EXISTS authority_evaluations_no_update
                BEFORE UPDATE ON authority_evaluations
                BEGIN SELECT RAISE(ABORT, 'authority evaluations are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS authority_evaluations_no_delete
                BEFORE DELETE ON authority_evaluations
                BEGIN SELECT RAISE(ABORT, 'authority evaluations are append-only'); END;

            CREATE TABLE IF NOT EXISTS authority_audit (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT NOT NULL REFERENCES authority_candidates(id),
                event_type   TEXT NOT NULL
                    CHECK (event_type IN
                        ('candidate_claimed','candidate_claim_lost',
                         'evaluation_recorded','candidate_consumed')),
                payload_json TEXT,
                created_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_authority_audit_candidate
                ON authority_audit(candidate_id);

            CREATE TRIGGER IF NOT EXISTS authority_audit_no_update
                BEFORE UPDATE ON authority_audit
                BEGIN SELECT RAISE(ABORT, 'authority audit is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS authority_audit_no_delete
                BEFORE DELETE ON authority_audit
                BEGIN SELECT RAISE(ABORT, 'authority audit is append-only'); END;

            -- THR-181 Track A (founder lifecycle envelope): the single-use continuation
            -- envelope. Minted ATOMICALLY with ``commit_authority_continue_
            -- same_root`` (state='active'), bound 1:1 to the authority
            -- candidate/evaluation and to the immutable causal task-result row,
            -- and consumed EXACTLY ONCE by the continued turn's normally
            -- validated manager decision (``active -> consumed``), or spent
            -- fail-closed when the lifecycle window aborts (``violated``).
            -- DB-level enforcement (not only Python): no delete, immutable
            -- identity, and a narrow finite lifecycle transition. Additive
            -- table (same class as the merged authority_candidates/evaluations/
            -- audit foundation) — no existing column or overloaded-column
            -- semantics is touched.
            CREATE TABLE IF NOT EXISTS authority_continue_envelopes (
                id                   TEXT PRIMARY KEY,
                candidate_id         TEXT NOT NULL UNIQUE REFERENCES authority_candidates(id),
                root_task_id         TEXT NOT NULL,
                team                 TEXT NOT NULL,
                manager_agent        TEXT NOT NULL,
                manager_session_id   TEXT NOT NULL,
                causal_event_id      TEXT NOT NULL,
                causal_event_digest  TEXT NOT NULL,
                policy_id            TEXT NOT NULL,
                policy_version       TEXT NOT NULL,
                policy_digest        TEXT NOT NULL,
                clause_id            TEXT NOT NULL,
                action               TEXT NOT NULL
                    CHECK (action IN ('escalate_to_founder','continue_same_root')),
                state                TEXT NOT NULL DEFAULT 'active'
                    CHECK (state IN ('active','consumed','violated')),
                consumed_at          TEXT,
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_authority_envelopes_root_state
                ON authority_continue_envelopes(root_task_id, state);

            CREATE TRIGGER IF NOT EXISTS authority_continue_envelopes_no_delete
                BEFORE DELETE ON authority_continue_envelopes
                BEGIN SELECT RAISE(ABORT, 'authority continue envelopes cannot be deleted'); END;
            CREATE TRIGGER IF NOT EXISTS authority_continue_envelopes_identity_immutable
                BEFORE UPDATE ON authority_continue_envelopes
                WHEN OLD.candidate_id != NEW.candidate_id
                  OR OLD.root_task_id != NEW.root_task_id
                  OR OLD.team != NEW.team
                  OR OLD.manager_agent != NEW.manager_agent
                  OR OLD.manager_session_id != NEW.manager_session_id
                  OR OLD.causal_event_id != NEW.causal_event_id
                  OR OLD.causal_event_digest != NEW.causal_event_digest
                  OR OLD.policy_id != NEW.policy_id
                  OR OLD.policy_version != NEW.policy_version
                  OR OLD.policy_digest != NEW.policy_digest
                  OR OLD.clause_id != NEW.clause_id
                  OR OLD.action != NEW.action
                  OR OLD.created_at != NEW.created_at
                BEGIN
                    SELECT RAISE(ABORT, 'authority continue envelope identity is immutable');
                END;
            CREATE TRIGGER IF NOT EXISTS authority_continue_envelopes_lifecycle_guard
                BEFORE UPDATE ON authority_continue_envelopes
                WHEN NOT (
                    -- active -> consumed|violated: exactly-once; consumed_at
                    -- is stamped exactly once (NULL -> value).
                    (OLD.state = 'active' AND NEW.state IN ('consumed','violated')
                     AND OLD.consumed_at IS NULL AND NEW.consumed_at IS NOT NULL)
                    -- no-op on the guarded columns (e.g. updated_at-only writes).
                    OR (OLD.state = NEW.state
                        AND NEW.consumed_at IS OLD.consumed_at)
                )
                BEGIN
                    SELECT RAISE(ABORT, 'authority continue envelope lifecycle is restricted');
                END;

            -- DB-level lifecycle enforcement (not only Python): blocks fabrication
            -- through a raw ``Database.execute`` UPDATE. Only the intended finite
            -- transitions are permitted, disposition and consumed_at are immutable
            -- once set, and the ``evaluated``/``consumed`` states require a
            -- consistent ``authority_evaluations`` row for the candidate whose
            -- disposition exactly mirrors the candidate's frozen disposition.
            -- The trigger body references ``authority_evaluations`` (created above),
            -- which SQLite resolves at trigger execution time. Databases created at
            -- earlier reviewed heads that already carry the weaker trigger body are
            -- upgraded by ``_retrofit_authority_lifecycle_trigger_if_needed``.
            {_AUTHORITY_LIFECYCLE_GUARD_TRIGGER_SQL}
        """)
        self._conn.commit()

    def _migrate_dark_authority_activation_seal_if_needed(self) -> None:
        """Upgrade only an empty interrupted-development activation table.

        S1 is unmerged and dark.  A pre-seal table containing rows has no
        truthful stored activation seal to preserve, so reopening fails closed
        instead of inventing provenance.  Empty interrupted tables can safely
        receive the final required column before normal creation resumes.
        """
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='authority_policy_activations'"
        ).fetchone()
        if exists is None:
            return
        columns = {
            row["name"] for row in self._conn.execute(
                "PRAGMA table_info(authority_policy_activations)"
            )
        }
        if "activation_digest" in columns:
            return
        if self._conn.execute(
            "SELECT 1 FROM authority_policy_activations LIMIT 1"
        ).fetchone() is not None:
            raise ValueError(
                "populated dark authority activations lack truthful activation_digest"
            )
        self._conn.execute(
            "ALTER TABLE authority_policy_activations "
            "ADD COLUMN activation_digest TEXT NOT NULL"
        )
        self._conn.commit()

    def _retrofit_authority_audit_fk_if_needed(self) -> None:
        """Idempotent forward retrofit of the ``authority_audit`` candidate FK.

        The corrective head (07eaaed0) added
        ``authority_audit.candidate_id REFERENCES authority_candidates(id)``,
        but that reference lives only inside ``CREATE TABLE IF NOT EXISTS``. A
        database created at the prior reviewed head (405697a0) therefore
        retains an ``authority_audit`` table WITHOUT the FK after opening the
        corrected build — ``CREATE TABLE IF NOT EXISTS`` is a no-op on the
        already-existing table, so a raw ``Database.execute`` orphan INSERT
        succeeds and commits. Fresh-database tests cannot see this.

        This retrofit upgrades that legacy table in place, atomically and
        idempotently:

        * If ``authority_audit`` is absent, this is a no-op — the corrected
          ``_create_authority_tables`` creates it with the FK on fresh files.
        * If the table already carries the FK, this is a no-op (idempotent).
        * If the table is the legacy no-FK shape, it is rebuilt as a
          transactionally-safe replacement table: every valid row is copied
          verbatim (id, candidate_id, event_type, payload_json, created_at —
          identity and order preserved), the old table is dropped, the new
          table is renamed into place, and the index + append-only triggers
          are recreated. All DDL and the row copy run inside ONE explicit
          transaction, so a mid-migration failure rolls back with no partial
          replacement, no synthetic empty table, and no data loss; a later
          reopen retries the whole migration.
        * Legacy orphan audit rows (a ``candidate_id`` with no matching
          ``authority_candidates`` row) are never deleted, rewritten, or
          re-parented. The migration refuses atomically — raising
          :class:`AuthorityAuditMigrationRefusal` before any mutation — and
          leaves the old schema/data intact for inspection.

        ``executescript`` is deliberately avoided: it issues an implicit
        COMMIT and swallows mid-script rollback, which would defeat the
        atomicity requirement. Every statement runs through ``execute`` so a
        failure raises with the whole transaction rolled back.
        """
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authority_audit'"
        ).fetchone()
        if exists is None:
            return
        fks = self._conn.execute(
            "PRAGMA foreign_key_list(authority_audit)"
        ).fetchall()
        if fks:
            return
        orphan_count = self._conn.execute(
            "SELECT COUNT(*) FROM authority_audit a "
            "WHERE NOT EXISTS (SELECT 1 FROM authority_candidates c "
            "WHERE c.id = a.candidate_id)"
        ).fetchone()[0]
        if orphan_count:
            raise AuthorityAuditMigrationRefusal(
                f"authority_audit contains {orphan_count} legacy orphan row(s) whose "
                "candidate_id has no matching authority_candidates row; refusing to "
                "retrofit the candidate FK. The legacy schema and data are left intact "
                "for inspection."
            )
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                """
                CREATE TABLE authority_audit__new (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL REFERENCES authority_candidates(id),
                    event_type   TEXT NOT NULL
                        CHECK (event_type IN
                            ('candidate_claimed','candidate_claim_lost',
                             'evaluation_recorded','candidate_consumed')),
                    payload_json TEXT,
                    created_at   TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "INSERT INTO authority_audit__new "
                "(id, candidate_id, event_type, payload_json, created_at) "
                "SELECT id, candidate_id, event_type, payload_json, created_at "
                "FROM authority_audit"
            )
            self._conn.execute("DROP TABLE authority_audit")
            self._conn.execute(
                "ALTER TABLE authority_audit__new RENAME TO authority_audit"
            )
            self._conn.execute(
                "CREATE INDEX idx_authority_audit_candidate "
                "ON authority_audit(candidate_id)"
            )
            self._conn.execute(
                "CREATE TRIGGER authority_audit_no_update "
                "BEFORE UPDATE ON authority_audit "
                "BEGIN SELECT RAISE(ABORT, 'authority audit is append-only'); END;"
            )
            self._conn.execute(
                "CREATE TRIGGER authority_audit_no_delete "
                "BEFORE DELETE ON authority_audit "
                "BEGIN SELECT RAISE(ABORT, 'authority audit is append-only'); END;"
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _retrofit_authority_policy_activation_trigger_if_needed(self) -> None:
        """Create or retrofit the activation validator without per-open DDL.

        The full normalized stored definition is compared with the canonical
        statement, rather than checking one repaired predicate, so any absent,
        legacy, or otherwise stale trigger is rebuilt exactly once. A database
        that already carries the canonical trigger returns before DROP/CREATE,
        preserving ordinary-open WAL/SHM history.
        """
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger'"
            " AND name='authority_policy_activations_validate_insert'"
        ).fetchone()

        def normalized(sql: str) -> str:
            return " ".join(sql.strip().removesuffix(";").split())

        expected = normalized(_AUTHORITY_POLICY_ACTIVATIONS_VALIDATE_INSERT_SQL)
        if row is not None and normalized(row["sql"]) == expected:
            return
        try:
            self._conn.execute(
                "DROP TRIGGER IF EXISTS authority_policy_activations_validate_insert"
            )
            self._conn.execute(_AUTHORITY_POLICY_ACTIVATIONS_VALIDATE_INSERT_SQL)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _retrofit_authority_lifecycle_trigger_if_needed(self) -> None:
        """Idempotent forward retrofit of the lifecycle-guard trigger body.

        ``CREATE TRIGGER IF NOT EXISTS`` inside ``_create_authority_tables`` is
        a no-op on a database that already carries the trigger, so a database
        created at an earlier reviewed head (which embedded the weaker body
        that accepted ANY evaluation row) would keep the weak body forever.
        This retrofit drops and recreates the trigger ONLY when the stored
        body is the legacy one (detected by the missing disposition-mirroring
        condition); on every later open it is a no-op, so a database never
        carries per-boot DDL churn and the ``-wal``/``-shm`` history stays
        stable. The recreated body is the exact
        ``_AUTHORITY_LIFECYCLE_GUARD_TRIGGER_SQL`` constant used by
        ``_create_authority_tables``, so the two surfaces cannot drift.
        """
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger'"
            " AND name='authority_candidates_lifecycle_guard'"
        ).fetchone()
        if row is not None and "e.disposition = NEW.disposition" in row["sql"]:
            return
        self._conn.execute(
            "DROP TRIGGER IF EXISTS authority_candidates_lifecycle_guard"
        )
        self._conn.execute(_AUTHORITY_LIFECYCLE_GUARD_TRIGGER_SQL)
        self._conn.commit()

    def _migrate_session_token_usage_scope_columns(self) -> None:
        """Add scope columns and make task_id nullable for conversation usage."""
        columns = {
            row["name"]: row
            for row in self._conn.execute(
                "PRAGMA table_info(session_token_usage)"
            ).fetchall()
        }
        if columns.get("task_id") and columns["task_id"]["notnull"]:
            self._conn.execute(
                "ALTER TABLE session_token_usage RENAME TO session_token_usage_old"
            )
            self._conn.execute(
                """CREATE TABLE session_token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id    TEXT,
                    agent      TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    executor   TEXT NOT NULL,
                    model      TEXT,
                    input_tokens          INTEGER,
                    output_tokens         INTEGER,
                    cache_read_tokens     INTEGER,
                    cache_creation_tokens INTEGER,
                    reasoning_tokens      INTEGER,
                    usage_raw_json TEXT,
                    scope_type TEXT,
                    scope_id TEXT,
                    thread_id TEXT,
                    invocation_purpose TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (task_id, agent, session_id)
                )"""
            )
            self._conn.execute(
                """INSERT INTO session_token_usage
                   (id, task_id, agent, session_id, executor, model,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_creation_tokens, reasoning_tokens, usage_raw_json,
                    scope_type, scope_id, created_at)
                   SELECT id, task_id, agent, session_id, executor, model,
                          input_tokens, output_tokens, cache_read_tokens,
                          cache_creation_tokens, reasoning_tokens, usage_raw_json,
                          'task', task_id, created_at
                     FROM session_token_usage_old"""
            )
            self._conn.execute("DROP TABLE session_token_usage_old")
            columns = {
                row["name"]: row
                for row in self._conn.execute(
                    "PRAGMA table_info(session_token_usage)"
                ).fetchall()
            }

        for name in (
            "scope_type",
            "scope_id",
            "thread_id",
            "invocation_purpose",
        ):
            if name not in columns:
                try:
                    self._conn.execute(
                        f"ALTER TABLE session_token_usage ADD COLUMN {name} TEXT"
                    )
                except sqlite3.OperationalError:
                    pass

        self._conn.execute(
            "UPDATE session_token_usage SET scope_type = 'task' "
            "WHERE scope_type IS NULL"
        )
        self._conn.execute(
            "UPDATE session_token_usage SET scope_id = task_id "
            "WHERE scope_id IS NULL AND task_id IS NOT NULL"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_token_usage_task "
            "ON session_token_usage (task_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_token_usage_agent "
            "ON session_token_usage (agent, created_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_token_usage_scope "
            "ON session_token_usage (scope_type, scope_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_token_usage_thread "
            "ON session_token_usage (thread_id) WHERE thread_id IS NOT NULL"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_session_token_usage_scope_unique "
            "ON session_token_usage ("
            "COALESCE(scope_type, 'task'), COALESCE(scope_id, task_id), "
            "agent, session_id)"
        )
        self._conn.commit()

    def _backfill_revisit_of_task_id(self) -> None:
        # Called from _create_tables during __init__, which is single-threaded
        # by construction (Database is instantiated once per daemon, before
        # any worker threads start). Accessing self._conn directly without
        # @_synchronized is therefore safe here; do not call from elsewhere.
        cursor = self._conn.execute(
            "SELECT task_id, payload FROM audit_log WHERE action = 'revisit_of'"
        )
        for row in cursor.fetchall():
            if not row["payload"]:
                continue
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            predecessor_root = payload.get("predecessor_root")
            if not predecessor_root:
                continue
            self._conn.execute(
                "UPDATE tasks SET revisit_of_task_id = ? "
                "WHERE id = ? AND revisit_of_task_id IS NULL",
                (predecessor_root, row["task_id"]),
            )
        self._conn.commit()

    @_synchronized
    def list_tables(self) -> list[str]:
        cursor = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        return [row["name"] for row in cursor.fetchall()]

    # --- Tasks ---

    @_synchronized
    def insert_task(self, task: TaskRecord) -> None:
        params = (
            task.id,
            task.status.value,
            task.assigned_agent,
            task.team,
            task.brief,
            task.revision_count,
            task.created_at.isoformat(),
            task.updated_at.isoformat(),
            task.completed_at.isoformat() if task.completed_at else None,
            task.parent_task_id,
            task.revisit_of_task_id,
            task.dispatched_from_thread_id,
            task.block_kind.value if task.block_kind else None,
            task.note,
            task.orchestration_step_count,
            task.session_timeout_seconds,
            task.task_type,
            task.active_fanout,
            task.current_session_id,
            task.zombie_flagged_at.isoformat() if task.zombie_flagged_at else None,
        )
        self._conn.execute(
            """INSERT INTO tasks (id, status, assigned_agent, team, brief,
               revision_count, created_at, updated_at, completed_at, parent_task_id,
               revisit_of_task_id, dispatched_from_thread_id,
               block_kind, note,
               orchestration_step_count, session_timeout_seconds, task_type, active_fanout,
               current_session_id, zombie_flagged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            params,
        )
        self._conn.commit()

    @_synchronized
    def get_task(self, task_id: str) -> TaskRecord | None:
        cursor = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return TaskRecord(
            id=row["id"],
            status=row["status"],
            assigned_agent=row["assigned_agent"],
            team=row["team"],
            brief=row["brief"],
            revision_count=row["revision_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            parent_task_id=row["parent_task_id"],
            revisit_of_task_id=row["revisit_of_task_id"],
            dispatched_from_thread_id=row["dispatched_from_thread_id"],
            block_kind=row["block_kind"],
            blocked_on_job_ids=row["blocked_on_job_ids"],
            active_chain=row["active_chain"],
            active_fanout=row["active_fanout"],
            note=row["note"],
            orchestration_step_count=row["orchestration_step_count"] or 0,
            final_output_dir=row["final_output_dir"],
            cancelled_at=row["cancelled_at"],
            last_heartbeat=row["last_heartbeat"],
            session_timeout_seconds=row["session_timeout_seconds"],
            task_type=row["task_type"],
            executor_pid=row["executor_pid"],
            current_session_id=row["current_session_id"],
            zombie_flagged_at=row["zombie_flagged_at"],
        )

    @_synchronized
    def list_tasks(
        self,
        limit: int = 20,
        assigned_agent: str | None = None,
        before_task_id: str | None = None,
        status: TaskStatus | str | None = None,
        block_kind: BlockKind | str | None = None,
        blocked_on_job_id: str | None = None,
    ) -> list[TaskRecord]:
        # Cursor pagination: callers pass the last task_id of the previous page
        # as `before_task_id`; we resolve its created_at and emit the next page
        # using (created_at, id) DESC for a stable tiebreak. `status` and
        # `block_kind` are optional equality filters (read-only backlog queries).
        # `blocked_on_job_id` is a DERIVE filter for the Jobs "if-approved"
        # cascade — finds tasks blocked on a specific job id.
        cursor_created_at: str | None = None
        if before_task_id is not None:
            row = self._conn.execute(
                "SELECT created_at FROM tasks WHERE id = ?", (before_task_id,),
            ).fetchone()
            if row is None:
                return []
            cursor_created_at = row["created_at"]

        # Assemble the WHERE clause dynamically: with four optional filter
        # dimensions (agent, status, block_kind, cursor) an if/elif tree would
        # be 2**4 branches. StrEnum members stringify to their value, so str()
        # accepts both the enum and a raw query-param string.
        conditions: list[str] = []
        params: list = []
        if assigned_agent is not None:
            conditions.append("assigned_agent = ?")
            params.append(assigned_agent)
        if status is not None:
            conditions.append("status = ?")
            params.append(str(status))
        if block_kind is not None:
            conditions.append("block_kind = ?")
            params.append(str(block_kind))
        if blocked_on_job_id is not None:
            # Mirror jobs_runner.py canonic pred: status + block_kind + LIKE.
            # Without the status/block_kind guard a task that was once
            # blocked on JOB-X but is now done/running leaks into the
            # "if approved" cascade. Path B changed the parked carrier
            # from blocked(blocked_on_job) to in_progress(blocked_on_job).
            conditions.append(
                "status = ? AND block_kind = ? AND blocked_on_job_ids LIKE ?"
            )
            params.extend([
                TaskStatus.IN_PROGRESS.value,
                BlockKind.BLOCKED_ON_JOB.value,
                f'%"{blocked_on_job_id}"%',
            ])
        if cursor_created_at is not None:
            conditions.append("(created_at, id) < (?, ?)")
            params.extend([cursor_created_at, before_task_id])
        where = f"WHERE {' AND '.join(conditions)} " if conditions else ""
        params.append(limit)
        cursor = self._conn.execute(
            f"SELECT * FROM tasks {where}"
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            tuple(params),
        )
        return [
            TaskRecord(
                id=row["id"],
                status=row["status"],
                assigned_agent=row["assigned_agent"],
                team=row["team"],
                brief=row["brief"],
                revision_count=row["revision_count"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                completed_at=row["completed_at"],
                parent_task_id=row["parent_task_id"],
                revisit_of_task_id=row["revisit_of_task_id"],
                dispatched_from_thread_id=row["dispatched_from_thread_id"],
                block_kind=row["block_kind"],
                blocked_on_job_ids=row["blocked_on_job_ids"],
                note=row["note"],
                orchestration_step_count=row["orchestration_step_count"] or 0,
                final_output_dir=row["final_output_dir"],
                cancelled_at=row["cancelled_at"],
                last_heartbeat=row["last_heartbeat"],
                session_timeout_seconds=row["session_timeout_seconds"],
                task_type=row["task_type"],
                executor_pid=row["executor_pid"],
                current_session_id=row["current_session_id"],
                zombie_flagged_at=row["zombie_flagged_at"],
            )
            for row in cursor.fetchall()
        ]

    @_synchronized
    def get_children(self, parent_task_id: str) -> list[str]:
        """Return direct children of a task, ordered by creation time."""
        cursor = self._conn.execute(
            "SELECT id FROM tasks WHERE parent_task_id = ? ORDER BY created_at",
            (parent_task_id,),
        )
        return [row["id"] for row in cursor.fetchall()]

    @_synchronized
    def get_descendant_task_ids(self, root_task_id: str) -> list[str]:
        """Return all descendant task IDs in the parent_task_id subtree
        (direct children, grandchildren, etc.). Excludes the root itself.

        Uses the same iterative get_children() walk as get_subtree_statuses().
        """
        ids: list[str] = []
        stack = list(self.get_children(root_task_id))
        while stack:
            child_id = stack.pop()
            ids.append(child_id)
            stack.extend(self.get_children(child_id))
        return ids

    # Severity ranking for subtree rollup: lower = worse.
    # escalated is the attention-grabbing worst (genuine founder attention);
    # superseded is the calmest. Under the Path-B stored model
    # (THR-037 Change B) a delegating/parked parent is in_progress (rank 2),
    # so a healthy delegating parent NO LONGER dominates its subtree to amber —
    # only a real escalated (0) or failed (1) descendant pulls the rollup up.
    # cancelled is a deliberate terminal stop with no pending work, so it ranks
    # calmer than completed. The deprecated 'blocked' value is intentionally
    # absent: any lingering blocked row falls to the default rank (99, calmest).
    _SEVERITY_RANK: dict[str, int] = {
        "escalated": 0,
        "failed": 1,
        "in_progress": 2,
        "pending": 3,
        "completed": 4,
        "cancelled": 5,
        "superseded": 6,
    }

    @_synchronized
    def get_subtree_statuses(self, root_task_id: str) -> list[str]:
        """Return the status values of all descendant tasks in the
        parent_task_id subtree (direct children, grandchildren, etc.).

        Walks the tree recursively via get_children(). Excludes the root
        task itself; only descendants are collected. An empty list means the
        root has no children (rollup = the root's own status).

        This is a DERIVE — no schema change; uses existing parent_task_id
        and get_children().
        """
        statuses: list[str] = []
        stack = list(self.get_children(root_task_id))
        while stack:
            child_id = stack.pop()
            child = self.get_task(child_id)
            if child is not None:
                statuses.append(child.status.value)
                stack.extend(self.get_children(child_id))
        return statuses

    def _worst_subtree_status(self, root_status: str, child_statuses: list[str]) -> str:
        """Return the worst status among a root's own status and its
        descendants' statuses.

        The rollup of a singleton subtree is the root's own status (P1: no
        guessed severity). Uses _SEVERITY_RANK — lowest rank wins.
        """
        worst = root_status
        worst_rank = self._SEVERITY_RANK.get(worst, 99)
        for s in child_statuses:
            rank = self._SEVERITY_RANK.get(s, 99)
            if rank < worst_rank:
                worst = s
                worst_rank = rank
        return worst

    @_synchronized
    def list_roots(
        self,
        limit: int = 20,
        assigned_agent: str | None = None,
        before_task_id: str | None = None,
        status: TaskStatus | str | None = None,
        block_kind: BlockKind | str | None = None,
    ) -> list[TaskRecord]:
        """Return root tasks (parent_task_id IS NULL) with cursor pagination,
        same filter parameters as list_tasks(), plus a per-root _severity_rollup.

        The _severity_rollup attribute (str) is the worst status among the
        root's own status and its entire parent_task_id subtree. A root
        without children shows its own status. Set as a dynamic attribute on
        the TaskRecord (not a model field — DERIVE, no schema).
        """
        cursor_created_at: str | None = None
        if before_task_id is not None:
            row = self._conn.execute(
                "SELECT created_at FROM tasks WHERE id = ?", (before_task_id,),
            ).fetchone()
            if row is None:
                return []
            cursor_created_at = row["created_at"]

        conditions = ["parent_task_id IS NULL"]
        params: list = []
        if assigned_agent is not None:
            conditions.append("assigned_agent = ?")
            params.append(assigned_agent)
        if status is not None:
            conditions.append("status = ?")
            params.append(str(status))
        if block_kind is not None:
            conditions.append("block_kind = ?")
            params.append(str(block_kind))
        if cursor_created_at is not None:
            conditions.append("(created_at, id) < (?, ?)")
            params.extend([cursor_created_at, before_task_id])
        where = f"WHERE {' AND '.join(conditions)} "
        params.append(limit)
        cursor = self._conn.execute(
            f"SELECT * FROM tasks {where}"
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            tuple(params),
        )
        results: list[TaskRecord] = []
        for row in cursor.fetchall():
            task = TaskRecord(
                id=row["id"],
                status=row["status"],
                assigned_agent=row["assigned_agent"],
                team=row["team"],
                brief=row["brief"],
                revision_count=row["revision_count"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                completed_at=row["completed_at"],
                parent_task_id=row["parent_task_id"],
                revisit_of_task_id=row["revisit_of_task_id"],
                dispatched_from_thread_id=row["dispatched_from_thread_id"],
                block_kind=row["block_kind"],
                blocked_on_job_ids=row["blocked_on_job_ids"],
                note=row["note"],
                orchestration_step_count=row["orchestration_step_count"] or 0,
                final_output_dir=row["final_output_dir"],
                cancelled_at=row["cancelled_at"],
                last_heartbeat=row["last_heartbeat"],
                session_timeout_seconds=row["session_timeout_seconds"],
                task_type=row["task_type"],
                executor_pid=row["executor_pid"],
                current_session_id=row["current_session_id"],
                zombie_flagged_at=row["zombie_flagged_at"],
            )
            child_statuses = self.get_subtree_statuses(task.id)
            object.__setattr__(
                task, '_severity_rollup',
                self._worst_subtree_status(task.status.value, child_statuses),
            )
            results.append(task)
        return results

    @_synchronized
    def list_tasks_by_brief_prefix(
        self,
        brief_prefix: str,
        *,
        assigned_agent: str | None = None,
        limit: int = 200,
    ) -> list[TaskRecord]:
        """Tasks whose brief starts with ``brief_prefix``, newest-first.

        Authoritative SQL-side marker filter (THR-195 / TASK-6043): unlike a
        bounded scan of recent ordinary tasks, an SQL ``brief LIKE`` filter
        can never be exhausted by newer non-matching rows, so older
        marker-matched rows cannot be hidden. ``%`` and ``_`` in the prefix
        are escaped (treated literally). ``assigned_agent`` narrows to one
        agent's rows when given.
        """
        escaped = (
            brief_prefix.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        conditions = ["brief LIKE ? ESCAPE '\\'"]
        params: list = [escaped + "%"]
        if assigned_agent is not None:
            conditions.append("assigned_agent = ?")
            params.append(assigned_agent)
        params.append(limit)
        cursor = self._conn.execute(
            f"SELECT * FROM tasks WHERE {' AND '.join(conditions)} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            tuple(params),
        )
        return [
            TaskRecord(
                id=row["id"],
                status=row["status"],
                assigned_agent=row["assigned_agent"],
                team=row["team"],
                brief=row["brief"],
                revision_count=row["revision_count"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                completed_at=row["completed_at"],
                parent_task_id=row["parent_task_id"],
                revisit_of_task_id=row["revisit_of_task_id"],
                dispatched_from_thread_id=row["dispatched_from_thread_id"],
                block_kind=row["block_kind"],
                blocked_on_job_ids=row["blocked_on_job_ids"],
                note=row["note"],
                orchestration_step_count=row["orchestration_step_count"] or 0,
                final_output_dir=row["final_output_dir"],
                cancelled_at=row["cancelled_at"],
                last_heartbeat=row["last_heartbeat"],
                session_timeout_seconds=row["session_timeout_seconds"],
                task_type=row["task_type"],
                executor_pid=row["executor_pid"],
                current_session_id=row["current_session_id"],
                zombie_flagged_at=row["zombie_flagged_at"],
            )
            for row in cursor.fetchall()
        ]

    @_synchronized
    def list_tasks_by_thread(
        self, thread_id: str,
    ) -> list[dict]:
        """Return tasks dispatched from a thread, newest-first.

        Uses the existing idx_tasks_dispatched_from_thread_id partial index.
        Returns lightweight summary dicts with the fields the frontend needs:
        id, status, brief, assigned_agent, created_at, parent_task_id.
        """
        cursor = self._conn.execute(
            "SELECT id, status, brief, assigned_agent, created_at, parent_task_id "
            "FROM tasks WHERE dispatched_from_thread_id = ? "
            "ORDER BY created_at DESC",
            (thread_id,),
        )
        return [
            {
                "id": row["id"],
                "status": row["status"],
                "brief": row["brief"],
                "assigned_agent": row["assigned_agent"],
                "created_at": row["created_at"],
                "parent_task_id": row["parent_task_id"],
            }
            for row in cursor.fetchall()
        ]

    @_synchronized
    def get_direct_revisits(self, task_id: str) -> list[str]:
        """Return IDs of tasks whose revisit_of_task_id points at this task,
        ordered by creation. Uses idx_tasks_revisit_of.
        """
        cursor = self._conn.execute(
            "SELECT id FROM tasks WHERE revisit_of_task_id = ? ORDER BY created_at",
            (task_id,),
        )
        return [row["id"] for row in cursor.fetchall()]

    @_synchronized
    def batch_get_direct_revisits(
        self, task_ids: list[str],
    ) -> dict[str, list[str]]:
        """Return direct revisits for multiple task_ids in a single query.

        Avoids the N+1 pattern when a list route needs direct_revisits for
        every returned item. Uses idx_tasks_revisit_of.
        """
        if not task_ids:
            return {}
        placeholders = ','.join(['?'] * len(task_ids))
        cursor = self._conn.execute(
            f"SELECT revisit_of_task_id, id FROM tasks"
            f" WHERE revisit_of_task_id IN ({placeholders})"
            f" ORDER BY created_at",
            tuple(task_ids),
        )
        result: dict[str, list[str]] = {tid: [] for tid in task_ids}
        for row in cursor.fetchall():
            root_id = row["revisit_of_task_id"]
            result.setdefault(root_id, []).append(row["id"])
        return result

    @_synchronized
    def walk_ancestors(self, task_id: str, max_hops: int = 20) -> list[TaskRecord]:
        """Return [task, parent, ..., root] by following parent_task_id.

        Raises LineageTooDeep if the walk exceeds max_hops (defensive bound;
        real lineages are 2-4 deep). A missing intermediate task truncates the
        walk silently — callers see the chain they could reconstruct.
        """
        chain: list[TaskRecord] = []
        current_id: str | None = task_id
        for _ in range(max_hops):
            if current_id is None:
                return chain
            task = self.get_task(current_id)
            if task is None:
                return chain
            chain.append(task)
            current_id = task.parent_task_id
        if current_id is not None:
            raise LineageTooDeep(f"walk from {task_id} exceeded {max_hops} hops")
        return chain

    @_synchronized
    def walk_revisit_chain(
        self, task_id: str, max_hops: int = 20, truncate: bool = False,
    ) -> list[TaskRecord]:
        """Return [task, predecessor, ..., original] by following revisit_of_task_id.

        Sideways edge — does NOT cross into parent_task_id ancestor space.
        Non-revisit tasks return [task]. Missing task returns []. Overruns
        raise LineageTooDeep by default (same pattern as walk_ancestors); pass
        truncate=True to return the first max_hops entries instead — read
        paths use this because revisit history grows naturally over a task's
        lifetime and must not 500 once it exceeds the defensive bound.
        """
        chain: list[TaskRecord] = []
        current_id: str | None = task_id
        for _ in range(max_hops):
            if current_id is None:
                return chain
            task = self.get_task(current_id)
            if task is None:
                return chain
            chain.append(task)
            current_id = task.revisit_of_task_id
        if current_id is not None and not truncate:
            raise LineageTooDeep(
                f"revisit chain from {task_id} exceeded {max_hops} hops"
            )
        return chain

    @_synchronized
    def get_recall_payload(self, task_id: str) -> dict | None:
        """Return a flat dict suitable for the /recall endpoint, or None.

        ``children`` is the list of direct child task ids — the route layer
        promotes them to full payloads when ``tree=true``.

        ``verdict`` is the structured verdict from the latest persisted
        ``task_results`` row (deterministic: ``ORDER BY created_at DESC, id DESC LIMIT 1``
        — result-recency first with a stable id tie-breaker).
        Absent / ``None`` when no result row exists or the latest row has no verdict.
        """
        task = self.get_task(task_id)
        if task is None:
            return None
        created_at = (
            task.created_at.isoformat()
            if hasattr(task.created_at, "isoformat")
            else task.created_at
        )
        completed_at = (
            task.completed_at.isoformat()
            if hasattr(task.completed_at, "isoformat")
            else task.completed_at
        )
        # Latest structured verdict from persisted task_results, if any.
        verdict: str | None = None
        latest = self._conn.execute(
            "SELECT verdict FROM task_results WHERE task_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if latest is not None:
            verdict = latest["verdict"] if "verdict" in latest.keys() else None
        return {
            "task_id": task.id,
            "parent_task_id": task.parent_task_id,
            "revisit_of_task_id": task.revisit_of_task_id,
            "assigned_agent": task.assigned_agent,
            "brief": task.brief,
            "status": task.status.value,
            "created_at": created_at,
            "completed_at": completed_at,
            "output_summary": task.note,
            "output_dir": task.final_output_dir,
            "verdict": verdict,
            "children": self.get_children(task.id),
        }

    @_synchronized
    def list_agent_tasks(self, agent: str, limit: int = 50) -> list[TaskRecord]:
        """Return tasks assigned to an agent, newest-first.

        Orders by the latest available timestamp (completed_at > updated_at >
        created_at) as a lexicographic string compare — our ISO-8601 values
        include microseconds and +00:00 which SQLite's ``datetime()`` parser
        rejects, but they sort correctly as raw strings.
        """
        cursor = self._conn.execute(
            """SELECT * FROM tasks WHERE assigned_agent = ?
               ORDER BY COALESCE(completed_at, updated_at, created_at) DESC
               LIMIT ?""",
            (agent, limit),
        )
        return [
            TaskRecord(
                id=row["id"],
                status=row["status"],
                assigned_agent=row["assigned_agent"],
                team=row["team"],
                brief=row["brief"],
                revision_count=row["revision_count"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                completed_at=row["completed_at"],
                parent_task_id=row["parent_task_id"],
                revisit_of_task_id=row["revisit_of_task_id"],
                dispatched_from_thread_id=row["dispatched_from_thread_id"],
                block_kind=row["block_kind"],
                blocked_on_job_ids=row["blocked_on_job_ids"],
                note=row["note"],
                orchestration_step_count=row["orchestration_step_count"] or 0,
                final_output_dir=row["final_output_dir"],
                cancelled_at=row["cancelled_at"],
                last_heartbeat=row["last_heartbeat"],
                session_timeout_seconds=row["session_timeout_seconds"],
                task_type=row["task_type"],
                executor_pid=row["executor_pid"],
                current_session_id=row["current_session_id"],
                zombie_flagged_at=row["zombie_flagged_at"],
            )
            for row in cursor.fetchall()
        ]

    @_synchronized
    def update_task(self, task_id: str, **fields: object) -> None:
        allowed = {
            "status", "assigned_agent", "revision_count", "completed_at",
            "block_kind", "blocked_on_job_ids", "note", "orchestration_step_count",
            "final_output_dir", "cancelled_at", "last_heartbeat",
            "executor_pid", "current_session_id", "zombie_flagged_at",
        }
        # NOTE: filter on membership, not on None-ness — block_kind must be
        # resettable to NULL when a task unblocks.
        updates: dict[str, object] = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if hasattr(v, "value"):
                updates[k] = v.value
            else:
                updates[k] = v
        if not updates:
            return
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [task_id]
        self._conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        self._conn.commit()

    @_synchronized
    def update_task_active_chain(self, task_id: str, active_chain: str | None) -> None:
        """Set or clear tasks.active_chain. Pass None to clear (chain finished,
        aborted, or never declared)."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE tasks SET active_chain = ?, updated_at = ? WHERE id = ?",
            (active_chain, now, task_id),
        )
        self._conn.commit()

    @_synchronized
    def update_task_active_fanout(self, task_id: str, active_fanout: str | None) -> None:
        """Set or clear tasks.active_fanout. Pass None to clear (fan-out join
        claimed or parent terminal)."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE tasks SET active_fanout = ?, updated_at = ? WHERE id = ?",
            (active_fanout, now, task_id),
        )
        self._conn.commit()

    @_synchronized
    def try_delegate_many(
        self, parent_id: str, children: list, *, parent_note: str,
        active_fanout_json: str | None = None,
        children_attachments: list[list[dict] | None] | None = None,
        carrier_chains: list[dict] | None = None,
        uploaded_by: str = "orchestrator",
    ) -> bool:
        """Atomic CAS: insert N child tasks + transition parent to
        IN_PROGRESS(DELEGATED) under a single explicit SQL transaction.

        All child inserts, parent status/block_kind/active_fanout update,
        note write, attachment links/audit rows, and pipeline carrier chain
        materialization (active_chain + first leg insert) happen in one
        transaction. On any exception the transaction rolls back — no partial
        children, no orphan rows, no orphan attachment links, no orphan
        carrier state.

        Same cancel-race semantics as try_delegate (single-child): if the
        parent is cancelled or already terminal at the time of the guarded
        SELECT, no children are inserted and the parent is not overwritten.

        When ``children_attachments`` is provided, it must be a list of the
        same length as ``children``; each element is either a list of
        attachment param dicts for that child, or None/empty.

        When ``carrier_chains`` is provided, it must be a list of dicts with
        keys ``child_index`` (int), ``active_chain_json`` (str), and
        ``first_leg`` (dict with keys: id, team, brief, assigned_agent,
        status, session_timeout_seconds, task_type). The first leg is
        inserted as a child of the carrier within the same transaction.

        On True: all children (and carrier first legs) exist and parent has
        transitioned.  On False: no DB changes were made.

        Children must already have their IDs allocated (caller calls
        next_task_id() N times before invoking this method). Carrier first
        leg IDs must also be pre-allocated.
        """
        cursor = self._conn.execute(
            "SELECT status, cancelled_at FROM tasks WHERE id = ?", (parent_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return False
        if row["cancelled_at"] is not None or row["status"] in (
            "completed", "failed", "superseded", "cancelled",
        ):
            return False
        now = datetime.now(timezone.utc).isoformat()
        try:
            # One explicit transaction: all child inserts + parent transition.
            self._conn.execute("BEGIN IMMEDIATE")
            for i, child in enumerate(children):
                self._conn.execute(
                    """INSERT INTO tasks (id, status, assigned_agent, team, brief,
                       revision_count, created_at, updated_at, completed_at, parent_task_id,
                       revisit_of_task_id, dispatched_from_thread_id,
                       block_kind, note,
                       orchestration_step_count, session_timeout_seconds, task_type, active_fanout)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        child.id,
                        child.status.value,
                        child.assigned_agent,
                        child.team,
                        child.brief,
                        child.revision_count,
                        child.created_at.isoformat(),
                        child.updated_at.isoformat(),
                        child.completed_at.isoformat() if child.completed_at else None,
                        child.parent_task_id,
                        child.revisit_of_task_id,
                        child.dispatched_from_thread_id,
                        child.block_kind.value if child.block_kind else None,
                        child.note,
                        child.orchestration_step_count,
                        child.session_timeout_seconds,
                        child.task_type,
                        child.active_fanout,
                    ),
                )
                # Insert attachment links for this child if present.
                child_atts = None
                if children_attachments and i < len(children_attachments):
                    child_atts = children_attachments[i]
                if child_atts:
                    self._insert_task_attachments_txn(
                        child.id, child_atts, uploaded_by,
                    )
            # Materialize pipeline carrier chains within the same transaction.
            # active_chain on the carrier + first leg insert as child of carrier
            # are atomic with the fanout spawn — no partial carrier state.
            if carrier_chains:
                for cc in carrier_chains:
                    ci = cc["child_index"]
                    cid = children[ci].id
                    self._conn.execute(
                        "UPDATE tasks SET active_chain = ? WHERE id = ?",
                        (cc["active_chain_json"], cid),
                    )
                    fl = cc["first_leg"]
                    self._conn.execute(
                        """INSERT INTO tasks (id, status, assigned_agent, team, brief,
                           revision_count, created_at, updated_at, completed_at,
                           parent_task_id, revisit_of_task_id, dispatched_from_thread_id,
                           block_kind, note,
                           orchestration_step_count, session_timeout_seconds, task_type,
                           active_fanout)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            fl["id"],
                            fl["status"].value if isinstance(fl["status"], TaskStatus) else fl["status"],
                            fl["assigned_agent"],
                            fl["team"],
                            fl["brief"],
                            fl.get("revision_count", 0),
                            fl.get("created_at", now),
                            fl.get("updated_at", now),
                            None,
                            cid,
                            None,
                            None,
                            None,
                            None,
                            fl.get("orchestration_step_count", 0),
                            fl.get("session_timeout_seconds", 0),
                            fl.get("task_type", "subtask"),
                            None,
                        ),
                    )
            self._conn.execute(
                """UPDATE tasks
                   SET status = ?, block_kind = ?, note = ?, active_fanout = ?, updated_at = ?
                   WHERE id = ?""",
                (TaskStatus.IN_PROGRESS.value, BlockKind.DELEGATED.value, parent_note,
                 active_fanout_json, now, parent_id),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def try_claim_for_step(
        self,
        task_id: str,
        expected_status: TaskStatus,
        expected_block_kind: BlockKind | None,
        new_count: int,
    ) -> bool:
        """Atomic compare-and-swap for the run_step entry transition.

        Transitions the row to status=in_progress, clears block_kind/note, and
        sets orchestration_step_count=new_count, but ONLY if the row currently
        matches (expected_status, expected_block_kind). Returns True iff the
        transition occurred.

        Why this exists: two workers can pop the same task_id (e.g. a multi-
        child fan-in double-enqueued the parent). Without this CAS, both pass
        the check-then-update at run_step steps 1→3 and both spawn an agent
        subprocess. The conditional WHERE ensures only the first writer wins.
        """
        now = datetime.now(timezone.utc).isoformat()
        if expected_block_kind is None:
            cursor = self._conn.execute(
                """UPDATE tasks
                   SET status = ?, block_kind = NULL, note = NULL,
                       orchestration_step_count = ?, updated_at = ?
                   WHERE id = ? AND status = ? AND block_kind IS NULL""",
                (TaskStatus.IN_PROGRESS.value, new_count, now,
                 task_id, expected_status.value),
            )
        else:
            cursor = self._conn.execute(
                """UPDATE tasks
                   SET status = ?, block_kind = NULL, note = NULL,
                       orchestration_step_count = ?, updated_at = ?
                   WHERE id = ? AND status = ? AND block_kind = ?""",
                (TaskStatus.IN_PROGRESS.value, new_count, now,
                 task_id, expected_status.value, expected_block_kind.value),
            )
        self._conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def try_escalate(self, task_id: str, *, reason: str) -> bool:
        """Atomic CAS: transition task to ESCALATED (Path B top-level status,
        block_kind cleared) only if it isn't cancelled or already terminal.

        Closes the post-_is_already_terminal race in the escalate decision
        branch — the Python-level check + UPDATE pair was non-atomic with the
        cancel route's UPDATE. By gating the transition with a SQL `WHERE
        cancelled_at IS NULL AND status NOT IN (...)` predicate under the
        Database RLock (same lock the cancel route's update_task uses), the
        operation serializes against cancel: either cancel ran first and we
        see cancelled_at != NULL → bail, or we ran first and cancel observes
        escalated → transitions cleanly to FAILED on its own.

        Returns True iff the row transitioned.

        See docs/superpowers/specs/2026-05-26-cancel-race-design.md §5.3
        (Codex review of PR #34 surfaced the residual race).
        """
        now = datetime.now(timezone.utc).isoformat()
        cursor = self._conn.execute(
            """UPDATE tasks
               SET status = ?, block_kind = NULL, note = ?, updated_at = ?
               WHERE id = ?
                 AND cancelled_at IS NULL
                 AND status NOT IN ('completed', 'failed', 'superseded', 'cancelled')""",
            (TaskStatus.ESCALATED.value, reason, now, task_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def try_escalate_runtime(
        self,
        task_id: str,
        *,
        reason: str,
        agent: str,
        reason_code: str,
        expected_status: TaskStatus | None = None,
        expected_block_kind: BlockKind | None = None,
        match_expected_state: bool = False,
        clear_active_fanout: bool = False,
    ) -> bool:
        """Atomically commit a runtime-raised escalation and its audit pair.

        Runtime mechanical fences are not authority decisions, so they never
        invoke the evaluator or create candidate/evaluation rows. Track A
        nevertheless requires one explicit ``authority_hook:not_applicable``
        denominator row linked to the committed ``escalation`` row.

        The task transition and both audit rows share one transaction. A lost
        CAS changes nothing; any audit append failure rolls the transition and
        both rows back. Re-entry after a committed escalation loses because an
        already-escalated task is excluded from the update predicate.
        """
        now = datetime.now(timezone.utc).isoformat()
        set_active_fanout = ", active_fanout = NULL" if clear_active_fanout else ""
        if match_expected_state:
            if expected_status is None:
                raise ValueError("expected_status is required for an expected-state CAS")
            if expected_block_kind is None:
                state_predicate = "status = ? AND block_kind IS NULL"
                state_args: tuple = (expected_status.value,)
            else:
                state_predicate = "status = ? AND block_kind = ?"
                state_args = (expected_status.value, expected_block_kind.value)
        else:
            state_predicate = (
                "cancelled_at IS NULL AND status NOT IN "
                "('completed', 'failed', 'superseded', 'cancelled', 'escalated')"
            )
            state_args = ()

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                f"""UPDATE tasks
                    SET status = ?, block_kind = NULL, note = ?, updated_at = ?
                        {set_active_fanout}
                    WHERE id = ? AND {state_predicate}""",
                (TaskStatus.ESCALATED.value, reason, now, task_id, *state_args),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return False
            escalation_audit_id = self.insert_audit_log_uncommitted(
                task_id=task_id,
                agent=agent,
                action="escalation",
                payload={"reason": reason},
            )
            self.insert_audit_log_uncommitted(
                task_id=task_id,
                agent=agent,
                action="authority_hook",
                payload={
                    "outcome": "not_applicable",
                    "reason_code": reason_code,
                    "reason": "runtime-raised escalation is not an authority decision",
                    "causal_escalation_audit_id": escalation_audit_id,
                },
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def try_escalate_over_budget(
        self,
        task_id: str,
        *,
        expected_status: TaskStatus,
        expected_block_kind: BlockKind | None,
        reason: str,
    ) -> bool:
        """Atomic CAS for the run_step max-steps budget guard.

        Transitions the row to ESCALATED (Path B top-level status, block_kind
        cleared) with note=reason, but ONLY if it still matches
        (expected_status, expected_block_kind) — the eligible pre-state observed
        at run_step step 1. Returns True iff it transitioned.

        Why this exists: the budget guard runs BEFORE try_claim_for_step, so it
        has no upstream CAS. Two duplicate queue deliveries can both read the
        same stale at-cap eligible row and both escalate, double-posting the
        thread `task_escalated` message + TASK_FOLLOWUP invocation. The
        conditional WHERE makes only the first writer win; the loser matches
        zero rows and bails. A /cancel landing in the window also moves the row
        out of the expected pre-state, so the CAS rejects it for free.
        """
        now = datetime.now(timezone.utc).isoformat()
        if expected_block_kind is None:
            cursor = self._conn.execute(
                """UPDATE tasks
                   SET status = ?, block_kind = NULL, note = ?, updated_at = ?
                   WHERE id = ? AND status = ? AND block_kind IS NULL""",
                (TaskStatus.ESCALATED.value, reason, now,
                 task_id, expected_status.value),
            )
        else:
            cursor = self._conn.execute(
                """UPDATE tasks
                   SET status = ?, block_kind = NULL, note = ?, updated_at = ?
                   WHERE id = ? AND status = ? AND block_kind = ?""",
                (TaskStatus.ESCALATED.value, reason, now,
                 task_id, expected_status.value, expected_block_kind.value),
            )
        self._conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def try_fail_over_budget(
        self,
        task_id: str,
        *,
        expected_status: TaskStatus,
        expected_block_kind: BlockKind | None,
        note: str,
    ) -> bool:
        """Atomic CAS for the run_step max-steps budget guard — non-root variant.

        Mirror of ``try_escalate_over_budget`` (the root variant), but transitions
        the row to FAILED (block_kind NULL, completed_at set — FAILED is terminal,
        unlike the ESCALATED template) ONLY if it still matches
        (expected_status, expected_block_kind). Returns True iff it transitioned.

        Per THR-033 Change A a NON-root task that hits the step budget must not
        escalate directly to the founder — it fails and hands back to its parent
        (bounded failure-recovery carries it up). The CAS is required for the same
        reason as ``try_escalate_over_budget``: the budget guard runs BEFORE
        try_claim_for_step, so it has no upstream CAS. Two duplicate queue
        deliveries can both read the same stale at-cap eligible row; the
        conditional WHERE makes only the first writer win, so the parent enqueue +
        thread followup fire exactly once. A /cancel landing in the window moves
        the row out of the expected pre-state and the CAS rejects it for free.
        """
        now = datetime.now(timezone.utc).isoformat()
        if expected_block_kind is None:
            cursor = self._conn.execute(
                """UPDATE tasks
                   SET status = ?, block_kind = NULL, note = ?,
                       completed_at = ?, updated_at = ?
                   WHERE id = ? AND status = ? AND block_kind IS NULL""",
                (TaskStatus.FAILED.value, note, now, now,
                 task_id, expected_status.value),
            )
        else:
            cursor = self._conn.execute(
                """UPDATE tasks
                   SET status = ?, block_kind = NULL, note = ?,
                       completed_at = ?, updated_at = ?
                   WHERE id = ? AND status = ? AND block_kind = ?""",
                (TaskStatus.FAILED.value, note, now, now,
                 task_id, expected_status.value, expected_block_kind.value),
            )
        self._conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def try_manager_supersede(
        self,
        task_id: str,
        *,
        actor_agent: str,
        actor_session_id: str,
        expected_team: str,
        successor_brief: str,
        rationale: str,
        attestation: dict[str, object],
    ) -> str | None:
        """Atomically replace one eligible claimed root with a pending successor.

        This intentionally has no generic target/actor override: callers supply
        only the server-derived current claim and the two decision fields.  A
        false return has no write side effects; exceptions roll the entire
        operation back.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None or (
                row["status"] != TaskStatus.IN_PROGRESS.value
                or row["block_kind"] is not None
                or row["cancelled_at"] is not None
                or row["task_type"] != "task"
                or row["parent_task_id"] is not None
                or row["assigned_agent"] != actor_agent
                or row["team"] != expected_team
                or row["current_session_id"] != actor_session_id
                or row["active_chain"] is not None
                or row["active_fanout"] is not None
                or row["blocked_on_job_ids"] is not None
                or row["dispatched_from_thread_id"] not in (None, "")
            ):
                self._conn.rollback()
                return None
            live_work = self._conn.execute(
                """WITH RECURSIVE family(id) AS (
                       SELECT ?
                       UNION ALL
                       SELECT t.id FROM tasks t JOIN family f ON t.parent_task_id = f.id
                   )
                   SELECT 1 FROM tasks
                    WHERE id IN family AND id != ?
                      AND status NOT IN ('completed', 'failed', 'cancelled', 'superseded')
                   UNION ALL
                   SELECT 1 FROM jobs
                    WHERE task_id IN family AND status IN ('pending', 'running')
                   LIMIT 1""",
                (task_id, task_id),
            ).fetchone()
            if live_work is not None:
                self._conn.rollback()
                return None
            predecessor = row["id"]
            prior = self._conn.execute(
                "SELECT original_root_task_id FROM manager_supersessions WHERE successor_task_id = ?",
                (predecessor,),
            ).fetchone()
            original_root = prior["original_root_task_id"] if prior else predecessor
            if self._conn.execute(
                "SELECT 1 FROM manager_supersessions WHERE original_root_task_id = ? LIMIT 1",
                (original_root,),
            ).fetchone() is not None:
                self._conn.rollback()
                return None
            successor_id = self.next_task_id()
            now = datetime.now(timezone.utc).isoformat()
            predecessor_brief = row["brief"]
            predecessor_hash = hashlib.sha256(predecessor_brief.encode()).hexdigest()
            successor_hash = hashlib.sha256(successor_brief.encode()).hexdigest()
            attestation_evidence = {
                "rule_version": "manager_supersession_attestation.v1",
                "actor_agent": actor_agent,
                "actor_session_id": actor_session_id,
                "attestation": attestation,
            }
            self._conn.execute(
                """INSERT INTO tasks (id, status, assigned_agent, team, brief, task_type,
                       revision_count, created_at, updated_at, parent_task_id,
                       orchestration_step_count, session_timeout_seconds)
                   VALUES (?, 'pending', ?, ?, ?, 'task', 0, ?, ?, NULL, 0, ?)""",
                (successor_id, actor_agent, row["team"], successor_brief, now, now,
                 row["session_timeout_seconds"]),
            )
            self._conn.execute(
                """INSERT INTO manager_supersessions
                   (predecessor_task_id, successor_task_id, original_root_task_id,
                    actor_agent, actor_session_id, rationale, attestation_evidence, predecessor_brief,
                    successor_brief, predecessor_brief_sha256, successor_brief_sha256, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (predecessor, successor_id, original_root, actor_agent, actor_session_id,
                 rationale, json.dumps(attestation_evidence, sort_keys=True), predecessor_brief, successor_brief, predecessor_hash,
                 successor_hash, now),
            )
            self._conn.execute(
                """UPDATE tasks SET status = 'superseded', block_kind = NULL,
                       blocked_on_job_ids = NULL, active_chain = NULL, active_fanout = NULL,
                       note = ?, completed_at = ?, updated_at = ?
                   WHERE id = ? AND status = 'in_progress' AND block_kind IS NULL
                     AND current_session_id = ?""",
                (f"manager-superseded by {successor_id}", now, now, predecessor,
                 actor_session_id),
            )
            if self._conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeError("supersession claim became stale")
            payload = {
                "original_root_task_id": original_root,
                "actor_session_id": actor_session_id,
                "rationale": rationale,
                "attestation_evidence": attestation_evidence,
                "predecessor_brief_sha256": predecessor_hash,
                "successor_brief_sha256": successor_hash,
            }
            for audit_task_id, counterpart_task_id in (
                (predecessor, successor_id), (successor_id, predecessor),
            ):
                audit_payload = {**payload, "counterpart_task_id": counterpart_task_id}
                self._conn.execute(
                    "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) VALUES (?, ?, ?, ?, ?)",
                    (audit_task_id, actor_agent, "manager_supersession", json.dumps(audit_payload), now),
                )
            self._conn.commit()
            return successor_id
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def try_reject_thread_origin_manager_supersede(
        self,
        task_id: str,
        *,
        actor_agent: str,
        actor_session_id: str,
        expected_team: str,
        reason: str,
        reason_code: str,
    ) -> bool:
        """Atomically escalate one currently claimed, thread-origin manager root.

        This is the ineligible counterpart to ``try_manager_supersede``.  Its
        complete claim predicate is evaluated under the same transaction as
        the state and audit writes, so a competing continuation, block, or
        cancellation wins without any rejection side effects.
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                """UPDATE tasks
                   SET status = ?, block_kind = NULL, note = ?, updated_at = ?
                   WHERE id = ?
                     AND status = 'in_progress'
                     AND block_kind IS NULL
                     AND cancelled_at IS NULL
                     AND task_type = 'task'
                     AND parent_task_id IS NULL
                     AND assigned_agent = ?
                     AND team = ?
                     AND current_session_id = ?
                     AND active_chain IS NULL
                     AND active_fanout IS NULL
                     AND blocked_on_job_ids IS NULL
                     AND dispatched_from_thread_id IS NOT NULL
                     AND dispatched_from_thread_id != ''""",
                (
                    TaskStatus.ESCALATED.value, reason, now, task_id,
                    actor_agent, expected_team, actor_session_id,
                ),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return False
            escalation_audit_id = self.insert_audit_log_uncommitted(
                task_id=task_id,
                agent=actor_agent,
                action="escalation",
                payload={"reason": reason},
            )
            self.insert_audit_log_uncommitted(
                task_id=task_id,
                agent=actor_agent,
                action="authority_hook",
                payload={
                    "outcome": "not_applicable",
                    "reason_code": reason_code,
                    "reason": "runtime-raised escalation is not an authority decision",
                    "causal_escalation_audit_id": escalation_audit_id,
                },
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def try_delegate(
        self, parent_id: str, child: TaskRecord, *, parent_note: str,
        attachments: list[dict] | None = None,
        active_chain_json: str | None = None,
        uploaded_by: str = "orchestrator",
    ) -> bool:
        """Atomic CAS: insert child task + transition parent to
        IN_PROGRESS(DELEGATED) (Path B: a parent waiting on its own children is
        in progress, with the waiting reason kept in block_kind), rejecting if
        parent is cancelled or already terminal.

        Closes the spawn-new-work race documented in
        docs/superpowers/specs/2026-05-26-cancel-race-design.md §5.3.
        Atomicity guarantee: both the child INSERT and the parent UPDATE
        happen under a single @_synchronized acquisition (threading.RLock,
        reentrant). The cancel route's update_task also acquires this lock,
        so the only two interleavings are:
        - cancel before us: our SELECT sees cancelled_at != NULL → bail, no writes
        - us before cancel: cancel sees parent in in_progress(delegated), transitions
          to FAILED, and its cascade walks our newly-inserted child for cleanup

        When ``attachments`` is provided, attachment links + audit rows
        are inserted in the same transaction as the child task. A duplicate
        storage_key raises sqlite3.IntegrityError (rolled back by caller).

        When ``active_chain_json`` is provided, it is written to the parent's
        active_chain column in the same transaction as the child insert +
        parent status update. A crash or write failure rolls back everything
        — no orphan chain state, no orphan child, no broken parent state.

        On True: parent has transitioned and child exists.
        On False: no DB changes were made (no orphan child, no parent overwrite).
        """
        cursor = self._conn.execute(
            "SELECT status, cancelled_at FROM tasks WHERE id = ?", (parent_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return False
        if row["cancelled_at"] is not None or row["status"] in (
            "completed", "failed", "superseded",
        ):
            return False
        # Single transaction: child insert + attachment links/audit + parent update.
        try:
            now_ts = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                """INSERT INTO tasks (id, status, assigned_agent, team, brief,
                   revision_count, created_at, updated_at, completed_at, parent_task_id,
                   revisit_of_task_id, dispatched_from_thread_id,
                   block_kind, note,
                   orchestration_step_count, session_timeout_seconds, task_type, active_fanout,
                   current_session_id, zombie_flagged_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    child.id,
                    child.status.value,
                    child.assigned_agent,
                    child.team,
                    child.brief,
                    child.revision_count,
                    child.created_at.isoformat(),
                    child.updated_at.isoformat(),
                    child.completed_at.isoformat() if child.completed_at else None,
                    child.parent_task_id,
                    child.revisit_of_task_id,
                    child.dispatched_from_thread_id,
                    child.block_kind.value if child.block_kind else None,
                    child.note,
                    child.orchestration_step_count,
                    child.session_timeout_seconds,
                    child.task_type,
                    child.active_fanout,
                    child.current_session_id,
                    child.zombie_flagged_at.isoformat() if child.zombie_flagged_at else None,
                ),
            )
            if attachments:
                self._insert_task_attachments_txn(
                    child.id, attachments, uploaded_by,
                )
            # Write active_chain within the same transaction so a crash or
            # write failure rolls back child + parent + chain atomically.
            if active_chain_json is not None:
                self._conn.execute(
                    "UPDATE tasks SET active_chain = ? WHERE id = ?",
                    (active_chain_json, parent_id),
                )
            now = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                """UPDATE tasks
                   SET status = ?, block_kind = ?, note = ?, updated_at = ?
                   WHERE id = ?""",
                (TaskStatus.IN_PROGRESS.value, BlockKind.DELEGATED.value, parent_note,
                 now, parent_id),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def try_advance_chain(
        self,
        parent_id: str,
        active_chain_json: str,
        next_child: "TaskRecord",
        *,
        attachments: list[dict] | None = None,
        uploaded_by: str = "orchestrator",
    ) -> bool:
        """Atomically update parent active_chain + insert next child +
        attachment links/audit in a single explicit transaction.

        Replaces the prior two-step pattern (update_task_active_chain +
        insert_task_with_attachments) with a single transaction so a crash
        or child/link/audit write failure rolls back the chain advance.

        Returns True on success (parent chain advanced, child exists,
        attachments linked). Returns False and rolls back on any failure.
        """
        now_ts = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "UPDATE tasks SET active_chain = ? WHERE id = ?",
                (active_chain_json, parent_id),
            )
            self._conn.execute(
                """INSERT INTO tasks (id, status, assigned_agent, team, brief,
                   revision_count, created_at, updated_at, completed_at, parent_task_id,
                   revisit_of_task_id, dispatched_from_thread_id,
                   block_kind, note,
                   orchestration_step_count, session_timeout_seconds, task_type, active_fanout,
                   current_session_id, zombie_flagged_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    next_child.id,
                    next_child.status.value,
                    next_child.assigned_agent,
                    next_child.team,
                    next_child.brief,
                    next_child.revision_count,
                    next_child.created_at.isoformat(),
                    next_child.updated_at.isoformat(),
                    next_child.completed_at.isoformat() if next_child.completed_at else None,
                    next_child.parent_task_id,
                    next_child.revisit_of_task_id,
                    next_child.dispatched_from_thread_id,
                    next_child.block_kind.value if next_child.block_kind else None,
                    next_child.note,
                    next_child.orchestration_step_count,
                    next_child.session_timeout_seconds,
                    next_child.task_type,
                    next_child.active_fanout,
                    next_child.current_session_id,
                    next_child.zombie_flagged_at.isoformat() if next_child.zombie_flagged_at else None,
                ),
            )
            if attachments:
                self._insert_task_attachments_txn(
                    next_child.id, attachments, uploaded_by,
                )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            return False

    @_synchronized
    def increment_revision_count(self, task_id: str) -> None:
        self._conn.execute(
            "UPDATE tasks SET revision_count = revision_count + 1, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), task_id),
        )
        self._conn.commit()

    @_synchronized
    def next_task_id(self) -> str:
        # MAX(numeric_suffix) over TASK-NNN-shaped rows. Robust to gaps and
        # foreign-shape rows that a COUNT(*)-based allocator would mis-count
        # and then collide with on the next insert.
        cursor = self._conn.execute(
            "SELECT MAX(CAST(SUBSTR(id, 6) AS INTEGER)) AS m "
            "FROM tasks WHERE id GLOB 'TASK-[0-9]*'"
        )
        n = (cursor.fetchone()["m"] or 0) + 1
        return f"TASK-{n:03d}"

    @_synchronized
    def get_nonterminal_task_ids(self) -> list[str]:
        # Path B: blocked dropped (no live row is `blocked` after the boot
        # migration); escalated added so the restart sweep visits escalated
        # rows to leave them alone (§B Branch 5). cancelled is terminal →
        # excluded.
        nonterminal = (
            TaskStatus.PENDING.value,
            TaskStatus.IN_PROGRESS.value,
            TaskStatus.ESCALATED.value,
        )
        cursor = self._conn.execute(
            f"SELECT id FROM tasks WHERE status IN ({','.join('?' * len(nonterminal))})",
            nonterminal,
        )
        return [row["id"] for row in cursor.fetchall()]

    @_synchronized
    def list_blocked_with_kind(self, kind) -> list[str]:
        """Return IDs of parked tasks with the given block_kind.

        Queries by in_progress + block_kind — the stored Path-B representation.
        """
        kind_value = kind.value if hasattr(kind, "value") else kind
        cursor = self._conn.execute(
            "SELECT id FROM tasks "
            "WHERE status = 'in_progress' AND block_kind = ?",
            (kind_value,),
        )
        return [row["id"] for row in cursor.fetchall()]

    @_synchronized
    def list_tasks_blocked_on_jobs(self) -> list[str]:
        """Return ids of tasks currently parked waiting on jobs (BLOCKED_ON_JOB).

        Used by startup recovery (spec §5.7) to re-evaluate the predicate after
        `recover_orphaned_running_jobs` force-fails any leftovers.
        """
        rows = self._conn.execute(
            "SELECT id FROM tasks "
            "WHERE status = ? AND block_kind = ?",
            (TaskStatus.IN_PROGRESS.value, BlockKind.BLOCKED_ON_JOB.value),
        ).fetchall()
        return [row["id"] for row in rows]

    # --- Audit Log ---

    @_synchronized
    def insert_audit_log(
        self,
        task_id: str,
        agent: str,
        action: str,
        payload: dict | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) VALUES (?, ?, ?, ?, ?)",
            (
                task_id,
                agent,
                action,
                json.dumps(payload) if payload else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    @_synchronized
    def insert_audit_log_uncommitted(
        self,
        task_id: str,
        agent: str,
        action: str,
        payload: dict | None = None,
    ) -> int:
        """Insert an audit row WITHOUT committing the transaction.

        The caller must call ``commit()`` to make the row durable.
        If the connection is closed without a commit (or an exception
        propagates through a context manager that closes the handle),
        the row is rolled back — no audit residue.

        This exists so that compound operations (e.g. adapter-profile
        binding) can batch the durable profile write, in-memory registry
        update, and audit write in a single logical transaction with a
        clean rollback path.  The standard ``insert_audit_log`` (which
        commits inline) is retained for all existing callers.
        """
        cur = self._conn.execute(
            "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) VALUES (?, ?, ?, ?, ?)",
            (
                task_id,
                agent,
                action,
                json.dumps(payload) if payload else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid

    @_synchronized
    def commit(self) -> None:
        """Commit the current transaction — public companion to insert_audit_log_uncommitted."""
        self._conn.commit()

    def _emit_reply_wake_audit(
        self,
        *,
        thread_id: str,
        agent_name: str,
        action: str,
        payload: dict,
    ) -> None:
        """Emit one reply-delivery lifecycle audit row INSIDE the open store
        transaction (caller commits). GH-688 Phase 1 Slice C.

        The six approved actions — thread_reply_wake_created / _coalesced /
        _claimed / _settled / _cancelled / _recovered — are written at the
        exact store transitions that already know the durable outcome, so
        duplicate queue notifications (stale claim CAS no-ops) and idempotent
        recovery can never fabricate false events. The existing
        ``audit_log.task_id = THR-*`` scope-prefix convention is unchanged;
        ``agent`` is the wake owner so /audit?agent= filters naturally.
        Payloads carry only truthfully observed fields (agent, inclusive
        range, 8-char token prefix, outcome/reason/follow-on result) and
        never expose full single-use invocation tokens.
        """
        self.insert_audit_log_uncommitted(
            task_id=thread_id,
            agent=agent_name,
            action=action,
            payload=payload,
        )

    @_synchronized
    def rollback(self) -> None:
        """Roll back the current transaction — companion to insert_audit_log_uncommitted."""
        self._conn.rollback()

    @_synchronized
    def get_audit_logs(self, task_id: str) -> list[dict]:
        cursor = self._conn.execute(
            "SELECT * FROM audit_log WHERE task_id = ? ORDER BY id", (task_id,)
        )
        rows = cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("payload"):
                d["payload"] = json.loads(d["payload"])
            result.append(d)
        return result

    # --- Org Settings (THR-095) ---

    @_synchronized
    def upsert_org_setting(
        self,
        section: str,
        value_json: str,
        *,
        before: dict | None = None,
        after: dict | None = None,
        actor: str = "founder",
    ) -> None:
        """Upsert an org_settings row AND insert its config:<section> audit
        row in one atomic transaction (same connection, single commit).

        A crash/failure before commit rolls BOTH back — no split-brain."""
        now = datetime.now(timezone.utc).isoformat()
        # F4 fix: emit only the actually-changed tiers, not the full before dict.
        if isinstance(before, dict) and isinstance(after, dict):
            _tiers = sorted(
                k for k in set(before) | set(after)
                if before.get(k) != after.get(k)
            )
        elif before is not None:
            _tiers = list(before) if isinstance(before, dict) else [section]
        else:
            _tiers = [section]
        audit_payload = json.dumps({
            "section": section,
            "tiers": _tiers,
            "before": before or {},
            "after": after or {},
        })
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                "INSERT INTO org_settings (section, value_json, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(section) DO UPDATE SET "
                "value_json = excluded.value_json, "
                "updated_at = excluded.updated_at, "
                "updated_by = excluded.updated_by",
                (section, value_json, now, actor),
            )
            self._conn.execute(
                "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) "
                "VALUES (?, ?, 'org_config_write', ?, ?)",
                (f"config:{section}", actor, audit_payload, now),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def get_org_setting(self, section: str) -> str | None:
        """Return the value_json for *section* or None if no row exists."""
        row = self._conn.execute(
            "SELECT value_json FROM org_settings WHERE section = ?",
            (section,),
        ).fetchone()
        return row["value_json"] if row else None

    @_synchronized
    def get_all_org_settings(self) -> dict[str, str]:
        """Return {section: value_json} for every row in org_settings."""
        rows = self._conn.execute(
            "SELECT section, value_json FROM org_settings"
        ).fetchall()
        return {row["section"]: row["value_json"] for row in rows}

    @_synchronized
    def fetch_one_readonly(
        self, sql: str, params: tuple = ()
    ) -> "sqlite3.Row | None":
        """Run a read-only SELECT and return the first row or None.

        For use by modules outside ``Database`` (e.g. ``dashboard_summary``)
        that need to issue read aggregations without bypassing ``_lock``.
        Holds the same ``RLock`` as every other public Database method.
        """
        return self._conn.execute(sql, params).fetchone()

    @_synchronized
    def fetch_all_readonly(
        self, sql: str, params: tuple = ()
    ) -> "list[sqlite3.Row]":
        """Run a read-only SELECT and return all rows.

        See ``fetch_one_readonly`` for the threading rationale.
        """
        return self._conn.execute(sql, params).fetchall()

    @_synchronized
    def get_audit_logs_by_action(self, action: str, since: str | None = None) -> list[dict]:
        """Get audit logs filtered by action, optionally since a date."""
        if since:
            cursor = self._conn.execute(
                "SELECT * FROM audit_log WHERE action = ? AND timestamp >= ? ORDER BY id",
                (action, since),
            )
        else:
            cursor = self._conn.execute(
                "SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,)
            )
        rows = cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("payload"):
                d["payload"] = json.loads(d["payload"])
            result.append(d)
        return result

    @_synchronized
    def query_audit_logs(
        self,
        task_id: str | None = None,
        agent: str | None = None,
        action: str | None = None,
        since: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """Filtered audit-log query used by the /audit route.

        All filters are optional and AND-composed. ``limit`` returns the most
        recent N rows (ORDER BY timestamp DESC, id DESC) but the result is
        re-sorted ascending so callers still see chronological order.

        Supports KEYSET cursor pagination: pass the ``cursor`` returned by a
        prior call to get the next older page.  The cursor is an opaque string
        encoding the (timestamp, id) of the last row in the prior page.
        ``next_cursor`` is ``None`` exactly when the result set is exhausted.
        """
        import base64

        clauses: list[str] = []
        params: list[object] = []

        # Decode cursor into a keyset filter (rows BEFORE the cursor anchor)
        if cursor is not None:
            cursor_ts, cursor_id = _decode_cursor(cursor)
            clauses.append(
                "(timestamp < ? OR (timestamp = ? AND id < ?))"
            )
            params.extend([cursor_ts, cursor_ts, cursor_id])

        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if agent is not None:
            clauses.append("agent = ?")
            params.append(agent)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        # Defensive guard: non-positive limit short-circuits to empty result.
        # Without this, limit=0 returns next_cursor anchored to a row that was
        # never returned, and limit<0 produces an IndexError on result[limit-1].
        if limit is not None and limit <= 0:
            return [], None

        if limit is not None:
            # Fetch limit+1 to detect whether another page exists
            sql = (
                f"SELECT * FROM audit_log {where} "
                f"ORDER BY timestamp DESC, id DESC LIMIT ?"
            )
            params.append(limit + 1)
        else:
            sql = f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC, id DESC"

        db_cursor = self._conn.execute(sql, tuple(params))
        rows = db_cursor.fetchall()

        result: list[dict] = []
        for row in rows:
            d = dict(row)
            if d.get("payload"):
                d["payload"] = json.loads(d["payload"])
            result.append(d)

        next_cursor: str | None = None
        if limit is not None and len(result) > limit:
            # The extra row tells us there is a next page.
            # Encode the (timestamp, id) of the last actual-page row as next_cursor.
            last_of_page = result[limit - 1]
            next_cursor = _encode_cursor(
                last_of_page["timestamp"], last_of_page["id"]
            )
            # Trim to exactly the requested page size
            result = result[:limit]

        # Re-sort ascending so callers see chronological (oldest-first) order.
        result.sort(key=lambda d: d["id"])

        return result, next_cursor

    def get_audit_logs_for_agent_since(
        self, agent: str, since: str, *, limit: int = 200,
    ) -> list[dict]:
        """Audit rows authored by ``agent`` with ``timestamp >= since`` (ISO),
        capped to the most recent ``limit`` in chronological order.

        Window-scoped accessor for the dream input window (spec "Input Window":
        "audit rows involving the agent since window_start"). Distinct from
        ``get_audit_logs(task_id)``, which is keyed on the scope-id column.
        Delegates to ``query_audit_logs`` to avoid duplicating the filter SQL.
        """
        entries, _ = self.query_audit_logs(agent=agent, since=since, limit=limit)
        return entries

    # --- Task Results ---

    @_synchronized
    def insert_task_result(
        self,
        task_id: str,
        agent: str,
        session_id: str,
        output_summary: str,
        confidence_score: int,
        status: str = "completed",
        risks_flagged: list[str] | None = None,
        learnings: str | None = None,
        duration_seconds: int | None = None,
        token_count: int | None = None,
        estimated_cost: float | None = None,
        output_dir: str | None = None,
        decision_json: str | None = None,
        waiting_on_job_ids: list[str] | None = None,
        verdict: str | None = None,
        local_ci_json: str | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO task_results
               (task_id, agent, session_id, status, output_summary, decision_json,
                confidence_score, learnings, risks_flagged, duration_seconds,
                token_count, estimated_cost, output_dir, waiting_on_job_ids,
                verdict, local_ci, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                agent,
                session_id,
                status,
                output_summary,
                decision_json,
                confidence_score,
                learnings,
                json.dumps(risks_flagged) if risks_flagged is not None else None,
                duration_seconds,
                token_count,
                estimated_cost,
                output_dir,
                json.dumps(waiting_on_job_ids) if waiting_on_job_ids is not None else None,
                verdict,
                local_ci_json,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    @_synchronized
    def get_task_results(self, task_id: str) -> list[dict]:
        cursor = self._conn.execute(
            "SELECT * FROM task_results WHERE task_id = ? ORDER BY id", (task_id,)
        )
        rows = cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("risks_flagged"):
                d["risks_flagged"] = json.loads(d["risks_flagged"])
            if d.get("waiting_on_job_ids"):
                d["waiting_on_job_ids"] = json.loads(d["waiting_on_job_ids"])
            result.append(d)
        return result

    @_synchronized
    def get_agent_task_results(self, agent: str, since: str | None = None) -> list[dict]:
        if since:
            cursor = self._conn.execute(
                "SELECT * FROM task_results WHERE agent = ? AND created_at >= ? ORDER BY id",
                (agent, since),
            )
        else:
            cursor = self._conn.execute(
                "SELECT * FROM task_results WHERE agent = ? ORDER BY id", (agent,)
            )
        rows = cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("risks_flagged"):
                d["risks_flagged"] = json.loads(d["risks_flagged"])
            if d.get("waiting_on_job_ids"):
                d["waiting_on_job_ids"] = json.loads(d["waiting_on_job_ids"])
            result.append(d)
        return result

    @_synchronized
    def get_latest_task_result(
        self, task_id: str, agent: str, session_id: str,
    ) -> dict | None:
        cursor = self._conn.execute(
            """SELECT * FROM task_results
               WHERE task_id = ? AND agent = ? AND session_id = ?
               ORDER BY id DESC LIMIT 1""",
            (task_id, agent, session_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("risks_flagged"):
            d["risks_flagged"] = json.loads(d["risks_flagged"])
        if d.get("waiting_on_job_ids"):
            d["waiting_on_job_ids"] = json.loads(d["waiting_on_job_ids"])
        return d

    @_synchronized
    def get_latest_completion_report(
        self, task_id: str, agent: str | None = None, session_id: str | None = None,
    ):
        """Return the most-recent task_results row for the given task as a
        CompletionReport, or None if no row exists.

        Used by the chain-advance logic in run_step to read the just-completed
        child's verdict without requiring the caller to know agent/session_id.

        THR-211: when ``agent`` AND ``session_id`` are both provided the lookup
        is scoped to the exact ``(task_id, agent, session_id)`` fingerprint
        (the same authority the boot sweep / zombie reaper use) so a newer
        unrelated row can never substitute for the authenticated report.
        Without the scope the most-recent row is returned (legacy behavior).

        THR-211 (TASK-5823): for the exact-fingerprint scope, a row whose
        persisted structured fields fail deserialization/structural
        validation (invalid JSON in ``risks_flagged`` / ``waiting_on_job_ids``,
        or values failing the strict ``CompletionReport`` contract) has NO
        acceptable authenticated report: it returns ``None`` so the caller's
        existing fail-closed path applies (chain cleared, parent woken once,
        task-wide evidence never consulted).  Only ``json.JSONDecodeError`` and
        ``pydantic.ValidationError`` are converted; SQLite, transaction, I/O,
        programming, and unrelated operational exceptions still propagate.
        The unscoped (legacy) read keeps its prior behavior.
        """
        from pydantic import ValidationError

        if agent is not None and session_id is not None:
            row = self._conn.execute(
                "SELECT * FROM task_results WHERE task_id = ? "
                "AND agent = ? AND session_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (task_id, agent, session_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM task_results WHERE task_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return self._row_to_completion_report(task_id, row)
        except (json.JSONDecodeError, ValidationError):
            if agent is None or session_id is None:
                # Unscoped (legacy) read: preserve prior behavior — a
                # structurally malformed newest row still surfaces as an
                # error rather than silently degrading the fallback.
                raise
            # Exact modern fingerprint: no acceptable authenticated report.
            return None

    def _row_to_completion_report(self, task_id: str, row) -> "CompletionReport":
        """Build a CompletionReport from a task_results row dict.

        ``risks_flagged`` / ``waiting_on_job_ids`` are persisted as JSON text
        and re-deserialized here; malformed JSON or a value failing the strict
        ``CompletionReport`` contract raises ``json.JSONDecodeError`` /
        ``pydantic.ValidationError``, which the exact-scope caller converts to
        the no-acceptable-report fail-closed outcome.  ``local_ci`` degrades
        to None (documented behavior).
        """
        from runtime.models import CompletionReport, LocalCiEvidence

        keys = row.keys()
        # Safely parse local_ci from the task_results row.
        # A missing legacy column, NULL, empty/malformed JSON, wrong shape,
        # or JSON failing the strict LocalCiEvidence contract → None.
        _local_ci_raw = row["local_ci"] if "local_ci" in keys else None
        _local_ci: LocalCiEvidence | None = None
        if _local_ci_raw:
            try:
                _parsed = json.loads(_local_ci_raw)
                if isinstance(_parsed, dict):
                    _local_ci = LocalCiEvidence(**_parsed)
            except Exception:
                pass
        manager_self_evaluation = None
        raw_decision = row["decision_json"] if "decision_json" in keys else None
        if raw_decision:
            parsed_decision = json.loads(raw_decision)
            if isinstance(parsed_decision, dict):
                manager_self_evaluation = parsed_decision.get(
                    "_manager_self_evaluation"
                )
        return CompletionReport(
            task_id=task_id,
            agent=row["agent"],
            status=row["status"] or "completed",
            confidence=row["confidence_score"] or 0,
            output_summary=row["output_summary"] or "",
            verdict=row["verdict"] if "verdict" in keys else None,
            manager_self_evaluation=manager_self_evaluation,
            output_dir=row["output_dir"] if "output_dir" in keys else None,
            risks_flagged=(
                json.loads(row["risks_flagged"])
                if row["risks_flagged"]
                else []
            ),
            waiting_on_job_ids=(
                json.loads(row["waiting_on_job_ids"])
                if "waiting_on_job_ids" in keys and row["waiting_on_job_ids"]
                else []
            ),
            local_ci=_local_ci,
        )

    # --- Session Token Usage ---

    @_synchronized
    def insert_session_token_usage(
        self,
        task_id: str | None,
        agent: str,
        session_id: str,
        executor: str,
        token_usage: TokenUsage,
        scope_type: str = "task",
        scope_id: str | None = None,
        thread_id: str | None = None,
        invocation_purpose: str | None = None,
    ) -> None:
        """Insert one token usage row. INSERT OR IGNORE: first write wins."""
        if scope_id is None and scope_type == "task":
            scope_id = task_id
        self._conn.execute(
            """INSERT OR IGNORE INTO session_token_usage
               (task_id, agent, session_id, executor, model,
                input_tokens, output_tokens, cache_read_tokens,
                cache_creation_tokens, reasoning_tokens,
                usage_raw_json, scope_type, scope_id, thread_id,
                invocation_purpose, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, agent, session_id, executor, token_usage.model,
                token_usage.input_tokens, token_usage.output_tokens,
                token_usage.cache_read_tokens, token_usage.cache_creation_tokens,
                token_usage.reasoning_tokens, token_usage.usage_raw_json,
                scope_type, scope_id, thread_id, invocation_purpose,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def _session_token_usage_filters(
        self,
        *,
        since: str | None = None,
        task_id: str | None = None,
        agent: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        thread_id: str | None = None,
        purpose: str | None = None,
    ) -> tuple[list[str], list[object]]:
        where: list[str] = []
        params: list[object] = []
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)
        if task_id is not None:
            where.append("task_id = ?")
            params.append(task_id)
        if agent is not None:
            where.append("agent = ?")
            params.append(agent)
        if scope_type is not None:
            where.append("COALESCE(scope_type, 'task') = ?")
            params.append(scope_type)
        if scope_id is not None:
            where.append("COALESCE(scope_id, task_id) = ?")
            params.append(scope_id)
        if thread_id is not None:
            where.append("thread_id = ?")
            params.append(thread_id)
        if purpose is not None:
            where.append("invocation_purpose = ?")
            params.append(purpose)
        return where, params

    @staticmethod
    def _token_usage_rollup_select(
        group_expr: str,
        group_alias: str,
        *,
        include_model_classification: bool = False,
    ) -> str:
        # Cutover-INDEPENDENT primitives a renderer applies the model-name
        # precedence over (the MODEL_FIX_CUTOVER_TS comparison itself is a
        # presentation concern, never in SQL). total_tokens is unaffected.
        model_cols = ""
        if include_model_classification:
            model_cols = """,
                         COUNT(DISTINCT model) AS model_distinct,
                         MAX(model) AS model_any,
                         SUM(CASE WHEN model IS NOT NULL THEN 1 ELSE 0 END) AS non_null_sessions,
                         SUM(CASE WHEN model IS NULL AND executor = 'codex' THEN 1 ELSE 0 END) AS null_codex_sessions,
                         SUM(CASE WHEN model IS NULL AND executor = 'claude' THEN 1 ELSE 0 END) AS null_claude_sessions,
                         MIN(CASE WHEN model IS NULL AND executor = 'claude' THEN created_at END) AS null_claude_min_created_at,
                         MAX(CASE WHEN model IS NULL AND executor = 'claude' THEN created_at END) AS null_claude_max_created_at"""
        return f"""SELECT {group_expr} AS {group_alias},
                         COUNT(*) AS sessions,
                         COALESCE(SUM(input_tokens), 0)          AS input_tokens,
                         COALESCE(SUM(output_tokens), 0)         AS output_tokens,
                         COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens,
                         COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                         COALESCE(SUM(reasoning_tokens), 0)      AS reasoning_tokens,
                         COALESCE(SUM(input_tokens), 0)
                           + COALESCE(SUM(output_tokens), 0)
                           + COALESCE(SUM(reasoning_tokens), 0)  AS total_tokens,
                         COALESCE(SUM(input_tokens), 0)
                           + COALESCE(SUM(output_tokens), 0)
                           + COALESCE(SUM(reasoning_tokens), 0)  AS churn_tokens,
                         COALESCE(SUM(input_tokens), 0)
                           + COALESCE(SUM(output_tokens), 0)
                           + COALESCE(SUM(reasoning_tokens), 0)
                           + COALESCE(SUM(cache_read_tokens), 0)
                           + COALESCE(SUM(cache_creation_tokens), 0)  AS context_tokens{model_cols}
                  FROM session_token_usage"""

    @_synchronized
    def list_session_token_usage(
        self,
        task_id: str | None = None,
        agent: str | None = None,
        since: str | None = None,
        limit: int | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        thread_id: str | None = None,
        purpose: str | None = None,
    ) -> list[dict]:
        """Return per-session rows, newest first."""
        where, params = self._session_token_usage_filters(
            since=since,
            task_id=task_id,
            agent=agent,
            scope_type=scope_type,
            scope_id=scope_id,
            thread_id=thread_id,
            purpose=purpose,
        )
        sql = """SELECT *,
                        COALESCE(scope_type, 'task') AS scope_type,
                        COALESCE(scope_id, task_id) AS scope_id,
                        COALESCE(input_tokens, 0)
                          + COALESCE(output_tokens, 0)
                          + COALESCE(reasoning_tokens, 0) AS total_tokens,
                        COALESCE(input_tokens, 0)
                          + COALESCE(output_tokens, 0)
                          + COALESCE(reasoning_tokens, 0) AS churn_tokens,
                        COALESCE(input_tokens, 0)
                          + COALESCE(output_tokens, 0)
                          + COALESCE(reasoning_tokens, 0)
                          + COALESCE(cache_read_tokens, 0)
                          + COALESCE(cache_creation_tokens, 0) AS context_tokens
                 FROM session_token_usage"""
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def aggregate_session_token_usage_by_agent(
        self,
        since: str | None = None,
        task_id: str | None = None,
        agent: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        thread_id: str | None = None,
        purpose: str | None = None,
    ) -> list[dict]:
        where, params = self._session_token_usage_filters(
            since=since,
            task_id=task_id,
            agent=agent,
            scope_type=scope_type,
            scope_id=scope_id,
            thread_id=thread_id,
            purpose=purpose,
        )
        sql = self._token_usage_rollup_select(
            "agent", "agent", include_model_classification=True
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY agent ORDER BY agent"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def aggregate_session_token_usage_by_task(
        self,
        since: str | None = None,
        agent: str | None = None,
        task_id: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        thread_id: str | None = None,
        purpose: str | None = None,
    ) -> list[dict]:
        where, params = self._session_token_usage_filters(
            since=since,
            task_id=task_id,
            agent=agent,
            scope_type=scope_type,
            scope_id=scope_id,
            thread_id=thread_id,
            purpose=purpose,
        )
        sql = self._token_usage_rollup_select("task_id", "task_id")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY task_id ORDER BY task_id"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def aggregate_session_token_usage_by_failed_task(
        self,
        since: str | None = None,
        agent: str | None = None,
        task_id: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        thread_id: str | None = None,
        purpose: str | None = None,
    ) -> list[dict]:
        """Per-(task, agent) token rollup for FAILED tasks only.

        Read-only INNER JOIN of ``session_token_usage`` to ``tasks`` on the
        canonical ``task_id`` (= ``tasks.id``), keeping only usage tied to a
        task in the terminal ``failed`` status. Caller filters AND-compose via
        the shared filter helper, applied inside the subquery so the JOIN
        cannot collide on ``created_at`` (a column both tables carry).
        """
        where, params = self._session_token_usage_filters(
            since=since,
            task_id=task_id,
            agent=agent,
            scope_type=scope_type,
            scope_id=scope_id,
            thread_id=thread_id,
            purpose=purpose,
        )
        subquery = "SELECT * FROM session_token_usage"
        if where:
            subquery += " WHERE " + " AND ".join(where)
        sql = f"""SELECT s.task_id AS task_id,
                         s.agent AS agent,
                         COUNT(*) AS sessions,
                         COALESCE(SUM(s.input_tokens), 0)          AS input_tokens,
                         COALESCE(SUM(s.output_tokens), 0)         AS output_tokens,
                         COALESCE(SUM(s.cache_read_tokens), 0)     AS cache_read_tokens,
                         COALESCE(SUM(s.cache_creation_tokens), 0) AS cache_creation_tokens,
                         COALESCE(SUM(s.reasoning_tokens), 0)      AS reasoning_tokens,
                         COALESCE(SUM(s.input_tokens), 0)
                           + COALESCE(SUM(s.output_tokens), 0)
                           + COALESCE(SUM(s.reasoning_tokens), 0)  AS total_tokens,
                         COALESCE(SUM(s.input_tokens), 0)
                           + COALESCE(SUM(s.output_tokens), 0)
                           + COALESCE(SUM(s.reasoning_tokens), 0)  AS churn_tokens,
                         COALESCE(SUM(s.input_tokens), 0)
                           + COALESCE(SUM(s.output_tokens), 0)
                           + COALESCE(SUM(s.reasoning_tokens), 0)
                           + COALESCE(SUM(s.cache_read_tokens), 0)
                           + COALESCE(SUM(s.cache_creation_tokens), 0)  AS context_tokens
                  FROM ({subquery}) s
                  JOIN tasks t ON t.id = s.task_id
                  WHERE t.status = ?
                  GROUP BY s.task_id, s.agent
                  ORDER BY s.task_id, s.agent"""
        params.append(TaskStatus.FAILED.value)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def aggregate_session_token_usage_by_scope(
        self,
        since: str | None = None,
        task_id: str | None = None,
        agent: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        thread_id: str | None = None,
        purpose: str | None = None,
    ) -> list[dict]:
        where, params = self._session_token_usage_filters(
            since=since,
            task_id=task_id,
            agent=agent,
            scope_type=scope_type,
            scope_id=scope_id,
            thread_id=thread_id,
            purpose=purpose,
        )
        sql = """SELECT COALESCE(scope_type, 'task') AS scope_type,
                        COALESCE(scope_id, task_id) AS scope_id,
                        COUNT(*) AS sessions,
                        COALESCE(SUM(input_tokens), 0)          AS input_tokens,
                        COALESCE(SUM(output_tokens), 0)         AS output_tokens,
                        COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens,
                        COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                        COALESCE(SUM(reasoning_tokens), 0)      AS reasoning_tokens,
                        COALESCE(SUM(input_tokens), 0)
                          + COALESCE(SUM(output_tokens), 0)
                          + COALESCE(SUM(reasoning_tokens), 0)  AS total_tokens,
                        COALESCE(SUM(input_tokens), 0)
                          + COALESCE(SUM(output_tokens), 0)
                          + COALESCE(SUM(reasoning_tokens), 0)  AS churn_tokens,
                        COALESCE(SUM(input_tokens), 0)
                          + COALESCE(SUM(output_tokens), 0)
                          + COALESCE(SUM(reasoning_tokens), 0)
                          + COALESCE(SUM(cache_read_tokens), 0)
                          + COALESCE(SUM(cache_creation_tokens), 0)  AS context_tokens
                 FROM session_token_usage"""
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY COALESCE(scope_type, 'task'), COALESCE(scope_id, task_id)"
        sql += " ORDER BY scope_type, scope_id"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def aggregate_session_token_usage_by_thread(
        self,
        since: str | None = None,
        task_id: str | None = None,
        agent: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        thread_id: str | None = None,
        purpose: str | None = None,
    ) -> list[dict]:
        where, params = self._session_token_usage_filters(
            since=since,
            task_id=task_id,
            agent=agent,
            scope_type=scope_type,
            scope_id=scope_id,
            thread_id=thread_id,
            purpose=purpose,
        )
        where.append("thread_id IS NOT NULL")
        sql = self._token_usage_rollup_select(
            "thread_id", "thread_id", include_model_classification=True
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY thread_id ORDER BY thread_id"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def aggregate_session_token_usage_by_purpose(
        self,
        since: str | None = None,
        task_id: str | None = None,
        agent: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        thread_id: str | None = None,
        purpose: str | None = None,
    ) -> list[dict]:
        where, params = self._session_token_usage_filters(
            since=since,
            task_id=task_id,
            agent=agent,
            scope_type=scope_type,
            scope_id=scope_id,
            thread_id=thread_id,
            purpose=purpose,
        )
        where.append("invocation_purpose IS NOT NULL")
        sql = self._token_usage_rollup_select("invocation_purpose", "purpose")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY invocation_purpose ORDER BY invocation_purpose"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def aggregate_session_token_usage_by_model(
        self,
        since: str | None = None,
        task_id: str | None = None,
        agent: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        thread_id: str | None = None,
        purpose: str | None = None,
    ) -> list[dict]:
        """Roll up session_token_usage grouped by model.

        NULL models are honest (not blank, not a guessed correction).
        The ``since`` window AND-composes with every other filter.
        """
        where, params = self._session_token_usage_filters(
            since=since,
            task_id=task_id,
            agent=agent,
            scope_type=scope_type,
            scope_id=scope_id,
            thread_id=thread_id,
            purpose=purpose,
        )
        sql = self._token_usage_rollup_select("model", "model")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY model ORDER BY COALESCE(model, '')"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # --- KB views ---

    @_synchronized
    def record_kb_view(self, slug: str) -> None:
        """Increment the view counter for a KB entry, stamping last_viewed_at.

        UPSERT: inserts the row at count 1 on first view, otherwise increments
        the existing count. Caller decides *when* to record (agent-CLI reads
        only — see kb-view-tracking-caller-signal). This is a metric write, not
        an audit row; it never routes through audit_log.
        """
        now = _now().isoformat()
        self._conn.execute(
            """INSERT INTO kb_views (slug, view_count, last_viewed_at)
               VALUES (?, 1, ?)
               ON CONFLICT(slug) DO UPDATE SET
                 view_count = view_count + 1,
                 last_viewed_at = excluded.last_viewed_at""",
            (slug, now),
        )
        self._conn.commit()

    @_synchronized
    def kb_view_stats(self) -> list[dict]:
        """Return per-slug view tallies, most-viewed first.

        Ordered by view_count DESC, then last_viewed_at DESC so ties surface
        the most recently read entry first.
        """
        rows = self._conn.execute(
            """SELECT slug, view_count, last_viewed_at
               FROM kb_views
               ORDER BY view_count DESC, last_viewed_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Skill validation events ---

    @_synchronized
    def insert_skill_validation_event(
        self,
        *,
        skill_id: str,
        slug: str,
        agent: str | None = None,
        source: str = "user_authored",
        severity: str = "info",
        ok: bool = True,
        version: str | None = None,
        findings: list[str] | None = None,
        reason_codes: list[str] | None = None,
    ) -> int:
        """Insert a skill validation event and return the row id."""
        now = _now().isoformat()
        findings_json = json.dumps(findings or [])
        codes_json = json.dumps(reason_codes or [])
        cursor = self._conn.execute(
            """INSERT INTO skill_validation_events
               (skill_id, slug, agent, source, severity, ok, version,
                findings, reason_codes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                skill_id,
                slug,
                agent,
                source,
                severity,
                1 if ok else 0,
                version,
                findings_json,
                codes_json,
                now,
            ),
        )
        self._conn.commit()
        return cursor.lastrowid

    @_synchronized
    def list_skill_validation_events(
        self,
        *,
        skill_id: str | None = None,
        agent: str | None = None,
        source: str | None = None,
        since: str | None = None,
        severity: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """List skill validation events with optional filters."""
        clauses = ["1=1"]
        params: list = []
        if skill_id is not None:
            clauses.append("skill_id = ?")
            params.append(skill_id)
        if agent is not None:
            clauses.append("agent = ?")
            params.append(agent)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        params.append(limit)
        rows = self._conn.execute(
            f"""SELECT id, skill_id, slug, agent, source, severity, ok, version,
                       findings, reason_codes, created_at
                FROM skill_validation_events
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC
                LIMIT ?""",
            params,
        ).fetchall()
        result: list[dict] = []
        for r in rows:
            d = dict(r)
            d["ok"] = bool(d["ok"])
            d["findings"] = json.loads(d["findings"] or "[]")
            d["reason_codes"] = json.loads(d["reason_codes"] or "[]")
            result.append(d)
        return result

    @_synchronized
    def get_latest_skill_validation(
        self, skill_id: str, version: str | None = None
    ) -> dict | None:
        """Return the latest validation event for a skill, optionally for a specific version."""
        if version is not None:
            row = self._conn.execute(
                """SELECT id, skill_id, slug, agent, source, severity, ok, version,
                           findings, reason_codes, created_at
                    FROM skill_validation_events
                    WHERE skill_id = ? AND version = ?
                    ORDER BY created_at DESC LIMIT 1""",
                (skill_id, version),
            ).fetchone()
        else:
            row = self._conn.execute(
                """SELECT id, skill_id, slug, agent, source, severity, ok, version,
                           findings, reason_codes, created_at
                    FROM skill_validation_events
                    WHERE skill_id = ?
                    ORDER BY created_at DESC LIMIT 1""",
                (skill_id,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["ok"] = bool(d["ok"])
        d["findings"] = json.loads(d["findings"] or "[]")
        d["reason_codes"] = json.loads(d["reason_codes"] or "[]")
        return d

    @_synchronized
    def get_latest_skill_materialization(
        self, skill_id: str, agent: str
    ) -> dict | None:
        """Return the latest materialization event for a skill+agent pair.

        Used by effective-state computation (§7.1): a skill is effective for an
        agent iff the latest materialization event's version matches the current
        store version.
        """
        row = self._conn.execute(
            """SELECT id, skill_id, slug, agent, source, severity, ok, version,
                       findings, reason_codes, created_at
                FROM skill_validation_events
                WHERE skill_id = ? AND agent = ? AND source = 'materialization'
                ORDER BY created_at DESC LIMIT 1""",
            (skill_id, agent),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["ok"] = bool(d["ok"])
        d["findings"] = json.loads(d["findings"] or "[]")
        d["reason_codes"] = json.loads(d["reason_codes"] or "[]")
        return d

    # --- Thread IDs ---

    @_synchronized
    def next_thread_id(self) -> str:
        """Return the next available THR-NNN id.

        Callers must hold DaemonState.db_lock across the next_thread_id() +
        insert_thread() pair to avoid duplicate IDs under concurrent requests
        (same requirement as next_task_id).
        """
        cursor = self._conn.execute(
            "SELECT MAX(CAST(SUBSTR(id, 5) AS INTEGER)) AS m "
            "FROM threads WHERE id GLOB 'THR-[0-9]*'"
        )
        n = (cursor.fetchone()["m"] or 0) + 1
        return f"THR-{n:03d}"

    @_synchronized
    def next_job_id(self) -> str:
        """Return the next available JOB-NNN id.

        Callers must hold DaemonState.db_lock across the next_job_id()
        + insert_job() pair to avoid duplicate IDs under concurrent
        requests (same requirement as next_task_id / next_thread_id).
        """
        cursor = self._conn.execute(
            "SELECT MAX(CAST(SUBSTR(id, 5) AS INTEGER)) AS m "
            "FROM jobs WHERE id GLOB 'JOB-[0-9]*'"
        )
        n = (cursor.fetchone()["m"] or 0) + 1
        return f"JOB-{n:03d}"

    @_synchronized
    def insert_job(self, r: "JobRecord") -> None:
        self._conn.execute(
            """INSERT INTO jobs (
                id, task_id, agent_name, title, rationale, script_text,
                interpreter, cwd_hint, status, exit_code,
                stdout_head, stderr_head, stdout_path, stderr_path,
                duration_ms, started_at, finished_at,
                reviewed_at, reviewed_by, reject_reason,
                cwd_resolved, max_runtime_seconds, max_output_bytes,
                review_required, persistent, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r.id, r.task_id, r.agent_name, r.title, r.rationale, r.script_text,
                r.interpreter.value, r.cwd_hint, r.status.value, r.exit_code,
                r.stdout_head, r.stderr_head, r.stdout_path, r.stderr_path,
                r.duration_ms, r.started_at, r.finished_at,
                r.reviewed_at, r.reviewed_by, r.reject_reason,
                r.cwd_resolved, r.max_runtime_seconds, r.max_output_bytes,
                int(r.review_required), int(r.persistent), r.created_at,
            ),
        )
        self._conn.commit()

    @_synchronized
    def get_job(self, job_id: str) -> "JobRecord | None":
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    @staticmethod
    def _row_to_job(row) -> "JobRecord":
        from runtime.models import JobRecord, JobStatus, JobInterpreter
        # ``reason`` may be missing on rows from pre-migration installs that
        # never hit a terminal transition with the new schema — use defensive
        # key access via SQLite's Row mapping interface.
        keys = row.keys() if hasattr(row, "keys") else ()
        reason = row["reason"] if "reason" in keys else None

        return JobRecord(
            id=row["id"],
            task_id=row["task_id"],
            agent_name=row["agent_name"],
            title=row["title"],
            rationale=row["rationale"],
            script_text=row["script_text"],
            interpreter=JobInterpreter(row["interpreter"]),
            cwd_hint=row["cwd_hint"],
            status=JobStatus(row["status"]),
            exit_code=row["exit_code"],
            stdout_head=row["stdout_head"],
            stderr_head=row["stderr_head"],
            stdout_path=row["stdout_path"],
            stderr_path=row["stderr_path"],
            duration_ms=row["duration_ms"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            reviewed_at=row["reviewed_at"],
            reviewed_by=row["reviewed_by"],
            reject_reason=row["reject_reason"],
            cwd_resolved=row["cwd_resolved"],
            max_runtime_seconds=row["max_runtime_seconds"],
            max_output_bytes=row["max_output_bytes"],
            review_required=bool(row["review_required"]),
            persistent=bool(row["persistent"]),
            reason=reason,
            created_at=row["created_at"],
        )

    @_synchronized
    def get_job_status(self, job_id: str) -> str | None:
        """Return jobs.status for the given job id, or None if not present.

        Used by the blocked-on-job predicate-check in _maybe_resume_blocked_task
        and by run_step_impl's entry-state branch (spec §5.1, §5.4).
        """
        row = self._conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return row["status"] if row is not None else None

    @_synchronized
    def get_job_owner_task_id(self, job_id: str) -> str | None:
        """Return jobs.task_id for the given job id, or None if not present.

        Used by the completion-route validation to verify that the agent
        submitting a blocked completion actually owns the referenced jobs.
        """
        row = self._conn.execute(
            "SELECT task_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return row["task_id"] if row is not None else None

    @_synchronized
    def list_jobs_db(
        self,
        *,
        status: str | list[str] | None = None,
        agent: str | None = None,
        task_id: str | None = None,
        review_required: bool | None = None,
        persistent: bool | None = None,
        limit: int = 50,
    ) -> list["JobRecord"]:
        clauses: list[str] = []
        params: list = []
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            placeholders = ",".join("?" * len(statuses))
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if agent is not None:
            clauses.append("agent_name = ?")
            params.append(agent)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if review_required is not None:
            clauses.append("review_required = ?")
            params.append(1 if review_required else 0)
        if persistent is not None:
            clauses.append("persistent = ?")
            params.append(1 if persistent else 0)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        rows = self._conn.execute(
            f"SELECT * FROM jobs {where} "
            f"ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    @_synchronized
    def list_job_ids_by_status(self, statuses: set[str]) -> list[str]:
        """Exhaustive status-filtered job-id query (no cap, DB-side filter).

        ``list_jobs_db`` is a presentation list capped (default 50) and ordered
        newest-first; using it for a liveness check can hide an old active row
        behind newer terminal rows. This returns every job id whose status is
        in ``statuses`` so a portability preflight cannot miss an active job.
        Read-only; returns ids only, not full job records.
        """
        if not statuses:
            return []
        placeholders = ",".join("?" * len(statuses))
        rows = self._conn.execute(
            f"SELECT id FROM jobs WHERE status IN ({placeholders}) "
            "ORDER BY created_at, id",
            tuple(sorted(statuses)),
        ).fetchall()
        return [row["id"] for row in rows]

    @_synchronized
    def list_stale_pending_jobs(self, cutoff_iso: str) -> list[dict]:
        """Read-only scan for never-started pending jobs older than ``cutoff_iso``.

        Predicate: ``status='pending' AND started_at IS NULL AND created_at <=
        cutoff`` — a row that was submitted but never dispatched (no
        ``transition_job_to_running`` ever stamped ``started_at``) and has
        reached the observation threshold. Purely observational: callers
        must NOT use this as a reaper/retry/cancel mechanism. Returns a
        lightweight dict per row (id/task_id/agent_name/title/review_required/
        created_at) — not full JobRecords — because this is a diagnostic scan.
        """
        rows = self._conn.execute(
            _STALE_PENDING_JOBS_SCAN_SQL,
            (cutoff_iso,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def transition_never_started_job_to_failed(
        self, job_id: str, *, now_iso: str, reason: str,
    ) -> None:
        """Guarded bookkeeping transition: pending + never-started → failed.

        The terminalization seam for founder-authorized reconciliation of
        abandoned never-dispatched jobs (THR-195). Only a row that is STILL
        ``status='pending'`` AND ``started_at IS NULL`` can be reconciled;
        any concurrent dispatch (which first transitions to ``running`` and
        stamps ``started_at``) makes this a no-op ValueError. Mirrors the
        guarded UPDATE shape of ``transition_job_to_rejected`` — callers get
        no ad-hoc SQL path.

        The UPDATE is deliberately left UNCOMMITTED: the caller owns the
        surrounding transaction so this terminalization and its
        ``job_reconciled_orphaned`` audit row commit atomically
        (``insert_audit_log_uncommitted`` + ``commit()``) and roll back
        together on any failure — a terminalized job must never survive
        without its durable non-live-proof audit record.
        """
        cur = self._conn.execute(
            "UPDATE jobs SET status='failed', reason=?, finished_at=?, "
            "duration_ms=COALESCE(duration_ms, 0) "
            "WHERE id=? AND status='pending' AND started_at IS NULL",
            (reason, now_iso, job_id),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"not_never_started_pending: job {job_id} cannot be reconciled"
            )

    @_synchronized
    def transition_job_to_rejected(
        self, job_id: str, *, reviewer: str, reason: str, reviewed_at: str
    ) -> None:
        cur = self._conn.execute(
            "UPDATE jobs "
            "SET status='rejected', reviewed_by=?, reject_reason=?, reviewed_at=? "
            "WHERE id=? AND status='pending'",
            (reviewer, reason, reviewed_at, job_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"not_pending: job {job_id} cannot be rejected")

    @_synchronized
    def transition_job_to_running(
        self,
        job_id: str,
        *,
        reviewer: str,
        reviewed_at: str,
        started_at: str,
        cwd_resolved: str,
        max_runtime_seconds: int | None,
        stdout_path: str,
        stderr_path: str,
    ) -> None:
        cur = self._conn.execute(
            "UPDATE jobs SET "
            "status='running', reviewed_by=?, reviewed_at=?, started_at=?, "
            "cwd_resolved=?, max_runtime_seconds=?, stdout_path=?, stderr_path=? "
            "WHERE id=? AND status='pending'",
            (reviewer, reviewed_at, started_at, cwd_resolved, max_runtime_seconds,
             stdout_path, stderr_path, job_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"not_pending: job {job_id} cannot transition to running")

    @_synchronized
    def transition_job_to_terminal(
        self,
        job_id: str,
        *,
        status: "JobStatus",
        exit_code: int | None,
        finished_at: str,
        duration_ms: int,
        stdout_head: str | None,
        stderr_head: str | None,
        reason: str | None = None,
    ) -> None:
        if status.value not in ("completed", "failed"):
            raise ValueError(f"invalid terminal status: {status.value}")
        cur = self._conn.execute(
            "UPDATE jobs SET "
            "status=?, exit_code=?, finished_at=?, duration_ms=?, "
            "stdout_head=?, stderr_head=?, reason=? "
            "WHERE id=? AND status='running'",
            (status.value, exit_code, finished_at, duration_ms,
             stdout_head, stderr_head, reason, job_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"not_running: job {job_id} cannot transition to terminal")

    @_synchronized
    def recover_orphaned_running_jobs(self, *, now_iso: str) -> list[str]:
        """Force-transition any SR left in 'running' state to 'failed'.

        Called from the daemon FastAPI lifespan on startup. The subprocess
        and its parent daemon process are gone; partial output on disk is
        preserved but the row is marked failed so the founder UI doesn't
        leave them in a permanent running state.
        """
        rows = self._conn.execute(
            "SELECT id FROM jobs WHERE status='running'"
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return []
        self._conn.executemany(
            "UPDATE jobs SET status='failed', reason='daemon_crash', finished_at=?, "
            "duration_ms=COALESCE(duration_ms, 0), "
            "stderr_head=COALESCE(stderr_head, '') || '\n[daemon restart killed run]' "
            "WHERE id=?",
            [(now_iso, job_id) for job_id in ids],
        )
        self._conn.commit()
        return ids

    @_synchronized
    def insert_thread(self, t: ThreadRecord) -> None:
        # Spec §3.1: composed_from_task_id is the sole composer attribution.
        self._conn.execute(
            """INSERT INTO threads (
                id, subject, started_at, archived_at, status,
                forwarded_from_id, forwarded_from_kind,
                turn_cap, turns_used, summary,
                transcript_path,
                composed_by, composed_from_task_id, composed_from_dream_id,
                pinned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                t.id,
                t.subject,
                t.started_at.isoformat(),
                t.archived_at.isoformat() if t.archived_at else None,
                t.status.value,
                t.forwarded_from_id,
                t.forwarded_from_kind,
                t.turn_cap,
                t.turns_used,
                t.summary,
                t.transcript_path,
                t.composed_by,
                t.composed_from_task_id,
                t.composed_from_dream_id,
                t.pinned_at.isoformat() if t.pinned_at else None,
            ),
        )
        self._conn.commit()

    def _row_to_thread(self, row) -> ThreadRecord:
        keys = row.keys()
        return ThreadRecord(
            id=row["id"],
            subject=row["subject"],
            status=ThreadStatus(row["status"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            archived_at=datetime.fromisoformat(row["archived_at"]) if row["archived_at"] else None,
            forwarded_from_id=row["forwarded_from_id"],
            forwarded_from_kind=row["forwarded_from_kind"],
            turn_cap=row["turn_cap"],
            turns_used=row["turns_used"],
            summary=row["summary"],
            transcript_path=row["transcript_path"],
            composed_by=row["composed_by"] if "composed_by" in keys else "founder",
            composed_from_task_id=row["composed_from_task_id"] if "composed_from_task_id" in keys else None,
            composed_from_dream_id=row["composed_from_dream_id"] if "composed_from_dream_id" in keys else None,
            last_speaker=row["last_speaker"] if "last_speaker" in keys else None,
            pinned_at=(
                datetime.fromisoformat(row["pinned_at"]) if row["pinned_at"] else None
            ) if "pinned_at" in keys else None,
            last_activity_at=(
                datetime.fromisoformat(row["last_activity_at"]) if row["last_activity_at"] else None
            ) if "last_activity_at" in keys else None,
        )

    @_synchronized
    def get_thread(self, thread_id: str) -> ThreadRecord | None:
        cursor = self._conn.execute(
            "SELECT * FROM threads WHERE id = ?", (thread_id,)
        )
        row = cursor.fetchone()
        return self._row_to_thread(row) if row else None

    @_synchronized
    def list_threads(self, *, status: str | None = None, limit: int = 50) -> list[ThreadRecord]:
        query = (
            "SELECT t.*, "
            "(SELECT tm.speaker FROM thread_messages tm "
            " WHERE tm.thread_id = t.id ORDER BY tm.seq DESC LIMIT 1) AS last_speaker, "
            "(SELECT MAX(tm.created_at) FROM thread_messages tm "
            " WHERE tm.thread_id = t.id) AS last_activity_at "
            "FROM threads t "
        )
        # THR-209 message-9 correction (TASK-5976): pinned threads rank above
        # unpinned ONLY in the OPEN list, ordered by immutable NUMERIC thread
        # ID descending (THR-10 above THR-2 — never lexicographic subject/text
        # and never activity). The numeric key is conditional on pinned_at
        # being set, so unpinned rows tie on it (NULL → 0) and fall through to
        # the exact existing ordinary key — ordinary order is byte-for-byte
        # unchanged when no pins exist. ARCHIVED and status-less views have
        # ZERO pin presentation: no pin rank at all, ordinary ordering only
        # (archived → archived_at DESC; status-less → started_at DESC), so
        # archived pin state can never leak into a mixed view.
        pinned_rank = "CASE WHEN t.pinned_at IS NOT NULL THEN 0 ELSE 1 END"
        pinned_numeric_id = (
            "CASE WHEN t.pinned_at IS NOT NULL THEN "
            "CAST(SUBSTR(t.id, 5) AS INTEGER) END DESC"
        )
        params: tuple
        if status == "archived":
            base_order = "COALESCE(t.archived_at, t.started_at) DESC"
            query += f"WHERE t.status = ? ORDER BY {base_order} LIMIT ?"
            params = (status, limit)
        elif status:
            query += (
                f"WHERE t.status = ? ORDER BY {pinned_rank}, {pinned_numeric_id}, "
                f"t.started_at DESC LIMIT ?"
            )
            params = (status, limit)
        else:
            query += f"ORDER BY t.started_at DESC LIMIT ?"
            params = (limit,)
        cursor = self._conn.execute(query, params)
        return [self._row_to_thread(r) for r in cursor.fetchall()]

    @_synchronized
    def list_threads_by_composed_from_task_id(
        self, task_id: str,
    ) -> list[ThreadRecord]:
        """Threads composed from a given task (daemon-cleanup provenance).

        Targeted identity lookup (THR-195 / TASK-6043) backed by the existing
        ``idx_threads_composed_from_task`` index: unlike a bounded
        presentation-page scan of open threads, this can locate an older
        thread no matter how many newer threads exist. Ordered newest-started
        first.
        """
        cursor = self._conn.execute(
            "SELECT * FROM threads WHERE composed_from_task_id = ? "
            "ORDER BY started_at DESC",
            (task_id,),
        )
        return [self._row_to_thread(r) for r in cursor.fetchall()]

    @_synchronized
    def add_thread_participant(
        self, thread_id: str, agent_name: str, *, added_by: str
    ) -> bool:
        """Insert a participant. Returns True if inserted, False if duplicate."""
        try:
            self._conn.execute(
                "INSERT INTO thread_participants (thread_id, agent_name, added_at, added_by) "
                "VALUES (?, ?, ?, ?)",
                (thread_id, agent_name, _now().isoformat(), added_by),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    @_synchronized
    def is_thread_participant(self, thread_id: str, agent_name: str) -> bool:
        cursor = self._conn.execute(
            "SELECT 1 FROM thread_participants WHERE thread_id = ? AND agent_name = ?",
            (thread_id, agent_name),
        )
        return cursor.fetchone() is not None

    @_synchronized
    def list_thread_participants(self, thread_id: str) -> list[ThreadParticipant]:
        cursor = self._conn.execute(
            "SELECT thread_id, agent_name, added_at, added_by "
            "FROM thread_participants WHERE thread_id = ? ORDER BY added_at",
            (thread_id,),
        )
        return [
            ThreadParticipant(
                thread_id=r["thread_id"],
                agent_name=r["agent_name"],
                added_at=datetime.fromisoformat(r["added_at"]),
                added_by=r["added_by"],
            )
            for r in cursor.fetchall()
        ]

    @_synchronized
    def remove_thread_participant(
        self, thread_id: str, agent_name: str
    ) -> bool:
        """Hard-delete a participant row. Returns True if a row was deleted."""
        cursor = self._conn.execute(
            "DELETE FROM thread_participants WHERE thread_id = ? AND agent_name = ?",
            (thread_id, agent_name),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def get_thread_session(
        self, thread_id: str, agent_name: str
    ) -> tuple[str | None, int]:
        """Return (agent_session_id, last_resumed_seq) for a (thread, agent).

        Returns (None, 0) when the participant row is absent — the safe
        turn-1 default that drives a full-context first invocation.
        """
        cursor = self._conn.execute(
            "SELECT agent_session_id, last_resumed_seq FROM thread_participants "
            "WHERE thread_id = ? AND agent_name = ?",
            (thread_id, agent_name),
        )
        row = cursor.fetchone()
        if row is None:
            return (None, 0)
        return (row["agent_session_id"], row["last_resumed_seq"] or 0)

    @_synchronized
    def update_thread_session(
        self,
        thread_id: str,
        agent_name: str,
        *,
        agent_session_id: str | None,
        last_resumed_seq: int,
    ) -> None:
        """Persist the resumable session id + delta watermark for a participant."""
        self._conn.execute(
            "UPDATE thread_participants SET agent_session_id = ?, last_resumed_seq = ? "
            "WHERE thread_id = ? AND agent_name = ?",
            (agent_session_id, last_resumed_seq, thread_id, agent_name),
        )
        self._conn.commit()

    @_synchronized
    def invalidate_thread_session_evicted(
        self,
        thread_id: str,
        agent_name: str,
        *,
        stale_session_id: str,
        error: str,
        executor: str = "claude",
    ) -> None:
        """One transaction: eviction audit + durable session-id invalidation.

        THR-200: fires at the provider-declared session-not-found boundary,
        BEFORE the full-prompt fallback launch. The audit row and the
        ``agent_session_id = NULL`` update commit atomically, so a failed
        fallback can never leave the stale id durable for the next wake.
        ``last_resumed_seq`` is intentionally preserved — a failed fallback
        must not advance delivery state; the next wake re-attempts the same
        required range (the id being NULL forces a full-prompt launch).
        """
        payload = {
            "executor": executor,
            "stale_session_id": stale_session_id,
            "error": (error or "")[:500],
        }
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    thread_id,
                    agent_name,
                    "agent_session_evicted_fallback",
                    json.dumps(payload),
                    now,
                ),
            )
            self._conn.execute(
                "UPDATE thread_participants SET agent_session_id = NULL "
                "WHERE thread_id = ? AND agent_name = ?",
                (thread_id, agent_name),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def reset_thread_session(
        self, thread_id: str, agent_name: str
    ) -> None:
        """Clear one participant's resume state: id NULL, watermark 0.

        Used by lifecycle invalidation (archive, executor switch, agent
        termination) where any later thread resume must start fresh with a
        full-prompt launch.
        """
        self._conn.execute(
            "UPDATE thread_participants SET agent_session_id = NULL, "
            "last_resumed_seq = 0 WHERE thread_id = ? AND agent_name = ?",
            (thread_id, agent_name),
        )
        self._conn.commit()

    @_synchronized
    def reset_thread_sessions_for_agent(
        self,
        agent_name: str,
        *,
        audit_scope_id: str | None = None,
        audit_agent: str | None = None,
        audit_reason: str | None = None,
    ) -> int:
        """Clear resume state (id NULL, watermark 0) for every thread
        participant row owned by ``agent_name``. Returns the row count.

        Executor-switch and agent-termination lifecycle: a participant whose
        executor changes must not resume a provider session minted under a
        different executor profile; a terminated agent must not resume at all.

        THR-200: when ``audit_scope_id`` is given, the participant reset and
        the ``thread_session_invalidated`` audit row commit in ONE database
        transaction, so a reset/audit failure can never leave a partially
        committed lifecycle state (a new executor installed over stale
        sessions, or cleared sessions with no audit). The audit is only
        written when at least one row was reset.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._reset_thread_sessions_for_agent_uncommitted(agent_name)
            if rows and audit_scope_id is not None:
                self.insert_audit_log_uncommitted(
                    task_id=audit_scope_id,
                    agent=audit_agent,
                    action="thread_session_invalidated",
                    payload={
                        "reason": audit_reason,
                        "rows": rows,
                        "name": agent_name,
                    },
                )
            self._conn.commit()
            return rows
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def _reset_thread_sessions_for_agent_uncommitted(self, agent_name: str) -> int:
        """UPDATE every participant row owned by ``agent_name`` WITHOUT
        committing. The owning transaction (``reset_thread_sessions_for_agent``,
        ``terminate_agent_cleanups``) commits or rolls back."""
        cursor = self._conn.execute(
            "UPDATE thread_participants SET agent_session_id = NULL, "
            "last_resumed_seq = 0 WHERE agent_name = ?",
            (agent_name,),
        )
        return cursor.rowcount

    @_synchronized
    def reset_thread_sessions_for_thread(
        self,
        thread_id: str,
        *,
        audit_scope_id: str | None = None,
        audit_agent: str | None = None,
        audit_reason: str | None = None,
    ) -> int:
        """Clear resume state (id NULL, watermark 0) for every participant of
        one thread. Returns the row count.

        Archive lifecycle: the thread is closed; if it is ever re-opened,
        every participant resumes from a fresh full-prompt launch.

        THR-200: when ``audit_scope_id`` is given, the participant reset and
        the ``thread_session_invalidated`` audit row commit in ONE database
        transaction, so a reset/audit failure can never leave a partially
        committed lifecycle state (cleared sessions with no audit). The audit
        is only written when at least one row was reset.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._reset_thread_sessions_for_thread_uncommitted(thread_id)
            if rows and audit_scope_id is not None:
                self.insert_audit_log_uncommitted(
                    task_id=audit_scope_id,
                    agent=audit_agent,
                    action="thread_session_invalidated",
                    payload={"reason": audit_reason, "rows": rows},
                )
            self._conn.commit()
            return rows
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def _reset_thread_sessions_for_thread_uncommitted(self, thread_id: str) -> int:
        """UPDATE every participant row of one thread WITHOUT committing. The
        owning transaction (``reset_thread_sessions_for_thread``,
        ``archive_thread_and_reset_sessions``) commits or rolls back."""
        cursor = self._conn.execute(
            "UPDATE thread_participants SET agent_session_id = NULL, "
            "last_resumed_seq = 0 WHERE thread_id = ?",
            (thread_id,),
        )
        return cursor.rowcount

    @_synchronized
    def archive_thread_and_reset_sessions(
        self,
        thread_id: str,
        *,
        summary: str,
        audit_scope_id: str,
        audit_agent: str,
    ) -> None:
        """Archive a thread and invalidate every participant's resume state in
        ONE database transaction: the ``ARCHIVED`` status flip, the
        participant session resets (id NULL, watermark 0), and the
        ``thread_session_invalidated`` audit row commit atomically. A failure
        at any step rolls back the whole archive, leaving the thread OPEN
        with every session row and no audit residue.

        THR-200: the thread is closed; if it is ever re-opened, every
        participant resumes from a fresh full-prompt launch instead of a
        stale provider session.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._set_thread_status_archived_uncommitted(
                thread_id, summary=summary,
            )
            rows = self._reset_thread_sessions_for_thread_uncommitted(thread_id)
            if rows:
                self.insert_audit_log_uncommitted(
                    task_id=audit_scope_id,
                    agent=audit_agent,
                    action="thread_session_invalidated",
                    payload={"reason": "archive", "rows": rows},
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _append_thread_message_uncommitted(
        self,
        *,
        thread_id: str,
        speaker: str,
        kind: ThreadMessageKind,
        body_markdown: str | None = None,
        decline_reason: str | None = None,
        system_payload: dict | None = None,
        attachments: list[ThreadAttachment] | None = None,
        sent_from_task_id: str | None = None,
        mentions: list[str] | None = None,
    ) -> int:
        """Allocate seq + insert a message (and its attachments) WITHOUT
        opening or committing a transaction. Callers must own the transaction
        (``BEGIN IMMEDIATE`` / ``BEGIN``) and commit/rollback themselves.

        Returns the allocated seq.
        """
        cursor = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
            "FROM thread_messages WHERE thread_id = ?",
            (thread_id,),
        )
        next_seq = cursor.fetchone()["next_seq"]
        self._conn.execute(
            "INSERT INTO thread_messages (thread_id, seq, speaker, kind, "
            "body_markdown, decline_reason, system_payload_json, "
            "sent_from_task_id, mentions_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                next_seq,
                speaker,
                kind.value,
                body_markdown,
                decline_reason,
                json.dumps(system_payload) if system_payload else None,
                sent_from_task_id,
                json.dumps(mentions) if mentions is not None else None,
                _now().isoformat(),
            ),
        )
        for ordinal, attachment in enumerate(attachments or []):
            self._conn.execute(
                "INSERT INTO thread_message_attachments ("
                "thread_id, message_seq, ordinal, artifact_name, display_name, "
                "size_bytes, content_type, uploaded_by, created_at, "
                "thread_attachment_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    next_seq,
                    ordinal,
                    attachment.artifact_name,
                    attachment.display_name,
                    attachment.size_bytes,
                    attachment.content_type,
                    attachment.uploaded_by,
                    _now().isoformat(),
                    attachment.thread_attachment_id,
                ),
            )
        return next_seq

    @_synchronized
    def append_thread_message(
        self,
        *,
        thread_id: str,
        speaker: str,
        kind: ThreadMessageKind,
        body_markdown: str | None = None,
        decline_reason: str | None = None,
        system_payload: dict | None = None,
        attachments: list[ThreadAttachment] | None = None,
        sent_from_task_id: str | None = None,
    ) -> int:
        """Append a message and return its allocated seq.

        Atomic against concurrent appends — both the seq allocation and the
        insert happen under the connection's transaction, and the unique
        index on (thread_id, seq) guards against any race.
        """
        try:
            self._conn.execute("BEGIN")
            next_seq = self._append_thread_message_uncommitted(
                thread_id=thread_id,
                speaker=speaker,
                kind=kind,
                body_markdown=body_markdown,
                decline_reason=decline_reason,
                system_payload=system_payload,
                attachments=attachments,
                sent_from_task_id=sent_from_task_id,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return next_seq

    def _attachments_for_messages(
        self, thread_id: str, seqs: list[int]
    ) -> dict[int, list[ThreadAttachment]]:
        if not seqs:
            return {}
        placeholders = ",".join("?" for _ in seqs)
        cursor = self._conn.execute(
            "SELECT * FROM thread_message_attachments "
            f"WHERE thread_id = ? AND message_seq IN ({placeholders}) "
            "ORDER BY message_seq, ordinal",
            (thread_id, *seqs),
        )
        out: dict[int, list[ThreadAttachment]] = {seq: [] for seq in seqs}
        for row in cursor.fetchall():
            out.setdefault(row["message_seq"], []).append(
                ThreadAttachment(
                    artifact_name=row["artifact_name"],
                    display_name=row["display_name"],
                    size_bytes=row["size_bytes"],
                    content_type=row["content_type"],
                    uploaded_by=row["uploaded_by"],
                    thread_attachment_id=row["thread_attachment_id"],
                )
            )
        return out

    @_synchronized
    def list_thread_messages(
        self, thread_id: str, *, since_seq: int = 0, limit: int | None = 1000
    ) -> list[ThreadMessage]:
        """Messages for ``thread_id`` with ``seq > since_seq``, ascending.

        ``limit`` caps the returned row count; pass ``None`` for an uncapped
        load — the daemon's resume seam needs the complete canonical
        transcript to prove delta completeness (TASK-5989).
        """
        sql = (
            "SELECT * FROM thread_messages "
            "WHERE thread_id = ? AND seq > ? ORDER BY seq"
        )
        params: list = [thread_id, since_seq]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        cursor = self._conn.execute(sql, params)
        rows = cursor.fetchall()
        attachments_by_seq = self._attachments_for_messages(
            thread_id,
            [r["seq"] for r in rows],
        )
        return [
            ThreadMessage(
                id=r["id"],
                thread_id=r["thread_id"],
                seq=r["seq"],
                speaker=r["speaker"],
                kind=ThreadMessageKind(r["kind"]),
                body_markdown=r["body_markdown"],
                decline_reason=r["decline_reason"],
                system_payload=json.loads(r["system_payload_json"]) if r["system_payload_json"] else None,
                attachments=attachments_by_seq.get(r["seq"], []),
                mentions=json.loads(r["mentions_json"]) if r["mentions_json"] else [],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    @_synchronized
    def get_thread_max_message_seq(self, thread_id: str) -> int:
        """Authoritative highest transcript seq for ``thread_id`` (0 if empty).

        Read-only — the independent upper bound the thread runner uses to
        prove that the loaded transcript covers the complete required range
        before authorizing a resumed delta prompt (TASK-5989).
        """
        return self._thread_tail_seq(thread_id)

    @_synchronized
    def get_thread_message_by_seq(
        self, thread_id: str, seq: int
    ) -> ThreadMessage | None:
        cursor = self._conn.execute(
            "SELECT * FROM thread_messages WHERE thread_id = ? AND seq = ?",
            (thread_id, seq),
        )
        row = cursor.fetchone()
        if not row:
            return None
        attachments_by_seq = self._attachments_for_messages(thread_id, [seq])
        return ThreadMessage(
            id=row["id"],
            thread_id=row["thread_id"],
            seq=row["seq"],
            speaker=row["speaker"],
            kind=ThreadMessageKind(row["kind"]),
            body_markdown=row["body_markdown"],
            decline_reason=row["decline_reason"],
            system_payload=json.loads(row["system_payload_json"]) if row["system_payload_json"] else None,
            attachments=attachments_by_seq.get(seq, []),
            mentions=json.loads(row["mentions_json"]) if row["mentions_json"] else [],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # --- Thread-scoped attachments (TASK-1616) ---

    @_synchronized
    def next_thread_attachment_id(self) -> str:
        cursor = self._conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM thread_scoped_attachments"
        )
        n = cursor.fetchone()[0]
        return f"att-{n:03d}"

    @_synchronized
    def insert_thread_scoped_attachment(
        self,
        *,
        attachment_id: str,
        thread_id: str,
        display_name: str,
        size_bytes: int | None,
        content_type: str | None,
        uploaded_by: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO thread_scoped_attachments "
            "(attachment_id, thread_id, display_name, size_bytes, "
            "content_type, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                attachment_id,
                thread_id,
                display_name,
                size_bytes,
                content_type,
                uploaded_by,
                _now().isoformat(),
            ),
        )
        self._conn.commit()

    @_synchronized
    def get_thread_scoped_attachment(
        self, thread_id: str, attachment_id: str
    ) -> ThreadScopedAttachment | None:
        cursor = self._conn.execute(
            "SELECT * FROM thread_scoped_attachments "
            "WHERE thread_id = ? AND attachment_id = ?",
            (thread_id, attachment_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return ThreadScopedAttachment(
            attachment_id=row["attachment_id"],
            thread_id=row["thread_id"],
            display_name=row["display_name"],
            size_bytes=row["size_bytes"],
            content_type=row["content_type"],
            uploaded_by=row["uploaded_by"],
            created_at=row["created_at"],
        )

    @_synchronized
    def list_thread_scoped_attachments(
        self, thread_id: str
    ) -> list[ThreadScopedAttachment]:
        cursor = self._conn.execute(
            "SELECT * FROM thread_scoped_attachments "
            "WHERE thread_id = ? ORDER BY created_at",
            (thread_id,),
        )
        return [
            ThreadScopedAttachment(
                attachment_id=row["attachment_id"],
                thread_id=row["thread_id"],
                display_name=row["display_name"],
                size_bytes=row["size_bytes"],
                content_type=row["content_type"],
                uploaded_by=row["uploaded_by"],
                created_at=row["created_at"],
            )
            for row in cursor.fetchall()
        ]

    @_synchronized
    def delete_thread_scoped_attachment(
        self, thread_id: str, attachment_id: str
    ) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM thread_scoped_attachments "
            "WHERE thread_id = ? AND attachment_id = ?",
            (thread_id, attachment_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # --- Task attachments (THR-109) ---

    @_synchronized
    def next_task_attachment_id(self) -> str:
        cursor = self._conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM task_attachments"
        )
        n = cursor.fetchone()[0]
        return f"ta-{n:04d}"

    @_synchronized
    def insert_task_attachment(
        self,
        *,
        task_id: str,
        ordinal: int,
        storage_key: str,
        display_name: str,
        size_bytes: int | None,
        content_type: str | None,
        uploaded_by: str,
    ) -> None:
        # Reject if storage_key is already claimed — including by legacy
        # duplicate rows that were excluded from the partial unique index.
        existing = self.get_task_attachment_by_storage_key(storage_key)
        if existing is not None:
            raise sqlite3.IntegrityError(
                f"UNIQUE constraint failed: task_attachments.storage_key: "
                f"{storage_key}"
            )
        self._conn.execute(
            "INSERT INTO task_attachments "
            "(task_id, ordinal, storage_key, display_name, size_bytes, "
            "content_type, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                ordinal,
                storage_key,
                display_name,
                size_bytes,
                content_type,
                uploaded_by,
                _now().isoformat(),
            ),
        )
        self._conn.commit()

    @_synchronized
    def insert_task_with_attachments(
        self,
        task: "TaskRecord",
        attachments: list[dict],
        uploaded_by: str,
    ) -> None:
        """Atomically insert a task + its private attachment links + audit rows.

        Everything within one BEGIN IMMEDIATE / COMMIT transaction so a
        duplicate-storage-key UNIQUE violation, a link-write error, or an
        audit-write error rolls back the task row and every prior link.

        Caller MUST hold ``org.db_lock`` — the claimability re-check
        (SELECT by storage_key) runs inside the same serialized boundary.

        Raises ``sqlite3.IntegrityError`` when a storage_key has already
        been claimed by another task — the caller must translate this to a
        conflict response.
        """
        now = _now().isoformat()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                """INSERT INTO tasks (id, status, assigned_agent, team, brief,
                   revision_count, created_at, updated_at, completed_at, parent_task_id,
                   revisit_of_task_id, dispatched_from_thread_id,
                   block_kind, note,
                   orchestration_step_count, session_timeout_seconds, task_type, active_fanout,
                   current_session_id, zombie_flagged_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.id,
                    task.status.value,
                    task.assigned_agent,
                    task.team,
                    task.brief,
                    task.revision_count,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                    task.completed_at.isoformat() if task.completed_at else None,
                    task.parent_task_id,
                    task.revisit_of_task_id,
                    task.dispatched_from_thread_id,
                    task.block_kind.value if task.block_kind else None,
                    task.note,
                    task.orchestration_step_count,
                    task.session_timeout_seconds,
                    task.task_type,
                    task.active_fanout,
                    task.current_session_id,
                    task.zombie_flagged_at.isoformat() if task.zombie_flagged_at else None,
                ),
            )
            for att in attachments:
                # Reject if storage_key is already claimed — including by
                # legacy duplicate rows excluded from the partial unique index.
                existing = self._conn.execute(
                    "SELECT 1 FROM task_attachments WHERE storage_key = ?",
                    (att["storage_key"],),
                ).fetchone()
                if existing is not None:
                    raise sqlite3.IntegrityError(
                        "UNIQUE constraint failed: "
                        f"task_attachments.storage_key: {att['storage_key']}"
                    )
                self._conn.execute(
                    "INSERT INTO task_attachments "
                    "(task_id, ordinal, storage_key, display_name, size_bytes, "
                    "content_type, uploaded_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task.id,
                        att["ordinal"],
                        att["storage_key"],
                        att["display_name"],
                        att["size_bytes"],
                        att["content_type"],
                        uploaded_by,
                        now,
                    ),
                )
                # Audit row for each linked attachment.
                audit_ts = _now().isoformat()
                audit_payload = json.dumps({
                    "storage_key": att["storage_key"],
                    "display_name": att["display_name"],
                    "content_type": att["content_type"],
                    "uploaded_by": uploaded_by,
                })
                self._conn.execute(
                    "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (task.id, uploaded_by, "task_attachment_added", audit_payload, audit_ts),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _insert_task_attachments_txn(
        self, task_id: str, attachments: list[dict], uploaded_by: str,
    ) -> None:
        """Insert attachment links + audit rows within an existing transaction.

        Caller MUST have already started a transaction (BEGIN IMMEDIATE) and
        MUST be holding the @_synchronized lock. Does NOT commit — the caller
        owns the transaction lifecycle.

        Raises sqlite3.IntegrityError on duplicate storage_key.
        """
        now = _now().isoformat()
        for att in attachments:
            existing = self._conn.execute(
                "SELECT 1 FROM task_attachments WHERE storage_key = ?",
                (att["storage_key"],),
            ).fetchone()
            if existing is not None:
                raise sqlite3.IntegrityError(
                    "UNIQUE constraint failed: "
                    f"task_attachments.storage_key: {att['storage_key']}"
                )
            self._conn.execute(
                "INSERT INTO task_attachments "
                "(task_id, ordinal, storage_key, display_name, size_bytes, "
                "content_type, uploaded_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    att["ordinal"],
                    att["storage_key"],
                    att["display_name"],
                    att["size_bytes"],
                    att["content_type"],
                    uploaded_by,
                    now,
                ),
            )
            audit_ts = _now().isoformat()
            audit_payload = json.dumps({
                "storage_key": att["storage_key"],
                "display_name": att["display_name"],
                "content_type": att["content_type"],
                "uploaded_by": uploaded_by,
            })
            self._conn.execute(
                "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, uploaded_by, "task_attachment_added", audit_payload, audit_ts),
            )

    # --- Cleanup-report thread + task atomic producer (THR-195) ----------
    #
    # TASK-6046 finding 1: the daemon's cleanup trigger previously created the
    # report thread (committing thread/participant/message/turn/audit rows)
    # BEFORE inserting the task, so an insert failure left an OPEN thread
    # claiming a nonexistent task. The fix is one atomic producer operation:
    # thread + task commit or roll back together (KB
    # ``atomic-multi-table-persistence`` — composite method, single
    # BEGIN IMMEDIATE/COMMIT with rollback), so any failure leaves ZERO
    # durable residue and a later retry succeeds exactly once.

    def _insert_thread_uncommitted(self, t: ThreadRecord) -> None:
        """Insert a thread row WITHOUT committing — caller owns the transaction.

        Mirrors ``insert_audit_log_uncommitted`` / ``_append_thread_message_uncommitted``:
        the row joins the caller's open BEGIN IMMEDIATE transaction and is
        rolled back with it on any later failure.

        TASK-6082 (founder ruling): the legacy ``threads.mention_routing_enabled``
        column is OMITTED from the insert (TASK-6027 removed the field from
        ``ThreadRecord`` — routing is unconditional), matching ``insert_thread``;
        the shipped column keeps its NOT NULL DEFAULT 1 (inert storage).
        """
        self._conn.execute(
            """INSERT INTO threads (
                id, subject, started_at, archived_at, status,
                forwarded_from_id, forwarded_from_kind,
                turn_cap, turns_used, summary,
                transcript_path,
                composed_by, composed_from_task_id, composed_from_dream_id,
                pinned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                t.id,
                t.subject,
                t.started_at.isoformat(),
                t.archived_at.isoformat() if t.archived_at else None,
                t.status.value,
                t.forwarded_from_id,
                t.forwarded_from_kind,
                t.turn_cap,
                t.turns_used,
                t.summary,
                t.transcript_path,
                t.composed_by,
                t.composed_from_task_id,
                t.composed_from_dream_id,
                t.pinned_at.isoformat() if t.pinned_at else None,
            ),
        )

    def _add_thread_participant_uncommitted(
        self, thread_id: str, agent_name: str, *, added_by: str,
    ) -> bool:
        """Insert a thread participant WITHOUT committing (caller owns the
        transaction). Returns True if inserted, False if duplicate."""
        try:
            self._conn.execute(
                "INSERT INTO thread_participants (thread_id, agent_name, added_at, added_by) "
                "VALUES (?, ?, ?, ?)",
                (thread_id, agent_name, _now().isoformat(), added_by),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def _increment_thread_turns_used_uncommitted(
        self, thread_id: str, *, by: int = 1,
    ) -> None:
        """Raise ``threads.turns_used`` WITHOUT committing (caller owns the
        transaction)."""
        self._conn.execute(
            "UPDATE threads SET turns_used = turns_used + ? WHERE id = ?",
            (by, thread_id),
        )

    @_synchronized
    def insert_cleanup_report_thread_and_task(
        self,
        *,
        thread_id: str,
        subject: str,
        composer: str,
        opening_body: str,
        initial_recipients: list[str],
        turn_cap: int,
        task: TaskRecord,
    ) -> str:
        """Atomically create a cleanup-report thread AND insert the task it was
        composed from, in ONE BEGIN IMMEDIATE / COMMIT transaction.

        The daemon's per-agent founder-report thread (row, composer
        participant, opening message, turn accounting, and the canonical
        ``thread_started`` / ``thread_message_sent`` audit rows) and the task
        row commit or roll back together (KB ``atomic-multi-table-persistence``;
        same compound pattern as ``insert_task_with_attachments`` /
        ``try_delegate_many``). On ANY exception the whole batch rolls back —
        zero durable residue in every affected table — nothing is enqueued by
        the caller, and a later retry succeeds exactly once (TASK-6046 finding
        1). A report thread can never survive claiming a task that was not
        inserted, and a task can never dangle without its thread.

        Thread semantics mirror ``_create_agent_thread_locked`` for the
        founder-only report thread: the owning agent is the composer and
        participant; the opening message is appended as seq 1 with the mention
        signal derived exactly the same way (``_derive_conversational_mentions``
        — the daemon's opening carries no @mentions, so the wake set is empty);
        one turn is counted; the two audit payloads match ``AuditLogger``
        shapes. @founder is NOT a participant row (spec §3.3) and no agent
        recipients exist, so no reply wake is derived. The inserted task is a
        clean root (no parent, no thread dispatch). ``thread_id`` and ``task.id``
        must be caller-allocated under ``org.db_lock``.

        Returns the thread id. Raises on failure (nothing persisted).
        """
        thread = ThreadRecord(
            id=thread_id,
            subject=subject,
            turn_cap=turn_cap,
            composed_by=composer,
            composed_from_task_id=task.id,
        )
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._insert_thread_uncommitted(thread)
            self._add_thread_participant_uncommitted(
                thread_id, composer, added_by=composer,
            )
            mentions = self._derive_conversational_mentions(
                thread_id, composer, ThreadMessageKind.MESSAGE, opening_body,
            )
            seq = self._append_thread_message_uncommitted(
                thread_id=thread_id,
                speaker=composer,
                kind=ThreadMessageKind.MESSAGE,
                body_markdown=opening_body,
                mentions=mentions,
            )
            self._increment_thread_turns_used_uncommitted(thread_id, by=1)
            self.insert_audit_log_uncommitted(
                task_id=thread_id,
                agent=composer,
                action="thread_started",
                payload={
                    "subject": subject,
                    "initial_recipients": initial_recipients,
                    "forwarded_from_id": None,
                    "composed_by": composer,
                    "composed_from_task_id": task.id,
                    "composed_from_dream_id": None,
                },
            )
            self.insert_audit_log_uncommitted(
                task_id=thread_id,
                agent=composer,
                action="thread_message_sent",
                payload={"seq": seq, "kind": "message"},
            )
            self._conn.execute(
                """INSERT INTO tasks (id, status, assigned_agent, team, brief,
                   revision_count, created_at, updated_at, completed_at, parent_task_id,
                   revisit_of_task_id, dispatched_from_thread_id,
                   block_kind, note,
                   orchestration_step_count, session_timeout_seconds, task_type, active_fanout,
                   current_session_id, zombie_flagged_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.id,
                    task.status.value,
                    task.assigned_agent,
                    task.team,
                    task.brief,
                    task.revision_count,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                    task.completed_at.isoformat() if task.completed_at else None,
                    task.parent_task_id,
                    task.revisit_of_task_id,
                    task.dispatched_from_thread_id,
                    task.block_kind.value if task.block_kind else None,
                    task.note,
                    task.orchestration_step_count,
                    task.session_timeout_seconds,
                    task.task_type,
                    task.active_fanout,
                    task.current_session_id,
                    task.zombie_flagged_at.isoformat() if task.zombie_flagged_at else None,
                ),
            )
            self._conn.commit()
            return thread_id
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def get_task_attachment(
        self, task_id: str, storage_key: str
    ) -> TaskAttachmentRecord | None:
        cursor = self._conn.execute(
            "SELECT * FROM task_attachments "
            "WHERE task_id = ? AND storage_key = ?",
            (task_id, storage_key),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return TaskAttachmentRecord(
            id=row["id"],
            task_id=row["task_id"],
            ordinal=row["ordinal"],
            storage_key=row["storage_key"],
            display_name=row["display_name"],
            size_bytes=row["size_bytes"],
            content_type=row["content_type"],
            uploaded_by=row["uploaded_by"],
            created_at=row["created_at"],
            legacy_status=row["legacy_status"] if "legacy_status" in row.keys() else None,
        )

    @_synchronized
    def list_task_attachments(self, task_id: str) -> list[TaskAttachmentRecord]:
        cursor = self._conn.execute(
            "SELECT * FROM task_attachments "
            "WHERE task_id = ? ORDER BY ordinal",
            (task_id,),
        )
        return [
            TaskAttachmentRecord(
                id=row["id"],
                task_id=row["task_id"],
                ordinal=row["ordinal"],
                storage_key=row["storage_key"],
                display_name=row["display_name"],
                size_bytes=row["size_bytes"],
                content_type=row["content_type"],
                uploaded_by=row["uploaded_by"],
                created_at=row["created_at"],
                legacy_status=row["legacy_status"] if "legacy_status" in row.keys() else None,
            )
            for row in cursor.fetchall()
        ]

    @_synchronized
    def resolve_ancestor_attachments(
        self, task_id: str, max_hops: int = 20
    ) -> list[TaskAttachmentRecord]:
        """Walk the parent_task_id chain and union OWN + ancestor attachments.

        Returns the spawning task's own attachments plus every ancestor's
        attachments up to root, in deterministic order (own first, then
        nearest ancestor to root). No rows are copied into child tasks.
        The owning task_id is preserved per record so callers know which
        task each attachment came from.
        """
        result: list[TaskAttachmentRecord] = []
        # 1. Own attachments first.
        own = self.list_task_attachments(task_id)
        result.extend(own)
        # 2. Walk up to root, unioning ancestor attachments.
        seen: set[str] = {task_id}
        current_id = task_id
        for _ in range(max_hops):
            row = self._conn.execute(
                "SELECT parent_task_id FROM tasks WHERE id = ?",
                (current_id,),
            ).fetchone()
            if row is None:
                break
            parent_id = row["parent_task_id"]
            if parent_id is None or parent_id in seen:
                break
            seen.add(parent_id)
            # Collect attachments from this ancestor.
            attachments = self.list_task_attachments(parent_id)
            result.extend(attachments)
            current_id = parent_id
        return result

    @_synchronized
    def delete_task_attachment(
        self, task_id: str, storage_key: str
    ) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM task_attachments "
            "WHERE task_id = ? AND storage_key = ?",
            (task_id, storage_key),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    @_synchronized
    def count_task_attachments(self, task_id: str) -> int:
        cursor = self._conn.execute(
            "SELECT COUNT(*) FROM task_attachments WHERE task_id = ?",
            (task_id,),
        )
        return cursor.fetchone()[0]

    @_synchronized
    def get_task_attachment_by_storage_key(
        self, storage_key: str
    ) -> TaskAttachmentRecord | None:
        cursor = self._conn.execute(
            "SELECT * FROM task_attachments WHERE storage_key = ?",
            (storage_key,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return TaskAttachmentRecord(
            id=row["id"],
            task_id=row["task_id"],
            ordinal=row["ordinal"],
            storage_key=row["storage_key"],
            display_name=row["display_name"],
            size_bytes=row["size_bytes"],
            content_type=row["content_type"],
            uploaded_by=row["uploaded_by"],
            created_at=row["created_at"],
            legacy_status=row["legacy_status"] if "legacy_status" in row.keys() else None,
        )

    @_synchronized
    def mint_thread_invocation(
        self,
        *,
        thread_id: str,
        agent_name: str,
        triggering_seq: int,
        purpose: ThreadInvocationPurpose,
    ) -> ThreadInvocation:
        import uuid as _uuid
        token = _uuid.uuid4().hex
        now = _now().isoformat()
        cursor = self._conn.execute(
            "INSERT INTO thread_invocations (thread_id, agent_name, "
            "invocation_token, triggering_seq, purpose, status, enqueued_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (thread_id, agent_name, token, triggering_seq, purpose.value, now),
        )
        self._conn.commit()
        return ThreadInvocation(
            id=cursor.lastrowid,
            thread_id=thread_id,
            agent_name=agent_name,
            invocation_token=token,
            triggering_seq=triggering_seq,
            purpose=purpose,
            status=ThreadInvocationStatus.PENDING,
            enqueued_at=datetime.fromisoformat(now),
        )

    def _row_to_invocation(self, row) -> ThreadInvocation:
        return ThreadInvocation(
            id=row["id"],
            thread_id=row["thread_id"],
            agent_name=row["agent_name"],
            invocation_token=row["invocation_token"],
            triggering_seq=row["triggering_seq"],
            purpose=ThreadInvocationPurpose(row["purpose"]),
            status=ThreadInvocationStatus(row["status"]),
            enqueued_at=datetime.fromisoformat(row["enqueued_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            consumed_at=datetime.fromisoformat(row["consumed_at"]) if row["consumed_at"] else None,
            session_id=row["session_id"],
            dispatched_task_id=row["dispatched_task_id"],
            decline_reason=row["decline_reason"],
        )

    @_synchronized
    def get_pending_invocation(self, token: str) -> ThreadInvocation | None:
        cursor = self._conn.execute(
            "SELECT * FROM thread_invocations "
            "WHERE invocation_token = ? AND status = 'pending'",
            (token,),
        )
        row = cursor.fetchone()
        return self._row_to_invocation(row) if row else None

    @_synchronized
    def get_invocation_any_status(self, token: str) -> ThreadInvocation | None:
        cursor = self._conn.execute(
            "SELECT * FROM thread_invocations WHERE invocation_token = ?",
            (token,),
        )
        row = cursor.fetchone()
        return self._row_to_invocation(row) if row else None

    @_synchronized
    def consume_invocation(self, token: str) -> bool:
        cursor = self._conn.execute(
            "UPDATE thread_invocations SET status = 'consumed', "
            "consumed_at = ? WHERE invocation_token = ? AND status = 'pending'",
            (_now().isoformat(), token),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def _autonomous_continuation_retry_lineage_cte(self) -> str:
        """Return the persisted per-slice retry predicate used by THR-166.

        The production retry ceiling treats a FAILED direct child as exhausted
        when its revisit chain contains an earlier FAILED child of this same
        parent.  Keep the 200-hop bound aligned with
        ``run_step._is_slice_retry_exhausted``.
        """
        return """WITH RECURSIVE retry_lineage
                   (child_id, id, parent_task_id, status, revisit_of_task_id, depth) AS (
                   SELECT id, id, parent_task_id, status, revisit_of_task_id, 0
                     FROM tasks
                    WHERE parent_task_id = ? AND status = 'failed'
                   UNION ALL
                   SELECT retry_lineage.child_id, predecessor.id,
                          predecessor.parent_task_id, predecessor.status,
                          predecessor.revisit_of_task_id, retry_lineage.depth + 1
                     FROM retry_lineage
                     JOIN tasks AS predecessor
                       ON predecessor.id = retry_lineage.revisit_of_task_id
                    WHERE retry_lineage.depth < 199
               )"""

    @_synchronized
    def autonomous_continuation_budget_exhausted(
        self, task_id: str, *, max_steps: int, max_revise_rounds: int,
    ) -> bool:
        """Whether a root is under any absolute THR-166 budget blocker.

        This derives all three durable causes from task state and lineage:
        orchestration steps, the configured revise-round cap, and the existing
        per-slice retry ceiling.  It intentionally does not inspect request
        evidence or manager-authored prose.
        """
        cte = self._autonomous_continuation_retry_lineage_cte()
        row = self._conn.execute(
            f"""{cte}
                SELECT 1
                  FROM tasks
                 WHERE id = ?
                   AND (
                       orchestration_step_count >= ?
                       OR (? > 0 AND revision_count >= ?)
                       OR EXISTS (
                           SELECT 1 FROM retry_lineage
                            WHERE depth > 0
                              AND parent_task_id = ?
                              AND status = 'failed'
                       )
                   )""",
            (task_id, task_id, max_steps, max_revise_rounds,
             max_revise_rounds, task_id),
        ).fetchone()
        return row is not None

    @_synchronized
    def continue_escalation_from_followup(
        self,
        *,
        task_id: str,
        thread_id: str,
        dispatcher: str,
        invocation_token: str,
        max_steps: int,
        max_revise_rounds: int,
        note: str,
        audit_payload: dict,
    ) -> bool:
        """Atomically consume the causal follow-up and make one queue intent.

        The caller has already validated policy/evidence.  This transaction is
        the final authority boundary: cancellation, a stale status, budget
        exhaustion, or a replay rolls the whole operation back.  The caller may
        notify the in-memory queue only after this commit; ``try_claim_for_step``
        remains the at-most-once admission gate if that notification is replayed.
        """
        now = _now().isoformat()
        try:
            self._conn.execute("BEGIN")
            cte = self._autonomous_continuation_retry_lineage_cte()
            self._conn.execute(
                f"""{cte}
                UPDATE tasks
                   SET status = ?, block_kind = NULL, note = ?, updated_at = ?
                   WHERE id = ? AND status = ? AND cancelled_at IS NULL
                     AND orchestration_step_count < ?
                     AND (? <= 0 OR revision_count < ?)
                     AND NOT EXISTS (
                         SELECT 1 FROM retry_lineage
                          WHERE depth > 0
                            AND parent_task_id = ?
                            AND status = 'failed'
                     )""",
                (task_id, TaskStatus.PENDING.value, note, now, task_id,
                 TaskStatus.ESCALATED.value, max_steps, max_revise_rounds,
                 max_revise_rounds, task_id),
            )
            # sqlite3 reports ``rowcount=-1`` for an UPDATE prefixed by a
            # recursive CTE, so use SQLite's statement-local change count.
            if self._conn.execute("SELECT changes()").fetchone()[0] != 1:
                self._conn.rollback()
                return False
            token_update = self._conn.execute(
                """UPDATE thread_invocations SET status = 'consumed', consumed_at = ?
                   WHERE invocation_token = ? AND thread_id = ? AND agent_name = ?
                     AND purpose = ? AND status = 'pending'""",
                (now, invocation_token, thread_id, dispatcher,
                 ThreadInvocationPurpose.TASK_FOLLOWUP.value),
            )
            if token_update.rowcount != 1:
                self._conn.rollback()
                return False
            self._conn.execute(
                "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, dispatcher, "escalation_continued_autonomously",
                 json.dumps(audit_payload), now),
            )
            self._conn.execute(
                """UPDATE escalation_notifications
                   SET consumed_at = ?, consumed_by = 'autonomous-continuation'
                   WHERE task_id = ? AND consumed_at IS NULL""",
                (now, task_id),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def mark_invocation_declined(
        self, token: str, *, decline_reason: str | None = None
    ) -> bool:
        """Set invocation status to 'declined' with an optional reason.

        Returns True if the row was updated (was pending), False otherwise.
        """
        cursor = self._conn.execute(
            "UPDATE thread_invocations SET status = 'declined', "
            "consumed_at = ?, decline_reason = ? "
            "WHERE invocation_token = ? AND status = 'pending'",
            (_now().isoformat(), decline_reason, token),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def decline_pending_invocations_for_agent(
        self, thread_id: str, agent_name: str,
        *, decline_reason: str | None = None,
    ) -> int:
        """Bulk-decline all pending invocations for (thread_id, agent_name).

        Returns the count of rows updated.
        """
        now = _now().isoformat()
        cursor = self._conn.execute(
            "UPDATE thread_invocations SET status = 'declined', "
            "consumed_at = ?, decline_reason = ? "
            "WHERE thread_id = ? AND agent_name = ? AND status = 'pending'",
            (now, decline_reason, thread_id, agent_name),
        )
        self._conn.commit()
        return cursor.rowcount

    @_synchronized
    def record_dispatch_on_invocation(
        self, token: str, *, task_id: str
    ) -> bool:
        cursor = self._conn.execute(
            "UPDATE thread_invocations SET dispatched_task_id = ? "
            "WHERE invocation_token = ? AND status = 'pending' "
            "AND dispatched_task_id IS NULL",
            (task_id, token),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def dispatch_task_followup_replacement(
        self,
        *,
        token: str,
        thread_id: str,
        dispatcher: str,
        task: TaskRecord,
        team: str,
    ) -> dict:
        """Atomically admit the single replacement allowed to a task follow-up.

        The triggering SYSTEM message and the dedicated audit row are the
        existing persisted authority for the causal-root/replacement relation.
        No task column is repurposed.  The caller must hold the teams registry
        lock after establishing that ``dispatcher`` is the team's manager.
        """
        now = _now().isoformat()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                """SELECT i.triggering_seq, i.dispatched_task_id,
                          m.system_payload_json
                     FROM thread_invocations i
                     JOIN threads t ON t.id = i.thread_id
                     LEFT JOIN thread_messages m
                       ON m.thread_id = i.thread_id AND m.seq = i.triggering_seq
                    WHERE i.invocation_token = ? AND i.thread_id = ?
                      AND i.agent_name = ? AND i.purpose = 'task_followup'
                      AND i.status = 'pending' AND t.status = 'open'""",
                (token, thread_id, dispatcher),
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return {"ok": False, "code": "task_followup_dispatch_stale"}
            if row["dispatched_task_id"] is not None:
                self._conn.rollback()
                return {"ok": False, "code": "task_followup_dispatch_already_used"}
            try:
                payload = json.loads(row["system_payload_json"] or "null")
            except (TypeError, json.JSONDecodeError):
                payload = None
            if not isinstance(payload, dict):
                self._conn.rollback()
                return {"ok": False, "code": "task_followup_dispatch_cause_invalid"}
            original_id = payload.get("original_task_id")
            source_id = payload.get("task_id")
            if (
                payload.get("kind_tag") not in {"task_completed", "task_failed", "task_escalated"}
                or not isinstance(original_id, str) or not original_id
                or not isinstance(source_id, str) or not source_id
            ):
                self._conn.rollback()
                return {"ok": False, "code": "task_followup_dispatch_cause_invalid"}
            source = self._conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (source_id,),
            ).fetchone()
            expected_status = payload.get("status")
            if (
                source is None
                or expected_status != source["status"]
                or source["status"] not in {
                    "completed", "failed", "cancelled", "superseded", "escalated",
                }
            ):
                self._conn.rollback()
                return {"ok": False, "code": "task_followup_dispatch_cause_invalid"}
            in_lineage = self._conn.execute(
                """WITH RECURSIVE lineage(id, parent_task_id, revisit_of_task_id, depth) AS (
                       SELECT id, parent_task_id, revisit_of_task_id, 0
                         FROM tasks WHERE id = ?
                       UNION
                       SELECT t.id, t.parent_task_id, t.revisit_of_task_id, l.depth + 1
                         FROM lineage l JOIN tasks t
                           ON t.id = l.parent_task_id OR t.id = l.revisit_of_task_id
                        WHERE l.depth < 399
                   ) SELECT 1 FROM lineage WHERE id = ?""",
                (source_id, original_id),
            ).fetchone()
            if in_lineage is None:
                self._conn.rollback()
                return {"ok": False, "code": "task_followup_dispatch_cause_invalid"}

            # The causal root must either be the original root dispatched from
            # this thread, or a root previously created by this exact contract.
            causal = self._conn.execute(
                "SELECT dispatched_from_thread_id FROM tasks WHERE id = ? AND parent_task_id IS NULL",
                (original_id,),
            ).fetchone()
            prior_replacement = self._conn.execute(
                """SELECT 1 FROM audit_log
                    WHERE task_id = ?
                      AND action = 'thread_task_followup_replacement_dispatched'""",
                (original_id,),
            ).fetchone()
            original_dispatch = self._conn.execute(
                """SELECT 1 FROM audit_log
                    WHERE task_id = ? AND action = 'thread_dispatch'
                      AND json_extract(payload, '$.task_id') = ?
                      AND json_extract(payload, '$.dispatcher') = ?""",
                (thread_id, original_id, dispatcher),
            ).fetchone()
            if causal is None or (
                causal["dispatched_from_thread_id"] != thread_id
                and prior_replacement is None
            ) or (prior_replacement is None and original_dispatch is None):
                self._conn.rollback()
                return {"ok": False, "code": "task_followup_dispatch_cause_invalid"}

            spent = prior_replacement is not None or self._conn.execute(
                """SELECT 1 FROM audit_log
                    WHERE action = 'thread_task_followup_replacement_dispatched'
                      AND json_extract(payload, '$.original_root_task_id') = ?""",
                (original_id,),
            ).fetchone() is not None
            if spent:
                self._conn.rollback()
                return {"ok": False, "code": "task_followup_dispatch_already_used"}

            params = (
                task.id, task.status.value, task.assigned_agent, task.team,
                task.brief, task.revision_count, task.created_at.isoformat(),
                task.updated_at.isoformat(), None, None, None,
                task.dispatched_from_thread_id, None, task.note,
                task.orchestration_step_count, task.session_timeout_seconds,
                task.task_type, task.active_fanout, task.current_session_id, None,
            )
            self._conn.execute(
                """INSERT INTO tasks (id, status, assigned_agent, team, brief,
                   revision_count, created_at, updated_at, completed_at, parent_task_id,
                   revisit_of_task_id, dispatched_from_thread_id, block_kind, note,
                   orchestration_step_count, session_timeout_seconds, task_type,
                   active_fanout, current_session_id, zombie_flagged_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                params,
            )
            sys_seq = self._append_thread_message_uncommitted(
                thread_id=thread_id, speaker=dispatcher,
                kind=ThreadMessageKind.SYSTEM,
                system_payload={
                    "kind_tag": "task_dispatched", "task_id": task.id,
                    "dispatcher": dispatcher, "target_agent": dispatcher,
                    "team": team, "brief_preview": task.brief[:160],
                    "replacement_for_task_id": original_id,
                },
            )
            changed = self._conn.execute(
                """UPDATE thread_invocations SET dispatched_task_id = ?
                     WHERE invocation_token = ? AND status = 'pending'
                       AND dispatched_task_id IS NULL""",
                (task.id, token),
            )
            if changed.rowcount != 1:
                self._conn.rollback()
                return {"ok": False, "code": "task_followup_dispatch_already_used"}
            audit_payload = {
                "thread_id": thread_id,
                "original_root_task_id": original_id,
                "replacement_root_task_id": task.id,
                "invocation_token_prefix": token[:8],
                "dispatcher": dispatcher,
                "team": team,
                "rule_version": "thr-225-v1",
            }
            self._conn.execute(
                "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) VALUES (?, ?, ?, ?, ?)",
                (task.id, dispatcher,
                 "thread_task_followup_replacement_dispatched",
                 json.dumps(audit_payload), now),
            )
            self._conn.execute(
                "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) VALUES (?, ?, ?, ?, ?)",
                (thread_id, dispatcher, "thread_dispatch", json.dumps({
                    "task_id": task.id, "dispatcher": dispatcher,
                    "target_agent": dispatcher, "team": team,
                    "replacement_for_task_id": original_id,
                }), now),
            )
            self._conn.commit()
            return {"ok": True, "system_message_seq": sys_seq,
                    "original_root_task_id": original_id}
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def fail_invocation(
        self, token: str, *, status: ThreadInvocationStatus, decline_reason: str
    ) -> bool:
        cursor = self._conn.execute(
            "UPDATE thread_invocations SET status = ?, decline_reason = ?, "
            "consumed_at = ? WHERE invocation_token = ? AND status = 'pending'",
            (status.value, decline_reason, _now().isoformat(), token),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def stamp_invocation_started(
        self, token: str, *, session_id: str | None
    ) -> None:
        self._conn.execute(
            "UPDATE thread_invocations SET started_at = ?, session_id = ? "
            "WHERE invocation_token = ? AND status = 'pending'",
            (_now().isoformat(), session_id, token),
        )
        self._conn.commit()

    @_synchronized
    def list_thread_invocations(
        self,
        thread_id: str,
        *,
        status: ThreadInvocationStatus | None = None,
    ) -> list[ThreadInvocation]:
        if status is not None:
            cursor = self._conn.execute(
                "SELECT * FROM thread_invocations "
                "WHERE thread_id = ? AND status = ? ORDER BY id",
                (thread_id, status.value),
            )
        else:
            cursor = self._conn.execute(
                "SELECT * FROM thread_invocations WHERE thread_id = ? ORDER BY id",
                (thread_id,),
            )
        return [self._row_to_invocation(r) for r in cursor.fetchall()]

    @_synchronized
    def list_pending_thread_invocations(self) -> list[ThreadInvocation]:
        """Return every org-wide ``pending`` thread invocation (any thread).

        Used by the portability preflight quiescence check: a pending reply/
        bootstrap/task-followup invocation is in-flight work and must block.
        """
        cursor = self._conn.execute(
            "SELECT * FROM thread_invocations "
            "WHERE status = ? ORDER BY id",
            (ThreadInvocationStatus.PENDING.value,),
        )
        return [self._row_to_invocation(r) for r in cursor.fetchall()]

    @_synchronized
    def list_started_invocations_for_agent(
        self, agent_name: str,
    ) -> list[tuple[str, str]]:
        """Return (invocation_token, thread_id) for pending invocations that
        have already started (``started_at`` is set) for ``agent_name``.
        """
        cursor = self._conn.execute(
            "SELECT invocation_token, thread_id FROM thread_invocations "
            "WHERE agent_name = ? AND status = 'pending' AND started_at IS NOT NULL",
            (agent_name,),
        )
        return [(row["invocation_token"], row["thread_id"]) for row in cursor.fetchall()]

    @_synchronized
    def decline_unstarted_invocations_for_agent(
        self, agent_name: str, *, decline_reason: str,
    ) -> int:
        """Decline all pending, not-yet-started invocations for ``agent_name``.

        Returns the number of rows updated.
        """
        now = _now().isoformat()
        cursor = self._conn.execute(
            "UPDATE thread_invocations "
            "SET status = ?, decline_reason = ?, consumed_at = ? "
            "WHERE agent_name = ? AND status = 'pending' AND started_at IS NULL",
            (ThreadInvocationStatus.DECLINED.value, decline_reason, now, agent_name),
        )
        self._conn.commit()
        return cursor.rowcount

    @_synchronized
    def list_invocations_for_thread_grouped_by_seq(
        self, thread_id: str
    ) -> dict[int, list[dict[str, object]]]:
        """Return {triggering_seq: [{agent_name, purpose, status, consumed_at}, ...]}
        for every REPLY and TASK_FOLLOWUP invocation in this thread.

        Used by GET /threads/{id} to build the per-message responder_status
        strip. Status values are the raw DB values (pending/consumed/declined/
        failed); the route's response builder renames consumed → replied.

        Each entry carries the authoritative ``purpose`` (''reply'' |
        ''task_followup'') so classification/dedup on the wire NEVER has to
        infer purpose from the triggering row's kind. A conversational REPLY
        invocation can hang off a SYSTEM row — the coalesced delivery range
        starts at the first unacknowledged sequence, which may be a system row
        (e.g. a resumed/terminal divider) rather than the founder message that
        caused the arrival. TASK_FOLLOWUP invocations hang off the SYSTEM row
        (task_completed / task_failed / task_escalated) that wakes a
        thread-dispatched agent (run_step._append_followup_system_and_reinvoke).
        Including TASK_FOLLOWUP lets the in-flight strip surface the woken agent
        on its system row. BOOTSTRAP is deliberately excluded — it has no
        triggering message row to attach a responder strip to.

        Note: ``consumed_at`` is set by both reply (``status='consumed'``) and
        decline (``status='declined'``) paths — the schema has no separate
        ``declined_at`` column. The wire ``responded_at`` field is sourced from
        this single timestamp regardless of which path consumed the invocation.
        """
        rows = self._conn.execute(
            "SELECT triggering_seq, agent_name, purpose, status, consumed_at, "
            "started_at, decline_reason "
            "FROM thread_invocations "
            "WHERE thread_id = ? AND purpose IN ('reply', 'task_followup') "
            "ORDER BY triggering_seq, agent_name",
            (thread_id,),
        ).fetchall()
        grouped: dict[int, list[dict[str, object]]] = {}
        for r in rows:
            entry = {
                "agent_name": r["agent_name"],
                "purpose": r["purpose"],
                "status": r["status"],
                "consumed_at": r["consumed_at"],
                "started_at": r["started_at"],
                "decline_reason": r["decline_reason"],
            }
            grouped.setdefault(r["triggering_seq"], []).append(entry)
        return grouped

    @_synchronized
    def count_pending_turn_obligations(self, thread_id: str) -> int:
        """Count pending invocations that represent future turn obligations.

        REPLY, BOOTSTRAP, TASK_FOLLOWUP count.

        No current callers in production routes — kept as a documented API.
        After the broadcast-only routing change (spec §7, "invite is free"),
        the /invite projection was dropped entirely; /send and /compose use
        a simpler turns_used + 1 projection; the task-followup auto-extend
        path (mint_followup_invocation_with_cap_extend) inlines its own
        pending-count SQL. Unit tests exercise this helper directly.
        """
        counted = (
            ThreadInvocationPurpose.REPLY.value,
            ThreadInvocationPurpose.BOOTSTRAP.value,
            ThreadInvocationPurpose.TASK_FOLLOWUP.value,
        )
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM thread_invocations "
            "WHERE thread_id = ? AND status = ? AND purpose IN ({})".format(
                ",".join("?" * len(counted))
            ),
            (thread_id, ThreadInvocationStatus.PENDING.value, *counted),
        ).fetchone()
        return int(row["n"])

    @_synchronized
    def reap_pending_invocations(
        self,
        thread_id: str,
        *,
        purposes: list[ThreadInvocationPurpose] | None = None,
        decline_reason: str,
    ) -> int:
        now = _now().isoformat()
        if purposes is None:
            cursor = self._conn.execute(
                "UPDATE thread_invocations SET status = 'failed', "
                "decline_reason = ?, consumed_at = ? "
                "WHERE thread_id = ? AND status = 'pending'",
                (decline_reason, now, thread_id),
            )
        else:
            placeholders = ",".join("?" * len(purposes))
            values = [decline_reason, now, thread_id] + [p.value for p in purposes]
            cursor = self._conn.execute(
                f"UPDATE thread_invocations SET status = 'failed', "
                f"decline_reason = ?, consumed_at = ? "
                f"WHERE thread_id = ? AND status = 'pending' "
                f"AND purpose IN ({placeholders})",
                values,
            )
        self._conn.commit()
        return cursor.rowcount

    # ── GitHub #688 Phase 1 Slice A: reply delivery state store ──────────
    #
    # HANDOFF CONTRACT (Slice B):
    #   These primitives are intentionally UNHOOKED in Slice A. Slice B MUST
    #   call them atomically with its route/runner activation:
    #     * cutover_thread_reply_delivery_state(thread_id) — once per thread at
    #       activation (and again on reopen; it is idempotent) to seed/coalesce
    #       per-pair state from any legacy pending REPLY rows.
    #     * recover_reply_delivery_state() — at startup, before thread workers
    #       start, replacing the conversational REPLY portion of
    #       _sweep_on_startup's generic reaper (Branch 6). Enqueue the returned
    #       tokens AFTER commit. BOOTSTRAP and TASK_FOLLOWUP keep the generic
    #       reaper's daemon_restart semantics.
    #   The claim (queued → running CAS) and settlement primitives are Slice B;
    #   routes/runner must NOT open-code the queued/running token transitions.

    def _row_to_reply_delivery_state(self, row) -> ThreadReplyDeliveryState:
        return ThreadReplyDeliveryState(
            thread_id=row["thread_id"],
            agent_name=row["agent_name"],
            acknowledged_through_seq=int(row["acknowledged_through_seq"] or 0),
            required_through_seq=int(row["required_through_seq"] or 0),
            queued_invocation_token=row["queued_invocation_token"],
            running_invocation_token=row["running_invocation_token"],
            running_from_seq=row["running_from_seq"],
            running_through_seq=row["running_through_seq"],
            last_terminal_reason=row["last_terminal_reason"],
            last_terminal_at=row["last_terminal_at"],
            updated_at=row["updated_at"],
        )

    @_synchronized
    def get_reply_delivery_state(
        self, thread_id: str, agent_name: str,
    ) -> ThreadReplyDeliveryState | None:
        cursor = self._conn.execute(
            "SELECT * FROM thread_reply_delivery_state "
            "WHERE thread_id = ? AND agent_name = ?",
            (thread_id, agent_name),
        )
        row = cursor.fetchone()
        return self._row_to_reply_delivery_state(row) if row else None

    def _row_to_reply_breaker_episode(self, row) -> ThreadReplyBreakerEpisode:
        return ThreadReplyBreakerEpisode(
            thread_id=row["thread_id"], agent_name=row["agent_name"],
            executor_key=row["executor_key"], episode_id=row["episode_id"],
            state=row["state"], consecutive_failures=row["consecutive_failures"],
            opened_at=row["opened_at"], cooldown_until=row["cooldown_until"],
            probe_lease_id=row["probe_lease_id"],
            last_failure_category=row["last_failure_category"],
            updated_at=row["updated_at"],
        )

    @_synchronized
    def get_thread_reply_breaker(
        self, thread_id: str, agent_name: str, executor_key: str,
    ) -> ThreadReplyBreakerEpisode | None:
        """Return the durable episode; absence means CLOSED."""
        row = self._conn.execute(
            "SELECT * FROM thread_reply_breaker_episodes WHERE thread_id = ? "
            "AND agent_name = ? AND executor_key = ?",
            (thread_id, agent_name, executor_key),
        ).fetchone()
        return self._row_to_reply_breaker_episode(row) if row else None

    @_synchronized
    def record_thread_reply_breaker_failure(
        self, *, thread_id: str, agent_name: str, executor_key: str,
        invocation_token: str, failure_category: str, threshold: int,
        cooldown_seconds: int, now: datetime | None = None,
    ) -> ThreadReplyBreakerEpisode:
        """Count one qualifying final launched-provider outcome exactly once."""
        if threshold < 1 or cooldown_seconds < 1:
            raise ValueError("breaker threshold and cooldown must be positive")
        at = (now or _now()).astimezone(timezone.utc)
        at_s = at.isoformat()
        transition_action: str | None = None
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            existing_receipt = self._conn.execute(
                "SELECT episode_id FROM thread_reply_breaker_receipts "
                "WHERE invocation_token = ?", (invocation_token,),
            ).fetchone()
            row = self._conn.execute(
                "SELECT * FROM thread_reply_breaker_episodes WHERE thread_id = ? "
                "AND agent_name = ? AND executor_key = ?",
                (thread_id, agent_name, executor_key),
            ).fetchone()
            if existing_receipt is not None:
                if row is None or row["episode_id"] != existing_receipt["episode_id"]:
                    raise RuntimeError("breaker receipt continuity mismatch")
                self._conn.commit()
                return self._row_to_reply_breaker_episode(row)
            if row is None or (
                row["state"] == "closed" and int(row["consecutive_failures"]) == 0
            ):
                episode_id = f"brep-{uuid.uuid4().hex}"
                failures = 1
                state = "open" if failures >= threshold else "closed"
                if state == "open":
                    transition_action = "thread_reply_breaker_opened"
                opened_at = at_s if state == "open" else None
                cooldown_until = (
                    (at + timedelta(seconds=cooldown_seconds)).isoformat()
                    if state == "open" else None
                )
                self._conn.execute(
                    "INSERT INTO thread_reply_breaker_episodes "
                    "(thread_id,agent_name,executor_key,episode_id,state,"
                    "consecutive_failures,opened_at,cooldown_until,probe_lease_id,"
                    "last_failure_category,updated_at) VALUES (?,?,?,?,?,?,?,?,NULL,?,?) "
                    "ON CONFLICT(thread_id,agent_name,executor_key) DO UPDATE SET "
                    "episode_id=excluded.episode_id,state=excluded.state,"
                    "consecutive_failures=excluded.consecutive_failures,"
                    "opened_at=excluded.opened_at,cooldown_until=excluded.cooldown_until,"
                    "probe_lease_id=NULL,last_failure_category=excluded.last_failure_category,"
                    "updated_at=excluded.updated_at",
                    (thread_id, agent_name, executor_key, episode_id, state,
                     failures, opened_at, cooldown_until, failure_category, at_s),
                )
            else:
                episode_id = row["episode_id"]
                failures = int(row["consecutive_failures"]) + 1
                state = "open" if failures >= threshold else row["state"]
                opened_at = row["opened_at"] or (at_s if state == "open" else None)
                cooldown_until = row["cooldown_until"]
                if state == "open" and row["state"] != "open":
                    cooldown_until = (at + timedelta(seconds=cooldown_seconds)).isoformat()
                    transition_action = "thread_reply_breaker_opened"
                if row["state"] == "probe":
                    state = "open"
                    transition_action = "thread_reply_breaker_reopened"
                    opened_at = at_s
                    cooldown_until = (at + timedelta(seconds=cooldown_seconds)).isoformat()
                self._conn.execute(
                    "UPDATE thread_reply_breaker_episodes SET state=?,"
                    "consecutive_failures=?,opened_at=?,cooldown_until=?,"
                    "probe_lease_id=NULL,last_failure_category=?,updated_at=? "
                    "WHERE episode_id=?",
                    (state, failures, opened_at, cooldown_until,
                     failure_category, at_s, episode_id),
                )
            self._conn.execute(
                "INSERT INTO thread_reply_breaker_receipts "
                "(invocation_token,episode_id,outcome,failure_category,recorded_at) "
                "VALUES (?,?,'failure',?,?)",
                (invocation_token, episode_id, failure_category, at_s),
            )
            if transition_action is not None:
                self.insert_audit_log_uncommitted(
                    task_id=thread_id, agent=agent_name,
                    action=transition_action,
                    payload={
                        "episode_id": episode_id,
                        "failure_category": failure_category,
                        "consecutive_failures": failures,
                    },
                )
            result = self._conn.execute(
                "SELECT * FROM thread_reply_breaker_episodes WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
            self._conn.commit()
            return self._row_to_reply_breaker_episode(result)
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def acquire_thread_reply_breaker_probe(
        self, *, thread_id: str, agent_name: str, executor_key: str,
        lease_id: str, now: datetime | None = None,
    ) -> ThreadReplyBreakerEpisode | None:
        """Acquire the single durable half-open lease when its cooldown is due."""
        at_s = (now or _now()).astimezone(timezone.utc).isoformat()
        cursor = self._conn.execute(
            "UPDATE thread_reply_breaker_episodes SET state='probe',"
            "probe_lease_id=?,updated_at=? WHERE thread_id=? AND agent_name=? "
            "AND executor_key=? AND state='open' AND cooldown_until<=?",
            (lease_id, at_s, thread_id, agent_name, executor_key, at_s),
        )
        self._conn.commit()
        if cursor.rowcount != 1:
            return None
        return self.get_thread_reply_breaker(thread_id, agent_name, executor_key)

    @_synchronized
    def mint_due_thread_reply_breaker_probes(
        self, *, now: datetime | None = None,
        no_episode_executor_keys: dict[tuple[str, str], str] | None = None,
        cooldown_seconds: int = 900,
    ) -> list[ThreadReplyRecoveryEntry]:
        """Atomically lease and mint timer-driven probes that are actually runnable.

        Startup/gap recovery can leave a durable obligation with neither an
        owner slot nor a breaker episode.  Such a pair is admitted only as a
        cooldown-aged probe, using the scheduler-resolved current continuity
        key; it is never silently converted into an ordinary CLOSED launch.
        """
        if cooldown_seconds < 1:
            raise ValueError("breaker cooldown must be positive")
        at = (now or _now()).astimezone(timezone.utc)
        at_s = at.isoformat()
        results: list[ThreadReplyRecoveryEntry] = []
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            # A prior tick may have committed the durable probe lease/token but
            # failed or been cancelled before publishing to the process queue.
            # Re-emit every still-pending queued probe on every tick. The
            # process queue deduplicates publication and the invocation claim
            # CAS remains the exactly-once launch boundary.
            for queued in self._conn.execute(
                "SELECT b.thread_id,b.agent_name,d.queued_invocation_token "
                "FROM thread_reply_breaker_episodes b JOIN "
                "thread_reply_delivery_state d ON d.thread_id=b.thread_id AND "
                "d.agent_name=b.agent_name JOIN thread_invocations i ON "
                "i.invocation_token=d.queued_invocation_token "
                "WHERE b.state='probe' AND b.probe_lease_id IS NOT NULL AND "
                "d.queued_invocation_token IS NOT NULL AND i.status='pending' "
                "ORDER BY b.thread_id,b.agent_name"
            ).fetchall():
                results.append(ThreadReplyRecoveryEntry(
                    thread_id=queued["thread_id"],
                    agent_name=queued["agent_name"],
                    invocation_token=queued["queued_invocation_token"],
                    kind="breaker_probe",
                ))
            cutoff_s = (at - timedelta(seconds=cooldown_seconds)).isoformat()
            for candidate in self._conn.execute(
                "SELECT d.thread_id,d.agent_name,d.updated_at FROM "
                "thread_reply_delivery_state d JOIN threads t ON t.id=d.thread_id "
                "AND t.status='open' JOIN thread_participants p ON "
                "p.thread_id=d.thread_id AND p.agent_name=d.agent_name WHERE "
                "d.required_through_seq>d.acknowledged_through_seq AND "
                "d.queued_invocation_token IS NULL AND "
                "d.running_invocation_token IS NULL AND d.updated_at<=? AND "
                "NOT EXISTS (SELECT 1 FROM thread_reply_breaker_episodes b "
                "WHERE b.thread_id=d.thread_id AND b.agent_name=d.agent_name) "
                "ORDER BY d.updated_at,d.thread_id,d.agent_name",
                (cutoff_s,),
            ).fetchall():
                pair = (candidate["thread_id"], candidate["agent_name"])
                executor_key = (no_episode_executor_keys or {}).get(pair)
                if executor_key is None:
                    continue
                held = self._conn.execute(
                    "SELECT 1 FROM thread_reply_exchange e JOIN "
                    "thread_exchange_deferrals x ON x.thread_id=e.thread_id "
                    "AND x.exchange_id=e.exchange_id WHERE e.thread_id=? AND "
                    "e.state='open' AND x.agent_name=? AND x.state='held' LIMIT 1",
                    pair,
                ).fetchone()
                if held is not None:
                    continue
                episode_id = f"brep-{uuid.uuid4().hex}"
                self._conn.execute(
                    "INSERT INTO thread_reply_breaker_episodes "
                    "(thread_id,agent_name,executor_key,episode_id,state,"
                    "consecutive_failures,opened_at,cooldown_until,probe_lease_id,"
                    "last_failure_category,updated_at) VALUES "
                    "(?,?,?,?,'open',0,?,?,NULL,NULL,?)",
                    (candidate["thread_id"], candidate["agent_name"], executor_key,
                     episode_id, candidate["updated_at"], candidate["updated_at"], at_s),
                )
            rows = self._conn.execute(
                "SELECT b.*,d.acknowledged_through_seq,d.required_through_seq,"
                "d.queued_invocation_token,d.running_invocation_token "
                "FROM thread_reply_breaker_episodes b "
                "JOIN thread_reply_delivery_state d ON d.thread_id=b.thread_id "
                "AND d.agent_name=b.agent_name "
                "JOIN threads t ON t.id=b.thread_id AND t.status='open' "
                "JOIN thread_participants p ON p.thread_id=b.thread_id "
                "AND p.agent_name=b.agent_name "
                "WHERE b.state='open' AND b.cooldown_until<=? "
                "ORDER BY b.cooldown_until,b.thread_id,b.agent_name,b.executor_key",
                (at_s,),
            ).fetchall()
            for row in rows:
                if (row["queued_invocation_token"] is not None
                        or row["running_invocation_token"] is not None
                        or int(row["required_through_seq"] or 0)
                        <= int(row["acknowledged_through_seq"] or 0)):
                    continue
                held = self._conn.execute(
                    "SELECT 1 FROM thread_reply_exchange e JOIN "
                    "thread_exchange_deferrals d ON d.thread_id=e.thread_id "
                    "AND d.exchange_id=e.exchange_id WHERE e.thread_id=? "
                    "AND e.state='open' AND d.agent_name=? AND d.state='held' LIMIT 1",
                    (row["thread_id"], row["agent_name"]),
                ).fetchone()
                if held is not None:
                    continue
                lease_id = f"brlease-{uuid.uuid4().hex}"
                token = self._mint_reply_invocation_uncommitted(
                    row["thread_id"], row["agent_name"],
                    int(row["acknowledged_through_seq"] or 0) + 1,
                )
                changed = self._conn.execute(
                    "UPDATE thread_reply_breaker_episodes SET state='probe',"
                    "probe_lease_id=?,updated_at=? WHERE episode_id=? AND state='open'",
                    (lease_id, at_s, row["episode_id"]),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("breaker probe lease CAS lost inside transaction")
                self._conn.execute(
                    "UPDATE thread_reply_delivery_state SET queued_invocation_token=?,"
                    "updated_at=? WHERE thread_id=? AND agent_name=?",
                    (token, at_s, row["thread_id"], row["agent_name"]),
                )
                self.insert_audit_log_uncommitted(
                    task_id=row["thread_id"], agent=row["agent_name"],
                    action="thread_reply_breaker_probe_started",
                    payload={"episode_id": row["episode_id"], "category": row["last_failure_category"]},
                )
                results.append(ThreadReplyRecoveryEntry(
                    thread_id=row["thread_id"], agent_name=row["agent_name"],
                    invocation_token=token, kind="breaker_probe",
                ))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return results

    @_synchronized
    def close_thread_reply_breaker(
        self, *, thread_id: str, agent_name: str, executor_key: str,
        now: datetime | None = None,
    ) -> bool:
        """Close/rearm one continuity identity without deleting episode history."""
        at_s = (now or _now()).astimezone(timezone.utc).isoformat()
        cursor = self._conn.execute(
            "UPDATE thread_reply_breaker_episodes SET state='closed',"
            "consecutive_failures=0,opened_at=NULL,cooldown_until=NULL,"
            "probe_lease_id=NULL,last_failure_category=NULL,updated_at=? "
            "WHERE thread_id=? AND agent_name=? AND executor_key=?",
            (at_s, thread_id, agent_name, executor_key),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def close_thread_reply_breakers_except(
        self, *, thread_id: str, agent_name: str, executor_key: str,
        now: datetime | None = None,
    ) -> int:
        """Close old executor/model/config continuity before a fresh launch."""
        at_s = (now or _now()).astimezone(timezone.utc).isoformat()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                "SELECT episode_id FROM thread_reply_breaker_episodes WHERE "
                "thread_id=? AND agent_name=? AND executor_key<>? "
                "AND state IN ('open','probe')",
                (thread_id, agent_name, executor_key),
            ).fetchall()
            for row in rows:
                self.insert_audit_log_uncommitted(
                    task_id=thread_id, agent=agent_name,
                    action="thread_reply_breaker_closed",
                    payload={"episode_id": row["episode_id"], "reason": "continuity_switch"},
                )
            cursor = self._conn.execute(
                "UPDATE thread_reply_breaker_episodes SET state='closed',"
                "consecutive_failures=0,opened_at=NULL,cooldown_until=NULL,"
                "probe_lease_id=NULL,last_failure_category=NULL,updated_at=? "
                "WHERE thread_id=? AND agent_name=? AND executor_key<>? "
                "AND state IN ('open','probe')",
                (at_s, thread_id, agent_name, executor_key),
            )
            self._conn.commit()
            return cursor.rowcount
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def settle_thread_reply_breaker_success(
        self, *, thread_id: str, agent_name: str, executor_key: str,
        invocation_token: str, episode_id: str, probe_lease_id: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Idempotently settle success without allowing stale episode closure.

        Probe completions additionally bind to the unique durable lease.  A
        callback from an older episode or lease is a truthful no-op.
        """
        at_s = (now or _now()).astimezone(timezone.utc).isoformat()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            receipt = self._conn.execute(
                "SELECT episode_id,outcome FROM thread_reply_breaker_receipts "
                "WHERE invocation_token=?", (invocation_token,),
            ).fetchone()
            if receipt is not None:
                self._conn.commit()
                return receipt["episode_id"] == episode_id and receipt["outcome"] == "success"
            row = self._conn.execute(
                "SELECT state,probe_lease_id FROM thread_reply_breaker_episodes "
                "WHERE thread_id=? AND agent_name=? AND executor_key=? AND episode_id=?",
                (thread_id, agent_name, executor_key, episode_id),
            ).fetchone()
            if row is None or (
                probe_lease_id is not None
                and (row["state"] != "probe" or row["probe_lease_id"] != probe_lease_id)
            ):
                self._conn.commit()
                return False
            self._conn.execute(
                "INSERT INTO thread_reply_breaker_receipts "
                "(invocation_token,episode_id,outcome,failure_category,recorded_at) "
                "VALUES (?,?,'success',NULL,?)",
                (invocation_token, episode_id, at_s),
            )
            self._conn.execute(
                "UPDATE thread_reply_breaker_episodes SET state='closed',"
                "consecutive_failures=0,opened_at=NULL,cooldown_until=NULL,"
                "probe_lease_id=NULL,last_failure_category=NULL,updated_at=? "
                "WHERE episode_id=?",
                (at_s, episode_id),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def list_reply_delivery_states(self) -> list[ThreadReplyDeliveryState]:
        """Every per-pair reply delivery state row (diagnostic surface)."""
        cursor = self._conn.execute(
            "SELECT * FROM thread_reply_delivery_state "
            "ORDER BY thread_id, agent_name",
        )
        return [self._row_to_reply_delivery_state(r) for r in cursor.fetchall()]

    def _thread_tail_seq(self, thread_id: str) -> int:
        """Highest transcript seq for ``thread_id`` (0 for an empty thread)."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS tail "
            "FROM thread_messages WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        return int(row["tail"])

    def _mint_reply_invocation_uncommitted(
        self, thread_id: str, agent_name: str, triggering_seq: int,
    ) -> str:
        """INSERT a pending REPLY invocation row inside the open transaction.

        Mirrors ``mint_thread_invocation`` without committing so the caller can
        bundle the mint with the state transition in one atomic transaction.
        Returns the generated invocation token.
        """
        import uuid as _uuid
        token = _uuid.uuid4().hex
        now = _now().isoformat()
        self._conn.execute(
            "INSERT INTO thread_invocations (thread_id, agent_name, "
            "invocation_token, triggering_seq, purpose, status, enqueued_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (thread_id, agent_name, token, triggering_seq,
             ThreadInvocationPurpose.REPLY.value, now),
        )
        return token

    @_synchronized
    def cutover_thread_reply_delivery_state(
        self, thread_id: str,
    ) -> list[ThreadReplyDeliveryState]:
        """Idempotently initialize per-pair reply delivery state for a thread.

        Callable explicitly by Slice B (per thread at activation / on reopen);
        Slice A does NOT auto-run it. For every CURRENT participant pair that
        has no state row yet:

          * no legacy pending REPLY → seed acknowledged_through_seq and
            required_through_seq to the thread tail (no queued/running token):
            Phase 1 starts at cutover and creates no historic work.
          * legacy pending REPLY(s) → derive ``from_seq`` = MIN(triggering_seq)
            across that pair's pending REPLYs; terminalize exactly those rows
            (status='failed', decline_reason='coalesced_cutover'); mint exactly
            one replacement pending REPLY and record it as queued, covering
            ``from_seq`` .. current tail (acknowledged = from_seq - 1,
            required = tail).

        Idempotent: a pair that already has a state row is left untouched, so
        repeat invocation/reopen never duplicates a queued wake or
        re-terminalizes rows. Only REPLY rows are touched — TASK_FOLLOWUP and
        BOOTSTRAP are never terminalized or minted here, and
        ``last_resumed_seq`` is never read.
        """
        now = _now().isoformat()
        created: list[ThreadReplyDeliveryState] = []
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            # Every cutoff-defining read (tail, participants, state existence,
            # legacy pending REPLY selection) runs AFTER the write lock is held
            # so a concurrent append cannot commit between the snapshot and the
            # state commit (a torn snapshot would drop a message from the
            # required range).
            tail = self._thread_tail_seq(thread_id)
            participants = [
                p.agent_name for p in self.list_thread_participants(thread_id)
            ]
            for agent_name in participants:
                existing = self._conn.execute(
                    "SELECT 1 FROM thread_reply_delivery_state "
                    "WHERE thread_id = ? AND agent_name = ?",
                    (thread_id, agent_name),
                ).fetchone()
                if existing is not None:
                    continue  # already cut over — idempotent no-op

                # Legacy pending REPLYs for this pair (never BOOTSTRAP /
                # TASK_FOLLOWUP).
                legacy = self._conn.execute(
                    "SELECT MIN(triggering_seq) AS from_seq "
                    "FROM thread_invocations "
                    "WHERE thread_id = ? AND agent_name = ? "
                    "AND status = 'pending' AND purpose = 'reply'",
                    (thread_id, agent_name),
                ).fetchone()
                from_seq = legacy["from_seq"]
                if from_seq is not None:
                    # Terminalize exactly those legacy pending REPLYs with an
                    # explicit coalesced_cutover receipt.
                    self._conn.execute(
                        "UPDATE thread_invocations SET status = 'failed', "
                        "decline_reason = 'coalesced_cutover', consumed_at = ? "
                        "WHERE thread_id = ? AND agent_name = ? "
                        "AND status = 'pending' AND purpose = 'reply'",
                        (now, thread_id, agent_name),
                    )
                    # Mint exactly one replacement queued REPLY covering
                    # from_seq .. tail.
                    token = self._mint_reply_invocation_uncommitted(
                        thread_id, agent_name, from_seq,
                    )
                    self._conn.execute(
                        "INSERT INTO thread_reply_delivery_state "
                        "(thread_id, agent_name, acknowledged_through_seq, "
                        "required_through_seq, queued_invocation_token, "
                        "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (thread_id, agent_name, from_seq - 1, tail, token, now),
                    )
                else:
                    # No legacy pending REPLY: seed to tail, nothing queued.
                    self._conn.execute(
                        "INSERT INTO thread_reply_delivery_state "
                        "(thread_id, agent_name, acknowledged_through_seq, "
                        "required_through_seq, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (thread_id, agent_name, tail, tail, now),
                    )
                row = self._conn.execute(
                    "SELECT * FROM thread_reply_delivery_state "
                    "WHERE thread_id = ? AND agent_name = ?",
                    (thread_id, agent_name),
                ).fetchone()
                created.append(self._row_to_reply_delivery_state(row))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return created

    def _running_recovery_fail_reason(
        self, inv, *, same_pair: bool, right_purpose: bool,
        pending: bool, started: bool, range_ok: bool,
    ) -> str:
        """Truthful fail-closed diagnostic for a non-recoverable running slot.

        Ordered so the most specific cause wins while keeping the distinct
        terminal vs malformed vs ownership causes that Slice B's retry
        projection and audit settlement depend on.
        """
        if inv is None or not same_pair or not right_purpose:
            return "invalid_running_token_on_recovery"
        if not pending:
            return "running_already_terminal_on_recovery"
        if not range_ok:
            return "malformed_running_range_on_recovery"
        if not started:
            return "running_missing_start_evidence_on_recovery"
        return "invalid_running_token_on_recovery"

    @_synchronized
    def recover_reply_delivery_state(self) -> list[ThreadReplyRecoveryEntry]:
        """Durable reply-delivery recovery (Slice A ships it UNHOOKED).

        Slice B must call this at startup (before thread workers start) and
        enqueue the returned tokens after commit, replacing the conversational
        REPLY portion of the generic reaper. Contract per state row:

          * queued token set, running clear → validate it is a pending
            UNSTARTED same-pair REPLY (the claim CAS enforces the same
            precondition). Valid → retain and return it. A queued receipt
            with started_at set (malformed/crash-window state) fails closed
            with a PAIR-SCOPED sweep: retire every owned pending REPLY
            receipt, clear the queued slot, preserve required_through_seq,
            never mint/return a replacement — the next conversational arrival
            mints the single covering wake. Any other invalid queued token →
            fail closed: clear the queued slot, record a diagnostic, return
            nothing.
          * both ownership slots populated → corruption. Fail closed: clear
            both slots, record a diagnostic, return nothing, never mint.
          * running token set → recoverable ONLY when the receipt is owned by
            this pair, is a REPLY, is still PENDING (the expected interrupted
            in-flight status), carries started evidence, and its durable range
            is internally consistent (acknowledged <= running_from <=
            running_through <= required). Recoverable → terminalize ONLY that
            owned attempt as daemon_restart, preserve the unacknowledged
            required range, clear running, mint/record exactly one replacement
            queued REPLY. Otherwise (consumed/failed/declined terminal,
            missing, wrong-pair, wrong-purpose, malformed range, missing start)
            → fail closed: clear the running slot, record a truthful
            diagnostic, never mint/return a runnable token.

        Pending released-deferral catch-up markers (``catchup_pending = 1``)
        are resolved by the post-loop reconciliation in the same transaction —
        consumed by a covering slot, minted exactly once when the blocking slot
        is terminal with no covering replacement, or cleared when fully
        covered. Every FAIL-CLOSED branch above (both-slots corruption,
        non-recoverable running slot, invalid queued started/ownership)
        revokes the pair's pending marker ATOMICALLY in that same transaction,
        so the reconciliation never mints a catch-up from state the branch
        declared untrustworthy (TASK-6065): the marker survives only when the
        blocking slot's terminal outcome was legitimately observed (settlement
        or a recoverable restart terminalization).

        Repeat recovery is idempotent: after a running row is replaced its slot
        holds a queued token, so a second pass retains rather than re-mints.
        BOOTSTRAP / TASK_FOLLOWUP rows are never touched.
        """
        now = _now().isoformat()
        results: list[ThreadReplyRecoveryEntry] = []
        rows = self._conn.execute(
            "SELECT * FROM thread_reply_delivery_state "
            "WHERE queued_invocation_token IS NOT NULL "
            "OR running_invocation_token IS NOT NULL "
            "ORDER BY thread_id, agent_name",
        ).fetchall()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                thread_id = row["thread_id"]
                agent_name = row["agent_name"]
                running_token = row["running_invocation_token"]
                queued_token = row["queued_invocation_token"]

                if running_token is not None and queued_token is not None:
                    # Both ownership slots populated: the mutually-exclusive
                    # claim/settle invariant was violated. Fail closed with a
                    # transactionally atomic PAIR-SCOPED sweep: retire EVERY
                    # invocation owned by this corrupt row's (thread_id,
                    # agent_name) pair that is purpose REPLY and status PENDING
                    # — including unreferenced same-pair pending receipts the
                    # slots never pointed at — so no duplicate/orphaned pending
                    # REPLY survives once Slice B replaces generic reaping. The
                    # sweep is gated on the pair, purpose='reply', and
                    # status='pending', so a foreign-pair, wrong-purpose,
                    # missing, or already-terminal receipt (even one referenced
                    # by a corrupt slot) is never mutated. No blanket global
                    # reaper is issued. Then clear both slots, record a
                    # truthful corruption diagnostic, and never mint or return
                    # a runnable replacement.
                    swept = self._conn.execute(
                        "UPDATE thread_invocations SET status = 'failed', "
                        "decline_reason = 'corrupt_both_slots_on_recovery', "
                        "consumed_at = ? "
                        "WHERE thread_id = ? AND agent_name = ? "
                        "AND status = 'pending' AND purpose = 'reply'",
                        (now, thread_id, agent_name),
                    )
                    self._conn.execute(
                        "UPDATE thread_reply_delivery_state SET "
                        "queued_invocation_token = NULL, "
                        "running_invocation_token = NULL, "
                        "running_from_seq = NULL, running_through_seq = NULL, "
                        "last_terminal_reason = ?, last_terminal_at = ?, "
                        "updated_at = ? WHERE thread_id = ? AND agent_name = ?",
                        ("corrupt_both_slots_on_recovery", now, now,
                         thread_id, agent_name),
                    )
                    # TASK-6065: revoke any pending released-deferral catch-up
                    # marker ATOMICALLY in the same transaction. The marker is
                    # a promise to mint exactly one post-slot catch-up; this
                    # fail-closed branch declared the pair's ownership state
                    # untrustworthy and explicitly never mints, so the marker
                    # must not survive into the post-loop reconciliation where
                    # it would fabricate runnable ownership from corrupt state
                    # (TASK-6063 reproduction). The revocation is audited with
                    # the corruption diagnostic so the suppression is truthful.
                    suppressed = self._clear_catchup_pending_uncommitted(
                        thread_id, agent_name,
                    )
                    # One truthful cancelled audit per corrupted pair — the
                    # pair-scoped sweep retired its owned obligations with a
                    # diagnostic reason; never mint a replacement.
                    self._emit_reply_wake_audit(
                        thread_id=thread_id, agent_name=agent_name,
                        action="thread_reply_wake_cancelled",
                        payload={
                            "agent_name": agent_name,
                            "boundary_seq": int(row["required_through_seq"] or 0),
                            "reason": "corrupt_both_slots_on_recovery",
                            "swept_count": swept.rowcount,
                            "catchup_suppressed": suppressed > 0,
                        },
                    )
                    continue

                if running_token is not None:
                    inv = self._conn.execute(
                        "SELECT * FROM thread_invocations "
                        "WHERE invocation_token = ?",
                        (running_token,),
                    ).fetchone()
                    same_pair = (
                        inv is not None
                        and inv["thread_id"] == thread_id
                        and inv["agent_name"] == agent_name
                    )
                    right_purpose = (
                        inv is not None
                        and inv["purpose"] == ThreadInvocationPurpose.REPLY.value
                    )
                    pending = (
                        inv is not None
                        and inv["status"] == ThreadInvocationStatus.PENDING.value
                    )
                    started = inv is not None and inv["started_at"] is not None

                    acknowledged = int(row["acknowledged_through_seq"] or 0)
                    required = int(row["required_through_seq"] or 0)
                    running_from = row["running_from_seq"]
                    running_through = row["running_through_seq"]
                    range_ok = (
                        running_from is not None
                        and running_through is not None
                        and acknowledged <= running_from
                        and running_from <= running_through
                        and running_through <= required
                    )

                    recoverable = (
                        same_pair and right_purpose and pending
                        and started and range_ok
                    )

                    if not recoverable:
                        reason = self._running_recovery_fail_reason(
                            inv, same_pair=same_pair,
                            right_purpose=right_purpose, pending=pending,
                            started=started, range_ok=range_ok,
                        )
                        # Fail closed: clear the running slot (never leave an
                        # ownership slot referencing a terminal/mismatched/
                        # malformed attempt), never mint a replacement, never
                        # return a runnable token. The invocation row itself is
                        # left untouched so truthful terminal diagnostics survive.
                        self._conn.execute(
                            "UPDATE thread_reply_delivery_state SET "
                            "running_invocation_token = NULL, "
                            "running_from_seq = NULL, "
                            "running_through_seq = NULL, "
                            "last_terminal_reason = ?, last_terminal_at = ?, "
                            "updated_at = ? WHERE thread_id = ? AND agent_name = ?",
                            (reason, now, now, thread_id, agent_name),
                        )
                        # TASK-6065: the non-recoverable running slot is an
                        # invalid/malformed ownership classification — the
                        # state is untrustworthy, so any pending released-
                        # deferral catch-up marker is revoked in the same
                        # transaction (never minted by the post-loop scan).
                        self._clear_catchup_pending_uncommitted(
                            thread_id, agent_name,
                        )
                        continue

                    # Recoverable interrupted in-flight attempt: terminalize
                    # ONLY the owned pending receipt as daemon_restart, preserve
                    # the unacknowledged required range, clear running,
                    # mint/record exactly one replacement queued REPLY.
                    self._conn.execute(
                        "UPDATE thread_invocations SET status = 'failed', "
                        "decline_reason = 'daemon_restart', consumed_at = ? "
                        "WHERE invocation_token = ? AND status = 'pending'",
                        (now, running_token),
                    )
                    replacement = self._mint_reply_invocation_uncommitted(
                        thread_id, agent_name, acknowledged + 1,
                    )
                    self._conn.execute(
                        "UPDATE thread_reply_delivery_state SET "
                        "running_invocation_token = NULL, "
                        "running_from_seq = NULL, "
                        "running_through_seq = NULL, "
                        "queued_invocation_token = ?, "
                        "last_terminal_reason = 'daemon_restart', "
                        "last_terminal_at = ?, updated_at = ? "
                        "WHERE thread_id = ? AND agent_name = ?",
                        (replacement, now, now, thread_id, agent_name),
                    )
                    self._emit_reply_wake_audit(
                        thread_id=thread_id, agent_name=agent_name,
                        action="thread_reply_wake_recovered",
                        payload={
                            "agent_name": agent_name,
                            "kind": "replacement_queued",
                            "from_seq": acknowledged + 1,
                            "through_seq": required,
                            "token_prefix": replacement[:8],
                        },
                    )
                    results.append(ThreadReplyRecoveryEntry(
                        thread_id=thread_id,
                        agent_name=agent_name,
                        invocation_token=replacement,
                        kind="replacement_queued",
                    ))
                    continue

                if queued_token is not None:
                    inv = self._conn.execute(
                        "SELECT * FROM thread_invocations "
                        "WHERE invocation_token = ?",
                        (queued_token,),
                    ).fetchone()
                    same_pair = (
                        inv is not None
                        and inv["thread_id"] == thread_id
                        and inv["agent_name"] == agent_name
                    )
                    right_purpose = (
                        inv is not None
                        and inv["purpose"] == ThreadInvocationPurpose.REPLY.value
                    )
                    pending = (
                        inv is not None
                        and inv["status"] == ThreadInvocationStatus.PENDING.value
                    )
                    started = inv is not None and inv["started_at"] is not None
                    # A valid queued wake is a same-pair pending REPLY whose
                    # receipt is UNSTARTED — claim_conversational_reply
                    # enforces the identical precondition. started_at on a
                    # queued receipt is malformed/crash-window state: the
                    # worker claim would no-op and the pair would strand
                    # forever, with later arrivals only coalescing into it.
                    valid_queued = (
                        inv is not None
                        and same_pair and right_purpose and pending
                        and not started
                    )
                    if valid_queued:
                        self._emit_reply_wake_audit(
                            thread_id=thread_id, agent_name=agent_name,
                            action="thread_reply_wake_recovered",
                            payload={
                                "agent_name": agent_name,
                                "kind": "retained_queued",
                                "from_seq": (
                                    int(row["acknowledged_through_seq"] or 0) + 1
                                ),
                                "through_seq": int(row["required_through_seq"] or 0),
                                "token_prefix": queued_token[:8],
                            },
                        )
                        results.append(ThreadReplyRecoveryEntry(
                            thread_id=thread_id,
                            agent_name=agent_name,
                            invocation_token=queued_token,
                            kind="retained_queued",
                        ))
                    elif same_pair and right_purpose and pending and started:
                        # Queued slot references a started receipt: invalid
                        # queued ownership. Fail closed with a transactionally
                        # atomic PAIR-SCOPED sweep (same class as the
                        # both-slots corruption branch): retire EVERY owned
                        # pending REPLY receipt for this pair — including
                        # unreferenced orphans no slot points at — so no
                        # pending REPLY survives that no claim can ever run,
                        # then clear the queued slot. Never mint or return a
                        # runnable replacement (no unowned provider run).
                        # required_through_seq is preserved, so the next
                        # conversational arrival mints a fresh wake covering
                        # the retained range (no swallowed arrival).
                        swept = self._conn.execute(
                            "UPDATE thread_invocations SET status = 'failed', "
                            "decline_reason = 'invalid_queued_started_on_recovery', "
                            "consumed_at = ? "
                            "WHERE thread_id = ? AND agent_name = ? "
                            "AND status = 'pending' AND purpose = 'reply'",
                            (now, thread_id, agent_name),
                        )
                        self._conn.execute(
                            "UPDATE thread_reply_delivery_state SET "
                            "queued_invocation_token = NULL, "
                            "last_terminal_reason = ?, last_terminal_at = ?, "
                            "updated_at = ? WHERE thread_id = ? AND agent_name = ?",
                            ("invalid_queued_started_on_recovery", now, now,
                             thread_id, agent_name),
                        )
                        # TASK-6065: invalid queued ownership is a fail-closed
                        # classification — revoke any pending released-deferral
                        # catch-up marker in the same transaction so the
                        # post-loop reconciliation can never mint from it.
                        suppressed = self._clear_catchup_pending_uncommitted(
                            thread_id, agent_name,
                        )
                        self._emit_reply_wake_audit(
                            thread_id=thread_id, agent_name=agent_name,
                            action="thread_reply_wake_cancelled",
                            payload={
                                "agent_name": agent_name,
                                "boundary_seq": int(row["required_through_seq"] or 0),
                                "reason": "invalid_queued_started_on_recovery",
                                "swept_count": swept.rowcount,
                                "catchup_suppressed": suppressed > 0,
                            },
                        )
                    else:
                        # Fail closed: clear the queued slot, return nothing.
                        self._conn.execute(
                            "UPDATE thread_reply_delivery_state SET "
                            "queued_invocation_token = NULL, "
                            "last_terminal_reason = ?, last_terminal_at = ?, "
                            "updated_at = ? WHERE thread_id = ? AND agent_name = ?",
                            ("invalid_queued_token_on_recovery", now, now,
                             thread_id, agent_name),
                        )
                        # TASK-6065: invalid queued ownership is a fail-closed
                        # classification — revoke any pending released-deferral
                        # catch-up marker in the same transaction so the
                        # post-loop reconciliation can never mint from it.
                        self._clear_catchup_pending_uncommitted(
                            thread_id, agent_name,
                        )

            # TASK-6057 — released-deferral post-slot catch-up reconciliation at
            # the restart boundary. A closure that found a live but NON-covering
            # slot (its immutable range ended before the released range) durably
            # marked the pair's deferral row ``catchup_pending``; the branches
            # above terminalized exactly the blocking slot (replacement minted /
            # retained / cleared). This pass resolves every still-pending marker
            # in the same transaction so the owed wake survives daemon restart:
            #   * a covering slot exists (replacement/retained queued wake) → the
            #     marker is satisfied, consume it;
            #   * no slot and ``acknowledged < required`` → the blocking slot is
            #     terminal with no covering replacement: mint exactly ONE
            #     range-covering catch-up now and consume the marker
            #     (exactly-once in this transaction);
            #   * ``acknowledged == required`` (fully covered) → nothing owed,
            #     consume the marker.
            # TASK-6065 — the FAIL-CLOSED branches above (both-slots corruption,
            # non-recoverable running slot, invalid queued started/ownership)
            # each revoked the pair's pending marker ATOMICALLY in the same
            # transaction, so the mint rule below can never fabricate runnable
            # ownership from state those branches declared untrustworthy: an
            # authoritative fail-closed terminal classification always survives
            # into this reconciliation as a revoked marker, never as a mint.
            for d in self._conn.execute(
                "SELECT DISTINCT thread_id, agent_name "
                "FROM thread_exchange_deferrals WHERE catchup_pending = 1",
            ).fetchall():
                thread_id = d["thread_id"]
                agent_name = d["agent_name"]
                srow = self._conn.execute(
                    "SELECT * FROM thread_reply_delivery_state "
                    "WHERE thread_id = ? AND agent_name = ?",
                    (thread_id, agent_name),
                ).fetchone()
                if (
                    srow is None
                    or srow["queued_invocation_token"]
                    or srow["running_invocation_token"]
                ):
                    self._clear_catchup_pending_uncommitted(thread_id, agent_name)
                    continue
                ack = int(srow["acknowledged_through_seq"] or 0)
                req = int(srow["required_through_seq"] or 0)
                if ack >= req:
                    self._clear_catchup_pending_uncommitted(thread_id, agent_name)
                    continue
                token = self._mint_reply_invocation_uncommitted(
                    thread_id, agent_name, ack + 1,
                )
                self._conn.execute(
                    "UPDATE thread_reply_delivery_state SET "
                    "queued_invocation_token = ?, updated_at = ? "
                    "WHERE thread_id = ? AND agent_name = ?",
                    (token, now, thread_id, agent_name),
                )
                self._clear_catchup_pending_uncommitted(thread_id, agent_name)
                self._emit_reply_wake_audit(
                    thread_id=thread_id, agent_name=agent_name,
                    action="thread_reply_wake_created",
                    payload={
                        "agent_name": agent_name,
                        "from_seq": ack + 1,
                        "through_seq": req,
                        "token_prefix": token[:8],
                        "kind": "deferred_catch_up",
                    },
                )
                self.insert_audit_log_uncommitted(
                    task_id=thread_id, agent=agent_name,
                    action="thread_deferral_catchup_minted",
                    payload={
                        "thread_id": thread_id,
                        "agent_name": agent_name,
                        "from_seq": ack + 1,
                        "through_seq": req,
                        "mint_token_prefix": token[:8],
                        "kind": "recovery",
                    },
                )
                results.append(ThreadReplyRecoveryEntry(
                    thread_id=thread_id,
                    agent_name=agent_name,
                    invocation_token=token,
                    kind="deferred_catchup",
                ))

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return results

    # ── GitHub #688 Phase 1 Slice B: reply delivery state wiring ────────────
    #
    # Slice A shipped the table + cutover/recovery primitives UNHOOKED. Slice B
    # owns the route/runner activation and the atomic conversational-arrival,
    # claim, and settlement operations. The store is the single authority for
    # the queued/running token transitions; routes/runner MUST NOT open-code
    # them. All state-changing methods run one explicit SQLite transaction
    # (BEGIN IMMEDIATE) so the append+arrival / append+settle+broadcast units
    # are atomic and queue notifications happen strictly after commit.

    @_synchronized
    def list_open_thread_ids(self) -> list[str]:
        """Every OPEN thread id (activation cutover sweep at startup)."""
        cursor = self._conn.execute(
            "SELECT id FROM threads WHERE status = 'open' ORDER BY id",
        )
        return [r["id"] for r in cursor.fetchall()]

    def _apply_arrival_uncommitted(
        self, thread_id: str, agent_name: str, seq: int,
    ) -> ThreadReplyArrival:
        """Raise ``required_through_seq`` to ``seq`` for one recipient pair,
        minting exactly one queued REPLY only when neither queued nor running
        ownership exists. Runs inside an open transaction (no commit).

        ``seq`` must be the just-appended conversational message sequence;
        the speaker is excluded by the caller. A missing pair row is created
        with acknowledged = seq - 1 (no historic replay) so Phase 1 delivery
        for a newly-seen pair starts at this message.
        """
        now = _now().isoformat()
        row = self._conn.execute(
            "SELECT * FROM thread_reply_delivery_state "
            "WHERE thread_id = ? AND agent_name = ?",
            (thread_id, agent_name),
        ).fetchone()
        if row is None:
            # New pair: seed acknowledged = seq - 1 (tail before this message),
            # required = seq, and mint one queued wake covering seq..seq.
            token = self._mint_reply_invocation_uncommitted(
                thread_id, agent_name, seq,
            )
            self._conn.execute(
                "INSERT INTO thread_reply_delivery_state "
                "(thread_id, agent_name, acknowledged_through_seq, "
                "required_through_seq, queued_invocation_token, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, agent_name, seq - 1, seq, token, now),
            )
            self._emit_reply_wake_audit(
                thread_id=thread_id, agent_name=agent_name,
                action="thread_reply_wake_created",
                payload={
                    "agent_name": agent_name,
                    "from_seq": seq,
                    "through_seq": seq,
                    "token_prefix": token[:8],
                },
            )
            return ThreadReplyArrival(
                agent_name=agent_name, invocation_token=token,
                coalesced=False, from_seq=seq, through_seq=seq,
            )

        acknowledged = int(row["acknowledged_through_seq"] or 0)
        required = int(row["required_through_seq"] or 0)
        queued = row["queued_invocation_token"]
        running = row["running_invocation_token"]
        if seq <= required:
            # Idempotent safety: already covered by the required watermark.
            # No durable change — deliberately NO audit (a duplicate/backdated
            # notification must not fabricate a coalesced event).
            return ThreadReplyArrival(
                agent_name=agent_name, invocation_token=None,
                coalesced=True, from_seq=acknowledged + 1, through_seq=required,
            )

        new_required = seq
        if queued is not None or running is not None:
            # Coalesce: raise required only; the existing wake already owns the
            # delivery obligation.
            self._conn.execute(
                "UPDATE thread_reply_delivery_state SET required_through_seq = ?, "
                "updated_at = ? WHERE thread_id = ? AND agent_name = ?",
                (new_required, now, thread_id, agent_name),
            )
            self._emit_reply_wake_audit(
                thread_id=thread_id, agent_name=agent_name,
                action="thread_reply_wake_coalesced",
                payload={
                    "agent_name": agent_name,
                    "from_seq": acknowledged + 1,
                    "through_seq": new_required,
                },
            )
            return ThreadReplyArrival(
                agent_name=agent_name, invocation_token=None,
                coalesced=True,
                from_seq=acknowledged + 1, through_seq=new_required,
            )

        # No queued/running ownership: mint one queued wake covering
        # acknowledged+1 .. seq.
        from_seq = acknowledged + 1
        token = self._mint_reply_invocation_uncommitted(
            thread_id, agent_name, from_seq,
        )
        self._conn.execute(
            "UPDATE thread_reply_delivery_state SET required_through_seq = ?, "
            "queued_invocation_token = ?, updated_at = ? "
            "WHERE thread_id = ? AND agent_name = ?",
            (new_required, token, now, thread_id, agent_name),
        )
        self._emit_reply_wake_audit(
            thread_id=thread_id, agent_name=agent_name,
            action="thread_reply_wake_created",
            payload={
                "agent_name": agent_name,
                "from_seq": from_seq,
                "through_seq": new_required,
                "token_prefix": token[:8],
            },
        )
        return ThreadReplyArrival(
            agent_name=agent_name, invocation_token=token,
            coalesced=False, from_seq=from_seq, through_seq=new_required,
        )

    def _derive_conversational_mentions(
        self,
        thread_id: str,
        speaker: str,
        kind: ThreadMessageKind,
        body_markdown: str | None,
    ) -> list[str] | None:
        """Server-side derivation of the durable mention signal for a
        conversational write (THR-198 Slice A). Only kind=MESSAGE rows carry
        the signal; system/decline rows stay NULL. The stored value is the
        canonical valid set: live participants at write time, excluding the
        speaker, deduped in first-occurrence order — derived from
        ``body_markdown``, never client-declared.
        """
        if kind is not ThreadMessageKind.MESSAGE:
            return None
        participants = [
            r["agent_name"] for r in self._conn.execute(
                "SELECT agent_name FROM thread_participants WHERE thread_id = ?",
                (thread_id,),
            ).fetchall()
        ]
        return valid_mentions(
            parse_mentions(body_markdown), participants, speaker,
        )

    # ── TASK-5966 strict mention-led exchange: store seams ────────────────
    #
    # Founder-ratified (THR-198 seq 194/195/196; TASK-5939 verdict): strict
    # exchange hold, additive exchange state, full-recipient
    # obligations, pre-claim attribution safeguard, frozen cohort, exactly one
    # range-covering catch-up per pair at closure, EXCHANGE_GRACE = 5 min,
    # absolute fail-open = 4 h. Any-burst deferral is explicitly excluded.
    #
    # Model A (exchange-epoch gate): ``thread_reply_exchange`` (1 row per
    # exchange, the state machine) + ``thread_exchange_deferrals`` (frozen
    # D(E), audit/sweep substrate). During E, held pairs get obligation-only
    # raises (no token, no wake audit — at-most-one slots untouched,
    # watermarks stay contiguous); at closure every pair with
    # ``acknowledged < required`` gets ONE slot-checked catch-up wake covering
    # the full contiguous range. No gaps, no fabricated acks.

    def _get_open_exchange_uncommitted(self, thread_id: str):
        """The single OPEN exchange row for a thread, or None. Runs inside
        an open transaction. The partial-unique index ``idx_trex_open`` makes
        at most one such row exist by construction."""
        return self._conn.execute(
            "SELECT * FROM thread_reply_exchange "
            "WHERE thread_id = ? AND state = 'open'",
            (thread_id,),
        ).fetchone()

    def _exchange_cohort_uncommitted(self, exchange_row) -> list[str]:
        """The frozen priority cohort P(E) of an exchange, read from the
        immutable ``M_open.mentions_json`` (no duplicated JSON column).
        Fail-closed: a malformed/missing opening mention signal yields []
        (the caller then treats the exchange as corrupt / quiescent-able)."""
        opener = self._conn.execute(
            "SELECT mentions_json FROM thread_messages "
            "WHERE thread_id = ? AND seq = ?",
            (exchange_row["thread_id"], exchange_row["open_seq"]),
        ).fetchone()
        if opener is None or not opener["mentions_json"]:
            return []
        try:
            data = json.loads(opener["mentions_json"])
        except (TypeError, ValueError):
            return []
        return [m for m in data if isinstance(m, str)]

    def _raise_required_uncommitted(
        self, thread_id: str, agent_name: str, seq: int,
    ) -> None:
        """U0 — obligation-only raise: advance ``required_through_seq`` to
        ``seq`` for one recipient pair WITHOUT minting any wake and WITHOUT a
        wake audit. Runs inside an open transaction (no commit).

        This is the full-recipient obligation half of U0: every conversational
        message raises ``required`` for EVERY recipient (I4 totality), while
        only the resolved wake set mints. A missing pair row is created with
        acknowledged = seq - 1 and NO token (obligation row; the next wake
        covers the full range). ``seq <= required`` is an idempotent silent
        no-op — deliberately NO audit (a duplicate/backdated notification
        must not fabricate an event). Watermarks stay monotonic and
        contiguous: ``acknowledged`` never moves, ``required`` only advances.
        """
        now = _now().isoformat()
        row = self._conn.execute(
            "SELECT required_through_seq FROM thread_reply_delivery_state "
            "WHERE thread_id = ? AND agent_name = ?",
            (thread_id, agent_name),
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO thread_reply_delivery_state "
                "(thread_id, agent_name, acknowledged_through_seq, "
                "required_through_seq, updated_at) VALUES (?, ?, ?, ?, ?)",
                (thread_id, agent_name, seq - 1, seq, now),
            )
            return
        required = int(row["required_through_seq"] or 0)
        if seq <= required:
            return
        self._conn.execute(
            "UPDATE thread_reply_delivery_state SET required_through_seq = ?, "
            "updated_at = ? WHERE thread_id = ? AND agent_name = ?",
            (seq, now, thread_id, agent_name),
        )

    def _open_reply_exchange_uncommitted(
        self,
        *,
        thread_id: str,
        open_seq: int,
        speaker: str,
        mentions: list[str],
        recipients: list[str],
    ) -> int:
        """Open a strict mention-led exchange inside the open transaction
        (caller commits). Preconditions (caller-verified): no open exchange,
        speaker == 'founder', ``mentions`` non-empty, deferred set non-empty,
        exchange enabled. P(E) = ``mentions`` (frozen, read later from
        ``M_open.mentions_json``); D(E) = recipients − speaker − P frozen
        into ``thread_exchange_deferrals`` (held). Returns the new
        exchange_id.
        """
        now = _now().isoformat()
        deferred = [r for r in recipients if r not in mentions]
        next_id = self._conn.execute(
            "SELECT COALESCE(MAX(exchange_id), 0) + 1 "
            "FROM thread_reply_exchange WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()[0]
        self._conn.execute(
            "INSERT INTO thread_reply_exchange (thread_id, exchange_id, "
            "state, open_seq, close_seq, opened_at, last_activity_at, "
            "deferred_count) VALUES (?, ?, 'open', ?, ?, ?, ?, ?)",
            (thread_id, next_id, open_seq, open_seq, now, now, len(deferred)),
        )
        for name in deferred:
            self._conn.execute(
                "INSERT INTO thread_exchange_deferrals (thread_id, "
                "exchange_id, agent_name, state, created_at) "
                "VALUES (?, ?, ?, 'held', ?)",
                (thread_id, next_id, name, now),
            )
        self.insert_audit_log_uncommitted(
            task_id=thread_id, agent="founder",
            action="thread_exchange_opened",
            payload={
                "thread_id": thread_id, "exchange_id": next_id,
                "open_seq": open_seq, "priority": mentions,
                "deferred": deferred,
            },
        )
        for name in deferred:
            self.insert_audit_log_uncommitted(
                task_id=thread_id, agent=name,
                action="thread_deferral_held",
                payload={
                    "thread_id": thread_id, "exchange_id": next_id,
                    "agent_name": name,
                },
            )
        return next_id

    def _extend_reply_exchange_uncommitted(
        self, thread_id: str, seq: int,
    ) -> None:
        """Extend an open exchange: every conversational MESSAGE inside E
        (any speaker) refreshes ``last_activity_at`` and advances
        ``close_seq`` to the frontier. Runs inside the open transaction."""
        now = _now().isoformat()
        self._conn.execute(
            "UPDATE thread_reply_exchange SET last_activity_at = ?, "
            "close_seq = ? WHERE thread_id = ? AND state = 'open'",
            (now, seq, thread_id),
        )

    def _resolve_exchange_wake_set(
        self,
        *,
        thread_id: str,
        mentions: list[str],
        recipients: list[str],
        open_exchange,
    ) -> list[str]:
        """U2 — strict-hold wake resolution inside an open exchange.

        * ``M`` has valid mentions → wake set = exactly that mention set
          (mention-pierce: newly mentioned / deferred members wake
          immediately; the cohort is NOT altered — frozen).
        * ``M`` has no valid mentions → wake set = the frozen cohort P(E)
          only (seq 188: "only that cohort receives wakes"). No-mention
          messages inside E NEVER broadcast to held members.

        TASK_FOLLOWUP/BOOTSTRAP never reach here (callers keep the isolated
        full-broadcast branch). ``recipients`` is the candidate set minus the
        speaker; every recipient NOT in the wake set is held (obligation-only).
        """
        if mentions:
            return [m for m in mentions if m in recipients]
        cohort = self._exchange_cohort_uncommitted(open_exchange)
        return [c for c in cohort if c in recipients]

    def _pair_held_by_open_exchange(
        self, thread_id: str, agent_name: str,
    ) -> bool:
        """True when ``agent_name`` is a held (non-cohort) member of an open
        exchange on this thread. Used to suppress the settlement follow-on for
        a held pair (the exchange's single catch-up at closure carries the
        residual range). Runs inside the open transaction."""
        row = self._get_open_exchange_uncommitted(thread_id)
        if row is None:
            return False
        cohort = self._exchange_cohort_uncommitted(row)
        return agent_name not in cohort

    def _pair_catchup_pending_uncommitted(
        self, thread_id: str, agent_name: str,
    ) -> bool:
        """True when the pair holds an unsatisfied released-deferral catch-up
        obligation: any ``thread_exchange_deferrals`` row (across released
        exchanges) with ``catchup_pending = 1``. Set at closure when the
        pair's single live slot did not cover the released range; the owed
        catch-up is minted exactly once when that slot reaches a terminal
        outcome. Runs inside the open transaction."""
        row = self._conn.execute(
            "SELECT 1 FROM thread_exchange_deferrals "
            "WHERE thread_id = ? AND agent_name = ? AND catchup_pending = 1 "
            "LIMIT 1",
            (thread_id, agent_name),
        ).fetchone()
        return row is not None

    def _mark_catchup_pending_uncommitted(
        self, thread_id: str, exchange_id: int, agent_name: str,
    ) -> bool:
        """Durably record a pending post-slot catch-up on THIS exchange's
        deferral row (a D(E) member whose live slot did not cover the
        released range at closure). Returns True when a row was actually
        updated (the pair is a deferred member of this exchange). Runs
        inside the open transaction."""
        cur = self._conn.execute(
            "UPDATE thread_exchange_deferrals SET catchup_pending = 1 "
            "WHERE thread_id = ? AND exchange_id = ? AND agent_name = ? "
            "AND state = 'released'",
            (thread_id, exchange_id, agent_name),
        )
        return cur.rowcount > 0

    def _clear_catchup_pending_uncommitted(
        self, thread_id: str, agent_name: str,
    ) -> int:
        """Consume every pending catch-up marker for the pair — one
        range-covering wake satisfies all of them. Runs inside the open
        transaction. Returns the number of deferral rows whose marker was
        actually cleared (0 when the pair held no pending marker), so a
        caller can record a truthful audit of marker revocation."""
        cur = self._conn.execute(
            "UPDATE thread_exchange_deferrals SET catchup_pending = 0 "
            "WHERE thread_id = ? AND agent_name = ?",
            (thread_id, agent_name),
        )
        return cur.rowcount

    def _exchange_has_live_cohort_wake(
        self, thread_id: str, open_seq: int, cohort: list[str],
    ) -> bool:
        """Quiescence predicate: True when any cohort member has a live wake
        whose coverage contains the exchange — a running wake with
        ``running_through_seq >= open_seq`` (claimed coverage is immutable, so
        a pre-arrival claim with running_through < open_seq is NOT live), or a
        queued wake whose ``required_through_seq >= open_seq`` (the recovery
        replacement keeps the cohort non-quiescent — daemon_restart is an
        interruption, never terminal, while replacement work exists)."""
        if not cohort:
            return False
        placeholders = ",".join("?" for _ in cohort)
        row = self._conn.execute(
            f"SELECT 1 FROM thread_reply_delivery_state WHERE thread_id = ? "
            f"AND agent_name IN ({placeholders}) AND ("
            f"(running_through_seq IS NOT NULL AND running_through_seq >= ?) "
            f"OR (queued_invocation_token IS NOT NULL "
            f"AND required_through_seq >= ?)) LIMIT 1",
            (thread_id, *cohort, open_seq, open_seq),
        ).fetchone()
        return row is not None

    def _close_reply_exchange_uncommitted(
        self, thread_id: str, exchange_id: int, *, reason: str,
    ) -> list[ThreadReplyArrival]:
        """Atomic close/release of one exchange (inside the open transaction;
        caller commits and enqueues the returned catch-up tokens after
        commit).

        E CAS ``open -> released`` (a duplicate evaluation is a CAS miss with
        NO audit and NO mint — idempotent), deferral rows ``held -> released``,
        then the watermark-based catch-up: for EVERY pair with
        ``acknowledged < required`` and no queued/running slot, mint ONE
        queued REPLY covering the full contiguous unread range (the
        coverage-safe superset: frozen D(E), mid-E joiners, cohort members
        with residual unread). A pair whose live slot GENUINELY covers the
        released range is marked coalesced (no second token — at-most-one
        slots honored); a pair whose live slot does NOT cover it (a running
        wake with immutable ``running_through_seq < required`` — e.g. a
        pre-exchange claim) is durably marked catch-up-pending: exactly ONE
        post-slot catch-up is minted when the blocking slot reaches a
        terminal outcome (failed/timeout settlement or recovery
        terminalization), surviving daemon restart — never stranded.
        """
        now = _now().isoformat()
        cur = self._conn.execute(
            "UPDATE thread_reply_exchange SET state = 'released', "
            "closed_at = ?, close_reason = ? "
            "WHERE thread_id = ? AND exchange_id = ? AND state = 'open'",
            (now, reason, thread_id, exchange_id),
        )
        if cur.rowcount == 0:
            return []  # CAS miss — duplicate closure/reaper/settlement no-op
        self._conn.execute(
            "UPDATE thread_exchange_deferrals SET state = 'released', "
            "released_at = ? WHERE thread_id = ? AND exchange_id = ? "
            "AND state = 'held'",
            (now, thread_id, exchange_id),
        )
        self.insert_audit_log_uncommitted(
            task_id=thread_id, agent="founder",
            action="thread_exchange_closed",
            payload={
                "thread_id": thread_id, "exchange_id": exchange_id,
                "close_reason": reason, "closed_at": now,
            },
        )
        arrivals: list[ThreadReplyArrival] = []
        rows = self._conn.execute(
            "SELECT * FROM thread_reply_delivery_state WHERE thread_id = ? "
            "AND acknowledged_through_seq < required_through_seq",
            (thread_id,),
        ).fetchall()
        for row in rows:
            agent = row["agent_name"]
            ack = int(row["acknowledged_through_seq"] or 0)
            req = int(row["required_through_seq"] or 0)
            if row["queued_invocation_token"] or row["running_invocation_token"]:
                # Genuinely-covering predicate (TASK-6057): a live slot
                # coalesces ONLY when its durable coverage contains the
                # released range.
                #   * running slot — the claimed range is IMMUTABLE
                #     (running_through_seq snapshotted at claim): it covers
                #     iff running_through_seq >= required. A pre-arrival
                #     claim whose through-seq ends before the released range
                #     does NOT cover (reviewer TASK-6056 counterexample).
                #   * queued slot — covers at claim (running_through =
                #     required-at-claim; required is monotonic), so it
                #     always covers.
                # A non-covering live slot blocks immediate minting
                # (at-most-one slot invariant): the released deferral is
                # durably marked catchup-pending and its exactly-one
                # post-slot catch-up fires when the blocking slot reaches a
                # terminal outcome — failed/timeout settlement, or recovery
                # terminalization on restart. Never a second token.
                non_covering = (
                    row["running_invocation_token"] is not None
                    and (
                        row["running_through_seq"] is None
                        or int(row["running_through_seq"]) < req
                    )
                )
                if non_covering:
                    if self._mark_catchup_pending_uncommitted(
                        thread_id, exchange_id, agent,
                    ):
                        self.insert_audit_log_uncommitted(
                            task_id=thread_id, agent=agent,
                            action="thread_deferral_catchup_pending",
                            payload={
                                "thread_id": thread_id,
                                "exchange_id": exchange_id,
                                "agent_name": agent,
                                "from_seq": ack + 1,
                                "through_seq": req,
                                "reason": "old_wake_does_not_cover",
                            },
                        )
                    arrivals.append(ThreadReplyArrival(
                        agent_name=agent, invocation_token=None,
                        coalesced=True, from_seq=ack + 1, through_seq=req,
                    ))
                    continue
                # Genuinely covering wake: coalesce — never a second token.
                self._clear_catchup_pending_uncommitted(thread_id, agent)
                arrivals.append(ThreadReplyArrival(
                    agent_name=agent, invocation_token=None, coalesced=True,
                    from_seq=ack + 1, through_seq=req,
                ))
                continue
            token = self._mint_reply_invocation_uncommitted(
                thread_id, agent, ack + 1,
            )
            self._conn.execute(
                "UPDATE thread_reply_delivery_state SET "
                "queued_invocation_token = ?, updated_at = ? "
                "WHERE thread_id = ? AND agent_name = ?",
                (token, now, thread_id, agent),
            )
            # A direct mint satisfies any pending catch-up from an earlier
            # release (one range-covering wake covers all of them).
            self._clear_catchup_pending_uncommitted(thread_id, agent)
            self._emit_reply_wake_audit(
                thread_id=thread_id, agent_name=agent,
                action="thread_reply_wake_created",
                payload={
                    "agent_name": agent,
                    "from_seq": ack + 1,
                    "through_seq": req,
                    "token_prefix": token[:8],
                    "kind": "exchange_catch_up",
                },
            )
            self._conn.execute(
                "UPDATE thread_exchange_deferrals SET mint_token_prefix = ? "
                "WHERE thread_id = ? AND exchange_id = ? "
                "AND agent_name = ? AND state = 'released'",
                (token[:8], thread_id, exchange_id, agent),
            )
            self.insert_audit_log_uncommitted(
                task_id=thread_id, agent=agent,
                action="thread_deferral_released",
                payload={
                    "thread_id": thread_id, "exchange_id": exchange_id,
                    "agent_name": agent, "mint_token_prefix": token[:8],
                },
            )
            arrivals.append(ThreadReplyArrival(
                agent_name=agent, invocation_token=token, coalesced=False,
                from_seq=ack + 1, through_seq=req,
            ))
        # Deferral rows whose pair was already fully covered (pierce/ack==req)
        # are released with no mint — record the coalesced audit. Rows whose
        # catch-up is pending (a live non-covering slot at release) are
        # audited by ``thread_deferral_catchup_pending`` in the loop above,
        # never as coalesced.
        held = self._conn.execute(
            "SELECT agent_name FROM thread_exchange_deferrals "
            "WHERE thread_id = ? AND exchange_id = ? "
            "AND state = 'released' AND mint_token_prefix IS NULL "
            "AND catchup_pending = 0",
            (thread_id, exchange_id),
        ).fetchall()
        for drow in held:
            self.insert_audit_log_uncommitted(
                task_id=thread_id, agent=drow["agent_name"],
                action="thread_deferral_coalesced",
                payload={
                    "thread_id": thread_id, "exchange_id": exchange_id,
                    "agent_name": drow["agent_name"],
                },
            )
        return arrivals

    def _suppress_open_exchanges_uncommitted(
        self, thread_id: str, *, reason: str,
    ) -> None:
        """Suppress every OPEN exchange on a thread (abort / archive /
        corruption / whole-thread discard). No catch-up is ever minted — the
        human stopped. Deferral rows flip to suppressed. Idempotent CAS per
        exchange. Runs inside the open transaction."""
        now = _now().isoformat()
        rows = self._conn.execute(
            "SELECT exchange_id FROM thread_reply_exchange "
            "WHERE thread_id = ? AND state = 'open'",
            (thread_id,),
        ).fetchall()
        for r in rows:
            cur = self._conn.execute(
                "UPDATE thread_reply_exchange SET state = 'suppressed', "
                "closed_at = ?, close_reason = ? "
                "WHERE thread_id = ? AND exchange_id = ? AND state = 'open'",
                (now, reason, thread_id, r["exchange_id"]),
            )
            if cur.rowcount == 0:
                continue
            self._conn.execute(
                "UPDATE thread_exchange_deferrals SET state = 'suppressed' "
                "WHERE thread_id = ? AND exchange_id = ? AND state = 'held'",
                (thread_id, r["exchange_id"]),
            )
            self.insert_audit_log_uncommitted(
                task_id=thread_id, agent="founder",
                action="thread_exchange_closed",
                payload={
                    "thread_id": thread_id, "exchange_id": r["exchange_id"],
                    "close_reason": reason, "suppressed": True,
                },
            )

    def _evaluate_exchange_closure(
        self, thread_id: str,
    ) -> list[ThreadReplyArrival]:
        """Evaluate closure for the thread's OPEN exchange(s) inside the
        open transaction. Returns catch-up arrivals (tokens to enqueue after
        commit) when a close actually fired; [] otherwise.

        Closure fires when ANY of:
          1. quiescence + grace: no live cohort wake covering E AND
             now - last_activity_at >= EXCHANGE_GRACE_SECONDS (5 min),
          2. absolute bound: now - opened_at >= MAX_PRIORITY_WAIT_SECONDS
             (4 h, fail-open),
          3. corruption (reconcile/sweep) — handled by the sweep, not here.
        Idempotent: every evaluation is a CAS; duplicates are silent misses.
        """
        now = datetime.now(timezone.utc)
        rows = self._conn.execute(
            "SELECT * FROM thread_reply_exchange WHERE thread_id = ? "
            "AND state = 'open'",
            (thread_id,),
        ).fetchall()
        arrivals: list[ThreadReplyArrival] = []
        for ex in rows:
            open_seq = int(ex["open_seq"])
            cohort = self._exchange_cohort_uncommitted(ex)
            live = self._exchange_has_live_cohort_wake(
                thread_id, open_seq, cohort,
            )
            last_activity = datetime.fromisoformat(
                ex["last_activity_at"].replace("Z", "+00:00")
            )
            opened = datetime.fromisoformat(
                ex["opened_at"].replace("Z", "+00:00")
            )
            grace_elapsed = (now - last_activity).total_seconds() \
                >= EXCHANGE_GRACE_SECONDS
            absolute_elapsed = (now - opened).total_seconds() \
                >= MAX_PRIORITY_WAIT_SECONDS
            if absolute_elapsed:
                arrivals.extend(self._close_reply_exchange_uncommitted(
                    thread_id, int(ex["exchange_id"]),
                    reason="max_priority_wait",
                ))
            elif not live and grace_elapsed:
                arrivals.extend(self._close_reply_exchange_uncommitted(
                    thread_id, int(ex["exchange_id"]),
                    reason="quiescence",
                ))
        return arrivals

    def _sweep_corrupt_exchanges_uncommitted(self) -> list[ThreadReplyArrival]:
        """U5 — fail-closed corruption sweep over every open exchange (runs
        in the startup reconcile transaction; caller commits + enqueues).
        The five sweep classes:
          1. ``held`` deferral row whose exchange is not ``open`` → aligned
             to the exchange's terminal state;
          2. ``held`` deferral row with NO exchange row → suppressed +
             diagnostic, never minted;
          3. open E whose opening ``mentions_json`` is malformed/missing →
             suppressed + diagnostic (fail-closed; never blind-release);
          4. ``close_seq < open_seq`` or ``last_activity_at`` in the future →
             suppressed + diagnostic;
          5. non-int/negative seq fields → fail-closed parse (bool-before-int
             per MEM-304).
        Returns catch-up arrivals for exchanges closed by this sweep (rare;
        normally suppresses, never releases)."""
        now = _now().isoformat()
        arrivals: list[ThreadReplyArrival] = []
        # Class 1/2: orphaned or misaligned held deferral rows.
        held_rows = self._conn.execute(
            "SELECT d.*, e.state AS exchange_state "
            "FROM thread_exchange_deferrals d "
            "LEFT JOIN thread_reply_exchange e "
            "ON e.thread_id = d.thread_id AND e.exchange_id = d.exchange_id "
            "WHERE d.state = 'held'",
        ).fetchall()
        for d in held_rows:
            if d["exchange_state"] is None:
                self._conn.execute(
                    "UPDATE thread_exchange_deferrals SET state = 'suppressed' "
                    "WHERE thread_id = ? AND exchange_id = ? AND agent_name = ? "
                    "AND state = 'held'",
                    (d["thread_id"], d["exchange_id"], d["agent_name"]),
                )
                self.insert_audit_log_uncommitted(
                    task_id=d["thread_id"], agent=d["agent_name"],
                    action="thread_deferral_suppressed",
                    payload={
                        "thread_id": d["thread_id"],
                        "exchange_id": d["exchange_id"],
                        "agent_name": d["agent_name"],
                        "reason": "orphan_deferral_row",
                    },
                )
            elif d["exchange_state"] != "open":
                target = "released" if d["exchange_state"] == "released" \
                    else "suppressed"
                self._conn.execute(
                    "UPDATE thread_exchange_deferrals SET state = ? "
                    "WHERE thread_id = ? AND exchange_id = ? AND agent_name = ? "
                    "AND state = 'held'",
                    (target, d["thread_id"], d["exchange_id"], d["agent_name"]),
                )
        # Class 3/4/5: malformed open exchanges → suppress + diagnostic.
        opens = self._conn.execute(
            "SELECT * FROM thread_reply_exchange WHERE state = 'open'",
        ).fetchall()
        for ex in opens:
            thread_id = ex["thread_id"]
            exchange_id = int(ex["exchange_id"])
            try:
                open_seq = int(ex["open_seq"])
                close_seq = int(ex["close_seq"])
            except (TypeError, ValueError):
                open_seq, close_seq = -1, -2
            bad_seq = (
                isinstance(ex["open_seq"], bool)  # bool-before-int (MEM-304)
                or isinstance(ex["close_seq"], bool)
                or open_seq < 1 or close_seq < open_seq
            )
            cohort = self._exchange_cohort_uncommitted(ex)
            bad_mentions = not cohort
            try:
                last_activity = _parse_dt(ex["last_activity_at"])
                future = last_activity > datetime.now(timezone.utc)
            except (TypeError, ValueError):
                future = True
            if bad_seq or bad_mentions or future:
                self._suppress_open_exchanges_uncommitted(
                    thread_id, reason="corrupt",
                )
                self.insert_audit_log_uncommitted(
                    task_id=thread_id, agent="founder",
                    action="thread_exchange_corrupt",
                    payload={
                        "thread_id": thread_id, "exchange_id": exchange_id,
                        "reason": (
                            "bad_seq" if bad_seq else
                            "bad_mentions" if bad_mentions else "future_activity"
                        ),
                    },
                )
        return arrivals

    @_synchronized
    def reconcile_reply_exchanges(self) -> list[ThreadReplyArrival]:
        """U5 — startup exchange reconcile (daemon step 6e). One transaction:
        corruption sweep first (fail-closed), then idempotent closure
        evaluation for every open exchange. Returns catch-up arrivals whose
        tokens the caller enqueues after commit."""
        arrivals: list[ThreadReplyArrival] = []
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            arrivals.extend(self._sweep_corrupt_exchanges_uncommitted())
            for ex in self._conn.execute(
                "SELECT DISTINCT thread_id FROM thread_reply_exchange "
                "WHERE state = 'open'",
            ).fetchall():
                arrivals.extend(self._evaluate_exchange_closure(ex["thread_id"]))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return arrivals

    @_synchronized
    def reaper_sweep_reply_exchanges(self) -> list[ThreadReplyArrival]:
        """U4 — reaper tick over every OPEN exchange (fail-open backstop).
        Closes exchanges past the 4h absolute bound or quiescence+grace;
        returns catch-up arrivals whose tokens the caller enqueues after
        commit. CAS-protected: a concurrent close is a silent miss."""
        arrivals: list[ThreadReplyArrival] = []
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for ex in self._conn.execute(
                "SELECT DISTINCT thread_id FROM thread_reply_exchange "
                "WHERE state = 'open'",
            ).fetchall():
                arrivals.extend(self._evaluate_exchange_closure(ex["thread_id"]))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return arrivals

    @_synchronized
    def list_reply_exchange_projections(
        self, thread_id: str,
    ) -> list[ThreadReplyExchangeProjection]:
        """Exchange-level wire projection for a thread (server contract).
        Returns the most recent exchange row in any state; empty for threads
        that never opened an exchange. Truthful — never fabricated."""
        rows = self._conn.execute(
            "SELECT * FROM thread_reply_exchange WHERE thread_id = ? "
            "ORDER BY exchange_id DESC LIMIT 1",
            (thread_id,),
        ).fetchall()
        out: list[ThreadReplyExchangeProjection] = []
        for row in rows:
            out.append(ThreadReplyExchangeProjection(
                thread_id=row["thread_id"],
                exchange_id=int(row["exchange_id"]),
                state=row["state"],  # type: ignore[arg-type]
                open_seq=int(row["open_seq"]),
                close_seq=int(row["close_seq"]),
                opened_at=row["opened_at"],
                last_activity_at=row["last_activity_at"],
                closed_at=row["closed_at"],
                close_reason=row["close_reason"],
                deferred_count=int(row["deferred_count"] or 0),
            ))
        return out

    @_synchronized
    def record_conversational_arrival(
        self,
        *,
        thread_id: str,
        speaker: str,
        kind: ThreadMessageKind,
        body_markdown: str | None = None,
        attachments: list[ThreadAttachment] | None = None,
        sent_from_task_id: str | None = None,
        recipients: list[str],
    ) -> tuple[int, list[ThreadReplyArrival]]:
        """Atomic conversational-arrival: append the message, raise the
        obligation watermark for EVERY recipient (U0 — full-recipient
        obligations), and mint/coalesce exactly one queued REPLY for the
        resolved WAKE SET only (mention routing; strict exchange hold while an
        exchange is open). Held members get obligation-only raises — no token,
        no wake audit.

        Returns (seq, arrivals). ``arrivals`` carry the newly-minted queue
        tokens (invocation_token is None when coalesced) — including any
        exchange catch-up tokens minted by a stale-exchange closure evaluated
        at the top of this write; the caller enqueues them ONLY after this
        transaction commits.
        """
        arrivals: list[ThreadReplyArrival] = []
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            mentions = self._derive_conversational_mentions(
                thread_id, speaker, kind, body_markdown,
            )
            # TASK-5966: a stale exchange closes BEFORE the new message is
            # classified (its catch-up tokens join this write's arrivals).
            arrivals.extend(self._evaluate_exchange_closure(thread_id))
            seq = self._append_thread_message_uncommitted(
                thread_id=thread_id,
                speaker=speaker,
                kind=kind,
                body_markdown=body_markdown,
                attachments=attachments,
                sent_from_task_id=sent_from_task_id,
                mentions=mentions,
            )
            open_exchange = self._get_open_exchange_uncommitted(thread_id)
            if open_exchange is not None and seq > int(open_exchange["open_seq"]):
                # Inside E: strict-hold resolution (U2).
                wake_set = self._resolve_exchange_wake_set(
                    thread_id=thread_id,
                    mentions=mentions or [],
                    recipients=recipients,
                    open_exchange=open_exchange,
                )
                if kind is ThreadMessageKind.MESSAGE:
                    self._extend_reply_exchange_uncommitted(thread_id, seq)
            else:
                # Outside E (or the opening message itself): unconditional
                # Phase-2 mention routing (valid mentions → exactly that
                # set; zero valid mentions → broadcast).
                wake_set = resolve_wake_set(
                    mentions or [], recipients, speaker,
                )
            # U0: wake-set members mint/coalesce exactly one queued REPLY.
            for name in wake_set:
                arrivals.append(
                    self._apply_arrival_uncommitted(thread_id, name, seq)
                )
            # U0: every OTHER recipient gets the obligation-only raise
            # (full-recipient obligations — I4 totality; no token, no wake
            # audit; watermarks stay contiguous and monotonic).
            for name in recipients:
                if name not in wake_set:
                    self._raise_required_uncommitted(thread_id, name, seq)
            # TASK-5966: a founder-authored MESSAGE with a non-empty valid
            # mention set and a non-empty deferred set OPENS a new exchange
            # (only when none is open after the stale-close above). The
            # opening message's own wake set is the mention set (priority
            # mints); D members were obligation-raised above and are now held.
            if (
                self._get_open_exchange_uncommitted(thread_id) is None
                and kind is ThreadMessageKind.MESSAGE
                and speaker == "founder"
                and mentions
            ):
                deferred = [r for r in recipients if r not in mentions]
                if deferred:
                    self._open_reply_exchange_uncommitted(
                        thread_id=thread_id,
                        open_seq=seq,
                        speaker=speaker,
                        mentions=mentions,
                        recipients=recipients,
                    )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return seq, arrivals

    def _settle_reply_uncommitted(
        self,
        token: str,
        *,
        outcome: str,
        decline_reason: str | None = None,
    ) -> ThreadReplySettlement | None:
        """Settle a conversational REPLY terminal path inside the open
        transaction. Returns None when ``token`` is not the running token of
        any delivery-state row (caller falls back to the legacy terminal path).

        Settlement contract (brief item 4):
          * reply/decline acknowledge ONLY the claimed coverage
            (``running_through``). The agent's own reply sequence is never
            part of its own required range (recipients exclude the speaker),
            so acknowledging through the claimed range never swallows an
            arrival that landed after the prompt was built.
          * arrivals during the run (``required > running_through``) yield
            exactly one post-settlement follow-on covering the retained
            unacknowledged range; it is the single wake for all of them.
            TASK-5966: a HELD pair (deferred member of an open exchange) has
            that follow-on SUPPRESSED — the exchange's single range-covering
            catch-up at closure carries the residual range
            (``exchange_held=True``, ``retry_required`` stays False).
          * failed/timeout do NOT advance acknowledgement and mint no
            immediate retry, leaving ``retry_required``
            (``required > acknowledged``) for the next conversational arrival
            to cover — EXCEPT the owed post-slot catch-up of a released
            deferral whose old slot never covered the released range (the
            closure durably marked ``catchup_pending``): that exactly-one
            range-covering wake is minted here so the release promise never
            strands (TASK-6057).

        The settled-audit payload is extended (founder-approved S4):
        ``covered_from_seq``/``covered_through_seq`` = the authoritative
        cover(S) (the immutable claimed range, or old acknowledged+1..required
        for a never-claimed queued settlement), ``claimed_at`` =
        ``thread_invocations.started_at``, ``minted_at`` =
        ``thread_invocations.enqueued_at`` (the durable mint instant), and
        ``exchange_held``. This is the tightened-D1 attribution substrate:
        a wake claimed before M has ``running_through < M.seq`` (range
        containment excludes it automatically) and ``claimed_at < M.created_at``
        (the durable recorded form); a queued settlement attributes only
        messages minted-at-or-before the wake.
        """
        now = _now().isoformat()
        row = self._conn.execute(
            "SELECT * FROM thread_reply_delivery_state "
            "WHERE running_invocation_token = ? OR queued_invocation_token = ?",
            (token, token),
        ).fetchone()
        if row is None:
            return None
        thread_id = row["thread_id"]
        agent_name = row["agent_name"]
        acknowledged = int(row["acknowledged_through_seq"] or 0)
        required = int(row["required_through_seq"] or 0)
        inv = self._conn.execute(
            "SELECT started_at, enqueued_at FROM thread_invocations "
            "WHERE invocation_token = ?",
            (token,),
        ).fetchone()
        minted_at = inv["enqueued_at"] if inv is not None else None
        if row["running_invocation_token"] == token:
            running_through = int(row["running_through_seq"] or 0)
            claimed_at = inv["started_at"] if inv is not None else None
        else:
            # A queued (not-yet-claimed) token: its delivery coverage is
            # acknowledged+1 .. required. Declining/replying to the whole
            # unclaimed wake acknowledges that full coverage.
            running_through = required
            claimed_at = None

        if outcome == "reply":
            status = ThreadInvocationStatus.CONSUMED.value
        elif outcome == "decline":
            status = ThreadInvocationStatus.DECLINED.value
        elif outcome == "timeout":
            status = ThreadInvocationStatus.TIMEOUT.value
        else:
            status = ThreadInvocationStatus.FAILED.value
        # reply/decline acknowledge exactly the claimed coverage; failure and
        # timeout leave the previously acknowledged watermark untouched.
        new_ack = running_through if outcome in ("reply", "decline") else acknowledged

        self._conn.execute(
            "UPDATE thread_invocations SET status = ?, decline_reason = ?, "
            "consumed_at = ? WHERE invocation_token = ? AND status = 'pending'",
            (status, decline_reason, now, token),
        )
        terminal_reason = (
            decline_reason if outcome in ("failed", "timeout") else None
        )
        self._conn.execute(
            "UPDATE thread_reply_delivery_state SET "
            "queued_invocation_token = NULL, "
            "running_invocation_token = NULL, running_from_seq = NULL, "
            "running_through_seq = NULL, acknowledged_through_seq = ?, "
            "last_terminal_reason = ?, last_terminal_at = ?, updated_at = ? "
            "WHERE thread_id = ? AND agent_name = ?",
            (new_ack, terminal_reason, now, now, thread_id, agent_name),
        )

        # THR-200 PR E: a real terminal reply/decline is the breaker success
        # boundary. Close the active continuity in this SAME transaction as
        # acknowledgement so neither state can commit without the other.
        if outcome in ("reply", "decline"):
            active_breakers = self._conn.execute(
                "SELECT episode_id FROM thread_reply_breaker_episodes "
                "WHERE thread_id=? AND agent_name=? AND state IN ('open','probe')",
                (thread_id, agent_name),
            ).fetchall()
            for active in active_breakers:
                self._conn.execute(
                    "INSERT OR IGNORE INTO thread_reply_breaker_receipts "
                    "(invocation_token,episode_id,outcome,failure_category,recorded_at) "
                    "VALUES (?,?,'success',NULL,?)",
                    (token, active["episode_id"], now),
                )
                self.insert_audit_log_uncommitted(
                    task_id=thread_id, agent=agent_name,
                    action="thread_reply_breaker_closed",
                    payload={"episode_id": active["episode_id"], "outcome": outcome},
                )
            self._conn.execute(
                "UPDATE thread_reply_breaker_episodes SET state='closed',"
                "consecutive_failures=0,opened_at=NULL,cooldown_until=NULL,"
                "probe_lease_id=NULL,last_failure_category=NULL,updated_at=? "
                "WHERE thread_id=? AND agent_name=? AND state IN ('open','probe')",
                (now, thread_id, agent_name),
            )

        follow_on: str | None = None
        exchange_held = False
        catchup_minted = False
        if required > new_ack:
            # A residual range remains. At most one wake may carry it:
            #   * a held pair (member of an OPEN exchange) suppresses the
            #     follow-on — the exchange's single catch-up at closure
            #     carries the residual range (exchange_held=True);
            #   * reply/decline mint exactly one follow-on covering arrivals
            #     strictly after the immutable running range
            #     (``required > running_through``);
            #   * failed/timeout mint ONLY the owed post-slot catch-up of a
            #     released deferral whose old slot never covered it (the
            #     closure durably marked catchup_pending) — a plain
            #     unacknowledged range gets no immediate retry (Phase-1
            #     fail-open: the next conversational arrival covers it).
            pending_catchup = self._pair_catchup_pending_uncommitted(
                thread_id, agent_name,
            )
            if self._pair_held_by_open_exchange(thread_id, agent_name):
                exchange_held = True
            elif outcome in ("reply", "decline") or pending_catchup:
                follow_on = self._mint_reply_invocation_uncommitted(
                    thread_id, agent_name, new_ack + 1,
                )
                self._conn.execute(
                    "UPDATE thread_reply_delivery_state SET "
                    "queued_invocation_token = ?, updated_at = ? "
                    "WHERE thread_id = ? AND agent_name = ?",
                    (follow_on, now, thread_id, agent_name),
                )
                if pending_catchup:
                    # The minted wake is the owed post-slot catch-up (for a
                    # reply/decline it is the natural follow-on; for a
                    # failed/timeout it is the release promise honoured) —
                    # consume the durable marker in the same transaction
                    # (exactly-once; a crash rolls both back).
                    catchup_minted = True
                    self._clear_catchup_pending_uncommitted(
                        thread_id, agent_name,
                    )
                    self.insert_audit_log_uncommitted(
                        task_id=thread_id, agent=agent_name,
                        action="thread_deferral_catchup_minted",
                        payload={
                            "thread_id": thread_id,
                            "agent_name": agent_name,
                            "from_seq": new_ack + 1,
                            "through_seq": required,
                            "mint_token_prefix": follow_on[:8],
                        },
                    )

        self._emit_reply_wake_audit(
            thread_id=thread_id, agent_name=agent_name,
            action="thread_reply_wake_settled",
            payload={
                "agent_name": agent_name,
                "outcome": outcome,
                "acknowledged_through_seq": new_ack,
                "required_through_seq": required,
                "covered_from_seq": acknowledged + 1,
                "covered_through_seq": running_through,
                "claimed_at": claimed_at,
                "minted_at": minted_at,
                "exchange_held": exchange_held,
                "catchup_minted": catchup_minted,
                "retry_required": (
                    required > new_ack and follow_on is None
                    and not exchange_held
                ),
                "follow_on_token_prefix": follow_on[:8] if follow_on else None,
                "decline_reason": decline_reason,
            },
        )

        return ThreadReplySettlement(
            thread_id=thread_id,
            agent_name=agent_name,
            outcome=outcome,  # type: ignore[arg-type]
            acknowledged_through_seq=new_ack,
            required_through_seq=required,
            # ``retry_required`` is the residual-obligation diagnostic: True
            # only when the range is still unacknowledged AND no follow-on wake
            # was minted to carry it (failure/timeout without an owed
            # catch-up). A reply/decline that minted a follow-on has an active
            # queued wake, so it is not retry_required; a held suppression
            # defers the range to the exchange catch-up, which is not a retry
            # condition; a failed/timeout that honoured a released-deferral
            # catch-up marker minted the owed wake, which is not a retry
            # either.
            retry_required=(
                required > new_ack and follow_on is None and not exchange_held
            ),
            follow_on_token=follow_on,
            exchange_held=exchange_held,
        )

    @_synchronized
    def settle_conversational_reply(
        self,
        *,
        token: str,
        outcome: str,
        decline_reason: str | None = None,
    ) -> ThreadReplySettlement | None:
        """Public settlement seam for a conversational REPLY terminal path.

        Returns None when the token is not the running token of any delivery-
        state row (BOOTSTRAP/TASK_FOLLOWUP, or an already-settled/stale REPLY);
        the caller then applies the legacy terminal transition.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            settlement = self._settle_reply_uncommitted(
                token,
                outcome=outcome,
                decline_reason=decline_reason,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return settlement

    @_synchronized
    def settle_conversational_reply_with_breaker_failure(
        self, *, token: str, outcome: str, decline_reason: str,
        thread_id: str, agent_name: str, executor_key: str,
        failure_category: str, threshold: int, cooldown_seconds: int,
        now: datetime | None = None,
    ) -> tuple[ThreadReplySettlement | None, ThreadReplyBreakerEpisode | None]:
        """Atomically settle a qualifying failure and advance its breaker."""
        if threshold < 1 or cooldown_seconds < 1:
            raise ValueError("breaker threshold and cooldown must be positive")
        at = (now or _now()).astimezone(timezone.utc)
        at_s = at.isoformat()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            settlement = self._settle_reply_uncommitted(
                token, outcome=outcome, decline_reason=decline_reason,
            )
            if settlement is None:
                self._conn.commit()
                return None, None
            receipt = self._conn.execute(
                "SELECT episode_id FROM thread_reply_breaker_receipts "
                "WHERE invocation_token=?", (token,),
            ).fetchone()
            row = self._conn.execute(
                "SELECT * FROM thread_reply_breaker_episodes WHERE thread_id=? "
                "AND agent_name=? AND executor_key=?",
                (thread_id, agent_name, executor_key),
            ).fetchone()
            if receipt is not None:
                episode = self._conn.execute(
                    "SELECT * FROM thread_reply_breaker_episodes WHERE episode_id=?",
                    (receipt["episode_id"],),
                ).fetchone()
                self._conn.commit()
                return settlement, self._row_to_reply_breaker_episode(episode)
            new_episode = row is None or (
                row["state"] == "closed" and int(row["consecutive_failures"]) == 0
            )
            episode_id = f"brep-{uuid.uuid4().hex}" if new_episode else row["episode_id"]
            failures = 1 if new_episode else int(row["consecutive_failures"]) + 1
            prior_state = "closed" if new_episode else row["state"]
            state = "open" if failures >= threshold or prior_state == "probe" else prior_state
            opened_at = at_s if state == "open" and prior_state != "open" else (
                None if new_episode else row["opened_at"]
            )
            cooldown_until = (
                (at + timedelta(seconds=cooldown_seconds)).isoformat()
                if state == "open" and prior_state != "open"
                else (None if new_episode else row["cooldown_until"])
            )
            self._conn.execute(
                "INSERT INTO thread_reply_breaker_episodes "
                "(thread_id,agent_name,executor_key,episode_id,state,consecutive_failures,"
                "opened_at,cooldown_until,probe_lease_id,last_failure_category,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,NULL,?,?) ON CONFLICT(thread_id,agent_name,executor_key) "
                "DO UPDATE SET episode_id=excluded.episode_id,state=excluded.state,"
                "consecutive_failures=excluded.consecutive_failures,opened_at=excluded.opened_at,"
                "cooldown_until=excluded.cooldown_until,probe_lease_id=NULL,"
                "last_failure_category=excluded.last_failure_category,updated_at=excluded.updated_at",
                (thread_id, agent_name, executor_key, episode_id, state, failures,
                 opened_at, cooldown_until, failure_category, at_s),
            )
            self._conn.execute(
                "INSERT INTO thread_reply_breaker_receipts VALUES (?,?,'failure',?,?)",
                (token, episode_id, failure_category, at_s),
            )
            if state == "open" and prior_state != "open":
                self.insert_audit_log_uncommitted(
                    task_id=thread_id, agent=agent_name,
                    action=("thread_reply_breaker_reopened" if prior_state == "probe"
                            else "thread_reply_breaker_opened"),
                    payload={"episode_id": episode_id,
                             "failure_category": failure_category,
                             "consecutive_failures": failures},
                )
            episode = self._conn.execute(
                "SELECT * FROM thread_reply_breaker_episodes WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
            self._conn.commit()
            return settlement, self._row_to_reply_breaker_episode(episode)
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def settle_conversational_reply_with_exchange(
        self,
        *,
        token: str,
        outcome: str,
        decline_reason: str | None = None,
    ) -> tuple[ThreadReplySettlement | None, list[ThreadReplyArrival]]:
        """Settlement seam for caller-owned exchange enqueue (TASK-5966).

        Same settlement transaction as ``settle_conversational_reply`` PLUS
        the exchange closure evaluation on the settled thread — the design's
        "every REPLY settlement is a closure evaluation point" — returning
        any catch-up arrivals whose tokens the caller enqueues after commit.
        A settlement on a thread with no open exchange is a no-op closure
        evaluation. Used by the decline route (which can enqueue); the
        runner's failure paths keep ``settle_conversational_reply`` and rely
        on the reaper for closure.
        """
        arrivals: list[ThreadReplyArrival] = []
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            settlement = self._settle_reply_uncommitted(
                token,
                outcome=outcome,
                decline_reason=decline_reason,
            )
            if settlement is not None:
                arrivals = self._evaluate_exchange_closure(
                    settlement.thread_id,
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return settlement, arrivals

    @_synchronized
    def reply_conversational(
        self,
        *,
        thread_id: str,
        speaker: str,
        body_markdown: str | None,
        attachments: list[ThreadAttachment] | None,
        token: str,
        token_purpose: ThreadInvocationPurpose,
    ) -> tuple[int, ThreadReplySettlement | None, list[ThreadReplyArrival]]:
        """Atomic reply: append the reply message, settle the held token, and
        broadcast to every OTHER participant.

        Returns (seq, settlement, arrivals). ``settlement`` is None for a
        non-REPLY token (BOOTSTRAP/TASK_FOLLOWUP use the legacy consume); the
        broadcast to other participants always uses the coalescing arrival path
        (replacing the legacy per-recipient REPLY mint).
        """
        now = _now().isoformat()
        arrivals: list[ThreadReplyArrival] = []
        settlement: ThreadReplySettlement | None = None
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            # TASK-5966: a stale exchange closes before this reply is
            # classified (catch-up tokens join this write's arrivals).
            arrivals.extend(self._evaluate_exchange_closure(thread_id))
            participants = [
                p["agent_name"] for p in self._conn.execute(
                    "SELECT agent_name FROM thread_participants WHERE thread_id = ?",
                    (thread_id,),
                ).fetchall()
            ]
            mentions = valid_mentions(
                parse_mentions(body_markdown), participants, speaker,
            )
            seq = self._append_thread_message_uncommitted(
                thread_id=thread_id,
                speaker=speaker,
                kind=ThreadMessageKind.MESSAGE,
                body_markdown=body_markdown,
                attachments=attachments,
                mentions=mentions,
            )
            if token_purpose is ThreadInvocationPurpose.REPLY:
                settlement = self._settle_reply_uncommitted(
                    token,
                    outcome="reply",
                )
                if settlement is None:
                    # Legacy/stale pending REPLY not owned by delivery state:
                    # fall back to the legacy consume transition.
                    self._conn.execute(
                        "UPDATE thread_invocations SET status = 'consumed', "
                        "consumed_at = ? WHERE invocation_token = ? "
                        "AND status = 'pending'",
                        (now, token),
                    )
            else:
                self._conn.execute(
                    "UPDATE thread_invocations SET status = 'consumed', "
                    "consumed_at = ? WHERE invocation_token = ? "
                    "AND status = 'pending'",
                    (now, token),
                )
            recipients = [name for name in participants if name != speaker]
            # Phase-2 mention routing (THR-198, Slice B): REPLY tokens resolve
            # the broadcast at write time from the persisted structured
            # mention signal + the thread setting. TASK_FOLLOWUP and BOOTSTRAP
            # are ISOLATED — they keep the full participants-minus-speaker
            # broadcast and are never mention-routed (documented pierce source
            # inside an exchange).
            if token_purpose is ThreadInvocationPurpose.REPLY:
                open_exchange = self._get_open_exchange_uncommitted(thread_id)
                if open_exchange is not None and seq > int(
                    open_exchange["open_seq"],
                ):
                    # Inside E: strict-hold resolution (U2) — mention-pierce
                    # or cohort-only; every other recipient is held
                    # (obligation-only raise below).
                    wake_set = self._resolve_exchange_wake_set(
                        thread_id=thread_id,
                        mentions=mentions,
                        recipients=recipients,
                        open_exchange=open_exchange,
                    )
                    self._extend_reply_exchange_uncommitted(thread_id, seq)
                else:
                    # Outside E: unconditional Phase-2 mention routing.
                    wake_set = resolve_wake_set(
                        mentions, recipients, speaker,
                    )
            else:
                wake_set = recipients
            # U0: wake-set members mint/coalesce; every other recipient gets
            # the obligation-only raise (full-recipient obligations).
            for name in wake_set:
                arrivals.append(
                    self._apply_arrival_uncommitted(thread_id, name, seq)
                )
            for name in recipients:
                if name not in wake_set:
                    self._raise_required_uncommitted(thread_id, name, seq)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return seq, settlement, arrivals

    @_synchronized
    def claim_conversational_reply(
        self, token: str,
    ) -> ThreadReplyClaim | None:
        """Durable queued→running CAS for a conversational REPLY.

        Succeeds only when ``token`` is the pair's queued_invocation_token AND
        the receipt is a pending, unstarted, same-pair REPLY. In one
        transaction it transfers queued→running, snapshots the immutable
        inclusive range (running_from = acknowledged + 1, running_through =
        required), and stamps started_at. A duplicate/stale job returns None so
        the runner no-ops before any prompt/subprocess work.
        """
        now = _now().isoformat()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT * FROM thread_reply_delivery_state "
                "WHERE queued_invocation_token = ?",
                (token,),
            ).fetchone()
            if row is None or row["running_invocation_token"] is not None:
                self._conn.commit()
                return None
            inv = self._conn.execute(
                "SELECT * FROM thread_invocations WHERE invocation_token = ?",
                (token,),
            ).fetchone()
            valid = (
                inv is not None
                and inv["thread_id"] == row["thread_id"]
                and inv["agent_name"] == row["agent_name"]
                and inv["purpose"] == ThreadInvocationPurpose.REPLY.value
                and inv["status"] == ThreadInvocationStatus.PENDING.value
                and inv["started_at"] is None
            )
            if not valid:
                self._conn.commit()
                return None
            acknowledged = int(row["acknowledged_through_seq"] or 0)
            required = int(row["required_through_seq"] or 0)
            running_from = acknowledged + 1
            running_through = required
            self._conn.execute(
                "UPDATE thread_invocations SET started_at = ? "
                "WHERE invocation_token = ? AND status = 'pending'",
                (now, token),
            )
            self._conn.execute(
                "UPDATE thread_reply_delivery_state SET "
                "queued_invocation_token = NULL, "
                "running_invocation_token = ?, running_from_seq = ?, "
                "running_through_seq = ?, updated_at = ? "
                "WHERE thread_id = ? AND agent_name = ?",
                (token, running_from, running_through, now,
                 row["thread_id"], row["agent_name"]),
            )
            self._emit_reply_wake_audit(
                thread_id=row["thread_id"], agent_name=row["agent_name"],
                action="thread_reply_wake_claimed",
                payload={
                    "agent_name": row["agent_name"],
                    "from_seq": running_from,
                    "through_seq": running_through,
                    "token_prefix": token[:8],
                },
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return ThreadReplyClaim(
            thread_id=row["thread_id"],
            agent_name=row["agent_name"],
            invocation_token=token,
            acknowledged_through_seq=acknowledged,
            required_through_seq=required,
            running_from_seq=running_from,
            running_through_seq=running_through,
        )

    @_synchronized
    def discard_reply_delivery(
        self,
        thread_id: str,
        *,
        agent_name: str | None = None,
        decline_reason: str,
        status: ThreadInvocationStatus = ThreadInvocationStatus.FAILED,
    ) -> int:
        """Terminalize owned conversational REPLY state with an explicit
        discard boundary (abort / archive / participant removal).

        Terminalizes every pending REPLY invocation for (thread_id[,
        agent_name]) under ``status`` + ``decline_reason``, clears the queued/
        running ownership slots + range, and advances acknowledged to required
        so no queued/running/retry_required obligation survives and a later
        message starts after the boundary. Never touches BOOTSTRAP /
        TASK_FOLLOWUP rows and never resurrects a discarded wake.
        Returns the number of reply invocation rows terminalized.
        """
        now = _now().isoformat()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            # Snapshot the per-pair obligations BEFORE the terminalizing
            # UPDATE so each affected pair emits exactly one truthful cancelled
            # audit. Two obligation classes are merged: (a) every pair with a
            # pending REPLY invocation (legacy-only pairs without a state row
            # included), and (b) every pair with a live delivery-state
            # obligation — a queued/running token, or an unacknowledged
            # retry_required range — even when no pending receipt row exists
            # (e.g. a failed wake awaiting the next conversational arrival).
            # Boundary = state required watermark when present, else the
            # pair's max triggering seq.
            if agent_name is None:
                pair_rows = self._conn.execute(
                    "SELECT agent_name, COUNT(*) AS n, MAX(triggering_seq) "
                    "AS max_seq FROM thread_invocations "
                    "WHERE thread_id = ? AND status = 'pending' "
                    "AND purpose = 'reply' GROUP BY agent_name",
                    (thread_id,),
                ).fetchall()
                state_rows = self._conn.execute(
                    "SELECT agent_name FROM thread_reply_delivery_state "
                    "WHERE thread_id = ? AND (queued_invocation_token IS NOT NULL "
                    "OR running_invocation_token IS NOT NULL "
                    "OR required_through_seq > acknowledged_through_seq)",
                    (thread_id,),
                ).fetchall()
            else:
                pair_rows = self._conn.execute(
                    "SELECT agent_name, COUNT(*) AS n, MAX(triggering_seq) "
                    "AS max_seq FROM thread_invocations "
                    "WHERE thread_id = ? AND agent_name = ? "
                    "AND status = 'pending' AND purpose = 'reply' "
                    "GROUP BY agent_name",
                    (thread_id, agent_name),
                ).fetchall()
                state_rows = self._conn.execute(
                    "SELECT agent_name FROM thread_reply_delivery_state "
                    "WHERE thread_id = ? AND agent_name = ? "
                    "AND (queued_invocation_token IS NOT NULL "
                    "OR running_invocation_token IS NOT NULL "
                    "OR required_through_seq > acknowledged_through_seq)",
                    (thread_id, agent_name),
                ).fetchall()
            # Merge state-only pairs (n = 0 receipts terminalized by the sweep)
            # into the audit set without duplicate rows.
            seen: set[str] = set()
            merged: list[dict] = []
            for pr in pair_rows:
                merged.append(pr)
                seen.add(pr["agent_name"])
            for sr in state_rows:
                if sr["agent_name"] not in seen:
                    merged.append({"agent_name": sr["agent_name"], "n": 0,
                                   "max_seq": None})
                    seen.add(sr["agent_name"])
            if agent_name is None:
                cursor = self._conn.execute(
                    "UPDATE thread_invocations SET status = ?, decline_reason = ?, "
                    "consumed_at = ? WHERE thread_id = ? AND status = 'pending' "
                    "AND purpose = 'reply'",
                    (status.value, decline_reason, now, thread_id),
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE thread_invocations SET status = ?, decline_reason = ?, "
                    "consumed_at = ? WHERE thread_id = ? AND agent_name = ? "
                    "AND status = 'pending' AND purpose = 'reply'",
                    (status.value, decline_reason, now, thread_id, agent_name),
                )
            if agent_name is None:
                self._conn.execute(
                    "UPDATE thread_reply_breaker_episodes SET state='closed',"
                    "consecutive_failures=0,opened_at=NULL,cooldown_until=NULL,"
                    "probe_lease_id=NULL,last_failure_category=NULL,updated_at=? "
                    "WHERE thread_id=?",
                    (now, thread_id),
                )
            else:
                self._conn.execute(
                    "UPDATE thread_reply_breaker_episodes SET state='closed',"
                    "consecutive_failures=0,opened_at=NULL,cooldown_until=NULL,"
                    "probe_lease_id=NULL,last_failure_category=NULL,updated_at=? "
                    "WHERE thread_id=? AND agent_name=?",
                    (now, thread_id, agent_name),
                )
            if agent_name is None:
                self._conn.execute(
                    "UPDATE thread_reply_delivery_state SET "
                    "acknowledged_through_seq = required_through_seq, "
                    "queued_invocation_token = NULL, "
                    "running_invocation_token = NULL, running_from_seq = NULL, "
                    "running_through_seq = NULL, "
                    "last_terminal_reason = ?, last_terminal_at = ?, "
                    "updated_at = ? WHERE thread_id = ?",
                    (decline_reason, now, now, thread_id),
                )
                # TASK-5966: whole-thread discard (abort/archive) SUPPRESSES
                # any open exchange — no catch-up is ever minted (G5: the
                # human stopped). Idempotent per-exchange CAS.
                if decline_reason == "archive_started":
                    _exchange_reason = "thread_archived"
                else:
                    _exchange_reason = "founder_aborted"
                self._suppress_open_exchanges_uncommitted(
                    thread_id, reason=_exchange_reason,
                )
            else:
                self._conn.execute(
                    "UPDATE thread_reply_delivery_state SET "
                    "acknowledged_through_seq = required_through_seq, "
                    "queued_invocation_token = NULL, "
                    "running_invocation_token = NULL, running_from_seq = NULL, "
                    "running_through_seq = NULL, "
                    "last_terminal_reason = ?, last_terminal_at = ?, "
                    "updated_at = ? WHERE thread_id = ? AND agent_name = ?",
                    (decline_reason, now, now, thread_id, agent_name),
                )
                # TASK-5966: participant removal suppresses the removed
                # agent's held deferral rows in any open exchange (the pair is
                # terminal — a removed member never receives a catch-up). The
                # exchange itself stays open; closure fires at the next
                # evaluation point (write/reaper/reconcile) with proper
                # enqueue, so no catch-up token can strand.
                self._conn.execute(
                    "UPDATE thread_exchange_deferrals SET state = 'suppressed' "
                    "WHERE thread_id = ? AND agent_name = ? AND state = 'held'",
                    (thread_id, agent_name),
                )
            for pr in merged:
                pair_agent = pr["agent_name"]
                st = self._conn.execute(
                    "SELECT required_through_seq FROM thread_reply_delivery_state "
                    "WHERE thread_id = ? AND agent_name = ?",
                    (thread_id, pair_agent),
                ).fetchone()
                boundary = (
                    int(st["required_through_seq"] or 0)
                    if st is not None else int(pr["max_seq"] or 0)
                )
                self._emit_reply_wake_audit(
                    thread_id=thread_id, agent_name=pair_agent,
                    action="thread_reply_wake_cancelled",
                    payload={
                        "agent_name": pair_agent,
                        "boundary_seq": boundary,
                        "reason": decline_reason,
                        "swept_count": int(pr["n"]),
                    },
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return cursor.rowcount

    @_synchronized
    def list_reply_delivery_projections(
        self, thread_id: str,
    ) -> list[ReplyDeliveryProjection]:
        """Pair-level reply_delivery projection for a thread (server contract).

        Derived from authoritative reply-delivery and exchange state — never
        fabricated from per-message invocation rows. A fully-settled pair (nothing queued/
        running/required) is omitted; terminal history remains on the
        per-message responder strips. ``coalesced_message_count`` is the number
        of transcript rows the wake's range covers (COUNT, not subtraction).
        """
        rows = self._conn.execute(
            "SELECT * FROM thread_reply_delivery_state WHERE thread_id = ? "
            "ORDER BY agent_name",
            (thread_id,),
        ).fetchall()
        out: list[ReplyDeliveryProjection] = []
        for row in rows:
            acknowledged = int(row["acknowledged_through_seq"] or 0)
            required = int(row["required_through_seq"] or 0)
            queued = row["queued_invocation_token"]
            running = row["running_invocation_token"]
            running_from = row["running_from_seq"]
            running_through = row["running_through_seq"]
            started_at = None
            held = self._conn.execute(
                "SELECT 1 FROM thread_reply_exchange e "
                "JOIN thread_exchange_deferrals d ON d.thread_id=e.thread_id "
                "AND d.exchange_id=e.exchange_id WHERE e.thread_id=? "
                "AND e.state='open' AND d.agent_name=? AND d.state='held' LIMIT 1",
                (thread_id, row["agent_name"]),
            ).fetchone()
            if held is not None and running is None and queued is None:
                state = "held"
                from_seq = acknowledged + 1
                through_seq = required
            elif running is not None:
                state = "running"
                from_seq = int(running_from or 0)
                through_seq = int(running_through or 0)
                inv = self._conn.execute(
                    "SELECT started_at FROM thread_invocations "
                    "WHERE invocation_token = ?",
                    (running,),
                ).fetchone()
                if inv is not None:
                    started_at = inv["started_at"]
            elif queued is not None:
                state = "queued"
                from_seq = acknowledged + 1
                through_seq = required
            elif required > acknowledged:
                if held is not None:
                    state = "held"
                else:
                    state = "retry_required"
                from_seq = acknowledged + 1
                through_seq = required
            else:
                continue  # fully settled — omit from the live projection
            cnt = self._conn.execute(
                "SELECT COUNT(*) AS n FROM thread_messages "
                "WHERE thread_id = ? AND seq >= ? AND seq <= ?",
                (thread_id, from_seq, through_seq),
            ).fetchone()
            out.append(ReplyDeliveryProjection(
                agent_name=row["agent_name"],
                state=state,  # type: ignore[arg-type]
                from_seq=from_seq,
                through_seq=through_seq,
                coalesced_message_count=int(cnt["n"]),
                started_at=started_at,
                updated_at=row["updated_at"],
                # Terminal reason is historical metadata, never state proof.
                # Expose it only for a currently genuine retry diagnostic.
                last_terminal_reason=(
                    row["last_terminal_reason"]
                    if state == "retry_required" else None
                ),
                current_failure_category=(
                    reply_failure_category("failed", row["last_terminal_reason"])
                    if state == "retry_required" else None
                ),
            ))
        return out

    @_synchronized
    def increment_thread_turns_used(self, thread_id: str, *, by: int = 1) -> None:
        self._conn.execute(
            "UPDATE threads SET turns_used = turns_used + ? WHERE id = ?",
            (by, thread_id),
        )
        self._conn.commit()

    @_synchronized
    def set_thread_status(
        self,
        thread_id: str,
        *,
        status: ThreadStatus,
        summary: str | None = None,
    ) -> None:
        if status is ThreadStatus.ARCHIVED:
            self._set_thread_status_archived_uncommitted(
                thread_id, summary=summary,
            )
        else:
            # OPEN (resume): plain status flip; archived_at + summary preserved as historical record.
            self._conn.execute(
                "UPDATE threads SET status = ? WHERE id = ?",
                (status.value, thread_id),
            )
        self._conn.commit()

    @_synchronized
    def _set_thread_status_archived_uncommitted(
        self, thread_id: str, *, summary: str | None = None,
    ) -> None:
        """Flip one thread to ARCHIVED (summary/archived_at preserved) WITHOUT
        committing. Callers own the transaction
        (``set_thread_status``, ``archive_thread_and_reset_sessions``)."""
        now = _now().isoformat()
        self._conn.execute(
            "UPDATE threads SET status = ?, summary = COALESCE(?, summary), "
            "archived_at = COALESCE(archived_at, ?) WHERE id = ?",
            (ThreadStatus.ARCHIVED.value, summary, now, thread_id),
        )

    @_synchronized
    def set_thread_subject(self, thread_id: str, *, subject: str) -> None:
        """Update a thread's display title (THR-209 rename).

        Identity (id), participants, routing, unread, and lifecycle are
        untouched — only the durable ``subject`` changes. The caller is
        responsible for the ``thread_renamed`` audit row.
        """
        self._conn.execute(
            "UPDATE threads SET subject = ? WHERE id = ?",
            (subject, thread_id),
        )
        self._conn.commit()

    @_synchronized
    def set_thread_pinned(self, thread_id: str, *, pinned: bool) -> None:
        """Set/clear founder-workspace pin state (THR-209).

        Pin state is presentation-only: this write touches ``pinned_at`` and
        nothing else — no message, notification, participant, unread, or
        activity-timestamp effect. The caller is responsible for the
        ``thread_pinned``/``thread_unpinned`` audit row.
        """
        if pinned:
            self._conn.execute(
                "UPDATE threads SET pinned_at = ? WHERE id = ?",
                (_now().isoformat(), thread_id),
            )
        else:
            self._conn.execute(
                "UPDATE threads SET pinned_at = NULL WHERE id = ?",
                (thread_id,),
            )
        self._conn.commit()

    @_synchronized
    def set_thread_subject_uncommitted(self, thread_id: str, *, subject: str) -> None:
        """Update a thread's subject WITHOUT committing (THR-209 rename).

        Deliberately left UNCOMMITTED: the caller owns the surrounding
        transaction (``BEGIN IMMEDIATE`` … ``commit()``/``rollback()``) so the
        rename and its ``thread_renamed`` audit row commit atomically. This
        helper never commits independently inside an atomic unit (TASK-5644).
        """
        self._conn.execute(
            "UPDATE threads SET subject = ? WHERE id = ?",
            (subject, thread_id),
        )

    @_synchronized
    def set_thread_pinned_uncommitted(self, thread_id: str, *, pinned: bool) -> None:
        """Set/clear thread pin state WITHOUT committing (THR-209).

        Same contract as ``set_thread_subject_uncommitted``: the caller owns
        the surrounding transaction so the pin transition and its audit row
        are atomic.
        """
        if pinned:
            self._conn.execute(
                "UPDATE threads SET pinned_at = ? WHERE id = ?",
                (_now().isoformat(), thread_id),
            )
        else:
            self._conn.execute(
                "UPDATE threads SET pinned_at = NULL WHERE id = ?",
                (thread_id,),
            )

    @_synchronized
    def rename_thread_with_audit(
        self, thread_id: str, *, subject: str, actor: str = "founder",
    ) -> bool:
        """Atomic founder rename + ``thread_renamed`` audit row (THR-209).

        ONE rollback-safe transaction: the authoritative subject read, the
        idempotence decision, the subject UPDATE, and the audit row insert
        commit together — and roll back together on ANY failure — so a rename
        can never survive without its audit row and concurrent renames always
        record the truthful sequential old→new chain (last successful save
        wins). The whole unit holds the connection lock, so no other thread
        can join or commit the open transaction from the inside.

        The audit row keeps the documented ``audit_log.task_id`` = THR-* scope
        (``task_id`` = thread id), the founder ``actor``, and the
        ``{old_subject, new_subject}`` payload shape.

        Returns True when a durable transition occurred; False for an
        identical (no-op) save — true no-ops write nothing and are not
        audited. Raises ValueError for an unknown thread.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT subject FROM threads WHERE id = ?", (thread_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"thread {thread_id} not found")
            old_subject = row["subject"]
            if old_subject == subject:
                self._conn.rollback()
                return False
            self.set_thread_subject_uncommitted(thread_id, subject=subject)
            self.insert_audit_log_uncommitted(
                task_id=thread_id,
                agent=actor,
                action="thread_renamed",
                payload={"old_subject": old_subject, "new_subject": subject},
            )
            self._conn.commit()
            return True
        except BaseException:
            self._conn.rollback()
            raise

    @_synchronized
    def set_thread_pinned_with_audit(
        self, thread_id: str, *, pinned: bool, actor: str = "founder",
    ) -> bool:
        """Atomic founder pin/unpin + audit row (THR-209).

        ONE rollback-safe transaction: the authoritative ``pinned_at`` read,
        the idempotence decision, the pin state UPDATE, and the
        ``thread_pinned``/``thread_unpinned`` audit row commit together — and
        roll back together on ANY failure — so pin state can never survive
        without its audit row. Concurrent same-state requests yield exactly
        one audit row for the one durable transition (the loser is a true
        no-op); opposite-state requests re-read the durable state inside their
        transaction, so neither is misclassified from a stale pre-lock
        snapshot. The whole unit holds the connection lock, so no other thread
        can join the open transaction.

        The audit row keeps the documented ``audit_log.task_id`` = THR-* scope
        (``task_id`` = thread id), the founder ``actor``, and the
        ``{pinned}`` payload shape.

        Returns True when a durable transition occurred; False for a
        same-state (no-op) save — true no-ops write nothing and are not
        audited. Raises ValueError for an unknown thread.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT pinned_at FROM threads WHERE id = ?", (thread_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"thread {thread_id} not found")
            currently_pinned = row["pinned_at"] is not None
            if currently_pinned == pinned:
                self._conn.rollback()
                return False
            self.set_thread_pinned_uncommitted(thread_id, pinned=pinned)
            self.insert_audit_log_uncommitted(
                task_id=thread_id,
                agent=actor,
                action="thread_pinned" if pinned else "thread_unpinned",
                payload={"pinned": pinned},
            )
            self._conn.commit()
            return True
        except BaseException:
            self._conn.rollback()
            raise

    @_synchronized
    def set_thread_transcript_path(
        self, thread_id: str, transcript_path: str,
    ) -> None:
        """Persist the transcript path for an archived thread."""
        self._conn.execute(
            "UPDATE threads SET transcript_path = ? WHERE id = ?",
            (transcript_path, thread_id),
        )
        self._conn.commit()

    @_synchronized
    def set_thread_turn_cap(self, thread_id: str, *, new_cap: int) -> None:
        self._conn.execute(
            "UPDATE threads SET turn_cap = ? WHERE id = ?",
            (new_cap, thread_id),
        )
        self._conn.commit()

    @_synchronized
    def bump_thread_turn_cap(self, thread_id: str, *, delta: int = 1) -> int:
        """Atomically increment turn_cap by ``delta`` and return the new value.

        Used by the task-followup hook to make room for the system-triggered
        re-invocation when the projected turn count would exceed the current
        cap.  Each bump is audited at the call site via
        log_thread_turn_cap_auto_extended.
        """
        cursor = self._conn.execute(
            "UPDATE threads SET turn_cap = turn_cap + ? WHERE id = ? "
            "RETURNING turn_cap",
            (delta, thread_id),
        )
        row = cursor.fetchone()
        self._conn.commit()
        if row is None:
            raise KeyError(f"thread {thread_id} not found")
        return int(row["turn_cap"])

    @_synchronized
    def mint_followup_invocation_with_cap_extend(
        self,
        thread_id: str,
        *,
        agent_name: str,
        triggering_seq: int,
        cap_delta_if_over: int = 1,
    ) -> "tuple[ThreadInvocation, int | None]":
        """Atomically mint a TASK_FOLLOWUP invocation, auto-extending turn_cap
        by ``cap_delta_if_over`` if the projection (turns_used + pending + 1)
        would exceed the current cap.

        Returns (minted_invocation, new_cap_if_bumped_else_None).

        Closes the TOCTOU race where two concurrent root-task completions on the
        same thread both observe pending=N, both skip the bump, both mint, and
        leave the thread with more counted obligations than turn_cap permits.
        The @_synchronized lock on this method (backed by threading.RLock)
        serializes the read-compare-bump-mint sequence.

        Because Database._lock is an RLock (re-entrant), calling
        self.mint_thread_invocation from within this @_synchronized method is
        safe — the same thread can re-acquire the lock without deadlock.
        """
        # Read thread state under the @_synchronized lock.
        cur = self._conn.execute(
            "SELECT turns_used, turn_cap FROM threads WHERE id = ?",
            (thread_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"thread {thread_id} not found")
        turns_used = int(row["turns_used"])
        turn_cap = int(row["turn_cap"])

        counted = (
            ThreadInvocationPurpose.REPLY.value,
            ThreadInvocationPurpose.BOOTSTRAP.value,
            ThreadInvocationPurpose.TASK_FOLLOWUP.value,
        )
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM thread_invocations "
            "WHERE thread_id = ? AND status = ? AND purpose IN ({})".format(
                ",".join("?" * len(counted))
            ),
            (thread_id, ThreadInvocationStatus.PENDING.value, *counted),
        )
        pending = int(cur.fetchone()["n"])

        projected = turns_used + pending + 1
        new_cap: int | None = None
        if projected > turn_cap:
            self._conn.execute(
                "UPDATE threads SET turn_cap = turn_cap + ? WHERE id = ?",
                (cap_delta_if_over, thread_id),
            )
            new_cap = turn_cap + cap_delta_if_over

        # Delegate to mint_thread_invocation — safe because RLock is re-entrant.
        inv = self.mint_thread_invocation(
            thread_id=thread_id,
            agent_name=agent_name,
            triggering_seq=triggering_seq,
            purpose=ThreadInvocationPurpose.TASK_FOLLOWUP,
        )
        # No separate commit needed: mint_thread_invocation commits inside its
        # own @_synchronized acquisition. The cap UPDATE above is committed by
        # mint_thread_invocation's commit (SQLite commits all pending changes).
        return inv, new_cap

    @_synchronized
    # --- Dreams ---

    @_synchronized
    def next_dream_id(self) -> str:
        cursor = self._conn.execute(
            "SELECT MAX(CAST(SUBSTR(id, 7) AS INTEGER)) AS m "
            "FROM dreams WHERE id GLOB 'DREAM-[0-9]*'"
        )
        n = (cursor.fetchone()["m"] or 0) + 1
        return f"DREAM-{n:03d}"

    def _dream_row_to_model(self, row) -> DreamRecord:
        return DreamRecord(
            id=row["id"],
            agent_name=row["agent_name"],
            local_date=row["local_date"],
            scheduled_for=_parse_dt(row["scheduled_for"]),
            window_start=_parse_dt(row["window_start"]) if row["window_start"] else None,
            window_end=_parse_dt(row["window_end"]),
            started_at=_parse_dt(row["started_at"]) if row["started_at"] else None,
            ended_at=_parse_dt(row["ended_at"]) if row["ended_at"] else None,
            status=DreamStatus(row["status"]),
            summary=row["summary"],
            transcript_path=row["transcript_path"],
            new_learnings_count=row["new_learnings_count"],
            kb_candidate_count=row["kb_candidate_count"],
            founder_thread_id=row["founder_thread_id"],
            session_id=row["session_id"],
            error=row["error"],
            created_at=_parse_dt(row["created_at"]),
        )

    @_synchronized
    def insert_dream(self, dream: DreamRecord) -> None:
        self._conn.execute(
            """INSERT INTO dreams (
                id, agent_name, local_date, scheduled_for, window_start, window_end,
                started_at, ended_at, status, summary, transcript_path,
                new_learnings_count, kb_candidate_count, founder_thread_id,
                session_id, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dream.id, dream.agent_name, dream.local_date,
                dream.scheduled_for.isoformat(),
                dream.window_start.isoformat() if dream.window_start else None,
                dream.window_end.isoformat(),
                dream.started_at.isoformat() if dream.started_at else None,
                dream.ended_at.isoformat() if dream.ended_at else None,
                dream.status.value, dream.summary, dream.transcript_path,
                dream.new_learnings_count, dream.kb_candidate_count,
                dream.founder_thread_id, dream.session_id, dream.error,
                dream.created_at.isoformat(),
            ),
        )
        self._conn.commit()

    @_synchronized
    def get_dream(self, dream_id: str) -> DreamRecord | None:
        row = self._conn.execute("SELECT * FROM dreams WHERE id = ?", (dream_id,)).fetchone()
        return self._dream_row_to_model(row) if row else None

    @_synchronized
    def get_dream_for_agent_date(self, agent_name: str, local_date: str) -> DreamRecord | None:
        row = self._conn.execute(
            "SELECT * FROM dreams WHERE agent_name = ? AND local_date = ?",
            (agent_name, local_date),
        ).fetchone()
        return self._dream_row_to_model(row) if row else None

    @_synchronized
    def list_dreams(self, *, agent: str | None = None, limit: int = 50) -> list[DreamRecord]:
        limit = max(1, min(limit, 500))
        params: list[object] = []
        where = ""
        if agent is not None:
            where = "WHERE agent_name = ?"
            params.append(agent)
        rows = self._conn.execute(
            f"SELECT * FROM dreams {where} ORDER BY scheduled_for DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._dream_row_to_model(row) for row in rows]

    @_synchronized
    def list_dream_ids_by_status(self, statuses: set[str]) -> list[str]:
        """Exhaustive status-filtered dream-id query (no cap, DB-side filter).

        ``list_dreams`` is a presentation list capped at 500 and ordered
        newest-first; using it for a liveness check can hide an old active row
        behind 500 newer terminal rows. This returns every dream id whose
        status is in ``statuses`` so a portability preflight cannot miss an
        active dream. Read-only.
        """
        if not statuses:
            return []
        placeholders = ",".join("?" * len(statuses))
        rows = self._conn.execute(
            f"SELECT id FROM dreams WHERE status IN ({placeholders})",
            tuple(sorted(statuses)),
        ).fetchall()
        return [row["id"] for row in rows]

    @_synchronized
    def get_last_successful_dream(self, agent_name: str) -> DreamRecord | None:
        row = self._conn.execute(
            "SELECT * FROM dreams WHERE agent_name = ? AND status = 'completed' "
            "ORDER BY ended_at DESC LIMIT 1",
            (agent_name,),
        ).fetchone()
        return self._dream_row_to_model(row) if row else None

    @_synchronized
    def update_dream(self, dream_id: str, **fields: object) -> None:
        allowed = {
            "started_at", "ended_at", "status", "summary", "transcript_path",
            "new_learnings_count", "kb_candidate_count", "founder_thread_id",
            "session_id", "error",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unsupported dream fields: {sorted(bad)}")
        if not fields:
            return
        values = []
        assignments = []
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            if hasattr(value, "value"):
                value = value.value
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            values.append(value)
        values.append(dream_id)
        self._conn.execute(
            f"UPDATE dreams SET {', '.join(assignments)} WHERE id = ?",
            values,
        )
        self._conn.commit()

    @_synchronized
    def update_dream_status_if(
        self,
        dream_id: str,
        expected_status: DreamStatus,
        new_status: DreamStatus,
        **fields: object,
    ) -> bool:
        """Atomically transition a dream only if it is still ``expected_status``.

        Returns ``True`` if the row was updated, ``False`` if the expected
        status no longer matched (e.g. a concurrent termination set it to
        SKIPPED). Extra fields are persisted only on a successful transition.
        """
        allowed = {
            "started_at", "ended_at", "summary", "transcript_path",
            "new_learnings_count", "kb_candidate_count", "founder_thread_id",
            "session_id", "error",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unsupported dream fields: {sorted(bad)}")
        assignments = ["status = ?"]
        values: list[object] = [new_status.value]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            values.append(value)
        values.append(dream_id)
        values.append(expected_status.value)
        cursor = self._conn.execute(
            f"UPDATE dreams SET {', '.join(assignments)} WHERE id = ? AND status = ?",
            values,
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def terminate_agent_cleanups(
        self, agent_name: str,
        *,
        audit_scope_id: str | None = None,
        audit_agent: str | None = None,
    ) -> None:
        """Atomically cancel/skip/decline all future work for ``agent_name``.

        Runs every cleanup DML statement and each audit write inside ONE
        explicit SQLite transaction (``BEGIN IMMEDIATE`` / ``COMMIT``). On any
        exception the COMPLETE transaction is rolled back BEFORE the exception
        propagates, so control returns to the caller with no open transaction
        and no partial cancellation or audit residue. The caller is responsible
        for archiving the AgentDef/workspace and removing team membership first
        (or rolling them back if this method raises).

        THR-200: when ``audit_scope_id`` is given, the provider-session reset
        (every thread participant row owned by the agent -> id NULL, watermark
        0) and its ``thread_session_invalidated`` audit run inside the SAME
        transaction — a terminated agent must never resume a provider session,
        and a reset/audit failure rolls back the complete cleanup so the agent
        stays fully active with prior session state intact.
        """
        now_iso = _now().isoformat()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Cancel armed schedules.
            schedule_rows = self._conn.execute(
                "SELECT id FROM schedules WHERE agent_name = ? AND status = ?",
                (agent_name, ScheduleStatus.ARMED.value),
            ).fetchall()
            if schedule_rows:
                self._conn.execute(
                    "UPDATE schedules SET status = ?, active = 0, updated_at = ? "
                    "WHERE agent_name = ? AND status = ?",
                    (ScheduleStatus.CANCELLED.value, now_iso, agent_name, ScheduleStatus.ARMED.value),
                )
                for row in schedule_rows:
                    self.insert_audit_log_uncommitted(
                        task_id=row["id"],
                        agent=agent_name,
                        action="schedule_cancelled",
                        payload={"reason": "agent_terminated"},
                    )

            # Skip pending work-hours wakes.
            wake_rows = self._conn.execute(
                "SELECT id FROM work_hours WHERE agent_name = ? AND status = ?",
                (agent_name, WorkHourStatus.PENDING.value),
            ).fetchall()
            if wake_rows:
                self._conn.execute(
                    "UPDATE work_hours SET status = ?, ended_at = ?, error = ? "
                    "WHERE agent_name = ? AND status = ?",
                    (WorkHourStatus.SKIPPED.value, now_iso, "agent_terminated", agent_name, WorkHourStatus.PENDING.value),
                )
                for row in wake_rows:
                    self.insert_audit_log_uncommitted(
                        task_id=row["id"],
                        agent=agent_name,
                        action="work_hour_skipped",
                        payload={"reason": "agent_terminated"},
                    )

            # Skip pending dreams.
            dream_rows = self._conn.execute(
                "SELECT id FROM dreams WHERE agent_name = ? AND status = ?",
                (agent_name, DreamStatus.PENDING.value),
            ).fetchall()
            if dream_rows:
                self._conn.execute(
                    "UPDATE dreams SET status = ?, ended_at = ?, error = ? "
                    "WHERE agent_name = ? AND status = ?",
                    (DreamStatus.SKIPPED.value, now_iso, "agent_terminated", agent_name, DreamStatus.PENDING.value),
                )
                for row in dream_rows:
                    self.insert_audit_log_uncommitted(
                        task_id=row["id"],
                        agent=agent_name,
                        action="dream_skipped",
                        payload={"reason": "agent_terminated"},
                    )

            # Decline not-yet-started thread invocations.
            self._conn.execute(
                "UPDATE thread_invocations "
                "SET status = ?, decline_reason = ?, consumed_at = ? "
                "WHERE agent_name = ? AND status = ? AND started_at IS NULL",
                (
                    ThreadInvocationStatus.DECLINED.value,
                    "agent_terminated",
                    now_iso,
                    agent_name,
                    ThreadInvocationStatus.PENDING.value,
                ),
            )

            # THR-200: a terminated agent must never resume a thread provider
            # session. Clear every participant row it owns and record the
            # invalidation audit inside this same transaction — a failure here
            # rolls back the whole cleanup, leaving the agent fully active
            # with prior session state intact.
            session_rows = self._reset_thread_sessions_for_agent_uncommitted(
                agent_name,
            )
            if session_rows and audit_scope_id is not None:
                self.insert_audit_log_uncommitted(
                    task_id=audit_scope_id,
                    agent=audit_agent,
                    action="thread_session_invalidated",
                    payload={
                        "reason": "termination",
                        "rows": session_rows,
                        "name": agent_name,
                    },
                )

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _dream_candidate_row_to_model(self, row) -> DreamKbCandidate:
        return DreamKbCandidate(
            id=row["id"],
            dream_id=row["dream_id"],
            agent_name=row["agent_name"],
            slug=row["slug"],
            title=row["title"],
            topic=row["topic"],
            rationale=row["rationale"],
            body_markdown=row["body_markdown"],
            status=row["status"],
            promoted_kb_slug=row["promoted_kb_slug"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    @_synchronized
    def insert_dream_kb_candidate(self, candidate: DreamKbCandidate) -> None:
        self._conn.execute(
            """INSERT INTO dream_kb_candidates (
                dream_id, agent_name, slug, title, topic, rationale,
                body_markdown, status, promoted_kb_slug, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate.dream_id, candidate.agent_name, candidate.slug,
                candidate.title, candidate.topic, candidate.rationale,
                candidate.body_markdown, candidate.status,
                candidate.promoted_kb_slug, candidate.created_at.isoformat(),
                candidate.updated_at.isoformat(),
            ),
        )
        self._conn.commit()

    @_synchronized
    def list_dream_kb_candidates(
        self,
        *,
        dream_id: str | None = None,
        agent: str | None = None,
        candidate_id: int | None = None,
    ) -> list[DreamKbCandidate]:
        clauses = []
        params: list[object] = []
        if dream_id is not None:
            clauses.append("dream_id = ?")
            params.append(dream_id)
        if agent is not None:
            clauses.append("agent_name = ?")
            params.append(agent)
        if candidate_id is not None:
            clauses.append("id = ?")
            params.append(candidate_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM dream_kb_candidates {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [self._dream_candidate_row_to_model(row) for row in rows]

    @_synchronized
    def update_dream_kb_candidate(
        self,
        candidate_id: int,
        *,
        status: str,
        promoted_kb_slug: str | None = None,
    ) -> None:
        allowed = {"pending", "promoted", "rejected", "superseded"}
        if status not in allowed:
            raise ValueError(f"invalid status: {status!r}, expected one of {sorted(allowed)}")
        now = _now().isoformat()
        params: list[object] = [status, now]
        slug_assign = ""
        if promoted_kb_slug is not None:
            slug_assign = ", promoted_kb_slug = ?"
            params.append(promoted_kb_slug)
        params.append(candidate_id)
        cursor = self._conn.execute(
            f"UPDATE dream_kb_candidates SET status = ?, updated_at = ?{slug_assign} WHERE id = ?",
            params,
        )
        if cursor.rowcount == 0:
            raise ValueError(f"dream_kb_candidate {candidate_id} not found")
        self._conn.commit()

    # --- Escalation Notifications ---

    @_synchronized
    def mint_escalation_notification(
        self,
        feishu_message_id: str,
        org_slug: str,
        task_id: str,
        chat_id: str,
        expires_at: datetime,
        kind: str = "escalation",
    ) -> None:
        if kind not in ("escalation", "failure", "job_request"):
            raise ValueError(
                f"kind must be 'escalation', 'failure', or 'job_request', got {kind!r}"
            )
        expires_at_str = expires_at.astimezone(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO escalation_notifications
               (feishu_message_id, org_slug, task_id, chat_id,
                created_at, expires_at, consumed_at, consumed_by, kind)
               VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
            (
                feishu_message_id, org_slug, task_id, chat_id,
                datetime.now(timezone.utc).isoformat(),
                expires_at_str,
                kind,
            ),
        )
        self._conn.commit()

    @_synchronized
    def get_escalation_notification(self, feishu_message_id: str) -> dict | None:
        cur = self._conn.execute(
            """SELECT feishu_message_id, org_slug, task_id, chat_id,
                      created_at, expires_at, consumed_at, consumed_by, kind
               FROM escalation_notifications WHERE feishu_message_id = ?""",
            (feishu_message_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    @_synchronized
    def get_latest_notification_for_sr(
        self, job_id: str, *, kind: str,
    ) -> dict | None:
        """Look up the most-recent escalation_notifications row for an SR.

        Used by the terminal-result follow-up: when a Feishu-initiated script run
        finishes, we post a threaded reply to the original push's message_id.
        Returns consumed rows too — the APPROVE reply consumes the row, but the
        parent message_id is still needed to thread the result post.
        """
        cur = self._conn.execute(
            """SELECT feishu_message_id, org_slug, task_id, chat_id,
                      created_at, expires_at, consumed_at, consumed_by, kind
               FROM escalation_notifications
               WHERE task_id = ? AND kind = ?
               ORDER BY created_at DESC LIMIT 1""",
            (job_id, kind),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    @_synchronized
    def consume_escalation_notification(
        self, feishu_message_id: str, consumed_by: str,
    ) -> bool:
        """Atomically mark a notification consumed. Returns True on first
        consume, False if already consumed or missing."""
        cur = self._conn.execute(
            """UPDATE escalation_notifications
               SET consumed_at = ?, consumed_by = ?
               WHERE feishu_message_id = ? AND consumed_at IS NULL""",
            (datetime.now(timezone.utc).isoformat(), consumed_by, feishu_message_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    # --- Processed Event Dedup ---

    @_synchronized
    def record_processed_event(
        self,
        org_slug: str,
        feishu_event_id: str,
        outcome: str,
        reason: str | None,
    ) -> bool:
        """INSERT OR IGNORE into the dedup table. Returns True on first insert,
        False on duplicate."""
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO processed_event_ids
               (org_slug, feishu_event_id, processed_at, outcome, reason)
               VALUES (?, ?, ?, ?, ?)""",
            (
                org_slug, feishu_event_id,
                datetime.now(timezone.utc).isoformat(),
                outcome, reason,
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    @_synchronized
    def update_processed_event_outcome(
        self,
        org_slug: str,
        feishu_event_id: str,
        outcome: str,
        reason: str | None = None,
    ) -> None:
        """Update the outcome on an existing processed_event_ids row. Used when
        the listener has decided how the event was disposed (consumed/rejected/ignored)."""
        self._conn.execute(
            """UPDATE processed_event_ids
               SET outcome = ?, reason = ?
               WHERE org_slug = ? AND feishu_event_id = ?""",
            (outcome, reason, org_slug, feishu_event_id),
        )
        self._conn.commit()

    @_synchronized
    def list_open_notifications_for_task(self, task_id: str) -> list[dict]:
        """Return un-consumed notification rows for a task. Used by CLI
        resolve-escalation to mark the matching Feishu row consumed."""
        cur = self._conn.execute(
            """SELECT feishu_message_id, org_slug, task_id, chat_id,
                      created_at, expires_at, consumed_at, consumed_by, kind
               FROM escalation_notifications
               WHERE task_id = ? AND consumed_at IS NULL""",
            (task_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    # --- THR-181 Track A: authority candidate/evaluation/audit API ---
    #
    # Narrow, additive persistence for the pre-escalation authority-evaluation
    # foundation, consumed by the authority hook (runtime/orchestrator/
    # authority.py). No evaluator is invoked HERE and no policy is enforced
    # HERE — these methods only persist/read controlled records — but they are
    # the durable surface the hook claims, records, and consumes through.
    # Prose-bearing content is stored as digests; raw bearer/provider
    # credentials, task prose, and unredacted model exchanges are never
    # accepted or persisted.

    def _authority_policy_release_from_row(self, row) -> AuthorityPolicyRelease:
        try:
            release = AuthorityPolicyRelease.model_validate(dict(row))
        except ValueError as exc:
            raise ValueError("authority policy release has corrupt digest or semantics") from exc
        canonical_digest = hashlib.sha256(
            release.canonical_payload_json.encode("utf-8")
        ).hexdigest()
        if canonical_digest != release.policy_digest:
            raise ValueError(f"authority policy release {release.id!r} has a corrupt digest")
        return release

    def _authority_policy_activation_from_row(self, row) -> AuthorityPolicyActivation:
        try:
            activation = AuthorityPolicyActivation.model_validate(dict(row))
        except ValueError as exc:
            raise ValueError("authority policy activation has corrupt digest or semantics") from exc
        release = self.get_authority_policy_release(activation.release_id)
        if release is None or release.team != activation.team:
            raise ValueError(f"authority policy activation {activation.id!r} has corrupt linkage")
        self._validate_authority_activation_history(activation)
        return activation

    def _validate_authority_activation_history(
        self, receipt: AuthorityPolicyActivation
    ) -> None:
        """Replay the sealed team history through the requested receipt."""
        rows = self._conn.execute(
            "SELECT * FROM authority_policy_activations "
            "WHERE team=? AND epoch<=? ORDER BY epoch",
            (receipt.team, receipt.epoch),
        ).fetchall()
        if len(rows) != receipt.epoch or not rows:
            raise ValueError(f"authority policy activation {receipt.id!r} has corrupt history")
        prior = None
        activated_release_ids: set[str] = set()
        for expected_epoch, row in enumerate(rows, 1):
            try:
                item = AuthorityPolicyActivation.model_validate(dict(row))
            except ValueError as exc:
                raise ValueError("authority policy activation history is corrupt") from exc
            release = self.get_authority_policy_release(item.release_id)
            if release is None or release.team != item.team or item.epoch != expected_epoch:
                raise ValueError("authority policy activation history linkage is corrupt")
            if prior is None:
                valid = (
                    item.action == "bootstrap"
                    and item.previous_activation_id is None
                    and item.expected_previous_epoch in (None, 0)
                )
            else:
                valid = (
                    item.previous_activation_id == prior.id
                    and item.expected_previous_epoch == prior.epoch
                    and item.action != "bootstrap"
                )
                if valid and item.action == "activate":
                    valid = item.release_id not in activated_release_ids
                elif valid and item.action == "reactivate_rollback":
                    current = self.get_authority_policy_release(prior.release_id)
                    valid = (
                        item.release_id in activated_release_ids
                        and item.release_id != prior.release_id
                        and current is not None
                        and release.policy_id == current.policy_id
                        and release.version < current.version
                    )
            if not valid:
                raise ValueError("authority policy activation history semantics are corrupt")
            activated_release_ids.add(item.release_id)
            prior = item
        if prior is None or prior.id != receipt.id:
            raise ValueError(f"authority policy activation {receipt.id!r} has corrupt history")

    @_synchronized
    def create_authority_policy_release(self, release: AuthorityPolicyRelease) -> AuthorityPolicyRelease:
        """Append an immutable release, idempotent only for an exact record."""
        # Pydantic intentionally reuses an already-constructed model instance.
        # Rebuild from a deep primitive snapshot so post-validation mutation,
        # including a nested value smuggled into a typed field, cannot bypass
        # canonical semantic identity validation at the durable boundary.
        release = AuthorityPolicyRelease.model_validate(
            release.model_dump(mode="python", round_trip=True, warnings=False)
        )
        expected = hashlib.sha256(release.canonical_payload_json.encode("utf-8")).hexdigest()
        if expected != release.policy_digest:
            raise ValueError("policy_digest does not match canonical_payload_json")
        values = release.model_dump(mode="json")
        try:
            self._conn.execute(
                """INSERT INTO authority_policy_releases
                   (id,team,policy_id,version,title,normative_text,clauses_json,
                    continuation_phrase,canonical_payload_json,policy_digest,
                    based_on_release_id,actor_kind,created_at)
                   VALUES (:id,:team,:policy_id,:version,:title,:normative_text,
                           :clauses_json,:continuation_phrase,:canonical_payload_json,
                           :policy_digest,:based_on_release_id,:actor_kind,:created_at)""",
                values,
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            row = self._conn.execute(
                "SELECT * FROM authority_policy_releases WHERE id=? OR policy_digest=?",
                (release.id, release.policy_digest),
            ).fetchone()
            if row is not None and dict(row) == values:
                return release
            raise
        return release

    @_synchronized
    def create_authority_policy_release_with_audit(
        self,
        release: AuthorityPolicyRelease,
        *,
        request_id: str,
        request_digest: str,
    ) -> AuthorityPolicyRelease:
        """Resolve replay, then append a release and closed audit atomically."""
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ValueError("request_id must be a non-empty string of at most 128 characters")
        validate_authority_digest(request_digest, "request_digest")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                "SELECT task_id,agent,payload FROM audit_log "
                "WHERE action=? ORDER BY id",
                ("authority_policy_release_created",),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("authority policy release audit receipt is corrupt") from exc
                if not isinstance(payload, dict):
                    raise ValueError("authority policy release audit receipt is corrupt")
                release_id = payload.get("release_id")
                persisted = (
                    self.get_authority_policy_release(release_id)
                    if isinstance(release_id, str)
                    else None
                )
                if persisted is None:
                    raise ValueError("authority policy release audit linkage is corrupt")
                expected_payload = {
                    "action": "create_release",
                    "actor_kind": "shared_local_operator_credential",
                    "policy_digest": persisted.policy_digest,
                    "release_id": persisted.id,
                    "request_digest": payload.get("request_digest"),
                    "request_id": payload.get("request_id"),
                    "team": persisted.team,
                }
                if (
                    payload != expected_payload
                    or row["task_id"] != f"config:authority-policy:{persisted.team}"
                    or row["agent"] != "shared_local_operator_credential"
                ):
                    raise ValueError("authority policy release audit receipt is corrupt")
                if payload.get("request_id") != request_id:
                    continue
                if payload.get("request_digest") != request_digest:
                    raise sqlite3.IntegrityError(
                        "authority policy release idempotency mismatch"
                    )
                self._conn.commit()
                return persisted

            release = AuthorityPolicyRelease.model_validate(
                release.model_dump(mode="python", round_trip=True, warnings=False)
            )
            values = release.model_dump(mode="json")
            scope = f"config:authority-policy:{release.team}"
            current = self._conn.execute(
                "SELECT * FROM authority_policy_activations WHERE team=? "
                "ORDER BY epoch DESC LIMIT 1",
                (release.team,),
            ).fetchone()
            current_release = None
            if current is not None:
                current_activation = self._authority_policy_activation_from_row(current)
                current_release = self.get_authority_policy_release(
                    current_activation.release_id
                )
                if current_release is None:
                    raise ValueError("active authority policy release is missing")
            expected_base = None if current_release is None else current_release.id
            if release.based_on_release_id != expected_base:
                raise sqlite3.IntegrityError("authority policy release base conflict")
            self._conn.execute(
                """INSERT INTO authority_policy_releases
                   (id,team,policy_id,version,title,normative_text,clauses_json,
                    continuation_phrase,canonical_payload_json,policy_digest,
                    based_on_release_id,actor_kind,created_at)
                   VALUES (:id,:team,:policy_id,:version,:title,:normative_text,
                           :clauses_json,:continuation_phrase,:canonical_payload_json,
                           :policy_digest,:based_on_release_id,:actor_kind,:created_at)""",
                values,
            )
            self.insert_audit_log_uncommitted(
                task_id=scope,
                agent="shared_local_operator_credential",
                action="authority_policy_release_created",
                payload={
                    "action": "create_release",
                    "actor_kind": "shared_local_operator_credential",
                    "policy_digest": release.policy_digest,
                    "release_id": release.id,
                    "request_digest": request_digest,
                    "request_id": request_id,
                    "team": release.team,
                },
            )
            self._conn.commit()
            return release
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def get_authority_policy_release(self, release_id: str) -> AuthorityPolicyRelease | None:
        row = self._conn.execute(
            "SELECT * FROM authority_policy_releases WHERE id=?", (release_id,)
        ).fetchone()
        return None if row is None else self._authority_policy_release_from_row(row)

    @_synchronized
    def get_next_authority_policy_release_version(self, team: str, policy_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(version) AS version FROM authority_policy_releases "
            "WHERE team=? AND policy_id=?",
            (team, policy_id),
        ).fetchone()
        return 1 if row is None or row["version"] is None else int(row["version"]) + 1

    @_synchronized
    def list_authority_policy_history(
        self,
        team: str,
        *,
        snapshot_version: int,
        snapshot_epoch: int,
        after_version: int | None,
        after_epoch: int | None,
        after_release_id: str | None,
        after_activation_id: str | None,
        limit: int,
    ) -> list[dict]:
        """Read immutable release/activation receipts without policy prose."""
        rows = self._conn.execute(
            """SELECT r.id AS release_id,r.policy_id,r.version,r.policy_digest,
                      r.created_at AS release_created_at,r.actor_kind,
                      a.id AS activation_id,a.epoch,a.action,
                      a.created_at AS activation_created_at,a.activation_digest
               FROM authority_policy_releases r
               LEFT JOIN authority_policy_activations a
                 ON a.release_id=r.id AND a.epoch<=?
               WHERE r.team=? AND r.version<=?
                 AND (? IS NULL OR (r.version,COALESCE(a.epoch,0),r.id,
                                      COALESCE(a.id,'')) < (?,?,?,?))
               ORDER BY r.version DESC,COALESCE(a.epoch,0) DESC,r.id DESC,
                        COALESCE(a.id,'') DESC LIMIT ?""",
            (
                snapshot_epoch, team, snapshot_version, after_version,
                after_version, after_epoch, after_release_id, after_activation_id,
                limit,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    @_synchronized
    def get_authority_policy_history_snapshot(self, team: str) -> tuple[int, int]:
        row = self._conn.execute(
            """SELECT COALESCE(MAX(r.version),0) AS version,
                      COALESCE((SELECT MAX(epoch) FROM authority_policy_activations
                                WHERE team=?),0) AS epoch
               FROM authority_policy_releases r WHERE r.team=?""",
            (team, team),
        ).fetchone()
        return int(row["version"]), int(row["epoch"])

    @_synchronized
    def list_authority_policy_outcomes(
        self,
        team: str,
        *,
        snapshot_created_at: str,
        snapshot_id: str,
        after_created_at: str | None,
        after_id: str | None,
        limit: int,
    ) -> list[dict]:
        """Read the joined immutable evaluation identity; projection authenticates completeness."""
        rows = self._conn.execute(
            """SELECT c.*,p.release_id,p.activation_id,p.activation_epoch,
                      p.provider_id,p.executor_kind,e.id AS evaluation_id,
                      e.disposition AS evaluation_disposition,e.disposition_code,
                      e.response_digest,e.created_at AS evaluation_created_at,
                      env.id AS envelope_id,env.state AS envelope_state,
                      env.consumed_at AS envelope_consumed_at
               FROM authority_candidates c
               LEFT JOIN authority_candidate_policy_pins p ON p.candidate_id=c.id
               LEFT JOIN authority_evaluations e ON e.candidate_id=c.id
               LEFT JOIN authority_continue_envelopes env ON env.candidate_id=c.id
               WHERE c.team=? AND (c.created_at,c.id) <= (?,?)
                 AND (? IS NULL OR (c.created_at,c.id) < (?,?))
               ORDER BY c.created_at DESC,c.id DESC LIMIT ?""",
            (team, snapshot_created_at, snapshot_id, after_created_at,
             after_created_at, after_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    @_synchronized
    def get_authority_policy_outcomes_snapshot(self, team: str) -> tuple[str, str] | None:
        row = self._conn.execute(
            "SELECT created_at,id FROM authority_candidates WHERE team=? "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            (team,),
        ).fetchone()
        return None if row is None else (str(row["created_at"]), str(row["id"]))

    @_synchronized
    def activate_authority_policy(
        self, activation: AuthorityPolicyActivation
    ) -> AuthorityPolicyActivation:
        """Append the next team epoch under BEGIN IMMEDIATE CAS semantics."""
        # Rebuild from a deep primitive snapshot without generating a seal.
        # Missing, malformed, or mismatched receipts fail before BEGIN, even
        # when a caller bypassed Pydantic's assignment guard.
        activation = AuthorityPolicyActivation.model_validate(
            activation.model_dump(mode="python", round_trip=True, warnings=False)
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            duplicate = self._conn.execute(
                "SELECT * FROM authority_policy_activations WHERE request_id=?",
                (activation.request_id,),
            ).fetchone()
            if duplicate is not None:
                persisted = self._authority_policy_activation_from_row(duplicate)
                if persisted.model_dump(mode="json") != activation.model_dump(mode="json"):
                    raise sqlite3.IntegrityError("authority activation idempotency mismatch")
                self._conn.commit()
                return persisted
            previous = self._conn.execute(
                "SELECT * FROM authority_policy_activations WHERE team=? ORDER BY epoch DESC LIMIT 1",
                (activation.team,),
            ).fetchone()
            previous_epoch = None if previous is None else previous["epoch"]
            expected_epoch = activation.expected_previous_epoch
            if previous is None:
                if expected_epoch not in (None, 0) or activation.epoch != 1 or activation.previous_activation_id is not None:
                    raise sqlite3.IntegrityError("authority activation bootstrap CAS conflict")
                if activation.action != "bootstrap":
                    raise sqlite3.IntegrityError("first authority activation must be bootstrap")
            elif (
                expected_epoch != previous_epoch
                or activation.epoch != previous_epoch + 1
                or activation.previous_activation_id != previous["id"]
            ):
                raise sqlite3.IntegrityError("authority activation CAS conflict")
            elif activation.action == "bootstrap":
                raise sqlite3.IntegrityError("authority bootstrap is forbidden after history exists")
            else:
                previously_activated = self._conn.execute(
                    "SELECT 1 FROM authority_policy_activations WHERE team=? AND release_id=? LIMIT 1",
                    (activation.team, activation.release_id),
                ).fetchone() is not None
                if activation.action == "activate" and previously_activated:
                    raise sqlite3.IntegrityError("activate target was previously activated")
                if activation.action == "reactivate_rollback" and (
                    not previously_activated or activation.release_id == previous["release_id"]
                ):
                    raise sqlite3.IntegrityError("reactivation target is not a non-current prior release")
                if activation.action == "reactivate_rollback":
                    older = self._conn.execute(
                        """SELECT 1
                           FROM authority_policy_releases target
                           JOIN authority_policy_releases current ON current.id=?
                           WHERE target.id=?
                             AND target.policy_id=current.policy_id
                             AND target.version<current.version""",
                        (previous["release_id"], activation.release_id),
                    ).fetchone()
                    if older is None:
                        raise sqlite3.IntegrityError(
                            "reactivation rollback target is not an older policy version"
                        )
            self._conn.execute(
                """INSERT INTO authority_policy_activations
                   (id,team,epoch,release_id,previous_activation_id,
                    expected_previous_epoch,action,actor_kind,request_id,
                    request_digest,created_at,activation_digest)
                   VALUES (:id,:team,:epoch,:release_id,:previous_activation_id,
                           :expected_previous_epoch,:action,:actor_kind,:request_id,
                           :request_digest,:created_at,:activation_digest)""",
                activation.model_dump(mode="json"),
            )
            self._conn.commit()
            return activation
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def activate_authority_policy_with_audit(
        self,
        *,
        team: str,
        release_id: str,
        expected_previous_epoch: int,
        action: str,
        request_id: str,
        request_digest: str,
    ) -> AuthorityPolicyActivation:
        """Server-construct and atomically append a CAS activation plus audit."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            duplicate = self._conn.execute(
                "SELECT * FROM authority_policy_activations WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if duplicate is not None:
                if duplicate["request_digest"] != request_digest:
                    raise sqlite3.IntegrityError(
                        "authority activation idempotency mismatch"
                    )
                persisted = self._authority_policy_activation_from_row(duplicate)
                self._conn.commit()
                return persisted
            release = self.get_authority_policy_release(release_id)
            if release is None or release.team != team:
                raise LookupError("authority activation release unavailable")
            previous = self._conn.execute(
                "SELECT * FROM authority_policy_activations WHERE team=? "
                "ORDER BY epoch DESC LIMIT 1",
                (team,),
            ).fetchone()
            if previous is None:
                if expected_previous_epoch != 0 or action != "bootstrap":
                    raise sqlite3.IntegrityError("authority activation bootstrap CAS conflict")
                epoch = 1
            elif previous["epoch"] != expected_previous_epoch or action == "bootstrap":
                raise sqlite3.IntegrityError("authority activation CAS conflict")
            else:
                epoch = previous["epoch"] + 1
            created_at = _now()
            activation_id = "APA-" + hashlib.sha256(
                f"{team}:{request_id}:{request_digest}".encode("utf-8")
            ).hexdigest()
            activation = AuthorityPolicyActivation.create(
                id=activation_id,
                team=team,
                epoch=epoch,
                release_id=release_id,
                previous_activation_id=None if previous is None else previous["id"],
                expected_previous_epoch=expected_previous_epoch,
                action=action,
                actor_kind="shared_local_operator_credential",
                request_id=request_id,
                request_digest=request_digest,
                created_at=created_at,
            )
            previously_activated = self._conn.execute(
                "SELECT 1 FROM authority_policy_activations "
                "WHERE team=? AND release_id=? LIMIT 1",
                (team, release_id),
            ).fetchone() is not None
            if action == "activate" and previously_activated:
                raise sqlite3.IntegrityError("activate target was previously activated")
            if action == "reactivate_rollback":
                older = self._conn.execute(
                    """SELECT 1 FROM authority_policy_releases target
                       JOIN authority_policy_releases current ON current.id=?
                       WHERE target.id=? AND target.policy_id=current.policy_id
                         AND target.version<current.version""",
                    (previous["release_id"], release_id),
                ).fetchone()
                if (
                    not previously_activated
                    or release_id == previous["release_id"]
                    or older is None
                ):
                    raise sqlite3.IntegrityError(
                        "reactivation target is not an older prior release"
                    )
            self._conn.execute(
                """INSERT INTO authority_policy_activations
                   (id,team,epoch,release_id,previous_activation_id,
                    expected_previous_epoch,action,actor_kind,request_id,
                    request_digest,created_at,activation_digest)
                   VALUES (:id,:team,:epoch,:release_id,:previous_activation_id,
                           :expected_previous_epoch,:action,:actor_kind,:request_id,
                           :request_digest,:created_at,:activation_digest)""",
                activation.model_dump(mode="json"),
            )
            audit_action = (
                "authority_policy_activated"
                if action in ("activate", "bootstrap")
                else "authority_policy_reactivated"
            )
            payload = {
                "activation_id": activation.id,
                "action": action,
                "actor_kind": "shared_local_operator_credential",
                "epoch": epoch,
                "policy_digest": release.policy_digest,
                "release_id": release.id,
                "request_digest": request_digest,
                "team": team,
            }
            self.insert_audit_log_uncommitted(
                task_id=f"config:authority-policy:{team}",
                agent="shared_local_operator_credential",
                action=audit_action,
                payload=payload,
            )
            self._conn.commit()
            return activation
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def get_authority_policy_activation(self, activation_id: str) -> AuthorityPolicyActivation | None:
        row = self._conn.execute(
            "SELECT * FROM authority_policy_activations WHERE id=?", (activation_id,)
        ).fetchone()
        return None if row is None else self._authority_policy_activation_from_row(row)

    @_synchronized
    def get_current_authority_policy_activation(
        self, team: str
    ) -> AuthorityPolicyActivation | None:
        """Return the latest authenticated activation for ``team``, if any.

        The point-read decoder is deliberately the sole validation boundary:
        it authenticates the linked release and replays the complete sealed
        activation history through the selected receipt.
        """
        row = self._conn.execute(
            "SELECT * FROM authority_policy_activations "
            "WHERE team=? ORDER BY epoch DESC LIMIT 1",
            (team,),
        ).fetchone()
        return None if row is None else self._authority_policy_activation_from_row(row)

    @_synchronized
    def get_authority_candidate_policy_pin(
        self, candidate_id: str
    ) -> AuthorityCandidatePolicyPin | None:
        row = self._conn.execute(
            "SELECT * FROM authority_candidate_policy_pins WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        pin = AuthorityCandidatePolicyPin.model_validate(dict(row))
        # Re-resolve every immutable identity on reads; missing/corrupt linkage
        # fails closed even if a damaged database bypassed normal FK/triggers.
        linked = self._conn.execute(
            """SELECT 1 FROM authority_candidates c
               JOIN authority_policy_releases r ON r.id=?
               JOIN authority_policy_activations a ON a.id=?
               WHERE c.id=? AND c.team=r.team AND c.team=a.team
                 AND a.release_id=r.id AND a.epoch=?
                 AND c.policy_id=r.policy_id
                 AND c.policy_version=CAST(r.version AS TEXT)
                 AND c.policy_digest=r.policy_digest""",
            (pin.release_id, pin.activation_id, pin.candidate_id, pin.activation_epoch),
        ).fetchone()
        if linked is None:
            raise ValueError(f"authority candidate policy pin {candidate_id!r} is corrupt")
        self.get_authority_policy_release(pin.release_id)
        activation = self.get_authority_policy_activation(pin.activation_id)
        if activation is None:
            raise ValueError(f"authority candidate policy pin {candidate_id!r} is corrupt")
        return pin

    @_synchronized
    def claim_authority_candidate_with_policy_pin(
        self,
        *,
        release_id: str,
        activation_id: str,
        activation_epoch: int,
        provider_id: str,
        executor_kind: str,
        **candidate_kwargs,
    ) -> tuple[AuthorityCandidate, AuthorityCandidatePolicyPin]:
        """Dark S1 API: atomically insert exactly one candidate and its pin."""
        if not provider_id or not executor_kind:
            raise ValueError("provider_id and executor_kind must be non-empty")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            candidate_id, won = self.claim_authority_candidate(
                **candidate_kwargs, _commit=False
            )
            if not won:
                raise sqlite3.IntegrityError("candidate policy pin claim already exists")
            now = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                """INSERT INTO authority_candidate_policy_pins
                   (candidate_id,release_id,activation_id,activation_epoch,
                    provider_id,executor_kind,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (candidate_id, release_id, activation_id, activation_epoch,
                 provider_id, executor_kind, now),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        candidate = self.get_authority_candidate(candidate_id)
        pin = self.get_authority_candidate_policy_pin(candidate_id)
        if candidate is None or pin is None:
            raise RuntimeError("atomic authority candidate policy pin write disappeared")
        return candidate, pin

    @_synchronized
    def claim_authority_candidate(
        self,
        *,
        root_task_id: str,
        team: str,
        manager_agent: str,
        manager_session_id: str,
        causal_event_id: str,
        causal_event_digest: str,
        causal_result_id: str | None,
        policy_id: str,
        policy_version: str,
        policy_digest: str,
        prompt_id: str,
        prompt_version: str,
        prompt_digest: str,
        model_id: str,
        model_version: str,
        model_digest: str,
        snapshot_digest: str,
        snapshot_retention_class: str = "digest_only",
        snapshot_redaction_class: str = "redacted",
        fence_results: dict | None = None,
        _commit: bool = True,
    ) -> tuple[str, bool]:
        """Deterministic, barrier-ready CAS claim/create contract.

        Exactly one durable candidate wins the
        root/session/causal-event/policy-prompt-model tuple. The candidate id
        and ``claim_key`` are both derived deterministically from that tuple,
        and ``claim_key`` carries a UNIQUE constraint, so a concurrent second
        claim with the same tuple cannot mint a second candidate.

        Returns ``(candidate_id, won)``. ``won`` is True only for the caller
        whose INSERT actually created the row (the durable winner). A loser
        receives the same deterministic ``candidate_id`` as the winner and
        ``won=False`` — the documented loser result. Callers must never assert
        incidental thread ordering; the UNIQUE constraint, not scheduling, is
        the arbiter. No evaluator is invoked and no consumption occurs here.

        Controlled inputs (``snapshot_retention_class``/
        ``snapshot_redaction_class``) are validated against their closed
        vocabulary and raise ``ValueError`` before any durable write.
        Non-uniqueness constraint failures raise ``sqlite3.IntegrityError``;
        the deterministic id/``claim_key`` uniqueness race maps to the
        ``(candidate_id, won=False)`` loser result ONLY when the conflicting
        row is proven to be the exact deterministic immutable claim tuple — a
        raw-SQL/imported row occupying either key under a different identity
        raises ``sqlite3.IntegrityError`` instead of misreporting an
        unrelated durable row as the winner. Never a phantom loser with no
        row.
        """
        # Pre-serialization validation — reject prose/credentials/model exchanges
        # smuggled into digest fields and non-closed fence results BEFORE any row
        # is written (no silent redaction, no durable residue).
        validate_authority_digest(causal_event_digest, "causal_event_digest")
        validate_authority_digest(policy_digest, "policy_digest")
        validate_authority_digest(prompt_digest, "prompt_digest")
        validate_authority_digest(model_digest, "model_digest")
        validate_authority_digest(snapshot_digest, "snapshot_digest")
        validate_authority_version(policy_version, "policy_version")
        validate_authority_version(prompt_version, "prompt_version")
        validate_authority_version(model_version, "model_version")
        fence_results_json = _serialize_authority_fence_results(fence_results)
        # Controlled-input validation BEFORE any durable write: an invalid
        # snapshot retention/redaction class must fail loudly here, never be
        # turned by conflict handling into a phantom CAS loser.
        _validate_authority_class(
            snapshot_retention_class, "snapshot_retention_class", AuthorityRetentionClass
        )
        _validate_authority_class(
            snapshot_redaction_class, "snapshot_redaction_class", AuthorityRedactionClass
        )

        claim_key = _authority_claim_key(
            root_task_id,
            manager_session_id,
            causal_event_id,
            policy_digest,
            prompt_digest,
            model_digest,
        )
        candidate_id = f"AUTH-CAND-{claim_key}"
        now = datetime.now(timezone.utc).isoformat()
        try:
            cur = self._conn.execute(
                """INSERT INTO authority_candidates (
                   id, claim_key, root_task_id, team, manager_agent,
                   manager_session_id, causal_event_id, causal_event_digest,
                   causal_result_id, policy_id, policy_version, policy_digest,
                   prompt_id, prompt_version, prompt_digest,
                   model_id, model_version, model_digest,
                   snapshot_digest, snapshot_retention_class,
                   snapshot_redaction_class, fence_results_json,
                   disposition, lifecycle_state, consumed_at,
                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, NULL, 'created', NULL, ?, ?)""",
            (
                candidate_id,
                claim_key,
                root_task_id,
                team,
                manager_agent,
                manager_session_id,
                causal_event_id,
                causal_event_digest,
                causal_result_id,
                policy_id,
                policy_version,
                policy_digest,
                prompt_id,
                prompt_version,
                prompt_digest,
                model_id,
                model_version,
                model_digest,
                snapshot_digest,
                snapshot_retention_class,
                snapshot_redaction_class,
                fence_results_json,
                now,
                now,
            ),
        )
        except sqlite3.IntegrityError as exc:
            # Scope conflict-to-CAS-loss handling to the intended uniqueness
            # race ONLY. id (PRIMARY KEY) and claim_key (UNIQUE) are both
            # derived deterministically from the same claim tuple, so a
            # UNIQUE/PRIMARYKEY violation means the exact tuple was already
            # claimed — the documented loser result. Any other constraint
            # failure (CHECK, NOT NULL, FK) is a real defect and must raise;
            # it must never masquerade as won=False with a phantom loser id
            # and no durable row.
            if exc.sqlite_errorname in (
                "SQLITE_CONSTRAINT_UNIQUE",
                "SQLITE_CONSTRAINT_PRIMARYKEY",
            ):
                self._conn.rollback()
                # Prove the conflicting row IS the exact deterministic
                # immutable tuple before returning the loser result. A
                # raw-SQL or imported row can occupy the derived candidate id
                # under a different claim_key, or the claim_key under a
                # different id; neither is our tuple, and reporting either as
                # the winner would misattribute an unrelated durable row.
                # Query by BOTH relevant keys and validate the complete
                # immutable tuple (both derived keys plus the six claim-tuple
                # source fields; claim_key is their sha256, so equality is
                # the tuple proof). The exact tuple row occupies both keys,
                # so it is necessarily the only match when present. On any
                # absence or contradiction, fail closed with an integrity
                # failure instead of returning a loser.
                winner = self._conn.execute(
                    """SELECT id, claim_key, root_task_id, manager_session_id,
                              causal_event_id, policy_digest, prompt_digest,
                              model_digest
                       FROM authority_candidates
                       WHERE id = ? OR claim_key = ?""",
                    (candidate_id, claim_key),
                ).fetchone()
                if (
                    winner is not None
                    and winner["id"] == candidate_id
                    and winner["claim_key"] == claim_key
                    and winner["root_task_id"] == root_task_id
                    and winner["manager_session_id"] == manager_session_id
                    and winner["causal_event_id"] == causal_event_id
                    and winner["policy_digest"] == policy_digest
                    and winner["prompt_digest"] == prompt_digest
                    and winner["model_digest"] == model_digest
                ):
                    return candidate_id, False
                raise sqlite3.IntegrityError(
                    "authority CAS collision: conflicting row does not match "
                    "the deterministic immutable claim tuple"
                ) from exc
            raise
        if _commit:
            self._conn.commit()
        return candidate_id, cur.rowcount == 1

    @_synchronized
    def record_authority_evaluation(
        self,
        *,
        candidate_id: str,
        disposition: str,
        disposition_code: str,
        response_digest: str,
        response_retention_class: str = "digest_only",
        response_redaction_class: str = "redacted",
        fence_results: dict | None = None,
    ) -> int:
        """Atomically persist the single immutable evaluation for a candidate.

        Writes the evaluation row and transitions the candidate
        ``created -> evaluated`` (setting its mirrored disposition) in ONE
        transaction. On any failure the whole transaction rolls back — no
        evaluation row and no candidate transition survive.

        The DB is the single-evaluation guard: ``authority_evaluations.
        candidate_id`` carries a UNIQUE constraint, so a second evaluation
        for the same candidate (or a missing candidate, via the FK) raises
        ``sqlite3.IntegrityError`` and rolls back. Stores only the response
        *digest* and controlled disposition/code — never raw response text.
        """
        validate_authority_digest(response_digest, "response_digest")
        fence_results_json = _serialize_authority_fence_results(fence_results)
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("BEGIN")
        try:
            cur = self._conn.execute(
                """INSERT INTO authority_evaluations (
                       candidate_id, disposition, disposition_code,
                       response_digest, response_retention_class,
                       response_redaction_class, fence_results_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate_id,
                    disposition,
                    disposition_code,
                    response_digest,
                    response_retention_class,
                    response_redaction_class,
                    fence_results_json,
                    now,
                ),
            )
            self._conn.execute(
                """UPDATE authority_candidates
                   SET disposition = ?, lifecycle_state = 'evaluated', updated_at = ?
                   WHERE id = ?""",
                (disposition, now, candidate_id),
            )
            self._conn.commit()
            return cur.lastrowid
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def record_authority_audit(
        self,
        *,
        candidate_id: str,
        event_type: str,
        payload: dict | None = None,
    ) -> int:
        """Append one immutable audit event. The table's BEFORE UPDATE/DELETE
        triggers make it append-only at the DB level, and candidate attribution
        is DB-enforced via a foreign key to ``authority_candidates``."""
        # API validation (in addition to the DB-level FK): closed event
        # vocabulary, closed payload, and an existing candidate.
        event_type_value = AuthorityAuditEventType(event_type).value
        payload_json = _serialize_authority_audit_payload(payload)
        exists = self._conn.execute(
            "SELECT 1 FROM authority_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if exists is None:
            raise ValueError(
                f"authority audit requires an existing candidate: {candidate_id!r}"
            )
        cur = self._conn.execute(
            """INSERT INTO authority_audit (candidate_id, event_type, payload_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (
                candidate_id,
                event_type_value,
                payload_json,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    @_synchronized
    def consume_authority_candidate(self, candidate_id: str) -> bool:
        """Exactly-once consumption CAS.

        Transitions ``evaluated -> consumed`` (setting ``consumed_at``) only
        if the candidate is currently evaluated. Returns True only for the
        first call; any later call — or a call on a candidate that was never
        evaluated (a partial record) — returns False, so no partial record
        becomes a future continuation and no extra consumption occurs.
        """
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            """UPDATE authority_candidates
               SET lifecycle_state = 'consumed', consumed_at = ?, updated_at = ?
               WHERE id = ? AND lifecycle_state = 'evaluated'""",
            (now, now, candidate_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    @_synchronized
    def commit_authority_continue_same_root(
        self,
        *,
        task_id: str,
        candidate_id: str,
        expected_manager_agent: str,
        expected_session: str,
        expected_team: str,
        expected_policy_id: str,
        expected_policy_version: str,
        expected_policy_digest: str,
        expected_prompt_id: str,
        expected_prompt_version: str,
        expected_prompt_digest: str,
        expected_model_id: str,
        expected_model_version: str,
        expected_model_digest: str,
        expected_input_digest: str,
        expected_causal_event_id: str,
        expected_max_orchestration_steps: int,
        expected_max_revise_rounds: int,
        expected_status: TaskStatus,
        expected_block_kind: BlockKind | None,
        note: str,
        audit_agent: str,
        authority_continue_payload: dict,
        hook_outcome_payload: dict,
        envelope_clause_id: str,
        envelope_action: str,
        envelope_causal_event_digest: str,
    ) -> bool:
        """THR-181 Track A: atomic same-root continuation CAS + full audit.

        Executes the authority hook's CONTINUE_SAME_ROOT permitted action in
        ONE transaction. Before the still-claimed root is returned to PENDING,
        the COMPLETE current fence set is atomically re-validated against
        live state — every category the hook used before/during evaluation
        (candidate/policy/input identity, manager ownership and session,
        exact team, root status, cancellation, block/active-work, revisit/
        successor lineage, orchestration and revise budgets, zombie/
        partial-work evidence, adverse child verdicts). Any drift that landed
        while the evaluator ran — cancellation, session/manager/team change,
        block, active work, a successor/revisit signal, an exhausted
        budget, partial-work evidence, an adverse child verdict, or a
        candidate/policy/input mismatch — rolls the whole transaction back
        and returns False (no continuation).

        The dispatched thread id/origin remains provenance and is not a
        rollback fence.

        Only when every recheck passes are BOTH the
        ``authority_continued_same_root`` and ``authority_hook`` audit rows
        appended atomically with the continuation, so an audit failure can
        never permit continuation. The single-use continuation ENVELOPE
        (``authority_continue_envelopes``, state ``active``, bound 1:1 to the
        candidate and to the immutable causal task-result row, and carrying
        the matched policy clause + exact permitted action) is minted in the
        SAME transaction — the continuation window that the daemon restricts
        on the next turn exists only when this commit succeeds. Returns False
        (nothing written) when any gate fails.
        """
        now = datetime.now(timezone.utc).isoformat()
        block_sql = (
            "block_kind IS NULL"
            if expected_block_kind is None
            else "block_kind = ?"
        )
        block_args = () if expected_block_kind is None else (expected_block_kind.value,)
        self._conn.execute("BEGIN")
        try:
            # -- 1. Candidate identity recheck (immutable claim tuple) --
            cand = self._conn.execute(
                """SELECT id, root_task_id, manager_session_id, causal_event_id,
                          policy_id, policy_version, policy_digest,
                          prompt_id, prompt_version, prompt_digest,
                          model_id, model_version, model_digest,
                          snapshot_digest, lifecycle_state
                   FROM authority_candidates WHERE id = ?""",
                (candidate_id,),
            ).fetchone()
            if cand is None:
                self._conn.rollback()
                return False
            if not (
                cand["root_task_id"] == task_id
                and cand["manager_session_id"] == expected_session
                and cand["causal_event_id"] == expected_causal_event_id
                and cand["policy_id"] == expected_policy_id
                and cand["policy_version"] == expected_policy_version
                and cand["policy_digest"] == expected_policy_digest
                and cand["prompt_id"] == expected_prompt_id
                and cand["prompt_version"] == expected_prompt_version
                and cand["prompt_digest"] == expected_prompt_digest
                and cand["model_id"] == expected_model_id
                and cand["model_version"] == expected_model_version
                and cand["model_digest"] == expected_model_digest
                and cand["snapshot_digest"] == expected_input_digest
                and cand["lifecycle_state"] == "consumed"
            ):
                self._conn.rollback()
                return False

            # -- 2. Complete task fence recheck against live state --
            t = self._conn.execute(
                """SELECT status, block_kind, cancelled_at, assigned_agent,
                          current_session_id, team, revisit_of_task_id,
                          dispatched_from_thread_id, active_chain, active_fanout,
                          blocked_on_job_ids, orchestration_step_count,
                          revision_count, zombie_flagged_at
                   FROM tasks WHERE id = ?""",
                (task_id,),
            ).fetchone()
            if t is None:
                self._conn.rollback()
                return False
            terminal = t["status"] in _AUTHORITY_TERMINAL_STATUSES
            budget_ok = (
                t["orchestration_step_count"] < expected_max_orchestration_steps
                and (
                    expected_max_revise_rounds <= 0
                    or t["revision_count"] < expected_max_revise_rounds
                )
            )
            if not (
                t["assigned_agent"] == expected_manager_agent
                and t["current_session_id"] == expected_session
                and t["team"] == expected_team
                and t["status"] == expected_status.value
                and (t["block_kind"] is None if expected_block_kind is None else t["block_kind"] == expected_block_kind.value)
                and t["cancelled_at"] is None
                and not terminal
                and t["active_chain"] is None
                and t["active_fanout"] is None
                and t["blocked_on_job_ids"] is None
                and t["revisit_of_task_id"] is None
                and budget_ok
                and t["zombie_flagged_at"] is None
            ):
                self._conn.rollback()
                return False

            # -- 3. Successor lineage recheck --
            succ = self._conn.execute(
                "SELECT 1 FROM manager_supersessions WHERE successor_task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
            if succ is not None:
                self._conn.rollback()
                return False

            # -- 4. Adverse child-verdict recheck (latest persisted verdict) --
            children = self._conn.execute(
                "SELECT id FROM tasks WHERE parent_task_id = ?", (task_id,),
            ).fetchall()
            for child in children:
                latest = self._conn.execute(
                    "SELECT verdict FROM task_results WHERE task_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (child["id"],),
                ).fetchone()
                verdict = latest["verdict"] if latest is not None else None
                if verdict is not None and verdict not in _AUTHORITY_APPROVED_VERDICTS:
                    self._conn.rollback()
                    return False

            # -- 5. Atomic continuation + audit rows --
            cur = self._conn.execute(
                f"""UPDATE tasks
                   SET status = ?, block_kind = NULL, note = ?, updated_at = ?
                   WHERE id = ? AND status = ? AND {block_sql}
                     AND cancelled_at IS NULL""",
                (
                    TaskStatus.PENDING.value,
                    note,
                    now,
                    task_id,
                    expected_status.value,
                )
                + block_args,
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                return False
            self._conn.execute(
                "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    audit_agent,
                    "authority_continued_same_root",
                    json.dumps(authority_continue_payload),
                    now,
                ),
            )
            self._conn.execute(
                "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    audit_agent,
                    "authority_hook",
                    json.dumps(hook_outcome_payload),
                    now,
                ),
            )
            # -- 6. Mint the single-use continuation envelope (atomic with the
            # continuation + audit rows). The envelope id is deterministic
            # from the candidate (1:1); its identity binds the immutable
            # causal task-result row, the evaluation (candidate), the matched
            # policy clause, and the exact permitted action. Any consumption
            # must recheck this identity (see
            # ``consume_authority_continue_envelope``).
            self._conn.execute(
                "INSERT INTO authority_continue_envelopes "
                "(id, candidate_id, root_task_id, team, manager_agent, "
                " manager_session_id, causal_event_id, causal_event_digest, "
                " policy_id, policy_version, policy_digest, clause_id, action, "
                " state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    f"CONT-{candidate_id}",
                    candidate_id,
                    task_id,
                    expected_team,
                    expected_manager_agent,
                    expected_session,
                    expected_causal_event_id,
                    envelope_causal_event_digest,
                    expected_policy_id,
                    expected_policy_version,
                    expected_policy_digest,
                    envelope_clause_id,
                    envelope_action,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    # ── THR-181 Track A (founder lifecycle envelope): single-use continuation envelope ──

    def get_active_authority_continue_envelope(self, root_task_id: str):
        """Return the single ACTIVE continuation envelope for ``root_task_id``
        (the continuation window that restricts the continued turn), or None.

        At most one envelope is active for a root at any time: an envelope is
        minted atomically with the continuation and spent exactly once by the
        continued turn's decision (or a failure/cancellation abort). A spent
        envelope never re-activates.
        """
        return self._conn.execute(
            "SELECT * FROM authority_continue_envelopes "
            "WHERE root_task_id = ? AND state = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (root_task_id,),
        ).fetchone()

    def get_authority_continue_envelope(self, envelope_id: str):
        """Return the envelope row by id (any state), or None."""
        return self._conn.execute(
            "SELECT * FROM authority_continue_envelopes WHERE id = ?",
            (envelope_id,),
        ).fetchone()

    @_synchronized
    def consume_authority_continue_envelope(
        self,
        *,
        envelope_id: str,
        root_task_id: str,
        decision_family: str,
        expected_manager_agent: str,
        expected_session_id: str,
        expected_causal_event_id: str,
        expected_causal_event_digest: str,
        expected_policy_id: str,
        expected_policy_version: str,
        expected_policy_digest: str,
        expected_clause_id: str,
        expected_action: str,
        audit_agent: str,
        error: str | None = None,
        violation: bool = False,
    ) -> str:
        """THR-181 Track A: consume the single-use continuation envelope
        EXACTLY ONCE, atomically rechecking the immutable identity.

        Returns ``"consumed"`` (state ``active -> consumed``; the continued
        turn produced a daemon-accepted manager decision), or
        ``"not_active"`` (the envelope was already spent or its identity
        drifted — never a second continuation).

        Inside ONE transaction the envelope's immutable identity (root,
        manager/team/session, causal task-result row, policy id/version/
        digest, matched clause, permitted action) is rechecked against the
        live row, and the root is rechecked to be still claimed
        (in_progress, not cancelled/terminal). The audit row for the
        consumption is written atomically with the transition, so a
        consumption can never be un-audited. Only bounded, non-secret-bearing
        fields are persisted (decision family + error code, never raw prose).
        """
        now = _now().isoformat()
        self._conn.execute("BEGIN")
        try:
            env = self._conn.execute(
                "SELECT * FROM authority_continue_envelopes WHERE id = ?",
                (envelope_id,),
            ).fetchone()
            if env is None or env["state"] != "active":
                self._conn.rollback()
                return "not_active"
            if not (
                env["root_task_id"] == root_task_id
                and env["manager_agent"] == expected_manager_agent
                and env["manager_session_id"] == expected_session_id
                and env["causal_event_id"] == expected_causal_event_id
                and env["causal_event_digest"] == expected_causal_event_digest
                and env["policy_id"] == expected_policy_id
                and env["policy_version"] == expected_policy_version
                and env["policy_digest"] == expected_policy_digest
                and env["clause_id"] == expected_clause_id
                and env["action"] == expected_action
            ):
                # Immutable identity drift: cannot be the continuation we
                # issued — fail closed (the envelope is never re-usable).
                self._conn.rollback()
                return "not_active"
            t = self._conn.execute(
                "SELECT status, cancelled_at "
                "FROM tasks WHERE id = ?",
                (root_task_id,),
            ).fetchone()
            if t is None:
                self._conn.rollback()
                return "not_active"
            if (
                t["status"] != "in_progress"
                or t["cancelled_at"] is not None
            ):
                # The root is no longer the claimed current turn:
                # cancellation/terminal/stale — spend fail-closed.
                self._conn.rollback()
                return "not_active"
            # The envelope is a single-use lifecycle/causality receipt, not
            # an action whitelist. Normal manager-decision validation and the
            # daemon's independent mechanical fences remain authoritative.
            new_state = "violated" if violation else "consumed"
            cur = self._conn.execute(
                "UPDATE authority_continue_envelopes "
                "SET state = ?, consumed_at = ?, updated_at = ? "
                "WHERE id = ? AND state = 'active'",
                (new_state, now, now, envelope_id),
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                return "not_active"
            action_label = (
                "authority_continue_envelope_consumed"
                if new_state == "consumed"
                else "authority_continue_envelope_violated"
            )
            payload: dict = {
                "envelope_id": envelope_id,
                "candidate_id": env["candidate_id"],
                "root_task_id": root_task_id,
                "decision_family": decision_family[:200],
                "clause_id": env["clause_id"],
                "action": env["action"],
                "policy_id": env["policy_id"],
                "policy_version": env["policy_version"],
                "policy_digest": env["policy_digest"],
                "causal_event_id": env["causal_event_id"],
                "causal_event_digest": env["causal_event_digest"],
                "state": new_state,
            }
            if error is not None:
                payload["error"] = error[:500]
            self._conn.execute(
                "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (root_task_id, audit_agent, action_label, json.dumps(payload), now),
            )
            self._conn.commit()
            return new_state
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def spend_authority_continue_envelope_if_active(
        self, root_task_id: str, *, audit_agent: str, error: str,
    ) -> bool:
        """Spend any ACTIVE envelope for ``root_task_id`` as ``violated``
        (fail-closed abort — the continuation window closed without the
        permitted decision: session failure, cancellation, terminal). Returns
        True when an envelope was spent. Exactly-once: only the transition
        from ``active`` wins.
        """
        env = self.get_active_authority_continue_envelope(root_task_id)
        if env is None:
            return False
        now = _now().isoformat()
        self._conn.execute("BEGIN")
        try:
            cur = self._conn.execute(
                "UPDATE authority_continue_envelopes "
                "SET state = 'violated', consumed_at = ?, updated_at = ? "
                "WHERE id = ? AND state = 'active'",
                (now, now, env["id"]),
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                return False
            payload: dict = {
                "envelope_id": env["id"],
                "candidate_id": env["candidate_id"],
                "root_task_id": root_task_id,
                "decision_family": "aborted",
                "clause_id": env["clause_id"],
                "action": env["action"],
                "policy_id": env["policy_id"],
                "policy_version": env["policy_version"],
                "policy_digest": env["policy_digest"],
                "causal_event_id": env["causal_event_id"],
                "causal_event_digest": env["causal_event_digest"],
                "state": "violated",
                "error": error[:500],
            }
            self._conn.execute(
                "INSERT INTO audit_log (task_id, agent, action, payload, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    root_task_id,
                    audit_agent,
                    "authority_continue_envelope_violated",
                    json.dumps(payload),
                    now,
                ),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def _authority_candidate_from_row(self, row) -> AuthorityCandidate:
        return AuthorityCandidate(
            id=row["id"],
            claim_key=row["claim_key"],
            root_task_id=row["root_task_id"],
            team=row["team"],
            manager_agent=row["manager_agent"],
            manager_session_id=row["manager_session_id"],
            causal_event_id=row["causal_event_id"],
            causal_event_digest=row["causal_event_digest"],
            causal_result_id=row["causal_result_id"],
            policy_id=row["policy_id"],
            policy_version=row["policy_version"],
            policy_digest=row["policy_digest"],
            prompt_id=row["prompt_id"],
            prompt_version=row["prompt_version"],
            prompt_digest=row["prompt_digest"],
            model_id=row["model_id"],
            model_version=row["model_version"],
            model_digest=row["model_digest"],
            snapshot_digest=row["snapshot_digest"],
            snapshot_retention_class=row["snapshot_retention_class"],
            snapshot_redaction_class=row["snapshot_redaction_class"],
            fence_results=_parse_authority_fence_results(row["fence_results_json"]),
            disposition=row["disposition"],
            lifecycle_state=row["lifecycle_state"],
            consumed_at=row["consumed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @_synchronized
    def get_authority_candidate(self, candidate_id: str) -> AuthorityCandidate | None:
        row = self._conn.execute(
            "SELECT * FROM authority_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            return None
        return self._authority_candidate_from_row(row)

    @_synchronized
    def get_authority_candidate_by_claim(self, claim_key: str) -> AuthorityCandidate | None:
        row = self._conn.execute(
            "SELECT * FROM authority_candidates WHERE claim_key = ?", (claim_key,)
        ).fetchone()
        if row is None:
            return None
        return self._authority_candidate_from_row(row)

    @_synchronized
    def list_authority_candidates_for_root(self, root_task_id: str) -> list[AuthorityCandidate]:
        rows = self._conn.execute(
            "SELECT * FROM authority_candidates WHERE root_task_id = ? ORDER BY id",
            (root_task_id,),
        ).fetchall()
        return [self._authority_candidate_from_row(r) for r in rows]

    @_synchronized
    def get_authority_evaluation(self, candidate_id: str) -> AuthorityEvaluation | None:
        row = self._conn.execute(
            "SELECT * FROM authority_evaluations WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            return None
        return AuthorityEvaluation(
            id=row["id"],
            candidate_id=row["candidate_id"],
            disposition=row["disposition"],
            disposition_code=row["disposition_code"],
            response_digest=row["response_digest"],
            response_retention_class=row["response_retention_class"],
            response_redaction_class=row["response_redaction_class"],
            fence_results=_parse_authority_fence_results(row["fence_results_json"]),
            created_at=row["created_at"],
        )

    @_synchronized
    def list_authority_audit(self, candidate_id: str) -> list[AuthorityAuditEvent]:
        rows = self._conn.execute(
            "SELECT * FROM authority_audit WHERE candidate_id = ? ORDER BY id",
            (candidate_id,),
        ).fetchall()
        return [
            AuthorityAuditEvent(
                id=r["id"],
                candidate_id=r["candidate_id"],
                event_type=r["event_type"],
                payload=(
                    AuthorityAuditPayload.model_validate(json.loads(r["payload_json"]))
                    if r["payload_json"]
                    else None
                ),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    @_synchronized
    def close(self) -> None:
        self._conn.close()
