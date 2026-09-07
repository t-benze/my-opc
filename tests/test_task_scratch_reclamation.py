from __future__ import annotations

import json
import os
import inspect
import threading
from dataclasses import replace
from pathlib import Path

import pytest
import runtime.daemon.task_scratch_reclamation as reclamation

from runtime.daemon.task_scratch_reclamation import (
    CoverageAssertions, EvidencePlatform, LifecycleAssertions, LivenessAssertions,
    ReclamationAssertions, ReclamationError, ZombieRecoveryState, execute_ledger, seal_ledger_row,
)
from runtime.orchestrator.task_scratch import prepare_task_scratch


def _candidate(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    contract = prepare_task_scratch(workspace=workspace, task_id="TASK-1",
                                    producer_kind="agent", producer_id="sess-1")
    (contract.root / "nested").mkdir()
    (contract.root / "nested/file").write_bytes(b"payload")
    old = 1_700_000_000_000_000_000
    for path in (contract.root / "nested/file", contract.root / "nested", contract.root):
        os.utime(path, ns=(old, old), follow_symlinks=False)
    lifecycle = LifecycleAssertions(
        "completed", "terminal:v1", old + 120_000_000_000, 0,
        ZombieRecoveryState.CLEAR, 0, 0, 0, True, False, False, False,
    )
    liveness = LivenessAssertions(
        "complete-proc-scan", EvidencePlatform.LINUX, "boot-1", "boot-1",
        True, False, False, False, False, 0, 0, 0, 0,
    )
    coverage = CoverageAssertions(
        "boot-coverage-1", "a" * 64, "boot-1", "boot-1",
        True, False, False, False, 0, 0, 0,
    )
    evidence = ReclamationAssertions("dev_agent", lifecycle, liveness, coverage)
    return workspace, contract, evidence, old


def test_removes_only_literal_root_and_preserves_sidecars(tmp_path):
    workspace, contract, evidence, old = _candidate(tmp_path)
    manifest_before = contract.manifest_path.read_bytes()
    lock_before = contract.manifest_path.with_suffix(".lock").read_bytes()
    sibling = prepare_task_scratch(workspace=workspace, task_id="TASK-2",
                                   producer_kind="agent", producer_id="sess-2")
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                          now_ns=old + 121_000_000_000)
    result, = execute_ledger((row,))
    assert result.outcome == "completed"
    assert (result.reclaimed_bytes, result.reclaimed_inodes) == (
        row.before.allocated_bytes, row.before.inodes)
    assert not contract.root.exists()
    assert contract.manifest_path.read_bytes() == manifest_before
    assert contract.manifest_path.with_suffix(".lock").read_bytes() == lock_before
    assert sibling.root.is_dir()


@pytest.mark.parametrize("field,value", [
    ("task_status", "in_progress"),
    ("nonterminal_revisits", 1),
    ("zombie_recovery", ZombieRecoveryState.PENDING),
    ("zombie_recovery", ZombieRecoveryState.AMBIGUOUS),
    ("active_jobs", 1),
    ("pending_recovery_consumers", 1),
    ("active_chain_members", 1),
    ("truncated", True),
    ("ambiguous", True),
    ("unavailable", True),
])
def test_incomplete_lifecycle_assertions_fail_before_ledger(field, value, tmp_path):
    workspace, _, assertions, old = _candidate(tmp_path)
    with pytest.raises(ReclamationError, match="lifecycle assertions"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1",
                        assertions=replace(assertions, lifecycle=replace(
                            assertions.lifecycle, **{field: value})),
                        now_ns=old + 121_000_000_000)


@pytest.mark.parametrize("field,value", [
    ("complete", False), ("truncated", True), ("ambiguous", True),
    ("permission_denied", True), ("live_sessions", 1), ("process_roots", 1),
    ("process_cwds", 1), ("open_fds", 1), ("observed_boot_id", "stale-boot"),
])
def test_liveness_ambiguity_and_live_references_fail_closed(field, value, tmp_path):
    workspace, _, evidence, old = _candidate(tmp_path)
    with pytest.raises(ReclamationError, match="liveness"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1",
                        assertions=replace(evidence, liveness=replace(evidence.liveness, **{field: value})),
                        now_ns=old + 121_000_000_000)


