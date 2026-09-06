from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum

from typing import Literal

from pydantic import BaseModel, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator


class TaskStatus(StrEnum):
    # THR-037 Change B (Path B, stored source-of-truth): the surfaced `blocked`
    # vocabulary collapses into a model that stores what is actually true. A
    # parent waiting on its own children/jobs is IN_PROGRESS, not BLOCKED; the
    # waiting reason is preserved in the `block_kind` discriminant. See
    # docs/superpowers/specs/2026-06-27-task-status-pathB-stored-design.md.
    PENDING = "pending"
    # Two-valued, discriminated by `block_kind` (see BlockKind):
    #   block_kind IS NULL                       ⟺ a subprocess is running now.
    #   block_kind IN (delegated, blocked_on_job) ⟺ parked, no subprocess,
    #     waiting on children/jobs it manages internally.
    IN_PROGRESS = "in_progress"
    # NEW (Path B), non-terminal. A task that needs a founder decision (genuine
    # agent escalation, failure-round-bound exhaustion, or budget exhaustion).
    # Was the legacy blocked(escalated) state; `block_kind` is cleared. The
    # founder resolves it via resolve-escalation (continue → pending / supersede →
    # cancelled, cancelled_at set). NOT in any terminal predicate.
    ESCALATED = "escalated"
    COMPLETED = "completed"              # terminal
    FAILED = "failed"                   # terminal
    # NEW (Path B), terminal. A founder-initiated stop (was failed + cancelled_at
    # set). Distinct from FAILED so the audit/event trail shows a deliberate
    # cancellation, not an agent/executor failure. `cancelled_at` is still set.
    # Replays as a failure-class terminal event with outcome="cancelled" (see
    # OrgState._TERMINAL_STATUS_TO_EVENT). Joins every terminal predicate.
    CANCELLED = "cancelled"
    # Terminal. An escalated|delegated task whose follow-up work moved to a
    # human-authorized continuation (founder `revisit` / thread-dispatch) is
    # closed here instead of re-running — distinct from COMPLETED so the audit
    # trail shows it was superseded, not finished by an agent. Joins every
    # terminal predicate (TERMINAL_STATES, _TERMINAL_TASK_STATUSES,
    # _TERMINAL_STATUS_TO_EVENT). See protocol/05c-orchestrator.md and
    # docs/agent-guides/features-and-invariants.md (escalation).
    #
    # Phase 3 (THR-037): BLOCKED and BlockKind.ESCALATED were fully retired
    # after the transition soak. No live row carries 'blocked' after the
    # idempotent boot migration. See
    # docs/superpowers/specs/2026-06-27-task-status-pathB-stored-design.md §I.
    SUPERSEDED = "superseded"



class BlockKind(StrEnum):
    # Path B: the waiting-reason discriminant for an IN_PROGRESS task —
    # "what this task is internally waiting on (NULL = a subprocess is running
    # now)". Live domain narrowed to {DELEGATED, BLOCKED_ON_JOB}.
    DELEGATED = "delegated"
    BLOCKED_ON_JOB = "blocked_on_job"


class ReviewVerdict(StrEnum):
    APPROVE = "approve"
    REVISE = "revise"
    REJECT = "reject"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TaskRecord(BaseModel):
    id: str
    status: TaskStatus = TaskStatus.PENDING
    assigned_agent: str | None = None
    team: str = "engineering"
    # Provenance, NOT a behavior label: "subtask" iff spawned from an ongoing
    # task; "task" otherwise (founder-dispatched root). The orchestration gate
    # in run_step keys on this — see
    # docs/superpowers/specs/2026-06-03-subtask-composite-task-design.md.
    task_type: Literal["task", "subtask"] = "task"
    brief: str
    parent_task_id: str | None = None
    revisit_of_task_id: str | None = None
    dispatched_from_thread_id: str | None = None
    block_kind: BlockKind | None = None
    blocked_on_job_ids: str | None = None
    # In-flight inline delegation chain (JSON-serialized ChainState). NULL when no
    # chain is active on this parent. See docs/superpowers/specs/2026-05-30-inline-
    # delegation-chain-design.md.
    active_chain: str | None = None
    # In-flight fan-out metadata (JSON-serialized FanoutState). NULL when no
    # fan-out is active on this parent. Set atomically with child spawns;
    # cleared on successful join claim or terminal parent close.
    active_fanout: str | None = None
    note: str | None = None
    final_output_dir: str | None = None
    orchestration_step_count: int = 0
    revision_count: int = 0
    # Per-task override for the agent-session subprocess timeout (seconds).
    # NULL → fall through to org/config.yaml, then Settings default. Set by
    # `happyranch revisit --session-timeout-seconds`; inherited from parent on
    # delegate, and from predecessor root on revisit.
    session_timeout_seconds: int | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    completed_at: datetime | None = None
    # Founder-initiated cancellation marker. Under Path B a new cancellation
    # sets status=CANCELLED alongside this timestamp; historical rows left
    # as-is carry the old status=FAILED + cancelled_at shape, so derivations
    # that must classify cancellation (e.g. _classify_predecessor_status) read
    # `cancelled_at` presence rather than the status label for backward compat.
    cancelled_at: datetime | None = None
    last_heartbeat: datetime | None = None
    # OS pid of the executor subprocess, persisted at session start for
    # daemon-restart liveness probe (THR-079). NULL for tasks that predate
    # the column or that haven't reached _on_started yet. Internal signal —
    # not serialized to API responses.
    executor_pid: int | None = None
    # Session id of the CURRENT live subprocess, persisted at session start
    # alongside executor_pid. Used by the daemon-restart sweep (THR-090 Track A)
    # to scope orphaned-result detection to the current session only — a
    # prior-step result row carries a different session uuid and must never
    # match. NULL for tasks that predate the column or that haven't reached
    # _on_started yet. Internal signal — not serialized to API responses.
    current_session_id: str | None = None
    # Timestamp of the FIRST zombie detection for the ongoing zombie reaper
    # (THR-090 Track B). Set when a zombie predicate matches; used for
    # flag-then-cancel-on-TTL. Cleared (set to NULL) if the task recovers
    # before the TTL expires. NULL default — never been flagged. Internal
    # signal — not serialized to API responses.
    zombie_flagged_at: datetime | None = None


