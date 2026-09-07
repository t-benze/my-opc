"""Connector supervisor tests (THR-097 phase unit 3).

Deterministic lifecycle coverage with fakes: readiness-gated listener start,
fail-closed listener stop on readiness loss, READY/WATCHDOG/STOPPING notify,
service-manager delegation (install/start/stop/upgrade/rollback), lab device
provisioning in first-run state, revocation across a restart, and redacted
local diagnostics.
"""
from __future__ import annotations

import json
import os
import shlex
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from runtime.remote_access.authorization import DeviceAuthorization, TrustState
from runtime.remote_access.credentials import (
    CredentialUnavailable,
    SystemdCredentialProvider,
)
from runtime.remote_access.diy_provider import DiyProviderError
from runtime.remote_access.lab_provider import LAB_ONLY_BANNER, LabProviderConfig, LabProviderError
from runtime.remote_access.managed_provider import (
    ManagedProviderAdapter,
    ManagedProviderConfig,
)
from runtime.remote_access.policy import PolicyEnvelope
from runtime.remote_access.readiness import (
    ConnectorReadiness,
    GateResult,
    ReadinessReport,
)
from runtime.remote_access.revocation import RevocationCoordinator
from runtime.remote_access.service_manager import ServiceStatus, UpgradeOutcome
from runtime.remote_access.state_store import (
    AtomicFileTrustStateStore,
    StateStoreError,
)
from runtime.remote_access.streams import StreamRegistry

from .fake_daemon import FakeDaemon
from runtime.remote_access.supervisor import (
    ConnectorConfig,
    ConnectorConfigError,
    ConnectorSupervisor,
    sd_notify,
)

from .conftest import NOW, build_consumer, default_identity, make_policy_envelope

_UNSET = object()

# A real customer-owned-network bind address for the supervisor-seam socket
# tests: the host's first non-loopback IPv4 (hairpin to self is a genuine TCP
# path through the kernel stack). Skipped with reason when the host has none.
def _host_network_ipv4() -> str | None:
    try:
        import subprocess

        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        for i, part in enumerate(parts):
            if part == "inet" and i + 1 < len(parts):
                addr = parts[i + 1].split("/")[0]
                if addr.startswith("127."):
                    continue
                try:
                    from runtime.remote_access.network import validate_customer_network_address

                    validate_customer_network_address(addr)
                    return addr
                except Exception:
                    continue
    return None


NETWORK_IPV4 = _host_network_ipv4()
BEARER = "supervisor-seam-bearer-7"


def _tailscale_stub(tmp_path, address: str) -> str:
    """A stub ``tailscale ip -4`` executable resolving to *address* — the
    REAL shipping resolution path (TailscaleCliResolver -> subprocess ->
    validation) without requiring a live tailnet on the test host."""
    stub = tmp_path / "tailscale-stub"
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' '{address}'\n")
    stub.chmod(0o755)
    return str(stub)

def _sse_closed_after(resp, timeout: float = 8.0) -> bool:
    """True when the SSE connection closes within *timeout* seconds (EOF or
    connection error). select-bounded via the response's own buffered reader
    (http.client nulls ``conn.sock`` on Connection: close responses, but the
    response's ``fp`` stays the live channel)."""
    import select
    import time as _time

    fp = resp.fp
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            ready, _, _ = select.select([fp], [], [], 0.2)
        except (OSError, ValueError):
            return True  # fd gone — closed
        if not ready:
            continue
        try:
            chunk = resp.read1(1024)
        except Exception:
            return True  # reset/refused/closed-file — the stream is gone
        if not chunk:
            return True  # EOF — closed
    return False




class FakeManager:
    def __init__(self) -> None:
        self.installed: list[tuple[str, str]] = []
        self.calls: list[str] = []
        self.status_result = ServiceStatus(
            unit_name="happyranch-connector.service",
            active_state="active",
            sub_state="running",
            load_state="loaded",
            pid=4242,
        )

    def install(self, unit_text, unit_name, *, enable=True):
        self.installed.append((unit_name, unit_text))
        self.calls.append("install")
        return Path("/fake/unit")

    def uninstall(self, unit_name):
        self.calls.append("uninstall")

    def start(self, unit_name):
        self.calls.append("start")

    def stop(self, unit_name):
        self.calls.append("stop")

    def restart(self, unit_name):
        self.calls.append("restart")

    def enable(self, unit_name):
        self.calls.append("enable")

    def disable(self, unit_name):
        self.calls.append("disable")

    def status(self, unit_name):
        self.calls.append("status")
        return self.status_result

    def upgrade(self, unit_text, unit_name, *, verify_start=True):
        self.calls.append("upgrade")
        return UpgradeOutcome(ok=True)

    def rollback(self, unit_name):
        self.calls.append("rollback")
        return UpgradeOutcome(ok=True)


class _FakeProvider:
    def __init__(self, fail_start: bool = False) -> None:
        self.starts = 0
        self.stops = 0
        self.listening = False
        self.bound_port = None
        self.fail_start = fail_start

    def start(self) -> None:
        if self.fail_start:
            raise LabProviderError("bind conflict")
        self.starts += 1
        self.listening = True

    def stop(self) -> None:
        self.stops += 1
        self.listening = False


class _FakeReadiness:
    """Readiness whose gates the test flips deterministically."""

    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.evaluations = 0

    def evaluate(self, now) -> ReadinessReport:
        self.evaluations += 1
        gates = {
            name: GateResult(True, f"{name}_ok", f"{name} ok")
            for name in ConnectorReadiness.GATE_NAMES
        }
        if not self.ready:
            gates["daemon_loopback"] = GateResult(
                False, "daemon_unavailable", "no daemon"
            )
        return ReadinessReport(ready=self.ready, gates=gates)


def _config(tmp_path, *, lab: bool = True, **overrides) -> ConnectorConfig:
    fields = dict(
        tenant_id="tenant-a",
        home_id="home-a",
        connector_id="connector-a",
        daemon_port=8999,
        daemon_token_path=str(tmp_path / "daemon.token"),
        policy_path=str(tmp_path / "policy.json"),
        state_path=str(tmp_path / "state.json"),
        unit_name="happyranch-connector.service",
        system=False,
        lab=LabProviderConfig(bind_host="127.0.0.1", lab_only=True)
        if lab
        else None,
    )
    fields.update(overrides)
    return ConnectorConfig(**fields)


def _supervisor(
    tmp_path,
    *,
    manager=None,
    readiness=None,
    provider=_UNSET,
    lab: bool = True,
    config=None,
    policy=None,
) -> ConnectorSupervisor:
    config = config or _config(tmp_path, lab=lab)
    if provider is _UNSET:
        provider = _FakeProvider()
    return ConnectorSupervisor(
        config=config,
        manager=manager or FakeManager(),
        readiness=readiness or _FakeReadiness(ready=True),
        provider=provider,
        policy=policy,
        now_fn=lambda: NOW(),
        notify_fn=lambda state: notifications.append(state),
    )


notifications: list[str] = []


def _notified(substring: str) -> bool:
    return any(substring in n for n in notifications)


class TestConfig:
    def test_validate_requires_identity_and_sources(self, tmp_path) -> None:
        with pytest.raises(ConnectorConfigError):
            ConnectorConfig().validate()
        with pytest.raises(ConnectorConfigError):
            _config(tmp_path, daemon_port=None).validate()
        with pytest.raises(ConnectorConfigError):
            _config(tmp_path, daemon_token_path=None, credentials_directory=None).validate()
        with pytest.raises(ConnectorConfigError):
            _config(tmp_path, policy_path=None).validate()
        with pytest.raises(ConnectorConfigError):
            cfg = _config(tmp_path)
            cfg.lab = LabProviderConfig(bind_host="127.0.0.1", lab_only=False)
            cfg.validate()

    def test_from_file_roundtrip(self, tmp_path) -> None:
        config = _config(tmp_path)
        path = tmp_path / "config.json"
        config.to_file(path)
        loaded = ConnectorConfig.from_file(path)
        assert loaded.tenant_id == "tenant-a"
        assert loaded.lab is not None
        assert loaded.lab.lab_only is True
        assert loaded.daemon_port == 8999

    def test_managed_roundtrip_is_exclusive_and_literal_loopback(
        self, tmp_path
    ) -> None:
        config = _config(
            tmp_path, lab=False, managed=ManagedProviderConfig(bind_port=9443)
        )
        path = tmp_path / "managed.json"
        config.to_file(path)
        loaded = ConnectorConfig.from_file(path)
        assert loaded.managed == ManagedProviderConfig(bind_port=9443)
        with pytest.raises(ConnectorConfigError, match="mutually exclusive"):
            _config(tmp_path, managed=ManagedProviderConfig()).validate()
        with pytest.raises(ConnectorConfigError, match="invalid managed"):
            _config(
                tmp_path,
                lab=False,
                managed=ManagedProviderConfig(bind_host="0.0.0.0"),
            ).validate()

    def test_managed_factory_and_unit_use_explicit_closed_mode(
        self, tmp_path, route_policy_fixture
    ) -> None:
        config = _config(tmp_path, lab=False, managed=ManagedProviderConfig(bind_port=0))
        supervisor = _supervisor(
            tmp_path,
            config=config,
            provider=None,
            lab=False,
            readiness=_FakeReadiness(ready=True),
            policy=build_consumer(route_policy_fixture),
        )
        provider = supervisor.build_managed_provider()
        assert isinstance(provider, ManagedProviderAdapter)
        assert provider.bind_address == "127.0.0.1"
        spec = supervisor.unit_spec()
        assert "--managed" in spec.exec_start
        assert "--diy" not in spec.exec_start and "--lab-only" not in spec.exec_start


