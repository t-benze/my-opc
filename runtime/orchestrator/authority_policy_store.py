"""Dark persistence boundary for immutable DB-backed authority policy.

S1 intentionally has no production caller.  The shipped authority hook keeps
using ``Database.claim_authority_candidate`` for legacy/static-policy attempts,
which truthfully creates no sidecar pin.  A later integration slice will call
this store only after resolving an active DB release and activation.
"""

from __future__ import annotations

import base64
import json

from runtime.infrastructure.database import Database
from runtime.models import (
    AuthorityCandidate,
    AuthorityCandidatePolicyPin,
    AuthorityPolicyActivation,
    AuthorityPolicyRelease,
)


class AuthorityPolicyStore:
    """Typed, test-callable facade over the transaction-owning DB methods."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create_release(self, release: AuthorityPolicyRelease) -> AuthorityPolicyRelease:
        return self._db.create_authority_policy_release(release)

    def create_release_with_audit(
        self,
        release: AuthorityPolicyRelease,
        *,
        request_id: str,
        request_digest: str,
    ) -> AuthorityPolicyRelease:
        return self._db.create_authority_policy_release_with_audit(
            release,
            request_id=request_id,
            request_digest=request_digest,
        )

    def activate(self, activation: AuthorityPolicyActivation) -> AuthorityPolicyActivation:
        receipt = AuthorityPolicyActivation.model_validate(
            activation.model_dump(mode="python", round_trip=True, warnings=False)
        )
        return self._db.activate_authority_policy(receipt)

    def activate_with_audit(
        self,
        *,
        team: str,
        release_id: str,
        expected_previous_epoch: int,
        action: str,
        request_id: str,
        request_digest: str,
    ) -> AuthorityPolicyActivation:
        return self._db.activate_authority_policy_with_audit(
            team=team,
            release_id=release_id,
            expected_previous_epoch=expected_previous_epoch,
            action=action,
            request_id=request_id,
            request_digest=request_digest,
        )

    def get_release(self, release_id: str) -> AuthorityPolicyRelease | None:
        return self._db.get_authority_policy_release(release_id)

    def next_release_version(self, team: str, policy_id: str) -> int:
        return self._db.get_next_authority_policy_release_version(team, policy_id)

    def get_activation(self, activation_id: str) -> AuthorityPolicyActivation | None:
        return self._db.get_authority_policy_activation(activation_id)

    def get_current_activation(self, team: str) -> AuthorityPolicyActivation | None:
        return self._db.get_current_authority_policy_activation(team)

    @staticmethod
    def _encode_cursor(payload: dict) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str, *, stream: str) -> dict:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload = json.loads(raw)
        except Exception as exc:
            raise ValueError("invalid pagination cursor") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("stream") != stream:
            raise ValueError("invalid pagination cursor")
        return payload

    @staticmethod
    def _cursor_int(payload: dict, key: str) -> int:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid pagination cursor")
        return value

    @staticmethod
    def _cursor_str(payload: dict, key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError("invalid pagination cursor")
        return value

    def list_history(self, team: str, *, cursor: str | None, limit: int) -> tuple[list[dict], str | None]:
        """Return an immutable-snapshot, deterministic newest-first page."""
        if cursor is None:
            snapshot_version, snapshot_epoch = self._db.get_authority_policy_history_snapshot(team)
            after = None
        else:
            token = self._decode_cursor(cursor, stream="history")
            if token.get("team") != team:
                raise ValueError("invalid pagination cursor")
            try:
                snapshot_version = self._cursor_int(token, "sv")
                snapshot_epoch = self._cursor_int(token, "se")
                after = (
                    self._cursor_int(token, "av"), self._cursor_int(token, "ae"),
                    self._cursor_str(token, "ar"), token.get("aa"),
                )
                if not isinstance(after[3], str) or len(after[3]) > 256:
                    raise ValueError("invalid pagination cursor")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid pagination cursor") from exc
        rows = self._db.list_authority_policy_history(
            team, snapshot_version=snapshot_version, snapshot_epoch=snapshot_epoch,
            after_version=None if after is None else after[0],
            after_epoch=None if after is None else after[1],
            after_release_id=None if after is None else after[2],
            after_activation_id=None if after is None else after[3], limit=limit + 1,
        )
        page = rows[:limit]
        if len(rows) <= limit or not page:
            return page, None
        last = page[-1]
        return page, self._encode_cursor({
            "v": 1, "stream": "history", "team": team,
            "sv": snapshot_version, "se": snapshot_epoch,
            "av": last["version"], "ae": last["epoch"] or 0,
            "ar": last["release_id"], "aa": last["activation_id"] or "",
        })

    def list_outcomes(self, team: str, *, cursor: str | None, limit: int) -> tuple[list[dict], str | None]:
        """Return secret-free receipts from a stable initial snapshot."""
        if cursor is None:
            snapshot = self._db.get_authority_policy_outcomes_snapshot(team)
            if snapshot is None:
                return [], None
            after = None
        else:
            token = self._decode_cursor(cursor, stream="outcomes")
            if token.get("team") != team:
                raise ValueError("invalid pagination cursor")
            try:
                snapshot = (self._cursor_str(token, "sc"), self._cursor_str(token, "si"))
                after = (self._cursor_str(token, "ac"), self._cursor_str(token, "ai"))
            except (KeyError, TypeError) as exc:
                raise ValueError("invalid pagination cursor") from exc
        rows = self._db.list_authority_policy_outcomes(
            team, snapshot_created_at=snapshot[0], snapshot_id=snapshot[1],
            after_created_at=None if after is None else after[0],
            after_id=None if after is None else after[1], limit=limit + 1,
        )
        page = rows[:limit]
        if len(rows) <= limit or not page:
            return page, None
        last = page[-1]
        return page, self._encode_cursor({
            "v": 1, "stream": "outcomes", "team": team,
            "sc": snapshot[0], "si": snapshot[1],
            "ac": last["created_at"], "ai": last["id"],
        })

    def get_candidate_pin(self, candidate_id: str) -> AuthorityCandidatePolicyPin | None:
        return self._db.get_authority_candidate_policy_pin(candidate_id)

    def claim_candidate_with_pin(
        self,
        *,
        release_id: str,
        activation_id: str,
        activation_epoch: int,
        provider_id: str,
        executor_kind: str,
        **candidate_kwargs,
    ) -> tuple[AuthorityCandidate, AuthorityCandidatePolicyPin]:
        return self._db.claim_authority_candidate_with_policy_pin(
            release_id=release_id,
            activation_id=activation_id,
            activation_epoch=activation_epoch,
            provider_id=provider_id,
            executor_kind=executor_kind,
            **candidate_kwargs,
        )