class TaskAttachmentRef(BaseModel):
    """Reference to a previously uploaded task attachment.

    The canonical model lives here so the orchestrator and daemon share
    the exact same shape without importing daemon route internals.
    """
    storage_key: str
    display_name: str | None = None


class ChainLeg(BaseModel):
    """One leg of an inline delegation chain. The manager declares legs 2..N in
    NextStep.then; the first leg is the existing delegate payload (agent +
    prompt + optional expect_verdict).
    """
    agent: str
    prompt: str
    expect_verdict: str | None = None
    # THR-109: per-leg task attachments. Only referenced when this leg is
    # auto-advanced and the orchestrator spawns the next child task.
    # Keys must have been pre-uploaded via `happyranch tasks attach-upload`.
    attachments: list[TaskAttachmentRef] | None = None


class FanoutChild(BaseModel):
    """One child in a fanout/parallel NextStep. Phase 1: read-only children only —
    each carries agent + prompt. ``then`` and ``expect_verdict`` are NOT allowed in
    Phase 1 and are parse-rejected (mutating fan-out is out of scope).
    """
    agent: str
    prompt: str
    # Phase 1 rejects these fields — they exist only for Phase 2+ forward compat.
    then: list[ChainLeg] = Field(default_factory=list)
    expect_verdict: str | None = None
    # THR-109: per-child task attachments. A pipeline carrier owns its
    # declared refs; its spawned first leg receives them only through
    # normal ancestor inheritance, never a duplicate link/claim.
    attachments: list[TaskAttachmentRef] | None = None
    # THR-078: mandatory lineage link when this fan-out child retries a
    # FAILED sibling assigned to the same agent.
    revisit_of_task_id: str | None = None


