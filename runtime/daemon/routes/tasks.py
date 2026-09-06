"""Task submission and inspection endpoints."""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import signal
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, ValidationError, field_validator
from sse_starlette.sse import EventSourceResponse

from runtime.daemon.auth import require_token
from runtime.daemon.org_state import OrgState
from runtime.daemon.routes._org_dep import OrgDep
from runtime.daemon.runner import enqueue_task
from runtime.daemon.state import DaemonState
from runtime.daemon.work_status import derive_work_status
from runtime.infrastructure.task_attachment_store import (
    MAX_TASK_ATTACHMENTS_PER_TASK,
    MAX_TASK_ATTACHMENT_BYTES,
    TaskAttachmentInvalidName,
    TaskAttachmentNotFound,
    TaskAttachmentStore,
    TaskAttachmentTooLarge,
    TaskAttachmentTooMany,
    TaskAttachmentUnsupportedType,
    resolve_content_type,
    sanitize_display_name,
)
from runtime.models import BlockKind, TaskAttachmentRecord, TaskAttachmentRef, TaskRecord, TaskStatus

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[require_token()])

# Terminal statuses for task-active gating. Referenced by both the cancel
# route (l.~700) and the agent-callback routes (submit_completion, submit_progress)
# so it lives at module scope rather than next to its first use.
_TERMINAL_TASK_STATUSES = frozenset({
    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SUPERSEDED,
    TaskStatus.CANCELLED,  # Path B: founder-initiated terminal stop.
})


def _require_task_active(task_id: str, task: TaskRecord | None) -> None:
    """Guard A: reject agent callbacks whose task is gone, terminal, or cancelled.

    Mirrors src/daemon/routes/scripts.py:64-90 validation order — existence
    before active, both before session ownership. A cancelled task reporting
    `session_mismatch` would mislead the agent into thinking its session was
    bumped when its whole task was terminated.

    Closes the cancel-race documented in
    docs/superpowers/specs/2026-05-26-cancel-race-design.md §5.1.
    """
    if task is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_task", "task_id": task_id},
        )
    if task.cancelled_at is not None or task.status in _TERMINAL_TASK_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "task_not_active",
                "task_id": task_id,
                "status": task.status.value,
                "cancelled": task.cancelled_at is not None,
            },
        )


def _task_to_dict(t: TaskRecord) -> dict:
    # Wire convention: every other task-shaped response (submit POST, recall)
    # exposes the primary key as `task_id`; thread routes do the same via
    # their own helpers. Rename here so list + detail responses match.
    d = t.model_dump(exclude={"executor_pid"})
    d["task_id"] = d.pop("id")
    return d


# Outputs are fully inlined into the recall response when an agent asks for
# them, so cap the total to keep one recall under a comfortable prompt budget.
MAX_OUTPUT_BYTES = 200 * 1024


class SubmitTask(BaseModel):
    team: str | None = None
    brief: str
    owner: str | None = None  # assign a specific agent (default: team manager)
    attachments: list[TaskAttachmentRef] | None = None


@router.post("/tasks")
async def submit_task(body: SubmitTask, org: OrgDep, request: Request) -> dict:
    state: DaemonState = request.app.state.daemon
    registry = org.teams
    if registry is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "unknown_team", "valid": []},
        )

    def _require_known_team(t: str) -> None:
        if t not in registry.teams():
            raise HTTPException(
                status_code=400,
                detail={"code": "unknown_team", "valid": registry.teams()},
            )

    if body.owner is not None:
        if body.owner not in registry.all_agents():
            raise HTTPException(
                status_code=400,
                detail={"code": "unknown_owner", "owner": body.owner,
                        "valid": registry.all_agents()},
            )
        # The owner's own team is authoritative for routing/audit. Derive it
        # when no team is requested; otherwise the requested team must match,
        # so the task.team that children inherit can't diverge from the owner.
        owner_team = (registry.team_for_agent(body.owner)
                      or registry.team_for_manager(body.owner))
        if body.team is None:
            team = owner_team
        else:
            team = body.team
            _require_known_team(team)
            if owner_team != team:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "owner_team_mismatch", "owner": body.owner,
                            "owner_team": owner_team, "requested_team": team},
                )
        assigned = body.owner
    else:
        team = body.team or "engineering"
        _require_known_team(team)
        assigned = registry.manager_for_team(team).name
    # Validate attachment refs if provided.
    # Every attachment reference is fully resolved/validated/normalized
    # BEFORE task persistence so no invalid reference creates an orphan task.
    uploaded_by = request.headers.get("X-HappyRanch-Caller", "founder")
    prevalidated_attachments: list[dict] = []
    if body.attachments:
        if len(body.attachments) > MAX_TASK_ATTACHMENTS_PER_TASK:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "too_many_attachments",
                    "max": MAX_TASK_ATTACHMENTS_PER_TASK,
                },
            )
        seen_keys: set[str] = set()
        store = _task_attachment_store(org)
        for ref in body.attachments:
            if ref.storage_key in seen_keys:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "duplicate_attachment",
                        "storage_key": ref.storage_key,
                    },
                )
            seen_keys.add(ref.storage_key)
            # Verify the storage_key exists in the file store.
            path = store.path_for(ref.storage_key)
            if not path.exists() or path.is_dir():
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "task_attachment_not_found",
                        "storage_key": ref.storage_key,
                    },
                )
            # Reject if already claimed by another task.
            existing = org.db.get_task_attachment_by_storage_key(ref.storage_key)
            if existing is not None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "attachment_already_claimed",
                        "storage_key": ref.storage_key,
                        "task_id": existing.task_id,
                    },
                )
            # Validate display name before durable task creation.
            display_name = ref.display_name or "attachment"
            try:
                sanitize_display_name(display_name)
            except TaskAttachmentInvalidName:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "invalid_attachment_display_name"},
                )
            # Resolve content type BEFORE durable task creation — an
            # unsupported extension must not create an orphan task.
            try:
                content_type = resolve_content_type(display_name, None)
            except TaskAttachmentUnsupportedType:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "unsupported_attachment_type"},
                )
            # Collect pre-validated attachment for atomic insertion.
            prevalidated_attachments.append({
                "ref": ref,
                "display_name": display_name,
                "content_type": content_type,
            })

    async with org.db_lock:
        task_id = org.db.next_task_id()

        # Re-check claimability INSIDE the lock — the pre-validation outside
        # is a fast-path hint; the authoritative claim gate is the
        # UNIQUE(storage_key) constraint enforced during the single
        # transaction below.
        if prevalidated_attachments:
            store = _task_attachment_store(org)
            attachment_params: list[dict] = []
            for idx, pva in enumerate(prevalidated_attachments):
                ref = pva["ref"]
                path = store.path_for(ref.storage_key)
                if not path.exists():
                    # File disappeared between upload and link — reject.
                    raise HTTPException(
                        status_code=404,
                        detail={
                            "code": "task_attachment_not_found",
                            "storage_key": ref.storage_key,
                        },
                    )
                size_bytes = path.stat().st_size
                attachment_params.append({
                    "ordinal": idx,
                    "storage_key": ref.storage_key,
                    "display_name": pva["display_name"],
                    "size_bytes": size_bytes,
                    "content_type": pva["content_type"],
                })

            try:
                org.db.insert_task_with_attachments(
                    TaskRecord(
                        id=task_id,
                        brief=body.brief,
                        team=team,
                        assigned_agent=assigned,
                    ),
                    attachments=attachment_params,
                    uploaded_by=uploaded_by,
                )
            except sqlite3.IntegrityError:
                # UNIQUE(storage_key) violation — another concurrent request
                # claimed this key within the serialized boundary.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "attachment_already_claimed",
                        "message": (
                            "One or more attachments were claimed by another "
                            "task after validation. Retry with a fresh upload."
                        ),
                    },
                )
        else:
            org.db.insert_task(
                TaskRecord(
                    id=task_id,
                    brief=body.brief,
                    team=team,
                    assigned_agent=assigned,
                )
            )

    enqueue_task(state, org.slug, task_id)
    return {"task_id": task_id, "team": team, "assigned_agent": assigned}


