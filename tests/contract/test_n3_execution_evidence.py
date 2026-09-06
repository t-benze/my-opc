import copy
import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path("app/linux/package/n3_evidence.py")
SPEC = importlib.util.spec_from_file_location("n3_evidence", MODULE_PATH)
assert SPEC and SPEC.loader
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)


def valid_artifact():
    records = []
    for phase, observations in evidence.PHASES.items():
        for observation in observations:
            sequence = len(records) + 1
            records.append({"sequence": sequence, "phase": phase, "observation": observation,
                            "assertion": {"id": f"assert-{sequence}", "kind": observation,
                                          "status": "completed", "completed_sequence": sequence}})
    doc = {"schema": evidence.SCHEMA, "version": evidence.VERSION,
           "subject": {"git_head": "a" * 40, "package_sha256": "b" * 64},
           "run": {"id": "run-unique", "zero_skip": True, "fake_count": 0, "skip_count": 0},
           "diagnostics": [{"id": "diagnostic-1", "category": "credential_input", "phase": "input_acquisition",
                            "actor": "systemd", "unit": "happyranch-tsnet-sidecar.service", "outcome": "failed",
                            "terminal": True, "assertion": {"status": "completed"}}],
           "acceptance": [
               {"id": arm_id, "sequence": sequence, "ordering": ordering, "variant": variant,
                "subject_git_head": "a" * 40, "package_sha256": "b" * 64,
                "input_sha256": "c" * 64, "zero_skip": True, "fake_count": 0,
                "skip_count": 0, "ready": variant == "candidate",
                "expected_peer_visible": variant == "candidate",
                "virtual_listener_reachable": variant == "candidate",
                "control_category": "engine_start" if variant == "control" else None,
                "control_phase": "engine_initialization" if variant == "control" else None,
                "cleanup_complete": True, "assertion": {"status": "completed"}}
               for sequence, (arm_id, ordering, variant) in enumerate(evidence.ARM_SPECS, 1)
           ],
           "records": records,
           "terminal": {"status": "complete", "record_count": len(records), "last_sequence": len(records)}}
    doc["digest"] = evidence._digest(doc)
    return doc


def test_validator_accepts_complete_exact_subject_artifact():
    evidence.validate(valid_artifact(), expected_subject="a" * 40, expected_run="run-unique")


@pytest.mark.parametrize("mutation", ["pre_assertion", "noop", "missing", "duplicate", "unknown", "partial", "forged", "skip", "fake", "prose"])
def test_validator_rejects_malformed_or_tautological_execution_artifact(mutation):
    doc = valid_artifact()
    if mutation == "pre_assertion": doc["records"][0]["assertion"]["completed_sequence"] = 2
    elif mutation == "noop": doc["records"][0]["assertion"]["kind"] = "true"
    elif mutation == "missing": doc["records"].pop()
    elif mutation == "duplicate": doc["records"][-1] = copy.deepcopy(doc["records"][0])
    elif mutation == "unknown": doc["records"][0]["observation"] = "test_name_present"
    elif mutation == "partial": doc["terminal"] = None
    elif mutation == "forged":
        doc["terminal"]["record_count"] -= 1
    elif mutation == "skip": doc["run"]["skip_count"] = 1
    elif mutation == "fake": doc["run"]["fake_count"] = 1
    elif mutation == "prose": doc["records"][0]["assertion"]["kind"] = "prose"
    # Re-sign structural mutations so each negative proves its semantic guard,
    # not merely the whole-document digest check. "forged" deliberately keeps
    # the old digest to exercise tamper rejection as well.
    if mutation != "forged":
        doc["digest"] = evidence._digest(doc)
    with pytest.raises(AssertionError):
        evidence.validate(doc)


