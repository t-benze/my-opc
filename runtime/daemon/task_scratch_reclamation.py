"""Dormant, production-unreferenced task-scratch reclamation primitives."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from runtime.orchestrator.task_scratch import TaskScratchError, validate_task_scratch_manifest

MTIME_FLOOR_NS = 60_000_000_000


class ReclamationError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileIdentity:
    relative_path: str
    device: int
    inode: int
    mode: int
    mtime_ns: int
    allocated_bytes: int
    apparent_bytes: int


@dataclass(frozen=True)
class Accounting:
    allocated_bytes: int
    apparent_bytes: int
    inodes: int


@dataclass(frozen=True)
class AuthorityEvidence:
    agent_name: str
    terminal_fingerprint: str
    terminal_observed_at_ns: int
    lifecycle_clear: bool
    liveness_receipt: str
    liveness_complete: bool
    coverage_receipt: str
    coverage_digest: str
    coverage_permits_deletion: bool


@dataclass(frozen=True)
class ProtectedIdentity:
    path: str
    device: int
    inode: int
    digest: str | None


@dataclass(frozen=True)
class LedgerRow:
    task_id: str
    agent_name: str
    literal_root: str
    manifest_path: str
    lock_path: str
    manifest_digest: str
    root_device: int
    root_inode: int
    newest_mtime_ns: int
    terminal_fingerprint: str
    liveness_receipt: str
    coverage_receipt: str
    coverage_digest: str
    entries: tuple[FileIdentity, ...]
    before: Accounting
    protected: tuple[ProtectedIdentity, ...]
    fingerprint: str


@dataclass(frozen=True)
class ReclamationResult:
    task_id: str
    outcome: str
    reason: str | None
    before: Accounting
    after: Accounting | None
    reclaimed_bytes: int
    reclaimed_inodes: int


def _regular_bytes(path: Path) -> tuple[bytes, os.stat_result]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ReclamationError("protected path is not regular")
            return handle.read(64 * 1024 + 1), info
    except OSError as exc:
        raise ReclamationError("protected path unavailable") from exc


def _identity(path: Path, *, digest: bool = False) -> ProtectedIdentity:
    try:
        if digest:
            raw, info = _regular_bytes(path)
            if len(raw) > 64 * 1024:
                raise ReclamationError("manifest exceeds bounded size")
            checksum = hashlib.sha256(raw).hexdigest()
        else:
            info = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise ReclamationError("protected path is a symlink")
            checksum = None
    except OSError as exc:
        raise ReclamationError("protected path unavailable") from exc
    return ProtectedIdentity(str(path), info.st_dev, info.st_ino, checksum)


def _census(root: Path) -> tuple[tuple[FileIdentity, ...], Accounting]:
    found: list[FileIdentity] = []

    def visit(path: Path, relative: str) -> None:
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise ReclamationError("census unavailable") from exc
        found.append(FileIdentity(relative, info.st_dev, info.st_ino, info.st_mode,
                                  info.st_mtime_ns, info.st_blocks * 512, info.st_size))
        if stat.S_ISDIR(info.st_mode):
            try:
                children = sorted(os.scandir(path), key=lambda item: item.name)
            except OSError as exc:
                raise ReclamationError("census unavailable") from exc
            for child in children:
                rel = child.name if not relative else f"{relative}/{child.name}"
                visit(Path(child.path), rel)

    visit(root, "")
    entries = tuple(found)
    return entries, Accounting(sum(x.allocated_bytes for x in entries),
                               sum(x.apparent_bytes for x in entries), len(entries))


def seal_ledger_row(*, workspace: Path, task_id: str, evidence: AuthorityEvidence,
                    now_ns: int, mtime_floor_ns: int = MTIME_FLOOR_NS) -> LedgerRow:
    """Re-derive a canonical row at action time; never trust manifest paths."""
    if not evidence.lifecycle_clear or not evidence.liveness_complete:
        raise ReclamationError("authoritative lifecycle or liveness evidence is incomplete")
    if not evidence.coverage_permits_deletion:
        raise ReclamationError("current-boot coverage does not permit deletion")
    try:
        workspace = Path(workspace).resolve(strict=True)
    except OSError as exc:
        raise ReclamationError("workspace unavailable") from exc
    if not task_id.startswith("TASK-") or not task_id[5:].isdigit():
        raise ReclamationError("noncanonical task id")
    owned = workspace / ".happyranch"
    parent = owned / "task-tmp"
    root = parent / task_id
    manifests = owned / "task-scratch-manifests"
    manifest = manifests / f"{task_id}.json"
    lock = manifests / f"{task_id}.lock"
    try:
        root_info = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise ReclamationError("canonical root unavailable") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ReclamationError("canonical root is not a literal directory")
    if root.resolve().parent != parent.resolve(strict=True) or root_info.st_dev != parent.stat().st_dev:
        raise ReclamationError("canonical root escaped or crossed device")
    raw, _ = _regular_bytes(manifest)
    if len(raw) > 64 * 1024:
        raise ReclamationError("manifest exceeds bounded size")
    try:
        data = json.loads(raw)
        producers = validate_task_scratch_manifest(data, expected_task_id=task_id, expected_root=root)
    except (json.JSONDecodeError, UnicodeDecodeError, TaskScratchError) as exc:
        raise ReclamationError("manifest invalid") from exc
    if not producers:
        raise ReclamationError("manifest has no producer evidence")
    entries, before = _census(root)
    if any(item.device != root_info.st_dev for item in entries):
        raise ReclamationError("cross-device entry")
    if any(Path(item.relative_path).name == ".git" for item in entries if item.relative_path):
        raise ReclamationError("git or repository evidence")
    newest = max(item.mtime_ns for item in entries)
    if newest > evidence.terminal_observed_at_ns or now_ns - newest < mtime_floor_ns:
        raise ReclamationError("mtime floor not met")
    siblings = tuple(sorted((_identity(Path(item.path)) for item in os.scandir(parent)
                             if item.name != task_id), key=lambda item: item.path))
    protected = (_identity(workspace), _identity(owned), _identity(parent), _identity(manifests),
                 _identity(manifest, digest=True), _identity(lock, digest=True), *siblings)
    manifest_digest = hashlib.sha256(raw).hexdigest()
    payload: Mapping[str, object] = {
        "task_id": task_id, "agent": evidence.agent_name, "root": str(root),
        "manifest_digest": manifest_digest, "device": root_info.st_dev, "inode": root_info.st_ino,
        "terminal": evidence.terminal_fingerprint, "liveness": evidence.liveness_receipt,
        "coverage": evidence.coverage_receipt, "coverage_digest": evidence.coverage_digest,
        "entries": [x.__dict__ for x in entries], "protected": [x.__dict__ for x in protected],
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return LedgerRow(task_id, evidence.agent_name, str(root), str(manifest), str(lock), manifest_digest,
                     root_info.st_dev, root_info.st_ino, newest, evidence.terminal_fingerprint,
                     evidence.liveness_receipt, evidence.coverage_receipt, evidence.coverage_digest,
                     entries, before, protected, fingerprint)


def _verify_ledger_identity(row: LedgerRow) -> None:
    if len(row.protected) < 6:
        raise ReclamationError("malformed ledger")
    workspace = Path(row.protected[0].path)
    canonical_root = workspace / ".happyranch" / "task-tmp" / row.task_id
    canonical_manifest = workspace / ".happyranch" / "task-scratch-manifests" / f"{row.task_id}.json"
    canonical_lock = canonical_manifest.with_suffix(".lock")
    if (Path(row.literal_root), Path(row.manifest_path), Path(row.lock_path)) != (
            canonical_root, canonical_manifest, canonical_lock):
        raise ReclamationError("cross-root ledger")
    payload: Mapping[str, object] = {
        "task_id": row.task_id, "agent": row.agent_name, "root": row.literal_root,
        "manifest_digest": row.manifest_digest, "device": row.root_device, "inode": row.root_inode,
        "terminal": row.terminal_fingerprint, "liveness": row.liveness_receipt,
        "coverage": row.coverage_receipt, "coverage_digest": row.coverage_digest,
        "entries": [x.__dict__ for x in row.entries], "protected": [x.__dict__ for x in row.protected],
    }
    actual = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if actual != row.fingerprint:
        raise ReclamationError("ledger fingerprint mismatch")


def _verify_protected(row: LedgerRow) -> None:
    for expected in row.protected:
        if _identity(Path(expected.path), digest=expected.digest is not None) != expected:
            raise ReclamationError("protected path changed")


def _remove_dir(fd: int, expected: Mapping[str, FileIdentity], prefix: str, device: int) -> None:
    names = sorted(os.listdir(fd))
    direct = sorted(path for path in expected if path and "/" not in path)
    if names != direct:
        raise ReclamationError("ledger changed after finalization")
    for name in names:
        relative = name if not prefix else f"{prefix}/{name}"
        item = expected[name]
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns) != (
                item.device, item.inode, item.mode, item.mtime_ns) or info.st_dev != device:
            raise ReclamationError("ledger entry changed")
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                descendants = {key[len(name) + 1:]: value for key, value in expected.items()
                               if key.startswith(name + "/")}
                _remove_dir(child_fd, descendants, relative, device)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)


def execute_ledger(rows: tuple[LedgerRow, ...]) -> tuple[ReclamationResult, ...]:
    """Consume finalized rows only; each row fails closed and independently."""
    results: list[ReclamationResult] = []
    seen: set[str] = set()
    for row in rows:
        if row.fingerprint in seen:
            results.append(ReclamationResult(row.task_id, "failed", "replayed ledger", row.before, None, 0, 0))
            continue
        seen.add(row.fingerprint)
        try:
            _verify_ledger_identity(row)
            root = Path(row.literal_root)
            current, accounting = _census(root)
            if current != row.entries or accounting != row.before:
                raise ReclamationError("ledger changed after finalization")
            _verify_protected(row)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != (row.root_device, row.root_inode):
                    raise ReclamationError("literal root changed")
                _remove_dir(fd, {x.relative_path: x for x in row.entries if x.relative_path}, "", row.root_device)
            finally:
                os.close(fd)
            os.rmdir(root)
            _verify_protected(row)
            results.append(ReclamationResult(row.task_id, "completed", None, row.before,
                                              Accounting(0, 0, 0), row.before.allocated_bytes,
                                              row.before.inodes))
        except (OSError, ReclamationError):
            try:
                _, after = _census(Path(row.literal_root))
            except ReclamationError:
                after = None
            results.append(ReclamationResult(row.task_id, "failed", "fail-closed filesystem error",
                                              row.before, after, 0, 0))
    return tuple(results)
