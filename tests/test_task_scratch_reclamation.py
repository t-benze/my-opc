from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from runtime.daemon.task_scratch_reclamation import (
    AuthorityEvidence, ReclamationError, execute_ledger, seal_ledger_row,
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
    evidence = AuthorityEvidence("dev_agent", "terminal:v1", old + 120_000_000_000, True,
                                 "complete-proc-scan", True, "boot-coverage-1", "a" * 64, True)
    return workspace, contract, evidence, old


def test_removes_only_literal_root_and_preserves_sidecars(tmp_path):
    workspace, contract, evidence, old = _candidate(tmp_path)
    manifest_before = contract.manifest_path.read_bytes()
    lock_before = contract.manifest_path.with_suffix(".lock").read_bytes()
    sibling = prepare_task_scratch(workspace=workspace, task_id="TASK-2",
                                   producer_kind="agent", producer_id="sess-2")
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", evidence=evidence,
                          now_ns=old + 121_000_000_000)
    result, = execute_ledger((row,))
    assert result.outcome == "completed"
    assert (result.reclaimed_bytes, result.reclaimed_inodes) == (
        row.before.allocated_bytes, row.before.inodes)
    assert not contract.root.exists()
    assert contract.manifest_path.read_bytes() == manifest_before
    assert contract.manifest_path.with_suffix(".lock").read_bytes() == lock_before
    assert sibling.root.is_dir()


@pytest.mark.parametrize("field", ["lifecycle_clear", "liveness_complete", "coverage_permits_deletion"])
def test_incomplete_authority_fails_before_ledger(field, tmp_path):
    workspace, _, evidence, old = _candidate(tmp_path)
    with pytest.raises(ReclamationError):
        seal_ledger_row(workspace=workspace, task_id="TASK-1",
                        evidence=replace(evidence, **{field: False}),
                        now_ns=old + 121_000_000_000)


def test_exact_mtime_boundary_and_terminal_order(tmp_path):
    workspace, _, evidence, old = _candidate(tmp_path)
    assert seal_ledger_row(workspace=workspace, task_id="TASK-1", evidence=evidence,
                           now_ns=old + 60_000_000_000).newest_mtime_ns == old
    with pytest.raises(ReclamationError, match="mtime"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", evidence=evidence,
                        now_ns=old + 59_999_999_999)
    with pytest.raises(ReclamationError, match="mtime"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1",
                        evidence=replace(evidence, terminal_observed_at_ns=old - 1),
                        now_ns=old + 60_000_000_000)


def test_manifest_path_mismatch_and_git_evidence_fail_closed(tmp_path):
    workspace, contract, evidence, old = _candidate(tmp_path)
    contract.manifest_path.write_text(contract.manifest_path.read_text().replace(
        str(contract.root), "/attacker/root"))
    with pytest.raises(ReclamationError, match="manifest invalid"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", evidence=evidence,
                        now_ns=old + 121_000_000_000)
    workspace, contract, evidence, old = _candidate(tmp_path / "second")
    (contract.root / ".git").mkdir()
    os.utime(contract.root / ".git", ns=(old, old))
    with pytest.raises(ReclamationError, match="git"):
        seal_ledger_row(workspace=workspace, task_id="TASK-1", evidence=evidence,
                        now_ns=old + 121_000_000_000)


def test_symlink_and_fifo_are_unlinked_without_following(tmp_path):
    workspace, contract, evidence, old = _candidate(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("protected")
    (contract.root / "link").symlink_to(outside)
    os.mkfifo(contract.root / "pipe")
    for path in (contract.root / "link", contract.root / "pipe", contract.root):
        os.utime(path, ns=(old, old), follow_symlinks=False)
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", evidence=evidence,
                          now_ns=old + 121_000_000_000)
    result, = execute_ledger((row,))
    assert result.outcome == "completed"
    assert outside.read_text() == "protected"


def test_post_finalization_mutation_is_rejected_without_reclaimed_claim(tmp_path):
    workspace, contract, evidence, old = _candidate(tmp_path)
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", evidence=evidence,
                          now_ns=old + 121_000_000_000)
    (contract.root / "late").write_text("race")
    result, = execute_ledger((row,))
    assert result.outcome == "failed"
    assert result.reclaimed_bytes == result.reclaimed_inodes == 0
    assert contract.root.exists()


def test_replayed_row_is_rejected(tmp_path):
    workspace, _, evidence, old = _candidate(tmp_path)
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", evidence=evidence,
                          now_ns=old + 121_000_000_000)
    first, replay = execute_ledger((row, row))
    assert first.outcome == "completed"
    assert replay.reason == "replayed ledger"


def test_forged_or_cross_root_ledger_is_rejected(tmp_path):
    workspace, _, evidence, old = _candidate(tmp_path)
    row = seal_ledger_row(workspace=workspace, task_id="TASK-1", evidence=evidence,
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