class TestRunLoop:
    def test_ready_starts_provider_and_notifies_readiness(
        self, tmp_path
    ) -> None:
        notifications.clear()
        provider = _FakeProvider()
        supervisor = _supervisor(tmp_path, provider=provider)
        supervisor.run(max_iterations=1, poll_seconds=0)
        assert provider.starts == 1
        assert supervisor._provider_running is True
        assert _notified("READY=1")

    def test_not_ready_never_starts_provider(self, tmp_path) -> None:
        notifications.clear()
        provider = _FakeProvider()
        supervisor = _supervisor(
            tmp_path, provider=provider, readiness=_FakeReadiness(ready=False)
        )
        supervisor.run(max_iterations=1, poll_seconds=0)
        assert provider.starts == 0
        assert _notified("STATUS=waiting for readiness")

    def test_readiness_loss_stops_listener_immediately(self, tmp_path) -> None:
        notifications.clear()
        provider = _FakeProvider()
        readiness = _FakeReadiness(ready=True)
        supervisor = _supervisor(tmp_path, provider=provider, readiness=readiness)
        supervisor.run(max_iterations=1, poll_seconds=0)
        assert provider.starts == 1
        readiness.ready = False  # daemon died / token perms loosened
        supervisor.run(max_iterations=1, poll_seconds=0)
        assert provider.stops == 1
        assert supervisor._provider_running is False
        assert _notified("STOPPING=1")

    def test_provider_refused_start_keeps_no_listener(self, tmp_path) -> None:
        notifications.clear()
        supervisor = _supervisor(tmp_path, provider=_FakeProvider(fail_start=True))
        supervisor.run(max_iterations=1, poll_seconds=0)
        assert supervisor._provider_running is False
        assert any("provider failed to start" in n for n in notifications)
        assert not _notified("READY=1")  # never READY without a proven listener

    def test_provider_start_failure_never_emits_ready(self, tmp_path) -> None:
        """The reviewer's [HIGH] READY-ordering finding: READY=1 must NEVER be
        emitted unless the provider actually started (listener proven). On a
        bind/start failure the loop retries, reports STATUS only, and the
        supervisor stays deterministically not-running with no listener."""
        notifications.clear()
        provider = _FakeProvider(fail_start=True)
        supervisor = _supervisor(tmp_path, provider=provider)
        supervisor.run(max_iterations=2, poll_seconds=0)
        assert (
            sum("provider failed to start" in n for n in notifications) == 2
        )  # the loop retried each poll
        assert supervisor._provider_running is False
        assert not _notified("READY=1")
        assert not _notified("WATCHDOG=1")
        assert any("provider failed to start" in n for n in notifications)
        # fail-closed status/shutdown: still not running, still no READY
        supervisor.shutdown()
        assert supervisor._provider_running is False
        assert provider.stops == 2  # every partial start gets cleanup
        assert not _notified("READY=1")

    def test_real_occupied_port_keeps_supervised_retry_never_ready(self, tmp_path) -> None:
        """QA TASK-6014 structural diagnosis through the SHIPPING path: the REAL
        ``LabProviderAdapter`` bind failure (occupied port) raised a bare
        OSError that escaped ``_start_provider``'s ``LabProviderError`` catch,
        crashing ``run()`` instead of retrying. With the adapter boundary
        normalization: zero READY, secret-free STATUS, no listener, and the
        loop keeps re-evaluating (no process exit, no systemd restart
        needed)."""
        notifications.clear()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = int(blocker.getsockname()[1])
            # Constructed directly (NOT via _config): the _config helper's
            # named ``lab`` param would shadow the occupied port with its
            # default ephemeral config.
            config = ConnectorConfig(
                tenant_id="tenant-a",
                home_id="home-a",
                connector_id="connector-a",
                daemon_port=8999,
                daemon_token_path=str(tmp_path / "daemon.token"),
                policy_path=str(tmp_path / "policy.json"),
                state_path=str(tmp_path / "state.json"),
                unit_name="happyranch-connector.service",
                system=False,
                lab=LabProviderConfig(
                    bind_host="127.0.0.1", bind_port=port, lab_only=True
                ),
            )
            # provider=None: NO injected fake — build_provider() constructs
            # the REAL LabProviderAdapter from the config.
            supervisor = ConnectorSupervisor(
                config=config,
                manager=FakeManager(),
                readiness=_FakeReadiness(ready=True),
                provider=None,
                now_fn=lambda: NOW(),
                notify_fn=lambda state: notifications.append(state),
            )
            supervisor.run(max_iterations=2, poll_seconds=0)  # must NOT raise
            assert supervisor._provider_running is False
            assert supervisor._provider is None  # no listener of ours
            assert (
                sum("provider failed to start" in n for n in notifications) == 2
            )  # supervised retry each poll
            assert not _notified("READY=1")
            assert not _notified("WATCHDOG=1")
            # secret-free STATUS: never leaks tokens/paths/exception text
            for n in notifications:
                assert "token" not in n.lower()
                assert "daemon" not in n.lower()
                assert "Errno" not in n
            supervisor.shutdown()
            assert supervisor._provider_running is False

    def test_real_occupied_port_recovers_when_freed(self, tmp_path) -> None:
        """QA TASK-6014 supervised retry/recovery: while the port is occupied
        the loop reports STATUS with zero READY; once the conflict clears the
        NEXT iteration binds a real listener and emits READY — the process
        never exits, so systemd never restart-throttles."""
        notifications.clear()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = int(blocker.getsockname()[1])
        # Constructed directly (NOT via _config): see the sibling test above.
        config = ConnectorConfig(
            tenant_id="tenant-a",
            home_id="home-a",
            connector_id="connector-a",
            daemon_port=8999,
            daemon_token_path=str(tmp_path / "daemon.token"),
            policy_path=str(tmp_path / "policy.json"),
            state_path=str(tmp_path / "state.json"),
            unit_name="happyranch-connector.service",
            system=False,
            lab=LabProviderConfig(
                bind_host="127.0.0.1", bind_port=port, lab_only=True
            ),
        )
        supervisor = ConnectorSupervisor(
            config=config,
            manager=FakeManager(),
            readiness=_FakeReadiness(ready=True),
            provider=None,
            now_fn=lambda: NOW(),
            notify_fn=lambda state: notifications.append(state),
        )
        try:
            supervisor.run(max_iterations=1, poll_seconds=0)
            assert not _notified("READY=1")  # occupied: no listener, no READY
            blocker.close()  # release the conflict
            supervisor.run(max_iterations=1, poll_seconds=0)
            assert _notified("READY=1")  # freed: recovered and listening
            assert supervisor._provider_running is True
            adapter = supervisor._provider
            assert adapter is not None
            assert adapter.listening is True
            assert adapter.bound_port == port
        finally:
            blocker.close()
            supervisor.shutdown()

    def test_unexpected_provider_defect_propagates(self, tmp_path) -> None:
        """The LabProviderError category contract is OPERATIONAL-only: an
        unexpected defect inside provider start (a programming error, not a
        bind/listen OSError) must propagate loudly — never be normalized or
        swallowed into the retry loop."""
        notifications.clear()

        class _DefectiveProvider:
            def start(self) -> None:
                raise ValueError("programming defect")

            def stop(self) -> None:
                pass

        supervisor = _supervisor(tmp_path, provider=_DefectiveProvider())
        with pytest.raises(ValueError, match="programming defect"):
            supervisor.run(max_iterations=1, poll_seconds=0)
        assert not _notified("READY=1")

    def test_shutdown_stops_provider(self, tmp_path) -> None:
        notifications.clear()
        provider = _FakeProvider()
        supervisor = _supervisor(tmp_path, provider=provider)
        supervisor.run(max_iterations=1, poll_seconds=0)
        supervisor.shutdown()
        assert provider.stops == 1
        assert _notified("STOPPING=1")

    def test_concurrent_provider_start_then_shutdown_is_linearized(self, tmp_path) -> None:
        entered = threading.Event()
        release = threading.Event()
        events: list[str] = []

        class BlockingProvider(_FakeProvider):
            def start(self) -> None:
                events.append("start_entered")
                entered.set()
                assert release.wait(timeout=5)
                super().start()
                events.append("listener_started")

            def stop(self) -> None:
                events.append("listener_stopped")
                super().stop()

        provider = BlockingProvider()
        supervisor = ConnectorSupervisor(
            config=_config(tmp_path),
            manager=FakeManager(),
            readiness=_FakeReadiness(ready=True),
            provider=provider,
            now_fn=lambda: NOW(),
            notify_fn=lambda state: events.append(state.strip()),
        )
        runner = threading.Thread(
            target=lambda: supervisor.run(max_iterations=1, poll_seconds=0)
        )
        stopper = threading.Thread(target=supervisor.shutdown)
        runner.start()
        assert entered.wait(timeout=5)
        stopper.start()
        assert stopper.is_alive(), "shutdown must wait for the in-flight start boundary"
        release.set()
        runner.join(timeout=5)
        stopper.join(timeout=5)

        assert not runner.is_alive() and not stopper.is_alive()
        assert events == [
            "start_entered",
            "listener_started",
            "READY=1",
            "listener_stopped",
            "STOPPING=1",
        ]
        assert provider.starts == provider.stops == 1
        assert not provider.listening and not supervisor._provider_running

    def test_shutdown_first_forbids_late_start_and_repeated_shutdown_has_no_residue(
        self, tmp_path
    ) -> None:
        events: list[str] = []
        provider = _FakeProvider()
        supervisor = ConnectorSupervisor(
            config=_config(tmp_path),
            manager=FakeManager(),
            readiness=_FakeReadiness(ready=True),
            provider=provider,
            now_fn=lambda: NOW(),
            notify_fn=lambda state: events.append(state.strip()),
        )
        supervisor.shutdown()
        supervisor.shutdown()
        assert supervisor._start_provider() is False
        assert supervisor.run(max_iterations=1, poll_seconds=0) == 0

        assert provider.starts == provider.stops == 0
        assert events == ["STOPPING=1", "STOPPING=1"]
        assert supervisor._provider is None
        assert supervisor._registry is None
        assert supervisor._pairing_manager is None

    def test_repeated_shutdown_closes_active_shipping_runtime_exactly_once(
        self, tmp_path
    ) -> None:
        events: list[str] = []

        class ObservableProvider(_FakeProvider):
            def stop(self) -> None:
                events.append("listener_stopped")
                super().stop()

        class ActiveFlow:
            stream_id = "active-flow"

            def __init__(self) -> None:
                self.close_count = 0

            @property
            def closed(self) -> bool:
                return self.close_count > 0

            def receive(self) -> bytes | None:
                return b"active"

            def close(self) -> None:
                events.append("active_flow_closed")
                self.close_count += 1

        provider = ObservableProvider()
        supervisor = ConnectorSupervisor(
            config=_config(tmp_path),
            manager=FakeManager(),
            readiness=_FakeReadiness(ready=True),
            provider=provider,
            now_fn=lambda: NOW(),
            notify_fn=lambda state: events.append(state.strip()),
        )
        assert supervisor._start_provider() is True
        registry = supervisor.registry
        pairing = supervisor.pairing_manager()
        flow = ActiveFlow()
        tracked = registry.open(flow.stream_id, flow)

        shutdown = threading.Thread(target=lambda: (supervisor.shutdown(), supervisor.shutdown()))
        shutdown.start()
        shutdown.join(timeout=5)

        assert not shutdown.is_alive(), "repeated shutdown must complete without deadlock"
        assert events.index("listener_stopped") < events.index("active_flow_closed")
        assert provider.starts == provider.stops == 1
        assert not provider.listening and not supervisor._provider_running
        assert flow.close_count == 1 and tracked.closed
        assert registry.sealed and registry.open_count() == 0
        assert supervisor._provider is None
        assert supervisor._registry is None
        assert supervisor._pairing_manager is None
        assert pairing is not None

    def test_start_failure_cleans_provider_registry_and_runtime_residue(
        self, tmp_path
    ) -> None:
        class PartialStartProvider(_FakeProvider):
            def start(self) -> None:
                self.starts += 1
                self.listening = True
                raise DiyProviderError("managed ingress unavailable")

        provider = PartialStartProvider()
        supervisor = _supervisor(tmp_path, provider=provider)
        stale_registry = supervisor.registry
        stale_pairing = supervisor.pairing_manager()

        assert supervisor._start_provider() is False

        assert provider.starts == provider.stops == 1
        assert not provider.listening
        assert supervisor._provider is None
        assert supervisor._provider_running is False
        assert supervisor._registry is None and stale_registry.sealed is False
        assert supervisor._pairing_manager is None and stale_pairing is not None

    def test_run_without_provider_fails_closed(self, tmp_path) -> None:
        """The reviewer's [HIGH] provider-less READY finding: a RUNNABLE
        configuration without a concrete lab provider/listener must fail
        closed at startup and must NEVER emit READY=1 (no listener could back
        it). Non-run construction (status/readiness/diagnose/install) stays
        valid — only run() rejects."""
        notifications.clear()
        supervisor = _supervisor(tmp_path, provider=None, lab=False)
        with pytest.raises(ConnectorConfigError):
            supervisor.run(max_iterations=2, poll_seconds=0)
        assert notifications == []  # never entered the notify loop; no READY

    def test_start_provider_without_provider_returns_false(self, tmp_path) -> None:
        """Belt-and-braces: even if run()'s startup gate were bypassed,
        _start_provider must never report proven success without a listener."""
        notifications.clear()
        supervisor = _supervisor(tmp_path, provider=None, lab=False)
        assert supervisor._start_provider() is False
        assert supervisor._provider_running is False
        assert not _notified("READY=1")

    def test_run_with_injected_provider_needs_no_lab_config(self, tmp_path) -> None:
        """A concrete injected provider IS a listener source: run() accepts it
        even when the config carries no lab provider (test-only seam)."""
        notifications.clear()
        supervisor = _supervisor(tmp_path, provider=_FakeProvider(), lab=False)
        supervisor.run(max_iterations=1, poll_seconds=0)
        assert supervisor._provider_running is True
        assert _notified("READY=1")

    def test_restart_after_readiness_loss_rebuilds_adapter(
        self, tmp_path, route_policy_fixture
    ) -> None:
        """The REAL lab adapter is rebuilt per start: a stopped
        ThreadingHTTPServer cannot be restarted, so a readiness-loss cycle
        followed by re-ready must construct and bind a fresh adapter."""
        notifications.clear()
        token = tmp_path / "daemon.token"
        token.write_text("token-x")
        token.chmod(0o600)
        readiness = _FakeReadiness(ready=True)
        supervisor = _supervisor(
            tmp_path,
            provider=None,  # build the real adapter
            readiness=readiness,
            policy=build_consumer(route_policy_fixture),
        )
        supervisor.run(max_iterations=1, poll_seconds=0)
        first = supervisor._provider
        assert first is not None and first.listening is True
        port_before = first.bound_port
        assert port_before is not None

        readiness.ready = False
        supervisor.run(max_iterations=1, poll_seconds=0)
        assert supervisor._provider_running is False
        assert first.listening is False

        readiness.ready = True
        supervisor.run(max_iterations=1, poll_seconds=0)
        second = supervisor._provider
        assert second is not None
        assert second is not first  # rebuilt, not reused
        assert second.listening is True
        assert second.bound_port is not None
        supervisor.shutdown()


