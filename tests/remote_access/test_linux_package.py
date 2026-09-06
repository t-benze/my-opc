from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest

from app.linux.package.build_connector import build_connector
from runtime.remote_access.cli import (
    _prepare_fresh_enrollment,
    _reconcile_enrollment_retirement,
    _retire_enrollment_source,
    main as connector_cli_main,
)
from runtime.remote_access.linux_package import (
    CompositeServiceManager,
    PackageError,
    build_linux_package,
    credential_capability,
    install_linux_package,
    render_composite_units,
    uninstall_linux_package,
)


def _stage_system_credentials(root: Path, *, enrollment: bool = False) -> None:
    config = root / "etc/happyranch"
    config.mkdir(parents=True, mode=0o700)
    (config / "daemon.token").write_text("daemon\n")
    (config / "daemon.token").chmod(0o600)
    if enrollment:
        (config / "enrollment.key").write_text("one-use\n")
        (config / "enrollment.key").chmod(0o600)


def test_connector_builder_installs_real_wheel_without_ambient_pip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / "happyranch-1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as built:
        built.writestr("runtime/remote_access/cli.py", "REAL_WHEEL = True\n")
        built.writestr("happyranch-1.dist-info/METADATA", "Name: happyranch\nVersion: 1\n")
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        installed = Path(command[command.index("--paths") + 1])
        assert (installed / "runtime/remote_access/cli.py").read_text() == "REAL_WHEEL = True\n"
        (tmp_path / "happyranch-connector").write_bytes(b"frozen-real-wheel")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    output = build_connector(wheel, tmp_path / "happyranch-connector")
    assert output.read_bytes() == b"frozen-real-wheel"
    assert len(commands) == 1
    assert "PyInstaller" in commands[0]
    assert "pip" not in commands[0]


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    sidecar = tmp_path / "sidecar"
    sidecar.write_bytes(b"sidecar-binary")
    connector = tmp_path / "connector"
    shutil.copy2(sys.executable, connector)
    connector.chmod(0o700)
    wheel = tmp_path / "happyranch-1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as built:
        built.writestr("runtime/__init__.py", "")
        built.writestr("runtime/remote_access/__init__.py", "")
        built.writestr("runtime/remote_access/cli.py", "print('fixture')\n")
        built.writestr("happyranch-1.dist-info/METADATA", "Metadata-Version: 2.1\nName: happyranch\nVersion: 1\n")
    inventory = tmp_path / "dependency-inventory.json"
    license_text = "fixture license\n"
    license_digest = hashlib.sha256(license_text.encode()).hexdigest()
    inventory.write_text(json.dumps({"schema_version": 1, "artifact": {"goos": "linux", "goarch": "amd64", "cgo_enabled": False, "package": "happyranch/linux-tsnet-sidecar"}, "generator": "tools/generate_inventory.py", "modules": [{"module": "example.test/mod", "version": "v1", "sum": "h1:x", "source": "https://example.test/mod", "spdx": "MIT", "license_sha256": license_digest, "relationship": "statically-linked-linux-build-input"}]}) + "\n")
    notices = tmp_path / "THIRD_PARTY_NOTICES.md"
    notices.write_text("# notices\n\n---\nModules:\n- example.test/mod@v1\n\nSPDX: MIT\nLicense-SHA256: " + license_digest + "\n\n```text\n" + license_text.rstrip() + "\n```\n")
    return sidecar, connector, wheel, inventory, notices


def test_composite_units_start_services_concurrently_without_readiness_cycle() -> None:
    units = render_composite_units("/opt/happyranch")
    connector = units["happyranch-connector.service"]
    sidecar = units["happyranch-tsnet-sidecar.service"]
    assert "Type=notify" in connector
    assert "NotifyAccess=main" in connector
    assert "credential-capability --name daemon.token --unit happyranch-connector.service" in connector
    assert "ExecStart=/opt/happyranch/bin/happyranch-tsnet-sidecar supervise-connector /opt/happyranch/bin/happyranch-connector run --managed" in connector
    assert "Before=happyranch-tsnet-sidecar.service" not in connector
    assert "After=happyranch-connector.service" not in sidecar
    assert "Requires=happyranch-connector.service" not in sidecar
    assert "BindsTo=happyranch-connector.service" in sidecar
    assert "Type=notify" in sidecar
    assert "NotifyAccess=main" in sidecar
    assert "ExecStartPre=+/opt/happyranch/bin/happyranch-connector reconcile-enrollment-retirement" in sidecar
    assert "ExecStartPre=/opt/happyranch/bin/happyranch-connector credential-capability --name enrollment.key --unit happyranch-tsnet-sidecar.service --consumed-marker /var/lib/happyranch-tsnet-sidecar/credential.consumed" in sidecar
    assert "ExecStart=/opt/happyranch/bin/happyranch-tsnet-sidecar --config /etc/happyranch/sidecar.json" in sidecar
    for directive in ("User=happyranch", "CapabilityBoundingSet=", "PrivateDevices=yes"):
        assert directive in sidecar
    assert "StateDirectoryMode=0700" in sidecar
    assert "LoadCredential=" not in sidecar
    assert "ExecStartPost=+/opt/happyranch/bin/happyranch-connector retire-enrollment-source" in sidecar
    assert "--dropin /etc/systemd/system/happyranch-tsnet-sidecar.service.d/10-enrollment-credential.conf" in sidecar
    assert "UMask=0077" in connector and "UMask=0077" in sidecar
    assert "0.0.0.0" not in connector + sidecar