@router.get("/tasks")
def list_tasks(
    org: OrgDep,
    limit: int = 20,
    assigned_agent: str | None = None,
    before: str | None = None,
    status: str | None = None,
    block_kind: str | None = None,
    blocked_on_job_id: str | None = None,
) -> dict:
    # Cursor pagination: `before` is the task_id of the last item on the
    # previous page. `next_cursor` is the last id of this page when the page
    # is full (heuristic — caller stops when next_cursor is null OR the next
    # page comes back empty). When `before` references a missing task, the
    # database returns [] and we surface that as the end of the list.
    # `status` / `block_kind` are read-only equality filters for backlog
    # queries (e.g. `tasks --status in_progress --block-kind delegated`).
    # `blocked_on_job_id` is a DERIVE filter for the Jobs "if-approved"
    # cascade — finds tasks blocked on a specific job id.
    tasks = org.db.list_tasks(
        limit=limit, assigned_agent=assigned_agent, before_task_id=before,
        status=status, block_kind=block_kind,
        blocked_on_job_id=blocked_on_job_id,
    )
    next_cursor = tasks[-1].id if len(tasks) == limit else None
    return {
        "tasks": [_task_to_dict(t) for t in tasks],
        "next_cursor": next_cursor,
    }


@router.get("/tasks/roots")
def list_roots(
    org: OrgDep,
    limit: int = 20,
    assigned_agent: str | None = None,
    before: str | None = None,
    status: str | None = None,
    block_kind: str | None = None,
) -> dict:
    """Return root tasks only (parent_task_id IS NULL) with a per-root
    severity rollup reflecting the worst status of each root's subtree.

    The rollup is a DERIVE over existing child statuses — no schema change.
    Each task dict includes a ``severity_rollup`` field (the worst status
    among the root and its entire parent_task_id subtree).
    """
    tasks = org.db.list_roots(
        limit=limit, assigned_agent=assigned_agent, before_task_id=before,
        status=status, block_kind=block_kind,
    )
    next_cursor = tasks[-1].id if len(tasks) == limit else None
    # Batch-fetch direct revisits for all returned roots (avoid N+1).
    root_ids = [t.id for t in tasks]
    revisits_map = org.db.batch_get_direct_revisits(root_ids)
    result_tasks: list[dict] = []
    for t in tasks:
        d = _task_to_dict(t)
        d["severity_rollup"] = getattr(t, '_severity_rollup', t.status.value)
        d["direct_revisits"] = revisits_map.get(t.id, [])
        result_tasks.append(d)
    return {
        "tasks": result_tasks,
        "next_cursor": next_cursor,
    }


@router.get("/tasks/{task_id}")
def get_task(task_id: str, org: OrgDep) -> dict:
    task = org.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")

    # Revisit context: chain (this task back to original), direct revisits
    # (tasks that revisit THIS task), and the predecessor's normalized
    # prior_status (pulled from the revisit_of audit entry).
    # truncate=True: revisit history grows naturally over a task's lifetime,
    # so the read path must not 500 once the chain exceeds the defensive bound.
    chain = [t.id for t in org.db.walk_revisit_chain(task_id, truncate=True)]
    direct_revisits = org.db.get_direct_revisits(task_id)
    audit_log = org.db.get_audit_logs(task_id)
    prior_status = None
    if task.revisit_of_task_id is not None:
        for entry in audit_log:
            if entry["action"] == "revisit_of":
                payload = entry.get("payload") or {}
                prior_status = payload.get("prior_status")
                break

    # When a task is blocked waiting for jobs, include the id+status of each
    # blocking job so `happyranch details` can show the founder what to act on.
    blocked_on_jobs: list[dict] | None = None
    if task.block_kind == BlockKind.BLOCKED_ON_JOB and task.status == TaskStatus.IN_PROGRESS:
        job_ids = _json.loads(task.blocked_on_job_ids or "[]")
        blocked_on_jobs = [
            {"job_id": jid, "status": org.db.get_job_status(jid) or "unknown"}
            for jid in job_ids
        ]

    active_chain = None
    if task.active_chain is not None:
        try:
            active_chain = _json.loads(task.active_chain)
        except _json.JSONDecodeError:
            active_chain = None  # defensive — never 500 on malformed on-disk state

    # DERIVE: a superseded predecessor is cited by the
    # escalation_superseded audit row whose structured successor_root
    # payload names the continuation task that superseded it.
    superseded_by_task_id: str | None = None
    for entry in reversed(audit_log):
        if entry["action"] == "escalation_superseded":
            payload = entry.get("payload") or {}
            succ = payload.get("successor_root")
            if isinstance(succ, str) and succ:
                superseded_by_task_id = succ
            break

    return {
        "task": _task_to_dict(task),
        "results": org.db.get_task_results(task_id),
        "audit_log": audit_log,
        "revisit_chain": chain,
        "direct_revisits": direct_revisits,
        "predecessor_prior_status": prior_status,
        "blocked_on_jobs": blocked_on_jobs,
        "active_chain": active_chain,
        "superseded_by_task_id": superseded_by_task_id,
        # TASK-5522: read-only derived work-status summary. Built from the
        # task record (last_heartbeat) plus the existing audit rows
        # (session_start / progress) — no schema, no synthetic audits, no
        # background monitor. See runtime/daemon/work_status.py.
        "work_status": derive_work_status(task, audit_log),
    }


def _read_output(
    workspaces_dir: Path, assigned_agent: str | None, output_dir: str | None,
) -> dict | None:
    """Return {files, truncated} for the output folder, or None if unresolvable.

    Files are read as text; anything that fails decoding (binaries) is skipped.
    If the total inlined payload would exceed MAX_OUTPUT_BYTES we flip to a
    path-only listing with truncated=True so the agent still sees the inventory.
    """
    if not assigned_agent or not output_dir:
        return None
    # output_dir is agent-supplied via the completion callback. Absolute paths
    # and `..` segments would let a buggy/malicious agent disclose arbitrary
    # readable files on the host, so confine the result to the assigned agent's
    # workspace by resolving both paths and checking containment.
    agent_root = (workspaces_dir / assigned_agent).resolve()
    base = (agent_root / output_dir).resolve()
    if not base.is_relative_to(agent_root):
        return None
    if not base.exists():
        return {"files": [], "truncated": False}
    all_files = sorted(f for f in base.rglob("*") if f.is_file())
    files: list[dict] = []
    total = 0
    for f in all_files:
        try:
            text = f.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        total += len(text.encode("utf-8"))
        if total > MAX_OUTPUT_BYTES:
            return {
                "files": [{"path": str(f.relative_to(base))} for f in all_files],
                "truncated": True,
            }
        files.append({"path": str(f.relative_to(base)), "content": text})
    return {"files": files, "truncated": False}


def _recall_node(
    org: OrgState, task_id: str, tree: bool, include_output: bool,
) -> dict | None:
    payload = org.db.get_recall_payload(task_id)
    if payload is None:
        return None
    if include_output:
        payload["output"] = _read_output(
            org.root / "workspaces",
            payload.get("assigned_agent"),
            payload.get("output_dir"),
        )
    if tree:
        child_ids = payload["children"]
        payload["children"] = [
            _recall_node(org, cid, tree=True, include_output=include_output)
            for cid in child_ids
        ]
    return payload


@router.get("/tasks/{task_id}/recall")
def recall_task(
    task_id: str,
    org: OrgDep,
    tree: bool = False,
    include_output: bool = False,
) -> dict:
    node = _recall_node(org, task_id, tree=tree, include_output=include_output)
    if node is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return node


