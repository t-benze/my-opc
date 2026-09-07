#!/usr/bin/env python3
"""Machine-verifiable evidence ledger for the Managed N3 production proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

SCHEMA = "happyranch.managed-n3.execution-evidence"
VERSION = 4
DIAGNOSTIC_PHASES = {
    "credential_input": "input_acquisition",
    "engine_start": "engine_initialization",
    "network_join": "peer_establishment",
    "durable_commit": "receipt_commit",
}
DIAGNOSTIC_ACTORS = {"systemd", "tsnet-sidecar"}
DIAGNOSTIC_UNITS = {"happyranch-tsnet-sidecar.service"}
SECRET_MARKERS = ("token", "authkey", "nodekey", "credential=", "/run/credentials/", "/etc/happyranch/", "provider error")
DENIAL_OPERATIONS = (
    "address_family_netlink", "linux_capabilities", "device_access",
    "writable_paths", "control_plane_operations",
)
DENIAL_RESULTS = {"allow", "deny", "unknown"}
DENIAL_CATEGORIES = {"none", "permission_denied", "unavailable", "timeout", "operational_error"}
DENIAL_ERRNOS = {None, "EACCES", "EPERM", "ENOENT", "ENODEV", "EAFNOSUPPORT", "ETIMEDOUT", "ECONNREFUSED", "EIO", "OTHER"}
PHASES = {
    "startup": ("process_absent", "tsnet_admission_absent", "connector_staged_credential_service_readable_non_writable", "sidecar_staged_credential_service_readable_non_writable", "credential_source_retired", "credential_dropin_retired", "composite_ready_after_sidecar", "missing_consumed_state_failed_closed"),
    "admission": ("tsnet_admission_reachable",),
    "active_flow": ("production_process_active", "watchdog_composite_current", "watchdog_ceased_on_sidecar_loss"),
    "readiness_loss": ("tsnet_admission_removed_before_connector",),
    "revocation": ("stop_before_connector_cleanup", "tsnet_admission_absent"),
    "shutdown": ("same_instance_stop_twice", "no_double_close", "no_residue"),
    "partial_failure": ("fresh_pid", "fresh_composite_gates"),
    "concurrency_reentry": ("start_then_stop_barrier", "stop_then_start_barrier", "stop_wins"),
    "recovery": (
        "fresh_install_rollback_reentry_each_checkpoint", "upgrade_rollback",
        "retained_payload_units", "fresh_composite_gates", "no_transaction_residue",
        "credential_free_stopped_restart", "interrupted_retirement_reentry",
        "explicit_fresh_reenrollment",
    ),
    "cleanup": ("virtual_admission_removed_while_peer_alive", "all_residue_absent", "task_work_removed"),
}
FORBIDDEN_KINDS = {"noop", "true", "prose", "test_name", "source_presence", "fake", "skip"}


def _canonical(doc: dict) -> bytes:
    unsigned = {k: v for k, v in doc.items() if k != "digest"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()


def _digest(doc: dict) -> str:
    return "sha256:" + hashlib.sha256(_canonical(doc)).hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate(doc: dict, *, expected_subject: str | None = None, expected_run: str | None = None) -> None:
    assert set(doc) == {"schema", "version", "subject", "run", "records", "diagnostics", "terminal", "digest"}
    assert doc.get("schema") == SCHEMA and doc.get("version") == VERSION
    subject, run = doc.get("subject"), doc.get("run")
    assert isinstance(subject, dict) and set(subject) == {"git_head", "package_sha256"}
    assert len(subject["git_head"]) == 40 and len(subject["package_sha256"]) == 64
    assert isinstance(run, dict) and set(run) == {"id", "zero_skip", "fake_count", "skip_count"}
    assert run["zero_skip"] is True and run["fake_count"] == run["skip_count"] == 0 and run["id"]
    if expected_subject is not None:
        assert subject["git_head"] == expected_subject
    if expected_run is not None:
        assert run["id"] == expected_run
    records = doc.get("records")
    assert isinstance(records, list)
    expected = {(phase, observation) for phase, values in PHASES.items() for observation in values}
    seen: set[tuple[str, str]] = set()
    assertion_ids: set[str] = set()
    for sequence, record in enumerate(records, 1):
        assert set(record) == {"sequence", "phase", "observation", "assertion"}
        assert record["sequence"] == sequence
        key = (record["phase"], record["observation"])
        assert key in expected and key not in seen
        assertion = record["assertion"]
        assert set(assertion) == {"id", "kind", "status", "completed_sequence"}
        assert assertion["id"] not in assertion_ids
        assert assertion["kind"] == record["observation"] and assertion["kind"] not in FORBIDDEN_KINDS
        assert assertion["status"] == "completed" and assertion["completed_sequence"] == sequence
        seen.add(key); assertion_ids.add(assertion["id"])
    assert seen == expected
    diagnostics = doc.get("diagnostics")
    assert isinstance(diagnostics, list) and diagnostics
    diagnostic_ids: set[str] = set()
    for receipt in diagnostics:
        assert set(receipt) == {"id", "category", "phase", "actor", "unit", "outcome", "terminal", "assertion"}
        assert receipt["id"] and receipt["id"] not in diagnostic_ids
        assert receipt["category"] in DIAGNOSTIC_PHASES
        assert receipt["phase"] == DIAGNOSTIC_PHASES[receipt["category"]]
        assert receipt["actor"] in DIAGNOSTIC_ACTORS and receipt["unit"] in DIAGNOSTIC_UNITS
        assert receipt["outcome"] == "failed" and receipt["terminal"] is True
        assert receipt["assertion"] == {"status": "completed"}
        rendered = json.dumps(receipt, sort_keys=True).lower()
        assert not any(marker in rendered for marker in SECRET_MARKERS)
        diagnostic_ids.add(receipt["id"])
    terminal = doc.get("terminal")
    assert terminal == {"status": "complete", "record_count": len(records), "last_sequence": len(records)}
    assert doc.get("digest") == _digest(doc)


def validate_denial_matrix(doc: dict, *, expected_arm: str | None = None) -> None:
    assert set(doc) == {"schema", "version", "arm_id", "operations"}
    assert doc["schema"] == "happyranch.n3.sandbox-denial-matrix" and doc["version"] == 1
    assert doc["arm_id"] == "shipping-unit"
    if expected_arm is not None:
        assert doc["arm_id"] == expected_arm
    operations = doc["operations"]
    assert isinstance(operations, list) and [row.get("id") for row in operations] == list(DENIAL_OPERATIONS)
    for row in operations:
        assert set(row) == {"id", "measured", "result", "category", "errno"}
        assert row["measured"] is True
        assert row["result"] in DENIAL_RESULTS and row["category"] in DENIAL_CATEGORIES
        assert row["errno"] in DENIAL_ERRNOS
        rendered = json.dumps(row, sort_keys=True).lower()
        assert not any(marker in rendered for marker in SECRET_MARKERS)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("path", type=Path); init.add_argument("--git-head", required=True)
    init.add_argument("--package-sha256", required=True); init.add_argument("--run-id", required=True)
    observe = sub.add_parser("observe")
    observe.add_argument("path", type=Path); observe.add_argument("--phase", required=True)
    observe.add_argument("--observation", required=True); observe.add_argument("--assertion-id", required=True)
    diagnose = sub.add_parser("diagnose")
    diagnose.add_argument("path", type=Path); diagnose.add_argument("--id", required=True)
    diagnose.add_argument("--category", required=True); diagnose.add_argument("--phase", required=True)
    diagnose.add_argument("--actor", required=True); diagnose.add_argument("--unit", required=True)
    finish = sub.add_parser("finalize"); finish.add_argument("path", type=Path)
    check = sub.add_parser("validate"); check.add_argument("path", type=Path)
    check.add_argument("--expected-subject"); check.add_argument("--expected-run")
    denial = sub.add_parser("validate-denial-matrix"); denial.add_argument("path", type=Path)
    denial.add_argument("--expected-arm", required=True)
    args = parser.parse_args()
    if args.command == "init":
        doc = {"schema": SCHEMA, "version": VERSION,
               "subject": {"git_head": args.git_head, "package_sha256": args.package_sha256},
               "run": {"id": args.run_id, "zero_skip": True, "fake_count": 0, "skip_count": 0},
               "records": [], "diagnostics": [], "terminal": None}
        args.path.parent.mkdir(parents=True, exist_ok=True)
        args.path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    elif args.command == "observe":
        doc = _load(args.path)
        assert doc.get("terminal") is None
        assert args.phase in PHASES and args.observation in PHASES[args.phase]
        sequence = len(doc["records"]) + 1
        doc["records"].append({"sequence": sequence, "phase": args.phase, "observation": args.observation,
                               "assertion": {"id": args.assertion_id, "kind": args.observation,
                                             "status": "completed", "completed_sequence": sequence}})
        args.path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    elif args.command == "diagnose":
        doc = _load(args.path)
        doc["diagnostics"].append({"id": args.id, "category": args.category, "phase": args.phase,
                                   "actor": args.actor, "unit": args.unit, "outcome": "failed",
                                   "terminal": True, "assertion": {"status": "completed"}})
        args.path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    elif args.command == "finalize":
        doc = _load(args.path)
        doc["terminal"] = {"status": "complete", "record_count": len(doc["records"]), "last_sequence": len(doc["records"])}
        doc["digest"] = _digest(doc)
        validate(doc)
        args.path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    elif args.command == "validate":
        validate(_load(args.path), expected_subject=args.expected_subject, expected_run=args.expected_run)
    elif args.command == "validate-denial-matrix":
        validate_denial_matrix(_load(args.path), expected_arm=args.expected_arm)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"invalid_n3_evidence:{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