def test_real_systemd_harness_is_zero_skip_and_uses_only_pinned_peer_artifacts() -> None:
    result = subprocess.run(
        ["bash", "-n", "app/linux/package/real_systemd_n3.sh"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_real_systemd_harness_uses_headscale_025_policy_schema() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    assert "'{\"acls\":[{\"action\":\"accept\",\"src\":[\"*\"],\"dst\":[\"*:*\"]}]}'" in harness
    assert '"proto"' not in harness


def test_real_systemd_harness_quiesces_failed_staging_before_first_enrollment() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    stop = harness.index(
        "stop happyranch-managed.target happyranch-tsnet-sidecar.service happyranch-connector.service"
    )
    reset = harness.index(
        "reset-failed happyranch-tsnet-sidecar.service happyranch-connector.service"
    )
    assert "reset-failed happyranch-tsnet-sidecar.service happyranch-connector.service happyranch-managed.target" not in harness
    staged_cleanup = harness.index('failed credential staging cleanup', reset)
    assert "sudo test ! -e /run/credentials/happyranch-tsnet-sidecar.service" in harness
    restore = harness.index(
        "mv /etc/happyranch/enrollment.key.held /etc/happyranch/enrollment.key",
        staged_cleanup,
    )
    assert stop < reset < staged_cleanup < restore


def test_real_systemd_harness_keeps_headscale_control_socket_in_task_root() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    assert "unix_socket: $work/hs/headscale.sock" in harness


def test_real_systemd_harness_probes_headscale_health_over_configured_https() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    assert (
        'curl --silent --fail --cacert "$work/tls/cert.pem" '
        "https://127.0.0.1:18080/health"
    ) in harness
    assert "http://127.0.0.1:19090/health" not in harness


def test_real_systemd_harness_proves_root_owned_binary_is_service_executable() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    assert 'stat -c %U:%G:%a /opt/happyranch)' in harness
    assert 'stat -c %U:%G:%a /opt/happyranch/bin)' in harness
    assert '== "root:root:755"' in harness
    assert 'sudo -u happyranch test -x "$binary"' in harness


def test_real_systemd_harness_keeps_load_credential_source_root_custodied() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    assert (
        'sudo install -m 0600 -o root -g root "$work/daemon.token" '
        "/etc/happyranch/daemon.token"
    ) in harness
    assert (
        'sudo install -m 0600 -o root -g root "$work/enrollment.key" '
        "/etc/happyranch/enrollment.key"
    ) in harness


def test_real_systemd_early_failure_cleanup_is_bounded_and_redacted() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    assert "wait_status" not in harness
    assert 'cp "$work/$log.log" "$diagnostics/$log.log"' not in harness
    assert 'journalctl -u happyranch-connector.service -u happyranch-tsnet-sidecar.service' not in harness
    assert 'cleanup-status.txt' in harness


def test_real_systemd_missing_credential_accepts_null_peer_map_as_no_identity() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    assert '(d.get("Peer") or {}).values()' in harness


def test_real_systemd_af_netlink_acceptance_is_four_arm_fail_closed() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    arms = [
        "ordering-a-control:A:control", "ordering-a-candidate:A:candidate",
        "ordering-b-candidate:B:candidate", "ordering-b-control:B:control",
    ]
    assert 'RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK' in harness
    assert '90-ci-af-netlink.conf' in harness
    assert "acceptance_arms=(" in harness
    assert all(arm in harness for arm in arms)
    assert 'preauthkeys create --user ci --reusable=false' in harness
    assert '--control-category engine_start --control-phase engine_initialization' in harness
    assert 'arm_result_args=(--ready)' in harness
    assert 'arm_result_args+=(--expected-peer-visible)' in harness
    assert 'arm_result_args+=(--virtual-listener-reachable)' in harness
    assert 'capture_denial_matrix' in harness
    assert 'address_family_netlink' in harness
    assert 'linux_capabilities' in harness
    assert 'device_access' in harness
    assert 'writable_paths' in harness
    assert 'control_plane_operations' in harness
    assert 'sudo systemctl daemon-reload' in harness
    assert 'cleanup_complete' in harness
    assert 'host port 443' not in harness.lower()


def test_real_systemd_candidate_expected_peer_is_production_observed() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    assert 'production_expected_peer_visible=0' in harness
    assert 'test ! -e /var/lib/happyranch-tsnet-sidecar/credential.consumed' in harness
    assert 'test -f /var/lib/happyranch-tsnet-sidecar/credential.consumed' in harness
    assert '(( production_expected_peer_visible == 1 ))' in harness
    assert 'arm_result_args=(--ready --expected-peer-visible' not in harness


def test_real_systemd_control_receipts_are_current_arm_exactly_once() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    assert 'journalctl -n 0 --show-cursor' in harness
    assert 'journalctl -u happyranch-tsnet-sidecar.service -n 0 --show-cursor' not in harness
    assert '--after-cursor="$cursor"' in harness
    assert '--output-fields=MESSAGE,_SYSTEMD_INVOCATION_ID' in harness
    assert 'systemctl_property "$unit" InvocationID' in harness
    assert 'current_control_terminal_evidence' in harness
    assert 'settle_control_terminal_invocation' in harness
    capture = harness.index('capture_control_terminal_snapshot()')
    pin = harness.index('invocation="$(systemctl_property', capture)
    stop = harness.index('sudo systemctl stop "$unit"', pin)
    receipt = harness.index('current_control_terminal_evidence "$cursor"', stop)
    snapshot = harness.index("printf '%s\\n' \"$terminal_evidence\"", receipt)
    reset = harness.index('sudo systemctl reset-failed "$unit"', snapshot)
    assert pin < stop < receipt < snapshot < reset
    assert '"$run_id:$arm_id:engine_start"' in harness


def _run_control_terminal_evidence(
    tmp_path: Path,
    journal: str,
    *,
    invocation: str = "a" * 32,
    result: str = "exit-code",
    exec_main_status: str = "1",
) -> subprocess.CompletedProcess[str]:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    helper = "current_control_terminal_evidence() {" + harness.split("current_control_terminal_evidence() {", 1)[1].split("\nsystemctl_absent_value() {", 1)[0]
    fake_bin = tmp_path / "bin"; fake_bin.mkdir(exist_ok=True)
    (fake_bin / "sudo").write_text("#!/bin/bash\nexec \"$@\"\n")
    (fake_bin / "journalctl").write_text("""#!/bin/bash
[[ " $* " == *" --after-cursor=cursor-1 "* ]] || exit 9
[[ " $* " != *" -u "* ]] || exit 9
printf '%s\n' "$FAKE_JOURNAL"
""")
    for executable in fake_bin.iterdir(): executable.chmod(0o700)
    script = f'''set -euo pipefail
{helper}
current_control_terminal_evidence cursor-1 {invocation} {result} {exec_main_status}
'''
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False,
        env=os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}", "FAKE_JOURNAL": journal},
    )


def _qualify_control_terminal_evidence(evidence: str) -> subprocess.CompletedProcess[str]:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    helper = "control_terminal_evidence_qualifies() {" + harness.split("control_terminal_evidence_qualifies() {", 1)[1].split("\nsystemctl_absent_value() {", 1)[0]
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{helper}\ncontrol_terminal_evidence_qualifies"],
        input=evidence, capture_output=True, text=True, check=False,
    )


def _journal_entry(invocation: str, receipt: dict[str, object]) -> str:
    return json.dumps({"_SYSTEMD_INVOCATION_ID": invocation, "MESSAGE": "diagnostic_receipt=" + json.dumps(receipt)})


def _control_receipt(**changes: object) -> dict[str, object]:
    receipt: dict[str, object] = {
        "category": "engine_start", "phase": "engine_initialization",
        "actor": "tsnet-sidecar", "unit": "happyranch-tsnet-sidecar.service",
        "outcome": "failed", "terminal": True, "assertion": {"status": "completed"},
    }
    receipt.update(changes)
    return receipt


def test_real_systemd_control_receipt_accepts_one_current_arm_and_redacts_output(tmp_path: Path) -> None:
    invocation = "a" * 32
    result = _run_control_terminal_evidence(tmp_path, _journal_entry(invocation, _control_receipt()), invocation=invocation)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "pinned_invocation": {
            "categories": ["engine_start"],
            "category_counts": {"credential_input": 0, "durable_commit": 0, "engine_start": 1, "network_join": 0},
            "cardinality": "one",
            "qualifying_receipt_count": 1,
            "receipt_count": 1,
        },
        "systemd": {"exec_main_status": 1, "result": "exit-code"},
        "window": {
            "categories": ["engine_start"],
            "category_counts": {"credential_input": 0, "durable_commit": 0, "engine_start": 1, "network_join": 0},
            "receipt_count": 1,
        },
    }


@pytest.mark.parametrize("count", [0, 1, 2])
def test_real_systemd_control_evidence_distinguishes_receipt_cardinality(tmp_path: Path, count: int) -> None:
    journal = "\n".join(_journal_entry("a" * 32, _control_receipt()) for _ in range(count))
    result = _run_control_terminal_evidence(tmp_path, journal)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["pinned_invocation"]["qualifying_receipt_count"] == count
    assert (_qualify_control_terminal_evidence(result.stdout).returncode == 0) == (count == 1)


def test_real_systemd_control_evidence_separates_wrong_or_stale_invocations(tmp_path: Path) -> None:
    journal = "\n".join([
        _journal_entry("b" * 32, _control_receipt()),
        _journal_entry("a" * 32, _control_receipt()),
    ])
    result = _run_control_terminal_evidence(tmp_path, journal)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["pinned_invocation"]["receipt_count"] == 1
    assert evidence["window"]["receipt_count"] == 2
    assert _qualify_control_terminal_evidence(result.stdout).returncode == 0

    stale_only = _run_control_terminal_evidence(tmp_path, _journal_entry("b" * 32, _control_receipt()))
    assert stale_only.returncode == 0
    assert _qualify_control_terminal_evidence(stale_only.stdout).returncode != 0


@pytest.mark.parametrize("cursor_output,expected_rc", [
    ("-- cursor: current-arm-cursor", 0),
    ("", 1),
    ("-- cursor: stale\n-- cursor: current", 1),
])
def test_real_systemd_current_arm_cursor_is_unfiltered_and_exactly_one(tmp_path: Path, cursor_output: str, expected_rc: int) -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    helper = "current_journal_cursor() {" + harness.split("current_journal_cursor() {", 1)[1].split("\nsystemctl_property() {", 1)[0]
    fake_bin = tmp_path / "bin"; fake_bin.mkdir()
    (fake_bin / "sudo").write_text("#!/bin/bash\nexec \"$@\"\n")
    (fake_bin / "journalctl").write_text("""#!/bin/bash
[[ " $* " == *" -n 0 "* && " $* " == *" --show-cursor "* ]] || exit 9
[[ " $* " != *" -u "* ]] || exit 9
printf '%s\n' "$FAKE_CURSOR"
""")
    for executable in fake_bin.iterdir(): executable.chmod(0o700)
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{helper}\ncurrent_journal_cursor"],
        capture_output=True, text=True, check=False,
        env=os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}", "FAKE_CURSOR": cursor_output},
    )
    assert (result.returncode == 0) == (expected_rc == 0)