@pytest.mark.parametrize("field,value", [
    ("complete", False), ("truncated", True), ("ambiguous", True), ("unavailable", True),
    ("dominant_unclassified", 1), ("dominant_durable", 1),
    ("dominant_recovery", 1), ("observed_boot_id", "stale-boot"),
])
def test_coverage_missing_stale_truncated_or_ambiguous_fails_closed(field, value, tmp_path):
    workspace, _, evidence, old = _candidate(tmp_path)
    with pytest.raises(ReclamationError, match="coverage"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1",
                        assertions=replace(evidence, coverage=replace(evidence.coverage, **{field: value})),
                        now_ns=old + 121_000_000_000)


@pytest.mark.parametrize("shape", [
    LifecycleAssertions, LivenessAssertions, CoverageAssertions,
])
def test_assertion_shapes_have_no_defaults(shape):
    assert all(parameter.default is inspect.Parameter.empty
               for parameter in inspect.signature(shape).parameters.values())


@pytest.mark.parametrize("component,field,value,error", [
    ("lifecycle", "terminal_assertion", "", "lifecycle"),
    ("lifecycle", "terminal_observed_at_ns", True, "lifecycle"),
    ("lifecycle", "nonterminal_revisits", True, "lifecycle"),
    ("lifecycle", "zombie_recovery", "clear", "lifecycle"),
    ("liveness", "census_assertion", "", "liveness"),
    ("liveness", "complete", 1, "liveness"),
    ("liveness", "unavailable", True, "liveness"),
    ("coverage", "coverage_assertion", "", "coverage"),
    ("coverage", "digest_assertion", "g" * 64, "coverage"),
    ("coverage", "dominant_durable", True, "coverage"),
])
def test_malformed_or_contradictory_assertions_fail_closed(
        component, field, value, error, tmp_path):
    workspace, _, assertions, old = _candidate(tmp_path)
    changed = replace(getattr(assertions, component), **{field: value})
    with pytest.raises(ReclamationError, match=error):
        seal_ledger_row(workspace=workspace, task_id="TASK-1",
                        assertions=replace(assertions, **{component: changed}),
                        now_ns=old + 121_000_000_000)


def test_assertion_provenance_boundary_is_honest(tmp_path):
    workspace, _, assertions, old = _candidate(tmp_path)
    fabricated = replace(
        assertions,
        lifecycle=replace(assertions.lifecycle, terminal_assertion="caller-made"),
        liveness=replace(assertions.liveness, census_assertion="caller-made"),
        coverage=replace(assertions.coverage, coverage_assertion="caller-made",
                         digest_assertion="0" * 64),
    )
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=fabricated,
                          now_ns=old + 121_000_000_000)
    assert row.terminal_assertion == "caller-made"
    assert "untrusted" in (ReclamationAssertions.__doc__ or "").lower()
    assert not any(name.endswith("Evidence") or name == "AuthorityEvidence"
                   for name in vars(reclamation))


def test_unsupported_platform_and_cross_evidence_boot_fail_closed(tmp_path):
    workspace, _, evidence, old = _candidate(tmp_path)
    with pytest.raises(ReclamationError, match="liveness"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1",
                        assertions=replace(evidence, liveness=replace(
                            evidence.liveness, platform="windows")),  # type: ignore[arg-type]
                        now_ns=old + 121_000_000_000)
    with pytest.raises(ReclamationError, match="boot mismatch"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1",
                        assertions=replace(evidence, coverage=replace(
                            evidence.coverage, boot_id="boot-2", observed_boot_id="boot-2")),
                        now_ns=old + 121_000_000_000)


def test_exact_mtime_boundary_and_terminal_order(tmp_path):
    workspace, _, evidence, old = _candidate(tmp_path)
    evidence = replace(evidence, lifecycle=replace(
        evidence.lifecycle, terminal_observed_at_ns=old))
    assert seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                           now_ns=old + 60_000_000_000).newest_mtime_ns == old
    with pytest.raises(ReclamationError, match="mtime"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                        now_ns=old + 59_999_999_999)
    with pytest.raises(ReclamationError, match="mtime"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1",
                        assertions=replace(evidence, lifecycle=replace(
                            evidence.lifecycle, terminal_observed_at_ns=old - 1)),
                        now_ns=old + 60_000_000_000)


