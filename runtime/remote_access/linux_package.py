"""Deterministic Linux composite package for the managed embedded transport."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import zipfile
import subprocess
import stat
from typing import Callable, Mapping

from runtime.remote_access.systemd_unit import ConnectorUnitSpec, render_connector_unit


class PackageError(RuntimeError):
    """Stable, category-only package failure."""


PREFIX = "happyranch-linux-amd64"
UNITS = (
    "happyranch-connector.service",
    "happyranch-tsnet-sidecar.service",
    "happyranch-managed.target",
)
PAYLOAD_MODES = {
    "bin/happyranch-tsnet-sidecar": "0o755",
    "bin/happyranch-connector": "0o755",
    "share/happyranch.whl": "0o600",
    "share/dependency-inventory.json": "0o600",
    "share/sbom.cdx.json": "0o600",
    "share/THIRD_PARTY_NOTICES.md": "0o600",
    **{f"systemd/{name}": "0o600" for name in UNITS},
}
TRANSACTION_MARKER = ".happyranch-install-transaction.json"


def credential_capability(
    source: Path,
    *,
    expected_uid: int | None,
    allowed_modes: tuple[int, ...] | None = (0o600,),
    require_read_only: bool = False,
) -> str:
    """Classify credential usability without exposing paths, bytes, or OS errors."""
    path = Path(source)
    try:
        current = path
        while current != current.parent:
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                return "credential_unsafe_symlink"
            current = current.parent
    except FileNotFoundError:
        return "credential_absent"
    except OSError:
        return "credential_staging_incompatible"
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return "credential_wrong_type"
        if expected_uid is not None and metadata.st_uid != expected_uid:
            return "credential_wrong_custody"
        if allowed_modes is not None and stat.S_IMODE(metadata.st_mode) not in allowed_modes:
            return "credential_wrong_custody"
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not os.read(descriptor, 1):
                return "credential_staging_incompatible"
        finally:
            os.close(descriptor)
        if require_read_only:
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
            except OSError:
                pass
            else:
                os.close(descriptor)
                return "credential_staging_incompatible"
    except OSError:
        return "credential_staging_incompatible"
    return "credential_valid"


def require_credential_capability(
    source: Path,
    *,
    expected_uid: int,
    allowed_modes: tuple[int, ...] = (0o600,),
) -> None:
    category = credential_capability(
        source, expected_uid=expected_uid, allowed_modes=allowed_modes
    )
    if category != "credential_valid":
        raise PackageError(category)


class CompositeServiceManager:
    """Injectable executable seam for the shipping composite systemd target."""

    def __init__(self, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
                 systemctl: str = "systemctl") -> None:
        self._run, self._systemctl = run, systemctl

    def _call(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return self._run([self._systemctl, *args], check=True, text=True,
                             capture_output=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise PackageError("service_manager_failed") from exc

    def start_ready(self) -> None:
        self._call("start", "happyranch-managed.target")
        for unit in UNITS[:2]:
            result = self._call("show", unit, "--property=ActiveState", "--value")
            if result.stdout.strip() != "active":
                raise PackageError("service_not_ready")

    def stop(self) -> None:
        self._call("stop", "happyranch-managed.target")

    def restart_after_crash(self, unit: str) -> None:
        if unit not in UNITS[:2]:
            raise PackageError("service_unit_invalid")
        self._call("restart", unit)
        result = self._call("show", unit, "--property=ActiveState", "--value")
        if result.stdout.strip() != "active":
            raise PackageError("service_not_ready")


def render_composite_units(prefix: str = "/opt/happyranch") -> dict[str, str]:
    connector = render_connector_unit(ConnectorUnitSpec(
        exec_start=(f"{prefix}/bin/happyranch-tsnet-sidecar", "supervise-connector",
                    f"{prefix}/bin/happyranch-connector", "run", "--managed", "--config",
                    "/etc/happyranch/connector.json"),
        user="happyranch", group="happyranch",
        daemon_token_path="/etc/happyranch/daemon.token",
    )).replace("After=network-online.target", "After=network-online.target\nPartOf=happyranch-managed.target").replace(
        "[Service]\n", "[Service]\nExecStartPre={prefix}/bin/happyranch-connector credential-capability --name daemon.token --unit happyranch-connector.service\n".format(prefix=prefix), 1
    ).replace("WantedBy=multi-user.target", "WantedBy=happyranch-managed.target")
    sidecar = """[Unit]
