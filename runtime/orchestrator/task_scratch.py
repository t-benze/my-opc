"""Runtime-owned, non-destructive per-task temporary containment.

This module creates scratch and records observations.  It deliberately has no
cleanup API: manifests are evidence, never deletion authority (TASK-6501).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

_TASK_ID = re.compile(r"[A-Z][A-Z0-9-]{0,63}(?<!-)\Z")
_PRODUCER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ENV_KEYS = ("TMPDIR", "TMP", "TEMP", "HAPPYRANCH_TASK_TMP_ROOT", "HAPPYRANCH_TASK_SCRATCH_MANIFEST")
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_PRODUCERS = 128
MANIFEST_VERSION = 1
_MANIFEST_KEYS = {
    "version", "task_id", "required_root", "observed_root",
    "root_classification", "manifest_classification", "lock_classification",
    "producers",
}
_PRODUCER_KEYS = {
    "producer_kind", "producer_id", "required", "observed",
    "classification", "observed_at",
}


class TaskScratchError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskScratch:
    task_id: str
    producer_kind: str
    producer_id: str
    root: Path
    manifest_path: Path
    inherited_temp: tuple[tuple[str, str | None], ...]


_ACTIVE: ContextVar[TaskScratch | None] = ContextVar("task_scratch", default=None)


def _validate_component(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise TaskScratchError(f"invalid {label}: {value!r}")
    return value


def _mkdir_owned(path: Path, *, parent: Path | None = None) -> None:
    if path.is_symlink():
        raise TaskScratchError(f"scratch path is a symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise TaskScratchError(f"scratch path is not a directory: {path}")
    if parent is not None and path.resolve().parent != parent.resolve():
        raise TaskScratchError(f"scratch path escaped canonical parent: {path}")
    os.chmod(path, 0o700)


def validate_task_scratch_manifest(
    data: object, *, expected_task_id: str, expected_root: Path,
) -> list[dict[str, object]]:
    """Validate the exact bounded version-1 manifest and producer shape."""
    if not isinstance(data, Mapping) or set(data) != _MANIFEST_KEYS:
        raise TaskScratchError("manifest is corrupt")
    if type(data["version"]) is not int or data["version"] != MANIFEST_VERSION:
        raise TaskScratchError("manifest is corrupt")
    scalar_contract = {
        "task_id": _TASK_ID,
        "required_root": None,
        "observed_root": None,
    }
    for field, pattern in scalar_contract.items():
        value = data[field]
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise TaskScratchError("manifest is corrupt")
        if pattern is not None and not pattern.fullmatch(value):
            raise TaskScratchError("manifest is corrupt")
    canonical_root = str(expected_root)
    if (
        data["task_id"] != expected_task_id
        or data["required_root"] != canonical_root
        or data["observed_root"] != canonical_root
    ):
        raise TaskScratchError("manifest is corrupt")
    if (
        data["root_classification"] != "regenerable_scratch"
        or data["manifest_classification"] != "durable_recovery_artifact"
        or data["lock_classification"] != "durable_recovery_artifact"
    ):
        raise TaskScratchError("manifest is corrupt")
    producers = data["producers"]
    if not isinstance(producers, list) or len(producers) > _MAX_PRODUCERS:
        raise TaskScratchError("manifest is corrupt")
    for producer in producers:
        if not isinstance(producer, Mapping) or set(producer) != _PRODUCER_KEYS:
            raise TaskScratchError("manifest is corrupt")
        for field in ("producer_kind", "producer_id"):
            value = producer[field]
            if not isinstance(value, str) or not _PRODUCER_ID.fullmatch(value):
                raise TaskScratchError("manifest is corrupt")
        required = producer["required"]
        observed = producer["observed"]
        if (
            not isinstance(required, Mapping)
            or set(required) != {"canonical_root", "ownership"}
            or required["canonical_root"] != canonical_root
            or required["ownership"] != "runtime"
            or not isinstance(observed, Mapping)
            or set(observed) != {"canonical_root", "mode"}
            or observed["canonical_root"] != canonical_root
            or observed["mode"] != "0700"
            or producer["classification"] != "regenerable_scratch"
        ):
            raise TaskScratchError("manifest is corrupt")
        observed_at = producer["observed_at"]
        if not isinstance(observed_at, str) or len(observed_at) > 64:
            raise TaskScratchError("manifest is corrupt")
        try:
            timestamp = datetime.fromisoformat(observed_at)
        except ValueError as exc:
            raise TaskScratchError("manifest is corrupt") from exc
        if timestamp.tzinfo is None:
            raise TaskScratchError("manifest is corrupt")
    return [dict(producer) for producer in producers]


def prepare_task_scratch(
    *, workspace: Path, task_id: str, producer_kind: str, producer_id: str,
) -> TaskScratch:
    """Create the canonical root and atomically append a bounded observation."""
    task_id = _validate_component(task_id, _TASK_ID, "task_id")
    producer_kind = _validate_component(producer_kind, _PRODUCER_ID, "producer_kind")
    producer_id = _validate_component(producer_id, _PRODUCER_ID, "producer_id")
    workspace = Path(workspace).resolve(strict=True)
    owned = workspace / ".happyranch"
    scratch_parent = owned / "task-tmp"
    manifest_parent = owned / "task-scratch-manifests"
    for path in (owned, scratch_parent, manifest_parent):
        _mkdir_owned(path)
    root = scratch_parent / task_id
    _mkdir_owned(root, parent=scratch_parent)
    manifest_path = manifest_parent / f"{task_id}.json"
    contract = TaskScratch(
        task_id, producer_kind, producer_id, root, manifest_path,
        tuple((key, os.environ.get(key)) for key in ("TMPDIR", "TMP", "TEMP")),
    )
    _write_observation(contract)
    return contract


def _write_observation(contract: TaskScratch) -> None:
    import fcntl

    lock_path = contract.manifest_path.with_suffix(".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current: dict = {}
            if contract.manifest_path.exists():
                if contract.manifest_path.is_symlink() or not contract.manifest_path.is_file():
                    raise TaskScratchError("manifest is not a regular file")
                raw = contract.manifest_path.read_bytes()
                if len(raw) > _MAX_MANIFEST_BYTES:
                    raise TaskScratchError("manifest exceeds bounded size")
                try:
                    current = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise TaskScratchError("manifest is corrupt") from exc
                if not isinstance(current, Mapping):
                    raise TaskScratchError("manifest is corrupt")
                if current.get("version") != MANIFEST_VERSION or current.get("task_id") != contract.task_id:
                    raise TaskScratchError("manifest identity/version mismatch")
                producers = validate_task_scratch_manifest(
                    current,
                    expected_task_id=contract.task_id,
                    expected_root=contract.root,
                )
            else:
                producers = []
            observation = {
                "producer_kind": contract.producer_kind,
                "producer_id": contract.producer_id,
                "required": {"canonical_root": str(contract.root), "ownership": "runtime"},
                "observed": {"canonical_root": str(contract.root), "mode": "0700"},
                "classification": "regenerable_scratch",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            producers = [p for p in producers if not (
                p.get("producer_kind") == contract.producer_kind
                and p.get("producer_id") == contract.producer_id
            )]
            producers.append(observation)
            if len(producers) > _MAX_PRODUCERS:
                raise TaskScratchError("manifest producer bound exceeded")
            payload = {
                "version": MANIFEST_VERSION,
                "task_id": contract.task_id,
                "required_root": str(contract.root),
                "observed_root": str(contract.root),
                "root_classification": "regenerable_scratch",
                "manifest_classification": "durable_recovery_artifact",
                "lock_classification": "durable_recovery_artifact",
                "producers": producers,
            }
            encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
            if len(encoded) > _MAX_MANIFEST_BYTES:
                raise TaskScratchError("manifest exceeds bounded size")
            tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{contract.task_id}.", dir=contract.manifest_path.parent)
            try:
                os.fchmod(tmp_fd, 0o600)
                with os.fdopen(tmp_fd, "wb") as tmp:
                    tmp.write(encoded)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, contract.manifest_path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
    except OSError as exc:
        raise TaskScratchError(f"scratch manifest write failed: {exc}") from exc


def activate_task_scratch(contract: TaskScratch) -> Token:
    return _ACTIVE.set(contract)


def reset_task_scratch(token: Token) -> None:
    _ACTIVE.reset(token)


def apply_task_scratch_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Inject active containment, rejecting inherited override/sidecar escapes."""
    result = dict(env)
    contract = _ACTIVE.get()
    if contract is None:
        return result
    inherited = dict(contract.inherited_temp)
    conflicts = sorted(
        key for key in ("TMPDIR", "TMP", "TEMP")
        if result.get(key) != inherited.get(key)
    )
    conflicts.extend(
        key for key in ("HAPPYRANCH_TASK_TMP_ROOT", "HAPPYRANCH_TASK_SCRATCH_MANIFEST")
        if result.get(key)
    )
    if conflicts:
        raise TaskScratchError("inherited task containment override refused: " + ", ".join(conflicts))
    root = str(contract.root)
    result.update({
        "TMPDIR": root,
        "TMP": root,
        "TEMP": root,
        "HAPPYRANCH_TASK_TMP_ROOT": root,
        "HAPPYRANCH_TASK_SCRATCH_MANIFEST": str(contract.manifest_path),
    })
    return result