def test_manifest_path_mismatch_and_git_evidence_fail_closed(tmp_path):
    workspace, contract, evidence, old = _candidate(tmp_path)
    contract.manifest_path.write_text(contract.manifest_path.read_text().replace(
        str(contract.root), "/attacker/root"))
    with pytest.raises(ReclamationError, match="manifest invalid"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                        now_ns=old + 121_000_000_000)


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_manifest_symlink_or_fifo_fails_closed(kind, tmp_path):
    workspace, contract, assertions, old = _candidate(tmp_path)
    manifest = contract.manifest_path
    manifest.unlink()
    if kind == "symlink":
        target = tmp_path / "outside-manifest"
        target.write_text("{}")
        manifest.symlink_to(target)
    else:
        os.mkfifo(manifest)
    with pytest.raises(ReclamationError):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=assertions,
                        now_ns=old + 121_000_000_000)


def test_root_symlink_and_task_prefix_confusion_fail_closed(tmp_path):
    workspace, contract, assertions, old = _candidate(tmp_path)
    moved = contract.root.with_name("TASK-10")
    contract.root.rename(moved)
    contract.root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ReclamationError, match="literal directory"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=assertions,
                        now_ns=old + 121_000_000_000)
    assert moved.is_dir()


def test_repository_stat_ambiguity_fails_closed(monkeypatch, tmp_path):
    workspace, contract, assertions, old = _candidate(tmp_path)
    original = Path.stat

    def denied(path, *args, **kwargs):
        if path == contract.root / ".git":
            raise PermissionError("injected")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(ReclamationError, match="repository classification unavailable"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=assertions,
                        now_ns=old + 121_000_000_000)


def test_census_cap_and_read_failure_fail_closed(monkeypatch, tmp_path):
    workspace, _, assertions, old = _candidate(tmp_path)
    monkeypatch.setattr(reclamation, "MAX_CENSUS_ENTRIES", 1)
    with pytest.raises(ReclamationError, match="cap"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=assertions,
                        now_ns=old + 121_000_000_000)
    monkeypatch.setattr(reclamation, "MAX_CENSUS_ENTRIES", 100_000)
    original = os.scandir

    def unavailable(path):
        if Path(path).name == "nested":
            raise TimeoutError("injected timeout")
        return original(path)

    monkeypatch.setattr(os, "scandir", unavailable)
    with pytest.raises(ReclamationError, match="census unavailable"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=assertions,
                        now_ns=old + 121_000_000_000)


@pytest.mark.parametrize("location", ["root", "descendant", "ancestor"])
def test_repository_and_worktree_signatures_fail_closed(location, tmp_path):
    workspace, contract, evidence, old = _candidate(tmp_path)
    target = {"root": contract.root, "descendant": contract.root / "nested",
              "ancestor": workspace}[location]
    (target / ".git").write_text("gitdir: /hostile/worktree")
    with pytest.raises(ReclamationError, match="repository"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                        now_ns=old + 121_000_000_000)


def test_bare_repository_signature_fails_closed(tmp_path):
    workspace, contract, evidence, old = _candidate(tmp_path)
    (contract.root / "HEAD").write_text("ref: refs/heads/main\n")
    (contract.root / "objects").mkdir()
    (contract.root / "refs").mkdir()
    with pytest.raises(ReclamationError, match="repository"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                        now_ns=old + 121_000_000_000)


@pytest.mark.parametrize("mutation", [
    "missing", "corrupt", "oversized", "unknown", "stale-version", "producer-overflow",
    "nonregular",
])
def test_hostile_manifest_shapes_fail_closed(mutation, tmp_path):
    workspace, contract, evidence, old = _candidate(tmp_path)
    manifest = contract.manifest_path
    if mutation == "missing":
        manifest.unlink()
    elif mutation == "corrupt":
        manifest.write_text("{")
    elif mutation == "oversized":
        manifest.write_bytes(b"x" * (64 * 1024 + 1))
    elif mutation == "unknown":
        data = json.loads(manifest.read_text())
        data["hostile"] = True
        manifest.write_text(json.dumps(data))
    elif mutation == "stale-version":
        data = json.loads(manifest.read_text())
        data["version"] = 0
        manifest.write_text(json.dumps(data))
    elif mutation == "producer-overflow":
        data = json.loads(manifest.read_text())
        data["producers"] = data["producers"] * 129
        manifest.write_text(json.dumps(data))
    else:
        manifest.unlink()
        manifest.mkdir()
    with pytest.raises(ReclamationError):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                        now_ns=old + 121_000_000_000)
    workspace, contract, evidence, old = _candidate(tmp_path / "second")
    (contract.root / ".git").mkdir()
    os.utime(contract.root / ".git", ns=(old, old))
    with pytest.raises(ReclamationError, match="git"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                        now_ns=old + 121_000_000_000)