Description=HappyRanch embedded tsnet sidecar
BindsTo=happyranch-connector.service
After=network-online.target
Wants=network-online.target
PartOf=happyranch-managed.target

[Service]
Type=notify
NotifyAccess=main
ExecStartPre=+{prefix}/bin/happyranch-connector reconcile-enrollment-retirement --source /etc/happyranch/enrollment.key --marker /var/lib/happyranch-tsnet-sidecar/credential.consumed --dropin /etc/systemd/system/happyranch-tsnet-sidecar.service.d/10-enrollment-credential.conf
ExecStartPre={prefix}/bin/happyranch-connector credential-capability --name enrollment.key --unit happyranch-tsnet-sidecar.service --consumed-marker /var/lib/happyranch-tsnet-sidecar/credential.consumed
ExecStart={prefix}/bin/happyranch-tsnet-sidecar --config /etc/happyranch/sidecar.json
ExecStartPost=+{prefix}/bin/happyranch-connector retire-enrollment-source --source /etc/happyranch/enrollment.key --marker /var/lib/happyranch-tsnet-sidecar/credential.consumed --dropin /etc/systemd/system/happyranch-tsnet-sidecar.service.d/10-enrollment-credential.conf
User=happyranch
Group=happyranch
Restart=on-failure
RestartSec=1
WatchdogSec=30
TimeoutStopSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
SystemCallArchitectures=native
CapabilityBoundingSet=
AmbientCapabilities=
UMask=0077
StateDirectory=happyranch-tsnet-sidecar
StateDirectoryMode=0700
RuntimeDirectory=happyranch-tsnet-sidecar
LogsDirectory=happyranch-tsnet-sidecar
[Install]
WantedBy=happyranch-managed.target
""".format(prefix=prefix)
    target = """[Unit]