class CompletionBody(BaseModel):
    session_id: str
    agent: str
    status: str
    confidence: int
    output_summary: str
    # Manager-only. Structured next-step decision; workers omit or pass null.
    # Must be a dict matching the NextStep schema if present — validated
    # on the orchestrator side when the parser runs.
    decision: dict | None = None
    # Manager-only S6a semantic evidence. Kept raw at the HTTP boundary so a
    # malformed attempt can be reduced to a digest-only durable diagnostic;
    # raw invalid content is never persisted.
    manager_self_evaluation: object | None = None
    # Worker-reported outcome label for inline delegation chains. Free string;
    # per-team vocabulary (APPROVE, PASS, REQUEST_CHANGES, etc.) defined in
    # each team's workflow KB entry. Omit when the task is not part of a chain
    # or when the worker has no verdict to report.
    verdict: str | None = None
    risks_flagged: list[str] = []
    dependencies: list[str] = []
    suggested_reviewer_focus: list[str] = []
    output_dir: str | None = None
    waiting_on_job_ids: list[str] = []
    # Push-PR local CI evidence. Optional; only contractually required for
    # pushed-PR reports. Must be a dict with exactly {"command": "scripts/local_ci.sh all",
    # "exit_code": 0} if present — validated server-side before durable persistence.
    local_ci: object | None = None


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str, org: OrgDep):
    # Reject unknown task IDs up front — otherwise EventBus.subscribe() replays
    # no history for a fabricated id and then blocks forever, which makes
    # `happyranch tail <bad-id>` hang instead of surfacing a 404.
    if org.db.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")

    async def gen():
        async for event in org.event_bus.subscribe(task_id):
            yield {"data": _json.dumps(event)}

    return EventSourceResponse(gen())


@router.post("/tasks/{task_id}/completion")
async def submit_completion(task_id: str, body: CompletionBody, org: OrgDep) -> dict:
    # Task-active gate runs BEFORE session ownership (see _require_task_active).
    _require_task_active(task_id, org.db.get_task(task_id))
    expected = org.sessions.get_active(task_id, body.agent)
    # Reject callbacks the daemon never spawned. Both branches are 409 — the
    # tracker is the source of truth for "is this a real session". Unknown
    # comes first so an empty tracker can't silently accept a fabricated id.
    if expected is None:
        # Idempotency short-circuit (TASK-3127): clearing the tracker after a
        # successful POST makes a duplicate of an exact persisted session reach
        # this read-only idempotency branch, while an unpersisted (genuinely
        # unknown / fabricated) session remains unknown_session.
        prior = org.db.get_latest_task_result(task_id, body.agent, body.session_id)
        if prior is not None:
            return {"ok": True}
        # No persisted row for this session -> genuinely-unknown / fabricated
        # session. Preserve the security gate: STILL 409.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "unknown_session", "task_id": task_id, "agent": body.agent},
        )
    if expected != body.session_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "session_mismatch", "active": expected, "got": body.session_id},
        )
    # Spec §6.2: validate waiting_on_job_ids if EXPLICITLY present. We check
    # model_fields_set rather than truthiness so we can distinguish "client
    # omitted the field" (legacy escalate path, no validation) from "client
    # explicitly sent []" (malformed payload — reject with 400). The truthy
    # guard collapses both cases, silently bypassing the contract.
    if "waiting_on_job_ids" in body.model_fields_set:
        if not body.waiting_on_job_ids:
            raise HTTPException(
                status_code=400,
                detail={"code": "empty_waiting_on_job_ids"},
            )
        if body.status != "blocked":
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "waiting_on_job_ids_requires_blocked",
                    "got_status": body.status,
                },
            )
        deduped = sorted(set(body.waiting_on_job_ids))
        for jid in deduped:
            owner = org.db.get_job_owner_task_id(jid)
            if owner is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "job_not_found", "job_id": jid},
                )
            if owner != task_id:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "job_not_owned_by_task",
                        "job_id": jid,
                        "owner_task_id": owner,
                    },
                )
        # Persist the deduped list so run_step_impl sees the cleaned-up payload.
        body.waiting_on_job_ids = deduped
    # Validate and normalise local_ci if explicitly present. Strict wire-type
    # check: command must be "scripts/local_ci.sh all", exit_code must be
    # integer 0. Boolean, missing fields, and wrong types reject with 400
    # before any durable mutation.
    from runtime.models import LocalCiEvidence

    local_ci: LocalCiEvidence | None = None
    if body.local_ci is not None:
        if not isinstance(body.local_ci, dict):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "local_ci_not_object",
                    "got_type": type(body.local_ci).__name__,
                },
            )
        try:
            local_ci = LocalCiEvidence(**body.local_ci)
        except ValidationError as exc:
            # exc.errors() contains ValueError objects in ctx which are
            # not JSON-serializable — stringify for the HTTP response.
            serializable_errors = []
            for e in exc.errors():
                se = {k: v for k, v in e.items() if k != "ctx"}
                se["msg"] = str(e["msg"])
                serializable_errors.append(se)
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "local_ci_invalid",
                    "errors": serializable_errors,
                },
            )
    local_ci_json = _json.dumps(local_ci.model_dump()) if local_ci is not None else None
    decision_payload = dict(body.decision) if body.decision is not None else None
    if body.manager_self_evaluation is not None:
        task = org.db.get_task(task_id)
        if task is None or not org.teams.is_team_manager(body.agent):
            raise HTTPException(status_code=400, detail={"code": "manager_self_evaluation_not_available"})
        from runtime.orchestrator.active_authority_policy import load_session_policy_binding
        binding = load_session_policy_binding(
            db=org.db, task_id=task_id, session_id=body.session_id,
            agent_name=body.agent,
        )
        if not binding or binding.get("mode") != "db_release":
            raise HTTPException(status_code=400, detail={"code": "manager_self_evaluation_not_available"})
        from runtime.models import ManagerSelfEvaluation
        import hashlib as _hashlib
        raw_canonical = _json.dumps(
            body.manager_self_evaluation, sort_keys=True, default=str,
        )
        try:
            sanitized = ManagerSelfEvaluation.model_validate(
                body.manager_self_evaluation
            ).model_dump(mode="json")
        except ValidationError:
            sanitized = {
                "_error_code": "malformed_output",
                "payload_digest": _hashlib.sha256(raw_canonical.encode()).hexdigest(),
            }
        if decision_payload is None:
            decision_payload = {}
        decision_payload["_manager_self_evaluation"] = sanitized
    decision_json = _json.dumps(decision_payload) if decision_payload is not None else None
    async with org.db_lock:
        org.db.insert_task_result(
            task_id=task_id,
            agent=body.agent,
            session_id=body.session_id,
            status=body.status,
            output_summary=body.output_summary,
            decision_json=decision_json,
            confidence_score=body.confidence,
            risks_flagged=body.risks_flagged,
            output_dir=body.output_dir,
            waiting_on_job_ids=body.waiting_on_job_ids or None,
            verdict=body.verdict,
            local_ci_json=local_ci_json,
        )
    # Clear the tracker so a duplicate POST for the same session is rejected as
    # unknown_session rather than silently persisting a second row.
    org.sessions.clear(task_id, body.agent)
    # TODO(events): subscribers that connect after this point won't replay
    # `completion_reported`. The terminal task_* event is still synthesized
    # from the DB status, but per-agent completion beats are lost. Acceptable
    # today because the orchestrator consumes completions via DB (not SSE) and
    # SSE is for human observers.
    await org.event_bus.publish(task_id, {
        "type": "completion_reported",
        "agent": body.agent,
        "session_id": body.session_id,
        "status": body.status,
    })
    return {"ok": True}


class ProgressBody(BaseModel):
    session_id: str
    agent: str
    message: str


