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
MAX_CENSUS_ENTRIES = 100_000


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
class LifecycleAssertions:
    """Untrusted caller assertions; B1 does not establish their provenance."""

    task_status: str
    terminal_assertion: str
    terminal_observed_at_ns: int
    nonterminal_revisits: int
    zombie_recovery: ZombieRecoveryState
    pending_recovery_consumers: int
    active_jobs: int
    active_chain_members: int
    complete: bool
    truncated: bool
    ambiguous: bool
    unavailable: bool

    def validate(self) -> None:
        counts = (self.nonterminal_revisits, self.pending_recovery_consumers,
                  self.active_jobs, self.active_chain_members)
        if (type(self.complete) is not bool or type(self.truncated) is not bool
                or type(self.ambiguous) is not bool or type(self.unavailable) is not bool
                or not self.complete or self.truncated or self.ambiguous or self.unavailable
                or self.task_status not in {"completed", "failed", "cancelled"}
                or not isinstance(self.terminal_assertion, str)
                or not self.terminal_assertion.strip()
                or type(self.terminal_observed_at_ns) is not int
                or self.terminal_observed_at_ns < 0
                or any(type(value) is not int or value < 0 for value in counts)
                or self.nonterminal_revisits or self.zombie_recovery is not ZombieRecoveryState.CLEAR
                or self.pending_recovery_consumers or self.active_jobs or self.active_chain_members):
            raise ReclamationError("lifecycle assertions are ineligible")


@dataclass(frozen=True)
class LivenessAssertions:
    """Untrusted caller shape for a future B2 action-time census producer."""

    census_assertion: str
    platform: EvidencePlatform
    boot_id: str
    observed_boot_id: str
    complete: bool
    truncated: bool
    ambiguous: bool
    permission_denied: bool
    unavailable: bool
    live_sessions: int
    process_roots: int
    process_cwds: int
    open_fds: int

    def validate(self) -> None:
        counts = (self.live_sessions, self.process_roots, self.process_cwds, self.open_fds)
        flags = (self.complete, self.truncated, self.ambiguous,
                 self.permission_denied, self.unavailable)
        if (any(type(value) is not bool for value in flags)
                or not isinstance(self.census_assertion, str)
                or not self.census_assertion.strip()
                or not isinstance(self.platform, EvidencePlatform)
                or not self.boot_id or self.boot_id != self.observed_boot_id
                or any(type(value) is not int or value < 0 for value in counts)
                or not self.complete or self.truncated or self.ambiguous
                or self.permission_denied or self.unavailable
                or self.live_sessions or self.process_roots or self.process_cwds or self.open_fds):
            raise ReclamationError("liveness assertions are ineligible")


@dataclass(frozen=True)
class CoverageAssertions:
    """Untrusted caller shape for a future B3 current-boot coverage producer."""

    coverage_assertion: str
    digest_assertion: str
    boot_id: str
    observed_boot_id: str
    complete: bool
    truncated: bool
    ambiguous: bool
    unavailable: bool
    dominant_unclassified: int
    dominant_durable: int
    dominant_recovery: int

    def validate(self) -> None:
        counts = (self.dominant_unclassified, self.dominant_durable, self.dominant_recovery)
        flags = (self.complete, self.truncated, self.ambiguous, self.unavailable)
        if (any(type(value) is not bool for value in flags)
                or not isinstance(self.coverage_assertion, str)
                or not self.coverage_assertion.strip()
                or not isinstance(self.digest_assertion, str)
                or len(self.digest_assertion) != 64
                or any(character not in "0123456789abcdef"
                       for character in self.digest_assertion)
                or not isinstance(self.boot_id, str) or not self.boot_id.strip()
                or not isinstance(self.observed_boot_id, str) or not self.observed_boot_id.strip()
                or any(type(value) is not int or value < 0 for value in counts)
                or self.boot_id != self.observed_boot_id or not self.complete or self.truncated
                or self.ambiguous or self.unavailable or self.dominant_unclassified
                or self.dominant_durable or self.dominant_recovery):
            raise ReclamationError("coverage assertions are ineligible")


