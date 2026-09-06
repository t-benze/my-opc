"""Fail-closed pytest comparator for the hosted macOS integration probe."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import sys

import pytest


RUN_KEYS = (
    "expected_run_macos_platform_independent",
    "expected_run_macos_macos_backend",
)
SKIP_REASON_FRAGMENTS = {
    "expected_skip_macos_systemd": (
        "systemctl",
        "cgroup",
        "probe failed",
        "Linux user systemd conformance not runnable",
    ),
    "expected_skip_macos_diy_conditional": ("no non-loopback IPv4",),
}
EXPECTED_RUN_COUNT = 107
EXPECTED_SKIP_COUNT = 32


def load_profile(path: Path) -> dict[str, list[str]]:
    raw = json.loads(path.read_text())
    required = set(RUN_KEYS) | set(SKIP_REASON_FRAGMENTS)
    if set(raw) != required:
        raise ValueError(
            f"profile keys differ: expected={sorted(required)!r} actual={sorted(raw)!r}"
        )
    if any(not isinstance(raw[key], list) for key in required):
        raise ValueError("every profile partition must be a list")
    all_nodes = [node for key in required for node in raw[key]]
    if any(not isinstance(node, str) or not node for node in all_nodes):
        raise ValueError("every profile node must be a non-empty string")
    if len(set(all_nodes)) != len(all_nodes):
        raise ValueError("profile contains duplicate node IDs")
    run_count = sum(len(raw[key]) for key in RUN_KEYS)
    skip_count = sum(len(raw[key]) for key in SKIP_REASON_FRAGMENTS)
    if (run_count, skip_count) != (EXPECTED_RUN_COUNT, EXPECTED_SKIP_COUNT):
        raise ValueError(
            "profile cardinality differs: "
            f"expected=({EXPECTED_RUN_COUNT}, {EXPECTED_SKIP_COUNT}) "
            f"actual=({run_count}, {skip_count})"
        )
    return raw


@dataclass(frozen=True)
class Outcome:
    state: str
    reason: str = ""
    wasxfail: str | None = None


class AcceptanceComparator:
    def __init__(self, profile: dict[str, list[str]]) -> None:
        self.profile = profile
        self.expected_run = frozenset(
            node for key in RUN_KEYS for node in profile[key]
        )
        self.skip_groups = {
            key: frozenset(profile[key]) for key in SKIP_REASON_FRAGMENTS
        }
        self.expected_skip = frozenset().union(*self.skip_groups.values())
        self.expected_all = self.expected_run | self.expected_skip
        self.collected: frozenset[str] | None = None
        self.outcomes: dict[str, Outcome] = {}

    def record_collection(self, nodeids: list[str]) -> None:
        self.collected = frozenset(nodeids)

    def record_outcome(
        self,
        nodeid: str,
        state: str,
        reason: str = "",
        *,
        wasxfail: str | None = None,
    ) -> None:
        self.outcomes[nodeid] = Outcome(state, reason, wasxfail)

    def errors(self) -> list[str]:
        errors: list[str] = []
        collected = self.collected or frozenset()
        missing_selection = sorted(self.expected_all - collected)
        extra_selection = sorted(collected - self.expected_all)
        if missing_selection or extra_selection:
            errors.append(
                "selection drift: "
                f"missing={missing_selection!r} extra={extra_selection!r}"
            )

        for nodeid in sorted(self.expected_all - self.outcomes.keys()):
            errors.append(f"missing node from test report: {nodeid}")

        for nodeid, outcome in sorted(self.outcomes.items()):
            if outcome.wasxfail is not None:
                errors.append(
                    f"xfail/xpass is forbidden: {nodeid} ({outcome.wasxfail})"
                )
                continue
            if nodeid in self.expected_run:
                if outcome.state == "skipped":
                    errors.append(f"unexpected skip: {nodeid} ({outcome.reason})")
                elif outcome.state != "passed":
                    errors.append(
                        f"expected-run failure: {nodeid} state={outcome.state}"
                    )
                continue
            if nodeid in self.expected_skip:
                if outcome.state != "skipped":
                    errors.append(
                        "expected-skip state change: "
                        f"{nodeid} state={outcome.state}"
                    )
                    continue
                group = next(
                    key for key, nodes in self.skip_groups.items() if nodeid in nodes
                )
                if not any(
                    fragment.lower() in outcome.reason.lower()
                    for fragment in SKIP_REASON_FRAGMENTS[group]
                ):
                    errors.append(
                        f"unexpected skip reason: {nodeid} ({outcome.reason})"
                    )
            else:
                errors.append(f"unexpected reported node: {nodeid}")
        return errors


_comparator: AcceptanceComparator | None = None


def pytest_configure(config: pytest.Config) -> None:
    global _comparator
    profile_path = os.environ.get("HAPPYRANCH_MACOS_ACCEPTANCE_PROFILE")
    if not profile_path:
        raise pytest.UsageError("HAPPYRANCH_MACOS_ACCEPTANCE_PROFILE is required")
    if sys.platform != "darwin":
        raise pytest.UsageError("macOS acceptance comparator requires darwin")
    _comparator = AcceptanceComparator(load_profile(Path(profile_path)))


def pytest_collection_finish(session: pytest.Session) -> None:
    assert _comparator is not None
    _comparator.record_collection([item.nodeid for item in session.items])


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    assert _comparator is not None
    if report.skipped:
        _comparator.record_outcome(
            report.nodeid,
            "skipped",
            str(report.longrepr),
            wasxfail=getattr(report, "wasxfail", None),
        )
    elif report.when == "call" or report.failed:
        _comparator.record_outcome(
            report.nodeid,
            "passed" if report.passed else "failed",
            str(report.longrepr),
            wasxfail=getattr(report, "wasxfail", None),
        )


def pytest_sessionfinish(
    session: pytest.Session, exitstatus: int | pytest.ExitCode
) -> None:
    assert _comparator is not None
    errors = _comparator.errors()
    report_path = Path(
        os.environ.get(
            "HAPPYRANCH_MACOS_ACCEPTANCE_REPORT",
            "macos-integration-acceptance-report.json",
        )
    )
    report_path.write_text(
        json.dumps(
            {
                "expected_run": len(_comparator.expected_run),
                "expected_skip": len(_comparator.expected_skip),
                "collected": len(_comparator.collected or ()),
                "reported": len(_comparator.outcomes),
                "errors": errors,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if errors:
        session.config.pluginmanager.get_plugin("terminalreporter").write_sep(
            "=", "macOS acceptance comparator failures"
        )
        for error in errors:
            session.config.pluginmanager.get_plugin("terminalreporter").write_line(error)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