@pytest.mark.parametrize("journal", [
    _journal_entry("a" * 32, _control_receipt(secret="credential=do-not-emit")),
    _journal_entry("b" * 32, _control_receipt(secret="credential=do-not-emit")),
])
def test_real_systemd_control_receipt_rejects_secret_bearing_input_and_output(tmp_path: Path, journal: str) -> None:
    result = _run_control_terminal_evidence(tmp_path, journal)
    assert result.returncode != 0
    assert "do-not-emit" not in result.stdout + result.stderr


@pytest.mark.parametrize("result_value,status", [
    ("", "1"), ("exit-code\nfailed", "1"), ("credential=do-not-emit", "1"),
    ("exit-code", ""), ("exit-code", "1\n2"), ("exit-code", "256"),
])
def test_real_systemd_control_evidence_rejects_missing_or_ambiguous_properties(
    tmp_path: Path, result_value: str, status: str,
) -> None:
    result = _run_control_terminal_evidence(
        tmp_path, _journal_entry("a" * 32, _control_receipt()), result=result_value, exec_main_status=status,
    )
    assert result.returncode != 0
    assert "do-not-emit" not in result.stdout + result.stderr


def _run_control_snapshot_lifecycle(
    tmp_path: Path,
    journal: str,
    *,
    invocation: str = "a" * 32,
    result: str = "exit-code",
    exec_main_status: str = "1",
    fail_on: str = "",
) -> subprocess.CompletedProcess[str]:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    helpers = "systemctl_property() {" + harness.split("systemctl_property() {", 1)[1].split("\nsystemctl_absent_value() {", 1)[0]
    fake_bin = tmp_path / "bin"; fake_bin.mkdir()
    (fake_bin / "sudo").write_text("#!/bin/bash\nexec \"$@\"\n")
    (fake_bin / "systemctl").write_text("""#!/bin/bash
printf '%s\\n' "$*" >>"$CALL_LOG"
[[ "${FAIL_ON:-}" != "$1" ]] || exit 7
case "$*" in
  "show happyranch-tsnet-sidecar.service -p InvocationID --value") printf '%s\\n' "$FAKE_INVOCATION" ;;
  "show happyranch-tsnet-sidecar.service -p Result --value") printf '%s\\n' "$FAKE_RESULT" ;;
  "show happyranch-tsnet-sidecar.service -p ExecMainStatus --value") printf '%s\\n' "$FAKE_STATUS" ;;
  "reset-failed happyranch-tsnet-sidecar.service") rm -f "$OBSERVABILITY" ;;
esac
""")
    (fake_bin / "journalctl").write_text("""#!/bin/bash
[[ -e "$OBSERVABILITY" ]] || exit 8
printf '%s\n' "$FAKE_JOURNAL"
""")
    for executable in fake_bin.iterdir(): executable.chmod(0o700)
    observable = tmp_path / "unit-observability"; observable.write_text("present")
    snapshot = tmp_path / "control-snapshot.json"
    script = f"set -euo pipefail\n{helpers}\ncapture_control_terminal_snapshot cursor-1 \"$SNAPSHOT\"\ncat \"$SNAPSHOT\""
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}", "CALL_LOG": str(tmp_path / "calls"),
        "OBSERVABILITY": str(observable), "SNAPSHOT": str(snapshot), "FAKE_JOURNAL": journal,
        "FAKE_INVOCATION": invocation, "FAKE_RESULT": result, "FAKE_STATUS": exec_main_status,
        "FAIL_ON": fail_on,
    }
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, env=env)


def test_real_systemd_control_snapshot_precedes_observability_destroying_reset(tmp_path: Path) -> None:
    invocation = "a" * 32
    ok = _run_control_snapshot_lifecycle(tmp_path, _journal_entry(invocation, _control_receipt()))
    assert ok.returncode == 0, ok.stderr
    assert (tmp_path / "calls").read_text().splitlines() == [
        "show happyranch-tsnet-sidecar.service -p InvocationID --value",
        "stop happyranch-tsnet-sidecar.service",
        "show happyranch-tsnet-sidecar.service -p Result --value",
        "show happyranch-tsnet-sidecar.service -p ExecMainStatus --value",
        "reset-failed happyranch-tsnet-sidecar.service",
    ]
    assert json.loads(ok.stdout)["pinned_invocation"]["receipt_count"] == 1
    assert not (tmp_path / "unit-observability").exists()


@pytest.mark.parametrize("count", [0, 2])
def test_real_systemd_control_snapshot_rejects_non_exact_receipt_cardinality(tmp_path: Path, count: int) -> None:
    journal = "\n".join(_journal_entry("a" * 32, _control_receipt()) for _ in range(count))
    assert _run_control_snapshot_lifecycle(tmp_path, journal).returncode != 0


def test_real_systemd_control_snapshot_rejects_wrong_or_stale_invocation_and_secrets(tmp_path: Path) -> None:
    wrong = _journal_entry("b" * 32, _control_receipt())
    wrong_path = tmp_path / "wrong"; wrong_path.mkdir()
    assert _run_control_snapshot_lifecycle(wrong_path, wrong).returncode != 0
    secret = _journal_entry("a" * 32, _control_receipt(secret="credential=do-not-emit"))
    secret_path = tmp_path / "secret"; secret_path.mkdir()
    rejected = _run_control_snapshot_lifecycle(secret_path, secret)
    assert rejected.returncode != 0
    assert "do-not-emit" not in rejected.stdout + rejected.stderr


@pytest.mark.parametrize("invocation", ["", "a" * 31, "a" * 32 + "\nb" * 32])
def test_real_systemd_control_snapshot_rejects_missing_or_ambiguous_invocation(
    tmp_path: Path, invocation: str,
) -> None:
    journal = _journal_entry("a" * 32, _control_receipt())
    assert _run_control_snapshot_lifecycle(tmp_path, journal, invocation=invocation).returncode != 0


@pytest.mark.parametrize("result_value,status", [("", "1"), ("exit-code\nfailed", "1"), ("exit-code", ""), ("exit-code", "1\n2")])
def test_real_systemd_control_snapshot_rejects_ambiguous_properties(
    tmp_path: Path, result_value: str, status: str,
) -> None:
    journal = _journal_entry("a" * 32, _control_receipt())
    assert _run_control_snapshot_lifecycle(tmp_path, journal, result=result_value, exec_main_status=status).returncode != 0


def test_real_systemd_control_snapshot_is_reusable_after_cleanup(tmp_path: Path) -> None:
    journal = _journal_entry("a" * 32, _control_receipt())
    result = _run_control_snapshot_lifecycle(tmp_path, journal)
    assert result.returncode == 0, result.stderr
    assert _qualify_control_terminal_evidence(result.stdout).returncode == 0


def test_real_systemd_control_snapshot_fails_closed_on_stop_or_reset(tmp_path: Path) -> None:
    journal = _journal_entry("a" * 32, _control_receipt())
    for failed_command in ("stop", "reset-failed"):
        case_path = tmp_path / failed_command; case_path.mkdir()
        failed = _run_control_snapshot_lifecycle(case_path, journal, fail_on=failed_command)
        assert failed.returncode != 0


def test_real_systemd_denial_matrix_executes_every_bounded_probe() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    for probe in ("socket.AF_NETLINK", "socket.SOCK_RAW", "/dev/net/tun", "probe-write", "create_connection"):
        assert probe in harness
    assert 'validate-denial-matrix' in harness
    assert '"measured":True' in harness
    assert 'systemd-run --quiet --wait --collect --pipe' in harness
    for sandbox_property in ("PrivateDevices=yes", "ProtectSystem=strict", "ProtectHome=yes", "CapabilityBoundingSet="):
        assert sandbox_property in harness


def test_real_systemd_candidate_failures_preserve_settled_terminal_evidence() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    assert "preserve_candidate_failure()" in harness
    assert 'current_acceptance_variant="$variant"' in harness
    assert 'candidate_failure_phase=ready' in harness
    assert 'candidate_failure_phase=expected_peers' in harness
    assert 'candidate_failure_phase=listener' in harness
    assert 'candidate_failure_phase=assertion' in harness
    assert 'candidate_failure_phase=cleanup' in harness
    assert 'validate-candidate-terminal' in harness
    assert 'candidate-terminal-evidence.json' in harness
    fail_body = harness.split("fail() {", 1)[1].split("\n}", 1)[0]
    assert "preserve_candidate_failure" in fail_body
    preserve = harness.split("preserve_candidate_failure() {", 1)[1].split("\n}", 1)[0]
    assert "capture_candidate_snapshot" in preserve
    snapshot = harness.split("capture_candidate_snapshot() {", 1)[1].split("\narm_cleanup() {", 1)[0]
    assert "capture_denial_matrix" in snapshot
    assert "settle_terminal_invocation" in snapshot
    assert "current_terminal_evidence" in snapshot
    assert snapshot.index("capture_denial_matrix") < snapshot.index("settle_terminal_invocation")


