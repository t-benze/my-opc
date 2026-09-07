"""Linux connector CLI tests (THR-097 phase unit 3)."""
from __future__ import annotations

import json

import pytest

from runtime.remote_access import cli
from runtime.remote_access.lab_provider import LAB_ONLY_BANNER, LabProviderConfig
from runtime.remote_access.supervisor import ConnectorConfigError, ConnectorSupervisor

from .test_supervisor import _config


class StubSupervisor:
    """Records commands; returns canned results for CLI assertions."""

    def readiness_report(self):
        from runtime.remote_access.readiness import GateResult, ReadinessReport

        self.calls.append("readiness_report")
        gates = {
            name: GateResult(self.ready, f"{name}_ok", f"{name} ok")
            for name in ("daemon_loopback", "credential_permissions", "current_policy", "bind_identity", "trust_state")
        }
        return ReadinessReport(ready=self.ready, gates=gates)

    def diagnose(self):
        self.calls.append("diagnose")
        return {"role": "happyranch-connector", "secrets": "redacted"}

    def status(self):
        self.calls.append("status")
        return type("S", (), {"__dict__": {"active_state": "active"}})()

    def install(self, enable=True):
        self.calls.append(f"install:{enable}")

    def uninstall(self):
        self.calls.append("uninstall")

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def restart(self):
        self.calls.append("restart")

    def enable(self):
        self.calls.append("enable")

    def disable(self):
        self.calls.append("disable")

    def upgrade(self, verify_start=True):
        self.calls.append(f"upgrade:{verify_start}")
        from runtime.remote_access.service_manager import UpgradeOutcome

        return UpgradeOutcome(ok=self.upgrade_ok)

    def rollback(self):
        self.calls.append("rollback")
        from runtime.remote_access.service_manager import UpgradeOutcome

        return UpgradeOutcome(ok=True)

    def pairing_manager(self):
        self.calls.append("pairing_manager")
        from datetime import datetime, timezone

        from runtime.remote_access.authorization import TrustState
        from runtime.remote_access.identity import ConnectorIdentity
        from runtime.remote_access.pairing import PairingManager
        from runtime.remote_access.state import InMemoryTrustStateStore

        if self._pairing is None:
            identity = ConnectorIdentity(
                tenant_id="diy", home_id="home-a", connector_id="connector-a"
            )
            store = InMemoryTrustStateStore(
                TrustState(connector_identity=identity, pairing_epoch=0, revocation_epoch=0)
            )
            self._pairing = PairingManager(
                state_store=store,
                identity=identity,
                now_fn=lambda: datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc),
            )
        return self._pairing

    def __init__(self, config) -> None:
        self.config = config
        self.calls: list[str] = []
        self.ready = True
        self.upgrade_ok = True
        self._pairing = None


@pytest.fixture
def config_file(tmp_path) -> str:
    config = _config(tmp_path)
    path = tmp_path / "config.json"
    config.to_file(path)
    return str(path)


@pytest.fixture(autouse=True)
def stub_supervisor(monkeypatch):
    instances: list[StubSupervisor] = []
    current: list[StubSupervisor | None] = [StubSupervisor(config=None)]
    instances.append(current[0])

    def factory(*args, **kwargs):
        return current[0]

    monkeypatch.setattr(cli, "ConnectorSupervisor", factory)
    return instances


def test_parser_exposes_all_commands() -> None:
    parser = cli.build_parser()
    names = {action.dest for action in parser._actions if hasattr(action, "choices")}
    sub_actions = [a for a in parser._actions if a.dest == "command"][0]
    assert set(sub_actions.choices) == {
        "run",
        "install",
        "uninstall",
        "start",
        "stop",
        "restart",
        "enable",
        "disable",
        "status",
        "readiness",
        "diagnose",
        "upgrade",
        "rollback",
        "pair",
        "list-devices",
        "revoke",
        "remove-device",
        "pairing-status",
        "recovery",
        "retire-enrollment-source",
        "reconcile-enrollment-retirement",
        "prepare-fresh-enrollment",
        "credential-capability",
    }


def test_missing_config_returns_1(tmp_path, capsys) -> None:
    code = cli.main(["status", "--config", str(tmp_path / "nope.json")])
    assert code == 1
    assert "config file not found" in capsys.readouterr().err


def test_invalid_config_returns_1(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"tenant_id": "x"}')
    code = cli.main(["status", "--config", str(bad)])
    assert code == 1
    assert "error" in capsys.readouterr().err.lower()