Description=HappyRanch managed remote access composite
Requires=happyranch-connector.service happyranch-tsnet-sidecar.service
After=happyranch-connector.service happyranch-tsnet-sidecar.service
StopWhenUnneeded=yes
"""
    return {UNITS[0]: connector, UNITS[1]: sidecar, UNITS[2]: target}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sbom(inventory: Mapping[str, object], version: str) -> bytes:
    modules = inventory.get("modules")
    if not isinstance(modules, list) or not modules:
        raise PackageError("inventory_invalid")
    components = []
    for item in modules:
        if not isinstance(item, dict):
            raise PackageError("inventory_invalid")
        try:
            components.append({
                "type": "library", "name": item["module"], "version": item["version"],
                "purl": f"pkg:golang/{item['module']}@{item['version']}",
                "licenses": [{"license": {"id": item["spdx"]}}],
                "properties": [
                    {"name": "happyranch:go.sum", "value": item["sum"]},
                    {"name": "happyranch:license-sha256", "value": item["license_sha256"]},
                ],
            })
        except KeyError as exc:
            raise PackageError("inventory_invalid") from exc
    payload = {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
               "metadata": {"component": {"type": "application", "name": "happyranch-linux", "version": version}},
               "components": sorted(components, key=lambda item: (item["name"], item["version"]))}
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def build_linux_package(output: Path, sidecar: Path, connector: Path, wheel: Path,
                        inventory_path: Path, notices_path: Path, *, version: str) -> Path:
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        notices = notices_path.read_bytes()
        modules = inventory["modules"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PackageError("package_input_invalid") from exc
    _validate_evidence(inventory, notices)
    if not sidecar.is_file() or not connector.is_file() or not zipfile.is_zipfile(wheel):
        raise PackageError("package_input_invalid")
    with zipfile.ZipFile(wheel) as built_wheel:
        if not any(name.startswith("runtime/") and name.endswith(".py") for name in built_wheel.namelist()):
            raise PackageError("wheel_invalid")
    units = render_composite_units()
    files: dict[str, tuple[bytes, int]] = {
        "bin/happyranch-tsnet-sidecar": (sidecar.read_bytes(), 0o755),
        "bin/happyranch-connector": (connector.read_bytes(), 0o755),
        "share/happyranch.whl": (wheel.read_bytes(), 0o600),
        "share/dependency-inventory.json": (inventory_path.read_bytes(), 0o600),
        "share/sbom.cdx.json": (_sbom(inventory, version), 0o600),
        "share/THIRD_PARTY_NOTICES.md": (notices, 0o600),
    }
    files.update({f"systemd/{name}": (text.encode(), 0o600) for name, text in units.items()})
    manifest = {"schema_version": 1, "version": version, "architecture": "linux-amd64",
                "sidecar_dependency_count": len(modules),
                "files": [{"path": name, "sha256": _sha(raw), "mode": oct(mode)}
                          for name, (raw, mode) in sorted(files.items())]}
    files["manifest.json"] = ((json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(), 0o600)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as archive:
        for name, (raw, mode) in sorted(files.items()):
            info = tarfile.TarInfo(f"{PREFIX}/{name}")
            info.size, info.mode, info.mtime, info.uid, info.gid = len(raw), mode, 0, 0, 0
            info.uname = info.gname = "root"
            archive.addfile(info, io.BytesIO(raw))
    return output


def _validate_evidence(inventory: Mapping[str, object], notices: bytes) -> None:
    try:
        text = notices.decode("utf-8")
        if set(inventory) != {"schema_version", "artifact", "generator", "modules"} or type(inventory.get("schema_version")) is not int or inventory["schema_version"] != 1:
            raise PackageError("inventory_invalid")
        artifact = inventory["artifact"]
        if (not isinstance(artifact, dict) or set(artifact) != {"goos", "goarch", "cgo_enabled", "package"}
                or artifact != {"goos": "linux", "goarch": "amd64", "cgo_enabled": False,
                                "package": "happyranch/linux-tsnet-sidecar"}
                or inventory["generator"] != "tools/generate_inventory.py"):
            raise PackageError("inventory_invalid")
        modules = inventory["modules"]
        if not isinstance(modules, list) or not modules:
            raise PackageError("inventory_invalid")
    except (UnicodeDecodeError, KeyError, TypeError) as exc:
        raise PackageError("notice_invalid") from exc
    blocks = text.split("\n---\n")
    seen: dict[str, tuple[str, str]] = {}
    for block in blocks:
        spdx = next((line.removeprefix("SPDX: ") for line in block.splitlines() if line.startswith("SPDX: ")), None)
        digest = next((line.removeprefix("License-SHA256: ") for line in block.splitlines() if line.startswith("License-SHA256: ")), None)
        lines = block.splitlines()
        try:
            fence = lines.index("```text")
            end = lines.index("```", fence + 1)
            license_text = "\n".join(lines[fence + 1:end]).rstrip() + "\n"
        except ValueError:
            license_text = ""
        if not digest or not license_text or _sha(license_text.encode()) != digest:
            if any(line.startswith("- ") for line in lines):
                raise PackageError("notice_invalid")
        for line in lines:
            if line.startswith("- ") and "@" in line:
                coordinate = line[2:].strip()
                if coordinate in seen or not spdx or not digest:
                    raise PackageError("notice_invalid")
                seen[coordinate] = (spdx, digest)
    required = ("module", "version", "sum", "source", "spdx", "license_sha256", "relationship")
    if any(not isinstance(item, dict) or set(item) != set(required)
           or any(type(item.get(key)) is not str or not item[key] for key in required)
           or item["source"] != f"https://{item['module']}"
           or item["relationship"] != "statically-linked-linux-build-input"
           or not item["sum"].startswith("h1:")
           or len(item["license_sha256"]) != 64 for item in modules):
        raise PackageError("inventory_invalid")
    expected = {f"{item['module']}@{item['version']}": (item["spdx"], item["license_sha256"]) for item in modules}
    if len(expected) != len(modules):
        raise PackageError("inventory_invalid")
    if seen != expected:
        raise PackageError("notice_inventory_mismatch")


def _read_verified(package: Path) -> tuple[dict[str, bytes], dict[str, object]]:
    files: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    with tarfile.open(package) as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if not member.isfile() or path.is_absolute() or ".." in path.parts or path.parts[0] != PREFIX:
                raise PackageError("archive_member_invalid")
            relative = str(path.relative_to(PREFIX))
            if relative in files:
                raise PackageError("archive_duplicate_member")
            if member.uid != 0 or member.gid != 0 or member.uname != "root" or member.gname != "root":
                raise PackageError("archive_owner_invalid")
            files[relative] = archive.extractfile(member).read()
            modes[relative] = member.mode & 0o7777
    try:
        manifest = json.loads(files["manifest.json"])
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or manifest["architecture"] != "linux-amd64" or type(manifest["version"]) is not str or not manifest["version"]:
            raise PackageError("manifest_invalid")
        entries = manifest["files"]
        if not isinstance(entries, list) or type(manifest["sidecar_dependency_count"]) is not int:
            raise PackageError("manifest_invalid")
        expected_paths = set(files) - {"manifest.json"}
        declared_paths = {item["path"] for item in entries}
        if len(entries) != len(declared_paths) or declared_paths != expected_paths:
            raise PackageError("manifest_membership_mismatch")
        if declared_paths != set(PAYLOAD_MODES):
            raise PackageError("manifest_path_invalid")
        expected_modes = {name: int(mode, 8) for name, mode in PAYLOAD_MODES.items()}
        expected_modes["manifest.json"] = 0o600
        if any(modes[name] != mode for name, mode in expected_modes.items()):
            raise PackageError("archive_mode_invalid")
        for item in entries:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "mode"} or any(type(item[k]) is not str for k in item):
                raise PackageError("manifest_invalid")
            path = PurePosixPath(item["path"])
            if path.is_absolute() or ".." in path.parts or str(path) != item["path"] or item["path"] not in PAYLOAD_MODES:
                raise PackageError("manifest_path_invalid")
            if item["mode"] != PAYLOAD_MODES[item["path"]]:
                raise PackageError("manifest_mode_invalid")
            if _sha(files[item["path"]]) != item["sha256"]:
                raise PackageError("manifest_hash_mismatch")
        inventory = json.loads(files["share/dependency-inventory.json"])
        if manifest["sidecar_dependency_count"] != len(inventory["modules"]):
            raise PackageError("manifest_count_invalid")
        sbom = json.loads(files["share/sbom.cdx.json"])
        if (type(sbom.get("version")) is not int or sbom.get("bomFormat") != "CycloneDX"
                or sbom.get("specVersion") != "1.5" or not isinstance(sbom.get("components"), list)):
            raise PackageError("sbom_invalid")
        def component_tuple(c: object) -> tuple[object, ...]:
            if not isinstance(c, dict): raise PackageError("sbom_invalid")
            props = c.get("properties")
            licenses = c.get("licenses")
            if not isinstance(props, list) or not isinstance(licenses, list) or len(licenses) != 1:
                raise PackageError("sbom_invalid")
            prop_map = {p.get("name"): p.get("value") for p in props if isinstance(p, dict)}
            try: spdx = licenses[0]["license"]["id"]
            except (KeyError, TypeError): raise PackageError("sbom_invalid")
            return (c.get("name"), c.get("version"), c.get("purl"), spdx,
                    prop_map.get("happyranch:go.sum"), prop_map.get("happyranch:license-sha256"))
        components = {component_tuple(c) for c in sbom["components"]}
        inventory_coordinates = {(m["module"], m["version"], f"pkg:golang/{m['module']}@{m['version']}",
                                  m["spdx"], m["sum"], m["license_sha256"]) for m in inventory["modules"]}
        if len(components) != len(sbom["components"]) or components != inventory_coordinates:
            raise PackageError("sbom_inventory_mismatch")
        _validate_evidence(inventory, files["share/THIRD_PARTY_NOTICES.md"])
    except PackageError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise PackageError("manifest_invalid") from exc
    return files, manifest


def _transaction_paths(root: Path) -> tuple[Path, Path, Path]:
    return root / ".happyranch-backup", root / ".happyranch-units-backup", root / TRANSACTION_MARKER


def _recover_interrupted(root: Path) -> None:
    """Classify and roll an interrupted publication back to its last-known-good set."""
    opt, units = root / "opt/happyranch", root / "etc/systemd/system"
    backup, unit_backup, marker = _transaction_paths(root)
    stages = list(root.glob(".happyranch-stage-*")) if root.exists() else []
    if not marker.exists():
        if backup.exists() or (unit_backup.exists() and any(unit_backup.iterdir())):
            raise PackageError("transaction_state_invalid")
        if unit_backup.exists(): shutil.rmtree(unit_backup)
        for stage in stages: shutil.rmtree(stage)
        return
    try:
        state = json.loads(marker.read_text())
        if set(state) != {"schema_version", "phase"} or type(state["schema_version"]) is not int or state["schema_version"] != 1 or state["phase"] not in {"prepared", "payload_retained", "payload_published", "units_publishing"}:
            raise PackageError("transaction_state_invalid")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise PackageError("transaction_state_invalid") from exc
    if backup.exists():
        if opt.exists(): shutil.rmtree(opt)
        opt.parent.mkdir(parents=True, exist_ok=True)
        backup.replace(opt)
    elif state["phase"] != "prepared" and not (
        state["phase"] == "payload_retained" and not opt.exists()
    ):
        raise PackageError("transaction_state_invalid")
    if unit_backup.exists():
        units.mkdir(parents=True, exist_ok=True)
        for unit in UNITS:
            target, saved = units / unit, unit_backup / unit
            if target.exists(): target.unlink()
            if saved.exists(): saved.replace(target)
        shutil.rmtree(unit_backup)
    for stage in stages: shutil.rmtree(stage)
    marker.unlink()


def install_linux_package(
    package: Path,
    root: Path,
    *,
    system_service: bool = False,
    fault: Callable[[str], None] | None = None,
) -> dict[str, object]:
    if type(system_service) is not bool:
        raise PackageError("install_mode_invalid")
    if system_service:
        require_credential_capability(
            root / "etc/happyranch/daemon.token", expected_uid=os.geteuid()
        )
        enrollment_source = root / "etc/happyranch/enrollment.key"
        if enrollment_source.exists() or enrollment_source.is_symlink():
            require_credential_capability(
                enrollment_source, expected_uid=os.geteuid()
            )
    files, manifest = _read_verified(package)
    _recover_interrupted(root)
    opt = root / "opt/happyranch"
    units = root / "etc/systemd/system"
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".happyranch-stage-", dir=root))
    staging.chmod(0o755 if system_service else 0o700)
    backup, unit_backup, marker = _transaction_paths(root)
    checkpoint = fault or (lambda _name: None)
    try:
        for name, raw in files.items():
            if name == "manifest.json" or name.startswith("systemd/"):
                continue
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.parent.chmod(
                0o755 if system_service and name.startswith("bin/") else 0o700
            )
            target.write_bytes(raw)
            mode = int(PAYLOAD_MODES[name], 8) if system_service else (0o700 if name.startswith("bin/") else 0o600)
            target.chmod(mode)
        (staging / "manifest.json").write_bytes(files["manifest.json"])
        (staging / "manifest.json").chmod(0o600)
        units.mkdir(parents=True, exist_ok=True)
        unit_backup.mkdir(mode=0o700)
        for unit in UNITS:
            target = units / unit
            if target.exists(): shutil.copy2(target, unit_backup / unit)
        opt.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"phase":"prepared","schema_version":1}\n')
        marker.chmod(0o600)
        if opt.exists():
            opt.replace(backup)
        marker.write_text('{"phase":"payload_retained","schema_version":1}\n')
        checkpoint("payload_old_retained")
        staging.replace(opt)
        marker.write_text('{"phase":"payload_published","schema_version":1}\n')
        checkpoint("payload_published")
        marker.write_text('{"phase":"units_publishing","schema_version":1}\n')
        for unit in UNITS:
            target = units / unit
            target.write_bytes(files[f"systemd/{unit}"])
            target.chmod(0o600)
            checkpoint(f"unit_published:{unit}")
        credential_source = root / "etc/happyranch/enrollment.key"
        credential_dropin = units / "happyranch-tsnet-sidecar.service.d/10-enrollment-credential.conf"
        if system_service and credential_source.is_file():
            credential_dropin.parent.mkdir(mode=0o755, exist_ok=True)
            credential_dropin.write_text(
                "[Service]\nLoadCredential=enrollment.key:/etc/happyranch/enrollment.key\n"
            )
            credential_dropin.chmod(0o600)
        shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(unit_backup, ignore_errors=True)
        marker.unlink()
    except Exception:
        credential_dropin = units / "happyranch-tsnet-sidecar.service.d/10-enrollment-credential.conf"
        if credential_dropin.exists(): credential_dropin.unlink()
        shutil.rmtree(staging, ignore_errors=True)
        if opt.exists(): shutil.rmtree(opt)
        if backup.exists(): backup.replace(opt)
        if unit_backup.exists():
            for unit in UNITS:
                target = units / unit
                if target.exists(): target.unlink()
                saved = unit_backup / unit
                if saved.exists(): saved.replace(target)
        shutil.rmtree(unit_backup, ignore_errors=True)
        if marker.exists(): marker.unlink()
        raise
    return {"version": manifest["version"], "manifest_sha256": _sha(files["manifest.json"])}


def uninstall_linux_package(root: Path) -> None:
    opt = root / "opt/happyranch"
    if opt.exists():
        shutil.rmtree(opt)
    for unit in UNITS:
        path = root / "etc/systemd/system" / unit
        if path.exists():
            path.unlink()
