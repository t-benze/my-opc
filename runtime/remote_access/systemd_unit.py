"""Least-privilege systemd unit rendering (THR-097 phase unit 3).

Renders the connector service unit file with a canonical hardening posture:

- a dedicated unprivileged service user (system mode) or the calling user
  (user mode — no ``User=``/``Group=`` allowed in user units);
- zero capabilities (``CapabilityBoundingSet=``/``AmbientCapabilities=``
  empty), ``NoNewPrivileges=yes``, ``ProtectSystem=strict`` with explicit
  state/log/runtime directories, ``ProtectHome=yes``, private tmp/dev,
  restricted address families, SUID/SGID + realtime + personality +
  write-execute-memory restrictions;
- the daemon bearer is injected into the unit ONLY via ``LoadCredential=``
  (the daemon token file path, never the token value) so the service user
  never needs direct access to the daemon home;
- ``Type=notify`` + ``WatchdogSec=`` (the supervisor reports READY/WATCHDOG
  through sd_notify) and ``Restart=on-failure`` for crash recovery.

Rendering is pure and deterministic. This is packaging surface only: no
daemon, gateway, auth, schema, permission-model, or dependency change.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Canonical least-privilege directive set. Every directive is load-bearing
# for the unit's posture; the checked-in tests guard each line exactly.
_SYSTEM_HARDENING: tuple[str, ...] = (
    "Type=notify",
    "Restart=on-failure",
    "RestartSec=1",
    "NoNewPrivileges=yes",
    "PrivateTmp=yes",
    "ProtectSystem=strict",
    "ProtectHome=yes",
    "ProtectKernelTunables=yes",
    "ProtectControlGroups=yes",
    "RestrictSUIDSGID=yes",
    "RestrictRealtime=yes",
    "LockPersonality=yes",
    "MemoryDenyWriteExecute=yes",
    "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK",
    "SystemCallArchitectures=native",
    "UMask=0077",
)

# System-mode-only directives. The USER manager cannot apply these: an empty
# capability bounding set means dropping capabilities the user manager does
# not itself hold (EPERM), and ``PrivateDevices=``/``ProtectKernelModules=``
# require namespace mounts with elevated privileges the user manager lacks —
# a user unit carrying them fails to start on real systemd (verified on
# Ubuntu 26.04 systemd, THR-097 unit 3 conformance). The system service
# (with a dedicated unprivileged service user) gets the full set; user-mode
# units omit all four.
_SYSTEM_ONLY_HARDENING: tuple[str, ...] = (
    "PrivateDevices=yes",
    "ProtectKernelModules=yes",
    "CapabilityBoundingSet=",
    "AmbientCapabilities=",
)

_DEFAULT_WATCHDOG_SEC = 30
_DEFAULT_RESTART_SEC = 1


@dataclass(frozen=True)
class ConnectorUnitSpec:
    """Everything needed to render the connector service unit."""

    unit_name: str = "happyranch-connector.service"
    exec_start: tuple[str, ...] = (
        "/opt/happyranch/venv/bin/python",
        "-m",
        "runtime.remote_access.cli",
        "run",
    )
    system: bool = True
    user: str | None = None
    group: str | None = None
    daemon_token_path: str = ""
    state_dir: str = "happyranch-connector"
    run_dir: str = "happyranch-connector"
    logs_dir: str = "happyranch-connector"
    watchdog_sec: int = _DEFAULT_WATCHDOG_SEC
    restart_sec: int = _DEFAULT_RESTART_SEC
    extra_environment: tuple[str, ...] = field(default_factory=tuple)


def _quote_systemd_arg(arg: str) -> str:
    """Quote one ExecStart argument per systemd's simple quoting rules.

    systemd single-quotes words containing spaces or quotes; an embedded
    single quote is escaped as ``''`` inside a quoted word.
    """
    if any(c in arg for c in " \t'\"\\\n"):
        return "'" + arg.replace("'", "''") + "'"
    return arg


def render_connector_unit(spec: ConnectorUnitSpec) -> str:
    """Render the connector systemd unit file deterministically."""
    lines: list[str] = []
    lines.append("[Unit]")
    lines.append("Description=HappyRanch remote-access connector (supervised Linux home connector)")
    lines.append("After=network-online.target")
    lines.append("Wants=network-online.target")
    lines.append("")
    lines.append("[Service]")
    exec_line = " ".join(_quote_systemd_arg(arg) for arg in spec.exec_start)
    lines.append(f"ExecStart={exec_line}")
    if spec.system:
        if not spec.user or not spec.group:
            raise ValueError("system-mode connector unit requires user and group")
        lines.append(f"User={spec.user}")
        lines.append(f"Group={spec.group}")
    lines.append(f"RestartSec={spec.restart_sec}")
    lines.append(f"WatchdogSec={spec.watchdog_sec}")
    lines.append("TimeoutStopSec=10")
    lines.extend(_SYSTEM_HARDENING)
    lines.append("NotifyAccess=main")
    if spec.system:
        lines.extend(_SYSTEM_ONLY_HARDENING)
    lines.append(f"StateDirectory={spec.state_dir}")
    lines.append(f"RuntimeDirectory={spec.run_dir}")
    lines.append(f"LogsDirectory={spec.logs_dir}")
    if spec.daemon_token_path:
        # The token VALUE is never rendered — only the file path, which
        # systemd copies into $CREDENTIALS_DIRECTORY at unit start.
        lines.append(f"LoadCredential=daemon.token:{spec.daemon_token_path}")
    for env in spec.extra_environment:
        lines.append(f"Environment={env}")
    lines.append("")
    lines.append("[Install]")
    lines.append("WantedBy=multi-user.target" if spec.system else "WantedBy=default.target")
    return "\n".join(lines) + "\n"
