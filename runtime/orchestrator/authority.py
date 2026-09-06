"""THR-181 Track A — pre-escalation authority evaluator seam + hook.

This module wires the release-controlled policy (``authority_policy.py``)
into the orchestrator's manager-root escalation commit point. It defines:

* the immutable per-attempt input snapshot;
* the closed output schema the evaluator must produce;
* the authenticated manager-completion self-evaluation parser, plus the
  deterministic injectable evaluator seam retained for strict tests;
* ``run_authority_hook`` — the single hook the orchestrator calls before a
  manager root's proposed escalation is committed. It returns
  ``"continue_same_root"`` (the named same-root permitted action was executed)
  or ``"escalate"`` (fail closed: the existing escalation path proceeds).

Invariants (THR-181 / KB escalation-bounded-self-resume-ruling):

* Exactly ONE audited evaluation per eligible attempt; the DB is the
  single-evaluation and exactly-once-consumption guard (UNIQUE candidate_id
  on ``authority_evaluations``, ``created -> evaluated -> consumed`` CAS).
* The proposed escalation reason is UNTRUSTED input: only its digest is ever
  persisted; the raw reason is passed to the evaluator and never stored.
* Audit failure cannot permit continuation: every audit event and the
  outcome row are written BEFORE (or atomically WITH) the continuation; any
  audit failure fails closed to ESCALATE.
* Every hook-eligible attempt records exactly one ``authority_hook``
  audit_log outcome row (the denominator), or fails closed so that no
  continuation can occur even when recording is impossible.
* Server-owned mechanical fences are non-overridable by any policy output.
* Structured SERVER-derived facts (budget counters/ceilings, lineage, active
  work, cancellation/block/session state, adverse child review verdicts,
  zombie/partial-work evidence, org permission digest, DB schema digest) are
  captured with provenance into the evaluation snapshot; a server-PROVEN
  must-escalate fact (adverse child verdict, partial-work evidence, DB-schema
  drift vs the release-pinned schema) forces ESCALATE regardless of what the
  untrusted reason prose claims, and a protected-surface change during the
  attempt (org permission digest or live schema digest drift) fails closed
  with the matched clause — neither a misleading nor an omitted reason can
  authorize CONTINUE_SAME_ROOT.
* CONTINUE_SAME_ROOT is granted ONLY when the proposed reason is a BYTE-EXACT
  member of the release-controlled closed routine set
  (``CONTINUE_ACCEPTED_REASONS``) AND every server-derived predicate is
  clean: the server then has complete knowledge of the prose, so the grant
  never depends on keyword classification or the completeness/truthfulness
  of untrusted reason prose for any protected boundary. Any other reason —
  including a semantically similar paraphrase — is not verifiable as routine
  and fails closed to ESCALATE. The exact narrow permitted action (return the
  current root to pending + re-enqueue) is additionally server-proven safe
  across every protected category (fixed audited transaction; no schema/
  permission/auth/compatibility/destructive/external side effects; spend
  bounded by the budget fence; reversible and re-escalatable).
* The single-use continuation lifecycle envelope: every CONTINUE_SAME_ROOT commit
  atomically mints a single-use ``authority_continue_envelopes`` row
  (bound to the evaluation/candidate, the immutable causal task-result
  row, the matched policy clause, and the same-root grant;
  ``active -> consumed | violated`` exactly-once with a DB-enforced finite
  lifecycle). The daemon-mediated completion point consumes it for the next
  normally validated manager result; it is not an exact-action whitelist.
  Supersession/fresh-root replacement remains outside the same-root grant.
  Cancellation/session-failure/restart windows spend the envelope
  fail-closed so the continuation is never re-used. A fixed phrase or
  untrusted reason truthfulness is NEVER safety proof on its own — the
  lifecycle envelope remains an identity, replay, and audit fence. Continued
  turns use the manager agent's ordinary configured executor permissions.
* Thread id/origin remain structured lineage provenance and are neither an
  initial eligibility fence nor a final continuation-CAS rejection. They do
  not relax the same-root or no-revisit/no-successor rules.
* The final continuation CAS atomically re-validates the COMPLETE current
  fence set at consumption time (candidate/policy/input identity, manager
  ownership and session, exact team, root status, cancellation, block/
  active-work, revisit/successor lineage, budgets, zombie/partial-
  work, adverse child verdicts) inside the same transaction as the
  continuation + audit rows.
* Candidate identity is derived from the IMMUTABLE task-result/session
  causality (the persisted ``task_results`` row id), never from a freshly
  written orchestration-step audit id — so real restart/recovery re-entry
  cannot mint a second candidate or evaluation.
* Historical census eligibility is NEVER consulted; reachability depends only
  on a release-controlled policy for the team and a current manager-owned
  root.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, Field, field_validator

from runtime.infrastructure.database import _authority_claim_key
from runtime.models import (
    AuthorityDisposition,
    AuthorityDispositionCode,
    AuthorityFenceResult,
    TaskStatus,
    ManagerSelfEvaluation,
    validate_authority_digest,
    validate_authority_version,
)
from runtime.orchestrator.authority_policy import (
    ACTION_CONTINUE_SAME_ROOT,
    ACTION_ESCALATE_TO_FOUNDER,
    CLOSED_ACTIONS,
    CONTINUE_ACCEPTED_REASONS,
    PROMPT_DIGEST,
    PROMPT_ID,
    PROMPT_VERSION,
    POLICY_BY_TEAM,
    AuthorityPolicy,
    build_authority_evaluation_prompt,
)

if TYPE_CHECKING:
    from runtime.models import TaskRecord
    from runtime.orchestrator.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

# Outcome vocabulary of the ``authority_hook`` audit_log record. Exactly one
# such record is written per hook-eligible attempt (the denominator).
OUTCOME_CONTINUED_SAME_ROOT = "continued_same_root"
OUTCOME_ESCALATED = "escalated"
OUTCOME_INELIGIBLE = "ineligible"
OUTCOME_CAS_LOST = "cas_lost"
OUTCOME_CANCELLED_STALE = "cancelled_stale"
OUTCOME_EVALUATOR_FAILURE = "evaluator_failure"
OUTCOME_AUDIT_FAILURE = "audit_failure"
OUTCOME_CAPTURE_FAILURE = "capture_failure"

AUDIT_ACTION_HOOK_OUTCOME = "authority_hook"
AUDIT_ACTION_CONTINUED_SAME_ROOT = "authority_continued_same_root"
AUDIT_ACTION_ENVELOPE_CONSUMED = "authority_continue_envelope_consumed"
AUDIT_ACTION_ENVELOPE_VIOLATED = "authority_continue_envelope_violated"

# Production evaluator bounds. A bounded invocation is part of the fail-closed
# contract: a hang must surface as a timeout disposition, never a stall.
DEFAULT_EVALUATOR_TIMEOUT_SECONDS = 60.0
DEFAULT_EXECUTOR_KIND = "pi"

# Minimum confidence required for a CONTINUE_SAME_ROOT verdict; anything
# below fails closed to ESCALATE (LOW_CONFIDENCE).
CONTINUE_MIN_CONFIDENCE = 0.5

# Diagnostic fail-closed disposition codes that are preserved verbatim into
# the recorded verdict (audit fidelity for uncertainty/error codes); anything
# else on a fail-closed verdict normalizes to the code of its own branch.
_DIAGNOSTIC_FAILURE_CODES = frozenset({
    AuthorityDispositionCode.TIMEOUT,
    AuthorityDispositionCode.MALFORMED_OUTPUT,
    AuthorityDispositionCode.INJECTION_GUARD,
    AuthorityDispositionCode.LOW_CONFIDENCE,
    AuthorityDispositionCode.EVALUATOR_ERROR,
    AuthorityDispositionCode.AUDIT_FAILURE,
})

# Closed uncertainty-code vocabulary (mirrored in the policy prompt).
CLOSED_UNCERTAINTY_CODES = frozenset({
    "low_confidence", "ambiguous", "missing_evidence",
    "conflicting_evidence", "novel",
})

_CREDENTIAL_MARKERS = (
    "bearer",
    "authorization",
    "api_key",
    "apikey",
    "password",
    "credential=",
    "credential:",
    "token=",
    "sk-",
    "private_key",
    "client_secret",
)

_TERMINAL_STATUSES = frozenset({
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.SUPERSEDED.value,
    TaskStatus.CANCELLED.value,
})


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Verified-approve verdict vocabulary used by the adverse-review server fact.
_APPROVED_VERDICTS = frozenset({"APPROVE", "PASS"})


# The release-expected DB schema digest: the schema a FRESH Database() built
# from the CURRENT code creates. Any divergence of the live DB from this
# release schema is an authoritative schema/migration drift signal — the
# surface the continuation would operate on is not the reviewed release
# surface, so a schema/migration condition is in flight and the attempt must
# escalate. Computed once per process and cached.
_release_schema_digest_cache: str | None = None


def _release_schema_digest() -> str:
    """Digest of the sqlite_master DDL a fresh Database() creates with the
    current code (the release-pinned schema surface). Cached after first
    computation; never raises (returns "unavailable" on any defect, which
    fails closed as a drift signal)."""
    global _release_schema_digest_cache
    if _release_schema_digest_cache is not None:
        return _release_schema_digest_cache
    try:
        import tempfile
        from pathlib import Path as _Path
        from runtime.infrastructure.database import Database
        with tempfile.TemporaryDirectory() as td:
            fresh = Database(_Path(td) / "fresh-authority-schema.db")
            try:
                _release_schema_digest_cache = _live_schema_digest(fresh)
            finally:
                try:
                    fresh._conn.close()
                except Exception:
                    pass
    except Exception:
        _release_schema_digest_cache = "unavailable"
    return _release_schema_digest_cache


def _live_schema_digest(db) -> str:
    """Digest of the live DB's sqlite_master DDL (same canonical query the
    release digest uses, so the two are directly comparable)."""
    try:
        rows = db.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
        ).fetchall()
        return _sha256("\n".join(str(r[0]) for r in rows))
    except Exception:
        return "unavailable"


def _permission_digest(orch: "Orchestrator", agent: str) -> str:
    """Digest of the current org permission surface (org_config + the active
    agent definition). ``orch`` may be None in unit contexts — the digest
    then degrades to "unavailable" (a change is then treated as drift,
    failing closed)."""
    if orch is None:
        return "unavailable"
    parts: list[str] = []
    try:
        from runtime.orchestrator.org_config import load_org_config
        parts.append(load_org_config(orch._paths).model_dump_json(sort_keys=True))
    except Exception:
        parts.append("org_config:unavailable")
    try:
        from runtime.orchestrator.prompt_loader import load_agent
        ad = load_agent(orch._paths, agent)
        if ad is not None:
            parts.append(
                json.dumps(
                    {"name": ad.name, "allow_rules": sorted(ad.allow_rules)},
                    sort_keys=True,
                )
            )
        else:
            parts.append("agent_def:unavailable")
    except Exception:
        parts.append("agent_def:unavailable")
    return _sha256("\x1f".join(parts))


def _is_accepted_routine_reason(reason: str) -> bool:
    """Server predicate over the prose: the proposed reason is a BYTE-EXACT
    member of the release-controlled closed routine set. The server then has
    complete knowledge of the reason's content, so a CONTINUE grant never
    depends on keyword classification, completeness, or truthfulness of
    untrusted prose. Any other reason is not verifiable as routine and fails
    closed to ESCALATE."""
    return reason in CONTINUE_ACCEPTED_REASONS


def _during_attempt_drift_clause(
    orch: "Orchestrator",
    agent: str,
    snapshot: "AuthorityInputSnapshot",
) -> str | None:
    """Authoritative server predicate at the post-evaluation recheck: if the
    org permission surface or the live DB schema digest captured in the
    snapshot changed while the evaluator ran, a permission/sandbox/allow-rule
    or schema condition is in flight and the attempt fails closed with the
    matched clause. A read defect fails closed too (digest unknown)."""
    facts = snapshot.structured_facts
    try:
        perm = json.loads(facts.get("org_permission", "{}"))
        if perm.get("digest") and perm["digest"] != _permission_digest(orch, agent):
            return "esc-permission-sandbox-allow"
    except (TypeError, ValueError):
        return "esc-permission-sandbox-allow"
    try:
        schema = json.loads(facts.get("db_schema", "{}"))
        if schema.get("digest") and schema["digest"] != _live_schema_digest(orch._db):
            return "esc-schema-overloaded-column"
    except (TypeError, ValueError):
        return "esc-schema-overloaded-column"
    return None


def _server_fact_clause(structured_facts: dict[str, str]) -> str | None:
    """Return the policy clause id that the structured SERVER facts PROVE.

    The server derives these facts from authoritative runtime/database state
    (never from the untrusted reason prose). When a fact is present the
    attempt must escalate regardless of what the reason text claims — a
    misleading or omitted reason cannot authorize continuation. Both the
    hook (shipping seam) and the strict CI fake apply this gate.
    """
    try:
        adverse = json.loads(structured_facts.get("adverse_review", "{}"))
        if adverse.get("value"):
            return "esc-adverse-review-qa"
    except (TypeError, ValueError):
        pass
    try:
        partial = json.loads(structured_facts.get("partial_work", "{}"))
        if partial.get("value"):
            return "esc-partial-work"
    except (TypeError, ValueError):
        pass
    try:
        schema = json.loads(structured_facts.get("db_schema", "{}"))
        if schema.get("drift"):
            # The live DB schema is not the release-pinned schema: an
            # authoritative schema/migration drift signal.
            return "esc-schema-overloaded-column"
    except (TypeError, ValueError):
        pass
    return None


def _is_terminal_or_cancelled(task: "TaskRecord | None") -> bool:
    if task is None:
        return True
    if task.cancelled_at is not None:
        return True
    return task.status.value in _TERMINAL_STATUSES


# ── Immutable per-attempt input snapshot ─────────────────────────────────

class AuthorityInputSnapshot(BaseModel):
    """The immutable, server-derived input to ONE authority evaluation.

    ``reason`` is the raw proposed escalation prose — transient, passed to the
    evaluator, NEVER persisted. Every other field is server-derived and
    immutable within the attempt; the canonical serialization (with the reason
    replaced by its digest) is the snapshot digest recorded on the candidate.
    """
    model_config = {"extra": "forbid"}

    root_task_id: str
    team: str
    manager_agent: str
    manager_session_id: str
    candidate_id: str
    causal_event_id: str
    causal_event_digest: str
    causal_result_id: str | None = None
    reason: str = ""
    reason_digest: str = ""
    policy_id: str
    policy_version: str
    policy_digest: str
    prompt_id: str
    prompt_version: str
    prompt_digest: str
    model_id: str
    model_version: str
    model_digest: str
    structured_facts: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "causal_event_digest",
        "reason_digest",
        "policy_digest",
        "prompt_digest",
        "model_digest",
    )
    @classmethod
    def _digests_are_bounded_hex(cls, value, info):
        return validate_authority_digest(value, f"snapshot.{info.field_name}")

    @field_validator("policy_version", "prompt_version", "model_version")
    @classmethod
    def _versions_are_bounded(cls, value, info):
        return validate_authority_version(value, f"snapshot.{info.field_name}")

    def digest(self) -> str:
        """Deterministic snapshot digest (reason is replaced by its digest)."""
        canonical = self.model_dump(mode="json", exclude={"reason"})
        canonical["reason_digest"] = self.reason_digest
        return _sha256(
            json.dumps(canonical, sort_keys=True, ensure_ascii=False)
        )

    def claim_key(self) -> str:
        """The deterministic CAS claim tuple digest (mirrors Database)."""
        return _authority_claim_key(
            self.root_task_id,
            self.manager_session_id,
            self.causal_event_id,
            self.policy_digest,
            self.prompt_digest,
            self.model_digest,
        )

    def derived_candidate_id(self) -> str:
        return f"AUTH-CAND-{self.claim_key()}"


# ── Closed evaluator output schema ───────────────────────────────────────

class AuthorityEvaluatorOutput(BaseModel):
    """The strict, closed schema every evaluator output must satisfy.

    Extra fields are rejected; digest fields are bounded hex; disposition,
    action, and uncertainty codes are closed vocabularies. Anything that
    fails validation is a MALFORMED_OUTPUT fail-closed result — never a
    continuation.
    """
    model_config = {"extra": "forbid"}

    policy_id: str
    policy_version: str
    policy_digest: str
    team: str
    candidate_id: str
    input_digest: str
    disposition: str  # validated against closed vocabulary below
    clause_id: str | None = None
    action: str | None = None
    rationale_digest: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    uncertainty_codes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("policy_digest", "rationale_digest")
    @classmethod
    def _digests_are_bounded_hex(cls, value, info):
        if value is None:
            return None
        return validate_authority_digest(value, f"output.{info.field_name}")

    @field_validator("disposition")
    @classmethod
    def _disposition_is_closed(cls, value):
        if value not in (
            AuthorityDisposition.ESCALATE.value,
            AuthorityDisposition.CONTINUE_SAME_ROOT.value,
        ):
            raise ValueError(f"unknown disposition {value!r}")
        return value

    @field_validator("clause_id")
    @classmethod
    def _clause_id_is_blank_or_token(cls, value):
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("clause_id must be non-empty")
        return value

    @field_validator("action")
    @classmethod
    def _action_is_closed(cls, value):
        if value is None:
            return None
        if value not in CLOSED_ACTIONS:
            raise ValueError(f"unknown action {value!r}")
        return value

    @field_validator("uncertainty_codes")
    @classmethod
    def _uncertainty_codes_are_closed(cls, value):
        for code in value:
            if not isinstance(code, str) or code not in CLOSED_UNCERTAINTY_CODES:
                raise ValueError(f"unknown uncertainty code {code!r}")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def _evidence_refs_are_bounded(cls, value):
        if len(value) > 32:
            raise ValueError("too many evidence refs")
        for ref in value:
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError("evidence refs must be non-empty strings")
            if len(ref) > 200:
                raise ValueError("evidence refs must be at most 200 chars")
        return value


# ── Evaluator result + seam ──────────────────────────────────────────────

@dataclass(frozen=True)
class AuthorityEvaluationResult:
    """The typed result of one evaluator call (from the strict fake or the
    production subprocess evaluator). The hook re-validates it against the
    policy/snapshot before it may authorize anything."""
    disposition: AuthorityDisposition
    disposition_code: AuthorityDispositionCode
    response_digest: str
    clause_id: str | None = None
    action: str | None = None
    confidence: float = 0.0
    uncertainty_codes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    rationale_digest: str | None = None
    error: str | None = None


class AuthorityEvaluator(Protocol):
    """Deterministic injectable seam. Production wires the subprocess
    evaluator; CI wires the strict fake. A failing call returns an
    EVALUATOR_ERROR-typed result — it never raises into the hook."""

    model_id: str
    model_version: str
    model_digest: str

    def evaluate(
        self, snapshot: AuthorityInputSnapshot, *, policy: AuthorityPolicy | None = None
    ) -> AuthorityEvaluationResult:
        ...


def _fake_model_identity(kind: str) -> tuple[str, str, str]:
    model_id = f"fake/{kind}"
    model_version = "v1"
    model_digest = _sha256(f"{model_id}:{model_version}")
    return model_id, model_version, model_digest


# ── Strict fake (CI) ─────────────────────────────────────────────────────

class StrictFakeAuthorityEvaluator:
    """Deterministic, strict fake used by CI.

    * Deterministic: identical input produces byte-identical output.
    * Strict: it validates the snapshot (policy/team/digest identity) and its
      own result before returning; misconfiguration raises.
    * Classification: a built-in deterministic keyword classifier mirrors the
      policy clauses (must-escalate categories -> ESCALATE; the narrow
      routine-same-root pattern -> CONTINUE_SAME_ROOT; anything else fails
      closed to ESCALATE). Tests may pin an exact reason string to a
      disposition/clause/action with ``pinned``.
    """
    model_id, model_version, model_digest = _fake_model_identity("strict-authority-evaluator")

    def __init__(
        self,
        *,
        pinned: dict[str, tuple[str, str, str]] | None = None,
    ) -> None:
        # reason -> (disposition, clause_id, action)
        self._pinned = dict(pinned or {})

    # -- policy-mirroring classifier --------------------------------------

    @classmethod
    def classify_reason(cls, reason: str) -> tuple[str, str, str]:
        """Deterministic closed classification over the reason prose.

        Mirrors the Engineering policy: any must-escalate category marker
        matches its clause and returns ESCALATE. CONTINUE_SAME_ROOT is
        returned ONLY for a BYTE-EXACT member of the release-controlled
        closed routine set (``CONTINUE_ACCEPTED_REASONS``) — the server then
        has complete knowledge of the prose, so the grant never depends on
        keyword classification or the completeness/truthfulness of untrusted
        prose. Everything else fails closed to ESCALATE (clause
        esc-ambiguity-novelty).
        """
        lowered = reason.lower()
        markers: tuple[tuple[tuple[str, ...], str], ...] = (
            (("schema", "migration", "overloaded-column", "overloaded column",
              "column semantics", "database structural"), "esc-schema-overloaded-column"),
            (("permission", "sandbox", "allow-rule", "allow rule", "capability"),
             "esc-permission-sandbox-allow"),
            (("auth", "credential", "secret", "security", "privacy", "data access",
              "data-access"), "esc-auth-credentials-security"),
            (("v0", "v1", "compatibility", "compat"), "esc-compatibility"),
            (("spend", "budget", "cost", "quota", "billing"), "esc-spend-budget"),
            (("destructive", "irreversible", "deletion", "data loss"),
             "esc-destructive-irreversible"),
            (("external", "product", "deploy", "deployment", "release", "third-party",
              "hardware dependency", "vendor"), "esc-external-product-deploy"),
            (("review", "qa", "verdict", "request_changes", "revise", "rejected",
              "withheld approval"), "esc-adverse-review-qa"),
            (("ambiguous", "ambiguity", "novel", "unknown condition", "conflicting",
              "missing evidence", "incomplete evidence"), "esc-ambiguity-novelty"),
            (("partial", "incomplete work", "unverifiable"), "esc-partial-work"),
            (("exhausted", "budget", "ceiling", "max steps", "retry", "limit",
              "ceiling exceeded"), "esc-exhausted-limits"),
            (("cancel", "live children", "in-flight"), "esc-cancellation-live-work"),
            (("successor", "supersede", "revisit", "fresh root", "new task"),
             "esc-successor-supersede-revisit"),
        )
        for keywords, clause_id in markers:
            if any(kw in lowered for kw in keywords):
                return AuthorityDisposition.ESCALATE.value, clause_id, ACTION_ESCALATE_TO_FOUNDER
        # Narrow routine same-root pattern: ONLY a byte-exact member of the
        # release-controlled closed routine set — the server has complete
        # knowledge of the prose, so no must-escalate content can be hidden,
        # omitted, or misstated behind keywords.
        if _is_accepted_routine_reason(reason):
            return (
                AuthorityDisposition.CONTINUE_SAME_ROOT.value,
                "cont-routine-same-root",
                ACTION_CONTINUE_SAME_ROOT,
            )
        # Fail closed: an unverifiable reason is ambiguous (the server cannot
        # prove the routine condition from untrusted prose).
        return (
            AuthorityDisposition.ESCALATE.value,
            "esc-ambiguity-novelty",
            ACTION_ESCALATE_TO_FOUNDER,
        )

    # -- AuthorityEvaluator contract --------------------------------------

    def evaluate(
        self, snapshot: AuthorityInputSnapshot, *, policy: AuthorityPolicy | None = None
    ) -> AuthorityEvaluationResult:
        self._validate_snapshot(snapshot, policy=policy)
        # Server-derived facts OUTRANK reason prose (and any pinned verdict):
        # a server-proven must-escalate fact forces ESCALATE even when the
        # untrusted reason omits or misstates the condition.
        server_clause = _server_fact_clause(snapshot.structured_facts)
        if server_clause is not None:
            disposition, clause_id, action = (
                AuthorityDisposition.ESCALATE.value,
                server_clause,
                ACTION_ESCALATE_TO_FOUNDER,
            )
        elif snapshot.reason in self._pinned:
            disposition, clause_id, action = self._pinned[snapshot.reason]
            # The server gate outranks any pinned verdict: a CONTINUE for a
            # reason that is not a byte-exact release-controlled routine
            # phrase is not verifiable as routine and fails closed.
            if (
                disposition == AuthorityDisposition.CONTINUE_SAME_ROOT.value
                and not _is_accepted_routine_reason(snapshot.reason)
            ):
                disposition, clause_id, action = (
                    AuthorityDisposition.ESCALATE.value,
                    "esc-ambiguity-novelty",
                    ACTION_ESCALATE_TO_FOUNDER,
                )
        else:
            disposition, clause_id, action = self.classify_reason(snapshot.reason)
        code = (
            AuthorityDispositionCode.CONTINUE_SAME_ROOT
            if disposition == AuthorityDisposition.CONTINUE_SAME_ROOT.value
            else AuthorityDispositionCode.ESCALATE
        )
        result = AuthorityEvaluationResult(
            disposition=AuthorityDisposition(disposition),
            disposition_code=code,
            response_digest=_sha256(
                json.dumps(
                    {
                        "disposition": disposition,
                        "clause_id": clause_id,
                        "action": action,
                        "input_digest": snapshot.digest(),
                        "candidate_id": snapshot.candidate_id,
                    },
                    sort_keys=True,
                )
            ),
            clause_id=clause_id,
            action=action,
            confidence=1.0 if disposition == AuthorityDisposition.CONTINUE_SAME_ROOT.value else 1.0,
            uncertainty_codes=(),
            evidence_refs=(),
            rationale_digest=_sha256(f"strict-fake:{snapshot.reason_digest}"),
        )
        self._validate_result(snapshot, result)
        return result

    # -- strict validation ------------------------------------------------

    def _validate_snapshot(
        self, snapshot: AuthorityInputSnapshot, *, policy: AuthorityPolicy | None = None
    ) -> None:
        effective_policy = policy or POLICY_BY_TEAM.get(snapshot.team)
        if effective_policy is None:
            raise ValueError(f"no release-controlled policy for team {snapshot.team!r}")
        if (snapshot.policy_id != effective_policy.id
                or snapshot.policy_version != effective_policy.version):
            raise ValueError("snapshot policy identity does not match the release policy")
        if snapshot.policy_digest != effective_policy.digest:
            raise ValueError("snapshot policy digest does not match the release policy")
        if snapshot.prompt_digest != PROMPT_DIGEST:
            raise ValueError("snapshot prompt digest does not match the release prompt")
        if not snapshot.reason:
            raise ValueError("snapshot reason must be non-empty")

    def _validate_result(self, snapshot: AuthorityInputSnapshot, result: AuthorityEvaluationResult) -> None:
        if result.disposition == AuthorityDisposition.CONTINUE_SAME_ROOT:
            if result.clause_id != "cont-routine-same-root":
                raise ValueError("strict fake continue requires clause cont-routine-same-root")
            if result.action != ACTION_CONTINUE_SAME_ROOT:
                raise ValueError("strict fake continue requires action continue_same_root")
            if result.disposition_code != AuthorityDispositionCode.CONTINUE_SAME_ROOT:
                raise ValueError("strict fake continue requires code continue_same_root")
        else:
            if result.disposition not in (
                AuthorityDisposition.ESCALATE,
                AuthorityDisposition.EVALUATOR_ERROR,
            ):
                raise ValueError("strict fake only emits escalate/evaluator_error dispositions")
        if result.response_digest != _sha256(
            json.dumps(
                {
                    "disposition": result.disposition.value,
                    "clause_id": result.clause_id,
                    "action": result.action,
                    "input_digest": snapshot.digest(),
                    "candidate_id": snapshot.candidate_id,
                },
                sort_keys=True,
            )
        ):
            raise ValueError("strict fake response digest mismatch")


# ── Production subprocess evaluator ──────────────────────────────────────

class LLMSubprocessAuthorityEvaluator:
    """Legacy non-production evaluator retained as a strict compatibility seam.

    S6a production wiring never constructs this class. Semantic evidence is
    supplied by the already-running manager in its authenticated completion.
    This bounded implementation remains for adversarial parser tests and old
    explicitly injected callers only. It performs one one-shot LLM call through the
    machine-local executor registry (shared-identity posture), followed by
    strict closed-schema output parsing.

    The invocation is bounded by ``timeout_seconds``; a hang, provider
    error, empty/malformed/extra-field/unknown-value output, credential
    marker, or any policy/team/version/digest/candidate/input mismatch fails
    closed to an EVALUATOR_ERROR-typed result (the hook then escalates).
    """
    model_id = f"executor/{DEFAULT_EXECUTOR_KIND}"
    model_version = "v1"
    model_digest = _sha256(f"{model_id}:{model_version}")

    def __init__(
        self,
        *,
        executor_kind: str = DEFAULT_EXECUTOR_KIND,
        timeout_seconds: float = DEFAULT_EVALUATOR_TIMEOUT_SECONDS,
        resolve_binary=None,
        invoke=None,
    ) -> None:
        self._executor_kind = executor_kind
        self._timeout_seconds = timeout_seconds
        # Injectable boundaries for deterministic tests: binary resolution
        # and subprocess invocation default to the real registry + subprocess.
        self._resolve_binary = resolve_binary or self._default_resolve_binary
        self._invoke = invoke or self._default_invoke
        self.model_id = f"executor/{executor_kind}"
        self.model_digest = _sha256(f"{self.model_id}:{self.model_version}")

    # -- boundaries -------------------------------------------------------

    @staticmethod
    def _default_resolve_binary(executor_kind: str) -> str | None:
        from runtime.orchestrator.executor_binary_registry import get_binary
        return get_binary(executor_kind)

    @staticmethod
    def _default_invoke(argv: list[str], prompt: str, timeout_seconds: float):
        return subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

    # -- build + invoke ---------------------------------------------------

    def build_argv(self, prompt: str) -> list[str]:
        binary = self._resolve_binary(self._executor_kind)
        if not binary:
            raise RuntimeError(
                f"no registered executor binary for kind {self._executor_kind!r}"
            )
        # One-shot, stdin-fed, JSONL event stream — the same accepted
        # uncontained posture as the headless PiAdapter (THR-056 §3).
        return [binary, "-p", "--mode", "json", prompt]

    def _extract_final_text(self, stdout: str) -> str:
        """Concatenate the assistant's text deltas from pi's JSONL event
        stream (message_update events). Empty output yields empty text."""
        parts: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "message_update":
                continue
            ame = event.get("assistantMessageEvent")
            if not isinstance(ame, dict) or ame.get("type") != "text_delta":
                continue
            delta = ame.get("delta")
            if isinstance(delta, str) and delta:
                parts.append(delta)
        return "".join(parts)

    @staticmethod
    def _extract_json_object(text: str) -> dict | None:
        """Extract exactly one JSON object from the model text. More than one
        object, or non-JSON text, returns None (malformed)."""
        text = text.strip()
        if not text.startswith("{"):
            # Tolerate a code fence only when it wraps exactly one object.
            if "```" in text:
                start = text.find("{")
                end = text.rfind("}")
                if start != -1 and end != -1 and start < end:
                    text = text[start:end + 1]
            else:
                return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return data

    # -- AuthorityEvaluator contract --------------------------------------

    def evaluate(
        self, snapshot: AuthorityInputSnapshot, *, policy: AuthorityPolicy | None = None
    ) -> AuthorityEvaluationResult:
        from runtime.models import AuthorityDispositionCode as _Code
        effective_policy = policy or POLICY_BY_TEAM[snapshot.team]
        prompt = build_authority_evaluation_prompt(
            policy=effective_policy,
            candidate_id=snapshot.candidate_id,
            team=snapshot.team,
            manager_agent=snapshot.manager_agent,
            manager_session_id=snapshot.manager_session_id,
            root_task_id=snapshot.root_task_id,
            causal_event_id=snapshot.causal_event_id,
            causal_event_digest=snapshot.causal_event_digest,
            reason=snapshot.reason,
            reason_digest=snapshot.reason_digest,
            input_digest=snapshot.digest(),
            structured_facts=snapshot.structured_facts,
        )
        try:
            argv = self.build_argv(prompt)
            proc = self._invoke(argv, prompt, self._timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            return self._error_result(_Code.TIMEOUT, f"evaluator timeout: {exc}")
        except Exception as exc:
            return self._error_result(_Code.EVALUATOR_ERROR, f"evaluator launch failed: {exc}")
        if proc.returncode != 0:
            return self._error_result(
                _Code.EVALUATOR_ERROR,
                f"evaluator exited {proc.returncode}: {(proc.stderr or '')[-500:]}",
            )
        text = self._extract_final_text(proc.stdout or "")
        if not text.strip():
            return self._error_result(_Code.MALFORMED_OUTPUT, "evaluator produced no text output")
        data = self._extract_json_object(text)
        if data is None:
            return self._error_result(_Code.MALFORMED_OUTPUT, "evaluator output is not a single JSON object")
        response_digest = _sha256(text)
        try:
            parsed = AuthorityEvaluatorOutput.model_validate(data)
        except Exception as exc:
            return self._error_result(
                _Code.MALFORMED_OUTPUT, f"evaluator output failed closed-schema validation: {exc}"
            )
        # Echo-contract: the output must name the exact attempt inputs.
        if (
            parsed.policy_id != snapshot.policy_id
            or parsed.policy_version != snapshot.policy_version
            or parsed.policy_digest != snapshot.policy_digest
            or parsed.team != snapshot.team
            or parsed.candidate_id != snapshot.candidate_id
            or parsed.input_digest != snapshot.digest()
        ):
            return self._error_result(
                _Code.MALFORMED_OUTPUT, "evaluator output policy/team/candidate/input mismatch"
            )
        disposition = AuthorityDisposition(parsed.disposition)
        clause = effective_policy.clause_by_id(parsed.clause_id) if parsed.clause_id else None
        expected_action = (
            ACTION_CONTINUE_SAME_ROOT
            if disposition == AuthorityDisposition.CONTINUE_SAME_ROOT
            else ACTION_ESCALATE_TO_FOUNDER
        )
        if parsed.clause_id is None:
            valid_clause_action = (
                disposition == AuthorityDisposition.ESCALATE and parsed.action is None
            )
        else:
            valid_clause_action = (
                clause is not None
                and clause.action == expected_action
                and parsed.action == clause.action
            )
        if not valid_clause_action:
            return self._error_result(
                _Code.MALFORMED_OUTPUT,
                "evaluator output names an unknown or mismatched policy clause/action",
            )
        # Scan only model-controlled free text after all trusted echo and
        # closed-vocabulary validation succeeds.  In particular, clause ids
        # such as ``esc-auth-credentials-security`` are policy vocabulary,
        # not credential-bearing prose. Rationale is digest-only in this
        # schema; evidence refs are its sole free-text output field.
        free_text = "\n".join(parsed.evidence_refs).lower()
        if any(marker in free_text for marker in _CREDENTIAL_MARKERS):
            return self._error_result(
                _Code.INJECTION_GUARD, "evaluator output carried a credential-like marker"
            )
        code = (
            AuthorityDispositionCode.CONTINUE_SAME_ROOT
            if disposition == AuthorityDisposition.CONTINUE_SAME_ROOT
            else AuthorityDispositionCode.ESCALATE
        )
        if parsed.confidence < CONTINUE_MIN_CONFIDENCE:
            code = AuthorityDispositionCode.LOW_CONFIDENCE
        return AuthorityEvaluationResult(
            disposition=disposition,
            disposition_code=code,
            response_digest=response_digest,
            clause_id=parsed.clause_id,
            action=parsed.action,
            confidence=parsed.confidence,
            uncertainty_codes=tuple(parsed.uncertainty_codes),
            evidence_refs=tuple(parsed.evidence_refs),
            rationale_digest=parsed.rationale_digest,
        )

    @staticmethod
    def _error_result(code: AuthorityDispositionCode, error: str) -> AuthorityEvaluationResult:
        return AuthorityEvaluationResult(
            disposition=AuthorityDisposition.EVALUATOR_ERROR,
            disposition_code=code,
            response_digest=_sha256(f"evaluator-error:{code.value}:{error}"),
            error=error,
        )


# ── Hook ─────────────────────────────────────────────────────────────────

@dataclass
class _NormalizedVerdict:
    disposition: AuthorityDisposition
    disposition_code: AuthorityDispositionCode
    clause_id: str | None
    action: str | None
    confidence: float
    uncertainty_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    rationale_digest: str | None
    response_digest: str
    error: str | None = None


def _normalize_result(
    policy: AuthorityPolicy,
    candidate_id: str,
    input_digest: str,
    result: AuthorityEvaluationResult,
) -> _NormalizedVerdict:
    """Re-validate the evaluator result against the policy and snapshot.

    This is the final authority gate shared by the fake and the production
    evaluator: a CONTINUE_SAME_ROOT verdict survives ONLY when the named
    clause exists, is a continue clause, names the exact permitted action,
    is unambiguous (matching code), and is confident enough. Everything else
    fails closed to ESCALATE.
    """
    if result.disposition == AuthorityDisposition.CONTINUE_SAME_ROOT:
        if result.confidence < CONTINUE_MIN_CONFIDENCE:
            return _NormalizedVerdict(
                AuthorityDisposition.ESCALATE, AuthorityDispositionCode.LOW_CONFIDENCE,
                None, None, result.confidence, result.uncertainty_codes,
                result.evidence_refs, result.rationale_digest, result.response_digest,
                error="continue below confidence threshold",
            )
        if result.disposition_code != AuthorityDispositionCode.CONTINUE_SAME_ROOT:
            code = (
                result.disposition_code
                if result.disposition_code
                in _DIAGNOSTIC_FAILURE_CODES
                else AuthorityDispositionCode.MALFORMED_OUTPUT
            )
            return _NormalizedVerdict(
                AuthorityDisposition.ESCALATE, code,
                None, None, result.confidence, result.uncertainty_codes,
                result.evidence_refs, result.rationale_digest, result.response_digest,
                error="continue with non-continue disposition code",
            )
        clause = policy.clause_by_id(result.clause_id) if result.clause_id else None
        if clause is None or clause.action != ACTION_CONTINUE_SAME_ROOT:
            return _NormalizedVerdict(
                AuthorityDisposition.ESCALATE, AuthorityDispositionCode.MALFORMED_OUTPUT,
                None, None, result.confidence, result.uncertainty_codes,
                result.evidence_refs, result.rationale_digest, result.response_digest,
                error="continue names an unknown or non-continue policy clause",
            )
        if result.action != ACTION_CONTINUE_SAME_ROOT or result.action != clause.action:
            return _NormalizedVerdict(
                AuthorityDisposition.ESCALATE, AuthorityDispositionCode.MALFORMED_OUTPUT,
                None, None, result.confidence, result.uncertainty_codes,
                result.evidence_refs, result.rationale_digest, result.response_digest,
                error="continue names an action other than continue_same_root",
            )
        return _NormalizedVerdict(
            AuthorityDisposition.CONTINUE_SAME_ROOT, AuthorityDispositionCode.CONTINUE_SAME_ROOT,
            result.clause_id, result.action, result.confidence,
            result.uncertainty_codes, result.evidence_refs,
            result.rationale_digest, result.response_digest,
        )
    if result.disposition == AuthorityDisposition.ESCALATE:
        # Carry the matched must-escalate clause when the result names a real
        # must-escalate clause of the policy (audit completeness); anything
        # else keeps clause_id None (fail-closed escalate).
        clause = policy.clause_by_id(result.clause_id) if result.clause_id else None
        matched = (
            clause.id if clause is not None and clause.action == ACTION_ESCALATE_TO_FOUNDER
            else None
        )
        code = (
            result.disposition_code
            if result.disposition_code in _DIAGNOSTIC_FAILURE_CODES
            else AuthorityDispositionCode.ESCALATE
        )
        return _NormalizedVerdict(
            AuthorityDisposition.ESCALATE,
            code,
            matched, None, result.confidence, result.uncertainty_codes,
            result.evidence_refs, result.rationale_digest, result.response_digest,
        )
    # EVALUATOR_ERROR / NOT_APPLICABLE / anything unknown: fail closed.
    code = (
        result.disposition_code
        if result.disposition_code in _DIAGNOSTIC_FAILURE_CODES
        else AuthorityDispositionCode.EVALUATOR_ERROR
    )
    return _NormalizedVerdict(
        AuthorityDisposition.ESCALATE,
        code,
        None, None, result.confidence, result.uncertainty_codes,
        result.evidence_refs, result.rationale_digest, result.response_digest,
        error=result.error,
    )


def _record_hook_outcome(
    db,
    *,
    task_id: str,
    agent: str,
    outcome: str,
    policy: AuthorityPolicy | None = None,
    candidate_id: str | None = None,
    snapshot: AuthorityInputSnapshot | None = None,
    verdict: _NormalizedVerdict | None = None,
    fences: dict[str, AuthorityFenceResult] | None = None,
    lifecycle: str | None = None,
    error: str | None = None,
) -> int | None:
    """Best-effort append of exactly one `authority_hook` outcome row.

    Never raises: if the audit row cannot be written, the caller still fails
    closed (never continues); the escalation audit row and the candidate
    lifecycle remain the capture-failure evidence.
    """
    payload: dict = {"outcome": outcome}
    if policy is not None:
        payload["policy_id"] = policy.id
        payload["policy_version"] = policy.version
        payload["policy_digest"] = policy.digest
    if candidate_id is not None:
        payload["candidate_id"] = candidate_id
    if snapshot is not None:
        payload["input_digest"] = snapshot.digest()
        payload["reason_digest"] = snapshot.reason_digest
        payload["causal_event_id"] = snapshot.causal_event_id
        payload["causal_event_digest"] = snapshot.causal_event_digest
        payload["causal_result_id"] = snapshot.causal_result_id
        payload["prompt_id"] = snapshot.prompt_id
        payload["prompt_version"] = snapshot.prompt_version
        payload["prompt_digest"] = snapshot.prompt_digest
        payload["model_id"] = snapshot.model_id
        payload["model_version"] = snapshot.model_version
        payload["model_digest"] = snapshot.model_digest
    if verdict is not None:
        payload["disposition"] = verdict.disposition.value
        payload["disposition_code"] = verdict.disposition_code.value
        payload["clause_id"] = verdict.clause_id
        payload["action"] = verdict.action
        payload["confidence"] = verdict.confidence
        payload["uncertainty_codes"] = list(verdict.uncertainty_codes)
        payload["evidence_refs"] = list(verdict.evidence_refs)
        payload["rationale_digest"] = verdict.rationale_digest
        payload["response_digest"] = verdict.response_digest
    if fences:
        payload["fence_results"] = {
            name: (result.model_dump(mode="json") if isinstance(result, AuthorityFenceResult) else result)
            for name, result in fences.items()
        }
    if lifecycle is not None:
        payload["lifecycle"] = lifecycle
    if error is not None:
        payload["error"] = error[:500]
    try:
        return db.insert_audit_log(
            task_id=task_id, agent=agent,
            action=AUDIT_ACTION_HOOK_OUTCOME, payload=payload,
        )
    except Exception as exc:  # pragma: no cover - audit failure path
        logger.warning("authority hook outcome row failed for %s: %s", task_id, exc)
        return None


def _current_budget_ceilings(orch: "Orchestrator") -> tuple[int, int]:
    """The CURRENT release-controlled budget ceilings (orchestration steps,
    revise rounds). ``max_revise_rounds <= 0`` means the revise budget is
    disabled (unlimited). Re-read at every call site so the consumption-time
    recheck uses the freshest ceilings, not the evaluation-time ones."""
    max_steps = orch._settings.max_orchestration_steps
    org_cap = 0
    try:
        from runtime.orchestrator.org_config import load_org_config
        org_cap = load_org_config(orch._paths).max_revise_rounds
    except Exception:
        org_cap = 0
    return max_steps, org_cap


def _server_evidence(
    orch: "Orchestrator",
    current: "TaskRecord",
    agent: str,
    policy: "AuthorityPolicy",
    fences: dict[str, AuthorityFenceResult],
) -> tuple[dict[str, str], list[str]]:
    """Collect authoritative server/runtime facts for the evaluation snapshot.

    Every fact is a JSON-encoded ``{"value": ..., "source": ...}`` object with
    an immutable provenance digest where applicable — the reason prose can
    never establish or alter a server fact. Returns ``(facts, matched)`` where
    ``matched`` lists the policy must-escalate clause ids the server state
    PROVES (the hook forces ESCALATE for those regardless of the evaluator):
    an adverse child review verdict (esc-adverse-review-qa), partial-work/
    zombie evidence (esc-partial-work), and DB-schema drift vs the
    release-pinned schema (esc-schema-overloaded-column).
    """
    db = orch._db
    facts: dict[str, str] = {}
    matched: list[str] = []

    # Mechanical fence outcomes (same objects recorded on the candidate).
    facts["fence_results"] = json.dumps(
        {
            name: fr.model_dump(mode="json")
            for name, fr in sorted(fences.items())
        },
        sort_keys=True,
    )

    # Budget counters + current ceilings.
    max_steps, org_cap = _current_budget_ceilings(orch)
    budget_exhausted = (
        current.orchestration_step_count >= max_steps
        or (org_cap > 0 and current.revision_count >= org_cap)
    )
    facts["budget"] = json.dumps(
        {
            "value": {
                "orchestration_step_count": current.orchestration_step_count,
                "max_orchestration_steps": max_steps,
                "revision_count": current.revision_count,
                "max_revise_rounds": org_cap,
                "exhausted": budget_exhausted,
            },
            "source": "tasks/settings/org_config",
        },
        sort_keys=True,
    )

    # Lineage: revisit / parent / successor / thread / fresh-root.
    is_successor = _is_successor_root(db, current.id)
    is_fresh_root = (
        current.parent_task_id is None
        and current.revisit_of_task_id is None
        and not is_successor
    )
    facts["lineage"] = json.dumps(
        {
            "value": {
                "revisit_of_task_id": current.revisit_of_task_id or "",
                "parent_task_id": current.parent_task_id or "",
                "is_successor": is_successor,
                "thread_origin": bool(current.dispatched_from_thread_id),
                "is_fresh_root": is_fresh_root,
            },
            "source": "tasks/manager_supersessions",
        },
        sort_keys=True,
    )

    # Active work / block / cancellation / session state.
    facts["active_work"] = json.dumps(
        {
            "value": {
                "active_chain": current.active_chain or "",
                "active_fanout": current.active_fanout or "",
                "blocked_on_job_ids": current.blocked_on_job_ids or "",
            },
            "source": "tasks",
        },
        sort_keys=True,
    )
    facts["cancellation"] = json.dumps(
        {
            "value": {
                "cancelled": current.cancelled_at is not None,
                "status": current.status.value,
            },
            "source": "tasks",
        },
        sort_keys=True,
    )
    facts["block_state"] = json.dumps(
        {
            "value": {"block_kind": current.block_kind.value if current.block_kind else None},
            "source": "tasks",
        },
        sort_keys=True,
    )
    facts["session"] = json.dumps(
        {
            "value": {
                "current_session_id": current.current_session_id or "",
                "manager_agent": current.assigned_agent or agent,
            },
            "source": "tasks",
        },
        sort_keys=True,
    )

    # Adverse review/QA: a child whose LATEST persisted verdict is a
    # non-approve verdict is an authoritative must-escalate fact.
    adverse: list[dict[str, str]] = []
    for child_id in db.get_children(current.id):
        latest = db.execute(
            "SELECT verdict FROM task_results WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (child_id,),
        ).fetchone()
        verdict = latest["verdict"] if latest is not None else None
        if verdict and verdict not in _APPROVED_VERDICTS:
            adverse.append({"task_id": child_id, "verdict": str(verdict)})
    has_adverse = bool(adverse)
    facts["adverse_review"] = json.dumps(
        {
            "value": has_adverse,
            "children": adverse[:5],
            "source": "task_results",
        },
        sort_keys=True,
    )
    if has_adverse:
        matched.append("esc-adverse-review-qa")

    # Partial-work evidence: the daemon flagged this task's session as dead
    # mid-turn (zombie reaper) — the authoritative partial-work signal.
    zombie = current.zombie_flagged_at is not None
    facts["partial_work"] = json.dumps(
        {
            "value": zombie,
            "source": "tasks.zombie_flagged_at",
        },
        sort_keys=True,
    )
    if zombie:
        matched.append("esc-partial-work")

    # Protected-boundary digests: the org permission/allow-rule surface and
    # the current DB schema, so the evaluator sees authoritative server state.
    # The permission digest is provenance (no release baseline exists for the
    # org-local surface); the permission boundary is enforced by the
    # closed-pattern CONTINUE gate + a during-attempt change recheck (step 8b)
    # + the action-safety proof. The DB schema digest IS a gate: a live schema
    # that differs from the release-pinned schema (a fresh Database() built
    # from the current code) is an authoritative schema/migration drift signal
    # that forces esc-schema-overloaded-column regardless of reason prose.
    facts["org_permission"] = json.dumps(
        {
            "digest": _permission_digest(orch, current.assigned_agent or agent),
            "source": "org_config/agent_def",
        },
        sort_keys=True,
    )
    live_schema_digest = _live_schema_digest(db)
    schema_drift = live_schema_digest != _release_schema_digest()
    facts["db_schema"] = json.dumps(
        {
            "digest": live_schema_digest,
            "drift": schema_drift,
            "source": "sqlite_master",
        },
        sort_keys=True,
    )
    if schema_drift:
        matched.append("esc-schema-overloaded-column")

    # The release-controlled policy binding (immutable identity).
    facts["team_policy"] = json.dumps(
        {
            "policy_id": policy.id,
            "policy_version": policy.version,
            "policy_digest": policy.digest,
            "source": "authority_policy.py",
        },
        sort_keys=True,
    )
    return facts, matched


def _eligible_fences(
    orch: "Orchestrator",
    current: "TaskRecord",
    agent: str,
) -> dict[str, AuthorityFenceResult]:
    """Server-owned mechanical fences. Every failure makes the root
    ineligible (the hook records the outcome and the escalation proceeds
    through the existing path). No policy output can override these."""
    fences: dict[str, AuthorityFenceResult] = {}
    try:
        manager = orch.teams.manager_for_team(current.team).name
    except (KeyError, ValueError):
        manager = None
    fences["manager_ownership"] = AuthorityFenceResult(
        passed=bool(
            manager is not None
            and agent == manager
            and current.assigned_agent == agent
        )
    )
    fences["current_session"] = AuthorityFenceResult(
        passed=current.current_session_id is not None
    )
    fences["cancellation"] = AuthorityFenceResult(
        passed=current.cancelled_at is None
        and current.status.value not in _TERMINAL_STATUSES
    )
    fences["claimed_root"] = AuthorityFenceResult(
        passed=current.status == TaskStatus.IN_PROGRESS
        and current.block_kind is None
    )
    fences["revisit_lineage"] = AuthorityFenceResult(
        passed=current.revisit_of_task_id is None
    )
    fences["successor_lineage"] = AuthorityFenceResult(
        passed=not _is_successor_root(orch._db, current.id)
    )
    fences["active_work"] = AuthorityFenceResult(
        passed=current.active_chain is None
        and current.active_fanout is None
        and current.blocked_on_job_ids is None
    )
    max_steps, org_cap = _current_budget_ceilings(orch)
    budget_ok = (
        current.orchestration_step_count < max_steps
        and (org_cap <= 0 or current.revision_count < org_cap)
    )
    fences["budget_exhausted"] = AuthorityFenceResult(
        passed=budget_ok,
        code="budget_exceeded" if not budget_ok else None,
    )
    return fences


def _is_successor_root(db, task_id: str) -> bool:
    try:
        row = db.execute(
            "SELECT 1 FROM manager_supersessions WHERE successor_task_id = ? LIMIT 1",
            (task_id,),
        ).fetchone()
    except Exception:
        return True  # fail closed on any read defect
    return row is not None


def run_authority_hook(
    orch: "Orchestrator",
    task: "TaskRecord",
    agent: str,
    reason: str,
    result_row_id: int | None,
    manager_self_evaluation: dict | None = None,
) -> str:
    """The pre-escalation authority hook. Returns ``"continue_same_root"``
    (the named same-root permitted action was executed and audited) or
    ``"escalate"`` (fail closed — the caller proceeds through the exact
    existing escalation path). Never raises into the caller.

    ``result_row_id`` is the immutable ``task_results`` row id whose
    CompletionReport produced this escalate decision — the causal event
    identity. It is stable across restart/recovery re-entry (the boot sweep
    and zombie reaper rebuild the report from the SAME row), so a replayed
    attempt cannot mint a second candidate or evaluation. When it is absent
    no immutable causality exists: the hook fails closed with a
    ``capture_failure`` outcome and never creates a candidate.
    """
    policy = POLICY_BY_TEAM.get(task.team)
    if policy is None:
        # No release-controlled policy for this team: hook not applicable.
        return "escalate"

    db = orch._db
    active_activation = None
    active_release = None
    try:
        from runtime.orchestrator.active_authority_policy import (
            load_session_policy_binding, load_session_policy_snapshot, policy_from_release,
            SELF_EVALUATION_CONTRACT_DIGEST, SELF_EVALUATION_CONTRACT_ID,
            SELF_EVALUATION_CONTRACT_VERSION,
        )
        from runtime.orchestrator.authority_policy_store import AuthorityPolicyStore
        policy_store = AuthorityPolicyStore(db)
        launch_snapshot = load_session_policy_snapshot(
            db=db, store=policy_store, task_id=task.id,
            session_id=task.current_session_id or "", agent_name=agent,
        )
        if launch_snapshot is not None:
            active_activation = launch_snapshot.activation
            active_release = launch_snapshot.release
            policy = policy_from_release(active_release)
        session_binding = load_session_policy_binding(
            db=db, task_id=task.id, session_id=task.current_session_id or "",
            agent_name=agent,
        )
    except Exception as exc:
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_CAPTURE_FAILURE,
            policy=policy, error=f"active policy resolution failed: {exc}",
        )
        return "escalate"
    current = db.get_task(task.id)
    if current is None:
        return "escalate"

    # ---- 1. Eligibility + mechanical fences (recorded, fail closed) ----
    fences = _eligible_fences(orch, current, agent)
    failed = [name for name, fr in fences.items() if not fr.passed]
    if failed:
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_INELIGIBLE,
            policy=policy, fences=fences,
            error="fences not passed: " + ", ".join(sorted(failed)),
        )
        return "escalate"

    # ---- 1b. Immutable causal identity REQUIRED. The candidate claim tuple
    # is derived from the persisted task-result row (stable across restart),
    # NEVER from a freshly written orchestration-step audit id (which a
    # restart re-entry would re-mint and turn into a second candidate). ----
    if result_row_id is None:
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_CAPTURE_FAILURE,
            policy=policy, fences=fences,
            error="no immutable task-result row id for causal identity",
        )
        return "escalate"

    # ---- 2. Immutable input snapshot (candidate id is the deterministic
    # claim derivation, so it is known before the CAS INSERT). Structured
    # SERVER facts carry authoritative provenance; the reason prose can never
    # establish or alter them. ----
    reason_digest = _sha256(reason or "")
    causal_event_id = f"result:{result_row_id}"
    causal_event_digest = _sha256(f"task-result:{result_row_id}")
    evaluator = orch._authority_evaluator
    manager_session_id = current.current_session_id or ""
    if active_activation is not None:
        provider_id = str((session_binding or {}).get("provider_id", "unknown"))
        executor_kind = str((session_binding or {}).get("executor_kind", "unknown"))
        effective_model_id = str((session_binding or {}).get("model_id", "default"))
        model_id = f"manager/{provider_id}/{executor_kind}/{effective_model_id}"
        model_version = SELF_EVALUATION_CONTRACT_VERSION
        model_digest = _sha256(
            f"{model_id}:{model_version}:{SELF_EVALUATION_CONTRACT_DIGEST}"
        )
    else:
        provider_id = getattr(evaluator, "provider_id", "local") if evaluator else "none"
        executor_kind = getattr(evaluator, "_executor_kind", "none") if evaluator else "none"
        model_id = evaluator.model_id if evaluator is not None else "none"
        model_version = evaluator.model_version if evaluator is not None else "v1"
        model_digest = evaluator.model_digest if evaluator is not None else _sha256("none:v1")
    structured_facts, server_clauses = _server_evidence(
        orch, current, agent, policy, fences,
    )
    candidate_id = f"AUTH-CAND-{_authority_claim_key(
        current.id,
        manager_session_id,
        causal_event_id,
        policy.digest,
        PROMPT_DIGEST,
        model_digest,
    )}"
    snapshot = AuthorityInputSnapshot(
        root_task_id=current.id,
        team=current.team,
        manager_agent=current.assigned_agent or agent,
        manager_session_id=manager_session_id,
        candidate_id=candidate_id,
        causal_event_id=causal_event_id,
        causal_event_digest=causal_event_digest,
        causal_result_id=None,
        reason=reason or "",
        reason_digest=reason_digest,
        policy_id=policy.id,
        policy_version=policy.version,
        policy_digest=policy.digest,
        prompt_id=PROMPT_ID,
        prompt_version=PROMPT_VERSION,
        prompt_digest=PROMPT_DIGEST,
        model_id=model_id,
        model_version=model_version,
        model_digest=model_digest,
        structured_facts=structured_facts,
    )
    input_digest = snapshot.digest()

    # ---- 3. Claim the candidate (deterministic CAS) ----
    try:
        candidate_kwargs = dict(
            root_task_id=current.id,
            team=current.team,
            manager_agent=current.assigned_agent or agent,
            manager_session_id=manager_session_id,
            causal_event_id=causal_event_id,
            causal_event_digest=causal_event_digest,
            causal_result_id=None,
            policy_id=policy.id,
            policy_version=policy.version,
            policy_digest=policy.digest,
            prompt_id=PROMPT_ID,
            prompt_version=PROMPT_VERSION,
            prompt_digest=PROMPT_DIGEST,
            model_id=model_id,
            model_version=model_version,
            model_digest=model_digest,
            snapshot_digest=input_digest,
            fence_results={name: fr.model_dump(mode="json") for name, fr in fences.items()},
        )
        if active_activation is not None and active_release is not None:
            candidate, _pin = policy_store.claim_candidate_with_pin(
                release_id=active_release.id,
                activation_id=active_activation.id,
                activation_epoch=active_activation.epoch,
                provider_id=provider_id,
                executor_kind=executor_kind,
                **candidate_kwargs,
            )
            candidate_id, won = candidate.id, True
        else:
            candidate_id, won = db.claim_authority_candidate(**candidate_kwargs)
    except Exception as exc:
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_CAPTURE_FAILURE,
            policy=policy, snapshot=snapshot, fences=fences,
            error=f"candidate claim failed: {exc}",
        )
        return "escalate"

    def _authenticate_persisted_candidate_policy() -> None:
        if active_release is None or active_activation is None:
            return
        pin = policy_store.get_candidate_pin(candidate_id)
        if pin is None:
            raise ValueError("DB-backed authority candidate is missing its policy pin")
        expected_provider = provider_id
        expected_kind = executor_kind
        if (pin.release_id != active_release.id
                or pin.activation_id != active_activation.id
                or pin.activation_epoch != active_activation.epoch
                or pin.provider_id != expected_provider
                or pin.executor_kind != expected_kind):
            raise ValueError("authority candidate policy pin identity mismatch")
        reread_release = policy_store.get_release(pin.release_id)
        if reread_release is None or policy_from_release(reread_release) != policy:
            raise ValueError("authority candidate release snapshot mismatch")

    try:
        _authenticate_persisted_candidate_policy()
    except Exception as exc:
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_CAPTURE_FAILURE,
            policy=policy, snapshot=snapshot, candidate_id=candidate_id,
            fences=fences, lifecycle="created", error=f"candidate pin authentication failed: {exc}",
        )
        return "escalate"

    if not won:
        # CAS loser: the exact deterministic tuple was already claimed. Never
        # evaluate again, never continue — fail closed.
        try:
            db.record_authority_audit(
                candidate_id=candidate_id, event_type="candidate_claim_lost",
                payload={"digest": input_digest, "version": policy.version},
            )
        except Exception:
            pass
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_CAS_LOST,
            policy=policy, snapshot=snapshot, candidate_id=candidate_id,
            fences=fences, lifecycle="created",
        )
        return "escalate"

    # ---- 4. Claimed audit event ----
    try:
        db.record_authority_audit(
            candidate_id=candidate_id, event_type="candidate_claimed",
            payload={"digest": input_digest, "version": policy.version},
        )
    except Exception as exc:
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_AUDIT_FAILURE,
            policy=policy, snapshot=snapshot, candidate_id=candidate_id,
            fences=fences, lifecycle="created", error=f"claimed audit event failed: {exc}",
        )
        return "escalate"

    # ---- 5. Cancellation/staleness re-check AFTER claim ----
    current = db.get_task(task.id)
    if _is_terminal_or_cancelled(current):
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_CANCELLED_STALE,
            policy=policy, snapshot=snapshot, candidate_id=candidate_id,
            fences=fences, lifecycle="created",
            error="task cancelled/terminal between claim and evaluation",
        )
        return "escalate"

    # ---- 6. Evaluate (bounded by the seam; evaluator failures are typed) ----
    if active_activation is not None:
        try:
            if manager_self_evaluation is None:
                raise ValueError("manager self-evaluation is missing")
            if manager_self_evaluation.get("_error_code"):
                raise ValueError("manager self-evaluation is malformed")
            supplied = ManagerSelfEvaluation.model_validate(manager_self_evaluation)
            expected_identity = {
                "contract_id": SELF_EVALUATION_CONTRACT_ID,
                "contract_version": SELF_EVALUATION_CONTRACT_VERSION,
                "contract_digest": SELF_EVALUATION_CONTRACT_DIGEST,
                "root_task_id": current.id,
                "manager_session_id": manager_session_id,
                "release_id": active_release.id,
                "policy_version": str(active_release.version),
                "policy_digest": active_release.policy_digest,
                "activation_id": active_activation.id,
                "activation_epoch": active_activation.epoch,
                "provider_id": provider_id,
                "executor_kind": executor_kind,
                "model_id": effective_model_id,
            }
            actual = supplied.model_dump(mode="json")
            mismatched = [key for key, value in expected_identity.items()
                          if actual.get(key) != value]
            if mismatched:
                raise ValueError(
                    "manager self-evaluation binding mismatch: "
                    + ", ".join(sorted(mismatched))
                )
            disposition = AuthorityDisposition(supplied.disposition)
            result = AuthorityEvaluationResult(
                disposition=disposition,
                disposition_code=(
                    AuthorityDispositionCode.CONTINUE_SAME_ROOT
                    if disposition == AuthorityDisposition.CONTINUE_SAME_ROOT
                    else AuthorityDispositionCode.ESCALATE
                ),
                response_digest=_sha256(json.dumps(actual, sort_keys=True)),
                clause_id=supplied.clause_id,
                action=supplied.action,
                confidence=supplied.confidence,
                uncertainty_codes=tuple(supplied.uncertainty_codes),
            )
        except Exception as exc:
            result = AuthorityEvaluationResult(
                disposition=AuthorityDisposition.EVALUATOR_ERROR,
                disposition_code=AuthorityDispositionCode.MALFORMED_OUTPUT,
                response_digest=_sha256(f"manager-self-evaluation-error:{exc}"),
                error=str(exc),
            )
    elif evaluator is None:
        result = AuthorityEvaluationResult(
            disposition=AuthorityDisposition.EVALUATOR_ERROR,
            disposition_code=AuthorityDispositionCode.EVALUATOR_ERROR,
            response_digest=_sha256("no-evaluator-configured"),
            error="no authority evaluator configured",
        )
    else:
        try:
            result = (
                evaluator.evaluate(snapshot, policy=policy)
                if active_activation is not None
                else evaluator.evaluate(snapshot)
            )
        except Exception as exc:
            result = AuthorityEvaluationResult(
                disposition=AuthorityDisposition.EVALUATOR_ERROR,
                disposition_code=AuthorityDispositionCode.EVALUATOR_ERROR,
                response_digest=_sha256(f"evaluator-raised:{exc}"),
                error=f"evaluator raised: {exc}",
            )
    verdict = _normalize_result(policy, candidate_id, input_digest, result)

    try:
        _authenticate_persisted_candidate_policy()
    except Exception as exc:
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_CAPTURE_FAILURE,
            policy=policy, snapshot=snapshot, candidate_id=candidate_id,
            fences=fences, lifecycle="created", verdict=verdict,
            error=f"pre-evaluation-commit pin authentication failed: {exc}",
        )
        return "escalate"

    # ---- 6b. SERVER-DERIVED must-escalate gate. A server-PROVEN fact
    # (adverse child review verdict, partial-work/zombie evidence, DB-schema
    # drift vs the release-pinned schema) forces ESCALATE regardless of the
    # evaluator verdict — neither misleading nor omitted reason prose can
    # authorize CONTINUE_SAME_ROOT. Additionally, the closed-pattern gate:
    # a CONTINUE_SAME_ROOT verdict is honored ONLY when the proposed reason
    # is a byte-exact member of the release-controlled routine phrase set
    # (the server then has complete knowledge of the prose). Any other prose
    # — a paraphrase that omits, misstates, or hides a protected boundary —
    # is not verifiable as routine and fails closed to ESCALATE. Neither the
    # closed pattern nor reason truthfulness is safety proof on its own: the
    # single-use continuation ENVELOPE (minted atomically with the commit and
    # enforced at the daemon-side decision acceptance point) is the
    # mechanical fence that restricts the continued turn.
    server_clause = _server_fact_clause(snapshot.structured_facts)
    if server_clause is not None:
        diagnostic_code = (
            verdict.disposition_code
            if verdict.disposition_code in _DIAGNOSTIC_FAILURE_CODES
            else AuthorityDispositionCode.ESCALATE
        )
        diagnostic_error = verdict.error
        server_error = f"server-derived must-escalate fact: {server_clause}"
        verdict = _NormalizedVerdict(
            AuthorityDisposition.ESCALATE,
            diagnostic_code,
            server_clause,
            ACTION_ESCALATE_TO_FOUNDER,
            verdict.confidence,
            verdict.uncertainty_codes,
            verdict.evidence_refs,
            verdict.rationale_digest,
            verdict.response_digest,
            error=(
                f"{diagnostic_error}; {server_error}"
                if diagnostic_error else server_error
            ),
        )
        if server_clause not in server_clauses:
            server_clauses.append(server_clause)
    elif (
        verdict.disposition == AuthorityDisposition.CONTINUE_SAME_ROOT
        and not _is_accepted_routine_reason(snapshot.reason)
    ):
        verdict = _NormalizedVerdict(
            AuthorityDisposition.ESCALATE,
            AuthorityDispositionCode.ESCALATE,
            "esc-ambiguity-novelty",
            ACTION_ESCALATE_TO_FOUNDER,
            verdict.confidence,
            verdict.uncertainty_codes,
            verdict.evidence_refs,
            verdict.rationale_digest,
            verdict.response_digest,
            error="continue verdict for a non-accepted reason (not a byte-exact "
            "release-controlled routine phrase)",
        )
        if "esc-ambiguity-novelty" not in server_clauses:
            server_clauses.append("esc-ambiguity-novelty")

    # ---- 7. Record the single immutable evaluation (atomic created->evaluated) ----
    try:
        db.record_authority_evaluation(
            candidate_id=candidate_id,
            disposition=verdict.disposition.value,
            disposition_code=verdict.disposition_code.value,
            response_digest=verdict.response_digest,
            fence_results={name: fr.model_dump(mode="json") for name, fr in fences.items()},
        )
    except Exception as exc:
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_EVALUATOR_FAILURE,
            policy=policy, snapshot=snapshot, candidate_id=candidate_id,
            fences=fences, lifecycle="created", verdict=verdict,
            error=f"evaluation record failed: {exc}",
        )
        return "escalate"

    # ---- 8. EVALUATION_RECORDED audit event ----
    try:
        db.record_authority_audit(
            candidate_id=candidate_id, event_type="evaluation_recorded",
            payload={
                "disposition": verdict.disposition.value,
                "disposition_code": verdict.disposition_code.value,
                "digest": verdict.response_digest,
                "version": policy.version,
            },
        )
    except Exception as exc:
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_AUDIT_FAILURE,
            policy=policy, snapshot=snapshot, candidate_id=candidate_id,
            fences=fences, lifecycle="evaluated", verdict=verdict,
            error=f"evaluation_recorded audit event failed: {exc}",
        )
        return "escalate"

    # ---- 8b. FULL fence re-check AFTER evaluation. Any coupled eligibility
    # category that changed while the evaluator ran (cancellation, budget
    # exhaustion, block/active-work, lineage, manager/session/team, etc.)
    # closes the window: the evaluation is still recorded (auditable) but it
    # can never continue. The authoritative atomic re-validation happens at
    # the final continuation CAS (step 11); this is the fail-fast pre-check.
    current = db.get_task(task.id)
    fresh_fences = _eligible_fences(orch, current, agent)
    fresh_failed = [n for n, fr in fresh_fences.items() if not fr.passed]
    # Permission/schema surface drift during the attempt is an authoritative
    # server predicate: the org permission digest and the live DB schema
    # digest captured in the snapshot must still match at this point, else a
    # permission/sandbox/allow-rule or schema change landed mid-evaluation
    # and the attempt fails closed with the matched clause.
    drift_clause = _during_attempt_drift_clause(orch, agent, snapshot)
    if _is_terminal_or_cancelled(current) or fresh_failed or drift_clause is not None:
        try:
            db.consume_authority_candidate(candidate_id)
        except Exception:
            pass
        drift_verdict = verdict
        if drift_clause is not None:
            drift_verdict = _NormalizedVerdict(
                AuthorityDisposition.ESCALATE,
                AuthorityDispositionCode.ESCALATE,
                drift_clause,
                ACTION_ESCALATE_TO_FOUNDER,
                verdict.confidence,
                verdict.uncertainty_codes,
                verdict.evidence_refs,
                verdict.rationale_digest,
                verdict.response_digest,
                error=f"protected surface changed during evaluation: {drift_clause}",
            )
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_CANCELLED_STALE,
            policy=policy, snapshot=snapshot, candidate_id=candidate_id,
            fences=fences, lifecycle="evaluated", verdict=drift_verdict,
            error=(
                f"protected surface changed during evaluation: {drift_clause}"
                if drift_clause is not None
                else (
                    "fence changed during evaluation: "
                    + ", ".join(sorted(fresh_failed))
                    if fresh_failed else "task cancelled/terminal during evaluation"
                )
            ),
        )
        return "escalate"

    # ---- 9. Final CAS: consume exactly once (evaluated -> consumed) ----
    try:
        consumed = db.consume_authority_candidate(candidate_id)
    except Exception as exc:
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_AUDIT_FAILURE,
            policy=policy, snapshot=snapshot, candidate_id=candidate_id,
            fences=fences, lifecycle="evaluated", verdict=verdict,
            error=f"candidate consume failed: {exc}",
        )
        return "escalate"
    if not consumed:
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_CAS_LOST,
            policy=policy, snapshot=snapshot, candidate_id=candidate_id,
            fences=fences, lifecycle="evaluated", verdict=verdict,
            error="candidate not consumed (restart-incomplete or already consumed)",
        )
        return "escalate"

    # ---- 10. CANDIDATE_CONSUMED audit event ----
    try:
        db.record_authority_audit(
            candidate_id=candidate_id, event_type="candidate_consumed",
            payload={
                "disposition": verdict.disposition.value,
                "disposition_code": verdict.disposition_code.value,
            },
        )
    except Exception as exc:
        _record_hook_outcome(
            db, task_id=task.id, agent=agent, outcome=OUTCOME_AUDIT_FAILURE,
            policy=policy, snapshot=snapshot, candidate_id=candidate_id,
            fences=fences, lifecycle="consumed", verdict=verdict,
            error=f"candidate_consumed audit event failed: {exc}",
        )
        return "escalate"

    # ---- 11. Execute the verdict ----
    if verdict.disposition == AuthorityDisposition.CONTINUE_SAME_ROOT:
        try:
            _authenticate_persisted_candidate_policy()
        except Exception as exc:
            _record_hook_outcome(
                db, task_id=task.id, agent=agent, outcome=OUTCOME_CAPTURE_FAILURE,
                policy=policy, snapshot=snapshot, candidate_id=candidate_id,
                fences=fences, lifecycle="consumed", verdict=verdict,
                error=f"continuation pin authentication failed: {exc}",
            )
            return "escalate"
        clause = policy.clause_by_id(verdict.clause_id)
        note = (
            f"authority-policy continued same root: "
            f"clause {clause.id} ({clause.action})"
        )
        outcome_payload: dict = {
            "outcome": OUTCOME_CONTINUED_SAME_ROOT,
            "candidate_id": candidate_id,
            "input_digest": input_digest,
            "reason_digest": reason_digest,
            "policy_id": policy.id,
            "policy_version": policy.version,
            "policy_digest": policy.digest,
            "prompt_id": snapshot.prompt_id,
            "prompt_version": snapshot.prompt_version,
            "prompt_digest": snapshot.prompt_digest,
            "model_id": snapshot.model_id,
            "model_version": snapshot.model_version,
            "model_digest": snapshot.model_digest,
            "causal_event_id": causal_event_id,
            "causal_event_digest": causal_event_digest,
            "causal_result_id": None,
            "disposition": verdict.disposition.value,
            "disposition_code": verdict.disposition_code.value,
            "clause_id": verdict.clause_id,
            "action": verdict.action,
            "confidence": verdict.confidence,
            "uncertainty_codes": list(verdict.uncertainty_codes),
            "evidence_refs": list(verdict.evidence_refs),
            "rationale_digest": verdict.rationale_digest,
            "response_digest": verdict.response_digest,
            "fence_results": {
                name: fr.model_dump(mode="json") for name, fr in fences.items()
            },
            "lifecycle": "consumed",
        }
        continue_payload = {
            "candidate_id": candidate_id,
            "policy_id": policy.id,
            "policy_version": policy.version,
            "policy_digest": policy.digest,
            "clause_id": verdict.clause_id,
            "action": verdict.action,
            "disposition_code": verdict.disposition_code.value,
            "envelope_id": f"CONT-{candidate_id}",
        }
        # Fresh ceilings at consumption time (release-controlled; may have
        # changed while the evaluator ran) — the DB recheck compares the
        # CURRENT task counters against these. ``current`` was re-fetched at
        # step 8b; ``current.status``/``block_kind`` are the expected values
        # for the atomic CAS.
        fresh_max_steps, fresh_revise_cap = _current_budget_ceilings(orch)
        try:
            committed = db.commit_authority_continue_same_root(
                task_id=task.id,
                candidate_id=candidate_id,
                expected_manager_agent=current.assigned_agent or agent,
                expected_session=current.current_session_id or "",
                expected_team=current.team,
                expected_policy_id=policy.id,
                expected_policy_version=policy.version,
                expected_policy_digest=policy.digest,
                expected_prompt_id=PROMPT_ID,
                expected_prompt_version=PROMPT_VERSION,
                expected_prompt_digest=PROMPT_DIGEST,
                expected_model_id=model_id,
                expected_model_version=model_version,
                expected_model_digest=model_digest,
                expected_input_digest=input_digest,
                expected_causal_event_id=causal_event_id,
                expected_max_orchestration_steps=fresh_max_steps,
                expected_max_revise_rounds=fresh_revise_cap,
                expected_status=current.status,
                expected_block_kind=current.block_kind,
                note=note,
                audit_agent=agent,
                authority_continue_payload=continue_payload,
                hook_outcome_payload=outcome_payload,
                envelope_clause_id=verdict.clause_id,
                envelope_action=verdict.action,
                envelope_causal_event_digest=causal_event_digest,
            )
        except Exception as exc:
            _record_hook_outcome(
                db, task_id=task.id, agent=agent, outcome=OUTCOME_AUDIT_FAILURE,
                policy=policy, snapshot=snapshot, candidate_id=candidate_id,
                fences=fences, lifecycle="consumed", verdict=verdict,
                error=f"same-root continuation commit failed: {exc}",
            )
            return "escalate"
        if not committed:
            # Cancellation/stale won the final CAS: no continuation.
            _record_hook_outcome(
                db, task_id=task.id, agent=agent, outcome=OUTCOME_CANCELLED_STALE,
                policy=policy, snapshot=snapshot, candidate_id=candidate_id,
                fences=fences, lifecycle="consumed", verdict=verdict,
                error="same-root continuation CAS lost (cancelled/stale)",
            )
            return "escalate"
        # Re-enqueue the root for its next manager decision step. Best-effort:
        # the run_step claim CAS keeps at-most-once admission if a replay lands.
        queue = getattr(orch, "_queue", None)
        if queue is not None:
            queue.put_nowait(orch._slug, task.id)
        return "continue_same_root"

    # ESCALATE (fail-closed default): the existing escalation path proceeds.
    _record_hook_outcome(
        db, task_id=task.id, agent=agent, outcome=OUTCOME_ESCALATED,
        policy=policy, snapshot=snapshot, candidate_id=candidate_id,
        fences=fences, lifecycle="consumed", verdict=verdict,
        error=verdict.error,
    )
    return "escalate"