def test_cli_finalization_publishes_only_complete_artifact(tmp_path, monkeypatch):
    path = tmp_path / "evidence.json"
    monkeypatch.setattr("sys.argv", [str(MODULE_PATH), "init", str(path), "--git-head", "a" * 40,
                                     "--package-sha256", "b" * 64, "--run-id", "run-unique"])
    evidence.main()
    assert json.loads(path.read_text())["terminal"] is None
    for arm_id, ordering, variant in evidence.ARM_SPECS:
        argv = [str(MODULE_PATH), "arm", str(path), "--id", arm_id, "--ordering", ordering,
                "--variant", variant, "--input-sha256", "c" * 64]
        if variant == "candidate":
            argv += ["--ready", "--expected-peer-visible", "--virtual-listener-reachable"]
        else:
            argv += ["--control-category", "engine_start", "--control-phase", "engine_initialization"]
        monkeypatch.setattr("sys.argv", argv)
        evidence.main()
    monkeypatch.setattr("sys.argv", [str(MODULE_PATH), "diagnose", str(path), "--id", "diagnostic-1",
                                     "--category", "credential_input", "--phase", "input_acquisition",
                                     "--actor", "systemd", "--unit", "happyranch-tsnet-sidecar.service"])
    evidence.main()
    for phase, observations in evidence.PHASES.items():
        for observation in observations:
            monkeypatch.setattr("sys.argv", [str(MODULE_PATH), "observe", str(path), "--phase", phase,
                                             "--observation", observation, "--assertion-id", f"{phase}:{observation}"])
            evidence.main()
    monkeypatch.setattr("sys.argv", [str(MODULE_PATH), "finalize", str(path)])
    evidence.main()
    evidence.validate(json.loads(path.read_text()))


@pytest.mark.parametrize("category,phase", list(evidence.DIAGNOSTIC_PHASES.items()))
def test_validator_accepts_each_mapped_diagnostic_category(category, phase):
    doc = valid_artifact()
    doc["diagnostics"][0].update(category=category, phase=phase)
    doc["digest"] = evidence._digest(doc)
    evidence.validate(doc)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "prose", "unmapped", "secret", "inconsistent", "incomplete"])
def test_validator_rejects_invalid_diagnostic_receipts(mutation):
    doc = valid_artifact()
    receipt = doc["diagnostics"][0]
    if mutation == "missing": doc.pop("diagnostics")
    elif mutation == "duplicate": doc["diagnostics"].append(copy.deepcopy(receipt))
    elif mutation == "prose": receipt["category"] = "credential problem"
    elif mutation == "unmapped": receipt["category"] = "unknown"
    elif mutation == "secret": receipt["id"] = "token=/etc/happyranch/enrollment.key"
    elif mutation == "inconsistent": receipt["phase"] = "peer_establishment"
    elif mutation == "incomplete": receipt["assertion"]["status"] = "pending"
    doc["digest"] = evidence._digest(doc)
    with pytest.raises((AssertionError, KeyError)):
        evidence.validate(doc)


@pytest.mark.parametrize("mutation", [
    "missing", "duplicate", "reordered", "package", "input", "partial_candidate",
    "skip", "fake", "cleanup", "order_bias", "fine_reason", "pre_assertion",
    "noop", "prose", "source_name", "tamper", "secret",
])
def test_validator_rejects_invalid_acceptance_arms(mutation):
    doc = valid_artifact()
    arms = doc["acceptance"]
    if mutation == "missing": arms.pop()
    elif mutation == "duplicate": arms[-1] = copy.deepcopy(arms[0])
    elif mutation == "reordered": arms[0], arms[1] = arms[1], arms[0]
    elif mutation == "package": arms[2]["package_sha256"] = "d" * 64
    elif mutation == "input": arms[2]["input_sha256"] = "d" * 64
    elif mutation == "partial_candidate": arms[1]["expected_peer_visible"] = False
    elif mutation == "skip": arms[0]["skip_count"] = 1
    elif mutation == "fake": arms[0]["fake_count"] = 1
    elif mutation == "cleanup": arms[0]["cleanup_complete"] = False
    elif mutation == "order_bias": arms[2]["ready"] = False
    elif mutation == "fine_reason": arms[0]["reason"] = "netlink denied"
    elif mutation == "pre_assertion": arms[0]["assertion"]["status"] = "pending"
    elif mutation == "noop": arms[0]["assertion"] = {"status": "completed", "kind": "noop"}
    elif mutation == "prose": arms[0]["proof"] = "looks correct"
    elif mutation == "source_name": arms[0]["proof"] = "real_systemd_n3.sh"
    elif mutation == "tamper":
        arms[0]["cleanup_complete"] = False
        with pytest.raises(AssertionError):
            evidence.validate(doc)
        return
    elif mutation == "secret": arms[0]["input_sha256"] = "token=/etc/happyranch/key"
    doc["digest"] = evidence._digest(doc)
    with pytest.raises(AssertionError):
        evidence.validate(doc)


def _valid_denial_matrix():
    return {
        "schema": "happyranch.n3.sandbox-denial-matrix",
        "version": 1,
        "arm_id": "ordering-a-candidate",
        "operations": [
            {"id": operation, "measured": True, "result": "allow", "category": "none", "errno": None}
            for operation in evidence.DENIAL_OPERATIONS
        ],
    }