def test_real_systemd_candidate_failure_preservation_covers_both_arms_and_all_exits() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    for arm in ("ordering-a-candidate", "ordering-b-candidate"):
        assert arm in harness
    for phase in ("pre_cursor", "pre_reset", "setup_start", "ready", "expected_peers", "listener", "denial_matrix", "assertion", "cleanup", "post_cleanup_assertion"):
        assert f"candidate_failure_phase={phase}" in harness
    assert 'candidate_failure_preserved=1' in harness
    assert '[[ "$current_acceptance_variant" == candidate' in harness


@pytest.mark.parametrize("arm", ["ordering-a-candidate", "ordering-b-candidate"])
@pytest.mark.parametrize("phase", [
    "pre_cursor", "pre_reset", "setup_start", "ready", "expected_peers", "listener",
    "denial_matrix", "assertion", "cleanup", "post_cleanup_assertion",
])
def test_real_systemd_fault_hook_executes_every_candidate_boundary(arm: str, phase: str) -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    helper = "candidate_boundary() {" + harness.split("candidate_boundary() {", 1)[1].split("\nport_open()", 1)[0]
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{helper}\ncurrent_acceptance_arm={arm}\ncandidate_boundary {phase}"],
        capture_output=True, text=True, check=False,
        env=os.environ | {"N3_FAULT_PHASE": phase},
    )
    assert result.returncode != 0


def test_real_systemd_snapshot_is_captured_before_destructive_cleanup() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    loop = harness.split('for arm_spec in "${acceptance_arms[@]}"; do', 1)[1].split("\n# Restore one fresh candidate fixture", 1)[0]
    assert loop.index("capture_candidate_snapshot") < loop.index('arm_cleanup || fail')
    assert loop.index('candidate_failure_phase=post_cleanup_assertion') > loop.index('arm_cleanup || fail')
    preserve = harness.split("preserve_candidate_failure() {", 1)[1].split("\nwrite_candidate_preservation_failure() {", 1)[0]
    assert preserve.index('[[ ! -s "$candidate_snapshot_path" ]]') < preserve.index('validate-candidate-terminal')


@pytest.mark.parametrize("arm", ["ordering-a-candidate", "ordering-b-candidate"])
@pytest.mark.parametrize("phase", [
    "pre_cursor", "pre_reset", "setup_start", "ready", "expected_peers", "listener",
    "denial_matrix", "assertion", "cleanup", "post_cleanup_assertion",
])
def test_real_fail_lifecycle_preserves_then_cleans_every_candidate_boundary(tmp_path: Path, arm: str, phase: str) -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    fail_helper = "fail() {" + harness.split("fail() {", 1)[1].split("\nwait_for() {", 1)[0]
    log = tmp_path / "lifecycle.log"
    script = f'''set -euo pipefail
current_acceptance_arm={arm}
current_acceptance_variant=candidate
candidate_failure_phase={phase}
candidate_failure_preserved=0
candidate_failure_preserving=0
preserve_candidate_failure() {{ printf 'preserve:%s:%s\n' "$current_acceptance_arm" "$candidate_failure_phase" >>"$LIFECYCLE_LOG"; candidate_failure_preserved=1; }}
arm_cleanup() {{ printf 'cleanup:%s:%s\n' "$current_acceptance_arm" "$candidate_failure_phase" >>"$LIFECYCLE_LOG"; }}
{fail_helper}
fail injected
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False,
                            env=os.environ | {"LIFECYCLE_LOG": str(log)})
    assert result.returncode == 1
    assert log.read_text().splitlines() == [f"preserve:{arm}:{phase}", f"cleanup:{arm}:{phase}"]


@pytest.mark.parametrize("arm", ["ordering-a-candidate", "ordering-b-candidate"])
@pytest.mark.parametrize("phase", ["cleanup", "post_cleanup_assertion"])
def test_real_preserver_uses_settled_snapshot_after_observability_is_destroyed(tmp_path: Path, arm: str, phase: str) -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    helpers = "preserve_candidate_failure() {" + harness.split("preserve_candidate_failure() {", 1)[1].split("\narm_cleanup() {", 1)[0]
    diagnostics = tmp_path / "diagnostics"; diagnostics.mkdir()
    counts = {"credential_input": 0, "engine_start": 1, "network_join": 0, "durable_commit": 0}
    snapshot = {
        "pinned_invocation": {"categories": ["engine_start"], "category_counts": counts,
                              "receipt_count": 1, "qualifying_receipt_count": 1, "cardinality": "one"},
        "window": {"categories": ["engine_start"], "category_counts": counts, "receipt_count": 1},
        "systemd": {"result": "exit-code", "exec_main_status": 1},
    }
    denial = {"schema": "happyranch.n3.sandbox-denial-matrix", "version": 1, "arm_id": arm,
              "operations": [{"id": operation, "measured": True, "result": "allow", "category": "none", "errno": None}
                             for operation in ("address_family_netlink", "linux_capabilities", "device_access",
                                               "writable_paths", "control_plane_operations")]}
    snapshot_path = diagnostics / f"{arm}-candidate-settled-snapshot.json"
    snapshot_path.write_text(json.dumps({"terminal_evidence": snapshot, "denial_matrix": denial}))
    script = f'''set -euo pipefail
current_acceptance_arm={arm}; candidate_failure_phase={phase}; candidate_failure_preserving=0; candidate_failure_preserved=0
arm_journal_cursor=fresh; candidate_snapshot_path="$SNAPSHOT"; diagnostics="$DIAGNOSTICS"; evidence_driver="$DRIVER"
capture_candidate_snapshot() {{ echo should-not-observe >&2; return 99; }}
{helpers}
rm -f "$OBSERVABILITY"
preserve_candidate_failure
'''
    observable = tmp_path / "unit.properties"; observable.write_text("present")
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, env=os.environ | {
        "SNAPSHOT": str(snapshot_path), "DIAGNOSTICS": str(diagnostics),
        "DRIVER": str(Path("app/linux/package/n3_evidence.py").resolve()), "OBSERVABILITY": str(observable),
    })
    assert result.returncode == 0, result.stderr
    assert "should-not-observe" not in result.stderr
    terminal = json.loads((diagnostics / f"{arm}-candidate-terminal-evidence.json").read_text())
    assert terminal["arm_id"] == arm and terminal["phase"] == phase


@pytest.mark.parametrize("cursor,started,phase,code", [
    ("", "0", "pre_cursor", "cursor_unavailable"),
    ("stale-prior-arm-cursor", "0", "pre_reset", "invocation_unavailable"),
])
def test_real_preserver_never_consumes_empty_or_stale_pre_arm_state(
    tmp_path: Path, cursor: str, started: str, phase: str, code: str,
) -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    helpers = "preserve_candidate_failure() {" + harness.split("preserve_candidate_failure() {", 1)[1].split("\narm_cleanup() {", 1)[0]
    diagnostics = tmp_path / "diagnostics"; diagnostics.mkdir()
    script = f'''set -euo pipefail