class ManagerSupersessionAttestation(BaseModel):
    """Manager-supplied postmortem evidence for a THR-152 supersession."""

    model_config = {"extra": "forbid", "strict": True}

    recovery_reason: StrictStr
    policy_product_intent_unchanged: StrictBool
    no_budget_or_external_commitment: StrictBool
    no_permission_or_cross_team_change: StrictBool
    no_schema_auth_security_privacy_or_data_access_change: StrictBool
    no_unresolved_founder_gate: StrictBool

    @field_validator("recovery_reason")
    @classmethod
    def _recovery_reason_is_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("supersede.attestation.recovery_reason must be a nonblank string")
        return value

    @field_validator(
        "policy_product_intent_unchanged",
        "no_budget_or_external_commitment",
        "no_permission_or_cross_team_change",
        "no_schema_auth_security_privacy_or_data_access_change",
        "no_unresolved_founder_gate",
    )
    @classmethod
    def _declaration_must_affirm_no_known_gate(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("supersede attestation declarations must affirm no known gate")
        return value


class NextStep(BaseModel):
    """Decision returned by a task owner for what the orchestrator should do next."""
    action: Literal["delegate", "done", "escalate", "fanout", "parallel", "supersede"]
    agent: str | None = None
    prompt: str | None = None
    expect_verdict: str | None = None
    then: list[ChainLeg] = Field(default_factory=list)
    children: list[FanoutChild] = Field(default_factory=list)
    width_cap_ack: int | None = None
    # THR-078: when a fan-out owner re-delegates a failed slice, this field
    # carries the failed child's task id so the orchestrator can track
    # per-slice retry count from existing DB lineage (no schema migration).
    revisit_of_task_id: str | None = None
    # THR-109: direct delegate attachments. Keys must have been pre-uploaded
    # via `happyranch tasks attach-upload`; no ambient paths or upload-on-
    # decision route exists.
    attachments: list[TaskAttachmentRef] | None = None

    @field_validator('action')
    @classmethod
    def _normalize_parallel_alias(cls, v: str) -> str:
        """Accept ``parallel`` as an alias for ``fanout``.

        Downstream code always sees ``fanout`` after validation so no
        dispatch changes are needed."""
        if v == "parallel":
            return "fanout"
        return v
    join_summary: str | None = None
    summary: str | None = None
    reason: str | None = None
    successor_brief: str | None = None
    rationale: str | None = None
    attestation: ManagerSupersessionAttestation | None = None

    @model_validator(mode="before")
    @classmethod
    def _supersede_is_a_closed_payload(cls, value: object) -> object:
        """Keep the manager supersession decision deliberately non-extensible.

        The target and authority come only from the claimed task/session, never
        from a manager-supplied override.  Other decisions retain legacy
        permissive-extra parsing for wire compatibility.
        """
        if not isinstance(value, dict) or value.get("action") != "supersede":
            return value
        allowed = {"action", "successor_brief", "rationale", "attestation"}
        extra = set(value) - allowed
        if extra:
            raise ValueError(
                "supersede accepts only action, successor_brief, and rationale; "
                f"forbidden fields: {', '.join(sorted(extra))}"
            )
        for field_name in ("successor_brief", "rationale"):
            field_value = value.get(field_name)
            if not isinstance(field_value, str) or not field_value.strip():
                raise ValueError(f"supersede.{field_name} must be a nonblank string")
        if "attestation" not in value:
            raise ValueError("supersede.attestation is required")
        if value.get("attestation") is None:
            raise ValueError("supersede.attestation must not be null")
        return value


class LocalCiEvidence(BaseModel):
    """Evidence that a pushed PR's local CI ran successfully.

    Wire contract — every field is strict:
      - ``command`` MUST be the exact string "scripts/local_ci.sh all".
        Non-string values (including null) and any other string are rejected.
      - ``exit_code`` MUST be the exact integer 0.  Boolean (true/false) and
        string "0" are rejected by StrictInt — Pydantic v2 strict mode does
        NOT coerce them.
      - Extra keys (any third field beyond command + exit_code) are forbidden
        via model-level ``extra='forbid'``.
    """
    model_config = {"extra": "forbid"}

    command: StrictStr
    exit_code: StrictInt

    @field_validator("command")
    @classmethod
    def _command_must_be_all_target(cls, v: str) -> str:
        if v != "scripts/local_ci.sh all":
            raise ValueError(
                f"local_ci.command must be 'scripts/local_ci.sh all', got {v!r}"
            )
        return v

    @field_validator("exit_code")
    @classmethod
    def _exit_code_must_be_zero(cls, v: int) -> int:
        if v != 0:
            raise ValueError(
                f"local_ci.exit_code must be 0 for a pushed PR, got {v}"
            )
        return v


class CompletionReport(BaseModel):
    task_id: str
    agent: str
    status: str
    confidence: int = Field(ge=0, le=100)
    output_summary: str
    # Optional structured outcome for review/QA-type workers (APPROVE, PASS,
    # REQUEST_CHANGES, etc.). Free-string; per-team vocabulary lives in each
    # team's workflow KB entry. Used by inline delegation chains to gate
    # auto-advance.
    verdict: str | None = None
    # Task-owner-only: structured next-step decision. Subtask agents leave this None.
    # Separating the decision from the prose summary eliminates the
    # double-encoding trap where the manager's output_summary had to itself
    # be JSON (see TASK-071 post-mortem).
    decision: NextStep | None = None
    manager_self_evaluation: dict | None = None
    risks_flagged: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    suggested_reviewer_focus: list[str] = Field(default_factory=list)
    output_dir: str | None = None
    waiting_on_job_ids: list[str] = Field(default_factory=list)
    # Push-PR local CI evidence. Optional for non-PR completions;
    # contractually required for any pushed-PR report.
    local_ci: LocalCiEvidence | None = None


class ManagerSelfEvaluation(BaseModel):
    """Strict manager-only S6a result. No prose-bearing field is permitted."""
    model_config = {"extra": "forbid", "strict": True}

    contract_id: StrictStr
    contract_version: StrictStr
    contract_digest: StrictStr
    root_task_id: StrictStr
    manager_session_id: StrictStr
    release_id: StrictStr
    policy_version: StrictStr
    policy_digest: StrictStr
    activation_id: StrictStr
    activation_epoch: StrictInt
    provider_id: StrictStr
    executor_kind: StrictStr
    model_id: StrictStr
    disposition: Literal["escalate", "continue_same_root"]
    clause_id: StrictStr
    action: Literal["escalate_to_founder", "continue_same_root"]
    confidence: float = Field(strict=True, ge=0.0, le=1.0)
    uncertainty_codes: list[Literal[
        "low_confidence", "ambiguous", "missing_evidence",
        "conflicting_evidence", "novel",
    ]] = Field(default_factory=list)

    @field_validator("contract_digest", "policy_digest")
    @classmethod
    def _self_evaluation_digests(cls, value: str, info):
        return validate_authority_digest(value, f"self_evaluation.{info.field_name}")


class TaskStep(BaseModel):
    agent: str
    action: str
    description: str


class StepRecord(BaseModel):
    """Record of a completed orchestration step, shown to the task owner as history."""
    step_number: int
    agent: str
    action: str
    result_summary: str
    success: bool


class DreamStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class DreamRecord(BaseModel):
    id: str
    agent_name: str
    local_date: str
    scheduled_for: datetime
    window_end: datetime
    window_start: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: DreamStatus = DreamStatus.PENDING
    summary: str | None = None
    transcript_path: str | None = None
    new_learnings_count: int = 0
    kb_candidate_count: int = 0
    founder_thread_id: str | None = None
    session_id: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)


class WorkHourMode(StrEnum):
    WINDOWED = "windowed"
    CONTINUOUS = "continuous"


class WorkHourStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class WorkHourRecord(BaseModel):
    id: str
    agent_name: str
    local_date: str
    slot: str
    mode: WorkHourMode
    scheduled_for: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: WorkHourStatus = WorkHourStatus.PENDING
    routine_count: int = 0
    dropped_count: int = 0
    spawned_task_ids: list[str] = Field(default_factory=list)
    spawned_task_count: int = 0
    summary: str | None = None
    transcript_path: str | None = None
    session_id: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)


class DreamKbCandidate(BaseModel):
    id: int | None = None
    dream_id: str
    agent_name: str
    slug: str
    title: str
    topic: str
    rationale: str
    body_markdown: str
    status: Literal["pending", "promoted", "rejected", "superseded"] = "pending"
    promoted_kb_slug: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class TokenUsage(BaseModel):
    """Per-session token usage, unified across executors.

    All fields nullable so we can write a row even when parsing partially
    succeeds (per spec §4.3). `total` deliberately excludes cache reads —
    cache hits are an effectiveness signal, not new consumption.
    """
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None
    model: str | None = None
    usage_raw_json: str | None = None

    @property
    def total(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0) + (self.reasoning_tokens or 0)


class ThreadStatus(StrEnum):
    OPEN = "open"
    ARCHIVED = "archived"


class ThreadMessageKind(StrEnum):
    MESSAGE = "message"
    DECLINE = "decline"
    SYSTEM = "system"


class ThreadInvocationStatus(StrEnum):
    PENDING = "pending"
    CONSUMED = "consumed"
    DECLINED = "declined"
    TIMEOUT = "timeout"
    FAILED = "failed"