@router.post("/tasks/{task_id}/progress")
async def submit_progress(task_id: str, body: ProgressBody, org: OrgDep) -> dict:
    """Agent-controlled mid-task progress note.

    Same auth shape as /completion (active session must match), but does NOT
    clear the tracker — the agent keeps working after a progress beat. Audit-
    logged as `action=progress` and broadcast on SSE so `happyranch tail` shows live
    movement on long-running tasks.
    """
    from runtime.infrastructure.audit_logger import AuditLogger

    # Task-active gate runs BEFORE session ownership (see _require_task_active).
    _require_task_active(task_id, org.db.get_task(task_id))
    expected = org.sessions.get_active(task_id, body.agent)
    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "unknown_session", "task_id": task_id, "agent": body.agent},
        )
    if expected != body.session_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "session_mismatch", "active": expected, "got": body.session_id},
        )
    message = body.message.strip()
    if not message:
        raise HTTPException(
            status_code=400,
            detail={"code": "message_required"},
        )
    async with org.db_lock:
        AuditLogger(org.db).log_progress(
            task_id=task_id, agent=body.agent, message=message,
        )
    await org.event_bus.publish(task_id, {
        "type": "progress",
        "agent": body.agent,
        "session_id": body.session_id,
        "message": message,
    })
    return {"ok": True}


class ResolveEscalationBody(BaseModel):
    decision: str  # "supersede" | "continue"
    rationale: str = ""
    # For supersede: the brief for the successor task.
    brief: str = ""
    # Caller-declared actor for attribution. Advisory only — founder and agents
    # share one bearer token. Omitted/blank → "founder".
    actor: str | None = None


async def resolve_escalation_in_process(
    org,
    state,
    *,
    task_id: str,
    decision: str,
    rationale: str,
    brief: str = "",
    actor: str = "founder",
    thread_id: str | None = None,
    resolution_path: str = "manual_break_glass",
) -> str:
    """Same DB transition / audit / queue re-enqueue as the HTTP handler at
    POST /tasks/{task_id}/resolve-escalation.

    Returns the new task status value (e.g. "pending" or "superseded").
    Raises HTTPException for the same validation failures the route raises so
    the HTTP wrapper can re-raise as-is.

    THR-080: unified resolution verb — decisions are "supersede" and
    "continue" only. Cancel is removed from the resolution vocabulary;
    cancelling an escalated task now uses the normal POST /cancel route.
    """
    from runtime.infrastructure.audit_logger import AuditLogger
    from runtime.models import BlockKind, TaskStatus
    from runtime.orchestrator.run_step import (
        _enqueue_parent_if_waiting,
        _kill_jobs_for_terminating_task,
    )

    if decision not in ("supersede", "continue"):
        raise HTTPException(status_code=400, detail={"code": "invalid_decision"})
    task = org.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    # Path B: an escalated task is status=ESCALATED.
    is_escalated = task.status == TaskStatus.ESCALATED
    if not is_escalated:
        raise HTTPException(
            status_code=409,
            detail={"code": "task_not_escalated", "current_status": task.status.value},
        )
    trimmed = rationale.strip()

    if decision == "supersede":
        # Mint a successor task from the provided brief and close the
        # predecessor as SUPERSEDED. Reuses the proven _eligible_supersede_block_kind
        # + _supersede_predecessor_locked path (THR-018 tier #3).
        successor_brief = brief.strip()
        if not successor_brief:
            raise HTTPException(
                status_code=422,
                detail={"code": "supersede_requires_brief"},
            )
        pred_block_kind = _eligible_supersede_block_kind(org, task)
        if pred_block_kind is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "task_not_supersedable",
                    "task_id": task_id,
                    "status": task.status.value,
                },
            )
        async with org.db_lock:
            audit = AuditLogger(org.db)
            successor_id = org.db.next_task_id()
            org.db.insert_task(TaskRecord(
                id=successor_id,
                brief=successor_brief,
                team=task.team,
                assigned_agent=task.assigned_agent,
                parent_task_id=task.parent_task_id,
                dispatched_from_thread_id=thread_id or task.dispatched_from_thread_id,
            ))
            note_suffix = f"resolved by {actor}" + (f" via thread {thread_id}" if thread_id else "")
            if trimmed:
                note_suffix += f" — {trimmed}"
            # Canonical supersede tail: closes predecessor + revisit family,
            # wakes parents, emits thread followups (THR-080 #4).
            # Replaces the old manual _supersede_predecessor_locked + tail.
            _close_predecessor_family_and_run_tail(
                org, audit,
                predecessor=task,
                successor_root=successor_id,
                pred_block_kind=pred_block_kind,
                actor=actor,
                note_suffix=note_suffix,
                thread_id=thread_id,
                close_revisit_family=True,
            )
            # Also log escalation_resolved for the audit trail.
            audit.log_escalation_resolved(
                task_id=task_id,
                decision=decision,
                rationale=rationale,
                actor=actor,
                thread_id=thread_id, resolution_path=resolution_path,
            )
            # Best-effort: consume any open notification rows not already
            # consumed by _supersede_predecessor_locked inside the helper.
            for nrow in org.db.list_open_notifications_for_task(task_id):
                org.db.consume_escalation_notification(
                    nrow["feishu_message_id"], consumed_by="superseded",
                )
        # Post-tail specifics: kill jobs and enqueue the successor.
        _kill_jobs_for_terminating_task(org.orchestrator, task_id)
        if state.queue is not None:
            state.queue.put_nowait(org.slug, successor_id)
        return TaskStatus.SUPERSEDED.value

    # --- continue ---
    # Fail-closed gating (THR-080 memo §3): NEVER continue a task with
    # live children or terminal status.
    if _has_live_children(org, task_id):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "cannot_continue_live_children",
                "remedy": "Use supersede to mint a successor task instead.",
            },
        )

    # continue sends the task back to PENDING with the rationale on `note`.
    verb = "continued"
    resolved_note = f"{actor} {verb}: {trimmed}" if trimmed else f"{actor} {verb}"
    async with org.db_lock:
        new_status = TaskStatus.PENDING
        org.db.update_task(task_id, status=new_status, block_kind=None, note=resolved_note)
        AuditLogger(org.db).log_escalation_resolved(
            task_id=task_id, decision=decision, rationale=rationale,
            actor=actor, thread_id=thread_id, resolution_path=resolution_path,
        )
        # Best-effort: mark any open notification rows for this task
        # consumed, so they don't dangle.
        for nrow in org.db.list_open_notifications_for_task(task_id):
            org.db.consume_escalation_notification(
                nrow["feishu_message_id"], consumed_by="cli-fallback",
            )
    # Re-enqueue self. The manager's next step sees the rationale via the
    # escalation-resolved prompt header.
    if state.queue is not None:
        state.queue.put_nowait(org.slug, task_id)
    return new_status.value


def _has_live_children(org, task_id: str) -> bool:
    """True if the task has at least one child that is NOT terminal."""
    from runtime.models import TaskStatus
    from runtime.orchestrator.run_step import TERMINAL_STATES
    children = org.db.get_children(task_id)
    for cid in children:
        child = org.db.get_task(cid)
        if child is not None and child.status not in TERMINAL_STATES:
            return True
    return False


@router.post("/tasks/{task_id}/resolve-escalation")
async def resolve_escalation(
    task_id: str, body: ResolveEscalationBody, org: OrgDep, request: Request,
) -> dict:
    state: DaemonState = request.app.state.daemon
    actor = (body.actor or "").strip() or "founder"
    new_status = await resolve_escalation_in_process(
        org, state,
        task_id=task_id, decision=body.decision, rationale=body.rationale,
        brief=body.brief, actor=actor,
    )
    return {"ok": True, "task_id": task_id, "new_status": new_status}


class CancelBody(BaseModel):
    rationale: str = ""
    # Default cascades down the delegated subtree. The caller can ask for a
    # point-cancel with cascade=False but it's dangerous: a parent waiting on
    # a live child is cancelled while the child keeps running, leaving the
    # child with no observer for its eventual completion. Surfaced as a flag
    # rather than removed entirely because there are narrow cases (rogue-agent
    # isolation) where targeting a single node is right.
    cascade: bool = True
    # Caller-declared actor for attribution. Advisory only — founder and agents
    # share one bearer token, so this is not validated. Omitted/blank → "founder",
    # preserving the original founder-only behavior byte-for-byte.
    actor: str | None = None


