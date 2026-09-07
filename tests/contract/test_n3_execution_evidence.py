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


def _valid_denial_matrix():
    return {
        "schema": "happyranch.n3.sandbox-denial-matrix",
        "version": 1,
        "arm_id": "shipping-unit",
        "operations": [
            {"id": operation, "measured": True, "result": "allow", "category": "none", "errno": None}
            for operation in evidence.DENIAL_OPERATIONS
        ],
    }


@pytest.mark.parametrize("mutation", ["default", "unmeasured", "missing", "wrong_arm", "secret", "prose", "bad_errno"])
def test_denial_matrix_requires_real_bounded_secret_safe_measurements(mutation):
    matrix = _valid_denial_matrix()
    if mutation == "default":
        for row in matrix["operations"]:
            row.update(measured=False, result="unknown", category="unmeasured")
    elif mutation == "unmeasured": matrix["operations"][1]["measured"] = False
    elif mutation == "missing": matrix["operations"].pop()
    elif mutation == "wrong_arm": matrix["arm_id"] = "ordering-a-candidate"
    elif mutation == "secret": matrix["operations"][0]["category"] = "token=/etc/happyranch/key"
    elif mutation == "prose": matrix["operations"][0]["category"] = "provider said denied"
    elif mutation == "bad_errno": matrix["operations"][0]["errno"] = "arbitrary-error"
    with pytest.raises(AssertionError):
        evidence.validate_denial_matrix(matrix, expected_arm="shipping-unit")


def test_denial_matrix_accepts_each_required_measured_dimension():
    evidence.validate_denial_matrix(_valid_denial_matrix(), expected_arm="shipping-unit")