class ThreadInvocationPurpose(StrEnum):
    REPLY = "reply"
    BOOTSTRAP = "bootstrap"
    TASK_FOLLOWUP = "task_followup"


class ThreadRecord(BaseModel):
    id: str
    subject: str
    status: ThreadStatus = ThreadStatus.OPEN
    started_at: datetime = Field(default_factory=_now)
    archived_at: datetime | None = None
    forwarded_from_id: str | None = None
    forwarded_from_kind: str | None = None  # 'thread'
    turn_cap: int = 500
    turns_used: int = 0
    summary: str | None = None
    transcript_path: str | None = None
    composed_by: str = "founder"
    composed_from_task_id: str | None = None
    composed_from_dream_id: str | None = None
    last_speaker: str | None = None
    # Founder-workspace presentation state (THR-209): non-None when the thread
    # is pinned by the founder. Pinning changes display only — never identity,
    # participants, routing, unread state, lifecycle, or activity timestamps.
    pinned_at: datetime | None = None
    # Most recent message created_at (derived; NULL for threads without
    # messages). Informational on the wire (THR-209 msg 9: pinned ranking
    # uses the immutable numeric thread ID, not activity).
    last_activity_at: datetime | None = None


class ThreadParticipant(BaseModel):
    thread_id: str
    agent_name: str
    added_at: datetime = Field(default_factory=_now)
    added_by: str = "founder"


class ThreadAttachment(BaseModel):
    artifact_name: str
    display_name: str
    size_bytes: int | None = None
    content_type: str | None = None
    uploaded_by: str
    # Thread-scoped attachment id (mutually exclusive with artifact_name when non-None).
    # When set, the attachment is a thread-scoped file stored in the thread's
    # private attachment store rather than the org-shared ArtifactStore.
    thread_attachment_id: str | None = None


class ThreadScopedAttachment(BaseModel):
    """A file stored in a thread's private attachment store."""
    attachment_id: str
    thread_id: str
    display_name: str
    size_bytes: int | None = None
    content_type: str | None = None
    uploaded_by: str
    created_at: str = ""


class TaskAttachmentRecord(BaseModel):
    """A file attached to a task, stored in the private task-attachment store.

    Attachments are owned by the task they were uploaded to (owning ancestor).
    Descendant tasks resolve them via parent_task_id walk at spawn time.

    legacy_status is None for normal rows; non-None (e.g. 'duplicate_v1')
    marks rows that were preserved from a legacy pre-constraint schema where
    duplicate storage_key values were permitted. These rows are readable but
    their keys cannot be newly claimed.
    """
    id: int | None = None
    task_id: str
    ordinal: int
    storage_key: str
    display_name: str
    size_bytes: int | None = None
    content_type: str | None = None
    uploaded_by: str
    created_at: str = ""
    legacy_status: str | None = None


class ThreadMessage(BaseModel):
    id: int | None = None
    thread_id: str
    seq: int
    speaker: str
    kind: ThreadMessageKind
    body_markdown: str | None = None
    decline_reason: str | None = None
    system_payload: dict | None = None
    attachments: list[ThreadAttachment] = Field(default_factory=list)
    # Phase-2 mention routing (THR-198): canonical valid-participant mentions
    # derived server-side from body_markdown at write time (stored as
    # mentions_json). Empty list when derived with no valid mentions; NULL
    # rows (system/decline + pre-change history) read back as empty list.
    mentions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


class ResponderStatusEntry(BaseModel):
    agent_name: str
    purpose: ThreadInvocationPurpose
    status: Literal["queued", "working", "replied", "declined", "failed"]
    responded_at: str | None
    started_at: str | None = None
    decline_reason: str | None = None
    category: Literal["declined", "no_callback", "no_callback_after_reprompt", "infra_fail"] | None = None


class ThreadInvocation(BaseModel):
    id: int | None = None
    thread_id: str
    agent_name: str
    invocation_token: str
    triggering_seq: int
    purpose: ThreadInvocationPurpose
    status: ThreadInvocationStatus = ThreadInvocationStatus.PENDING
    enqueued_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    consumed_at: datetime | None = None
    session_id: str | None = None
    dispatched_task_id: str | None = None
    decline_reason: str | None = None


class ThreadReplyDeliveryState(BaseModel):
    """Durable per-(thread_id, agent_name) conversational REPLY delivery state.

    GitHub #688 Phase 1 Slice A. This is the provider-neutral required-delivery
    contract for coalesced conversational reply wakes. One row per pair:
    at most one queued and one running REPLY slot; both tokens (when present)
    reference same-pair REPLY invocations.

    ``acknowledged_through_seq`` is the highest transcript sequence
    intentionally acknowledged as presented/handled; ``required_through_seq``
    is the highest conversational sequence that must still be offered. The
    queued/running tokens occupy the single unstarted/started REPLY slots.
    ``running_from_seq``/``running_through_seq`` are the immutable prompt
    receipt captured at claim time.

    ``last_terminal_reason``/``last_terminal_at`` carry the diagnostic data
    Slice B uses to project ``retry_required`` and to audit settlement.
    """
    thread_id: str
    agent_name: str
    acknowledged_through_seq: int = 0
    required_through_seq: int = 0
    queued_invocation_token: str | None = None
    running_invocation_token: str | None = None
    running_from_seq: int | None = None
    running_through_seq: int | None = None
    last_terminal_reason: str | None = None
    last_terminal_at: str | None = None
    updated_at: str = ""


class ThreadReplyBreakerEpisode(BaseModel):
    """Durable provider-failure breaker for one thread/agent/executor identity."""
    thread_id: str
    agent_name: str
    executor_key: str
    episode_id: str
    state: Literal["closed", "open", "probe"]
    consecutive_failures: int = Field(ge=0)
    opened_at: str | None = None
    cooldown_until: str | None = None
    probe_lease_id: str | None = None
    last_failure_category: str | None = None
    updated_at: str