def test_install_delegates(config_file, stub_supervisor) -> None:
    code = cli.main(["install", "--config", config_file])
    assert code == 0
    assert stub_supervisor[0].calls == ["install:True"]


def test_install_no_enable(config_file, stub_supervisor) -> None:
    code = cli.main(["install", "--no-enable", "--config", config_file])
    assert code == 0
    assert stub_supervisor[0].calls == ["install:False"]


def test_lifecycle_verbs(config_file, stub_supervisor) -> None:
    for verb in ("start", "stop", "restart", "enable", "disable", "uninstall"):
        code = cli.main([verb, "--config", config_file])
        assert code == 0
    assert stub_supervisor[0].calls == [
        "start",
        "stop",
        "restart",
        "enable",
        "disable",
        "uninstall",
    ]


def test_readiness_exit_0_when_ready(config_file, stub_supervisor, capsys) -> None:
    code = cli.main(["readiness", "--config", config_file])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True


def test_readiness_exit_1_when_not_ready(config_file, stub_supervisor, capsys) -> None:
    stub_supervisor[0].ready = False
    code = cli.main(["readiness", "--config", config_file])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False


def test_diagnose_redacted(config_file, stub_supervisor, capsys) -> None:
    code = cli.main(["diagnose", "--config", config_file])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["secrets"] == "redacted"


@pytest.mark.parametrize("command", ["readiness", "diagnose"])
def test_policy_load_failure_is_category_only_at_real_cli_seam(
    tmp_path, monkeypatch, capsys, command
) -> None:
    policy_path = tmp_path / "HOSTILE-ABSOLUTE-POLICY-NAME.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": "HOSTILE-SCHEMA-VALUE",
                "artifact": {"secret": "HOSTILE-BODY-VALUE"},
                "artifact_version": 1,
                "issued_at": "not-a-date",
            }
        )
    )
    config = _config(tmp_path, policy_path=str(policy_path))
    config_path = tmp_path / "config.json"
    config.to_file(config_path)
    monkeypatch.setattr(cli, "ConnectorSupervisor", ConnectorSupervisor)

    code = cli.main([command, "--config", str(config_path)])

    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert code == (1 if command == "readiness" else 0)
    assert "policy_malformed" in rendered
    for hostile in (
        str(policy_path),
        "HOSTILE-ABSOLUTE-POLICY-NAME",
        "HOSTILE-SCHEMA-VALUE",
        "HOSTILE-BODY-VALUE",
        "Traceback",
    ):
        assert hostile not in rendered


def test_status_delegates(config_file, stub_supervisor, capsys) -> None:
    code = cli.main(["status", "--config", config_file])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["active_state"] == "active"


def test_upgrade_and_rollback(config_file, stub_supervisor) -> None:
    assert cli.main(["upgrade", "--config", config_file]) == 0
    assert stub_supervisor[0].calls == ["upgrade:True"]
    assert cli.main(["upgrade", "--no-verify", "--config", config_file]) == 0
    assert stub_supervisor[0].calls == ["upgrade:True", "upgrade:False"]
    assert cli.main(["rollback", "--config", config_file]) == 0
    assert stub_supervisor[0].calls[-1] == "rollback"


def test_upgrade_failure_exit_1(config_file, stub_supervisor) -> None:
    stub_supervisor[0].upgrade_ok = False
    assert cli.main(["upgrade", "--config", config_file]) == 1


def test_run_requires_lab_only_when_lab_configured(tmp_path, capsys) -> None:
    config = _config(tmp_path)  # lab configured
    path = tmp_path / "config.json"
    config.to_file(path)
    code = cli.main(["run", "--config", str(path)])
    assert code == 1
    assert "lab-only" in capsys.readouterr().err


def test_run_with_lab_only_flag_banner_printed(tmp_path, monkeypatch, capsys) -> None:
    config = _config(tmp_path)
    path = tmp_path / "config.json"
    config.to_file(path)
    seen: dict = {}

    class RunStub(StubSupervisor):
        def __init__(self, config):
            super().__init__(config)
            self.config.lab = LabProviderConfig(bind_host="127.0.0.1", lab_only=True)

        def run(self, *args, **kwargs):
            seen["ran"] = True
            return 0

    monkeypatch.setattr(cli, "ConnectorSupervisor", RunStub)
    code = cli.main(["run", "--lab-only", "--config", str(path)])
    assert code == 0
    assert seen.get("ran") is True
    assert LAB_ONLY_BANNER in capsys.readouterr().err


