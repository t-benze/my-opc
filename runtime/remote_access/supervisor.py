"""Connector supervisor orchestration (THR-097 phase unit 3).

Composes readiness, the service manager, the lab provider, the trust-state
store, the policy consumer, and the credential provider into the supervised
Linux connector lifecycle:

- ``run`` — the systemd ``Type=notify`` foreground loop: readiness gates
  before ANY listener; on readiness loss the listener is stopped
  immediately (fail closed); READY/WATCHDOG via sd_notify. A runnable
  configuration REQUIRES a concrete lab provider/listener — provider-less
  run configurations are rejected at startup and NEVER emit READY=1.
- ``install``/``uninstall``/``start``/``stop``/``restart``/``enable``/
  ``disable``/``status`` — systemd service lifecycle. ``install`` treats the
  source trust state only as an initial seed when NO managed pair exists;
  once a managed snapshot+anchor pair exists it is authoritative and is
  never overwritten or rolled back by a stale operator source pair.
- ``upgrade``/``rollback`` — unit replacement with auto-rollback on failed
  start.
- ``readiness_report``/``diagnose`` — local diagnostics that never render
  the daemon bearer.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from runtime.remote_access.authorization import AuthorizationVerifier, DeviceAuthorization, TrustState
from runtime.remote_access.credentials import (
    CredentialUnavailable,
    DaemonCredentialProvider,
    FileDaemonCredentialProvider,
    SystemdCredentialProvider,
)
from runtime.remote_access.diy_provider import (
    DiyProviderAdapter,
    DiyProviderConfig,
    DiyProviderError,
    make_diy_context_factory,
)
from runtime.remote_access.forwarding import LOOPBACK_HOST, HttpLoopbackForwarder, LoopbackTarget
from runtime.remote_access.gateway import GatewayContext
from runtime.remote_access.identity import (
    ConnectorIdentity,
    DeviceProof,
    ProofVerdict,
    SingleUseGuard,
    StaticProofVerifier,
)
from runtime.remote_access.lab_provider import (
    LAB_ONLY_BANNER,
    LabProviderAdapter,
    LabProviderConfig,
    LabProviderError,
)
from runtime.remote_access.managed_provider import (
    ManagedProviderAdapter,
    ManagedProviderConfig,
    ManagedProviderError,
)
from runtime.remote_access.network import NetworkAddressError, NetworkConfig
from runtime.remote_access.pairing import PairingManager
from runtime.remote_access.policy import PolicyEnvelope, PolicyError, RoutePolicyConsumer
from runtime.remote_access.readiness import ConnectorReadiness, ReadinessReport
from runtime.remote_access.service_manager import (
    ServiceManagerError,
    ServiceStatus,
    SystemdServiceManager,
    UpgradeOutcome,
)
from runtime.remote_access.state import TrustStateStore
from runtime.remote_access.state_store import (
    AtomicFileTrustStateStore,
    CorruptTrustStateError,
    StateStoreError,
)
from runtime.remote_access.streams import StreamCloseError, StreamRegistry
from runtime.remote_access.stripping import CredentialScanner
from runtime.remote_access.systemd_unit import ConnectorUnitSpec, render_connector_unit

DEFAULT_UNIT_NAME = "happyranch-connector.service"
DEFAULT_STATE_DIR = "happyranch-connector"
DEFAULT_POLL_SECONDS = 5.0

# Owner-only mode for staged managed files (config/policy/state under the
# dedicated service user's state directory).
_MANAGED_FILE_MODE = 0o600


class ConnectorConfigError(Exception):
    """The connector config is invalid (fail closed before any side effect)."""


class PolicyLoadError(Exception):
    """Stable, secret-free expected failure to load a local policy artifact."""

    def __init__(self, category: str = "policy_malformed") -> None:
        super().__init__("route policy malformed or unreadable")
        self.category = category


@dataclass
class ConnectorConfig:
    """Operator-facing connector configuration (local JSON file)."""

    # bind identity
    tenant_id: str = ""
    home_id: str = ""
    connector_id: str = ""
    # daemon + credential sources
    daemon_port: int | None = None
    daemon_token_path: str | None = None
    credentials_directory: str | None = None
    # policy artifact (PolicyEnvelope JSON) and trust state file
    policy_path: str | None = None
    state_path: str = "~/.happyranch/remote_access/trust-state.json"
    # managed-dir root override: where install() stages the service config/
    # state/policy. None = /var/lib (system mode) or the user XDG state home
    # (user mode). An override keeps hermetic tests/conformance fully under a
    # temp root; production leaves it None.
    managed_dir_root: str | None = None
    # service-manager settings
    unit_name: str = DEFAULT_UNIT_NAME
    system: bool = False
    service_user: str = "happyranch-connector"
    service_group: str = "happyranch-connector"
    state_dir: str = DEFAULT_STATE_DIR
    run_dir: str = DEFAULT_STATE_DIR
    logs_dir: str = DEFAULT_STATE_DIR
    watchdog_sec: int = 30
    restart_sec: int = 1
    exec_start: tuple[str, ...] | None = None
    poll_seconds: float = DEFAULT_POLL_SECONDS
    # LAB-ONLY conformance provider (never a product/DIY lane)
    lab: LabProviderConfig | None = None
    # Supported-DIY customer-owned-network provider (THR-097 Unit 3A)
    diy: DiyProviderConfig | None = None
    managed: ManagedProviderConfig | None = None

    def validate(self) -> None:
        if not (self.tenant_id and self.home_id and self.connector_id):
            raise ConnectorConfigError("tenant_id/home_id/connector_id are required")
        if self.daemon_port is None:
            raise ConnectorConfigError("daemon_port is required")
        if not self.daemon_token_path and not self.credentials_directory:
            raise ConnectorConfigError("daemon_token_path or credentials_directory is required")
        if not self.policy_path:
            raise ConnectorConfigError("policy_path is required")
        if self.lab is not None and self.lab.lab_only is not True:
            raise ConnectorConfigError("lab provider requires lab_only: true")
        if (
            sum(value is not None for value in (self.lab, self.diy, self.managed))
            > 1
        ):
            raise ConnectorConfigError("lab, diy, and managed providers are mutually exclusive")
        if self.diy is not None:
            try:
                self.diy.validate()
            except (DiyProviderError, NetworkAddressError) as exc:
                raise ConnectorConfigError(f"invalid diy provider config: {exc}") from exc
        if self.managed is not None:
            try:
                self.managed.validate()
            except ManagedProviderError as exc:
                raise ConnectorConfigError("invalid managed provider config") from exc

    @classmethod
    def from_file(cls, path: Path) -> "ConnectorConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        lab_raw = raw.pop("lab", None)
        diy_raw = raw.pop("diy", None)
        managed_raw = raw.pop("managed", None)
        config = cls(**raw)
        if lab_raw is not None:
            config.lab = LabProviderConfig(**lab_raw)
        if diy_raw is not None:
            network_raw = diy_raw.pop("network", None) or {}
            config.diy = DiyProviderConfig(network=NetworkConfig(**network_raw), **diy_raw)
        if managed_raw is not None:
            config.managed = ManagedProviderConfig(**managed_raw)
        config.validate()
        return config

    def to_file(self, path: Path) -> None:
        data = json.loads(json.dumps(self, default=_json_default))
        Path(path).write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
        )


def _json_default(obj):
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"not serializable: {obj!r}")


class _MissingCredentialProvider:
    """A credential provider that always fails closed (no source configured)."""

    def read_bearer(self) -> str:
        raise CredentialUnavailable("no daemon credential source configured")


def sd_notify(state: str, notify_socket: str | None = None) -> bool:
    """Send one sd_notify datagram (``READY=1``/``WATCHDOG=1``/``STOPPING=1``)
    to ``$NOTIFY_SOCKET``. Returns False when not running under systemd."""
    health_fd = os.environ.get("HAPPYRANCH_CHILD_HEALTH_FD")
    generation = os.environ.get("HAPPYRANCH_CHILD_HEALTH_GENERATION")
    if health_fd is not None or generation is not None:
        return _write_child_health(state, health_fd, generation)

    import socket

    path = notify_socket if notify_socket is not None else __import__("os").environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(path)
            sock.sendall(state.encode("utf-8"))
        finally:
            sock.close()
        return True
    except OSError:
        return False


_health_lock = threading.Lock()
_health_sequence = 0


def _write_child_health(state: str, raw_fd: str | None, generation: str | None) -> bool:
    """Write one strict, generation-bound record to the main supervisor.

    Frozen PyInstaller executables use a bootloader parent, so only this
    structured pipe crosses the child boundary; the child never inherits or
    writes systemd's notification socket.
    """
    kinds = {
        "READY=1\n": "ready",
        "WATCHDOG=1\n": "healthy",
        "STOPPING=1\n": "stopping",
        "STATUS=waiting for readiness\n": "waiting",
        "STATUS=no provider configured; no listener\n": "failed",
        "STATUS=provider failed to start; no listener\n": "failed",
    }
    if raw_fd is None or generation is None or state not in kinds:
        return False
    try:
        fd = int(raw_fd)
        if fd < 3 or len(generation) != 32:
            return False
        int(generation, 16)
        global _health_sequence
        with _health_lock:
            _health_sequence += 1
            record = {
                "version": 1,
                "generation": generation,
                "sequence": _health_sequence,
                "state": kinds[state],
            }
            raw = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
            os.write(fd, raw)
        return True
    except (OSError, ValueError):
        return False


class ConnectorSupervisor:
    """Composes the Linux connector lifecycle."""

    def __init__(
        self,
        *,
        config: ConnectorConfig,
        manager: SystemdServiceManager | None = None,
        state_store: TrustStateStore | None = None,
        policy: RoutePolicyConsumer | None = None,
        readiness: ConnectorReadiness | None = None,
        provider: LabProviderAdapter | None = None,
        now_fn: Callable[[], datetime] | None = None,
        notify_fn: Callable[[str], bool] | None = None,
    ) -> None:
        self.config = config
        self._manager = manager
        self._state_store = state_store
        self._policy = policy
        self._readiness = readiness
        self._injected_provider = provider
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._notify_fn = notify_fn or (lambda state: sd_notify(state))
        self._provider: LabProviderAdapter | None = None
        self._provider_running = False
        self._lifecycle_lock = threading.RLock()
        self._shutdown = False
        # ONE authoritative live-stream registry and ONE pairing manager per
        # process, shared by the gateway ctx factories, the provider adapter,
        # and the ceremony surface — in-process revocation closes the REAL
        # shipping streams, never an unrelated empty registry (TASK-6039
        # reviewer [CRITICAL] finding 2).
        self._registry: StreamRegistry | None = None
        self._pairing_manager: PairingManager | None = None
        # The revocation epoch the run loop has already reconciled (streams
        # closed): cross-process revoke/remove from the CLI is closed here
        # within one poll interval.
        self._reconciled_revocation_epoch: int = 0

    # ── construction helpers (CLI path) ───────────────────────────────────

    @property
    def manager(self) -> SystemdServiceManager:
        if self._manager is None:
            self._manager = SystemdServiceManager(system=self.config.system)
        return self._manager

    @property
    def state_store(self) -> TrustStateStore:
        if self._state_store is None:
            state_path = Path(self.config.state_path).expanduser()
            self._state_store = AtomicFileTrustStateStore(
                state_path, self.initial_state()
            )
        return self._state_store

    @property
    def registry(self) -> StreamRegistry:
        """The ONE authoritative live-stream registry for this process,
        shared by the gateway ctx factories, the provider adapters, and the
        pairing manager: a revocation closes the REAL shipping streams
        (never an unrelated empty registry reporting false success)."""
        if self._registry is None:
            self._registry = StreamRegistry()
        return self._registry

    def initial_state(self) -> TrustState:
        """First-run state: connector identity + (lab-only) lab device pairing.

        In lab mode the explicit lab config is the pairing ceremony (clearly
        LAB-ONLY — never a product pairing path); the state store persists it
        so revocation stays effective across restarts. The Supported-DIY lane
        starts with NO paired devices: pairing is the explicit local ceremony
        (``pair``/``POST /pair``) — the operator approves each device.
        """
        state = TrustState(
            connector_identity=self._configured_identity(),
            pairing_epoch=0,
            revocation_epoch=0,
        )
        lab = self.config.lab
        if lab is not None:
            state.apply_pairing(
                DeviceAuthorization(
                    device_id=lab.lab_device_id,
                    tenant_id=self.config.tenant_id,
                    home_id=self.config.home_id,
                    authorization_epoch=1,
                    expires_at=self._now_fn() + timedelta(days=3650),
                )
            )
        return state

    def _configured_identity(self) -> ConnectorIdentity:
        return ConnectorIdentity(
            tenant_id=self.config.tenant_id,
            home_id=self.config.home_id,
            connector_id=self.config.connector_id,
        )

    def credential_provider(self) -> DaemonCredentialProvider:
        # Under the service path systemd sets $CREDENTIALS_DIRECTORY (the
        # LoadCredential= injection the unit renders): consume it AUTOMATICALLY
        # — no redundant config, and never fall back to reading the daemon
        # home. A missing injected credential fails closed instead.
        if os.environ.get("CREDENTIALS_DIRECTORY"):
            return SystemdCredentialProvider()
        if self.config.credentials_directory:
            return SystemdCredentialProvider(self.config.credentials_directory)
        if self.config.daemon_token_path:
            return FileDaemonCredentialProvider(Path(self.config.daemon_token_path))
        return _MissingCredentialProvider()

    def load_policy(self) -> RoutePolicyConsumer:
        if self._policy is not None:
            return self._policy
        if not self.config.policy_path:
            raise ConnectorConfigError("policy_path is required")
        try:
            raw = json.loads(
                Path(self.config.policy_path).read_text(encoding="utf-8")
            )
            envelope = PolicyEnvelope.model_validate(raw)
            self._policy = RoutePolicyConsumer.from_envelope(
                envelope, now=self._now_fn()
            )
        except PolicyError as exc:
            category = (
                exc.outcome.audit_category
                if exc.outcome is not None
                else "policy_malformed"
            )
            raise PolicyLoadError(category) from None
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError):
            raise PolicyLoadError() from None
        return self._policy

    def build_readiness(self) -> ConnectorReadiness:
        if self._readiness is not None:
            return self._readiness
        policy = None
        policy_failure_category = None
        try:
            policy = self.load_policy()
        except PolicyLoadError as exc:
            policy_failure_category = exc.category
        return ConnectorReadiness(
            daemon_port=self.config.daemon_port,
            credential_provider=self.credential_provider(),
            policy=policy,
            policy_failure_category=policy_failure_category,
            configured_identity=self._configured_identity(),
            state_store=self.state_store,
        )

    def build_ctx_factory(self) -> Callable[[datetime], GatewayContext]:
        """Wire the full gateway context used by the LAB provider (a fixed
        lab proof — clearly LAB-ONLY, never a product pairing path). Streams
        register in the ONE authoritative process registry."""
        config = self.config
        identity_ = self._configured_identity()
        lab_device = config.lab.lab_device_id if config.lab else "lab-client-1"
        credential_provider = self.credential_provider()
        forwarder = HttpLoopbackForwarder(
            LoopbackTarget(LOOPBACK_HOST, config.daemon_port)
        )
        registry = self.registry

        def factory(now: datetime) -> GatewayContext:
            state = self.state_store.load()
            return GatewayContext(
                connector_identity=identity_,
                proof=DeviceProof(
                    device_id=lab_device,
                    tenant_id=identity_.tenant_id,
                    home_id=identity_.home_id,
                    nonce=f"lab-{uuid.uuid4().hex[:16]}",
                    issued_at=now - timedelta(minutes=1),
                    expires_at=now + timedelta(minutes=5),
                ),
                proof_verifier=StaticProofVerifier(ProofVerdict(ok=True)),
                single_use_guard=SingleUseGuard(),
                authorization=AuthorizationVerifier(state),
                policy=self.load_policy(),
                credential_provider=credential_provider,
                forwarder=forwarder,
                stream_registry=registry,
                scanner=CredentialScanner(),
                now=now,
            )

        return factory

    def build_diy_ctx_factory(self):
        """Wire the per-request gateway context factory for the Supported-DIY
        provider: the device proof is derived from the presented
        ``X-HappyRanch-Device-Credential`` (THR-034 wire contract), verified
        by the production pairing verifier. Streams register in the ONE
        authoritative process registry shared with the pairing manager, so
        in-process revoke closes the REAL shipping streams."""
        config = self.config
        identity_ = self._configured_identity()
        credential_provider = self.credential_provider()
        forwarder = HttpLoopbackForwarder(
            LoopbackTarget(LOOPBACK_HOST, config.daemon_port)
        )
        return make_diy_context_factory(
            identity=identity_,
            pairing=self.pairing_manager(),
            policy=self.load_policy(),
            credential_provider=credential_provider,
            forwarder=forwarder,
            registry=self.registry,
            now_fn=self._now_fn,
        )

    def pairing_manager(self) -> PairingManager:
        """The ONE authoritative Supported-DIY pairing ceremony manager for
        this process, bound to the shared authoritative stream registry —
        revocation closes the REAL shipping streams, never an unrelated
        empty registry. Cached so the provider, the ctx factory, and the
        ceremony surface all mutate/verify through the SAME instance."""
        if self._pairing_manager is None:
            config = self.config
            diy = config.diy
            self._pairing_manager = PairingManager(
                state_store=self.state_store,
                identity=self._configured_identity(),
                now_fn=self._now_fn,
                code_ttl_seconds=diy.token_ttl_seconds if diy else 300,
                credential_ttl_days=diy.credential_ttl_days if diy else 365,
                registry=self.registry,
            )
        return self._pairing_manager

    def build_provider(self) -> LabProviderAdapter | None:
        """Construct a FRESH lab adapter. A stopped ``ThreadingHTTPServer``
        cannot be restarted, so each start after a stop builds a new adapter
        (tests inject a fake via the constructor instead)."""
        if self.config.lab is None:
            return None
        return LabProviderAdapter(
            config=self.config.lab,
            readiness=self.build_readiness(),
            ctx_factory=self.build_ctx_factory(),
        )

    def build_diy_provider(self) -> DiyProviderAdapter | None:
        """Construct a FRESH Supported-DIY adapter (same restart rule as the
        lab adapter). Returns None when no diy provider is configured."""
        config = self.config
        if config.diy is None:
            return None
        return DiyProviderAdapter(
            config=config.diy,
            readiness=self.build_readiness(),
            pairing=self.pairing_manager(),
            identity=self._configured_identity(),
            ctx_factory=self.build_diy_ctx_factory(),
        )

    def build_managed_provider(self) -> ManagedProviderAdapter | None:
        if self.config.managed is None:
            return None
        return ManagedProviderAdapter(
            config=self.config.managed,
            readiness=self.build_readiness(),
            pairing=self.pairing_manager(),
            identity=self._configured_identity(),
            ctx_factory=self.build_diy_ctx_factory(),
        )

    # ── readiness / diagnostics ───────────────────────────────────────────

    def readiness_report(self) -> ReadinessReport:
        return self.build_readiness().evaluate(self._now_fn())

    def diagnose(self) -> dict:
        """Redacted local diagnostics. NEVER renders the daemon bearer."""
        report = self.readiness_report()
        gates = {
            name: {"ok": gate.ok, "category": gate.category}
            for name, gate in report.gates.items()
        }
        status: dict | None = None
        try:
            service = self.manager.status(self.config.unit_name)
            status = {
                "active_state": service.active_state,
                "sub_state": service.sub_state,
                "running": service.running,
            }
        except ServiceManagerError:
            status = {"error": "unavailable"}
        store_ok = True
        store_reason = "ok"
        try:
            self.state_store.load()
        except StateStoreError as exc:
            store_ok = False
            store_reason = "corrupt" if isinstance(exc, CorruptTrustStateError) else "unavailable"
        provider = None
        if self.config.lab is not None:
            try:
                adapter = self.build_provider()
            except PolicyLoadError:
                adapter = None
            provider = {
                "type": "lab",
                "listening": bool(adapter and adapter.listening),
                "bound_port": adapter.bound_port if adapter else None,
                "banner": LAB_ONLY_BANNER,
            }
        elif self.config.diy is not None:
            try:
                adapter = self.build_diy_provider()
            except PolicyLoadError:
                adapter = None
            pairing = self.pairing_manager()
            try:
                pairing_status = pairing.pairing_status()
            except StateStoreError:
                pairing_status = {"error": "state_unavailable"}
            provider = {
                "type": "diy",
                "listening": bool(adapter and adapter.listening),
                "bound_port": adapter.bound_port if adapter else None,
                "bind_address": adapter.bind_address if adapter else None,
                "network_mode": self.config.diy.network.mode,
                "pairing": pairing_status,
            }
        elif self.config.managed is not None:
            try:
                adapter = self.build_managed_provider()
            except PolicyLoadError:
                adapter = None
            provider = {
                "type": "managed",
                "listening": bool(adapter and adapter.listening),
                "bound_port": adapter.bound_port if adapter else None,
                "bind_address": "loopback" if adapter else None,
            }
        return {
            "role": "happyranch-connector",
            "unit_name": self.config.unit_name,
            "readiness": {"ready": report.ready, "gates": gates},
            "service": status,
            "state_store": {"ok": store_ok, "reason": store_reason},
            "policy": {
                "configured": bool(self.config.policy_path),
                "current": gates.get("current_policy", {}).get("ok", False),
            },
            "provider": provider,
            "secrets": "redacted",
        }

    # ── systemd lifecycle ─────────────────────────────────────────────────

    def _managed_state_root(self, config: ConnectorConfig | None = None) -> Path:
        """The managed state root for *config*: where install() stages the
        service config/state/policy and where the rendered unit points
        ``--config``. ``StateDirectory=<state_dir>`` resolves to the same
        location systemd creates for the service user (system: ``/var/lib``;
        user: the XDG state home)."""
        cfg = config or self.config
        if cfg.managed_dir_root:
            return Path(cfg.managed_dir_root).expanduser() / cfg.state_dir
        if cfg.system:
            return Path("/var/lib") / cfg.state_dir
        return Path.home() / ".local" / "state" / cfg.state_dir

    def unit_spec(self, config: ConnectorConfig | None = None) -> ConnectorUnitSpec:
        cfg = config or self.config
        daemon_token = cfg.daemon_token_path or ""
        args = cfg.exec_start
        if args is None:
            # The default unit points --config at the MANAGED config path (the
            # dedicated service user's state directory) — never at a
            # ~/.happyranch path the hardened unit cannot read. The rendered
            # unit passes the provider opt-in flag (--lab-only / --diy) so a
            # provider is never silently started without its explicit opt-in.
            args = (
                "/opt/happyranch/venv/bin/python",
                "-m",
                "runtime.remote_access.cli",
                "run",
                "--config",
                str(self._managed_state_root(cfg) / "config.json"),
            )
            if cfg.lab is not None:
                args += ("--lab-only",)
            if cfg.diy is not None:
                args += ("--diy",)
            if cfg.managed is not None:
                args += ("--managed",)
        return ConnectorUnitSpec(
            unit_name=cfg.unit_name,
            exec_start=args,
            system=cfg.system,
            user=cfg.service_user if cfg.system else None,
            group=cfg.service_group if cfg.system else None,
            daemon_token_path=daemon_token,
            state_dir=cfg.state_dir,
            run_dir=cfg.run_dir,
            logs_dir=cfg.logs_dir,
            watchdog_sec=cfg.watchdog_sec,
            restart_sec=cfg.restart_sec,
        )

    def _managed_config(self) -> ConnectorConfig:
        """The config the SERVICE actually runs: the operator's config with
        state/policy re-pointed at the managed directories (accessible to the
        dedicated service user). Never a ``~/.happyranch`` path."""
        cfg = self.config
        root = self._managed_state_root(cfg)
        return ConnectorConfig(
            tenant_id=cfg.tenant_id,
            home_id=cfg.home_id,
            connector_id=cfg.connector_id,
            daemon_port=cfg.daemon_port,
            daemon_token_path=cfg.daemon_token_path,
            credentials_directory=cfg.credentials_directory,
            policy_path=str(root / "policy.json"),
            state_path=str(root / "trust-state.json"),
            unit_name=cfg.unit_name,
            system=cfg.system,
            service_user=cfg.service_user,
            service_group=cfg.service_group,
            state_dir=cfg.state_dir,
            run_dir=cfg.run_dir,
            logs_dir=cfg.logs_dir,
            watchdog_sec=cfg.watchdog_sec,
            restart_sec=cfg.restart_sec,
            exec_start=cfg.exec_start,
            poll_seconds=cfg.poll_seconds,
            lab=cfg.lab,
            diy=cfg.diy,
            managed=cfg.managed,
            managed_dir_root=cfg.managed_dir_root,
        )

    def install(self, *, enable: bool = True) -> Path:
        """Stage config/state/policy into the declared managed directories and
        install the rendered unit. The source trust state is only an initial
        seed when NO managed pair exists; once a managed snapshot+anchor pair
        exists it is AUTHORITATIVE — reinstall never overwrites or rolls it
        back with a stale operator source pair (a revocation must survive
        reinstall/upgrade-recovery), refuses same-generation conflicts, and
        stages any monotonic advance transactionally. A corrupt or partial
        source state refuses installation (fail closed)."""
        managed = self._managed_config()
        return self._install_managed(managed, enable=enable)

    def _install_managed(self, managed: ConnectorConfig, *, enable: bool) -> Path:
        root = Path(managed.state_path).expanduser().parent
        try:
            root.mkdir(parents=True, exist_ok=True)
            os.chmod(root, 0o700)
        except OSError as exc:
            raise ConnectorConfigError(
                f"cannot create managed state directory {root}: {exc}"
            ) from exc
        # 1. The SOURCE trust state must be loadable before staging anything:
        #    a corrupt/partial pair (snapshot without its companion anchor)
        #    could hide a revocation and must never be installed over.
        try:
            self.state_store.load()
        except StateStoreError as exc:
            raise ConnectorConfigError(
                f"refusing to install: source trust state unusable: {exc}"
            ) from exc
        # 2. The managed snapshot+anchor pair is AUTHORITATIVE once it exists
        #    (TASK-6004 [HIGH]): install/reinstall must never overwrite or roll
        #    it back with a stale operator source pair — that would resurrect
        #    a revoked device. The source pair is only an initial seed when NO
        #    managed pair exists. Otherwise:
        #      - managed pair unreadable/partial  -> refuse (never replace it);
        #      - source generation < managed     -> refuse rollback;
        #      - equal generation, same bytes    -> keep managed (no-op);
        #      - equal generation, different     -> refuse (split/conflict);
        #      - source generation > managed     -> monotonic advance, staged
        #        transactionally (both files as temps BEFORE any rename, so an
        #        interrupted staging never leaves a usable mixed pair and
        #        never rolls back).
        managed_state_path = root / "trust-state.json"
        managed_anchor_path = root / "trust-state.json.anchor"
        source_path = Path(self.config.state_path).expanduser()
        managed_exists = managed_state_path.exists() or managed_anchor_path.exists()
        if managed_exists:
            managed_store = AtomicFileTrustStateStore(
                managed_state_path, self.initial_state()
            )
            try:
                managed_store.load()  # full pair binding validation
            except StateStoreError as exc:
                raise ConnectorConfigError(
                    f"refusing to install: managed trust state unusable: {exc}"
                ) from exc
            source_gen = self.state_store.anchored_generation()
            managed_gen = managed_store.anchored_generation()
            if source_gen is not None and managed_gen is not None:
                if source_gen < managed_gen:
                    raise ConnectorConfigError(
                        f"refusing to install: source trust state (generation "
                        f"{source_gen}) is OLDER than the managed state "
                        f"(generation {managed_gen}); refusing to roll back a "
                        f"revocation. Delete the managed pair deliberately to "
                        f"factory-reset."
                    )
                if source_gen == managed_gen:
                    if source_path.read_bytes() != managed_state_path.read_bytes():
                        raise ConnectorConfigError(
                            f"refusing to install: conflicting trust state at "
                            f"the same generation ({source_gen}); refusing to "
                            f"replace the managed pair with a different state."
                        )
                    # identical pair already in sync: keep the managed pair
                else:
                    # strictly newer source pair: monotonic advance
                    self._stage_state_pair(source_path, root)
        elif source_path.exists():
            # initial install: seed the managed pair from the source pair
            self._stage_state_pair(source_path, root)
        # 3. Stage the policy artifact the service consumes.
        src_policy = Path(self.config.policy_path).expanduser()
        if not src_policy.is_file():
            raise ConnectorConfigError(f"policy_path not found: {src_policy}")
        try:
            policy_data = src_policy.read_bytes()
            target = root / "policy.json"
            target.write_bytes(policy_data)
            os.chmod(target, _MANAGED_FILE_MODE)
        except OSError as exc:
            raise ConnectorConfigError(f"cannot stage policy: {exc}") from exc
        # 4. Write the managed config (what the service actually loads).
        try:
            managed.to_file(root / "config.json")
            os.chmod(root / "config.json", _MANAGED_FILE_MODE)
        except OSError as exc:
            raise ConnectorConfigError(f"cannot write managed config: {exc}") from exc
        # 5. System mode: the staged tree must be owned by the service user.
        if managed.system:
            self._chown_tree(root, managed.service_user, managed.service_group)
        spec = self.unit_spec(managed)
        return self.manager.install(
            render_connector_unit(spec), managed.unit_name, enable=enable
        )

    def _stage_state_pair(self, source_path: Path, root: Path) -> None:
        """Stage the VALIDATED source pair (snapshot + companion anchor) into
        the managed root under the managed names. Transactional staging: both
        files are written to temp names and fsynced BEFORE either is renamed
        into place, so a failure before the renames leaves the existing managed
        pair fully intact (never a rollback); a crash between the renames
        leaves a mismatched pair that ``load()`` rejects (never a usable mixed
        pair)."""
        entries = (
            ("trust-state.json", source_path),
            ("trust-state.json.anchor", Path(str(source_path) + ".anchor")),
        )
        temps: list[tuple[Path, Path]] = []
        try:
            for managed_name, src in entries:
                if not src.is_file():
                    raise ConnectorConfigError(
                        f"refusing to install: source state file missing: {src}"
                    )
                tmp = root / f".{managed_name}.stage-tmp"
                data = src.read_bytes()
                with open(tmp, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp, _MANAGED_FILE_MODE)
                temps.append((tmp, root / managed_name))
            # Both temps are durable: publish snapshot first, then anchor. A
            # crash between the renames leaves a mismatched pair that load()
            # rejects — fail closed, never a usable mixed pair.
            for tmp, target in temps:
                os.replace(tmp, target)
                self._fsync_dir(root)
        except OSError as exc:
            raise ConnectorConfigError(f"cannot stage trust state: {exc}") from exc
        finally:
            for tmp, _target in temps:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Best-effort directory fsync after a rename (durability nicety; the
        store's own writes fsync the directory on every save)."""
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
        finally:
            os.close(dir_fd)

    def _chown_tree(self, root: Path, user: str, group: str) -> None:
        """Own the staged managed tree to the dedicated service user/group
        (system mode only). Unknown user/group fails closed."""
        try:
            import grp
            import pwd

            uid = pwd.getpwnam(user).pw_uid
            gid = grp.getgrnam(group).gr_gid
        except (KeyError, ImportError) as exc:
            raise ConnectorConfigError(
                f"service user/group not found: {user}/{group}"
            ) from exc
        for path in (root, *sorted(root.rglob("*"))):
            try:
                os.chown(path, uid, gid)
            except OSError as exc:
                raise ConnectorConfigError(f"cannot chown {path}: {exc}") from exc

    def uninstall(self) -> None:
        self.manager.uninstall(self.config.unit_name)

    def start(self) -> None:
        self.manager.start(self.config.unit_name)

    def stop(self) -> None:
        self.manager.stop(self.config.unit_name)

    def restart(self) -> None:
        self.manager.restart(self.config.unit_name)

    def enable(self) -> None:
        self.manager.enable(self.config.unit_name)

    def disable(self) -> None:
        self.manager.disable(self.config.unit_name)

    def status(self) -> ServiceStatus:
        return self.manager.status(self.config.unit_name)

    def upgrade(self, *, verify_start: bool = True) -> UpgradeOutcome:
        return self.manager.upgrade(
            render_connector_unit(self.unit_spec()), self.config.unit_name, verify_start=verify_start
        )

    def rollback(self) -> UpgradeOutcome:
        return self.manager.rollback(self.config.unit_name)

    # ── the foreground readiness loop (systemd Type=notify) ───────────────

    def run(
        self,
        *,
        max_iterations: int | None = None,
        poll_seconds: float | None = None,
        wait_fn: Callable[[float], None] = time.sleep,
    ) -> int:
        """Readiness-gated foreground loop.

        - a provider-less run configuration (no lab config, no injected
          provider) is REJECTED at startup — fail closed, never READY=1;
        - readiness passes: start the provider (if configured); READY=1 is
          emitted ONLY when the listener actually started (``_start_provider``
          returns proven success) — never on a bind/start failure; subsequent
          passes ping WATCHDOG.
        - readiness fails while the provider is up: stop the listener
          IMMEDIATELY (fail closed) and notify STOPPING.
        - the loop runs until ``max_iterations`` (tests) or a signal-driven
          stop (the CLI installs SIGTERM/SIGINT handlers that call
          ``shutdown()``).
        """
        poll_seconds = poll_seconds if poll_seconds is not None else self.config.poll_seconds
        # A RUNNABLE configuration REQUIRES a concrete listener provider (a lab
        # config, a diy config, or an injected adapter): without one, READY=1
        # could never be backed by a proven bound listener. Reject provider-less
        # run configurations at startup — fail closed, never READY. Non-run
        # construction (status/readiness/diagnose/install) stays valid.
        if (
            self.config.lab is None
            and self.config.diy is None
            and self.config.managed is None
            and self._injected_provider is None
        ):
            raise ConnectorConfigError(
                "refusing to run: no lab/diy/managed provider configured — the connector "
                "has no listener to bring up, so READY would be false"
            )
        iterations = 0
        while True:
            iterations += 1
            if max_iterations is not None and iterations > max_iterations:
                break
            with self._lifecycle_lock:
                if self._shutdown:
                    break
                report = self.readiness_report()
                if report.ready:
                    if not self._provider_running:
                        if self._start_provider():
                            self._notify_fn("READY=1\n")
                        # else: no listener — STATUS already emitted; never READY
                    else:
                        self._notify_fn("WATCHDOG=1\n")
                else:
                    if self._provider_running:
                        self._stop_provider()
                        self._notify_fn("STOPPING=1\n")
                    else:
                        self._notify_fn("STATUS=waiting for readiness\n")
            self._reconcile_revocation()
            wait_fn(poll_seconds)
        return 0

    def _reconcile_revocation(self) -> None:
        """Cross-process revocation closure at the REAL shipping seam
        (TASK-6039 reviewer [CRITICAL] finding 2 / TASK-6044 reviewer [HIGH]
        finding 2): the operator's ``revoke``/``remove-device`` CLI runs in a
        SEPARATE process and persists the revocation epoch; the connector
        process closes its live streams on the next loop pass (bounded by
        ``poll_seconds``, fail closed) and then ROTATES the authoritative
        live-stream runtime.

        Rotation is mandatory because the stream registry is ONE-SHOT:
        ``close_all`` seals it permanently, and the pairing manager, the ctx
        factory, and the provider adapter all capture it. Without rotation a
        reconciled revocation would leave the shipping process permanently
        sealed — a re-paired or unaffected current device could never open a
        new stream (TASK-6044 finding 2). ``_rotate_runtime`` drops the
        listener FIRST (no request/stream is admitted during the handoff),
        clears the cached registry + pairing manager, rebuilds the provider
        and every captured ctx-factory reference, and restarts the listener
        at a readiness-gated fail-closed boundary."""
        with self._lifecycle_lock:
            self._reconcile_revocation_locked()

    def _reconcile_revocation_locked(self) -> None:
        if (
            self.config.diy is None
            and self.config.lab is None
            and self.config.managed is None
        ):
            return
        try:
            state = self.state_store.load()
        except StateStoreError:
            # Fail closed: the readiness gate already stops the listener on
            # unusable state; nothing to close here.
            return
        if state.revocation_epoch <= self._reconciled_revocation_epoch:
            return
        registry = self.registry
        sealed = registry.sealed
        open_count = registry.open_count()
        needs_rotation = sealed or open_count > 0
        was_running = self._provider_running
        # Persisted revocation removes external admission first.  Even when a
        # provider stop reports failure, the subsequent registry seal denies
        # stale authorization; retaining the provider lets the next pass retry
        # listener cleanup instead of losing the only cleanup handle.
        provider_stopped = (
            not needs_rotation or not was_running or self._stop_provider()
        )
        if open_count > 0:
            try:
                registry.close_all()
            except StreamCloseError:
                pass  # registry sealed; no live stream survives (fail closed)
            sealed = True
        if not provider_stopped:
            return
        self._reconciled_revocation_epoch = state.revocation_epoch
        # The authoritative registry is (now or already) sealed — one-shot:
        # rotate the whole runtime so the process is never permanently
        # sealed. When nothing was sealed (revoke with no live streams and
        # no earlier seal) there is no rotation and therefore no listener
        # gap — the common CLI-revoke-while-idle path stays seamless.
        if sealed:
            self._rotate_runtime(restart_provider=was_running)

    def _rotate_runtime(self, *, restart_provider: bool | None = None) -> None:
        """Rotate the authoritative live-stream runtime at a fail-closed
        lifecycle boundary after a persisted revocation: stop the provider
        listener (no request/stream is admitted during the handoff), drop
        the cached one-shot registry and pairing manager, rebuild the
        provider adapter and every captured ctx-factory reference against a
        FRESH registry, and restart the listener only when readiness still
        passes (else the run loop brings it up when ready). The old registry
        is sealed (denies) and the listener is down (refuses) until the new
        runtime is live — nothing is admitted against a half-rotated state."""
        was_running = self._provider_running if restart_provider is None else restart_provider
        if was_running:
            self._stop_provider()
        # The one-shot registry and the cached ceremony manager are captured
        # by the old ctx factory and provider: rebuild them all from scratch.
        self._registry = None
        self._pairing_manager = None
        if was_running and self.readiness_report().ready:
            # Rebuild the provider (fresh registry + pairing manager + ctx
            # factory) and reopen the listener. On any failure the loop
            # re-evaluates readiness and retries — fail closed, no READY.
            self._start_provider()

    def shutdown(self) -> None:
        """Deterministic one-shot cleanup: listener, flows, then runtime."""
        with self._lifecycle_lock:
            first_shutdown = not self._shutdown
            self._shutdown = True
            if first_shutdown and self._provider_running:
                self._stop_provider()
            if first_shutdown and self._registry is not None:
                try:
                    self._registry.close_all()
                except StreamCloseError:
                    pass  # sealed fail closed; shutdown still drops runtime residue
                self._registry = None
                self._pairing_manager = None
            self._notify_fn("STOPPING=1\n")

    def _start_provider(self) -> bool:
        """Start the provider and return PROVEN success: True only when the
        listener is actually running (``_provider_running`` set). A bind/start
        failure returns False — the caller must never emit READY."""
        with self._lifecycle_lock:
            if self._shutdown:
                return False
            if self._provider_running:
                return True
            return self._start_provider_locked()

    def _start_provider_locked(self) -> bool:
        if self._injected_provider is not None:
            provider = self._injected_provider
        elif self.config.diy is not None:
            provider = self.build_diy_provider()
        elif self.config.managed is not None:
            provider = self.build_managed_provider()
        else:
            provider = self.build_provider()
        if provider is None:
            # Provider-less run is rejected at startup; belt-and-braces: never
            # report proven success (READY) without a listener.
            self._notify_fn("STATUS=no provider configured; no listener\n")
            return False
        try:
            provider.start()
        except (LabProviderError, DiyProviderError):
            try:
                provider.stop()
            except Exception:  # noqa: BLE001 — best-effort failed-start cleanup
                pass
            self._provider = None
            self._provider_running = False
            self._registry = None
            self._pairing_manager = None
            # fail-closed: readiness passed but the provider refused to bind
            # (e.g. bind conflict) — keep the loop re-evaluating; no listener.
            self._notify_fn("STATUS=provider failed to start; no listener\n")
            return False
        self._provider = provider
        self._provider_running = True
        return True

    def _stop_provider(self) -> bool:
        with self._lifecycle_lock:
            return self._stop_provider_locked()

    def _stop_provider_locked(self) -> bool:
        provider = self._provider
        if provider is None:
            self._provider_running = False
            return True
        try:
            provider.stop()
        except Exception:  # noqa: BLE001 — retain the cleanup handle for retry
            return False
        self._provider_running = False
        self._provider = None  # drop: a stopped adapter cannot be restarted
        return True