class ThreadReplyRecoveryEntry(BaseModel):
    """One runnable token returned by the durable reply-delivery recovery pass.

    ``kind`` distinguishes a retained queued wake from a replacement queued
    wake minted for an interrupted running attempt.
    """
    thread_id: str
    agent_name: str
    invocation_token: str
    kind: Literal[
        "retained_queued", "replacement_queued", "deferred_catchup",
        "breaker_probe",
    ]


class ThreadReplyArrival(BaseModel):
    """One recipient's delivery result from a conversational arrival.

    GitHub #688 Phase 1 Slice B. ``invocation_token`` is non-None only when
    this arrival minted a NEW queued REPLY (no queued/running ownership
    existed); ``coalesced=True`` means the arrival merely raised
    ``required_through_seq`` on an existing wake. ``from_seq``/``through_seq``
    carry the delivery range the pair's single wake now covers (diagnostic).
    """
    agent_name: str
    invocation_token: str | None
    coalesced: bool
    from_seq: int
    through_seq: int


class ThreadReplyClaim(BaseModel):
    """Successful queued→running CAS result for a conversational REPLY.

    ``running_from_seq``/``running_through_seq`` are the immutable inclusive
    prompt receipt snapshotted at claim time; they never change for the life
    of the running attempt even if later arrivals raise ``required_through_seq``.
    """
    thread_id: str
    agent_name: str
    invocation_token: str
    acknowledged_through_seq: int
    required_through_seq: int
    running_from_seq: int
    running_through_seq: int


class ThreadReplySettlement(BaseModel):
    """Result of settling a conversational REPLY terminal path.

    ``follow_on_token`` is at most one newly-minted queued REPLY covering
    arrivals strictly after the immutable running range — the reply/decline
    follow-on, or the owed post-slot catch-up of a released deferral whose
    old slot never covered the released range (TASK-6057: minted on
    failed/timeout too, never a plain immediate retry).
    ``retry_required`` is the residual-obligation diagnostic: True only when
    ``required_through_seq`` still exceeds the acknowledged watermark AND no
    follow-on wake was minted to carry it (failure/timeout without an owed
    catch-up).

    TASK-5966 strict mention-led exchange: ``exchange_held`` is True when the
    pair was a held (deferred) member of an open exchange and its follow-on
    wake was therefore SUPPRESSED — the unacknowledged range is intentionally
    deferred to the exchange's single range-covering catch-up at closure, and
    ``retry_required`` stays False (this is not a retry condition).
    """
    thread_id: str
    agent_name: str
    outcome: Literal["reply", "decline", "failed", "timeout"]
    acknowledged_through_seq: int
    required_through_seq: int
    retry_required: bool
    follow_on_token: str | None
    exchange_held: bool = False


class ThreadReplyExchangeProjection(BaseModel):
    """Exchange-level wire projection for a thread (server contract,
    TASK-5966). Derived from ``thread_reply_exchange`` only — never
    fabricated. The most recent exchange row (any state) is returned so the
    UI/CLI can truthfully disclose an open exchange's bounds and deferred
    set; a thread that never opened an exchange has none.
    """
    thread_id: str
    exchange_id: int
    state: Literal["open", "released", "suppressed"]
    open_seq: int
    close_seq: int
    opened_at: str
    last_activity_at: str
    closed_at: str | None
    close_reason: str | None
    deferred_count: int


class ReplyDeliveryProjection(BaseModel):
    """Pair-level reply-delivery wire projection (server contract, Slice B).

    Derived from ``thread_reply_delivery_state``, never fabricated from
    per-message invocation rows. ``state`` truthfully distinguishes the four
    live obligations:
      * ``queued`` — one unstarted coalesced REPLY wake (token set, not started)
      * ``running`` — one claimed in-flight REPLY (immutable range)
      * ``held`` — an unacknowledged range intentionally deferred by both an
        OPEN reply exchange and this participant's matching HELD deferral row
      * ``retry_required`` — unacknowledged range with no active wake; the
        next conversational arrival mints the single covering retry
    A fully-settled pair (nothing queued/running/required) is omitted from the
    live projection; terminal history stays on the per-message responder strips.
    ``coalesced_message_count`` is the number of transcript rows the wake's
    range covers (computed in the store, not inferred by numeric subtraction).
    """
    agent_name: str
    state: Literal["queued", "running", "held", "retry_required"]
    from_seq: int
    through_seq: int
    coalesced_message_count: int
    started_at: str | None
    updated_at: str | None
    last_terminal_reason: str | None
    current_failure_category: Literal[
        "no_callback", "no_callback_after_reprompt", "infra_fail"
    ] | None = None


class JobStatus(StrEnum):
    PENDING   = "pending"
    REJECTED  = "rejected"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"


class JobInterpreter(StrEnum):
    BASH    = "bash"
    SH      = "sh"
    ZSH     = "zsh"
    PYTHON3 = "python3"