class TestServiceManagerDelegation:
    def test_install_renders_unit(self, tmp_path) -> None:
        token = tmp_path / "daemon.token"
        token.write_text("token-x")
        token.chmod(0o600)
        (tmp_path / "policy.json").write_text("{}")
        manager = FakeManager()
        cfg = _config(tmp_path, managed_dir_root=str(tmp_path / "managed"))
        supervisor = _supervisor(tmp_path, manager=manager, config=cfg)
        supervisor.install()
        assert manager.calls == ["install"]
        assert manager.installed[0][0] == "happyranch-connector.service"
        unit_text = manager.installed[0][1]
        assert "NoNewPrivileges=yes" in unit_text
        assert "LoadCredential" in unit_text

    def test_start_stop_restart_enable_disable_uninstall(self, tmp_path) -> None:
        manager = FakeManager()
        supervisor = _supervisor(tmp_path, manager=manager)
        supervisor.start()
        supervisor.stop()
        supervisor.restart()
        supervisor.enable()
        supervisor.disable()
        supervisor.uninstall()
        assert manager.calls == ["start", "stop", "restart", "enable", "disable", "uninstall"]

    def test_upgrade_and_rollback_delegate(self, tmp_path) -> None:
        manager = FakeManager()
        supervisor = _supervisor(tmp_path, manager=manager)
        outcome = supervisor.upgrade()
        assert outcome.ok is True
        assert manager.calls == ["upgrade"]
        outcome = supervisor.rollback()
        assert outcome.ok is True
        assert manager.calls[-1] == "rollback"

    def test_status_delegates(self, tmp_path) -> None:
        manager = FakeManager()
        supervisor = _supervisor(tmp_path, manager=manager)
        status = supervisor.status()
        assert status.active_state == "active"
        assert status.running is True


class TestDiagnostics:
    def test_diagnose_never_contains_bearer(self, tmp_path) -> None:
        (tmp_path / "daemon.token").write_text("top-secret-bearer-value")
        (tmp_path / "daemon.token").chmod(0o600)
        supervisor = _supervisor(tmp_path)
        report = supervisor.diagnose()
        blob = json.dumps(report)
        assert "top-secret-bearer-value" not in blob
        assert "Bearer" not in blob
        assert report["role"] == "happyranch-connector"
        assert report["secrets"] == "redacted"
        assert report["readiness"]["ready"] is True

    def test_diagnose_reports_gates_and_provider(self, tmp_path) -> None:
        supervisor = _supervisor(tmp_path, readiness=_FakeReadiness(ready=False))
        report = supervisor.diagnose()
        assert report["readiness"]["ready"] is False
        assert report["readiness"]["gates"]["daemon_loopback"]["ok"] is False
        assert report["provider"]["type"] == "lab"
        assert LAB_ONLY_BANNER in report["provider"]["banner"]