def test_symlink_and_fifo_are_unlinked_without_following(tmp_path):
    workspace, contract, evidence, old = _candidate(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("protected")
    (contract.root / "link").symlink_to(outside)
    os.mkfifo(contract.root / "pipe")
    for path in (contract.root / "link", contract.root / "pipe", contract.root):
        os.utime(path, ns=(old, old), follow_symlinks=False)
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                          now_ns=old + 121_000_000_000)
    result, = execute_ledger((row,))
    assert result.outcome == "completed"
    assert outside.read_text() == "protected"


def test_post_finalization_mutation_is_rejected_without_reclaimed_claim(tmp_path):
    workspace, contract, evidence, old = _candidate(tmp_path)
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                          now_ns=old + 121_000_000_000)
    (contract.root / "late").write_text("race")
    result, = execute_ledger((row,))
    assert result.outcome == "failed"
    assert result.reclaimed_bytes == result.reclaimed_inodes == 0
    assert contract.root.exists()


@pytest.mark.parametrize("target", ["manifest", "lock", "parent-entry", "workspace"])
def test_protected_path_mutation_fails_with_zero_claim(target, tmp_path):
    workspace, contract, assertions, old = _candidate(tmp_path)
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=assertions,
                          now_ns=old + 121_000_000_000)
    if target == "manifest":
        contract.manifest_path.write_text(contract.manifest_path.read_text() + " ")
    elif target == "lock":
        contract.manifest_path.with_suffix(".lock").write_text("changed")
    elif target == "parent-entry":
        (contract.root.parent / "late-sibling").mkdir()
    else:
        workspace.rename(tmp_path / "moved-workspace")
    result, = execute_ledger((row,))
    assert result.outcome == "failed"
    assert result.reclaimed_bytes == result.reclaimed_inodes == 0


def test_accounting_mismatch_and_unavailable_after_are_zero_claim(monkeypatch, tmp_path):
    workspace, contract, assertions, old = _candidate(tmp_path)
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=assertions,
                          now_ns=old + 121_000_000_000)
    original = reclamation._census
    calls = 0

    def mismatched(root, **kwargs):
        nonlocal calls
        calls += 1
        entries, accounting = original(root, **kwargs)
        if calls == 1:
            return entries, replace(accounting, allocated_bytes=accounting.allocated_bytes + 1)
        contract.root.rename(contract.root.with_name("unavailable-after"))
        raise ReclamationError("after unavailable")

    monkeypatch.setattr(reclamation, "_census", mismatched)
    result, = execute_ledger((row,))
    assert result.outcome == "failed"
    assert result.after is None
    assert result.reclaimed_bytes == result.reclaimed_inodes == 0


def test_final_root_rename_recreate_cannot_report_completed(monkeypatch, tmp_path):
    workspace, contract, evidence, old = _candidate(tmp_path)
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                          now_ns=old + 121_000_000_000)
    original_rmdir = os.rmdir
    renamed = contract.root.with_name("renamed-sealed-root")

    def hostile_rmdir(path, *args, **kwargs):
        if path == row.task_id and kwargs.get("dir_fd") is not None:
            os.rename(contract.root, renamed)
            contract.root.mkdir()
        return original_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", hostile_rmdir)
    result, = execute_ledger((row,))
    assert result.outcome == "failed"
    assert result.reclaimed_bytes == result.reclaimed_inodes == 0
    assert renamed.exists()