class RevisitBody(BaseModel):
    founder_note: str | None = None
    # Founder-supplied per-task subprocess timeout (seconds). Persisted on the
    # new root and inherited by every delegated child. No daemon auto-successor
    # exists — recovery is explicit manager/founder action. NULL falls through
    # to the predecessor's value (so a manual revisit of an already-bumped
    # task keeps the bump) and then to org/Settings.
    session_timeout_seconds: int | None = None

    @field_validator("session_timeout_seconds")
    @classmethod
    def _positive_int(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ValueError("session_timeout_seconds must be a positive integer")
        return v


# Predecessor-root states that revisit accepts. Everything else is 409.
# `failed-cancelled` is not a DB value — it's the normalized label for
# (status=failed, cancelled_at!=NULL) that the response body returns and
# the team-manager prompt header surfaces.
_REVISIT_ELIGIBLE_STATUSES = frozenset({
    TaskStatus.FAILED, TaskStatus.COMPLETED,
})


@dataclass(frozen=True)
class RevisitResult:
    """Structured return value from revisit_from_notification.

    Carries all fields needed to build the HTTP response body, so the
    route does not need a second walk_ancestors call after the helper
    returns (eliminates the LineageTooDeep race and the double DB round-trip).
    """
    new_root_id: str
    predecessor_root_id: str
    flagged_task_id: str
    cascade: list[str]
    prior_status: str


def _classify_predecessor_status(task: TaskRecord) -> str | None:
    """Return the normalized prior_status label, or None if ineligible.

    Maps DB shape → the 4-valued spec vocabulary (preserved for revisit/
    supersede audit-citation compat):
      cancelled_at != NULL           → 'failed-cancelled'
      failed (no cancelled_at)       → 'failed'
      escalated                      → 'blocked-escalated'
      completed                      → 'completed'

    Path B: read ``cancelled_at`` FIRST (before the status label) so this
    classifies BOTH the new CANCELLED status and the historical
    failed+cancelled_at rows left as-is by the migration. The escalated case
    reads the new top-level ESCALATED status. Phase 3: the boot migration
    has already flipped any legacy blocked(escalated) rows.
    """
    from runtime.models import BlockKind
    if task.cancelled_at is not None:
        return "failed-cancelled"
    if task.status == TaskStatus.FAILED:
        return "failed"
    if task.status == TaskStatus.COMPLETED:
        return "completed"
    if task.status == TaskStatus.ESCALATED:
        return "blocked-escalated"
    return None


def _delegated_children_all_terminal(org, predecessor_id: str) -> bool:
    """True iff an in_progress(delegated) predecessor has at least one child and ALL
    its children are terminal.

    The non-cascading safety gate (Gap-B): a delegated parent may be superseded
    only when no live sibling would be abandoned — and never via cancel's
    SIGTERM cascade. THR-018 tier #3.
    """
    from runtime.orchestrator.run_step import TERMINAL_STATES
    children = [org.db.get_task(c) for c in org.db.get_children(predecessor_id)]
    return bool(children) and all(
        c is not None and c.status in TERMINAL_STATES for c in children
    )


def _eligible_supersede_block_kind(org, predecessor: TaskRecord) -> str | None:
    """Return 'escalated'/'delegated' if `predecessor` is a blocked task that a
    human-authorized continuation may auto-resolve to SUPERSEDED, else
    None.

    Delegated requires all children terminal (Gap-B safety gate). Used by the
    thread-dispatch supersede path, which names its predecessor explicitly via
    `resolves`; the revisit path derives the same eligibility from its lineage
    walk + `_classify_predecessor_status`. THR-018 tier #3 §3a.

    Path B: an escalated predecessor is status=ESCALATED; a delegating
    predecessor is in_progress(delegated). Phase 3: no legacy blocked shapes;
    the boot-time migration flips them before request handling.
    """
    if predecessor.status == TaskStatus.ESCALATED:
        return "escalated"
    if (
        predecessor.status == TaskStatus.IN_PROGRESS
        and predecessor.block_kind == BlockKind.DELEGATED
        and _delegated_children_all_terminal(org, predecessor.id)
    ):
        return "delegated"
    return None


def _supersede_predecessor_locked(
    org,
    audit,
    *,
    predecessor_id: str,
    successor_root: str,
    prior_block_kind: str,
    actor: str,
    note_suffix: str | None = None,
    thread_id: str | None = None,
) -> None:
    """Transition an escalated or in_progress(delegated) predecessor to the terminal
    SUPERSEDED status — block_kind cleared, audit citing the concrete
    successor root (the maker-checker evidence) and, on the thread path, the
    dispatching thread ruling.

    Caller MUST hold ``org.db_lock``. Gap-A: this NEVER re-enqueues the
    predecessor (no ``queue.put_nowait``) — a terminal close must not spawn a
    wasted manager session. The caller runs ``_enqueue_parent_if_waiting`` AFTER
    releasing the lock so a delegated parent still learns its branch reached
    terminal. Shared by the founder-`revisit` and founder/manager thread-dispatch
    continuation paths so the two surfaces cannot drift. THR-018 tier #3 §3a.
    """
    note = f"Resolved: superseded by continuation {successor_root}"
    if note_suffix:
        note += f" — {note_suffix}"
    org.db.update_task(
        predecessor_id,
        status=TaskStatus.SUPERSEDED,
        block_kind=None,
        note=note,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    audit.log_escalation_superseded(
        predecessor_id,
        successor_root=successor_root,
        prior_block_kind=prior_block_kind,
        actor=actor,
        founder_note=note_suffix,
        thread_id=thread_id,
    )
    # An escalated/delegated predecessor may carry an open notification;
    # the continuation resolves it, so consume any open rows.
    for nrow in org.db.list_open_notifications_for_task(predecessor_id):
        org.db.consume_escalation_notification(
            nrow["feishu_message_id"], consumed_by="superseded",
        )


def _close_predecessor_family_and_run_tail(
    org,
    audit,
    *,
    predecessor: TaskRecord,
    successor_root: str,
    pred_block_kind: str,
    actor: str,
    note_suffix: str | None = None,
    thread_id: str | None = None,
    close_revisit_family: bool = True,
) -> list[str]:
    """Close predecessor (+ optionally revisit family), wake parents, emit
    thread followups.

    Canonical supersede tail shared by resolve_escalation_in_process and the
    thread-dispatch {resolves:} path so the two surfaces cannot drift
    (THR-080 #4).  Caller MUST hold ``org.db_lock`` for the
    ``_supersede_predecessor_locked`` calls.  The tail operations
    (``_enqueue_parent_if_waiting``, ``_maybe_post_thread_followup``) are
    safe under or outside the lock.

    Returns the list of revisit-family task ids that were also closed.
    """
    from runtime.orchestrator.run_step import (
        _enqueue_parent_if_waiting,
        _maybe_post_thread_followup,
    )

    # Close predecessor.
    _supersede_predecessor_locked(
        org, audit,
        predecessor_id=predecessor.id,
        successor_root=successor_root,
        prior_block_kind=pred_block_kind,
        actor=actor,
        note_suffix=note_suffix,
        thread_id=thread_id,
    )

    # Optionally close the revisit family.
    family_closed: list[str] = []
    if close_revisit_family:
        for family_task in _collect_eligible_revisit_family(
            org,
            explicit_predecessor_id=predecessor.id,
            successor_root=successor_root,
        ):
            family_block_kind = _eligible_supersede_block_kind(org, family_task)
            _supersede_predecessor_locked(
                org, audit,
                predecessor_id=family_task.id,
                successor_root=successor_root,
                prior_block_kind=family_block_kind,
                actor=actor,
                note_suffix=note_suffix,
                thread_id=thread_id,
            )
            family_closed.append(family_task.id)

    # Tail: wake parents and emit thread followups for thread-originated
    # predecessors (the missing followup in THR-080 #3).
    _enqueue_parent_if_waiting(org.orchestrator, predecessor.id)
    _maybe_post_thread_followup(
        org.orchestrator, predecessor.id,
        status=TaskStatus.SUPERSEDED, auto_revisit_spawned=False,
    )
    for family_task_id in family_closed:
        _enqueue_parent_if_waiting(org.orchestrator, family_task_id)
        _maybe_post_thread_followup(
            org.orchestrator, family_task_id,
            status=TaskStatus.SUPERSEDED, auto_revisit_spawned=False,
        )

    return family_closed


def _collect_eligible_revisit_family(
    org,
    *,
    explicit_predecessor_id: str,
    successor_root: str,
) -> list[TaskRecord]:
    """Find eligible revisit-family siblings that should be superseded alongside
    the explicit predecessor on a human-authorized continuation.

    Walks the revisit_of_task_id chain from the explicit predecessor to the
    family root, then collects all tasks reachable through revisit_of_task_id
    in the same family tree. Each collected task is filtered through
    ``_eligible_supersede_block_kind`` — escalated and in_progress(delegated)
    with all-terminal children are eligible; completed, failed, cancelled,
    pending, in_progress(non-delegated), and already superseded tasks
    are skipped. The explicit predecessor and the new successor root are also
    excluded.

    THR-046 msg127 option 3: broader sibling revisit-family closure.
    Caller MUST hold ``org.db_lock`` so eligibility checks are consistent.
    """
    # Find the family root by walking the revisit_of_task_id chain up.
    family_root_id = explicit_predecessor_id
    while True:
        task = org.db.get_task(family_root_id)
        if task is None or task.revisit_of_task_id is None:
            break
        family_root_id = task.revisit_of_task_id

    # Collect all tasks in the revisit-family tree via BFS.
    eligible: list[TaskRecord] = []
    to_process = [family_root_id]
    visited: set[str] = {family_root_id, successor_root, explicit_predecessor_id}

    # Evaluate the family root through the eligibility gate (unless it IS the
    # explicit predecessor, which the caller handles separately). When the
    # explicit predecessor is itself a revisit, the original ancestor root
    # must be checked for supersedability here — it is never reached by the
    # BFS below because it is pre-seeded in `visited`.
    if family_root_id != explicit_predecessor_id:
        root_task = org.db.get_task(family_root_id)
        if root_task is not None and _eligible_supersede_block_kind(org, root_task) is not None:
            eligible.append(root_task)

    while to_process:
        current_id = to_process.pop()
        for r_id in org.db.get_direct_revisits(current_id):
            if r_id in visited:
                continue
            visited.add(r_id)
            to_process.append(r_id)

            task = org.db.get_task(r_id)
            if task is None:
                continue

            # Skip the explicit predecessor and successor — already handled / new.
            if r_id == explicit_predecessor_id or r_id == successor_root:
                continue

            # Eligibility gate: only escalated or in_progress(delegated) with
            # all-terminal children may be superseded.
            if _eligible_supersede_block_kind(org, task) is not None:
                eligible.append(task)

    return eligible


async def revisit_from_notification(
    org,
    state,
    *,
    task_id: str,
    founder_note: str | None,
    actor: str,
    session_timeout_seconds: int | None = None,
) -> RevisitResult:
    """Spawn a new root task linked to the predecessor.

    Mirrors the POST /tasks/{id}/revisit HTTP handler.

    Args:
        actor: "cli" (HTTP route) or another surface name. Recorded
            on the revisit_of audit row.
        session_timeout_seconds: Override; if None, inherit from predecessor.

    Returns a RevisitResult with all fields needed by the caller.
    Raises HTTPException for 404 / 409.
    """
    from runtime.infrastructure.audit_logger import AuditLogger
    from runtime.infrastructure.database import LineageTooDeep

    flagged = org.db.get_task(task_id)
    if flagged is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")

    # Walk to the predecessor root. Defensive bound guards against corrupt cycles.
    try:
        chain = org.db.walk_ancestors(task_id, max_hops=20)
    except LineageTooDeep as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "lineage_too_deep", "reason": str(exc)},
        )
    if not chain:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    predecessor = chain[-1]  # root is last; chain is [flagged, ..., root]

    prior_status = _classify_predecessor_status(predecessor)
    # Gap-B (THR-018 §3): an in_progress(delegated) predecessor is revisit-eligible
    # only when ALL its children are terminal. Superseding it must never
    # abandon — or cascade-SIGTERM — a live sibling, so a delegated parent with
    # any in-flight child stays ineligible (falls into the 409 below). The
    # all-terminal gate is the safety boundary for the non-cascading close.
    if (
        prior_status is None
        and predecessor.status == TaskStatus.IN_PROGRESS
        and predecessor.block_kind == BlockKind.DELEGATED
        and _delegated_children_all_terminal(org, predecessor.id)
    ):
        prior_status = "blocked-delegated"
    if prior_status is None:
        from runtime.models import BlockKind as _BK
        raise HTTPException(
            status_code=409,
            detail={
                "code": "cannot_revisit",
                "reason": f"predecessor {predecessor.id} is {predecessor.status.value}",
                "predecessor_root_task_id": predecessor.id,
                "predecessor_status": predecessor.status.value,
                "block_kind": (
                    predecessor.block_kind.value
                    if isinstance(predecessor.block_kind, _BK) else None
                ),
            },
        )

    # cascade: [predecessor_root, ..., flagged]. chain is [flagged, ..., root],
    # so reverse it. When flagged == root, this is a single-element list.
    cascade = [t.id for t in reversed(chain)]

    new_timeout = (
        session_timeout_seconds
        if session_timeout_seconds is not None
        else predecessor.session_timeout_seconds
    )
    async with org.db_lock:
        new_id = org.db.next_task_id()
        # Track family tasks closed during the db lock so tail handling
        # (parent-wake, thread-followup) can be applied after release.
        family_closed: list[str] = []
        org.db.insert_task(TaskRecord(
            id=new_id,
            brief=predecessor.brief,
            team=predecessor.team,
            assigned_agent=predecessor.assigned_agent,
            status=TaskStatus.PENDING,
            parent_task_id=None,
            revisit_of_task_id=predecessor.id,
            session_timeout_seconds=new_timeout,
        ))
        audit = AuditLogger(org.db)
        audit.log_revisit_of(
            task_id=new_id,
            predecessor_root=predecessor.id,
            flagged=task_id,
            cascade=cascade,
            prior_status=prior_status,
            founder_note=founder_note,
            actor=actor,
        )
        audit.log_revisit_spawned(
            predecessor_task_id=predecessor.id, new_root=new_id,
        )
        # §3(a) forcing function: an escalated or in_progress(delegated) predecessor is
        # auto-resolved to the terminal SUPERSEDED — block_kind
        # cleared, audit citing the new continuation root (the maker-checker
        # evidence). It is NOT re-enqueued (that would spawn a wasted manager
        # session); parent-wake is preserved below. Distinct from the founder's
        # manual `resolve-escalation continue`, which intentionally re-runs work.
        if prior_status in ("blocked-escalated", "blocked-delegated"):
            prior_block_kind = (
                "escalated" if prior_status == "blocked-escalated" else "delegated"
            )
            _supersede_predecessor_locked(
                org, audit,
                predecessor_id=predecessor.id,
                successor_root=new_id,
                prior_block_kind=prior_block_kind,
                actor=actor,
                note_suffix=founder_note,
            )
            # THR-046 msg127: broader revisit-family closure — also supersede
            # eligible sibling/ancestor revisits in the same revisit family.
            for family_task in _collect_eligible_revisit_family(
                org,
                explicit_predecessor_id=predecessor.id,
                successor_root=new_id,
            ):
                family_block_kind = _eligible_supersede_block_kind(org, family_task)
                _supersede_predecessor_locked(
                    org, audit,
                    predecessor_id=family_task.id,
                    successor_root=new_id,
                    prior_block_kind=family_block_kind,
                    actor=actor,
                    note_suffix=founder_note,
                )
                family_closed.append(family_task.id)
        # When the founder revisits via CLI, any open failure notification row
        # for this task is implicitly resolved — consume it with cli-fallback
        # so it doesn't dangle. Mirrors resolve_escalation_in_process's behavior.
        if actor == "cli":
            for nrow in org.db.list_open_notifications_for_task(task_id):
                if nrow.get("kind") == "failure":
                    org.db.consume_escalation_notification(
                        nrow["feishu_message_id"], consumed_by="cli-fallback",
                    )

    enqueue_task(state, org.slug, new_id)

    # Preserve parent-wake: the superseded predecessor just reached a terminal,
    # so a delegated parent (if any) must learn its branch is done. Mirrors the
    # cancel path in resolve_escalation_in_process; runs outside the db_lock.
    # The superseded task itself is NEVER re-enqueued (no queue.put_nowait).
    if prior_status in ("blocked-escalated", "blocked-delegated"):
        from runtime.orchestrator.run_step import (
            _enqueue_parent_if_waiting,
            _maybe_post_thread_followup,
        )
        _enqueue_parent_if_waiting(org.orchestrator, predecessor.id)
        _maybe_post_thread_followup(
            org.orchestrator, predecessor.id,
            status=TaskStatus.SUPERSEDED, auto_revisit_spawned=False,
        )
        # Same tail for each family sibling closed — parent-wake and
        # thread-followup where applicable (thread-dispatch path pattern).
        for family_task_id in family_closed:
            _enqueue_parent_if_waiting(org.orchestrator, family_task_id)
            _maybe_post_thread_followup(
                org.orchestrator, family_task_id,
                status=TaskStatus.SUPERSEDED, auto_revisit_spawned=False,
            )

    return RevisitResult(
        new_root_id=new_id,
        predecessor_root_id=predecessor.id,
        flagged_task_id=task_id,
        cascade=cascade,
        prior_status=prior_status,
    )