current_acceptance_arm=ordering-a-candidate; candidate_failure_phase={phase}
candidate_failure_preserving=0; candidate_failure_preserved=0; candidate_invocation_started={started}
arm_journal_cursor={cursor!r}; candidate_snapshot_path="$DIAGNOSTICS/missing"; diagnostics="$DIAGNOSTICS"; evidence_driver="$DRIVER"
capture_candidate_snapshot() {{ echo stale-consumed >>"$CALL_LOG"; return 0; }}
{helpers}
preserve_candidate_failure
'''
    call_log = tmp_path / "calls"
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, env=os.environ | {
        "DIAGNOSTICS": str(diagnostics), "DRIVER": str(Path("app/linux/package/n3_evidence.py").resolve()),
        "CALL_LOG": str(call_log),
    })
    assert result.returncode != 0
    assert not call_log.exists()
    record = json.loads((diagnostics / "ordering-a-candidate-candidate-preservation-failure.json").read_text())
    assert record == {"schema": "happyranch.n3.candidate-preservation-failure", "version": 1,
                      "arm_id": "ordering-a-candidate", "phase": phase, "failure_code": code}


def _run_real_systemd_arm_cleanup(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    unit_helpers = "systemctl_absent_value() {" + harness.split("systemctl_absent_value() {", 1)[1].split("\ndiagnostics=", 1)[0]
    cleanup = harness.split("arm_cleanup() {", 1)[1].split("\n}\narm_reset()", 1)[0]
    fake_bin = tmp_path / "bin"; fake_bin.mkdir()
    (fake_bin / "systemctl").write_text("""#!/bin/bash
if [[ $1 == show ]]; then p=$4; case $p in LoadState) v=${LOAD_STATE-not-found}; s=${LOAD_RC:-4};; ActiveState) v=${ACTIVE_STATE-inactive}; s=${ACTIVE_RC:-4};; SubState) v=${SUB_STATE-dead}; s=${SUB_RC:-4};; MainPID) v=${MAIN_PID-0}; s=${PID_RC:-4};; esac; printf '%s\\n' "$v"; exit "$s"; fi
[[ $1 == list-unit-files && ${UNIT_LIST_RESIDUE:-0} == 1 ]] && echo loaded
exit 0
""")
    (fake_bin / "sudo").write_text("""#!/bin/bash
[[ $1 == rm ]] && exit 0
exec "$@"
""")
    (fake_bin / "pgrep").write_text("#!/bin/bash\n[[ ${PROCESS_RESIDUE:-0} == 1 ]]\n")
    for executable in fake_bin.iterdir(): executable.chmod(0o700)
    work = tmp_path / "work"; (work / "hs").mkdir(parents=True)
    (work / "headscale").write_text("#!/bin/bash\n[[ ${FIXTURE_RESIDUE:-0} == 1 ]] && echo '[{\"id\":1,\"name\":\"home-sidecar-ci\"}]' || echo '[]'\n")
    (work / "headscale").chmod(0o700)
    script = f"""set -euo pipefail
fail() {{ echo "$1" >&2; exit 1; }}
port_open() {{ [[ ${{PORT_RESIDUE:-0}} == 1 ]]; }}
tsnet_open() {{ [[ ${{LISTENER_RESIDUE:-0}} == 1 ]]; }}
{unit_helpers}
work={work!s}; diagnostics={tmp_path!s}
arm_cleanup() {{
{cleanup}
}}
arm_cleanup
"""
    run_env = os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}", "N3_RESIDUE_ROOT": str(tmp_path), "N3_UNIT_ROOT": str(tmp_path)} | env
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=run_env, check=False)


@pytest.mark.parametrize("exit_codes", [(0, 0, 0, 0), (1, 1, 1, 1), (4, 0, 1, 4)])
def test_real_systemd_arm_cleanup_accepts_recognized_absent_exit_orderings(tmp_path: Path, exit_codes: tuple[int, int, int, int]) -> None:
    result = _run_real_systemd_arm_cleanup(tmp_path, **dict(zip(("LOAD_RC", "ACTIVE_RC", "SUB_RC", "PID_RC"), map(str, exit_codes), strict=True)))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("env", [
    {"LOAD_STATE": "", "LOAD_RC": "4"}, {"LOAD_STATE": "not-found", "LOAD_RC": "2"},
    {"LOAD_STATE": "loaded", "LOAD_RC": "0"},
    {"ACTIVE_STATE": "active", "ACTIVE_RC": "0"}, {"MAIN_PID": "42", "PID_RC": "0"},
    {"UNIT_LIST_RESIDUE": "1"}, {"PROCESS_RESIDUE": "1"}, {"PORT_RESIDUE": "1"},
    {"LISTENER_RESIDUE": "1"}, {"FIXTURE_RESIDUE": "1"},
])
def test_real_systemd_arm_cleanup_rejects_query_and_probe_residue(tmp_path: Path, env: dict[str, str]) -> None:
    assert _run_real_systemd_arm_cleanup(tmp_path, **env).returncode != 0


@pytest.mark.parametrize("residue", [
    "etc/systemd/system/happyranch-managed.target", "run/systemd/system/happyranch-managed.target.d",
    "etc/happyranch/enrollment.key", ".happyranch-install-transaction.json", ".happyranch-backup",
    ".happyranch-units-backup", "opt/happyranch", "var/lib/happyranch-connector",
    "run/happyranch-tsnet-sidecar", "var/log/happyranch-connector", ".happyranch-stage-leftover",
    ".happyranch-tmp-leftover",
])
def test_real_systemd_arm_cleanup_rejects_every_filesystem_residue_class(tmp_path: Path, residue: str) -> None:
    path = tmp_path / residue
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir() if "." not in path.name else path.write_text("residue")
    assert _run_real_systemd_arm_cleanup(tmp_path).returncode != 0


def test_composite_service_manager_executes_start_ready_stop_crash_restart() -> None:
    events: list[tuple[str, ...]] = []
    active = {"happyranch-connector.service": False, "happyranch-tsnet-sidecar.service": False}
    def run(command, **_kwargs):
        args = tuple(command[1:])
        events.append(args)
        if args[:2] == ("start", "happyranch-managed.target"):
            active["happyranch-connector.service"] = True
            active["happyranch-tsnet-sidecar.service"] = True
        elif args[:2] == ("stop", "happyranch-managed.target"):
            active["happyranch-tsnet-sidecar.service"] = False
            active["happyranch-connector.service"] = False
        elif args[0] == "restart": active[args[1]] = True
        stdout = ""
        if args[0] == "show": stdout = "active\n" if active[args[1]] else "inactive\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")
    manager = CompositeServiceManager(run=run)
    manager.start_ready()
    active["happyranch-tsnet-sidecar.service"] = False  # observed crash/readiness loss
    manager.restart_after_crash("happyranch-tsnet-sidecar.service")
    manager.stop()
    assert events == [
        ("start", "happyranch-managed.target"),
        ("show", "happyranch-connector.service", "--property=ActiveState", "--value"),
        ("show", "happyranch-tsnet-sidecar.service", "--property=ActiveState", "--value"),
        ("restart", "happyranch-tsnet-sidecar.service"),
        ("show", "happyranch-tsnet-sidecar.service", "--property=ActiveState", "--value"),
        ("stop", "happyranch-managed.target"),
    ]
    assert active == {"happyranch-connector.service": False, "happyranch-tsnet-sidecar.service": False}


def test_build_is_reproducible_and_manifest_couples_every_payload(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    first = build_linux_package(tmp_path / "one.tar", *inputs, version="1.2.3")
    second = build_linux_package(tmp_path / "two.tar", *inputs, version="1.2.3")
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as archive:
        names = archive.getnames()
        manifest = json.load(archive.extractfile("happyranch-linux-amd64/manifest.json"))
        for item in manifest["files"]:
            raw = archive.extractfile("happyranch-linux-amd64/" + item["path"]).read()
            assert hashlib.sha256(raw).hexdigest() == item["sha256"]
        assert "happyranch-linux-amd64/share/sbom.cdx.json" in names
        assert "happyranch-linux-amd64/share/THIRD_PARTY_NOTICES.md" in names
        assert manifest["sidecar_dependency_count"] == 1


def test_build_rejects_incomplete_notices(tmp_path: Path) -> None:
    sidecar, connector, wheel, inventory, notices = _inputs(tmp_path)
    notices.write_text("# notices\n")
    with pytest.raises(PackageError, match="notice_inventory_mismatch"):
        build_linux_package(tmp_path / "bad.tar", sidecar, connector, wheel, inventory, notices, version="1")


def test_fixture_install_upgrade_uninstall_is_owner_only_and_residue_free(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    root = tmp_path / "root"
    receipt = install_linux_package(package, root)
    assert receipt["version"] == "1"
    assert (root / "opt/happyranch/bin/happyranch-tsnet-sidecar").stat().st_mode & 0o077 == 0
    assert (root / "etc/systemd/system/happyranch-managed.target").exists()
    install_linux_package(package, root)  # idempotent/re-entry
    uninstall_linux_package(root)
    assert not (root / "opt/happyranch").exists()
    assert not list((root / "etc/systemd/system").glob("happyranch-*"))


def test_system_service_install_keeps_root_payload_executable_by_service_user(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    root = tmp_path / "root"
    _stage_system_credentials(root)
    install_linux_package(package, root, system_service=True)
    assert (root / "opt/happyranch").stat().st_mode & 0o777 == 0o755
    assert (root / "opt/happyranch/bin").stat().st_mode & 0o777 == 0o755
    for name in ("happyranch-connector", "happyranch-tsnet-sidecar"):
        binary = root / "opt/happyranch/bin" / name
        assert binary.stat().st_mode & 0o777 == 0o755


def test_no_root_install_retains_owner_only_payload_mode(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    root = tmp_path / "root"
    install_linux_package(package, root, system_service=False)
    assert (root / "opt/happyranch").stat().st_mode & 0o777 == 0o700
    assert (root / "opt/happyranch/bin").stat().st_mode & 0o777 == 0o700
    for name in ("happyranch-connector", "happyranch-tsnet-sidecar"):
        assert (root / "opt/happyranch/bin" / name).stat().st_mode & 0o777 == 0o700


def test_install_rejects_ambiguous_service_mode_before_write(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    root = tmp_path / "root"
    with pytest.raises(PackageError, match="install_mode_invalid"):
        install_linux_package(package, root, system_service=1)  # type: ignore[arg-type]
    assert not root.exists()


def test_system_service_mode_survives_rollback_and_reentry(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    root = tmp_path / "root"
    _stage_system_credentials(root)
    install_linux_package(package, root, system_service=True)
    with pytest.raises(RuntimeError, match="injected"):
        install_linux_package(
            package,
            root,
            system_service=True,
            fault=lambda name: (_ for _ in ()).throw(RuntimeError("injected"))
            if name == "payload_published" else None,
        )
    install_linux_package(package, root, system_service=True)
    assert (root / "opt/happyranch").stat().st_mode & 0o777 == 0o755
    assert (root / "opt/happyranch/bin").stat().st_mode & 0o777 == 0o755
    for name in ("happyranch-connector", "happyranch-tsnet-sidecar"):
        assert (root / "opt/happyranch/bin" / name).stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("field", ["uid", "gid"])
def test_archive_rejects_non_root_payload_ownership_before_write(tmp_path: Path, field: str) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    def mutate(entries) -> None:
        member, _ = next(item for item in entries if item[0].name.endswith("happyranch-connector"))
        setattr(member, field, 1000)
    root = tmp_path / "root"
    with pytest.raises(PackageError, match="archive_owner_invalid"):
        install_linux_package(_rewrite_package(package, tmp_path / f"bad-{field}.tar", mutate), root)
    assert not root.exists()


@pytest.mark.parametrize("boundary", ["payload_old_retained", "payload_published", *[f"unit_published:{name}" for name in ("happyranch-connector.service", "happyranch-tsnet-sidecar.service", "happyranch-managed.target")]])
def test_upgrade_rolls_back_at_every_publication_boundary(tmp_path: Path, boundary: str) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    root = tmp_path / "root"
    install_linux_package(package, root)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    def fault(name: str) -> None:
        if name == boundary:
            raise RuntimeError("injected")
    with pytest.raises(RuntimeError, match="injected"):
        install_linux_package(package, root, fault=fault)
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before
    assert not list(root.glob(".happyranch-*"))


def test_archive_rejects_duplicate_member(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    duplicate = tmp_path / "duplicate.tar"
    with tarfile.open(package) as source, tarfile.open(duplicate, "w") as target:
        members = source.getmembers()
        for member in [*members, members[0]]:
            raw = source.extractfile(member).read()
            target.addfile(member, io.BytesIO(raw))
    with pytest.raises(PackageError, match="archive_duplicate_member"):
        install_linux_package(duplicate, tmp_path / "root")


def test_archive_rejects_actual_mode_mismatch_before_write(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    def mutate(entries) -> None:
        member, _raw = next(item for item in entries if item[0].name.endswith("happyranch-tsnet-sidecar"))
        member.mode = 0o777
    root = tmp_path / "root"
    with pytest.raises(PackageError, match="archive_mode_invalid"):
        install_linux_package(_rewrite_package(package, tmp_path / "bad-mode.tar", mutate), root)
    assert not root.exists()


def test_install_rejects_tampered_payload_without_partial_residue(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    tampered = tmp_path / "tampered.tar"
    with tarfile.open(package) as source, tarfile.open(tampered, "w", format=tarfile.PAX_FORMAT) as target:
        for member in source.getmembers():
            data = source.extractfile(member).read() if member.isfile() else None
            if member.name.endswith("happyranch-tsnet-sidecar"):
                data = b"tampered"
                member.size = len(data)
            target.addfile(member, io.BytesIO(data) if data is not None else None)
    root = tmp_path / "root"
    with pytest.raises(PackageError, match="manifest_hash_mismatch"):
        install_linux_package(tampered, root)
    assert not (root / "opt/happyranch").exists()


def _rewrite_package(package: Path, output: Path, mutate) -> Path:
    with tarfile.open(package) as source:
        entries = [(member, source.extractfile(member).read()) for member in source.getmembers()]
    mutate(entries)
    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as target:
        for member, raw in entries:
            member.size = len(raw)
            target.addfile(member, io.BytesIO(raw))
    return output


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("sidecar_dependency_count", True),
    ("architecture", "linux-arm64"), ("sidecar_dependency_count", 99),
])
def test_manifest_wire_values_fail_closed_before_write(tmp_path: Path, field: str, value: object) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    def mutate(entries) -> None:
        for index, (member, raw) in enumerate(entries):
            if member.name.endswith("manifest.json"):
                manifest = json.loads(raw)
                manifest[field] = value
                entries[index] = member, json.dumps(manifest).encode()
    malformed = _rewrite_package(package, tmp_path / "bad.tar", mutate)
    root = tmp_path / "root"
    with pytest.raises(PackageError): install_linux_package(malformed, root)
    assert not root.exists()


def test_exact_archive_allowlist_rejects_manifested_extra_payload(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    def mutate(entries) -> None:
        manifest_index = next(i for i, (m, _) in enumerate(entries) if m.name.endswith("manifest.json"))
        member, raw = entries[manifest_index]
        manifest = json.loads(raw)
        payload = b"extra"
        manifest["files"].append({"path": "bin/other", "sha256": hashlib.sha256(payload).hexdigest(), "mode": "0o700"})
        entries[manifest_index] = member, json.dumps(manifest).encode()
        extra = tarfile.TarInfo("happyranch-linux-amd64/bin/other")
        extra.mode = 0o700
        extra.uname = extra.gname = "root"
        entries.append((extra, payload))
    with pytest.raises(PackageError, match="manifest_path_invalid"):
        install_linux_package(_rewrite_package(package, tmp_path / "bad.tar", mutate), tmp_path / "root")


def test_archive_rejects_traversal_and_unmanifested_members_before_write(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    for name, expected in (("happyranch-linux-amd64/../escape", "archive_member_invalid"),
                           ("happyranch-linux-amd64/unmanifested", "manifest_membership_mismatch")):
        def mutate(entries, member_name=name) -> None:
            member = tarfile.TarInfo(member_name)
            member.mode = 0o600
            member.uname = member.gname = "root"
            entries.append((member, b"hostile"))
        root = tmp_path / expected
        with pytest.raises(PackageError, match=expected):
            install_linux_package(_rewrite_package(package, tmp_path / f"{expected}.tar", mutate), root)
        assert not root.exists()


@pytest.mark.parametrize("mutation", ["inventory_boolean", "sbom_missing_purl", "notice_wrong_content"])
def test_complete_evidence_structure_fails_closed_before_write(tmp_path: Path, mutation: str) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    def mutate(entries) -> None:
        manifest_i = next(i for i, (m, _) in enumerate(entries) if m.name.endswith("manifest.json"))
        manifest_member, manifest_raw = entries[manifest_i]
        manifest = json.loads(manifest_raw)
        suffix = {"inventory_boolean": "dependency-inventory.json", "sbom_missing_purl": "sbom.cdx.json",
                  "notice_wrong_content": "THIRD_PARTY_NOTICES.md"}[mutation]
        evidence_i = next(i for i, (m, _) in enumerate(entries) if m.name.endswith(suffix))
        evidence_member, evidence_raw = entries[evidence_i]
        if mutation == "inventory_boolean":
            evidence = json.loads(evidence_raw); evidence["schema_version"] = True
            evidence_raw = json.dumps(evidence).encode()
        elif mutation == "sbom_missing_purl":
            evidence = json.loads(evidence_raw); evidence["components"][0].pop("purl")
            evidence_raw = json.dumps(evidence).encode()
        else:
            evidence_raw = evidence_raw.replace(b"fixture license", b"tampered license")
        entries[evidence_i] = evidence_member, evidence_raw
        relative = str(PurePosixPath(evidence_member.name).relative_to("happyranch-linux-amd64"))
        next(item for item in manifest["files"] if item["path"] == relative)["sha256"] = hashlib.sha256(evidence_raw).hexdigest()
        entries[manifest_i] = manifest_member, json.dumps(manifest).encode()
    root = tmp_path / "root"
    with pytest.raises(PackageError):
        install_linux_package(_rewrite_package(package, tmp_path / "bad.tar", mutate), root)
    assert not root.exists()


@pytest.mark.parametrize("phase", ["payload_retained", "payload_published", "units_publishing"])
def test_interrupted_payload_publication_restores_last_known_good(tmp_path: Path, phase: str) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    root = tmp_path / "root"
    install_linux_package(package, root)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    (root / "opt/happyranch").replace(root / ".happyranch-backup")
    if phase != "payload_retained":
        (root / "opt/happyranch").mkdir(parents=True)
        (root / "opt/happyranch/broken").write_bytes(b"partial")
    unit_backup = root / ".happyranch-units-backup"
    unit_backup.mkdir(mode=0o700)
    for name in ("happyranch-connector.service", "happyranch-tsnet-sidecar.service", "happyranch-managed.target"):
        shutil.copy2(root / "etc/systemd/system" / name, unit_backup / name)
    (root / ".happyranch-install-transaction.json").write_text(json.dumps({"phase": phase, "schema_version": 1}) + "\n")
    install_linux_package(package, root)
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before
    assert not list(root.glob(".happyranch-*"))


def test_interrupted_fresh_install_without_backup_recovers(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    root = tmp_path / "root"
    root.mkdir()
    (root / ".happyranch-units-backup").mkdir(mode=0o700)
    (root / ".happyranch-install-transaction.json").write_text(
        '{"phase":"payload_retained","schema_version":1}\n'
    )
    install_linux_package(package, root)
    assert (root / "opt/happyranch/manifest.json").exists()
    assert not list(root.glob(".happyranch-*"))


def test_pre_marker_empty_unit_backup_is_recoverable(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    root = tmp_path / "root"
    (root / ".happyranch-units-backup").mkdir(parents=True, mode=0o700)
    install_linux_package(package, root)
    assert (root / "opt/happyranch/manifest.json").exists()
    assert not list(root.glob(".happyranch-*"))


def test_enrollment_source_retirement_is_atomic_reentrant_and_rolls_back(tmp_path: Path) -> None:
    source = tmp_path / "enrollment.key"
    marker = tmp_path / "state" / "credential.consumed"
    marker.parent.mkdir(mode=0o700)
    source.write_text("one-use\n")
    source.chmod(0o600)
    marker.write_text("durable\n")
    marker.chmod(0o600)
    _retire_enrollment_source(source, marker)
    assert not source.exists() and not source.with_name("enrollment.key.retiring").exists()
    _retire_enrollment_source(source, marker)

    marker.unlink()
    source.with_name("enrollment.key.retiring").write_text("rollback\n")
    source.with_name("enrollment.key.retiring").chmod(0o600)
    with pytest.raises(OSError, match="enrollment not durable"):
        _retire_enrollment_source(source, marker)
    assert source.read_text() == "rollback\n"


def test_enrollment_source_retirement_removes_transient_dropin_after_marker(tmp_path: Path) -> None:
    source = tmp_path / "enrollment.key"
    marker = tmp_path / "state" / "credential.consumed"
    dropin = tmp_path / "unit.d" / "10-enrollment-credential.conf"
    marker.parent.mkdir()
    dropin.parent.mkdir()
    source.write_text("one-use\n")
    source.chmod(0o600)
    marker.write_text("durable\n")
    marker.chmod(0o600)
    dropin.write_text("[Service]\nLoadCredential=enrollment.key:/source\n")
    dropin.chmod(0o600)
    reloads: list[str] = []
    _retire_enrollment_source(source, marker, dropin=dropin, reload_manager=lambda: reloads.append("reload"))
    assert not source.exists()
    assert not dropin.exists()
    _retire_enrollment_source(source, marker, dropin=dropin, reload_manager=lambda: reloads.append("reload"))
    assert reloads == ["reload"]


def test_retirement_removes_and_reloads_dropin_before_source(tmp_path: Path) -> None:
    source = tmp_path / "enrollment.key"
    marker = tmp_path / "state" / "credential.consumed"
    dropin = tmp_path / "unit.d" / "10-enrollment-credential.conf"
    marker.parent.mkdir(); dropin.parent.mkdir()
    source.write_text("one-use\n"); source.chmod(0o600)
    marker.write_text("durable\n"); marker.chmod(0o600)
    dropin.write_text("[Service]\nLoadCredential=enrollment.key:/source\n"); dropin.chmod(0o600)
    observed: list[tuple[bool, bool]] = []
    _retire_enrollment_source(
        source, marker, dropin=dropin,
        reload_manager=lambda: observed.append((dropin.exists(), source.exists())),
    )
    assert observed == [(False, True)]
    assert not source.exists() and not dropin.exists()


def test_interrupted_retirement_reentry_finishes_source_after_dropin_reload(tmp_path: Path) -> None:
    source = tmp_path / "enrollment.key"
    marker = tmp_path / "state" / "credential.consumed"
    dropin = tmp_path / "unit.d" / "10-enrollment-credential.conf"
    marker.parent.mkdir(); dropin.parent.mkdir()
    source.write_text("one-use\n"); source.chmod(0o600)
    marker.write_text("durable\n"); marker.chmod(0o600)
    _reconcile_enrollment_retirement(source, marker, dropin=dropin)
    assert not source.exists()


def test_explicit_fresh_enrollment_replaces_consumed_state(tmp_path: Path) -> None:
    source = tmp_path / "enrollment.key"
    marker = tmp_path / "state" / "credential.consumed"
    dropin = tmp_path / "unit.d" / "10-enrollment-credential.conf"
    marker.parent.mkdir(); dropin.parent.mkdir()
    source.write_text("fresh-one-use\n"); source.chmod(0o600)
    marker.write_text("durable\n"); marker.chmod(0o600)
    reloads: list[str] = []
    _prepare_fresh_enrollment(
        source, marker, dropin=dropin, reload_manager=lambda: reloads.append("reload"),
        service_is_active=lambda: False,
    )
    assert not marker.exists()
    assert source.read_text() == "fresh-one-use\n"
    assert dropin.read_text() == "[Service]\nLoadCredential=enrollment.key:/etc/happyranch/enrollment.key\n"
    assert reloads == ["reload"]


def test_explicit_fresh_enrollment_refuses_running_service(tmp_path: Path) -> None:
    source = tmp_path / "enrollment.key"
    marker = tmp_path / "state" / "credential.consumed"
    dropin = tmp_path / "unit.d" / "10-enrollment-credential.conf"
    marker.parent.mkdir(); dropin.parent.mkdir()
    source.write_text("fresh-one-use\n"); source.chmod(0o600)
    marker.write_text("durable\n"); marker.chmod(0o600)
    with pytest.raises(OSError, match="service must be stopped"):
        _prepare_fresh_enrollment(
            source, marker, dropin=dropin, service_is_active=lambda: True
        )
    assert marker.exists() and not dropin.exists()


def test_system_install_stages_credential_only_in_transient_dropin(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    root = tmp_path / "root"
    source = root / "etc/happyranch/enrollment.key"
    _stage_system_credentials(root)
    source.write_text("one-use\n")
    source.chmod(0o600)
    install_linux_package(package, root, system_service=True)
    dropin = root / "etc/systemd/system/happyranch-tsnet-sidecar.service.d/10-enrollment-credential.conf"
    assert dropin.read_text() == "[Service]\nLoadCredential=enrollment.key:/etc/happyranch/enrollment.key\n"
    assert dropin.stat().st_mode & 0o777 == 0o600


def test_credential_capability_distinguishes_fail_closed_categories(tmp_path: Path) -> None:
    source = tmp_path / "credential"
    uid = os.geteuid()
    assert credential_capability(source, expected_uid=uid) == "credential_absent"
    source.mkdir()
    assert credential_capability(source, expected_uid=uid) == "credential_wrong_type"
    source.rmdir()
    target = tmp_path / "target"
    target.write_text("secret\n")
    target.chmod(0o600)
    source.symlink_to(target)
    assert credential_capability(source, expected_uid=uid) == "credential_unsafe_symlink"
    source.unlink()
    source.write_text("secret\n")
    source.chmod(0o640)
    assert credential_capability(source, expected_uid=uid) == "credential_wrong_custody"
    source.chmod(0o600)
    source.write_text("")
    assert credential_capability(source, expected_uid=uid) == "credential_staging_incompatible"
    source.write_text("secret\n")
    assert credential_capability(source, expected_uid=uid) == "credential_valid"


def test_staged_credential_capability_rejects_service_writable_file(tmp_path: Path) -> None:
    source = tmp_path / "credential"
    source.write_text("secret\n")
    source.chmod(0o600)

    assert credential_capability(
        source,
        expected_uid=None,
        allowed_modes=(0o600,),
        require_read_only=True,
    ) == "credential_staging_incompatible"


@pytest.mark.parametrize(
    ("kind", "category"),
    [
        ("absent", "credential_absent"),
        ("directory", "credential_wrong_type"),
        ("symlink", "credential_unsafe_symlink"),
        ("loose", "credential_wrong_custody"),
        ("empty", "credential_staging_incompatible"),
    ],
)
def test_system_install_preflights_daemon_source_before_publication(
    tmp_path: Path, kind: str, category: str
) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    root = tmp_path / "root"
    source = root / "etc/happyranch/daemon.token"
    source.parent.mkdir(parents=True)
    if kind == "directory":
        source.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_text("daemon\n")
        target.chmod(0o600)
        source.symlink_to(target)
    elif kind != "absent":
        source.write_text("" if kind == "empty" else "daemon\n")
        source.chmod(0o640 if kind == "loose" else 0o600)
    with pytest.raises(PackageError, match=category):
        install_linux_package(package, root, system_service=True)
    assert not (root / "opt/happyranch").exists()
    assert not (root / ".happyranch-install-transaction.json").exists()


@pytest.mark.parametrize(
    ("name", "unit"),
    [
        ("daemon.token", "happyranch-connector.service"),
        ("enrollment.key", "happyranch-tsnet-sidecar.service"),
    ],
)
def test_packaged_preflight_uses_ownership_neutral_systemd_staged_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    unit: str,
) -> None:
    observed: list[tuple[Path, int | None, tuple[int, ...], bool]] = []

    def classify(
        path: Path,
        *,
        expected_uid: int | None,
        allowed_modes: tuple[int, ...] | None,
        require_read_only: bool,
    ) -> str:
        observed.append((path, expected_uid, allowed_modes, require_read_only))
        return "credential_valid"

    monkeypatch.setattr("runtime.remote_access.cli.credential_capability", classify)
    staged = tmp_path / "run" / "credentials" / unit
    staged.mkdir(parents=True)
    staged.chmod(0o500)
    monkeypatch.setattr(
        "runtime.remote_access.cli._expected_systemd_credentials_directory",
        lambda _unit: staged,
    )
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(staged))
    assert connector_cli_main([
        "credential-capability", "--name", name, "--unit", unit,
    ]) == 0
    assert observed == [(staged / name, None, None, True)]


@pytest.mark.parametrize(
    ("name", "unit"),
    [
        ("daemon.token", "happyranch-connector.service"),
        ("enrollment.key", "happyranch-tsnet-sidecar.service"),
    ],
)
def test_packaged_preflight_accepts_real_systemd_0440_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    unit: str,
) -> None:
    staged = tmp_path / "run" / "credentials" / unit
    staged.mkdir(parents=True)
    credential = staged / name
    credential.write_text("secret\n")
    # Real systemd 255 on the Ubuntu 24.04 shipping runner stages both
    # credentials as service-readable, non-writable 0440 files.  The mode is
    # an implementation detail; the service-observable capability is the
    # contract.
    credential.chmod(0o440)
    staged.chmod(0o500)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(staged))
    monkeypatch.setattr(
        "runtime.remote_access.cli._expected_systemd_credentials_directory",
        lambda _unit: staged,
        raising=False,
    )

    assert connector_cli_main([
        "credential-capability", "--name", name, "--unit", unit,
    ]) == 0


@pytest.mark.parametrize(
    ("name", "unit"),
    [
        ("daemon.token", "happyranch-connector.service"),
        ("enrollment.key", "happyranch-tsnet-sidecar.service"),
    ],
)
def test_packaged_preflight_rejects_service_writable_staging_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    unit: str,
) -> None:
    staged = tmp_path / unit
    staged.mkdir()
    (staged / name).write_text("secret\n")
    (staged / name).chmod(0o400)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(staged))
    monkeypatch.setattr(
        "runtime.remote_access.cli._expected_systemd_credentials_directory",
        lambda _unit: staged,
    )

    assert connector_cli_main([
        "credential-capability", "--name", name, "--unit", unit,
    ]) == 1
    assert capsys.readouterr().err.strip() == "credential_staging_incompatible"


@pytest.mark.parametrize(
    ("name", "unit", "directory"),
    [
        ("daemon.token", "happyranch-connector.service", "/run/credentials/happyranch-tsnet-sidecar.service"),
        ("enrollment.key", "happyranch-tsnet-sidecar.service", "/run/credentials/happyranch-connector.service"),
        ("daemon.token", "happyranch-connector.service", "/tmp/credentials/happyranch-connector.service"),
        ("enrollment.key", "happyranch-tsnet-sidecar.service", "relative/credentials"),
    ],
)
def test_packaged_preflight_rejects_invalid_systemd_staging_provenance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    unit: str,
    directory: str,
) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", directory)
    assert connector_cli_main([
        "credential-capability", "--name", name, "--unit", unit,
    ]) == 1
    assert capsys.readouterr().err.strip() == "credential_staging_incompatible"


@pytest.mark.parametrize(
    ("name", "unit"),
    [
        ("daemon.token", "happyranch-connector.service"),
        ("enrollment.key", "happyranch-tsnet-sidecar.service"),
    ],
)
@pytest.mark.parametrize(
    "category",
    ("credential_wrong_type", "credential_unsafe_symlink", "credential_wrong_custody", "credential_staging_incompatible"),
)
def test_each_rendered_unit_rejects_invalid_staged_type_mode_or_path_without_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    unit: str,
    category: str,
) -> None:
    secret = "forbidden-credential-material"
    staged = tmp_path / unit
    staged.mkdir()
    staged.chmod(0o500)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(staged))
    monkeypatch.setattr(
        "runtime.remote_access.cli._expected_systemd_credentials_directory",
        lambda _unit: staged,
    )
    monkeypatch.setattr(
        "runtime.remote_access.cli.credential_capability",
        lambda *_args, **_kwargs: category,
    )
    assert connector_cli_main([
        "credential-capability", "--name", name, "--unit", unit,
    ]) == 1
    error = capsys.readouterr().err.strip()
    assert error == category
    assert secret not in error
    assert "/run/credentials" not in error


def test_packaged_preflight_uses_each_units_staged_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = tmp_path / "happyranch-connector.service"
    staged.mkdir()
    staged.chmod(0o500)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(staged))
    monkeypatch.setattr(
        "runtime.remote_access.cli._expected_systemd_credentials_directory",
        lambda _unit: staged,
    )
    monkeypatch.setattr(
        "runtime.remote_access.cli.credential_capability",
        lambda path, **_kwargs: "credential_valid" if path.name == "credential.consumed" else "credential_absent",
    )
    assert connector_cli_main(["credential-capability", "--name", "daemon.token", "--unit", "happyranch-connector.service"]) == 1
    assert capsys.readouterr().err.strip() == "credential_absent"
    monkeypatch.delenv("CREDENTIALS_DIRECTORY")
    marker = tmp_path / "credential.consumed"
    marker.write_text("durable\n")
    marker.chmod(0o600)
    assert connector_cli_main([
        "credential-capability", "--name", "enrollment.key", "--unit", "happyranch-tsnet-sidecar.service",
        "--consumed-marker", str(marker),
    ]) == 0


def test_packaged_connector_binary_executes_outside_source_checkout(tmp_path: Path) -> None:
    package = build_linux_package(tmp_path / "pkg.tar", *_inputs(tmp_path), version="1")
    root = tmp_path / "root"
    install_linux_package(package, root)
    result = subprocess.run([root / "opt/happyranch/bin/happyranch-connector", "--help"],
                            cwd=tmp_path, text=True, capture_output=True, check=True)
    assert "usage:" in result.stdout
    assert "No module named" not in result.stderr