def observe_task_scratch_manifest(*, workspace: Path, task_id: str) -> dict[str, object]:
    """Report-only manifest observation; never repairs, removes, or authorizes."""
    try:
        task_id = _validate_component(task_id, _TASK_ID, "task_id")
    except TaskScratchError as exc:
        return {"status": "invalid", "reason": str(exc)}
    workspace = Path(workspace).resolve()
    expected_root = workspace / ".happyranch" / "task-tmp" / task_id
    path = workspace / ".happyranch" / "task-scratch-manifests" / f"{task_id}.json"
    if not path.exists():
        return {"status": "missing", "path": str(path)}
    try:
        if path.is_symlink() or not path.is_file():
            raise TaskScratchError("manifest is not a regular file")
        raw = path.read_bytes()
        if len(raw) > _MAX_MANIFEST_BYTES:
            raise TaskScratchError("manifest exceeds bounded size")
        data = json.loads(raw)
        if not isinstance(data, Mapping):
            raise TaskScratchError("manifest is corrupt")
        if data.get("version") != MANIFEST_VERSION or data.get("task_id") != task_id:
            return {"status": "stale", "path": str(path)}
        validate_task_scratch_manifest(
            data,
            expected_task_id=task_id,
            expected_root=expected_root,
        )
        return {"status": "ok", "path": str(path), "manifest": data}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, TaskScratchError) as exc:
        return {"status": "corrupt", "path": str(path), "reason": str(exc)}
