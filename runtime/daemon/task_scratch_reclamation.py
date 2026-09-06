"""Dormant, production-unreferenced task-scratch reclamation primitives."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
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


class EvidencePlatform(StrEnum):
    LINUX = "linux"
    DARWIN = "darwin"


class ZombieRecoveryState(StrEnum):
    CLEAR = "clear"
    PENDING = "pending"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class LifecycleEvidence:
    """Bounded projection of existing task/revisit/zombie/job authority."""

    task_status: str
    terminal_fingerprint: str
    terminal_observed_at_ns: int
    nonterminal_revisits: int = 0
    zombie_recovery: ZombieRecoveryState = ZombieRecoveryState.CLEAR
    pending_recovery_consumers: int = 0
    active_jobs: int = 0
    active_chain_members: int = 0
    complete: bool = True

    def validate(self) -> None:
        counts = (self.nonterminal_revisits, self.pending_recovery_consumers,
                  self.active_jobs, self.active_chain_members)
        if (not self.complete or self.task_status not in {"completed", "failed", "cancelled"}
                or not self.terminal_fingerprint or self.terminal_observed_at_ns < 0
                or any(not isinstance(value, int) or value < 0 for value in counts)
                or self.nonterminal_revisits or self.zombie_recovery is not ZombieRecoveryState.CLEAR
                or self.pending_recovery_consumers or self.active_jobs or self.active_chain_members):
            raise ReclamationError("authoritative lifecycle evidence is ineligible")


@dataclass(frozen=True)
class LivenessEvidence:
    """Action-time complete process/session reference census."""

    receipt: str
    platform: EvidencePlatform
    boot_id: str
    observed_boot_id: str
    complete: bool = True
    truncated: bool = False
    ambiguous: bool = False
    permission_denied: bool = False
    live_sessions: int = 0
    process_roots: int = 0
    process_cwds: int = 0
    open_fds: int = 0

    def validate(self) -> None:
        counts = (self.live_sessions, self.process_roots, self.process_cwds, self.open_fds)
        if (not self.receipt or not isinstance(self.platform, EvidencePlatform)
                or not self.boot_id or self.boot_id != self.observed_boot_id
                or any(not isinstance(value, int) or value < 0 for value in counts)
                or not self.complete or self.truncated or self.ambiguous or self.permission_denied
                or self.live_sessions or self.process_roots or self.process_cwds or self.open_fds):
            raise ReclamationError("authoritative liveness evidence is ineligible")


@dataclass(frozen=True)
class CoverageEvidence:
    """Current-boot bounded dominant-consumer classification receipt."""

    receipt: str
    digest: str
    boot_id: str
    observed_boot_id: str
    complete: bool = True
    truncated: bool = False
    ambiguous: bool = False
    unavailable: bool = False
    dominant_unclassified: int = 0
    dominant_durable: int = 0
    dominant_recovery: int = 0

    def validate(self) -> None:
        counts = (self.dominant_unclassified, self.dominant_durable, self.dominant_recovery)
        if (not self.receipt or len(self.digest) != 64 or not self.boot_id
                or any(not isinstance(value, int) or value < 0 for value in counts)
                or self.boot_id != self.observed_boot_id or not self.complete or self.truncated
                or self.ambiguous or self.unavailable or self.dominant_unclassified
                or self.dominant_durable or self.dominant_recovery):
            raise ReclamationError("current-boot coverage evidence is ineligible")


@dataclass(frozen=True)
class AuthorityEvidence:
    agent_name: str
    lifecycle: LifecycleEvidence
    liveness: LivenessEvidence
    coverage: CoverageEvidence

    def validate(self) -> None:
        if not self.agent_name:
            raise ReclamationError("agent identity unavailable")
        self.lifecycle.validate()
        self.liveness.validate()
        self.coverage.validate()
        if self.liveness.boot_id != self.coverage.boot_id:
            raise ReclamationError("evidence boot mismatch")


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
    evidence.validate()
    if now_ns < evidence.lifecycle.terminal_observed_at_ns:
        raise ReclamationError("terminal evidence is from the future")
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
    if _has_repository_evidence(root, workspace):
        raise ReclamationError("git or repository evidence")
    newest = max(item.mtime_ns for item in entries)
    if newest > evidence.lifecycle.terminal_observed_at_ns or now_ns - newest < mtime_floor_ns:
        raise ReclamationError("mtime floor not met")
    siblings = tuple(sorted((_identity(Path(item.path)) for item in os.scandir(parent)
                             if item.name != task_id), key=lambda item: item.path))
    protected = (_identity(workspace), _identity(owned), _identity(parent), _identity(manifests),
                 _identity(manifest, digest=True), _identity(lock, digest=True), *siblings)
    manifest_digest = hashlib.sha256(raw).hexdigest()
    payload: Mapping[str, object] = {
        "task_id": task_id, "agent": evidence.agent_name, "root": str(root),
        "manifest_digest": manifest_digest, "device": root_info.st_dev, "inode": root_info.st_ino,
        "terminal": evidence.lifecycle.terminal_fingerprint, "liveness": evidence.liveness.receipt,
        "coverage": evidence.coverage.receipt, "coverage_digest": evidence.coverage.digest,
        "entries": [x.__dict__ for x in entries], "protected": [x.__dict__ for x in protected],
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return LedgerRow(task_id, evidence.agent_name, str(root), str(manifest), str(lock), manifest_digest,
                     root_info.st_dev, root_info.st_ino, newest, evidence.lifecycle.terminal_fingerprint,
                     evidence.liveness.receipt, evidence.coverage.receipt, evidence.coverage.digest,
                     entries, before, protected, fingerprint)


def _has_repository_evidence(root: Path, workspace: Path) -> bool:
    """Recognize ordinary, linked-worktree, and bare Git repositories without following links."""
    def signature(path: Path) -> bool:
        try:
            git = path / ".git"
            if git.exists(follow_symlinks=False):
                return True
            head = path / "HEAD"
            objects = path / "objects"
            refs = path / "refs"
            head_info = head.stat(follow_symlinks=False)
            return (stat.S_ISREG(head_info.st_mode)
                    and stat.S_ISDIR(objects.stat(follow_symlinks=False).st_mode)
                    and stat.S_ISDIR(refs.stat(follow_symlinks=False).st_mode))
        except OSError:
            return False

    cursor = root
    while True:
        if signature(cursor):
            return True
        if cursor == workspace:
            break
        if workspace not in cursor.parents:
            raise ReclamationError("repository ancestor classification escaped workspace")
        cursor = cursor.parent
    for path, directories, _files in os.walk(root, topdown=True, followlinks=False):
        candidate = Path(path)
        if signature(candidate):
            return True
        directories[:] = [name for name in directories
                           if not (candidate / name).is_symlink()]
    return False


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


def _verify_parent_entries(row: LedgerRow, parent_fd: int, *, root_absent: bool) -> None:
    expected = {Path(item.path).name: item for item in row.protected[6:]}
    names = set(os.listdir(parent_fd))
    required = set(expected)
    if not root_absent:
        required.add(row.task_id)
    if names != required:
        raise ReclamationError("protected parent directory entries changed")
    for name, identity in expected.items():
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (identity.device, identity.inode):
            raise ReclamationError("protected sibling changed")


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
            parent_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                _verify_parent_entries(row, parent_fd, root_absent=False)
                fd = os.open(row.task_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=parent_fd)
                try:
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) != (row.root_device, row.root_inode):
                        raise ReclamationError("literal root changed")
                    _remove_dir(fd, {x.relative_path: x for x in row.entries if x.relative_path}, "",
                                row.root_device)
                    linked = os.stat(row.task_id, dir_fd=parent_fd, follow_symlinks=False)
                    if (linked.st_dev, linked.st_ino) != (row.root_device, row.root_inode):
                        raise ReclamationError("literal root directory entry changed")
                    os.rmdir(row.task_id, dir_fd=parent_fd)
                    if os.fstat(fd).st_nlink != 0:
                        raise ReclamationError("sealed root inode survived final removal")
                finally:
                    os.close(fd)
                _verify_parent_entries(row, parent_fd, root_absent=True)
            finally:
                os.close(parent_fd)
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