class TestPolicyLoadFailureReadiness:
    """Expected local policy artifact failures stay inside readiness."""

    @pytest.mark.parametrize(
        "policy_contents",
        [
            "{not-json",
            json.dumps(
                {
                    "schema_version": "HOSTILE-SCHEMA-VALUE",
                    "artifact": {"secret": "HOSTILE-BODY-VALUE"},
                    "artifact_version": 1,
                    "issued_at": "not-a-date",
                }
            ),
        ],
        ids=["malformed-json", "schema-invalid"],
    )
    def test_malformed_policy_is_stable_secret_free_denial(
        self, tmp_path, policy_contents
    ) -> None:
        policy_path = tmp_path / "HOSTILE-ABSOLUTE-POLICY-NAME.json"
        policy_path.write_text(policy_contents)
        config = _config(tmp_path, policy_path=str(policy_path))
        provider = _FakeProvider()
        supervisor = ConnectorSupervisor(
            config=config,
            manager=FakeManager(),
            provider=provider,
            now_fn=lambda: NOW(),
            notify_fn=lambda state: notifications.append(state),
        )

        notifications.clear()
        report = supervisor.readiness_report()
        diagnostic = supervisor.diagnose()
        assert report.ready is False
        assert report.gates["current_policy"] == GateResult(
            False, "policy_malformed", "route policy malformed or unreadable"
        )
        rendered = json.dumps(diagnostic)
        assert diagnostic["readiness"]["gates"]["current_policy"] == {
            "ok": False,
            "category": "policy_malformed",
        }
        for hostile in (
            str(policy_path),
            "HOSTILE-ABSOLUTE-POLICY-NAME",
            "HOSTILE-SCHEMA-VALUE",
            "HOSTILE-BODY-VALUE",
            "Traceback",
        ):
            assert hostile not in rendered

        assert supervisor.run(max_iterations=2, poll_seconds=0) == 0
        assert provider.starts == 0
        assert provider.listening is False
        assert not _notified("READY=1")

    @pytest.mark.parametrize("kind", ["missing", "unreadable"])
    def test_missing_or_unreadable_policy_is_stable_denial(
        self, tmp_path, kind
    ) -> None:
        policy_path = tmp_path / "private-policy.json"
        if kind == "unreadable":
            policy_path.mkdir()
        supervisor = ConnectorSupervisor(
            config=_config(tmp_path, policy_path=str(policy_path)),
            manager=FakeManager(),
            provider=_FakeProvider(),
            now_fn=lambda: NOW(),
        )

        report = supervisor.readiness_report()
        assert report.ready is False
        assert report.gates["current_policy"] == GateResult(
            False, "policy_malformed", "route policy malformed or unreadable"
        )

    def test_valid_policy_behavior_is_unchanged(
        self, tmp_path, route_policy_fixture
    ) -> None:
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(
            make_policy_envelope(route_policy_fixture).model_dump_json()
        )
        supervisor = ConnectorSupervisor(
            config=_config(tmp_path, policy_path=str(policy_path)),
            policy=None,
            now_fn=lambda: NOW(),
        )

        assert supervisor.load_policy().require_current(NOW()) is None

    def test_real_lab_loop_stays_alive_and_recovers_after_policy_repair(
        self, tmp_path, route_policy_fixture
    ) -> None:
        policy_path = tmp_path / "policy.json"
        policy_path.write_text("{not-json")
        token_path = tmp_path / "daemon.token"
        token_path.write_text(BEARER)
        token_path.chmod(0o600)
        daemon = FakeDaemon(BEARER)
        daemon.start()
        now = datetime.now(timezone.utc)
        config = _config(
            tmp_path,
            daemon_port=daemon.port,
            lab=LabProviderConfig(bind_host="127.0.0.1", bind_port=0, lab_only=True),
        )
        supervisor = ConnectorSupervisor(
            config=config,
            manager=FakeManager(),
            now_fn=lambda: now,
            notify_fn=lambda state: notifications.append(state),
        )

        repaired = False

        def repair_after_first_poll(_seconds: float) -> None:
            nonlocal repaired
            if not repaired:
                assert supervisor._provider_running is False
                assert supervisor._provider is None
                policy_path.write_text(
                    make_policy_envelope(
                        route_policy_fixture, issued_at=now - timedelta(seconds=60)
                    ).model_dump_json()
                )
                repaired = True

        notifications.clear()
        try:
            assert supervisor.run(
                max_iterations=2, poll_seconds=0, wait_fn=repair_after_first_poll
            ) == 0
            assert repaired is True
            report = supervisor.readiness_report()
            assert report.ready is True, report.gates
            assert supervisor._provider_running is True
            assert supervisor._provider is not None
            assert supervisor._provider.listening is True
            assert any("waiting for readiness" in note for note in notifications)
            assert _notified("READY=1")
        finally:
            supervisor.shutdown()
            daemon.stop()

    @pytest.mark.parametrize("provider_mode", ["lab", "diy"])
    def test_corrupt_policy_denies_real_provider_construction_secret_free(
        self, tmp_path, provider_mode
    ) -> None:
        policy_path = tmp_path / "HOSTILE-ABSOLUTE-POLICY-NAME.json"
        policy_path.write_text('{"secret":"HOSTILE-BODY-VALUE"}')
        config = (
            _config(tmp_path, policy_path=str(policy_path))
            if provider_mode == "lab"
            else _diy_config(tmp_path, policy_path=str(policy_path))
        )
        supervisor = ConnectorSupervisor(
            config=config,
            manager=FakeManager(),
            now_fn=lambda: NOW(),
        )

        diagnostic = supervisor.diagnose()

        assert diagnostic["readiness"]["gates"]["current_policy"] == {
            "ok": False,
            "category": "policy_malformed",
        }
        assert diagnostic["provider"]["type"] == provider_mode
        assert diagnostic["provider"]["listening"] is False
        assert diagnostic["provider"]["bound_port"] is None
        rendered = json.dumps(diagnostic)
        for hostile in (
            str(policy_path),
            "HOSTILE-ABSOLUTE-POLICY-NAME",
            "HOSTILE-BODY-VALUE",
            "Traceback",
        ):
            assert hostile not in rendered

    @pytest.mark.parametrize("provider_mode", ["lab", "diy"])
    def test_unexpected_policy_programming_defect_propagates(
        self, tmp_path, monkeypatch, provider_mode
    ) -> None:
        policy_path = tmp_path / "policy.json"
        policy_path.write_text("{}")
        config = (
            _config(tmp_path, policy_path=str(policy_path))
            if provider_mode == "lab"
            else _diy_config(tmp_path, policy_path=str(policy_path))
        )
        supervisor = ConnectorSupervisor(
            config=config,
            manager=FakeManager(),
            now_fn=lambda: NOW(),
        )

        def programming_defect(_cls, _raw):
            raise RuntimeError("unexpected programmer defect")

        monkeypatch.setattr(
            PolicyEnvelope, "model_validate", classmethod(programming_defect)
        )

        with pytest.raises(RuntimeError, match="unexpected programmer defect"):
            supervisor.diagnose()


class TestLabProvisioning:
    def test_first_run_state_pairs_lab_device(self, tmp_path) -> None:
        supervisor = _supervisor(tmp_path)
        state = supervisor.initial_state()
        assert state.connector_identity == default_identity()
        assert state.current_device_id == "lab-client-1"
        assert state.devices["lab-client-1"].revoked is False

    def test_revocation_across_restart_at_supervisor_level(
        self, tmp_path, route_policy_fixture
    ) -> None:
        """The full revocation-across-restart path: revoke via the coordinator,
        persist, then a NEW supervisor (fresh process) loads the state and the
        lab context factory denies the device."""
        config = _config(tmp_path)
        supervisor = _supervisor(tmp_path, config=config, policy=build_consumer(route_policy_fixture))
        # first run: persist first-run state with the lab device paired
        store = AtomicFileTrustStateStore(
            Path(config.state_path), supervisor.initial_state()
        )
        store.save(supervisor.initial_state())
        state = store.load()
        RevocationCoordinator(state, StreamRegistry()).revoke(epoch=2)
        store.save(state)

        # restart: a new supervisor instance over the same state file
        restarted = _supervisor(tmp_path, config=config, policy=build_consumer(route_policy_fixture))
        loaded = restarted.state_store.load()
        assert loaded.revocation_epoch == 2
        ctx = restarted.build_ctx_factory()(NOW())
        verdict = ctx.authorization.check("tenant-a", "home-a", "lab-client-1", NOW())
        assert verdict.ok is False
        assert verdict.reason == "revocation"

    def test_ctx_factory_forwarder_targets_literal_loopback(
        self, tmp_path, route_policy_fixture
    ) -> None:
        config = _config(tmp_path, daemon_port=9876)
        supervisor = _supervisor(tmp_path, config=config, policy=build_consumer(route_policy_fixture))
        ctx = supervisor.build_ctx_factory()(NOW())
        assert ctx.forwarder.target.host == "127.0.0.1"
        assert ctx.forwarder.target.port == 9876