@router.post("/tasks/{task_id}/revisit")
async def revisit_task(
    task_id: str, body: RevisitBody, org: OrgDep, request: Request,
) -> dict:
    """Founder-initiated: spawn a fresh root that inherits the predecessor's
    brief and references it via audit-log entries.

    The predecessor root (the ancestor we walk up to) MUST be in a terminal-ish
    state — see `_classify_predecessor_status`. The flagged task (the id the
    founder gave us) can be in any state; only the root's status is validated.
    """
    state: DaemonState = request.app.state.daemon
    result = await revisit_from_notification(
        org, state,
        task_id=task_id,
        founder_note=body.founder_note,
        actor="cli",
        session_timeout_seconds=body.session_timeout_seconds,
    )
    return {
        "new_root_task_id": result.new_root_id,
        "predecessor_root_task_id": result.predecessor_root_id,
        "flagged_task_id": result.flagged_task_id,
        "cascade": result.cascade,
        "predecessor_status": result.prior_status,
    }


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str, body: CancelBody, org: OrgDep,
) -> dict:
    """Founder-initiated cancel of a task and (by default) its descendants.

    Order of operations matters. We stamp the DB row *before* sending SIGTERM
    so that run_step's post-Popen classifier — which re-enters with a
    subprocess rc=-15 looking like a normal failure — observes
    ``status=FAILED`` and ``cancelled_at != NULL`` and short-circuits instead
    of overwriting the founder's note with "agent session failed (rc=-15)".

    The corresponding idempotence guards in ``_complete`` / ``_fail`` are the
    other half of the race lock.
    """
    from runtime.infrastructure.audit_logger import AuditLogger
    from runtime.orchestrator.run_step import _kill_jobs_for_terminating_task

    root = org.db.get_task(task_id)
    if root is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    if root.status in _TERMINAL_TASK_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "task_already_terminal", "current_status": root.status.value},
        )

    # BFS subtree walk (cascade=True) or single-task (cascade=False). Only
    # non-terminal rows are collected — anything already COMPLETED/FAILED
    # stays untouched.  Also capture prior statuses so Phase 1 can route
    # PENDING-only followup calls without a second DB round-trip.
    to_cancel: list[str] = []
    prior_statuses: dict[str, TaskStatus] = {}
    # Path B: capture the prior block_kind too. A parked task is now
    # in_progress(delegated|blocked_on_job) with no live subprocess; the
    # discriminant is what tells "parked" apart from "running" so Phase 1b
    # fires the thread followup for exactly the right set (see below).
    prior_block_kinds: dict[str, BlockKind | None] = {}
    stack = [task_id]
    while stack:
        tid = stack.pop()
        t = org.db.get_task(tid)
        if t is None or t.status in _TERMINAL_TASK_STATUSES:
            continue
        to_cancel.append(tid)
        prior_statuses[tid] = t.status
        prior_block_kinds[tid] = t.block_kind
        if body.cascade:
            stack.extend(org.db.get_children(tid))

    now = datetime.now(timezone.utc).isoformat()
    rationale = body.rationale.strip()
    actor = (body.actor or "").strip() or "founder"
    note = f"cancelled by {actor}: {rationale}" if rationale else f"cancelled by {actor}"

    # Phase 1: DB writes + audit under the lock, to serialise with run_step
    # transitions. Collect opaque cancel controls AND (defensively) pids
    # without a registered control while we hold the lock — we invoke them
    # outside.
    controls_to_invoke: list[tuple[str, str, Callable[[], None]]] = []
    pids_without_control: list[tuple[str, str, int]] = []
    audit = AuditLogger(org.db)
    async with org.db_lock:
        for tid in to_cancel:
            # Path B: a founder cancel writes the dedicated terminal CANCELLED
            # status (was FAILED + cancelled_at). cancelled_at is still set so
            # the cancel-race guards and cancellation derivations are unchanged.
            org.db.update_task(
                tid,
                status=TaskStatus.CANCELLED,
                block_kind=None,
                note=note,
                cancelled_at=now,
                completed_at=now,
            )
            controls_by_agent = dict(org.sessions.iter_task_cancel_controls(tid))
            for agent, control in controls_by_agent.items():
                controls_to_invoke.append((tid, agent, control))
            # THR-207: a wired session's PID is diagnostic/restart evidence
            # only — cancellation goes through the opaque control above, never
            # a bare PID signal. A defensive SIGTERM remains ONLY for a
            # (task, agent) that has a pid but NO registered control
            # (non-wired/legacy sessions — impossible in production because
            # the control is registered before launch).
            for agent, pid in org.sessions.iter_task_pids(tid):
                if agent not in controls_by_agent:
                    pids_without_control.append((tid, agent, pid))
            audit.log_task_cancelled(
                task_id=tid, rationale=rationale, cascade=body.cascade, actor=actor,
            )

    # Phase 1b: fire thread followup for every cancelled task that had NO live
    # subprocess at cancel time.
    #
    # Two-site coverage (disjoint conditions, no double-fire risk):
    #   • No live subprocess → cancel route owns the followup here, because
    #     SIGTERM is never sent so run_step never runs and the cancel-race guard
    #     in run_step never triggers. Under Path B that set is: PENDING,
    #     ESCALATED, the parked carriers in_progress(delegated|blocked_on_job),
    #     and the parked carriers in_progress(delegated|blocked_on_job).
    #   • Live subprocess (in_progress + block_kind IS NULL) → cancel route sends
    #     SIGTERM (Phase 2 below); the subprocess exits with rc=-15; run_step's
    #     cancel-race guard (``refetch.cancelled_at is not None → return``) fires
    #     the helper there instead.
    #
    # The discriminant `block_kind IS NULL` is exactly what tells a running task
    # from a parked one under Path B, so this stays disjoint and never
    # double-fires.
    from runtime.orchestrator.run_step import _maybe_post_thread_followup
    for tid in to_cancel:
        prior_status = prior_statuses.get(tid)
        had_live_subprocess = (
            prior_status == TaskStatus.IN_PROGRESS
            and prior_block_kinds.get(tid) is None
        )
        if not had_live_subprocess:
            _maybe_post_thread_followup(
                org.orchestrator, tid,
                status=TaskStatus.FAILED, auto_revisit_spawned=False,
            )

    # Phase 2: deliver cancellation. Wired sessions go through the opaque
    # containment control, invoked OFF the event loop (backend finish is
    # blocking: bounded TERM grace + KILL escalation + quiescence verify); the
    # control freezes CANCELLED on the durable ownership record and drives
    # containment teardown, and the run-step cancel-race guard drops the late
    # result. os.kill runs outside the db_lock so a slow signal delivery can't
    # stall concurrent DB writers. ProcessLookupError means the subprocess
    # already exited — fine, the DB row is already in its terminal shape.
    killed: list[dict] = []
    for tid, agent, control in controls_to_invoke:
        try:
            await asyncio.to_thread(control)
            killed.append({"task_id": tid, "agent": agent})
        except Exception as exc:
            logger.warning(
                "cancel %s: opaque cancel control invocation failed: %s",
                tid, exc,
            )
        # Clear the tracker entry so a parent auto-resume (if one gets
        # enqueued via run_step's dependent-child check) can't find a stale
        # pid/control and mis-route a subsequent cancel.
        org.sessions.clear(tid, agent)
    for tid, agent, pid in pids_without_control:
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append({"task_id": tid, "agent": agent, "pid": pid})
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.warning(
                "cancel %s: os.kill(%s, SIGTERM) failed: %s", tid, pid, exc,
            )
        org.sessions.clear(tid, agent)

    # Phase 3: publish terminal events for any live SSE tails. EventBus
    # recognises `task_failed` as terminal (see _TERMINAL_TYPES in
    # event_bus.py) and closes the stream on the observer side.
    for tid in to_cancel:
        await org.event_bus.publish(tid, {
            "type": "task_failed",
            "outcome": "cancelled",
            "task_id": tid,
        })

    # Phase 4: kill persistent jobs owned by any cancelled task. Fire-and-
    # forget so the route response isn't blocked on the 5s SIGTERM grace.
    for tid in to_cancel:
        _kill_jobs_for_terminating_task(org.orchestrator, tid)

    return {
        "ok": True,
        "task_id": task_id,
        "cancelled": to_cancel,
        "killed": killed,
    }