@dataclass(frozen=True)
class ReclamationAssertions:
    """Untrusted caller-constructible assertions, never authoritative/non-forgeable proof."""

    agent_name: str
    lifecycle: LifecycleAssertions
    liveness: LivenessAssertions
    coverage: CoverageAssertions

    def validate(self) -> None:
        if not isinstance(self.agent_name, str) or not self.agent_name.strip():
            raise ReclamationError("agent assertion unavailable")
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
    terminal_assertion: str
    liveness_assertion: str
    coverage_assertion: str
    coverage_digest_assertion: str
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
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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


def _census(root: Path, *, max_entries: int | None = None) -> tuple[tuple[FileIdentity, ...], Accounting]:
    found: list[FileIdentity] = []
    limit = MAX_CENSUS_ENTRIES if max_entries is None else max_entries
    if type(limit) is not int or limit < 1:
        raise ReclamationError("invalid census cap")

    def visit(path: Path, relative: str) -> None:
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise ReclamationError("census unavailable") from exc
        found.append(FileIdentity(relative, info.st_dev, info.st_ino, info.st_mode,
                                  info.st_mtime_ns, info.st_blocks * 512, info.st_size))
        if len(found) > limit:
            raise ReclamationError("census entry cap exceeded")
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


def seal_ledger_row(*, workspace: Path, task_id: str, assertions: ReclamationAssertions,
                    now_ns: int, mtime_floor_ns: int = MTIME_FLOOR_NS) -> LedgerRow:
    """Validate untrusted shapes and re-derive a row; B1 proves no assertion provenance."""
    assertions.validate()
    if type(now_ns) is not int or type(mtime_floor_ns) is not int or mtime_floor_ns < 0:
        raise ReclamationError("invalid time assertions")
    if now_ns < assertions.lifecycle.terminal_observed_at_ns:
        raise ReclamationError("terminal assertion is from the future")
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
    if newest > assertions.lifecycle.terminal_observed_at_ns or now_ns - newest < mtime_floor_ns:
        raise ReclamationError("mtime floor not met")
    siblings = tuple(sorted((_identity(Path(item.path)) for item in os.scandir(parent)
                             if item.name != task_id), key=lambda item: item.path))
    protected = (_identity(workspace), _identity(owned), _identity(parent), _identity(manifests),
                 _identity(manifest, digest=True), _identity(lock, digest=True), *siblings)
    manifest_digest = hashlib.sha256(raw).hexdigest()
    payload: Mapping[str, object] = {
        "task_id": task_id, "agent": assertions.agent_name, "root": str(root),
        "manifest_digest": manifest_digest, "device": root_info.st_dev, "inode": root_info.st_ino,
        "terminal": assertions.lifecycle.terminal_assertion,
        "liveness": assertions.liveness.census_assertion,
        "coverage": assertions.coverage.coverage_assertion,
        "coverage_digest": assertions.coverage.digest_assertion,
        "entries": [x.__dict__ for x in entries], "protected": [x.__dict__ for x in protected],
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return LedgerRow(task_id, assertions.agent_name, str(root), str(manifest), str(lock), manifest_digest,
                     root_info.st_dev, root_info.st_ino, newest,
                     assertions.lifecycle.terminal_assertion,
                     assertions.liveness.census_assertion,
                     assertions.coverage.coverage_assertion,
                     assertions.coverage.digest_assertion,
                     entries, before, protected, fingerprint)


def _has_repository_evidence(root: Path, workspace: Path) -> bool:
    """Recognize ordinary, linked-worktree, and bare Git repositories without following links."""
    def signature(path: Path) -> bool:
        try:
            git = path / ".git"
            try:
                git.stat(follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                return True
            head = path / "HEAD"
            objects = path / "objects"
            refs = path / "refs"
            try:
                head_info = head.stat(follow_symlinks=False)
            except FileNotFoundError:
                return False
            return (stat.S_ISREG(head_info.st_mode)
                    and stat.S_ISDIR(objects.stat(follow_symlinks=False).st_mode)
                    and stat.S_ISDIR(refs.stat(follow_symlinks=False).st_mode))
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ReclamationError("repository classification unavailable") from exc

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
        "terminal": row.terminal_assertion, "liveness": row.liveness_assertion,
        "coverage": row.coverage_assertion,
        "coverage_digest": row.coverage_digest_assertion,
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
