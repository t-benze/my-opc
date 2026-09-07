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


def test_composite_units_share_exact_address_family_sandbox() -> None:
    units = render_composite_units("/opt/happyranch")
    expected = "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK"
    rendered = {
        unit: [line for line in text.splitlines() if line.startswith("RestrictAddressFamilies=")]
        for unit, text in units.items()
        if unit.endswith(".service")
    }

    assert rendered == {
        "happyranch-connector.service": [expected],
        "happyranch-tsnet-sidecar.service": [expected],
    }


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


def test_real_systemd_uses_plain_shipping_unit_without_af_netlink_ab_arms() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    assert "acceptance_arms" not in harness
    assert "ordering-a-control" not in harness
    assert "ordering-a-candidate" not in harness
    assert "ordering-b-candidate" not in harness
    assert "ordering-b-control" not in harness
    assert "reset_shipping_unit" in harness
    assert "capture_denial_matrix shipping-unit" in harness
    assert "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK" in harness
    assert "90-ci-af-netlink.conf" not in harness


def test_real_systemd_denial_matrix_executes_every_bounded_probe() -> None:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    for probe in ("socket.AF_NETLINK", "socket.SOCK_RAW", "/dev/net/tun", "probe-write", "create_connection"):
        assert probe in harness
    assert 'validate-denial-matrix' in harness
    assert '"measured":True' in harness
    assert 'systemd-run --quiet --wait --collect --pipe' in harness
    for sandbox_property in ("PrivateDevices=yes", "ProtectSystem=strict", "ProtectHome=yes", "CapabilityBoundingSet="):
        assert sandbox_property in harness


def _run_real_systemd_shipping_cleanup(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    harness = Path("app/linux/package/real_systemd_n3.sh").read_text()
    unit_helpers = "systemctl_absent_value() {" + harness.split("systemctl_absent_value() {", 1)[1].split("\ndiagnostics=", 1)[0]
    cleanup = harness.split("shipping_cleanup() {", 1)[1].split("\n}\nreset_shipping_unit()", 1)[0]
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
shipping_cleanup() {{
{cleanup}
}}
shipping_cleanup
"""
    run_env = os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}", "N3_RESIDUE_ROOT": str(tmp_path), "N3_UNIT_ROOT": str(tmp_path)} | env
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=run_env, check=False)


@pytest.mark.parametrize("exit_codes", [(0, 0, 0, 0), (1, 1, 1, 1), (4, 0, 1, 4)])
def test_real_systemd_shipping_cleanup_accepts_recognized_absent_exit_orderings(tmp_path: Path, exit_codes: tuple[int, int, int, int]) -> None:
    result = _run_real_systemd_shipping_cleanup(tmp_path, **dict(zip(("LOAD_RC", "ACTIVE_RC", "SUB_RC", "PID_RC"), map(str, exit_codes), strict=True)))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("env", [
    {"LOAD_STATE": "", "LOAD_RC": "4"}, {"LOAD_STATE": "not-found", "LOAD_RC": "2"},
    {"LOAD_STATE": "loaded", "LOAD_RC": "0"},
    {"ACTIVE_STATE": "active", "ACTIVE_RC": "0"}, {"MAIN_PID": "42", "PID_RC": "0"},
    {"UNIT_LIST_RESIDUE": "1"}, {"PROCESS_RESIDUE": "1"}, {"PORT_RESIDUE": "1"},
    {"LISTENER_RESIDUE": "1"}, {"FIXTURE_RESIDUE": "1"},
])
def test_real_systemd_shipping_cleanup_rejects_query_and_probe_residue(tmp_path: Path, env: dict[str, str]) -> None:
    assert _run_real_systemd_shipping_cleanup(tmp_path, **env).returncode != 0


@pytest.mark.parametrize("residue", [
    "etc/systemd/system/happyranch-managed.target", "run/systemd/system/happyranch-managed.target.d",
    "etc/happyranch/enrollment.key", ".happyranch-install-transaction.json", ".happyranch-backup",
    ".happyranch-units-backup", "opt/happyranch", "var/lib/happyranch-connector",
    "run/happyranch-tsnet-sidecar", "var/log/happyranch-connector", ".happyranch-stage-leftover",
    ".happyranch-tmp-leftover",
])
def test_real_systemd_shipping_cleanup_rejects_every_filesystem_residue_class(tmp_path: Path, residue: str) -> None:
    path = tmp_path / residue
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir() if "." not in path.name else path.write_text("residue")
    assert _run_real_systemd_shipping_cleanup(tmp_path).returncode != 0


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