def test_run_without_provider_config_fails_closed(tmp_path, monkeypatch, capsys) -> None:
    """TASK-6004 [HIGH]: a runnable config WITHOUT a concrete lab
    provider/listener must fail closed — the supervisor's run() raises
    ConnectorConfigError and the CLI exits 1 with an error; READY=1 can never
    be emitted without a listener."""
    config = _config(tmp_path, lab=False)
    path = tmp_path / "config.json"
    config.to_file(path)
    seen: dict = {}

    class RunStub(StubSupervisor):
        def run(self, *args, **kwargs):
            # Mirrors the REAL supervisor: provider-less run is rejected at
            # startup before the notify loop.
            raise ConnectorConfigError(
                "refusing to run: no lab provider configured"
            )

    monkeypatch.setattr(cli, "ConnectorSupervisor", RunStub)
    code = cli.main(["run", "--config", str(path)])
    assert code == 1
    assert seen == {}
    assert "no lab provider" in capsys.readouterr().err


# ── Supported-DIY ceremony commands (THR-097 Unit 3A) ──────────────────────


def _diy_config_file(tmp_path) -> str:
    from runtime.remote_access.diy_provider import DiyProviderConfig
    from runtime.remote_access.network import NetworkConfig

    config = _config(tmp_path, lab=False)
    config.diy = DiyProviderConfig(
        network=NetworkConfig(mode="tailscale"),
        bind_port=8443,
    )
    path = tmp_path / "diy-config.json"
    config.to_file(path)
    return str(path)


def test_run_requires_diy_flag(tmp_path, capsys) -> None:
    """A Supported-DIY provider config is never started without the explicit
    --diy opt-in (mirrors the lab-only double-gating)."""
    path = _diy_config_file(tmp_path)
    code = cli.main(["run", "--config", path])
    assert code == 1
    assert "pass --diy" in capsys.readouterr().err


def test_run_requires_managed_flag(tmp_path, capsys) -> None:
    from runtime.remote_access.managed_provider import ManagedProviderConfig

    config = _config(tmp_path, lab=False, managed=ManagedProviderConfig())
    path = tmp_path / "managed-config.json"
    config.to_file(path)
    assert cli.main(["run", "--config", str(path)]) == 1
    assert "pass --managed" in capsys.readouterr().err


def test_pair_prints_code_once(tmp_path, capsys) -> None:
    path = _diy_config_file(tmp_path)
    code = cli.main(["pair", "--config", path, "--device", "macbook-pro"])
    assert code == 0
    out = capsys.readouterr().out
    assert "pairing code for device 'macbook-pro':" in out
    code_line = [l for l in out.splitlines() if "pairing code for device" in l][0]
    code_text = code_line.split(": ")[-1].strip()
    # The code is short and single-use; never a credential.
    assert len(code_text) == 8


def test_pair_rejects_blank_device(tmp_path, capsys) -> None:
    path = _diy_config_file(tmp_path)
    code = cli.main(["pair", "--config", path, "--device", "  "])
    assert code == 1
    assert "error" in capsys.readouterr().err


def test_revoke_all_and_one(tmp_path, capsys) -> None:
    path = _diy_config_file(tmp_path)
    assert cli.main(["pair", "--config", path, "--device", "phone"]) == 0
    assert cli.main(["pair", "--config", path, "--device", "macbook-pro"]) == 0
    code = cli.main(["revoke", "--config", path, "--device", "phone"])
    assert code == 0
    out = capsys.readouterr().out
    assert "revoked phone" in out
    code = cli.main(["revoke", "--config", path])
    assert code == 0
    out = capsys.readouterr().out
    assert "ALL devices" in out


def test_revoke_output_never_claims_cross_process_stream_closure(tmp_path, capsys) -> None:
    """The CLI revoke runs in a SEPARATE process from the connector that
    serves the live streams. It must persist the revocation and point at the
    connector's reconciliation — never report false success
    ("live streams closed") for streams it cannot prove closed
    (TASK-6039 reviewer [CRITICAL] finding 2)."""
    path = _diy_config_file(tmp_path)
    assert cli.main(["pair", "--config", path, "--device", "macbook-pro"]) == 0
    capsys.readouterr()
    code = cli.main(["revoke", "--config", path])
    assert code == 0
    out = capsys.readouterr().out
    assert "live streams closed" not in out
    assert "reconciliation" in out