class TestSdNotify:
    def test_child_health_transport_is_structured_and_does_not_touch_notify_socket(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        read_fd, write_fd = os.pipe()
        try:
            monkeypatch.setenv("HAPPYRANCH_CHILD_HEALTH_FD", str(write_fd))
            monkeypatch.setenv("HAPPYRANCH_CHILD_HEALTH_GENERATION", "a" * 32)
            monkeypatch.setenv("NOTIFY_SOCKET", "/must/not/be/used")
            assert sd_notify("READY=1\n") is True
            record = json.loads(os.read(read_fd, 4096))
            assert record == {
                "generation": "a" * 32,
                "sequence": record["sequence"],
                "state": "ready",
                "version": 1,
            }
            assert type(record["sequence"]) is int and record["sequence"] > 0
        finally:
            os.close(read_fd)
            os.close(write_fd)

    @pytest.mark.parametrize("generation", ["", "not-hex", "a" * 31, "a" * 33])
    def test_child_health_transport_rejects_bad_generation(
        self, monkeypatch: pytest.MonkeyPatch, generation: str,
    ) -> None:
        read_fd, write_fd = os.pipe()
        try:
            monkeypatch.setenv("HAPPYRANCH_CHILD_HEALTH_FD", str(write_fd))
            monkeypatch.setenv("HAPPYRANCH_CHILD_HEALTH_GENERATION", generation)
            assert sd_notify("READY=1\n") is False
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_sd_notify_without_socket_returns_false(self) -> None:
        assert sd_notify("READY=1\n", notify_socket=None) is False

    @pytest.mark.parametrize(
        "socket_parent", [Path("neutral"), Path(".happyranch/orgs")]
    )
    def test_sd_notify_sends_datagram(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socket_parent: Path
    ) -> None:
        import socket

        parent = tmp_path / socket_parent
        parent.mkdir(parents=True)
        monkeypatch.chdir(parent)
        sock_path = Path("notify.sock")
        assert sock_path.parts == ("notify.sock",)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(str(sock_path))
        server.settimeout(2)
        try:
            assert sd_notify("READY=1\n", notify_socket=str(sock_path)) is True
            data, _ = server.recvfrom(64)
            assert data == b"READY=1\n"
        finally:
            server.close()

    def test_sd_notify_missing_socket_fails_safe(self, tmp_path) -> None:
        assert sd_notify("WATCHDOG=1\n", notify_socket=str(tmp_path / "nope.sock")) is False


class TestCredentialProvider:
    """Finding 3: the service path must automatically consume
    CREDENTIALS_DIRECTORY/LoadCredential — without redundant config and
    without ever falling back to reading the daemon home."""

    def test_auto_consumes_crdentials_directory_env(self, tmp_path, monkeypatch) -> None:
        creds = tmp_path / "creds"
        creds.mkdir()
        (creds / "daemon.token").write_text("svc-token")
        (creds / "daemon.token").chmod(0o600)
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
        supervisor = _supervisor(tmp_path)  # config also carries daemon_token_path
        provider = supervisor.credential_provider()
        assert isinstance(provider, SystemdCredentialProvider)
        assert provider.read_bearer() == "svc-token"

    def test_never_falls_back_to_daemon_home_under_systemd(
        self, tmp_path, monkeypatch
    ) -> None:
        """Under LoadCredential= the injected credential is the ONLY source:
        a missing credential fails closed — it must never fall through to the
        daemon-token file path (the service user may not read the daemon home)."""
        creds = tmp_path / "creds"
        creds.mkdir()  # empty: nothing injected
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
        supervisor = _supervisor(tmp_path)  # daemon_token_path set, file absent
        provider = supervisor.credential_provider()
        assert isinstance(provider, SystemdCredentialProvider)
        with pytest.raises(CredentialUnavailable):
            provider.read_bearer()

    def test_no_env_uses_configured_sources(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        token = tmp_path / "daemon.token"
        token.write_text("file-token")
        token.chmod(0o600)
        supervisor = _supervisor(tmp_path)
        provider = supervisor.credential_provider()
        assert provider.read_bearer() == "file-token"


class TestInstallStaging:
    """Finding 3: install() stages config/state into the declared managed
    directories (accessible to the dedicated service user), never pointing the
    unit at ~/.happyranch paths the hardened service cannot read."""

    def _policy(self, tmp_path) -> Path:
        p = tmp_path / "policy.json"
        p.write_text("{}")
        return p

    def test_install_stages_managed_config_policy_and_state(self, tmp_path) -> None:
        token = tmp_path / "daemon.token"
        token.write_text("token-x")
        token.chmod(0o600)
        policy = self._policy(tmp_path)
        state = TrustState(connector_identity=default_identity(), pairing_epoch=0, revocation_epoch=0)
        store = AtomicFileTrustStateStore(tmp_path / "state.json", state)
        store.save(state)
        manager = FakeManager()
        cfg = _config(tmp_path, managed_dir_root=str(tmp_path / "managed"))
        supervisor = _supervisor(tmp_path, manager=manager, config=cfg)
        path = supervisor.install()
        managed_root = tmp_path / "managed" / "happyranch-connector"
        assert (managed_root / "config.json").is_file()
        assert (managed_root / "policy.json").is_file()
        assert (managed_root / "trust-state.json").is_file()
        assert (managed_root / "trust-state.json.anchor").is_file()
        managed_cfg = ConnectorConfig.from_file(managed_root / "config.json")
        assert managed_cfg.state_path == str(managed_root / "trust-state.json")
        assert managed_cfg.policy_path == str(managed_root / "policy.json")
        assert managed_cfg.tenant_id == "tenant-a"
        unit_text = manager.installed[0][1]
        assert str(managed_root / "config.json") in unit_text
        assert "--lab-only" in unit_text  # lab provider → unit passes --lab-only

    @staticmethod
    def _assert_unit_config_target(
        unit_text: str, *, expected: Path, forbidden_daemon_home: Path
    ) -> None:
        exec_start = next(
            line.removeprefix("ExecStart=")
            for line in unit_text.splitlines()
            if line.startswith("ExecStart=")
        )
        argv = shlex.split(exec_start)
        config_path = Path(argv[argv.index("--config") + 1])
        assert config_path == expected
        assert not config_path.is_relative_to(forbidden_daemon_home)

    @pytest.mark.parametrize(
        "runtime_parent", [Path("neutral"), Path(".happyranch/orgs")]
    )
    def test_install_staged_unit_never_points_at_daemon_home(
        self, tmp_path: Path, runtime_parent: Path
    ) -> None:
        """The rendered unit's --config must be the managed path — never a
        ~/.happyranch/... path the hardened service user cannot read."""
        runtime_root = tmp_path / runtime_parent
        runtime_root.mkdir(parents=True)
        token = runtime_root / "daemon.token"
        token.write_text("token-x")
        token.chmod(0o600)
        self._policy(runtime_root)
        manager = FakeManager()
        managed_config = (
            runtime_root / "managed" / "happyranch-connector" / "config.json"
        )
        daemon_home = runtime_root / ".happyranch"
        cfg = _config(runtime_root, managed_dir_root=str(runtime_root / "managed"))
        supervisor = _supervisor(runtime_root, manager=manager, config=cfg)
        supervisor.install()
        unit_text = manager.installed[0][1]
        self._assert_unit_config_target(
            unit_text,
            expected=managed_config,
            forbidden_daemon_home=daemon_home,
        )

        forbidden_unit = unit_text.replace(
            str(managed_config), str(daemon_home / "config.json")
        )
        with pytest.raises(AssertionError):
            self._assert_unit_config_target(
                forbidden_unit,
                expected=managed_config,
                forbidden_daemon_home=daemon_home,
            )

    def test_install_refuses_missing_policy(self, tmp_path) -> None:
        manager = FakeManager()
        cfg = _config(tmp_path, managed_dir_root=str(tmp_path / "managed"))
        supervisor = _supervisor(tmp_path, manager=manager, config=cfg)
        with pytest.raises(ConnectorConfigError):
            supervisor.install()
        assert manager.calls == []  # nothing staged, nothing installed

    def test_install_refuses_partial_source_state(self, tmp_path) -> None:
        """A source snapshot without its companion anchor is partial/corrupt
        state — install() must refuse to stage it (revocation history could be
        silently dropped)."""
        token = tmp_path / "daemon.token"
        token.write_text("token-x")
        token.chmod(0o600)
        self._policy(tmp_path)
        state = TrustState(connector_identity=default_identity(), pairing_epoch=0, revocation_epoch=0)
        store = AtomicFileTrustStateStore(tmp_path / "state.json", state)
        store.save(state)
        (tmp_path / "state.json.anchor").unlink()
        manager = FakeManager()
        cfg = _config(tmp_path, managed_dir_root=str(tmp_path / "managed"))
        supervisor = _supervisor(tmp_path, manager=manager, config=cfg)
        with pytest.raises(ConnectorConfigError):
            supervisor.install()
        assert manager.calls == []

    def test_install_staged_files_are_owner_only(self, tmp_path) -> None:
        token = tmp_path / "daemon.token"
        token.write_text("token-x")
        token.chmod(0o600)
        self._policy(tmp_path)
        manager = FakeManager()
        cfg = _config(tmp_path, managed_dir_root=str(tmp_path / "managed"))
        supervisor = _supervisor(tmp_path, manager=manager, config=cfg)
        supervisor.install()
        managed_root = tmp_path / "managed" / "happyranch-connector"
        import stat

        for name in ("config.json", "policy.json"):
            mode = stat.S_IMODE((managed_root / name).stat().st_mode)
            assert mode & 0o077 == 0


class TestInstallReinstallAuthority:
    """TASK-6004 [HIGH]: once a managed snapshot+anchor pair exists it is
    AUTHORITATIVE. install/reinstall must never overwrite or roll it back
    with a stale operator source pair — the source pair is only an initial
    seed when NO managed state exists."""

    def _policy(self, tmp_path) -> Path:
        p = tmp_path / "policy.json"
        p.write_text("{}")
        return p

    def _source_supervisor(self, tmp_path, *, manager=None, **overrides):
        token = tmp_path / "daemon.token"
        token.write_text("token-x")
        token.chmod(0o600)
        self._policy(tmp_path)
        cfg = _config(
            tmp_path, managed_dir_root=str(tmp_path / "managed"), **overrides
        )
        return _supervisor(tmp_path, manager=manager or FakeManager(), config=cfg)

    def _managed_root(self, tmp_path) -> Path:
        return tmp_path / "managed" / "happyranch-connector"

    def _revoked_state(self) -> TrustState:
        """A state whose lab device is revoked at epoch 1 (the managed state
        advances past the initial source seed)."""
        state = TrustState(
            connector_identity=default_identity(), pairing_epoch=0, revocation_epoch=1
        )
        state.apply_pairing(
            DeviceAuthorization(
                device_id="lab-client-1",
                tenant_id="tenant-a",
                home_id="home-a",
                authorization_epoch=1,
                expires_at=NOW() + timedelta(days=3650),
                revoked=True,
            )
        )
        return state

    def test_initial_install_seeds_managed_from_source(self, tmp_path) -> None:
        """No managed pair yet: the source pair is the initial seed (the
        pre-existing staging contract still holds)."""
        state = TrustState(
            connector_identity=default_identity(), pairing_epoch=0, revocation_epoch=0
        )
        store = AtomicFileTrustStateStore(tmp_path / "state.json", state)
        store.save(state)
        manager = FakeManager()
        supervisor = self._source_supervisor(tmp_path, manager=manager)
        supervisor.install()
        managed_root = self._managed_root(tmp_path)
        assert (managed_root / "trust-state.json").is_file()
        assert (managed_root / "trust-state.json.anchor").is_file()
        # A NEW store instance over the managed pair loads the seeded state.
        reloaded = AtomicFileTrustStateStore(
            managed_root / "trust-state.json", state
        ).load()
        assert reloaded.revocation_epoch == 0

    def test_reinstall_after_managed_revocation_refuses_and_preserves(
        self, tmp_path
    ) -> None:
        """The reviewer's exact repro: install epoch 0 (gen 1), advance the
        MANAGED pair to revoked epoch 1 (gen 2), then reinstall with the stale
        source pair — install must REFUSE (never roll back a revocation), and
        a NEW store instance must still load the revoked managed state."""
        state = TrustState(
            connector_identity=default_identity(), pairing_epoch=0, revocation_epoch=0
        )
        store = AtomicFileTrustStateStore(tmp_path / "state.json", state)
        store.save(state)
        manager = FakeManager()
        supervisor = self._source_supervisor(tmp_path, manager=manager)
        supervisor.install()
        managed_root = self._managed_root(tmp_path)
        # Advance the MANAGED pair (the running service revokes) → generation 2.
        managed_store = AtomicFileTrustStateStore(
            managed_root / "trust-state.json", state
        )
        managed_store.save(self._revoked_state())
        # Reinstall with the unchanged STALE source pair → must refuse.
        with pytest.raises(ConnectorConfigError):
            supervisor.install()
        # The managed pair is untouched: a NEW instance (restart) still denies.
        reloaded = AtomicFileTrustStateStore(
            managed_root / "trust-state.json", state
        ).load()
        assert reloaded.revocation_epoch == 1
        assert reloaded.devices["lab-client-1"].revoked is True

    def test_reinstall_with_newer_source_pair_adopts_monotonically(
        self, tmp_path
    ) -> None:
        """A strictly NEWER source pair (higher generation) is a monotonic
        advance: install adopts it, and the adopted pair is authoritative."""
        state = TrustState(
            connector_identity=default_identity(), pairing_epoch=0, revocation_epoch=0
        )
        store = AtomicFileTrustStateStore(tmp_path / "state.json", state)
        store.save(state)
        manager = FakeManager()
        supervisor = self._source_supervisor(tmp_path, manager=manager)
        supervisor.install()
        # The source advances (operator re-provisions a newer pair) → gen 2.
        store.save(self._revoked_state())
        supervisor.install()
        managed_root = self._managed_root(tmp_path)
        reloaded = AtomicFileTrustStateStore(
            managed_root / "trust-state.json", state
        ).load()
        assert reloaded.revocation_epoch == 1
        assert reloaded.devices["lab-client-1"].revoked is True

    def test_reinstall_with_equal_identical_pair_is_noop(self, tmp_path) -> None:
        """An equal (identical) pair is already in sync: install keeps the
        managed pair untouched and still succeeds."""
        state = TrustState(
            connector_identity=default_identity(), pairing_epoch=0, revocation_epoch=0
        )
        store = AtomicFileTrustStateStore(tmp_path / "state.json", state)
        store.save(state)
        manager = FakeManager()
        supervisor = self._source_supervisor(tmp_path, manager=manager)
        supervisor.install()
        managed_root = self._managed_root(tmp_path)
        managed_store = AtomicFileTrustStateStore(managed_root / "trust-state.json", state)
        managed_store.save(self._revoked_state())  # managed → gen 2
        # Operator re-saves the SAME pair at the source → source gen 2 == managed gen 2.
        store.save(self._revoked_state())
        supervisor.install()  # must not raise
        reloaded = AtomicFileTrustStateStore(
            managed_root / "trust-state.json", state
        ).load()
        assert reloaded.revocation_epoch == 1  # managed preserved (still revoked)

    def test_reinstall_with_conflicting_same_generation_refuses(self, tmp_path) -> None:
        """A different pair at the SAME generation is a split/conflict —
        install refuses and the managed pair stays authoritative."""
        state = TrustState(
            connector_identity=default_identity(), pairing_epoch=0, revocation_epoch=0
        )
        store = AtomicFileTrustStateStore(tmp_path / "state.json", state)
        store.save(state)
        manager = FakeManager()
        supervisor = self._source_supervisor(tmp_path, manager=manager)
        supervisor.install()
        managed_root = self._managed_root(tmp_path)
        managed_store = AtomicFileTrustStateStore(managed_root / "trust-state.json", state)
        managed_store.save(self._revoked_state())  # managed → gen 2 (revoked)
        # Source advances to gen 2 with a DIFFERENT (non-revoked) state.
        other = TrustState(
            connector_identity=default_identity(), pairing_epoch=0, revocation_epoch=0
        )
        other.apply_pairing(
            DeviceAuthorization(
                device_id="lab-client-1",
                tenant_id="tenant-a",
                home_id="home-a",
                authorization_epoch=1,
                expires_at=NOW() + timedelta(days=3650),
            )
        )
        store.save(other)
        assert store.anchored_generation() == 2
        with pytest.raises(ConnectorConfigError):
            supervisor.install()
        reloaded = AtomicFileTrustStateStore(
            managed_root / "trust-state.json", state
        ).load()
        assert reloaded.revocation_epoch == 1  # managed untouched

    def test_reinstall_interrupted_staging_leaves_no_usable_mixed_pair(
        self, tmp_path, monkeypatch
    ) -> None:
        """An interrupted/partial staging (second member write fails) must
        leave NO usable mixed pair: the managed pair either stays intact or
        fails closed under load() — never a loadable mixed snapshot, and
        never a rollback to a lower generation."""
        state = TrustState(
            connector_identity=default_identity(), pairing_epoch=0, revocation_epoch=0
        )
        store = AtomicFileTrustStateStore(tmp_path / "state.json", state)
        store.save(state)
        manager = FakeManager()
        supervisor = self._source_supervisor(tmp_path, manager=manager)
        supervisor.install()
        managed_root = self._managed_root(tmp_path)
        managed_store = AtomicFileTrustStateStore(managed_root / "trust-state.json", state)
        managed_store.save(state)  # managed → gen 2 (still unrevoked)
        # Source advances to gen 3 (strictly newer → adoption path); the
        # anchor publish fails mid-staging.
        store.save(state)
        store.save(self._revoked_state())
        real_replace = os.replace

        def _fail_second_replace(src, dst, *a, **kw):
            if "trust-state.json.anchor" in str(dst):
                raise OSError("injected anchor publish failure")
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(os, "replace", _fail_second_replace)
        with pytest.raises(ConnectorConfigError):
            supervisor.install()
        monkeypatch.undo()
        # The managed pair must NOT be a usable mixed pair: loading it fails
        # closed (new snapshot + old anchor = mismatched), never a loadable
        # un-revoked state.
        fresh = AtomicFileTrustStateStore(managed_root / "trust-state.json", state)
        with pytest.raises(StateStoreError):
            fresh.load()

    def test_reinstall_source_partial_pair_refuses(self, tmp_path) -> None:
        """A source snapshot without its companion anchor is partial state
        and must refuse reinstall exactly as it refuses initial install."""
        state = TrustState(
            connector_identity=default_identity(), pairing_epoch=0, revocation_epoch=0
        )
        store = AtomicFileTrustStateStore(tmp_path / "state.json", state)
        store.save(state)
        manager = FakeManager()
        supervisor = self._source_supervisor(tmp_path, manager=manager)
        supervisor.install()
        (tmp_path / "state.json.anchor").unlink()  # source becomes partial
        with pytest.raises(ConnectorConfigError):
            supervisor.install()
        assert manager.calls == ["install"]  # no second unit install


class TestManagedPaths:
    def test_system_mode_defaults_to_var_lib(self, tmp_path) -> None:
        cfg = _config(tmp_path, system=True)
        supervisor = _supervisor(tmp_path, config=cfg)
        assert str(supervisor._managed_state_root()) == "/var/lib/happyranch-connector"

    def test_user_mode_defaults_to_xdg_state_home(self, tmp_path) -> None:
        cfg = _config(tmp_path, system=False)
        supervisor = _supervisor(tmp_path, config=cfg)
        root = supervisor._managed_state_root()
        assert str(root).startswith(str(Path.home() / ".local" / "state"))

    def test_managed_dir_root_override_wins(self, tmp_path) -> None:
        cfg = _config(tmp_path, system=True, managed_dir_root=str(tmp_path / "m"))
        supervisor = _supervisor(tmp_path, config=cfg)
        assert str(supervisor._managed_state_root()) == str(tmp_path / "m" / "happyranch-connector")

    def test_unit_spec_default_exec_start_uses_managed_config(self, tmp_path) -> None:
        cfg = _config(tmp_path, managed_dir_root=str(tmp_path / "m"))
        supervisor = _supervisor(tmp_path, config=cfg)
        spec = supervisor.unit_spec()
        assert "--config" in spec.exec_start
        assert str(tmp_path / "m" / "happyranch-connector" / "config.json") in spec.exec_start
        assert "--lab-only" in spec.exec_start  # lab configured

    def test_unit_spec_no_lab_omits_lab_only(self, tmp_path) -> None:
        cfg = _config(tmp_path, lab=False, managed_dir_root=str(tmp_path / "m"))
        supervisor = _supervisor(tmp_path, config=cfg)
        spec = supervisor.unit_spec()
        assert "--lab-only" not in spec.exec_start


# ── Supported-DIY provider wiring (THR-097 Unit 3A) ─────────────────────────


def _diy_config(tmp_path, **overrides) -> ConnectorConfig:
    from runtime.remote_access.diy_provider import DiyProviderConfig
    from runtime.remote_access.network import NetworkConfig

    fields = dict(
        tenant_id="diy",
        home_id="home-a",
        connector_id="connector-a",
        daemon_port=8999,
        daemon_token_path=str(tmp_path / "daemon.token"),
        policy_path=str(tmp_path / "policy.json"),
        state_path=str(tmp_path / "state.json"),
        unit_name="happyranch-connector.service",
        system=False,
        lab=None,
        diy=DiyProviderConfig(
            network=NetworkConfig(mode="tailscale"),
            bind_port=8443,
        ),
    )
    fields.update(overrides)
    return ConnectorConfig(**fields)


class TestDiyConfig:
    def test_lab_and_diy_mutually_exclusive(self, tmp_path) -> None:
        config = _config(tmp_path, lab=True)
        from runtime.remote_access.diy_provider import DiyProviderConfig
        from runtime.remote_access.network import NetworkConfig

        config.diy = DiyProviderConfig(
            network=NetworkConfig(mode="tailscale")
        )
        with pytest.raises(ConnectorConfigError, match="mutually exclusive"):
            config.validate()

    def test_config_file_round_trip_preserves_diy(self, tmp_path) -> None:
        config = _diy_config(tmp_path)
        path = tmp_path / "diy.json"
        config.to_file(path)
        loaded = ConnectorConfig.from_file(path)
        assert loaded.diy is not None
        assert loaded.diy.network.mode == "tailscale"
        assert loaded.diy.bind_port == 8443
        assert loaded.lab is None

    def test_diy_config_validation_errors_fail_closed(self, tmp_path) -> None:
        from runtime.remote_access.diy_provider import DiyProviderConfig
        from runtime.remote_access.network import NetworkConfig

        config = _diy_config(
            tmp_path, diy=DiyProviderConfig(network=NetworkConfig(mode="explicit", address="0.0.0.0"))
        )
        with pytest.raises(ConnectorConfigError, match="diy"):
            config.validate()

    def test_unit_spec_carries_diy_flag(self, tmp_path) -> None:
        config = _diy_config(tmp_path)
        supervisor = ConnectorSupervisor(config=config)
        spec = supervisor.unit_spec()
        assert "--diy" in spec.exec_start

    def test_initial_state_has_no_devices_for_diy(self, tmp_path) -> None:
        config = _diy_config(tmp_path)
        supervisor = ConnectorSupervisor(config=config)
        state = supervisor.initial_state()
        assert state.devices == {}
        assert state.connector_identity is not None

    def test_pairing_manager_persists_across_restart(self, tmp_path) -> None:
        config = _diy_config(tmp_path)
        supervisor = ConnectorSupervisor(config=config)
        pairing = supervisor.pairing_manager()
        issued = pairing.issue_pairing_code("macbook-pro")
        credential = pairing.redeem_pairing(issued.code)
        assert credential is not None
        # Fresh supervisor over the same files:
        fresh = ConnectorSupervisor(config=config)
        state = fresh.pairing_manager().load_state()
        assert "macbook-pro" in state.devices
        assert state.devices["macbook-pro"].credential_digest is not None

    def test_build_diy_provider(self, tmp_path, route_policy_fixture) -> None:
        config = _diy_config(tmp_path)
        supervisor = _supervisor(tmp_path, config=config, policy=build_consumer(route_policy_fixture))
        provider = supervisor.build_diy_provider()
        assert provider is not None
        # A lab-only config has no diy provider.
        lab_config = _config(tmp_path, lab=True)
        lab_supervisor = _supervisor(tmp_path, config=lab_config, policy=build_consumer(route_policy_fixture))
        assert lab_supervisor.build_diy_provider() is None

    def test_diy_provider_listens_via_supervisor(self, tmp_path, route_policy_fixture) -> None:
        """The supervisor's readiness-gated start actually brings the DIY
        listener up on the resolved customer-network address."""
        import subprocess

        from runtime.remote_access.diy_provider import DiyProviderConfig
        from runtime.remote_access.network import NetworkConfig
        from runtime.remote_access.readiness import ConnectorReadiness

        # Find a real non-loopback IPv4 (as in the adapter acceptance tests).
        try:
            out = subprocess.run(
                ["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, check=False, timeout=10
            ).stdout
        except (OSError, subprocess.SubprocessError):
            pytest.skip("no ip tool for a real-network listener test")
        addr = None
        for line in out.splitlines():
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "inet" and i + 1 < len(parts):
                    candidate = parts[i + 1].split("/")[0]
                    if not candidate.startswith("127."):
                        addr = candidate
        if addr is None:
            pytest.skip("host has no non-loopback IPv4 for a real listener test")

        from runtime.remote_access.diy_provider import DiyProviderConfig as DPC
        from runtime.remote_access.network import NetworkConfig as NC

        config = _diy_config(
            tmp_path,
            diy=DPC(network=NC(mode="tailscale", tailscale_cli=_tailscale_stub(tmp_path, addr)), bind_port=0),
        )
        supervisor = _supervisor(
            tmp_path, config=config, policy=build_consumer(route_policy_fixture)
        )

        class Ready:
            def evaluate(self, now):
                from runtime.remote_access.readiness import GateResult, ReadinessReport

                gates = {n: GateResult(True, f"{n}_ok", n) for n in ConnectorReadiness.GATE_NAMES}
                return ReadinessReport(ready=True, gates=gates)

        provider = supervisor.build_diy_provider()
        provider._readiness = Ready()  # noqa: SLF001 — test seam
        provider.start()
        try:
            assert provider.listening is True
            assert provider.bound_port is not None
            assert provider.bind_address == addr
        finally:
            provider.stop()


# ── Authoritative live-stream registry (TASK-6039 reviewer [CRITICAL] finding 2) ──


@pytest.mark.skipif(
    NETWORK_IPV4 is None,
    reason="host has no non-loopback IPv4 address for a customer-network socket test",
)
def test_revoke_at_supervisor_seam_closes_live_streams(tmp_path, route_policy_fixture) -> None:
    """REVOKE through the SUPERVISOR wiring must close the REAL live SSE
    streams the shipping provider serves — ONE authoritative registry shared
    by the gateway ctx factory, the provider, and the pairing manager (never
    an unrelated empty registry reporting false success)."""
    import http.client as _http

    from runtime.remote_access.diy_provider import DEVICE_CREDENTIAL_HEADER
    from runtime.remote_access.diy_provider import DiyProviderConfig as DPC
    from runtime.remote_access.network import NetworkConfig as NC

    from .fake_daemon import FakeDaemon

    daemon = FakeDaemon(BEARER, hold_open=True)
    daemon.start()
    try:
        token_path = tmp_path / "daemon.token"
        token_path.write_text(BEARER)
        token_path.chmod(0o600)
        config = _diy_config(
            tmp_path,
            daemon_port=daemon.port,
            diy=DPC(
                network=NC(mode="tailscale", tailscale_cli=_tailscale_stub(tmp_path, NETWORK_IPV4)),
                bind_port=0,
            ),
        )
        # Policy built against the REAL clock (the adapter stamps requests
        # with datetime.now; the conftest NOW is a fixed historical instant
        # that would fail the policy freshness gate).
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        real_now = _dt.now(_tz.utc)
        policy = build_consumer(
            route_policy_fixture, issued_at=real_now - _td(seconds=60), now=real_now
        )
        supervisor = _supervisor(tmp_path, config=config, policy=policy)
        provider = supervisor.build_diy_provider()
        assert provider is not None
        provider.start()
        try:
            pairing = supervisor.pairing_manager()
            issued = pairing.issue_pairing_code("macbook-pro")
            credential = pairing.redeem_pairing(issued.code)
            assert credential is not None
            conn = _http.HTTPConnection(NETWORK_IPV4, provider.bound_port, timeout=10)
            conn.connect()
            conn.sock.settimeout(2)  # type: ignore[union-attr]
            conn.request(
                "GET",
                "/api/v1/orgs/acme/threads/T-1/tail",
                headers={DEVICE_CREDENTIAL_HEADER: credential, "Accept": "text/event-stream"},
            )
            sock = conn.sock  # captured BEFORE getresponse consumes it
            resp = conn.getresponse()
            assert resp.status == 200
            # The daemon flushed the SSE headers and is HOLDING the body
            # open: the stream is genuinely in flight.
            assert daemon.started.wait(timeout=10)
            outcome = pairing.revoke("macbook-pro")
            assert outcome.complete is True
            assert _sse_closed_after(resp), "supervisor-wired revoke must close the live SSE stream"
            conn.close()
        finally:
            provider.stop()
    finally:
        daemon.stop()


# ── Runtime rotation after cross-process revocation (TASK-6045 reviewer [HIGH] finding 2) ──
#
# The stream registry is ONE-SHOT: ``close_all`` seals it permanently, so the
# cross-process reconciliation that closes genuinely in-flight SSE streams
# MUST rotate the authoritative runtime — registry, pairing manager, provider
# adapter, and every captured ctx-factory reference — at a fail-closed
# lifecycle boundary (listener down during the handoff), or the shipping
# process stays sealed forever and a re-paired/current device can never open
# a new stream. These regressions prove the rotation at the supervisor seam.

def _diy_supervisor_with_real_provider(tmp_path, route_policy_fixture, daemon):
    """A supervisor wired to a REAL DIY provider (no injected fake) whose
    readiness is deterministically ready, so ``_start_provider`` runs the
    shipping adapter and ``_rotate_runtime`` can restart it. The policy is
    built against the REAL clock (the adapter stamps requests with
    datetime.now; the conftest NOW is a fixed historical instant that would
    fail the policy freshness gate)."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    from runtime.remote_access.diy_provider import DiyProviderConfig
    from runtime.remote_access.network import NetworkConfig

    real_now = _dt.now(_tz.utc)
    policy = build_consumer(
        route_policy_fixture, issued_at=real_now - _td(seconds=60), now=real_now
    )
    # The daemon token file the FileDaemonCredentialProvider reads on the
    # final loopback hop (mirrors the shipping-seam test setup).
    token_path = tmp_path / "daemon.token"
    token_path.write_text(BEARER)
    token_path.chmod(0o600)
    config = _diy_config(
        tmp_path,
        daemon_port=daemon.port,
        diy=DiyProviderConfig(
            network=NetworkConfig(
                mode="tailscale", tailscale_cli=_tailscale_stub(tmp_path, NETWORK_IPV4)
            ),
            bind_port=0,
        ),
    )
    supervisor = ConnectorSupervisor(
        config=config,
        manager=FakeManager(),
        readiness=_FakeReadiness(ready=True),
        policy=policy,
        notify_fn=lambda state: None,
    )
    return supervisor


def _open_sse(host, port, credential, daemon):
    import http.client as _http

    from runtime.remote_access.diy_provider import DEVICE_CREDENTIAL_HEADER

    conn = _http.HTTPConnection(host, port, timeout=10)
    conn.connect()
    conn.sock.settimeout(2)  # type: ignore[union-attr]
    conn.request(
        "GET",
        "/api/v1/orgs/acme/threads/T-1/tail",
        headers={DEVICE_CREDENTIAL_HEADER: credential, "Accept": "text/event-stream"},
    )
    resp = conn.getresponse()
    assert resp.status == 200, (
        "expected the SSE stream to open (status %s)" % resp.status
    )
    # The daemon flushed the SSE headers and is HOLDING the body open: the
    # stream is genuinely in flight and registry-tracked.
    assert daemon.started.wait(timeout=10)
    return conn, resp


@pytest.mark.skipif(
    NETWORK_IPV4 is None,
    reason="host has no non-loopback IPv4 address for a customer-network socket test",
)
class TestReconciliationRotation:
    @pytest.mark.parametrize("revocation_first", [False, True])
    def test_provider_start_racing_persisted_revocation_is_linearized_in_both_orderings(
        self, tmp_path, revocation_first: bool
    ) -> None:
        start_entered = threading.Event()
        release_start = threading.Event()
        close_entered = threading.Event()
        release_close = threading.Event()
        events: list[str] = []

        class BarrierProvider(_FakeProvider):
            def start(self) -> None:
                events.append("start")
                if not revocation_first and self.starts == 0:
                    start_entered.set()
                    assert release_start.wait(timeout=5)
                super().start()

            def stop(self) -> None:
                events.append("stop")
                super().stop()

        class BarrierRegistry:
            sealed = False

            def open_count(self) -> int:
                return 1

            def close_all(self) -> None:
                events.append("close")
                if revocation_first:
                    close_entered.set()
                    assert release_close.wait(timeout=5)
                self.sealed = True

        provider = BarrierProvider()
        supervisor = _supervisor(
            tmp_path, provider=provider, readiness=_FakeReadiness(ready=True)
        )
        old_registry = BarrierRegistry()
        supervisor._registry = old_registry
        state = supervisor.state_store.load()
        state.revocation_epoch += 1
        supervisor.state_store.save(state)

        if revocation_first:
            assert supervisor._start_provider() is True
            revoker = threading.Thread(target=supervisor._reconcile_revocation)
            starter = threading.Thread(target=supervisor._start_provider)
            revoker.start()
            assert close_entered.wait(timeout=5)
            starter.start()
            assert starter.is_alive(), "admission must wait behind revocation cleanup"
            release_close.set()
        else:
            starter = threading.Thread(target=supervisor._start_provider)
            revoker = threading.Thread(target=supervisor._reconcile_revocation)
            starter.start()
            assert start_entered.wait(timeout=5)
            revoker.start()
            assert revoker.is_alive(), "revocation must serialize after in-flight start"
            release_start.set()

        starter.join(timeout=5)
        revoker.join(timeout=5)
        assert not starter.is_alive() and not revoker.is_alive()
        assert events[-3:] == ["stop", "close", "start"]
        assert provider.stops == 1
        assert provider.starts == 2
        assert provider.listening and supervisor._provider_running
        assert old_registry.sealed is True
        assert supervisor._registry is None  # injected provider has no runtime capture
        assert supervisor._pairing_manager is None

    def test_persisted_revocation_removes_listener_before_stream_cleanup(
        self, tmp_path, route_policy_fixture,
    ) -> None:
        """The shipping supervisor removes admission before active cleanup."""
        events: list[str] = []

        class OrderedProvider(_FakeProvider):
            def stop(self) -> None:
                events.append("listener_stopped")
                super().stop()

        class OrderedRegistry:
            sealed = False

            def open_count(self) -> int:
                return 1

            def close_all(self) -> None:
                assert events == ["listener_stopped"]
                events.append("active_flows_closed")
                self.sealed = True

        provider = OrderedProvider()
        supervisor = _supervisor(
            tmp_path,
            provider=provider,
            readiness=_FakeReadiness(ready=False),
        )
        supervisor._registry = OrderedRegistry()
        assert supervisor._start_provider() is True
        state = supervisor.state_store.load()
        state.revocation_epoch += 1
        supervisor.state_store.save(state)

        supervisor._reconcile_revocation()

        assert events == ["listener_stopped", "active_flows_closed"]

    def test_persisted_revocation_retries_failed_listener_stop_without_double_close(
        self, tmp_path,
    ) -> None:
        events: list[str] = []

        class RetryProvider(_FakeProvider):
            def stop(self) -> None:
                events.append("listener_stop")
                if events.count("listener_stop") == 1:
                    raise RuntimeError("hostile stop detail")
                super().stop()

        class OnceRegistry:
            sealed = False
            live = True

            def open_count(self) -> int:
                return int(self.live)

            def close_all(self) -> None:
                events.append("active_flows_closed")
                self.live = False
                self.sealed = True

        provider = RetryProvider()
        supervisor = _supervisor(
            tmp_path, provider=provider, readiness=_FakeReadiness(ready=False)
        )
        supervisor._registry = OnceRegistry()
        assert supervisor._start_provider() is True
        state = supervisor.state_store.load()
        state.revocation_epoch += 1
        supervisor.state_store.save(state)

        supervisor._reconcile_revocation()
        assert supervisor._provider is provider
        assert supervisor._provider_running is True
        assert events == ["listener_stop", "active_flows_closed"]

        supervisor._reconcile_revocation()
        assert events == ["listener_stop", "active_flows_closed", "listener_stop"]
        assert supervisor._provider is None
        assert supervisor._provider_running is False

    def test_cross_process_revoke_closes_stream_then_rotation_reopens_repair(
        self, tmp_path, route_policy_fixture,
    ) -> None:
        """The FULL finding-2 regression at the supervisor seam: a persisted
        cross-process revoke closes the genuinely in-flight SSE AND the
        authoritative runtime is ROTATED (fresh registry, fresh pairing
        manager, fresh provider + ctx factory, listener restarted), so a
        RE-PAIRED device opens a NEW SSE in the SAME process lifetime."""

        daemon = FakeDaemon(BEARER, hold_open=True)
        daemon.start()
        try:
            supervisor = _diy_supervisor_with_real_provider(tmp_path, route_policy_fixture, daemon)
            assert supervisor._start_provider() is True
            provider_before = supervisor._provider
            assert provider_before is not None and provider_before.listening
            registry_before = supervisor.registry
            pairing_before = supervisor.pairing_manager()
            issued = pairing_before.issue_pairing_code("macbook-pro")
            credential = pairing_before.redeem_pairing(issued.code)
            assert credential is not None
            conn, resp = _open_sse(NETWORK_IPV4, provider_before.bound_port, credential, daemon)
            try:
                # SEPARATE process semantics: a fresh store instance over the
                # same files persists the revocation epoch (no live streams
                # in that process — an unrelated empty registry).
                state_path = Path(supervisor.config.state_path).expanduser()
                fresh = AtomicFileTrustStateStore(state_path, supervisor.initial_state())
                state = fresh.load()
                from runtime.remote_access.revocation import RevocationCoordinator as _Coord

                _Coord(state, StreamRegistry()).revoke(
                    max(state.pairing_epoch, state.revocation_epoch) + 1
                )
                fresh.save(state)
                # The run-loop reconciliation closes the stream AND rotates.
                supervisor._reconcile_revocation()
                assert _sse_closed_after(resp), "reconcile must close the live SSE"
                # The authoritative runtime was rotated at a fail-closed
                # boundary (listener down during the handoff).
                assert supervisor.registry is not registry_before
                assert supervisor.pairing_manager() is not pairing_before
                provider_after = supervisor._provider
                assert provider_after is not None and provider_after is not provider_before
                assert provider_after.listening
                assert provider_after._ctx_factory is not provider_before._ctx_factory
            finally:
                conn.close()
            # RE-PAIR: a fresh credential opens a NEW SSE in the SAME process
            # (the old one-shot registry is gone; the shipping process is not
            # permanently sealed).
            pairing_after = supervisor.pairing_manager()
            issued2 = pairing_after.issue_pairing_code("macbook-pro")
            credential2 = pairing_after.redeem_pairing(issued2.code)
            assert credential2 is not None and credential2 != credential
            conn2, resp2 = _open_sse(NETWORK_IPV4, provider_after.bound_port, credential2, daemon)
            try:
                assert resp2.status == 200, (
                    "re-paired device must open a NEW SSE after rotation "
                    f"(status {resp2.status})"
                )
            finally:
                conn2.close()
            # The OLD (revoked) credential remains denied.
            conn3 = _http_deny_probe(NETWORK_IPV4, provider_after.bound_port, credential)
            try:
                assert conn3.status == 403
            finally:
                conn3.close()
        finally:
            daemon.stop()

    def test_targeted_revoke_rotation_unaffected_device_still_opens_and_ws_denied(
        self, tmp_path, route_policy_fixture,
    ) -> None:
        """A TARGETED cross-process revoke of one device closes the live SSE,
        rotates the runtime, and an UNAFFECTED current credential opens a NEW
        SSE afterwards; the revoked credential and WebSocket upgrades remain
        denied (allow-list unchanged)."""
        daemon = FakeDaemon(BEARER, hold_open=True)
        daemon.start()
        try:
            supervisor = _diy_supervisor_with_real_provider(tmp_path, route_policy_fixture, daemon)
            assert supervisor._start_provider() is True
            provider = supervisor._provider
            pairing = supervisor.pairing_manager()
            issued_a = pairing.issue_pairing_code("macbook-pro")
            cred_a = pairing.redeem_pairing(issued_a.code)
            issued_b = pairing.issue_pairing_code("phone")
            cred_b = pairing.redeem_pairing(issued_b.code)
            assert cred_a and cred_b
            conn, resp = _open_sse(NETWORK_IPV4, provider.bound_port, cred_a, daemon)
            try:
                state_path = Path(supervisor.config.state_path).expanduser()
                fresh = AtomicFileTrustStateStore(state_path, supervisor.initial_state())
                state = fresh.load()
                from runtime.remote_access.revocation import RevocationCoordinator as _Coord

                _Coord(state, StreamRegistry()).revoke_device(
                    "macbook-pro", max(state.pairing_epoch, state.revocation_epoch) + 1
                )
                fresh.save(state)
                supervisor._reconcile_revocation()
                assert _sse_closed_after(resp)
            finally:
                conn.close()
            provider_after = supervisor._provider
            # Unaffected current device: opens a NEW SSE after the targeted
            # revoke (the process was not sealed).
            conn_b, resp_b = _open_sse(NETWORK_IPV4, provider_after.bound_port, cred_b, daemon)
            try:
                assert resp_b.status == 200, (
                    "unaffected current credential must open a NEW SSE after "
                    f"a targeted revoke (status {resp_b.status})"
                )
                # WebSocket denial retained after rotation: an Upgrade request
                # with a CURRENT credential is denied at the allow-list and
                # never forwarded.
                import http.client as _http

                from runtime.remote_access.diy_provider import DEVICE_CREDENTIAL_HEADER

                daemon_requests_before = len(daemon.requests)
                conn_ws = _http.HTTPConnection(NETWORK_IPV4, provider_after.bound_port, timeout=10)
                conn_ws.request(
                    "GET",
                    "/api/v1/orgs/acme/threads/T-1/tail",
                    headers={
                        DEVICE_CREDENTIAL_HEADER: cred_b,
                        "Upgrade": "websocket",
                        "Connection": "Upgrade",
                    },
                )
                ws_resp = conn_ws.getresponse()
                ws_resp.read()
                conn_ws.close()
                assert ws_resp.status == 403
                # The WebSocket upgrade was never forwarded to the daemon
                # (only the two legitimate SSE streams above reached /tail).
                assert not any(
                    r["path"].endswith("/tail")
                    for r in daemon.requests[daemon_requests_before:]
                )
            finally:
                conn_b.close()
            # The revoked device's credential remains denied (identical deny).
            conn_a = _http_deny_probe(NETWORK_IPV4, provider_after.bound_port, cred_a)
            try:
                assert conn_a.status == 403
            finally:
                conn_a.close()
        finally:
            daemon.stop()


def _http_deny_probe(host, port, credential):
    """Open an SSE request that must be DENIED (revoked/unknown credential);
    returns the response object (caller closes it)."""
    import http.client as _http

    from runtime.remote_access.diy_provider import DEVICE_CREDENTIAL_HEADER

    conn = _http.HTTPConnection(host, port, timeout=10)
    conn.request(
        "GET",
        "/api/v1/orgs/acme/threads/T-1/tail",
        headers={DEVICE_CREDENTIAL_HEADER: credential, "Accept": "text/event-stream"},
    )
    resp = conn.getresponse()
    resp.read()
    return resp