class JobRecord(BaseModel):
    id:               str
    # Scope id of the submission context. Always a TASK-NNN id for task-originated
    # jobs. Keeping one column avoids plumbing a ``scope_id`` everywhere it's
    # already in use.
    task_id:          str
    agent_name:       str
    title:            str
    rationale:        str
    script_text:      str
    interpreter:      JobInterpreter
    cwd_hint:         str | None = None
    status:           JobStatus = JobStatus.PENDING
    exit_code:        int | None = None
    stdout_head:      str | None = None
    stderr_head:      str | None = None
    stdout_path:      str | None = None
    stderr_path:      str | None = None
    duration_ms:      int | None = None
    started_at:       str | None = None
    finished_at:      str | None = None
    reviewed_at:      str | None = None
    reviewed_by:      str | None = None
    reject_reason:    str | None = None
    cwd_resolved:     str | None = None
    max_runtime_seconds: int | None = None
    # Per-stream output-size cap (bytes). Either stdout OR stderr crossing
    # this triggers SIGKILL with reason="output_cap". 50 MiB default matches
    # the column default in the jobs table schema.
    max_output_bytes: int | None = 52428800
    # Founder-review gate. True → row inserted as `pending`, awaits explicit
    # /run. False (default) → auto-run inline at /submit.
    review_required:  bool = False
    # Long-running flag. True → no default runtime cap (unbounded unless an
    # explicit max_runtime_seconds is provided), killed only by /stop or the
    # task-terminal kill hook. False (default) → 300s default cap when no
    # explicit override is provided.
    persistent:       bool = False
    # Terminal-status reason — populated by the runner when status='failed'.
    # Examples: "timeout", "output_cap", "founder_stop", "agent_stop",
    # "task_ended", "spawn_failed", "internal_error", "daemon_crash".
    # NULL when status='completed' or the job hasn't reached terminal yet.
    reason:           str | None = None
    created_at:       str


class ScheduleKind(StrEnum):
    """THR-105 schedule cadence kinds."""
    ONE_SHOT = "one_shot"
    WEEKLY = "weekly"
    RECURRING = "recurring"


class ScheduleStatus(StrEnum):
    """THR-105: Schedule lifecycle states.  armed → firing → fired (one-shot terminal)
    or armed → firing → armed (weekly cycle).  paused / cancelled / expired / failed /
    timeout are terminal or suspended states."""
    ARMED = "armed"
    FIRING = "firing"
    FIRED = "fired"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    TIMEOUT = "timeout"


class ScheduleRecord(BaseModel):
    """THR-105 Phase 1: a persisted, agent-owned scheduled work commitment.

    Internal primitive is ``Schedule``; user-facing label is "Todos".
    See docs/superpowers/specs/2026-07-18-agent-scheduled-work-design.md
    and docs/product/prds/2026-07-19-agent-todos.md.
    """
    id: str
    agent_name: str
    team: str = "engineering"
    kind: ScheduleKind
    fire_at: datetime
    recurrence: dict | None = None
    timezone: str = "UTC"
    normalized_brief: str
    source_instruction: str
    status: ScheduleStatus = ScheduleStatus.ARMED
    active: int = 1
    expires_at: datetime | None = None
    indefinite: int = 0
    spawned_task_ids: list[str] = Field(default_factory=list)
    last_fired_at: datetime | None = None
    fire_count: int = 0
    end_reason: str | None = None
    # Fields needed by the later runner (Phase 2+), consistent with WorkHourRecord
    session_id: str | None = None
    error: str | None = None
    transcript_path: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ── THR-181 Track A: durable authority candidate/evaluation/audit foundation ──
#
# Slice 1 supplied the isolated, additive persistence foundation; the
# pre-escalation authority hook (runtime/orchestrator/authority.py) now runs
# one audited LLM evaluation before a manager root's proposed escalation is
# committed (Engineering policy v1), consuming these records via the
# database.py persistence API. The vocabularies below are closed StrEnums
# mirrored by SQLite CHECK constraints. Every prose-bearing field is stored
# as a *digest* (never the raw content); raw bearer/provider credentials,
# task prose, and unredacted model exchanges are never accepted or
# persisted.


class AuthorityLifecycleState(StrEnum):
    """Controlled candidate lifecycle. Mirrored by a SQLite CHECK constraint."""
    CREATED = "created"          # claimed, not yet evaluated
    EVALUATED = "evaluated"      # a single evaluation disposition recorded
    CONSUMED = "consumed"        # disposition consumed exactly once (hook CAS)


class AuthorityDisposition(StrEnum):
    """The primary controlled outcome recorded by the (later) evaluator slice."""
    CONTINUE_SAME_ROOT = "continue_same_root"
    ESCALATE = "escalate"
    NOT_APPLICABLE = "not_applicable"
    EVALUATOR_ERROR = "evaluator_error"


class AuthorityDispositionCode(StrEnum):
    """Fine-grained fail-closed reason codes. Superset of AuthorityDisposition."""
    CONTINUE_SAME_ROOT = "continue_same_root"
    ESCALATE = "escalate"
    NOT_APPLICABLE = "not_applicable"
    EVALUATOR_ERROR = "evaluator_error"
    LOW_CONFIDENCE = "low_confidence"
    TIMEOUT = "timeout"
    MALFORMED_OUTPUT = "malformed_output"
    INJECTION_GUARD = "injection_guard"
    AUDIT_FAILURE = "audit_failure"


class AuthorityRetentionClass(StrEnum):
    """How long a snapshot/response digest is retained. Mirrored by CHECK."""
    DIGEST_ONLY = "digest_only"
    SHADOW = "shadow"
    INDEFINITE = "indefinite"


class AuthorityRedactionClass(StrEnum):
    """Whether content was redacted before it was digested. Mirrored by CHECK."""
    NONE = "none"
    REDACTED = "redacted"


class AuthorityAuditEventType(StrEnum):
    """Controlled append-only authority audit event vocabulary."""
    CANDIDATE_CLAIMED = "candidate_claimed"
    CANDIDATE_CLAIM_LOST = "candidate_claim_lost"
    EVALUATION_RECORDED = "evaluation_recorded"
    CANDIDATE_CONSUMED = "candidate_consumed"


# Bounded digest/version validators. Shared by the closed Pydantic records
# below AND the ``Database`` writer boundary (database.py) so the two surfaces
# cannot drift. They reject task prose, raw model exchanges, and bearer/provider
# credentials smuggled into a field that must only ever hold a hex digest or a
# short version token — the writer refuses to persist (never silently redacts).

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