def test_list_devices_redacted(tmp_path, capsys, monkeypatch) -> None:
    """Real ceremony flow: pair issues a pending code; redemption (the away
    client's POST /pair) pairs the device; list-devices shows it — redacted
    (no credentials/digests)."""
    from pathlib import Path

    from runtime.remote_access.supervisor import ConnectorSupervisor, ConnectorConfig

    path = _diy_config_file(tmp_path)
    # Use the REAL supervisor (not the autouse stub) for this test.
    monkeypatch.setattr(cli, "ConnectorSupervisor", ConnectorSupervisor)
    config = ConnectorConfig.from_file(Path(path))
    supervisor = ConnectorSupervisor(config=config)
    pairing = supervisor.pairing_manager()

    issued = pairing.issue_pairing_code("macbook-pro")
    # Not redeemed yet:
    assert [d.device_id for d in pairing.list_devices()] == []
    # Redeem like the away client:
    credential = pairing.redeem_pairing(issued.code)
    assert credential is not None
    devices = pairing.list_devices()
    assert [d.device_id for d in devices] == ["macbook-pro"]

    code = cli.main(["list-devices", "--config", path])
    assert code == 0
    out = capsys.readouterr().out
    assert "macbook-pro" in out
    assert "hrpair_" not in out
    assert "credential_digest" not in out
    assert credential not in out


def test_remove_device(tmp_path, capsys) -> None:
    path = _diy_config_file(tmp_path)
    assert cli.main(["pair", "--config", path, "--device", "macbook-pro"]) == 0
    capsys.readouterr()
    code = cli.main(["remove-device", "--config", path, "--device", "macbook-pro"])
    assert code == 0
    assert "removed device 'macbook-pro'" in capsys.readouterr().out
    code = cli.main(["pairing-status", "--config", path])
    assert code == 0
    assert "macbook-pro" not in capsys.readouterr().out


def test_pairing_status_truthful(tmp_path, capsys) -> None:
    path = _diy_config_file(tmp_path)
    assert cli.main(["pair", "--config", path, "--device", "phone"]) == 0
    capsys.readouterr()
    code = cli.main(["pairing-status", "--config", path])
    assert code == 0
    out = capsys.readouterr().out
    assert '"pending"' in out or "pending" in out
    # No credential material in status.
    assert "hrpair_" not in out


def test_recovery_requires_confirmation(tmp_path, capsys) -> None:
    path = _diy_config_file(tmp_path)
    assert cli.main(["pair", "--config", path, "--device", "phone"]) == 0
    capsys.readouterr()
    # Without --factory-reset the destructive action is refused.
    code = cli.main(["recovery", "--config", path])
    assert code == 1
    assert "--factory-reset" in capsys.readouterr().err


def test_recovery_factory_reset_real_files(tmp_path, capsys, monkeypatch) -> None:
    """recovery --factory-reset deletes BOTH snapshot+anchor so the next load
    returns the fresh deny-all default (never a partial pair)."""
    from datetime import datetime, timezone
    from pathlib import Path

    from runtime.remote_access.authorization import DeviceAuthorization, TrustState
    from runtime.remote_access.identity import ConnectorIdentity
    from runtime.remote_access.state_store import AtomicFileTrustStateStore
    from runtime.remote_access.supervisor import ConnectorSupervisor

    identity = ConnectorIdentity(tenant_id="diy", home_id="home-a", connector_id="connector-a")
    state_path = tmp_path / "trust-state.json"
    store = AtomicFileTrustStateStore(
        state_path,
        TrustState(connector_identity=identity, pairing_epoch=0, revocation_epoch=0),
    )
    state = store.load()
    state.apply_pairing(
        DeviceAuthorization(
            device_id="phone",
            tenant_id="diy",
            home_id="home-a",
            authorization_epoch=1,
            expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
    )
    store.save(state)
    assert state_path.exists() and Path(str(state_path) + ".anchor").exists()

    config = _config(tmp_path, lab=False)
    config.state_path = str(state_path)
    supervisor = ConnectorSupervisor(config=config)

    code = cli._factory_reset(supervisor)
    assert code == 0
    assert not state_path.exists()
    assert not Path(str(state_path) + ".anchor").exists()
    out = capsys.readouterr().out
    assert "factory reset complete" in out


def test_recovery_no_state_is_noop(tmp_path, capsys) -> None:
    from runtime.remote_access.supervisor import ConnectorSupervisor

    config = _config(tmp_path, lab=False)
    config.state_path = str(tmp_path / "missing-trust-state.json")
    supervisor = ConnectorSupervisor(config=config)
    code = cli._factory_reset(supervisor)
    assert code == 0
    assert "nothing to reset" in capsys.readouterr().out