@pytest.mark.parametrize("mutation", ["default", "unmeasured", "missing", "secret", "prose", "bad_errno"])
def test_denial_matrix_requires_real_bounded_secret_safe_measurements(mutation):
    matrix = _valid_denial_matrix()
    if mutation == "default":
        for row in matrix["operations"]:
            row.update(measured=False, result="unknown", category="unmeasured")
    elif mutation == "unmeasured": matrix["operations"][1]["measured"] = False
    elif mutation == "missing": matrix["operations"].pop()
    elif mutation == "secret": matrix["operations"][0]["category"] = "token=/etc/happyranch/key"
    elif mutation == "prose": matrix["operations"][0]["category"] = "provider said denied"
    elif mutation == "bad_errno": matrix["operations"][0]["errno"] = "arbitrary-error"
    with pytest.raises(AssertionError):
        evidence.validate_denial_matrix(matrix, expected_arm="ordering-a-candidate")


def test_denial_matrix_accepts_each_required_measured_dimension():
    evidence.validate_denial_matrix(_valid_denial_matrix(), expected_arm="ordering-a-candidate")


def _valid_candidate_terminal():
    return {
        "schema": "happyranch.n3.candidate-terminal-evidence", "version": 1,
        "arm_id": "ordering-a-candidate", "phase": "ready",
        "invocation_binding": "settled_current",
        "terminal_evidence": {
            "pinned_invocation": {"categories": ["engine_start"], "category_counts": {
                "credential_input": 0, "engine_start": 1, "network_join": 0, "durable_commit": 0,
            }, "receipt_count": 1, "qualifying_receipt_count": 1, "cardinality": "one"},
            "window": {"categories": ["engine_start"], "category_counts": {
                "credential_input": 0, "engine_start": 2, "network_join": 0, "durable_commit": 0,
            }, "receipt_count": 2},
            "systemd": {"result": "exit-code", "exec_main_status": 1},
        },
        "denial_matrix": _valid_denial_matrix(),
    }


@pytest.mark.parametrize("mutation", [
    "missing", "wrong_arm", "bad_phase", "placeholder", "bad_cardinality", "tamper_counts",
    "missing_systemd", "secret", "denial_arm", "incomplete_denial",
])
def test_candidate_terminal_validator_rejects_incomplete_or_tampered_evidence(mutation):
    doc = _valid_candidate_terminal()
    if mutation == "missing": doc.pop("phase")
    elif mutation == "wrong_arm": doc["arm_id"] = "ordering-a-control"
    elif mutation == "bad_phase": doc["phase"] = "provider said no"
    elif mutation == "placeholder": doc["terminal_evidence"] = "unavailable"
    elif mutation == "bad_cardinality": doc["terminal_evidence"]["pinned_invocation"]["cardinality"] = "multiple"
    elif mutation == "tamper_counts": doc["terminal_evidence"]["pinned_invocation"]["receipt_count"] = 2
    elif mutation == "missing_systemd": doc["terminal_evidence"].pop("systemd")
    elif mutation == "secret": doc["phase"] = "token=/etc/happyranch/key"
    elif mutation == "denial_arm": doc["denial_matrix"]["arm_id"] = "ordering-b-candidate"
    elif mutation == "incomplete_denial": doc["denial_matrix"]["operations"].pop()
    with pytest.raises((AssertionError, KeyError, TypeError)):
        evidence.validate_candidate_terminal(doc, expected_arm="ordering-a-candidate")


@pytest.mark.parametrize("count,cardinality", [(0, "zero"), (1, "one"), (2, "multiple")])
def test_candidate_terminal_validator_distinguishes_receipt_cardinality(count, cardinality):
    doc = _valid_candidate_terminal()
    pinned = doc["terminal_evidence"]["pinned_invocation"]
    pinned["category_counts"]["engine_start"] = count
    pinned["receipt_count"] = pinned["qualifying_receipt_count"] = count
    pinned["categories"] = ["engine_start"] if count else []
    pinned["cardinality"] = cardinality
    evidence.validate_candidate_terminal(doc, expected_arm="ordering-a-candidate")


@pytest.mark.parametrize("arm", ["ordering-a-candidate", "ordering-b-candidate"])
@pytest.mark.parametrize("phase", sorted(evidence.CANDIDATE_FAILURE_PHASES))
def test_candidate_terminal_validator_accepts_each_arm_and_failure_phase(arm, phase):
    doc = _valid_candidate_terminal()
    doc["arm_id"] = arm
    doc["phase"] = phase
    doc["denial_matrix"]["arm_id"] = arm
    evidence.validate_candidate_terminal(doc, expected_arm=arm)