_CREDENTIAL_MARKERS = (
    "bearer",
    "authorization",
    "api_key",
    "apikey",
    "password",
    "credential",
    "token=",
    "sk-",
    "private_key",
    "client_secret",
)


def _reject_credential_like(value: str, field: str) -> str:
    lowered = value.lower()
    for marker in _CREDENTIAL_MARKERS:
        if marker in lowered:
            raise ValueError(
                f"{field} appears to carry a credential-like token ({marker!r}); "
                "refusing to persist it"
            )
    return value


def validate_authority_digest(value: str, field: str) -> str:
    """Validate a digest field: bounded hex only.

    Rejects task prose, raw model exchanges (JSON), and bearer/provider
    credentials — none of which are valid hex — rather than silently storing
    or redacting them.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if not (32 <= len(value) <= 128) or not set(value) <= _HEX_DIGITS:
        raise ValueError(
            f"{field} must be a bounded hex digest (32-128 hex chars); "
            "refusing to persist prose, credentials, or raw model-exchange content"
        )
    return value


def validate_authority_version(value: str, field: str) -> str:
    """Validate a version token: short, non-blank, credential-free."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-blank string")
    if len(value) > 64:
        raise ValueError(f"{field} must be at most 64 characters")
    return _reject_credential_like(value, field)


class AuthorityFenceCode(StrEnum):
    """Closed vocabulary of mechanical fence outcome codes.

    Only these codes may be recorded in an ``AuthorityFenceResult``. Unknown
    codes are rejected at the writer boundary (and by the model's closed
    validation) rather than persisted.
    """
    CANCELLED = "cancelled"
    STALE = "stale"
    BUDGET_EXCEEDED = "budget_exceeded"
    TIMEOUT = "timeout"
    INJECTION = "injection"
    MALFORMED = "malformed"


class AuthorityFenceResult(BaseModel):
    """One mechanical fence outcome: a boolean plus an optional closed code.

    Structured, never prose — the fence name is the dict key, and this model
    is the value. Extra keys and unknown codes are rejected; evaluator
    prose/attestation text must never be stored here.
    """
    model_config = {"extra": "forbid"}

    passed: StrictBool
    code: AuthorityFenceCode | None = None


class AuthorityAuditPayload(BaseModel):
    """Bounded, closed audit-event payload.

    Only digest / redaction / version / classification fields are permitted.
    Never prose, credentials, raw model exchanges, or arbitrary evaluator
    responses. Unknown keys are rejected.
    """
    model_config = {"extra": "forbid"}

    disposition: AuthorityDisposition | None = None
    disposition_code: AuthorityDispositionCode | None = None
    retention_class: AuthorityRetentionClass | None = None
    redaction_class: AuthorityRedactionClass | None = None
    digest: StrictStr | None = None
    version: StrictStr | None = None

    @field_validator("digest")
    @classmethod
    def _digest_is_bounded_hex(cls, value, info):
        if value is None:
            return None
        return validate_authority_digest(value, f"payload.{info.field_name}")

    @field_validator("version")
    @classmethod
    def _version_is_bounded(cls, value, info):
        if value is None:
            return None
        return validate_authority_version(value, f"payload.{info.field_name}")


class AuthorityCandidate(BaseModel):
    """Immutable identity of one pre-escalation authority candidate.

    ``claim_key`` is the deterministic sha256 digest of the
    root/session/causal-event/policy-prompt-model tuple — the CAS key that
    guarantees at most one durable candidate per tuple. Every digest field is
    validated as bounded hex; unknown keys are rejected.
    """
    model_config = {"extra": "forbid"}

    id: str
    claim_key: str
    root_task_id: str
    team: str
    manager_agent: str
    manager_session_id: str
    causal_event_id: str
    causal_event_digest: str
    causal_result_id: str | None = None
    policy_id: str
    policy_version: str
    policy_digest: str
    prompt_id: str
    prompt_version: str
    prompt_digest: str
    model_id: str
    model_version: str
    model_digest: str
    snapshot_digest: str
    snapshot_retention_class: AuthorityRetentionClass = AuthorityRetentionClass.DIGEST_ONLY
    snapshot_redaction_class: AuthorityRedactionClass = AuthorityRedactionClass.REDACTED
    fence_results: dict[str, AuthorityFenceResult] | None = None
    disposition: AuthorityDisposition | None = None
    lifecycle_state: AuthorityLifecycleState = AuthorityLifecycleState.CREATED
    consumed_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator(
        "claim_key",
        "causal_event_digest",
        "policy_digest",
        "prompt_digest",
        "model_digest",
        "snapshot_digest",
    )
    @classmethod
    def _digests_are_bounded_hex(cls, value, info):
        return validate_authority_digest(value, info.field_name)

    @field_validator("policy_version", "prompt_version", "model_version")
    @classmethod
    def _versions_are_bounded(cls, value, info):
        return validate_authority_version(value, info.field_name)


