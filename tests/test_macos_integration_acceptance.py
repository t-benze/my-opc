from pathlib import Path

import pytest

from scripts.macos_integration_acceptance import AcceptanceComparator, load_profile


def _profile() -> dict[str, list[str]]:
    return {
        "expected_run_macos_platform_independent": ["tests/test_ok.py::test_ok"],
        "expected_run_macos_macos_backend": ["tests/test_mac.py::test_mac"],
        "expected_skip_macos_systemd": ["tests/test_linux.py::test_systemd"],
        "expected_skip_macos_diy_conditional": ["tests/test_net.py::test_network"],
    }


def _valid_comparator() -> AcceptanceComparator:
    comparator = AcceptanceComparator(_profile())
    comparator.record_collection(
        [
            "tests/test_ok.py::test_ok",
            "tests/test_mac.py::test_mac",
            "tests/test_linux.py::test_systemd",
            "tests/test_net.py::test_network",
        ]
    )
    comparator.record_outcome("tests/test_ok.py::test_ok", "passed")
    comparator.record_outcome("tests/test_mac.py::test_mac", "passed")
    comparator.record_outcome(
        "tests/test_linux.py::test_systemd", "skipped", "systemctl unavailable"
    )
    comparator.record_outcome(
        "tests/test_net.py::test_network", "skipped", "no non-loopback IPv4"
    )
    return comparator


def test_exact_profile_accepts_only_expected_results() -> None:
    assert _valid_comparator().errors() == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda c: c.record_collection(["tests/test_ok.py::test_ok"]), "selection drift"),
        (
            lambda c: c.record_outcome("tests/test_ok.py::test_ok", "skipped", "later"),
            "unexpected skip",
        ),
        (
            lambda c: c.record_outcome("tests/test_linux.py::test_systemd", "passed"),
            "expected-skip state change",
        ),
        (
            lambda c: c.record_outcome(
                "tests/test_linux.py::test_systemd", "skipped", "unrelated"
            ),
            "unexpected skip reason",
        ),
        (
            lambda c: c.record_outcome("tests/test_ok.py::test_ok", "failed"),
            "expected-run failure",
        ),
        (
            lambda c: c.record_outcome(
                "tests/test_ok.py::test_ok", "passed", wasxfail="reason"
            ),
            "xfail/xpass",
        ),
    ],
)
def test_comparator_fails_closed_on_adversarial_report(mutation, message: str) -> None:
    comparator = _valid_comparator()
    mutation(comparator)
    assert any(message in error for error in comparator.errors())


def test_comparator_rejects_a_missing_report_node() -> None:
    comparator = _valid_comparator()
    comparator.outcomes.pop("tests/test_ok.py::test_ok")
    assert any("missing node" in error for error in comparator.errors())


def test_repository_profile_is_exact_and_keeps_setsid_probe_expected_run() -> None:
    profile = load_profile(Path(".github/acceptance/partition-macos.json"))
    comparator = AcceptanceComparator(profile)

    assert len(comparator.expected_run) == 107
    assert len(comparator.expected_skip) == 32
    assert len(comparator.expected_all) == 139
    assert (
        "tests/platform/test_macos_process_group_backend.py::"
        "test_escaped_descendant_is_best_effort_survivor_real"
        in comparator.expected_run
    )


def test_workflow_is_manual_only_and_runs_the_exact_command() -> None:
    workflow = Path(".github/workflows/macos-integration-acceptance.yml").read_text()

    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "push:" not in workflow
    assert "schedule:" not in workflow
    assert "self-hosted" not in workflow
    assert "runs-on: macos-15" in workflow
    assert "persist-credentials: false" in workflow
    assert "uv run pytest tests/ -v -m integration" in workflow
    assert "pytest tests/integration -v" not in workflow
    assert "secrets." not in workflow
    assert "HOME: ${{ env.ATTEMPT_HOME }}" in workflow
    assert "PYTEST_PLUGINS: macos_integration_acceptance" in workflow
    assert "systemctl" in workflow
    assert "setsid" in workflow
    assert "systemctl ip setsid" in workflow
    assert "HAPPYRANCH_*|*TOKEN*|*SECRET*|*CREDENTIAL*|*API_KEY*|*ACCESS_KEY*" in workflow
