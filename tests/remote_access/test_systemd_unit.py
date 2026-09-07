"""Least-privilege systemd unit rendering (THR-097 phase unit 3).

The connector must run as a dedicated, unprivileged service user with a
minimal capability/filesystem/namespace posture and fail-closed restart
semantics. The renderer is pure and deterministic: the same spec always
produces the same bytes, so the checked-in tests are exact-match guards on
every load-bearing directive.
"""
from __future__ import annotations

import pytest

from runtime.remote_access.systemd_unit import ConnectorUnitSpec, render_connector_unit

CANONICAL_HARDENING = [
    "Type=notify",
    "Restart=on-failure",
    "RestartSec=1",
    "NoNewPrivileges=yes",
    "PrivateTmp=yes",
    "PrivateDevices=yes",
    "ProtectSystem=strict",
    "ProtectHome=yes",
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectControlGroups=yes",
    "RestrictSUIDSGID=yes",
    "RestrictRealtime=yes",
    "LockPersonality=yes",
    "MemoryDenyWriteExecute=yes",
    "CapabilityBoundingSet=",
    "AmbientCapabilities=",
    "UMask=0077",
]


def _spec(**overrides) -> ConnectorUnitSpec:
    fields = dict(
        unit_name="happyranch-connector.service",
        exec_start=("/opt/happyranch/venv/bin/python", "-m", "runtime.remote_access.cli", "run"),
        system=True,
        user="happyranch-connector",
        group="happyranch-connector",
        daemon_token_path="/home/happyranch/.happyranch/daemon.token",
        state_dir="happyranch-connector",
        run_dir="happyranch-connector",
        logs_dir="happyranch-connector",
        watchdog_sec=30,
        restart_sec=1,
    )
    fields.update(overrides)
    return ConnectorUnitSpec(**fields)


class TestCanonicalHardening:
    @pytest.mark.parametrize("directive", CANONICAL_HARDENING)
    def test_hardening_directive_present(self, directive) -> None:
        assert directive in render_connector_unit(_spec())

    def test_system_mode_has_service_user(self) -> None:
        text = render_connector_unit(_spec())
        assert "User=happyranch-connector" in text
        assert "Group=happyranch-connector" in text
        assert "WantedBy=multi-user.target" in text

    def test_exact_address_family_sandbox_is_unique(self) -> None:
        lines = [
            line
            for line in render_connector_unit(_spec()).splitlines()
            if line.startswith("RestrictAddressFamilies=")
        ]
        assert lines == ["RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK"]

    def test_user_mode_omits_service_user(self) -> None:
        text = render_connector_unit(_spec(system=False))
        assert "User=happyranch-connector" not in text
        assert "Group=happyranch-connector" not in text
        assert "WantedBy=default.target" in text

    def test_user_mode_omits_system_only_capability_directives(self) -> None:
        # The user manager cannot drop capabilities it does not hold (EPERM)
        # and cannot mount the private /dev or /lib/modules namespaces — a
        # user unit carrying these fails to start on real systemd.
        text = render_connector_unit(_spec(system=False))
        assert "CapabilityBoundingSet=" not in text
        assert "AmbientCapabilities=" not in text
        assert "PrivateDevices=yes" not in text
        assert "ProtectKernelModules=yes" not in text

    def test_system_mode_keeps_full_posture(self) -> None:
        text = render_connector_unit(_spec(system=True))
        assert "PrivateDevices=yes" in text
        assert "ProtectKernelModules=yes" in text
        assert "CapabilityBoundingSet=" in text
        assert "AmbientCapabilities=" in text

    def test_load_credential_pins_daemon_token_path(self) -> None:
        text = render_connector_unit(_spec())
        assert "LoadCredential=daemon.token:/home/happyranch/.happyranch/daemon.token" in text
        # The unit must never carry the bearer value itself — only the path.
        assert "LoadCredential" in text
        assert "Bearer " not in text

    def test_state_directories_declared(self) -> None:
        text = render_connector_unit(_spec())
        assert "StateDirectory=happyranch-connector" in text
        assert "RuntimeDirectory=happyranch-connector" in text
        assert "LogsDirectory=happyranch-connector" in text

    def test_watchdog_and_restart(self) -> None:
        text = render_connector_unit(_spec(watchdog_sec=15, restart_sec=3))
        assert "WatchdogSec=15" in text
        assert "RestartSec=3" in text

    def test_exec_start_uses_type_notify_contract(self) -> None:
        text = render_connector_unit(_spec())
        assert "Type=notify" in text
        assert "python" in text
        assert text.index("Type=notify") < text.index("[Install]")


class TestDeterminismAndStructure:
    def test_render_is_deterministic(self) -> None:
        assert render_connector_unit(_spec()) == render_connector_unit(_spec())

    def test_argv_quoting(self) -> None:
        text = render_connector_unit(
            _spec(exec_start=("/bin/echo", "hello world", "a'b"))
        )
        assert "ExecStart=/bin/echo 'hello world'" in text
        assert "a''b" in text

    def test_no_secret_material(self) -> None:
        text = render_connector_unit(_spec())
        assert "hrpair_" not in text
        assert "token_urlsafe" not in text
        # the daemon token VALUE is never rendered; only its path
        assert "daemon.token" in text  # path reference is required and safe

    def test_no_trailing_whitespace(self) -> None:
        for line in render_connector_unit(_spec()).splitlines():
            assert line == line.rstrip(), f"trailing whitespace: {line!r}"

    def test_install_section_last(self) -> None:
        text = render_connector_unit(_spec())
        assert text.rstrip().endswith("WantedBy=multi-user.target")

    def test_empty_capability_bounding_set(self) -> None:
        # A connector needs zero capabilities — bounding set must be empty.
        assert "CapabilityBoundingSet=" in render_connector_unit(_spec())
        assert "CapabilityBoundingSet=CAP_" not in render_connector_unit(_spec())
        assert "AmbientCapabilities=CAP_" not in render_connector_unit(_spec())