@pytest.mark.parametrize("entry_kind", ["file", "directory"])
def test_final_pathname_swap_is_characterized_without_false_success(
        entry_kind, monkeypatch, tmp_path):
    """Portable unlink/rmdir is name-bound, not inode-bound.

    A hostile same-UID replacement in the final syscall window is deliberately
    outside the threat contract.  Once the displaced ledger entry makes the
    mismatch detectable, the row must still fail with zero reclaimed claims.
    """
    workspace, contract, evidence, old = _candidate(tmp_path)
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                          now_ns=old + 121_000_000_000)
    nested = contract.root / "nested"

    if entry_kind == "file":
        original_action = os.unlink
        original = nested / "file"
        displaced = nested / "displaced-file"

        def hostile_action(path, *args, **kwargs):
            if path == "file" and kwargs.get("dir_fd") is not None:
                original.rename(displaced)
                original.write_bytes(b"hostile replacement")
            return original_action(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", hostile_action)
    else:
        original_action = os.rmdir
        displaced = contract.root / "displaced-directory"

        def hostile_action(path, *args, **kwargs):
            if path == "nested" and kwargs.get("dir_fd") is not None:
                nested.rename(displaced)
                nested.mkdir()
            return original_action(path, *args, **kwargs)

        monkeypatch.setattr(os, "rmdir", hostile_action)

    result, = execute_ledger((row,))
    assert result.outcome == "failed"
    assert result.reclaimed_bytes == result.reclaimed_inodes == 0
    assert displaced.exists()
    # The same-name replacement can be removed by the final pathname syscall;
    # its survival is intentionally not promised by the portable contract.
    assert not (nested / "file" if entry_kind == "file" else nested).exists()


def test_concurrent_finalizers_yield_one_completion_and_one_failure(tmp_path):
    workspace, _, evidence, old = _candidate(tmp_path)
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                          now_ns=old + 121_000_000_000)
    barrier = threading.Barrier(2)
    results = []

    def run():
        barrier.wait()
        results.extend(execute_ledger((row,)))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(result.outcome for result in results) == ["completed", "failed"]


def test_per_row_failure_isolated_and_protected_sibling_addition_detected(tmp_path):
    workspace, first, evidence, old = _candidate(tmp_path)
    second = prepare_task_scratch(workspace=workspace, task_id="TASK-2",
                                  producer_kind="agent", producer_id="sess-2")
    os.utime(second.root, ns=(old, old))
    first_row = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                                now_ns=old + 121_000_000_000)
    (first.root.parent / "hostile-sibling").mkdir()
    second_row = seal_ledger_row(workspace=workspace, task_id="TASK-2", assertions=evidence,
                                 now_ns=old + 121_000_000_000)
    first_result, second_result = execute_ledger((first_row, second_row))
    assert first_result.outcome == "failed"
    assert second_result.outcome == "completed"


def test_partial_syscall_failure_requires_fresh_ledger_retry(monkeypatch, tmp_path):
    workspace, contract, evidence, old = _candidate(tmp_path)
    (contract.root / "nested/a").write_text("first")
    (contract.root / "nested/z").write_text("last")
    for path in (contract.root / "nested/a", contract.root / "nested/z",
                 contract.root / "nested", contract.root):
        os.utime(path, ns=(old, old))
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                          now_ns=old + 121_000_000_000)
    original_unlink = os.unlink

    def fail_last(path, *args, **kwargs):
        if path == "z":
            raise OSError("injected partial syscall")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_last)
    failed, = execute_ledger((row,))
    assert failed.outcome == "failed"
    assert failed.reclaimed_bytes == failed.reclaimed_inodes == 0
    replay, = execute_ledger((row,))
    assert replay.outcome == "failed"
    monkeypatch.setattr(os, "unlink", original_unlink)
    for path in (contract.root / "nested/file", contract.root / "nested/z",
                 contract.root / "nested", contract.root):
        if path.exists():
            os.utime(path, ns=(old, old))
    fresh = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                            now_ns=old + 121_000_000_000)
    retried, = execute_ledger((fresh,))
    assert retried.outcome == "completed"


def test_replayed_row_is_rejected(tmp_path):
    workspace, _, evidence, old = _candidate(tmp_path)
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                          now_ns=old + 121_000_000_000)
    first, replay = execute_ledger((row, row))
    assert first.outcome == "completed"
    assert replay.reason == "replayed ledger"


def test_forged_or_cross_root_ledger_is_rejected(tmp_path):
    workspace, _, evidence, old = _candidate(tmp_path)
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", assertions=evidence,
                          now_ns=old + 121_000_000_000)
    forged = replace(row, literal_root=str(tmp_path))
    result, = execute_ledger((forged,))
    assert result.outcome == "failed"
    assert tmp_path.is_dir()


def test_module_has_no_production_importers():
    root = Path(__file__).parents[1]
    assert [path for path in (root / "runtime").rglob("*.py")
            if path.name != "task_scratch_reclamation.py"
            and "task_scratch_reclamation" in path.read_text()] == []
