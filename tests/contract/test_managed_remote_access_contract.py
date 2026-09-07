"""Semantic validator for the managed remote-access normative contract fixtures.

Merge unit A (THR-097, TASK-5771): contract-only. These tests validate the
non-production fixtures under ``tests/contract/managed_remote_access/`` against
the normative contract *encoded in this test module*. They are semantic
validators, not tautological snapshots: the normative invariants (locked
connector decision order, failure/audit category taxonomy, credential classes,
threat-matrix coverage, sentinel-credential hygiene) are hard-coded here and the
fixtures must encode them. A fixture that merely mirrors itself cannot pass.

The fixtures describe *required future behavior*. No production Python, Swift,
Go, or web behavior is read, changed, or asserted here beyond the read-only
route snapshots (``tests/contract/openapi.json`` and
``tests/contract/route-classification.json``) used to prove the allow-list is
derived from the real daemon route inventory and never admits a
forbidden/agent-only route.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

CONTRACT_DIR = Path(__file__).parent / "managed_remote_access"

# Read-only route snapshots (the same files the web coverage test and the Swift
# drift-guard read). We never modify them; we prove the fixture allow-list is
# consistent with the daemon route inventory and never admits a forbidden route.
OPENAPI_SNAPSHOT_PATH = Path(__file__).parent / "openapi.json"
ROUTE_CLASSIFICATION_PATH = Path(__file__).parent / "route-classification.json"

FIXTURE_FILES = {
    "managed_topology": CONTRACT_DIR / "managed-topology.json",
    "route_policy": CONTRACT_DIR / "route-policy.json",
    "credential_taxonomy": CONTRACT_DIR / "credential-taxonomy.json",
    "failure_categories": CONTRACT_DIR / "failure-categories.json",
    "threat_cases": CONTRACT_DIR / "threat-cases.json",
    "lifecycle_matrix": CONTRACT_DIR / "lifecycle-matrix.json",
}

# Extra required top-level keys beyond the common (version/name/status/description)
# set, per fixture.
_TOP_LEVEL_EXTRA = {
    "managed_topology": ["transport", "endpoints", "sidecar_boundary", "connector_ingress", "readiness", "secrets", "visibility", "fallbacks", "n2_lifecycle_matrix", "n3_lifecycle_matrix", "n3_acceptance_harness", "acceptance_matrix", "delivery_status"],
    "route_policy": [
        "decision_order",
        "default_behavior",
        "normalization",
        "header_stripping",
        "upgrade_semantics",
        "forbidden_classes",
        "allow",
    ],
    "credential_taxonomy": ["classes"],
    "failure_categories": ["deny_categories", "audit_categories", "existence_guard"],
    "threat_cases": ["cases"],
}

NORMATIVE_MANAGED_ACCEPTANCE_ROWS = [
    "same_wifi_direct", "same_wifi_forced_private_derp",
    "separate_network_direct_when_possible", "separate_network_forced_private_derp",
    "failure_recovery",
]

# ---------------------------------------------------------------------------
# Normative contract encoded in this test module.
# ---------------------------------------------------------------------------

# The locked connector request decision order. Step N must appear at index N.
NORMATIVE_DECISION_ORDER = [
    "authenticate",  # establish authenticated connector/device context
    "bind",          # verify tenant/home/device/cell binding and current pairing
    "proof",         # validate non-expired/non-replayed proof
    "policy",        # require present, well-formed, current policy/revocation state
    "normalize",     # parse and normalize method/path exactly once; deny ambiguity
    "allowlist",     # match normalized method+path against the explicit allow-list
    "strip",         # strip all remote auth/forwarding/hop-by-hop credentials
    "bearer",        # only now read the local daemon bearer; forward to 127.0.0.1
    "redact",        # redact/categorize the outcome
]

# Ordered pairs that must be preserved (earlier step strictly before later).
DECISION_ORDER_BEFORE: list[tuple[str, str]] = [
    ("normalize", "allowlist"),
    ("strip", "bearer"),
]

# Credential classes mandated by the architecture (TASK-5724 §6).
REQUIRED_CREDENTIAL_CLASSES = [
    "account_session",
    "one_use_enrollment",
    "node_device",
    "connector_device_proof",
    "pairing_authorization",
    "policy_revocation_material",
    "local_daemon_bearer",
]

CREDENTIAL_CLASS_FIELDS = {
    "id": str,
    "name": str,
    "issuer": str,
    "subject_audience": str,
    "storage_custodian": str,
    "lifetime": str,
    "single_use": bool,
    "replay_rules": str,
    "rotation": str,
    "revocation": str,
    "forbidden_exposure": list,
    "network_presentation": str,
    "example_placeholder": str,
}

# Deny/failure categories mandated by the brief: identity, enrollment,
# cell/policy/map/peer, pairing/current-device/revocation, replay/expiry,
# route/method/normalization, relay/direct transport, local-daemon availability,
# and internal/unknown.
REQUIRED_DENY_CATEGORIES = [
    "identity",
    "enrollment",
    "cell",
    "policy",
    "map",
    "peer",
    "pairing",
    "current_device",
    "revocation",
    "replay",
    "expiry",
    "route",
    "method",
    "normalization",
    "relay",
    "direct",
    "transport",
    "local_daemon",
    "internal",
    "unknown",
]

# Forbidden route/method classes that must NEVER appear in the remote allow-list.
REQUIRED_FORBIDDEN_CLASSES = [
    "auth_bootstrap_registration",
    "agent_callbacks",
    "management",
    "founder_as_founder",
    "memory_learning_writes",
    "artifact_upload_agent_only",
    "adapter_administration",
    "executor_registration",
    "portability_reconciliation",
    "unclassified_default_deny",
]

# Threat-matrix coverage mandated by the brief (one case per key minimum).
REQUIRED_THREAT_CATEGORIES = [
    # tenant-A-vs-B hostile negatives
    "wrong_cell_enrollment",
    "wrong_cell_redeem",
    "cross_cell_node_reuse",
    "cross_cell_key_reuse",
    "cross_cell_account_reuse",
    "cross_cell_home_reuse",
    "cross_cell_device_reuse",
    "peer_absent",
    "map_absent",
    "direct_path_denied",
    "forced_derp_denied",
    "derp_cannot_bypass_headscale_policy",
    "forged_tags",
    "forged_routes",
    "forged_subnet_advertisements",
    "forged_exit_node",
    "forged_ssh",
    "client_to_client",
    "home_to_client",
    "non_connector_port",
    # policy states and compiler/apply failure
    "policy_empty",
    "policy_malformed",
    "policy_stale",
    "policy_future",
    "policy_rollback",
    "policy_compile_failed",
    "policy_apply_failed",
    # device/pairing/revocation
    "current_device_mismatch",
    "pairing_mismatch",
    "revoked_before_request",
    "revoked_mid_stream",
    # credential states
    "expired_credential",
    "replayed_credential",
    "reused_credential",
    "wrong_audience_credential",
    "wrong_home_credential",
    # path normalization
    "encoded_path",
    "traversal_path",
    "ambiguous_path",
    # route/method policy
    "forbidden_route",
    "forbidden_method",
    "unclassified_route",
    # upgrade/header smuggling semantics
    "unsupported_upgrade",
    "unsupported_body",
    "smuggling_headers",
    "duplicate_critical_headers",
    # local daemon boundary and internal/unknown
    "daemon_bearer_in_remote_input",
    "daemon_unavailable",
    "daemon_bind_mismatch",
    "internal_error_redacted",
]

# Positive-control categories that must each have at least one case.
REQUIRED_POSITIVE_CONTROL_CATEGORIES = [
    "positive_control_allowed_http",
    "positive_control_allowed_sse",
]

VALID_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

VALID_POLICY_STATES = {
    "current",
    "empty",
    "malformed",
    "stale",
    "future",
    "rollback",
    "compiler_failed",
    "apply_failed",
    "missing",
}

VALID_TRANSPORTS = {"direct", "derp", "any", "none"}

# Values that carry real credential material must use obvious non-secret
# placeholders of this shape.
PLACEHOLDER_RE = re.compile(r"^PLACEHOLDER_[A-Z][A-Z0-9_]*$")

# High-confidence sentinel credential shapes. These must never occur anywhere in
# the serialized fixtures (values, expected audit fields, failure details).
SENTINEL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("legacy pairing bearer prefix (hrpair_)", re.compile(r"hrpair_[A-Za-z0-9._-]+")),
    ("daemon registration-token prefix (hrreg_)", re.compile(r"hrreg_[A-Za-z0-9._-]+")),
    ("HTTP Bearer value", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("PEM private key block", re.compile(r"-----BEGIN [A-Z0-9 ]+PRIVATE KEY-----")),
    ("Stripe-style secret key", re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{8,}")),
    ("GitHub fine-grained PAT", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
]

# Raw-exception markers that must never appear in failure details or audit text.
RAW_EXCEPTION_MARKERS = [
    "Traceback",
    "Error:",
    "Exception",
    " at 0x",
    'File "',
    r"line \d+",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(name: str) -> dict:
    path = FIXTURE_FILES[name]
    assert path.is_file(), f"missing fixture {path.relative_to(CONTRACT_DIR.parent)}"
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_exact_keys(
    obj: dict, required: list[str], context: str, optional: list[str] | None = None
) -> None:
    allowed = set(required) | set(optional or [])
    unknown = sorted(set(obj) - allowed)
    missing = sorted(set(required) - set(obj))
    assert not unknown, f"{context}: unknown fields {unknown}"
    assert not missing, f"{context}: missing required fields {missing}"


def _scan_sentinels(text: str) -> list[tuple[str, str]]:
    """Return [(sentinel-class, matched-value), ...] found in ``text``."""
    hits: list[tuple[str, str]] = []
    for label, pattern in SENTINEL_PATTERNS:
        for match in pattern.finditer(text):
            hits.append((label, match.group(0)))
    return hits


def _format_sentinel_hits(fixture_name: str, hits: list[tuple[str, str]]) -> str:
    """Format sentinel hits WITHOUT echoing any matched value.

    Only the fixture name, the sentinel class label, and a count are reported,
    so a violation can never leak the offending credential into an error
    message (this is itself part of the redaction contract).
    """
    counts: dict[str, int] = {}
    for label, _ in hits:
        counts[label] = counts.get(label, 0) + 1
    lines = [f"fixture {fixture_name}: sentinel credential shapes detected:"]
    lines += [f"  - {label} x{n}" for label, n in sorted(counts.items())]
    return "\n".join(lines)


def _route_set() -> set[str]:
    """All documented daemon routes as 'METHOD /path' (from openapi.json)."""
    schema = json.loads(OPENAPI_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    out: set[str] = set()
    for path, methods in schema.get("paths", {}).items():
        for method in methods:
            out.add(f"{method.upper()} {path}")
    return out


def _classification() -> tuple[set[str], set[str]]:
    """(included, excluded) route sets from route-classification.json."""
    data = json.loads(ROUTE_CLASSIFICATION_PATH.read_text(encoding="utf-8"))
    included = set(data["included"])
    excluded = set(data["excluded"].keys())
    return included, excluded


def _require_fields(obj: dict, required: list[str], context: str) -> None:
    _assert_exact_keys(obj, required, context)
    for key in required:
        assert obj[key] not in (None, ""), f"{context}: field {key!r} must be present"


# ---------------------------------------------------------------------------
# 1. Fixture presence and well-formedness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(FIXTURE_FILES))
def test_fixture_present_and_parses(name: str) -> None:
    path = FIXTURE_FILES[name]
    assert path.is_file(), f"missing fixture: {path.relative_to(CONTRACT_DIR.parent)}"
    raw = path.read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert isinstance(doc, dict), f"{name}: fixture must be a JSON object"
    _assert_exact_keys(
        doc,
        ["version", "name", "status", "description", *_TOP_LEVEL_EXTRA[name]],
        name,
    )
    assert doc["version"], f"{name}: version must be non-empty"
    assert doc["status"] == "normative-contract", f"{name}: status must be 'normative-contract'"


def test_managed_embedded_topology_is_load_bearing() -> None:
    doc = _load("managed_topology")
    assert doc["transport"] == "embedded"
    assert doc["endpoints"] == {"mac": "embedded_tsnet_userspace_wireguard", "linux_tailnet": "happyranch_packaged_embedded_network_sidecar_core_build_test", "linux_connector_bind": "127.0.0.1_only", "daemon_bind": "127.0.0.1:8765_only"}
    assert doc["sidecar_boundary"] == {"tailnet_listener": True, "proxy_protocol": "raw_tcp_only", "fixed_target": "loopback_python_connector_only", "authorization_authority": False, "daemon_bearer_access": False, "happy_ranch_route_parsing": False}
    assert doc["connector_ingress"] == {"mode": "managed", "bind_host": "127.0.0.1_literal_only", "reuses_gateway_pipeline": True, "application_authority": "connector_only", "sidecar_bypass": False}
    assert doc["readiness"] == {"mode": "conjunctive_composite", "required_gates": ["configuration_valid", "encrypted_engine_started", "private_control_plane_joined", "expected_peer_map_visible", "tailnet_listener_active", "loopback_connector_reachable"], "failure_order": "remove_tailnet_listener_first", "early_ready_forbidden": True}


def test_managed_embedded_topology_secrets_and_observability_are_bounded() -> None:
    doc = _load("managed_topology")
    assert doc["secrets"] == {"enrollment_input": "owner_only_one_use_credential_file", "command_line_forbidden": True, "logging_forbidden": True, "delete_after_durable_redemption": True, "durable_transport_state_owner_only": True, "errors": "category_only", "daemon_bearer_hop": "connector_to_127.0.0.1:8765_only"}
    assert doc["visibility"] == {"headscale_derp_may_observe": ["operational_metadata", "wireguard_ciphertext"], "headscale_derp_must_never_observe": ["http_plaintext", "sse_plaintext", "websocket_plaintext", "happy_ranch_secrets"]}
    assert doc["fallbacks"] == {"wildcard_listener": False, "lan_or_plaintext_concrete_address": False, "same_wifi_direct_lan_plaintext": False, "public_tailscale": False, "public_derp": False}


def test_managed_acceptance_matrix_has_no_external_or_plaintext_escape() -> None:
    rows = _load("managed_topology")["acceptance_matrix"]
    assert [row["id"] for row in rows] == NORMATIVE_MANAGED_ACCEPTANCE_ROWS
    required = {"no_system_tailscale_either_endpoint", "loopback_only_daemon_and_connector", "ciphertext_only_control_and_relay", "no_public_or_lan_fallback"}
    for row in rows:
        _assert_exact_keys(row, ["id", "path", "required_invariants"], "acceptance_matrix[]")
        assert set(row["required_invariants"]) == required


def test_n2_lifecycle_matrix_covers_shipping_boundaries() -> None:
    rows = _load("managed_topology")["n2_lifecycle_matrix"]
    expected = {
        "startup": ("invalid_or_unready_before_bind", "no_listener_no_residue"),
        "admission": ("readiness_then_loopback_bind", "full_gateway_pipeline_only"),
        "active_flow": ("authorize_then_final_local_hop", "bearer_final_hop_only"),
        "readiness_loss": ("listener_stop_before_downstream_cleanup", "no_new_admission"),
        "revocation": ("listener_stop_then_active_flow_close_then_downstream_cleanup", "no_stale_authorization_or_residue"),
        "shutdown": ("listener_stop_then_active_flow_close_then_runtime_cleanup", "repeated_shutdown_exactly_once_no_residue"),
        "partial_failure": ("admission_removed_before_cleanup_retry", "category_only_fail_closed"),
        "concurrency_reentry": ("provider_start_races_shutdown_or_persisted_revocation", "linearized_both_orderings_no_deadlock_or_double_close"),
        "recovery": ("fresh_gates_after_cleanup_before_fresh_listener", "current_identity_epoch_policy_only"),
    }
    required_observable_mappings = {
        "startup": {
            "tests/remote_access/test_supervisor.py::TestRunLoop::test_not_ready_never_starts_provider",
            "tests/remote_access/test_supervisor.py::TestRunLoop::test_start_failure_cleans_provider_registry_and_runtime_residue",
        },
        "admission": {
            "tests/remote_access/test_managed_provider.py::test_readiness_failure_never_creates_listener_and_is_redacted",
        },
        "active_flow": {
            "tests/remote_access/test_managed_provider.py::test_complete_gateway_pipeline_and_final_hop_bearer_placement",
        },
        "readiness_loss": {
            "tests/remote_access/test_supervisor.py::TestRunLoop::test_readiness_loss_stops_listener_immediately",
        },
        "revocation": {
            "tests/remote_access/test_supervisor.py::TestReconciliationRotation::test_persisted_revocation_removes_listener_before_stream_cleanup",
            "tests/remote_access/test_supervisor.py::TestReconciliationRotation::test_cross_process_revoke_closes_stream_then_rotation_reopens_repair",
        },
        "shutdown": {
            "tests/remote_access/test_supervisor.py::TestRunLoop::test_repeated_shutdown_closes_active_shipping_runtime_exactly_once",
        },
        "partial_failure": {
            "tests/remote_access/test_streams.py::test_close_all_partial_multi_handle_failure_seals_all",
            "tests/remote_access/test_supervisor.py::TestReconciliationRotation::test_persisted_revocation_retries_failed_listener_stop_without_double_close",
            "tests/remote_access/test_supervisor.py::TestRunLoop::test_start_failure_cleans_provider_registry_and_runtime_residue",
        },
        "concurrency_reentry": {
            "tests/remote_access/test_supervisor.py::TestRunLoop::test_concurrent_provider_start_then_shutdown_is_linearized",
            "tests/remote_access/test_supervisor.py::TestReconciliationRotation::test_provider_start_racing_persisted_revocation_is_linearized_in_both_orderings",
        },
        "recovery": {
            "tests/remote_access/test_managed_provider.py::test_stop_is_idempotent_and_recovery_requires_fresh_readiness",
            "tests/remote_access/test_supervisor.py::TestReconciliationRotation::test_targeted_revoke_rotation_unaffected_device_still_opens_and_ws_denied",
        },
    }
    assert [row["phase"] for row in rows] == list(expected)
    for row in rows:
        _assert_exact_keys(row, ["phase", "ordering", "outcome", "shipping_tests"], "n2_lifecycle_matrix[]")
        assert (row["ordering"], row["outcome"]) == expected[row["phase"]]
        assert row["shipping_tests"], f"{row['phase']}: shipping test mapping required"
        assert all(test.startswith("tests/remote_access/") and "::test_" in test for test in row["shipping_tests"])
        assert set(row["shipping_tests"]) == required_observable_mappings[row["phase"]], (
            f"{row['phase']}: exact observable production-seam mappings required"
        )


def test_managed_delivery_status_preserves_diy_and_gates_future_units() -> None:
    assert _load("managed_topology")["delivery_status"] == {"n0": "contract_implemented", "n1": "sidecar_core_build_test_only", "n2": "managed_loopback_connector_ingress_implemented", "n3": "linux_package_and_composite_supervision_implemented_no_deployment_or_acceptance", "n4_through_n6": "founder_gated_separate_units", "unit_4b_2": "delivered_independent_not_network_provisioning_or_acceptance", "supported_diy": "unchanged_truthful_voluntary_external_tailscale_or_customer_headscale", "supported_diy_for_founder_acceptance": "non_executable_not_managed_path"}


_TOP_LEVEL_EXTRA = {
    "managed_topology": ["transport", "endpoints", "sidecar_boundary", "connector_ingress", "readiness", "secrets", "visibility", "fallbacks", "n2_lifecycle_matrix", "n3_lifecycle_matrix", "n3_acceptance_harness", "acceptance_matrix", "delivery_status"],
    "route_policy": [
        "decision_order",
        "default_behavior",
        "normalization",
        "header_stripping",
        "upgrade_semantics",
        "forbidden_classes",
        "allow",
    ],
    "credential_taxonomy": ["classes"],
    "failure_categories": ["deny_categories", "audit_categories", "existence_guard"],
    "threat_cases": ["cases"],
    "lifecycle_matrix": ["governing_rule", "boundary", "linearization_points", "mutations", "notes"],
}


# ---------------------------------------------------------------------------
# 2. Route-policy contract
# ---------------------------------------------------------------------------


def test_route_policy_decision_order_is_locked() -> None:
    doc = _load("route_policy")
    assert doc["decision_order"] == NORMATIVE_DECISION_ORDER, (
        "decision_order must be exactly the normative locked order "
        f"{NORMATIVE_DECISION_ORDER}; found {doc['decision_order']}"
    )
    for earlier, later in DECISION_ORDER_BEFORE:
        assert doc["decision_order"].index(earlier) < doc["decision_order"].index(later), (
            f"decision order must place {earlier!r} before {later!r}"
        )


def test_route_policy_default_is_deny_unclassified() -> None:
    doc = _load("route_policy")
    assert doc["default_behavior"] == "deny_unclassified", (
        "remote policy must deny unclassified routes by default"
    )


def test_route_policy_normalization_rules_present() -> None:
    doc = _load("route_policy")
    _assert_exact_keys(
        doc["normalization"],
        [
            "normalize_once",
            "percent_encoding",
            "dot_segments",
            "duplicate_slashes",
            "query_separation",
            "unicode_control_bytes",
            "absolute_form_authority",
            "ambiguity_denied",
        ],
        "route_policy.normalization",
    )
    assert doc["normalization"]["normalize_once"] is True, "path must normalize exactly once"
    assert doc["normalization"]["ambiguity_denied"] is True, "ambiguity must be denied"
    for key in (
        "percent_encoding",
        "dot_segments",
        "duplicate_slashes",
        "query_separation",
        "unicode_control_bytes",
        "absolute_form_authority",
    ):
        assert doc["normalization"][key], f"normalization.{key} must be non-empty"


def test_route_policy_header_stripping_and_bearer_injection() -> None:
    doc = _load("route_policy")
    _assert_exact_keys(
        doc["header_stripping"],
        [
            "strip_authorization",
            "strip_forwarding",
            "strip_hop_by_hop",
            "strip_cookies",
            "strip_host",
            "daemon_bearer_injection_hop",
            "inject_only_on_loopback",
            "reject_duplicate_critical_headers",
            "reject_smuggling",
        ],
        "route_policy.header_stripping",
    )
    hs = doc["header_stripping"]
    assert hs["strip_authorization"] is True
    assert hs["strip_forwarding"] is True
    assert hs["strip_hop_by_hop"] is True
    assert hs["strip_cookies"] is True
    assert hs["strip_host"] is True
    assert hs["inject_only_on_loopback"] is True, "bearer must be injected only on the loopback hop"
    assert hs["daemon_bearer_injection_hop"] == "connector_to_127.0.0.1_only"
    assert hs["reject_duplicate_critical_headers"] is True
    assert hs["reject_smuggling"] is True


def test_route_policy_upgrade_semantics() -> None:
    doc = _load("route_policy")
    up = doc["upgrade_semantics"]
    _assert_exact_keys(
        up,
        ["allowed", "sse", "websocket", "unsupported_upgrades_denied", "unsupported_bodies_denied"],
        "route_policy.upgrade_semantics",
    )
    assert set(up["allowed"]) <= {"sse", "websocket"}, "only SSE and WebSocket may be allowed"
    assert up["unsupported_upgrades_denied"] is True
    assert up["unsupported_bodies_denied"] is True
    for stream in ("sse", "websocket"):
        _assert_exact_keys(up[stream], ["allowed_templates"], f"upgrade_semantics.{stream}")
        for tpl in up[stream]["allowed_templates"]:
            method, path = tpl.split(" ", 1)
            assert method == "GET", f"{stream}: upgrades must be GET-only, found {tpl}"
            assert path.startswith("/api/v1/"), f"{stream}: template must be daemon-prefixed: {tpl}"
    # Every upgrade-allowed template must be on the explicit allow-list.
    allow_entries = {f"{e['method']} {e['path_template']}" for e in doc["allow"]}
    for stream in up["allowed"]:
        for tpl in up[stream]["allowed_templates"]:
            assert tpl in allow_entries, (
                f"upgrade_semantics.{stream}: {tpl!r} must also be on the allow-list"
            )


def test_route_policy_forbidden_classes_complete() -> None:
    doc = _load("route_policy")
    classes = doc["forbidden_classes"]
    assert classes, "forbidden_classes must be non-empty"
    seen: set[str] = set()
    for cls in classes:
        _assert_exact_keys(cls, ["id", "description", "examples"], "forbidden_classes[]")
        assert cls["id"] not in seen, f"duplicate forbidden class {cls['id']!r}"
        seen.add(cls["id"])
        assert isinstance(cls["examples"], list) and cls["examples"], (
            f"forbidden class {cls['id']!r} must carry example templates"
        )
    assert set(seen) >= set(REQUIRED_FORBIDDEN_CLASSES), (
        "forbidden classes must cover: "
        + ", ".join(sorted(set(REQUIRED_FORBIDDEN_CLASSES) - seen))
    )


def test_route_policy_allow_entries_wellformed_and_unique() -> None:
    doc = _load("route_policy")
    allow = doc["allow"]
    assert allow, "allow must be non-empty (explicit allow-by-method+template)"
    seen: set[tuple[str, str]] = set()
    for entry in allow:
        _assert_exact_keys(entry, ["method", "path_template"], "allow[]")
        method = entry["method"]
        path = entry["path_template"]
        assert method in VALID_HTTP_METHODS, f"allow[]: invalid method {method!r}"
        assert path.startswith("/api/v1/"), f"allow[]: path must be daemon-prefixed: {path}"
        assert " " not in path and "?" not in path and "#" not in path and "*" not in path, (
            f"allow[]: path_template must be a bare daemon template (no query/fragment/wildcard/space): {path}"
        )
        assert (method, path) not in seen, f"allow[]: duplicate {method} {path}"
        seen.add((method, path))
    assert len(seen) == len(allow)


def test_route_policy_allow_entries_derive_from_daemon_snapshot() -> None:
    """Every allow entry must exist in the daemon route inventory (no stale/typo entries)."""
    doc = _load("route_policy")
    routes = _route_set()
    stale = [f"{e['method']} {e['path_template']}" for e in doc["allow"] if f"{e['method']} {e['path_template']}" not in routes]
    assert not stale, f"allow entries not present in openapi.json: {stale}"


def test_route_policy_never_allows_forbidden_route() -> None:
    """The allow-list must never admit a route the classification marks agent-only."""
    doc = _load("route_policy")
    _, excluded = _classification()
    forbidden = [
        f"{e['method']} {e['path_template']}"
        for e in doc["allow"]
        if f"{e['method']} {e['path_template']}" in excluded
    ]
    assert not forbidden, (
        "allow-list must never admit agent-only/forbidden routes; found: " + ", ".join(forbidden)
    )


def test_route_policy_allow_and_forbidden_classes_do_not_overlap() -> None:
    """An allow entry may not also appear in any forbidden-class example list."""
    doc = _load("route_policy")
    allow_entries = {f"{e['method']} {e['path_template']}" for e in doc["allow"]}
    for cls in doc["forbidden_classes"]:
        for example in cls["examples"]:
            # Examples may carry a method prefix or 'ANY'.
            tokens = example.split(" ", 1)
            if len(tokens) == 2 and tokens[0] in VALID_HTTP_METHODS:
                candidate = example
            else:
                candidate = "ANY " + example
            # Normalize 'ANY' into concrete method comparisons only when exact.
            if candidate in allow_entries:
                pytest.fail(
                    f"allow entry {candidate!r} also listed in forbidden class {cls['id']!r}"
                )


# ---------------------------------------------------------------------------
# 3. Credential taxonomy
# ---------------------------------------------------------------------------


def test_credential_taxonomy_classes_complete() -> None:
    doc = _load("credential_taxonomy")
    classes = doc["classes"]
    assert classes, "credential taxonomy must be non-empty"
    seen: set[str] = set()
    for cls in classes:
        _assert_exact_keys(cls, list(CREDENTIAL_CLASS_FIELDS), "classes[]")
        assert cls["id"] not in seen, f"duplicate credential class {cls['id']!r}"
        seen.add(cls["id"])
    assert set(seen) >= set(REQUIRED_CREDENTIAL_CLASSES), (
        "credential taxonomy must cover: " + ", ".join(sorted(set(REQUIRED_CREDENTIAL_CLASSES) - seen))
    )


def test_credential_taxonomy_placeholders_are_obvious_non_secrets() -> None:
    doc = _load("credential_taxonomy")
    for cls in doc["classes"]:
        placeholder = cls["example_placeholder"]
        assert PLACEHOLDER_RE.match(placeholder), (
            f"class {cls['id']!r}: example_placeholder must be an obvious non-secret "
            f"placeholder of the form PLACEHOLDER_*, found {placeholder!r}"
        )
        assert not _scan_sentinels(placeholder), f"class {cls['id']!r}: placeholder looks secret-like"


def test_credential_taxonomy_daemon_bearer_boundary() -> None:
    """The local daemon bearer class must encode the fixed injection boundary."""
    doc = _load("credential_taxonomy")
    bearer = next(cls for cls in doc["classes"] if cls["id"] == "local_daemon_bearer")
    forbidden = " ".join(bearer["forbidden_exposure"]).lower()
    for surface in ("network", "headscale", "derp", "services", "client", "log", "audit", "fixture"):
        assert surface in forbidden, (
            f"local_daemon_bearer.forbidden_exposure must forbid {surface!r}"
        )
    assert "127.0.0.1" in bearer["storage_custodian"] or "loopback" in bearer["storage_custodian"].lower(), (
        "local_daemon_bearer storage/custodian must state the loopback-only injection boundary"
    )
    assert bearer["single_use"] is False and "rotate" in bearer["rotation"].lower()


def test_credential_taxonomy_transport_rules() -> None:
    """Remotely-presented credentials: encrypted transport to the exact audience only.

    A class normatively returned/presented over authenticated TLS/WireGuard to its
    intended device must not also carry a blanket bare-``network`` prohibition
    (that makes credential locality impossible to implement or validate). Every
    non-bearer class must instead distinguish permitted encrypted transport to
    its exact authenticated audience from forbidden plaintext/unintended network
    exposure; only the local daemon bearer retains the absolute network
    prohibition.
    """
    doc = _load("credential_taxonomy")
    for cls in doc["classes"]:
        cid = cls["id"]
        rule = cls["network_presentation"]
        assert rule, f"class {cid}: network_presentation is required"
        if cid == "local_daemon_bearer":
            low = rule.lower()
            assert "never" in low and "network" in low, (
                f"local_daemon_bearer.network_presentation must state the absolute "
                "never-over-any-network rule"
            )
            continue
        low = rule.lower()
        assert "plaintext" in low, (
            f"class {cid}: network_presentation must forbid plaintext transport"
        )
        assert any(k in low for k in ("tls", "https", "wireguard", "encrypted")), (
            f"class {cid}: network_presentation must permit encrypted transport to "
            "its exact authenticated audience"
        )
        bare = [e for e in cls["forbidden_exposure"] if e.strip().lower() == "network"]
        assert not bare, (
            f"class {cid}: forbidden_exposure must not blanket-forbid 'network'; "
            "permitted encrypted transport to the exact audience is stated in "
            "network_presentation"
        )

# ---------------------------------------------------------------------------
# 4. Failure/audit category taxonomy
# ---------------------------------------------------------------------------


def test_failure_categories_taxonomy_complete() -> None:
    doc = _load("failure_categories")
    deny = doc["deny_categories"]
    assert deny, "deny_categories must be non-empty"
    seen: set[str] = set()
    for cat in deny:
        _assert_exact_keys(cat, ["id", "description"], "deny_categories[]")
        assert cat["id"] not in seen, f"duplicate deny category {cat['id']!r}"
        seen.add(cat["id"])
    assert set(seen) >= set(REQUIRED_DENY_CATEGORIES), (
        "deny category taxonomy must cover: " + ", ".join(sorted(set(REQUIRED_DENY_CATEGORIES) - seen))
    )


def test_failure_categories_audit_categories_are_stable_and_redacted() -> None:
    doc = _load("failure_categories")
    deny = doc["deny_categories"]
    audit = doc["audit_categories"]
    assert audit, "audit_categories must be non-empty"
    seen: set[str] = set()
    for cat in audit:
        _assert_exact_keys(cat, ["id", "description"], "audit_categories[]")
        assert cat["id"] not in seen, f"duplicate audit category {cat['id']!r}"
        seen.add(cat["id"])
        assert re.fullmatch(r"[a-z][a-z0-9_]*", cat["id"]), (
            f"audit category {cat['id']!r} must be stable lowercase snake_case"
        )
        assert not re.search(
            r"(?:tenant|cell|home|device)[-_]?[a-z0-9](?=[_-]|$)", cat["id"], re.IGNORECASE
        ), f"audit category {cat['id']!r} must never embed a tenant/cell/home/device identifier"
    for cat in deny:
        assert not re.search(
            r"(?:tenant|cell|home|device)[-_]?[a-z0-9](?=[_-]|$)", cat["id"], re.IGNORECASE
        ), f"deny category {cat['id']!r} must never embed a tenant/cell/home/device identifier"
        assert cat["id"].isdigit() is False
    assert all(not _scan_sentinels(cat["description"]) for cat in audit), (
        "audit category descriptions must not carry sentinel credential shapes"
    )


def test_failure_categories_existence_guard_declared() -> None:
    """Audit output must not leak cross-tenant existence."""
    doc = _load("failure_categories")
    guard = doc["existence_guard"]
    _assert_exact_keys(guard, ["enforced", "rule"], "existence_guard")
    assert guard["enforced"] is True
    assert guard["rule"], "existence_guard.rule must state the no-oracle requirement"


# ---------------------------------------------------------------------------
# 5. Threat cases
# ---------------------------------------------------------------------------


def test_threat_cases_schema() -> None:
    doc = _load("threat_cases")
    cases = doc["cases"]
    assert cases, "threat cases must be non-empty"
    for case in cases:
        cid = case.get("id", "<no id>")
        _assert_exact_keys(
            case,
            ["id", "class", "category", "scenario", "actor", "target", "inputs", "expected"],
            f"cases[{cid}]",
            optional=["existence_pair"],
        )
        assert case["class"] in {"positive_control", "hostile"}, f"case {cid}: bad class"
        _assert_exact_keys(
            case["inputs"],
            ["method", "path", "policy_state", "transport", "credential_placeholder"],
            f"cases[{cid}].inputs",
        )
        inputs = case["inputs"]
        assert inputs["method"] in VALID_HTTP_METHODS or inputs["method"] == "UPGRADE", (
            f"case {cid}: invalid method {inputs['method']!r}"
        )
        assert inputs["path"], f"case {cid}: path required"
        assert inputs["policy_state"] in VALID_POLICY_STATES, (
            f"case {cid}: invalid policy_state {inputs['policy_state']!r}"
        )
        assert inputs["transport"] in VALID_TRANSPORTS, (
            f"case {cid}: invalid transport {inputs['transport']!r}"
        )
        cp = inputs["credential_placeholder"]
        assert cp is None or PLACEHOLDER_RE.match(cp), (
            f"case {cid}: credential_placeholder must be null or PLACEHOLDER_*, found {cp!r}"
        )
        _assert_exact_keys(
            case["expected"],
            ["outcome", "audit_category", "failure_detail", "proves"],
            f"cases[{cid}].expected",
            optional=["deny_category"],
        )
        expected = case["expected"]
        assert expected["outcome"] in {"allowed", "denied"}, f"case {cid}: bad outcome"
        if expected["outcome"] == "allowed":
            assert "deny_category" not in expected, f"case {cid}: allowed case must omit deny_category"
            assert expected["audit_category"] == "allowed_request", (
                f"case {cid}: allowed case must audit as allowed_request"
            )
        else:
            assert "deny_category" in expected, f"case {cid}: denied case must carry deny_category"


def _assert_class_outcome_consistency(cases: list[dict], context: str) -> None:
    """Hostile cases must be denied with a deny_category; positive controls allowed.

    A hostile tenant-crossing/replay/revocation/forbidden-route/DERP-bypass case
    rewritten as allowed must never pass, even when its required category label
    still satisfies coverage. This is the finding-2 invariant (TASK-5779).
    """
    for case in cases:
        cid = case["id"]
        expected = case["expected"]
        if case["class"] == "hostile":
            assert expected["outcome"] == "denied", (
                f"{context}: hostile case {cid} must be denied, not allowed"
            )
            assert "deny_category" in expected, (
                f"{context}: hostile case {cid} must carry a deny_category"
            )
        else:
            assert expected["outcome"] == "allowed", (
                f"{context}: positive control {cid} must be allowed, not denied"
            )
            assert "deny_category" not in expected, (
                f"{context}: positive control {cid} must omit deny_category"
            )
            assert expected["audit_category"] == "allowed_request", (
                f"{context}: positive control {cid} must audit as allowed_request"
            )


def test_threat_case_class_outcome_consistency() -> None:
    """Every hostile case is denied with a deny_category; positives are allowed."""
    doc = _load("threat_cases")
    _assert_class_outcome_consistency(doc["cases"], "threat_cases")


def test_mutation_hostile_to_allowed_is_rejected() -> None:
    """Checked-in mutation proof: hostile->allowed must never pass validation.

    Adversarial regression guard: flip a hostile case to outcome=allowed,
    audit_category=allowed_request, and drop its deny_category. Its category
    label still satisfies the coverage matrix, so only the class<->outcome
    invariant can reject it — and it must.
    """
    doc = json.loads(FIXTURE_FILES["threat_cases"].read_text(encoding="utf-8"))
    hostile = [c for c in doc["cases"] if c["class"] == "hostile"]
    assert hostile, "precondition: fixture must contain hostile cases"
    target = next(c for c in hostile if "existence_pair" not in c)
    target["expected"]["outcome"] = "allowed"
    target["expected"]["audit_category"] = "allowed_request"
    target["expected"].pop("deny_category", None)
    with pytest.raises(AssertionError, match="must be denied"):
        _assert_class_outcome_consistency(doc["cases"], "threat_cases")


def test_threat_cases_unique() -> None:
    doc = _load("threat_cases")
    ids: set[str] = set()
    signatures: set[tuple] = set()
    for case in doc["cases"]:
        cid = case["id"]
        assert cid not in ids, f"duplicate threat case id {cid!r}"
        ids.add(cid)
        sig = (
            case["class"],
            case["category"],
            case["actor"],
            case["target"],
            case["inputs"]["method"],
            case["inputs"]["path"],
        )
        assert sig not in signatures, f"ambiguous duplicate threat case: {sig}"
        signatures.add(sig)


def test_threat_cases_categories_valid() -> None:
    doc = _load("threat_cases")
    fail = _load("failure_categories")
    deny_ids = {c["id"] for c in fail["deny_categories"]}
    audit_ids = {c["id"] for c in fail["audit_categories"]}
    for case in doc["cases"]:
        cid = case["id"]
        expected = case["expected"]
        assert expected["audit_category"] in audit_ids, (
            f"case {cid}: audit_category {expected['audit_category']!r} not in taxonomy"
        )
        if "deny_category" in expected:
            assert expected["deny_category"] in deny_ids, (
                f"case {cid}: deny_category {expected['deny_category']!r} not in taxonomy"
            )


def test_threat_matrix_coverage_complete() -> None:
    """The hostile matrix must cover every mandated scenario class; positives too."""
    doc = _load("threat_cases")
    hostile = {c["category"] for c in doc["cases"] if c["class"] == "hostile"}
    positive = {c["category"] for c in doc["cases"] if c["class"] == "positive_control"}
    missing_hostile = sorted(set(REQUIRED_THREAT_CATEGORIES) - hostile)
    assert not missing_hostile, (
        "threat matrix incomplete; missing hostile categories: " + ", ".join(missing_hostile)
    )
    missing_positive = sorted(set(REQUIRED_POSITIVE_CONTROL_CATEGORIES) - positive)
    assert not missing_positive, (
        "threat matrix must include positive controls for: " + ", ".join(missing_positive)
    )


def test_threat_cases_failure_details_redacted() -> None:
    """Failure details must be stable category-level prose: no raw exceptions, no secrets."""
    doc = _load("threat_cases")
    for case in doc["cases"]:
        cid = case["id"]
        detail = case["expected"]["failure_detail"]
        assert detail, f"case {cid}: failure_detail required"
        assert len(detail) <= 500, f"case {cid}: failure_detail too long"
        for marker in RAW_EXCEPTION_MARKERS:
            assert re.search(marker, detail) is None, (
                f"case {cid}: failure_detail contains raw-exception marker {marker!r}"
            )
        hits = _scan_sentinels(detail)
        assert not hits, f"case {cid}: failure_detail contains sentinel shapes: {_format_sentinel_hits(cid, hits)}"
        # Redacted details must be tenant-neutral: no concrete tenant/cell/home
        # identifiers (the pairing-specific tokens that would leak existence).
        assert not re.search(
            r"\b(?:tenant|cell|home|device)[-_ ]?[ab]\b", detail, re.IGNORECASE
        ), f"case {cid}: failure_detail must not name concrete tenants/cells/homes"


def test_existence_guard_pairs_share_identical_categories() -> None:
    """Paired cases (target exists vs target absent) must produce identical categories."""
    doc = _load("threat_cases")
    by_pair: dict[str, list[dict]] = {}
    for case in doc["cases"]:
        pair = case.get("existence_pair")
        if pair:
            by_pair.setdefault(pair, []).append(case)
    assert by_pair, "at least one existence-guard pair is required"
    for pair, members in by_pair.items():
        assert len(members) == 2, f"existence pair {pair!r} must have exactly two cases"
        a, b = members
        assert a["expected"]["outcome"] == b["expected"]["outcome"] == "denied"
        assert a["expected"].get("deny_category") == b["expected"].get("deny_category"), (
            f"pair {pair!r}: deny categories must be identical (no existence oracle)"
        )
        assert a["expected"]["audit_category"] == b["expected"]["audit_category"], (
            f"pair {pair!r}: audit categories must be identical (no existence oracle)"
        )
        assert a["class"] == b["class"] == "hostile"


def test_absent_vs_consumed_credential_pair_identical_visible_outcome() -> None:
    """The absent-vs-consumed credential pair is externally indistinguishable.

    A single-use enrollment credential that was already consumed/reused and one
    that never existed must produce identical deny category, audit category, and
    the exact same visible failure detail — otherwise the detail is a
    credential-existence oracle (finding 1, TASK-5779).
    """
    doc = _load("threat_cases")
    by_pair: dict[str, list[dict]] = {}
    for case in doc["cases"]:
        pair = case.get("existence_pair")
        if pair:
            by_pair.setdefault(pair, []).append(case)
    cred_pairs = [
        members
        for members in by_pair.values()
        if any(m["expected"]["audit_category"] == "credential_reused" for m in members)
    ]
    assert cred_pairs, (
        "an absent-vs-consumed/replayed existence pair (audit credential_reused) "
        "is required"
    )
    for members in cred_pairs:
        assert len(members) == 2, "absent-vs-consumed pair must have exactly two cases"
        a, b = members
        assert a["expected"]["outcome"] == b["expected"]["outcome"] == "denied"
        assert a["expected"].get("deny_category") == b["expected"].get("deny_category")
        assert a["expected"]["audit_category"] == b["expected"]["audit_category"]
        assert a["expected"]["failure_detail"] == b["expected"]["failure_detail"], (
            f"absent-vs-consumed pair {a['id']}/{b['id']}: externally visible failure "
            "details must be identical (no credential-existence oracle)"
        )
        low = a["expected"]["failure_detail"].lower()
        assert "already" not in low, (
            f"{a['id']}/{b['id']}: failure_detail must not confirm prior "
            "existence/consumption ('already')"
        )
        assert "consumed" not in low, (
            f"{a['id']}/{b['id']}: failure_detail must not confirm prior "
            "existence/consumption ('consumed')"
        )


def test_audit_categories_never_embed_tenant_identifiers() -> None:
    """Audit category ids are a fixed enumerated set — no per-tenant values."""
    doc = _load("threat_cases")
    fail = _load("failure_categories")
    audit_ids = {c["id"] for c in fail["audit_categories"]}
    for case in doc["cases"]:
        assert case["expected"]["audit_category"] in audit_ids, (
            f"case {case['id']}: audit category must come from the fixed taxonomy"
        )


# ---------------------------------------------------------------------------
# 5b. Lifecycle ownership matrix (THR-097 Unit C fresh lifecycle redesign)
# ---------------------------------------------------------------------------

# Every public membership mutation the lifecycle matrix MUST enumerate. The
# audit is producer-complete over runtime/remote_access (streams.py, gateway.py,
# forwarding.py, revocation.py); a mutation path discovered later must be added
# here AND to the fixture AND to the deterministic barrier battery — never
# silently omitted.
REQUIRED_LIFECYCLE_MUTATIONS = [
    "admission open",
    "admission rejection after seal",
    "duplicate-id replacement",
    "explicit single-stream close",
    "bulk close",
    "retained wrapper close",
    "missing/idempotent close",
    "derived wrapper surface",
    "derived membership reads",
    "re-entrant close_all callback",
    "re-entrant revoke callback",
    "concurrent close_all waiters",
    "RevocationCoordinator transaction",
    "gateway HTTP normal-EOF cleanup",
    "gateway HTTP error-driven cleanup",
    "gateway SSE/WebSocket admission",
    "gateway revoked-admission path",
    "transport-driven self-close",
    "transport-close callback mutation",
]


def _assert_lifecycle_ordering_cell(cell: dict, mutation_id: str, ordering: str) -> None:
    _assert_exact_keys(
        cell,
        ["ordering", "revocation_success_requires", "permitted_outcome"],
        f"lifecycle_matrix.mutations[{mutation_id}].{ordering}",
    )
    assert cell["ordering"] == ordering, (
        f"lifecycle_matrix mutation {mutation_id}: ordering must be {ordering!r}"
    )
    assert cell["revocation_success_requires"], (
        f"lifecycle_matrix mutation {mutation_id} {ordering}: "
        "revocation_success_requires must be stated"
    )
    assert cell["permitted_outcome"], (
        f"lifecycle_matrix mutation {mutation_id} {ordering}: "
        "permitted_outcome must be stated"
    )


def _assert_governing_rule(rule: str) -> None:
    """The single rule governing every matrix cell: one atomic lifecycle
    boundary with seal-before-escape and the acknowledgement barrier."""
    assert rule, "lifecycle_matrix: governing_rule required"
    for token in ("atomic lifecycle boundary", "seal", "acknowledgement barrier"):
        assert token in rule, f"lifecycle_matrix: governing_rule must encode {token!r}"


def test_lifecycle_matrix_schema_and_exhaustiveness() -> None:
    """The lifecycle matrix is semantically validated and exhaustive: every
    required mutation is present; every mutation crosses revocation in BOTH
    deterministic orderings with an exact linearization point, the atomic
    seal+membership transition, callback scope, and permitted outcomes."""
    doc = _load("lifecycle_matrix")
    _assert_governing_rule(doc["governing_rule"])
    _assert_exact_keys(
        doc["boundary"],
        ["lifecycle_lock", "atomic_transition", "callback_scope", "acknowledgement_barrier", "seal_first_orderings"],
        "lifecycle_matrix.boundary",
    )
    assert "linearization_points" in doc["linearization_points"] or doc["linearization_points"], (
        "lifecycle_matrix: linearization_points required"
    )

    mutations = doc["mutations"]
    assert mutations, "lifecycle_matrix: mutations must be non-empty"
    ids: set[str] = set()
    labels: set[str] = set()
    for mutation in mutations:
        mid = mutation.get("id", "<no id>")
        _assert_exact_keys(
            mutation,
            [
                "id",
                "public_entry",
                "membership_effect",
                "seal_scope",
                "linearization_point",
                "callbacks",
                "orderings",
                "cleanup_acknowledgement",
            ],
            f"lifecycle_matrix.mutations[{mid}]",
        )
        assert mid not in ids, f"lifecycle_matrix: duplicate mutation id {mid!r}"
        ids.add(mid)
        assert mutation["public_entry"], f"lifecycle_matrix mutation {mid}: public_entry required"
        assert mutation["linearization_point"], (
            f"lifecycle_matrix mutation {mid}: linearization_point required"
        )
        callbacks = mutation["callbacks"].lower()
        assert "outside" in callbacks or callbacks.startswith("none"), (
            f"lifecycle_matrix mutation {mid}: callbacks must be declared outside the lock "
            "(or explicitly 'none' for read-only cells)"
        )
        assert mutation["cleanup_acknowledgement"], (
            f"lifecycle_matrix mutation {mid}: cleanup_acknowledgement required"
        )
        orderings = mutation["orderings"]
        assert len(orderings) == 2, f"lifecycle_matrix mutation {mid}: exactly two orderings required"
        seen = {cell["ordering"] for cell in orderings}
        assert seen == {"mutation_first", "seal_first"}, (
            f"lifecycle_matrix mutation {mid}: must cross BOTH orderings "
            f"(mutation_first, seal_first), found {seen}"
        )
        for cell in orderings:
            _assert_lifecycle_ordering_cell(cell, mid, cell["ordering"])
        labels.add(mutation["membership_effect"].lower())

    present = {
        m["membership_effect"].lower()
        + " "
        + m["public_entry"].lower()
        + " "
        + m["seal_scope"].lower()
        + " "
        + m["linearization_point"].lower()
        + " "
        + m["callbacks"].lower()
        + " "
        + m["cleanup_acknowledgement"].lower()
        + " "
        + " ".join(cell["permitted_outcome"].lower() for cell in m["orderings"])
        for m in mutations
    }
    for required in REQUIRED_LIFECYCLE_MUTATIONS:
        tokens = required.lower().split()
        assert any(all(token in label for token in tokens) for label in present), (
            f"lifecycle_matrix: missing required mutation path {required!r}"
        )


def test_lifecycle_matrix_threat_case_parity() -> None:
    """The close-vs-revoke cells are encoded in the threat matrix too: REV-005
    (close linearizes first), REV-006 (seal linearizes first), and the
    THR-097 seq140 re-entrancy ruling REV-007 (same-thread re-entrant close_all
    fails closed with non-success) / REV-008 (a callback failure that becomes
    terminal after the rejection is persisted and re-surfaced) must exist and
    be hostile/denied with the normative revocation categories."""
    doc = _load("lifecycle_matrix")
    threat = _load("threat_cases")
    by_id = {c["id"]: c for c in threat["cases"]}
    for cid in ("REV-005", "REV-006", "REV-007", "REV-008"):
        assert cid in by_id, f"lifecycle_matrix: threat case {cid} missing"
        case = by_id[cid]
        assert case["class"] == "hostile"
        assert case["expected"]["outcome"] == "denied"
        assert case["expected"]["deny_category"] == "revocation"
        assert case["expected"]["audit_category"] == "revocation_stream_closed"
    # The matrix names the close-vs-seal mutations (M4/M6) and their cells.
    matrix_prose = json.dumps(doc)
    assert "single_close" in matrix_prose and "REV-005" in matrix_prose
    assert "REV-007" in matrix_prose and "REV-008" in matrix_prose


def test_lifecycle_matrix_reentrancy_cells_encode_fail_closed_ruling() -> None:
    """The THR-097 seq140 founder ruling is the normative contract: matrix cell
    M10 (same-thread re-entrant close_all from an unfinished transport cleanup
    callback) must state the fail-closed non-success rejection — never success,
    never an incomplete failed-id publish, never a self-inflight exclusion — and
    the persisted-failure guarantee. M12 (concurrent waiters) must state that a
    same-thread callback re-entry is rejected rather than waiting on the shared
    completion event (no self-deadlock)."""
    doc = _load("lifecycle_matrix")
    by_id = {m["id"]: m for m in doc["mutations"]}
    m10 = by_id["M10"]
    prose = json.dumps(m10)
    for token in (
        "rejected",
        "non-success",
        "never publishes success",
        "incomplete failed-id set",
        "never erased",
        "never excludes its own in-flight cleanup",
    ):
        assert token in prose, f"lifecycle_matrix M10 must encode {token!r} (founder ruling)"
    for cell in m10["orderings"]:
        assert "RuntimeError" in cell["permitted_outcome"], (
            f"lifecycle_matrix M10 {cell['ordering']}: must use the existing non-success "
            "representation (RuntimeError, mirroring the coordinator re-entrancy rejection)"
        )
    m12 = by_id["M12"]
    m12_prose = json.dumps(m12)
    assert "REJECTED" in m12_prose, (
        "lifecycle_matrix M12 must state the same-thread callback re-entry rejection"
    )
    assert "self-deadlock" in m12_prose, (
        "lifecycle_matrix M12 must state that waiting on the shared event would self-deadlock"
    )


_STALE_REENTRANCY_TOKENS = (
    "returns immediately",
    "return immediately",
    "immediate-return",
    "early success",
    "success immediately",
)


def _assert_no_stale_immediate_return(low: str, context: str) -> None:
    """Reject the TASK-5925 forbidden semantics wherever a re-entrancy cell
    states same-thread close_all behavior: immediate-return/early-success
    wording must never be normative in the lifecycle matrix."""
    for stale in _STALE_REENTRANCY_TOKENS:
        assert stale not in low, (
            f"{context} must not encode stale immediate-return/early-success "
            f"semantics ({stale!r} is the forbidden TASK-5925 behavior)"
        )


def test_lifecycle_matrix_m5_callback_reentrancy_encodes_fail_closed_ruling() -> None:
    """The THR-097 seq140 founder ruling governs M5 (bulk close_all) as well:
    M5's callback cell must NOT state that a same-thread re-entrant close_all
    from a snapshot transport-close callback 'returns immediately' (the exact
    stale semantics TASK-5936 flagged) and MUST encode the fail-closed
    rejection — RuntimeError/non-success before any seal, membership
    transition, cleanup acknowledgement, or success publication; callback
    termination stays inside the acknowledgement barrier; a later close_all
    reports terminal success or the persisted failure."""
    doc = _load("lifecycle_matrix")
    m5 = next(m for m in doc["mutations"] if m["id"] == "M5")
    callbacks = m5["callbacks"]
    low = callbacks.lower()
    _assert_no_stale_immediate_return(low, "lifecycle_matrix M5 callbacks")
    for token in (
        "rejected",
        "non-success",
        "before any seal",
        "acknowledgement barrier",
        "terminal success",
        "persisted",
    ):
        assert token in low, (
            f"lifecycle_matrix M5 callbacks must encode the seq140 fail-closed "
            f"ruling token {token!r}"
        )
    assert "RuntimeError" in callbacks, (
        "lifecycle_matrix M5 callbacks must name the existing non-success "
        "representation (RuntimeError)"
    )


def test_lifecycle_matrix_reentrancy_cells_cross_consistent() -> None:
    """Cross-cell consistency: every cell that states same-thread re-entrant
    close_all semantics — M5 (bulk close_all callbacks), M10 (re-entrant
    close_all), M12 (concurrent waiters re-entry), M19 (transport-close
    callback mutation) — must agree on the seq140 fail-closed non-success
    rejection, tie success to the acknowledgement barrier, and NONE may carry
    stale immediate-return / early-success semantics."""
    doc = _load("lifecycle_matrix")
    by_id = {m["id"]: m for m in doc["mutations"]}
    cells = {
        "M5": json.dumps(by_id["M5"]),
        "M10": json.dumps(by_id["M10"]),
        "M12": json.dumps(by_id["M12"]),
        "M19": json.dumps(by_id["M19"]),
    }
    for mid, prose in cells.items():
        low = prose.lower()
        _assert_no_stale_immediate_return(low, f"lifecycle_matrix {mid}")
        assert "rejected" in low, (
            f"lifecycle_matrix {mid} must encode the fail-closed rejection "
            "(seq140 same-thread re-entry)"
        )
        assert "non-success" in low, (
            f"lifecycle_matrix {mid} must encode the fail-closed non-success result"
        )
        assert "acknowledg" in low, (
            f"lifecycle_matrix {mid} must tie the re-entrant outcome to the "
            "acknowledgement barrier (callback termination inside it; no early success)"
        )


def test_lifecycle_matrix_mutation_close_vs_revoke_cells_are_distinct() -> None:
    """The two close-vs-revoke orderings must state DISTINCT permitted outcomes:
    mutation-first (transport close acknowledged before revocation success) vs
    seal-first (idempotent no-op, no double close) — never a merged cell."""
    doc = _load("lifecycle_matrix")
    for mutation in doc["mutations"]:
        if mutation["id"] not in {"M4", "M6"}:
            continue
        by_ordering = {cell["ordering"]: cell for cell in mutation["orderings"]}
        first = by_ordering["mutation_first"]["permitted_outcome"].lower()
        second = by_ordering["seal_first"]["permitted_outcome"].lower()
        assert first != second, (
            f"lifecycle_matrix {mutation['id']}: orderings must have distinct permitted outcomes"
        )
        assert "acknowledg" in first or "before revocation" in first, (
            f"lifecycle_matrix {mutation['id']} mutation_first: must require "
            "acknowledgement before revocation success"
        )
        assert "idempotent" in second and "double" in second, (
            f"lifecycle_matrix {mutation['id']} seal_first: must state the idempotent "
            "no-op / no double close outcome"
        )


def test_lifecycle_matrix_mutation_remove_boundary_detected() -> None:
    """Checked-in mutation proof: removing the atomic boundary from the
    governing rule (the fixed contract weakened to match old code) must fail
    the semantic validator — the matrix is the normative contract, not a
    description of existing code."""
    doc = json.loads(FIXTURE_FILES["lifecycle_matrix"].read_text(encoding="utf-8"))
    doc["governing_rule"] = (
        "admission checks the sealed flag; close pops membership and seals "
        "afterwards; revocation reports success after its own snapshot cleanup."
    )
    with pytest.raises(AssertionError):
        _assert_governing_rule(doc["governing_rule"])


# ---------------------------------------------------------------------------
# 6. Sentinel credential hygiene (fixtures + validation errors)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(FIXTURE_FILES))
def test_no_sentinel_credentials_in_serialized_fixtures(name: str) -> None:
    raw = FIXTURE_FILES[name].read_text(encoding="utf-8")
    hits = _scan_sentinels(raw)
    assert not hits, _format_sentinel_hits(name, hits)


def _assert_n3_lifecycle_evidence(matrix: list[dict]) -> None:
    assert [row["phase"] for row in matrix] == [
        "startup", "admission", "active_flow", "readiness_loss", "revocation",
        "shutdown", "partial_failure", "concurrency_reentry", "recovery",
    ]
    assert all(row["shipping_tests"] for row in matrix)
    required_observations = {
        "startup": {
            "process_absent",
            "tsnet_admission_absent",
            "connector_staged_credential_service_readable_non_writable",
            "sidecar_staged_credential_service_readable_non_writable",
            "credential_source_retired",
            "credential_dropin_retired",
            "composite_ready_after_sidecar",
            "missing_consumed_state_failed_closed",
        },
        "admission": {"tsnet_admission_reachable"},
        "active_flow": {"production_process_active", "watchdog_composite_current", "watchdog_ceased_on_sidecar_loss"},
        "readiness_loss": {"tsnet_admission_removed_before_connector"},
        "revocation": {"stop_before_connector_cleanup", "tsnet_admission_absent"},
        "shutdown": {"same_instance_stop_twice", "no_double_close", "no_residue"},
        "partial_failure": {"fresh_pid", "fresh_composite_gates"},
        "concurrency_reentry": {"start_then_stop_barrier", "stop_then_start_barrier", "stop_wins"},
        "recovery": {"fresh_install_rollback_reentry_each_checkpoint", "upgrade_rollback", "retained_payload_units", "fresh_composite_gates", "no_transaction_residue", "credential_free_stopped_restart", "interrupted_retirement_reentry", "explicit_fresh_reenrollment"},
    }
    for row in matrix:
        assert all(test_id.startswith("app/linux/package/real_systemd_n3.sh#") for test_id in row["shipping_tests"]), row
        assert set(row["observations"]) == required_observations[row["phase"]], row
        assert len(row["observations"]) == len(set(row["observations"])), row
        assert "deferred_n6" in row["outcome"] or row["phase"] not in {"admission", "active_flow"}


def test_n3_lifecycle_matrix_covers_shipping_package_boundaries() -> None:
    topology = _load("managed_topology")
    _assert_n3_lifecycle_evidence(topology["n3_lifecycle_matrix"])
    assert topology["delivery_status"]["n3"].startswith("linux_package")
    assert topology["n3_acceptance_harness"] == {
        "status": "shipping_unit_exact_head_proof",
        "unit_source": "fresh_plain_generated_shipping_unit",
        "af_netlink_dropin_absent": True,
        "denial_matrix_arm": "shipping-unit",
        "denial_matrix_address_families": "AF_INET AF_INET6 AF_UNIX AF_NETLINK",
    }


def test_n3_lifecycle_validator_rejects_prose_or_tautological_evidence() -> None:
    topology = _load("managed_topology")
    index = next(i for i, item in enumerate(topology["n3_lifecycle_matrix"]) if item["phase"] == "shutdown")
    for invalid in ([], ["shutdown"], ["proof_comment_present"], ["no_residue", "no_residue"]):
        matrix = [dict(row) for row in topology["n3_lifecycle_matrix"]]
        matrix[index]["observations"] = invalid
        with pytest.raises((AssertionError, KeyError)):
            _assert_n3_lifecycle_evidence(matrix)


def test_sentinel_hits_never_echoed_in_validation_errors() -> None:
    """The redaction contract extends to validator error messages themselves.

    A sentinel hit must be reported by class label and count only — the matched
    credential *value* may never be echoed into an assertion message.
    """
    synthetic = (
        '{"credential": "hrpair_ABC123secret", "auth": "Bearer abcdefghijklmnop12345678", '
        '"key": "-----BEGIN RSA PRIVATE KEY-----"}'
    )
    hits = _scan_sentinels(synthetic)
    assert hits
    message = _format_sentinel_hits("synthetic", hits)
    for _, value in hits:
        assert value not in message, f"validation error must not echo sentinel value {value!r}"
    # The message must still tell the operator WHICH class was hit (redacted to
    # category), without any credential-shaped value.
    for label, _ in hits:
        assert label in message, f"validation error must name the sentinel class {label!r}"


def test_placeholder_values_never_resemble_real_credentials() -> None:
    """Every credential-bearing value across all fixtures is an obvious placeholder."""
    for name in FIXTURE_FILES:
        doc = json.loads(FIXTURE_FILES[name].read_text(encoding="utf-8"))

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in {"example_placeholder", "credential_placeholder"}:
                        assert value is None or PLACEHOLDER_RE.match(value), (
                            f"{name}: {key} must be a PLACEHOLDER_* value, found {value!r}"
                        )
                        if value:
                            assert not _scan_sentinels(value), f"{name}: {value!r} looks secret-like"
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(doc)