class AuthorityPolicyRelease(BaseModel):
    """One immutable authored team-policy release.

    The digest covers exactly policy_id, version, team, title,
    normative_text, clauses, and continuation_phrase. Release ancestry,
    actor attribution, and creation time are receipts and are deliberately
    outside that semantic identity.
    """
    model_config = {"extra": "forbid", "frozen": True}

    id: str
    team: str
    policy_id: str
    version: int = Field(gt=0)
    title: str
    normative_text: str
    clauses_json: str
    continuation_phrase: str
    canonical_payload_json: str
    policy_digest: str
    based_on_release_id: str | None = None
    actor_kind: Literal["shared_local_operator_credential"]
    created_at: datetime = Field(default_factory=_now)

    @model_validator(mode="before")
    @classmethod
    def _derive_and_validate_semantic_identity(cls, raw):
        if not isinstance(raw, dict):
            return raw
        values = dict(raw)
        try:
            clauses = json.loads(values["clauses_json"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("clauses_json must be canonical JSON") from exc
        if not isinstance(clauses, list):
            raise ValueError("clauses_json must be a JSON array")
        clause_keys = {"id", "category", "condition", "action"}
        seen: set[str] = set()
        for clause in clauses:
            if not isinstance(clause, dict) or set(clause) != clause_keys:
                raise ValueError("each policy clause must have the exact closed schema")
            if any(not isinstance(clause[key], str) or not clause[key].strip() for key in clause_keys):
                raise ValueError("policy clause fields must be nonblank strings")
            if clause["action"] not in {"escalate_to_founder", "continue_same_root"}:
                raise ValueError("policy clause action is outside the closed vocabulary")
            if clause["id"] in seen:
                raise ValueError("policy clause ids must be unique")
            seen.add(clause["id"])
        canonical_clauses = json.dumps(
            clauses, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        if values["clauses_json"] != canonical_clauses:
            raise ValueError("clauses_json is not canonical")
        payload = {
            "policy_id": values.get("policy_id"), "version": values.get("version"),
            "team": values.get("team"), "title": values.get("title"),
            "normative_text": values.get("normative_text"), "clauses": clauses,
            "continuation_phrase": values.get("continuation_phrase"),
        }
        canonical_payload = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        expected_id = f"APR-{digest}"
        for field, expected in (
            ("canonical_payload_json", canonical_payload),
            ("policy_digest", digest), ("id", expected_id),
        ):
            supplied = values.get(field)
            if supplied is not None and supplied != expected:
                raise ValueError(f"{field} does not match canonical policy semantics")
            values[field] = expected
        return values

    @field_validator("policy_digest")
    @classmethod
    def _policy_digest_is_bounded_hex(cls, value, info):
        return validate_authority_digest(value, info.field_name)


class _AuthorityPolicyActivationDraft(BaseModel):
    """Trusted construction input; never accepted as a persisted receipt."""
    model_config = {"extra": "forbid", "frozen": True}

    id: str
    team: str
    epoch: int = Field(gt=0)
    release_id: str
    previous_activation_id: str | None = None
    expected_previous_epoch: int | None = Field(default=None, ge=0)
    action: Literal["activate", "reactivate_rollback", "bootstrap"]
    actor_kind: Literal["shared_local_operator_credential"]
    request_id: str
    request_digest: str
    created_at: datetime = Field(default_factory=_now)

    @field_validator("request_digest")
    @classmethod
    def _request_digest_is_bounded_hex(cls, value, info):
        return validate_authority_digest(value, info.field_name)


def _authority_activation_digest(values: dict[str, object]) -> str:
    """Derive a seal from an already validated closed canonical payload."""
    payload = {
        field: values[field]
        for field in (
            "id", "team", "epoch", "release_id", "previous_activation_id",
            "expected_previous_epoch", "action", "actor_kind", "request_id",
            "request_digest", "created_at",
        )
    }
    if isinstance(payload["created_at"], datetime):
        payload["created_at"] = payload["created_at"].isoformat()
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuthorityPolicyActivation(_AuthorityPolicyActivationDraft):
    """Immutable receipt whose mandatory seal is never derived at ingress."""

    activation_digest: str

    @classmethod
    def create(cls, **values: object) -> AuthorityPolicyActivation:
        """Trusted fresh-activation factory; the sole seal-derivation boundary."""
        draft = _AuthorityPolicyActivationDraft.model_validate(values)
        snapshot = draft.model_dump(mode="python", round_trip=True, warnings=False)
        return cls.model_validate({
            **snapshot,
            "activation_digest": _authority_activation_digest(snapshot),
        })

    @model_validator(mode="after")
    def _validate_activation_digest(self) -> AuthorityPolicyActivation:
        snapshot = self.model_dump(
            mode="python", exclude={"activation_digest"}, round_trip=True,
            warnings=False,
        )
        if self.activation_digest != _authority_activation_digest(snapshot):
            raise ValueError("activation_digest does not match canonical activation semantics")
        return self

    @field_validator("activation_digest")
    @classmethod
    def _activation_digest_is_bounded_hex(cls, value, info):
        return validate_authority_digest(value, info.field_name)


class AuthorityCandidatePolicyPin(BaseModel):
    """Immutable one-to-one release/activation identity for a DB-policy candidate."""
    model_config = {"extra": "forbid", "frozen": True}

    candidate_id: str
    release_id: str
    activation_id: str
    activation_epoch: int = Field(gt=0)
    provider_id: str
    executor_kind: str
    created_at: datetime = Field(default_factory=_now)


class AuthorityEvaluation(BaseModel):
    """The single, immutable evaluation outcome for a candidate.

    Stores the *digest* of the evaluator response plus a controlled
    disposition/code — never the raw response text or unredacted exchange.
    """
    model_config = {"extra": "forbid"}

    id: int | None = None
    candidate_id: str
    disposition: AuthorityDisposition
    disposition_code: AuthorityDispositionCode
    response_digest: str
    response_retention_class: AuthorityRetentionClass = AuthorityRetentionClass.DIGEST_ONLY
    response_redaction_class: AuthorityRedactionClass = AuthorityRedactionClass.REDACTED
    fence_results: dict[str, AuthorityFenceResult] | None = None
    created_at: datetime = Field(default_factory=_now)

    @field_validator("response_digest")
    @classmethod
    def _response_digest_is_bounded_hex(cls, value, info):
        return validate_authority_digest(value, info.field_name)


class AuthorityAuditEvent(BaseModel):
    """One append-only authority audit event. Immutable after write."""
    model_config = {"extra": "forbid"}

    id: int | None = None
    candidate_id: str
    event_type: AuthorityAuditEventType
    payload: AuthorityAuditPayload | None = None
    created_at: datetime = Field(default_factory=_now)