# ── Task attachment routes (THR-109) ─────────────────────────────────────────


def _task_attachment_store(org: object) -> TaskAttachmentStore:
    from runtime.orchestrator._paths import OrgPaths
    return TaskAttachmentStore(OrgPaths(org.root).task_attachments_dir)


class TaskAttachmentUploadResponse(BaseModel):
    storage_key: str
    display_name: str
    size_bytes: int
    content_type: str | None = None
    uploaded_by: str


@router.post("/tasks/attachments")
async def upload_task_attachment(
    slug: str,
    org: OrgDep,
    request: Request,
    file: UploadFile = File(...),
    agent: str = Query("founder"),
) -> dict:
    """Upload a file to the task-attachment private store.

    Returns a storage_key for reference on POST /tasks.
    Does NOT create a task_attachments DB row — that happens on task create.
    """
    from fastapi import UploadFile
    import mimetypes

    content = await file.read(MAX_TASK_ATTACHMENT_BYTES + 1)
    if len(content) > MAX_TASK_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "attachment_too_large",
                "max_bytes": MAX_TASK_ATTACHMENT_BYTES,
                "size_bytes": len(content),
            },
        )

    display_name = file.filename or "attachment"
    try:
        sanitize_display_name(display_name)
    except TaskAttachmentInvalidName:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_attachment_display_name"},
        )

    # Resolve content type from declared mime-type or file extension.
    content_type = file.content_type or mimetypes.guess_type(display_name)[0]
    if content_type and content_type not in _allowed_content_types_set():
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unsupported_attachment_type",
                "content_type": content_type,
            },
        )

    # Validate and resolve content type.
    resolved_type = resolve_content_type(display_name, content_type)

    import uuid
    storage_key = _sanitize_storage_key(display_name) + "-" + uuid.uuid4().hex[:12]

    store = _task_attachment_store(org)
    try:
        size_bytes = store.put(storage_key, content)
    except TaskAttachmentTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": "attachment_too_large", "detail": str(exc)},
        )

    # Audit: private-store upload.
    from runtime.infrastructure.audit_logger import AuditLogger
    AuditLogger(org.db).log_task_attachment_uploaded(
        storage_key=storage_key,
        display_name=display_name,
        size_bytes=size_bytes,
        content_type=resolved_type,
        agent=agent,
    )

    return {
        "storage_key": storage_key,
        "display_name": display_name,
        "size_bytes": size_bytes,
        "content_type": resolved_type,
        "uploaded_by": agent,
    }


@router.get("/tasks/{task_id}/attachments")
def list_task_attachments(
    slug: str, task_id: str, org: OrgDep,
) -> dict:
    """List attachments for a task (THR-109 seq25 org-scoped bearer read).

    Any authenticated caller in the same org may list attachments for an
    extant task. Returns the task's own attachments plus inherited
    ancestor attachments resolved via the parent_task_id chain (own +
    ancestors union, deterministic order).

    Cross-org access is denied by the existing org-scoped context.
    """
    task = org.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_task"})

    attachments = org.db.resolve_ancestor_attachments(task_id)
    return {
        "task_id": task_id,
        "attachments": [
            {
                "storage_key": a.storage_key,
                "task_id": a.task_id,
                "ordinal": a.ordinal,
                "display_name": a.display_name,
                "size_bytes": a.size_bytes,
                "content_type": a.content_type,
                "uploaded_by": a.uploaded_by,
                "created_at": a.created_at,
            }
            for a in attachments
        ],
    }


@router.get("/tasks/{task_id}/attachments/{storage_key}")
def get_task_attachment(
    slug: str,
    task_id: str,
    storage_key: str,
    org: OrgDep,
    request: Request,
):
    """Download a task attachment's bytes (THR-109 seq25 org-scoped bearer read).

    Any authenticated caller in the same org may download attachment bytes
    for an extant task. The attachment is resolved from the task's own
    attachments or inherited from ancestors via the parent_task_id chain.

    Cross-org access is denied by the existing org-scoped context.
    No requester task/session identity is accepted or required.
    """
    from fastapi.responses import StreamingResponse

    task = org.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_task"})

    # Check own attachments first, then inherited.
    record = org.db.get_task_attachment(task_id, storage_key)
    if record is None:
        # Try inherited: resolve ancestor attachments and match by storage_key.
        inherited = org.db.resolve_ancestor_attachments(task_id)
        for att in inherited:
            if att.storage_key == storage_key and att.task_id != task_id:
                record = att
                break
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "task_attachment_not_found"},
        )

    store = _task_attachment_store(org)
    try:
        content = store.read(record.storage_key)
    except TaskAttachmentNotFound:
        raise HTTPException(
            status_code=404,
            detail={"code": "task_attachment_bytes_missing"},
        )

    media_type = record.content_type or "application/octet-stream"
    return StreamingResponse(
        iter([content]),
        media_type=media_type,
        headers={
            "Content-Disposition":
                f'attachment; filename="{record.display_name}"',
        },
    )


# ── Content-type allowlist (lazy init to avoid circular imports) ─────────────

_ALLOWED_CTS: frozenset[str] | None = None


def _allowed_content_types_set() -> frozenset[str]:
    global _ALLOWED_CTS
    if _ALLOWED_CTS is None:
        from runtime.infrastructure.task_attachment_store import \
            _ALLOWED_CONTENT_TYPES
        _ALLOWED_CTS = _ALLOWED_CONTENT_TYPES
    return _ALLOWED_CTS


def _sanitize_storage_key(display_name: str) -> str:
    """Generate a safe prefix for the storage_key from the display_name."""
    import re
    base = Path(display_name).stem[:50]
    base = re.sub(r'[^A-Za-z0-9._-]', '_', base)
    return base or "file"
