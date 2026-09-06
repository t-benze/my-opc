#!/usr/bin/env bash
# CI-only, zero-skip proof of the packaged N3 production seam.
set -euo pipefail

HEADSCALE_VERSION=0.25.1
HEADSCALE_SHA256=d2cda0a5d748587f77c920a76cd1bf1ab429e5299ba5bc6b3dda90712721b45b
TAILSCALE_VERSION=1.102.3
TAILSCALE_SHA256=36ddd9b51be57ffc2990cf76323cfa13643bfbb1b8a969f6183fa164741cdef5
readonly HEADSCALE_VERSION HEADSCALE_SHA256 TAILSCALE_VERSION TAILSCALE_SHA256

current_acceptance_arm=""; current_acceptance_variant=""; candidate_failure_phase=""
candidate_failure_preserved=0; candidate_failure_preserving=0; arm_journal_cursor=""
candidate_snapshot_path=""; candidate_preservation_failure_path=""
candidate_invocation_started=0
fail() {
  local message="$1"
  local preservation_ok=1
  if [[ "$current_acceptance_variant" == candidate && "$candidate_failure_preserved" == 0 && "$candidate_failure_preserving" == 0 ]] && declare -F preserve_candidate_failure >/dev/null; then
    preserve_candidate_failure || preservation_ok=0
    if declare -F arm_cleanup >/dev/null; then
      candidate_failure_preserving=1
      arm_cleanup || true
    fi
  fi
  (( preservation_ok == 1 )) || echo "n3-real-systemd: candidate terminal evidence preservation failed closed" >&2
  echo "n3-real-systemd: $message" >&2
  exit 1
}
wait_for() {
  local label="$1"; shift
  for _attempt in $(seq 1 60); do "$@" && return 0; sleep 1; done
  fail "timeout waiting for $label"
}
wait_for_candidate() {
  for _attempt in $(seq 1 60); do "$@" && return 0; sleep 1; done
  return 1
}
candidate_boundary() { [[ "${N3_FAULT_PHASE:-}" != "$1" ]]; }
port_open() { timeout 1 bash -c "</dev/tcp/127.0.0.1/$1" 2>/dev/null; }
active() { sudo systemctl is-active --quiet "$1"; }
absent() { ! sudo test -e "$1" || fail "residue at $1"; }
evidence() {
  python "$evidence_driver" observe "$evidence_artifact" --phase "$1" --observation "$2" --assertion-id "$run_id:$1:$2"
}
diagnostic() {
  python "$evidence_driver" diagnose "$evidence_artifact" --id "$run_id:$1" --category "$1" --phase "$2" --actor "$3" --unit "$4"
}
acceptance_arm() {
  python "$evidence_driver" arm "$evidence_artifact" --id "$1" --ordering "$2" --variant "$3" \
    --input-sha256 "$acceptance_input_sha" "${@:4}"
}
tsnet_open() {
  [[ -n "${sidecar_ip:-}" ]] || return 1
  printf 'GET / HTTP/1.0\r\n\r\n' | timeout 5 sudo "$ts_dir/tailscale" --socket="$work/peer.sock" nc "$sidecar_ip" 443 >/dev/null 2>&1
}
current_journal_cursor() {
  local output cursors
  output="$(sudo journalctl -n 0 --show-cursor --no-pager)" || return 1
  cursors="$(sed -n 's/^-- cursor: //p' <<<"$output")"
  [[ "$(wc -l <<<"$cursors" | xargs)" == 1 && -n "$cursors" ]] || return 1
  printf '%s\n' "$cursors"
}
systemctl_property() {
  local unit="$1" property="$2" value status
  set +e
  value="$(systemctl show "$unit" -p "$property" --value 2>/dev/null)"
  status=$?
  set -e
  (( status == 0 )) && [[ -n "$value" && "$value" != *$'\n'* ]] || return 1
  printf '%s\n' "$value"
}
settle_control_terminal_invocation() {
  local unit=happyranch-tsnet-sidecar.service invocation result exec_main_status
  sudo systemctl stop "$unit" || return 1
  sudo systemctl reset-failed "$unit" || return 1
  invocation="$(systemctl_property "$unit" InvocationID)" || return 1
  result="$(systemctl_property "$unit" Result)" || return 1
  exec_main_status="$(systemctl_property "$unit" ExecMainStatus)" || return 1
  [[ "$invocation" =~ ^[0-9a-f]{32}$ && "$result" =~ ^[a-z][a-z-]*$ && "$exec_main_status" =~ ^[0-9]+$ ]] || return 1
  printf '%s\t%s\t%s\n' "$invocation" "$result" "$exec_main_status"
}
current_control_terminal_evidence() {
  local cursor="$1" invocation_id="$2" result="$3" exec_main_status="$4" qualifying_mode="${5:-engine_start}"
  sudo journalctl --after-cursor="$cursor" -o json --output-fields=MESSAGE,_SYSTEMD_INVOCATION_ID --no-pager | python -c '
import json, sys
invocation, result, exec_main_status, qualifying_mode = sys.argv[1:]
if len(invocation) != 32 or any(c not in "0123456789abcdef" for c in invocation):
    raise SystemExit(1)
if result not in {"success", "resources", "timeout", "exit-code", "signal", "core-dump", "watchdog", "start-limit-hit", "protocol"}:
    raise SystemExit(1)
if not exec_main_status.isascii() or not exec_main_status.isdigit() or int(exec_main_status) > 255:
    raise SystemExit(1)
phases = {
    "credential_input": "input_acquisition",
    "engine_start": "engine_initialization",
    "network_join": "peer_establishment",
    "durable_commit": "receipt_commit",
}
counts = {scope: {category: 0 for category in phases} for scope in ("pinned", "window")}
for line in sys.stdin:
    if not line.strip():
        continue
    entry = json.loads(line)
    message = entry.get("MESSAGE")
    if not isinstance(message, str) or not message.startswith("diagnostic_receipt="):
        continue
    receipt = json.loads(message.split("=", 1)[1])
    category = receipt.get("category") if isinstance(receipt, dict) else None
    expected = {
        "category": category, "phase": phases.get(category),
        "actor": "tsnet-sidecar", "unit": "happyranch-tsnet-sidecar.service",
        "outcome": "failed", "terminal": True, "assertion": {"status": "completed"},
    }
    if category not in phases or receipt != expected:
        raise SystemExit(1)
    counts["window"][category] += 1
    if entry.get("_SYSTEMD_INVOCATION_ID") == invocation:
        counts["pinned"][category] += 1
def summary(scope):
    values = counts[scope]
    return {
        "categories": sorted(category for category, count in values.items() if count),
        "category_counts": values,
        "receipt_count": sum(values.values()),
    }
pinned = summary("pinned")
pinned["qualifying_receipt_count"] = pinned["receipt_count"] if qualifying_mode == "all" else counts["pinned"]["engine_start"]
pinned["cardinality"] = "zero" if pinned["receipt_count"] == 0 else "one" if pinned["receipt_count"] == 1 else "multiple"
output = {
    "pinned_invocation": pinned,
    "systemd": {"result": result, "exec_main_status": int(exec_main_status)},
    "window": summary("window"),
}
print(json.dumps(output, sort_keys=True, separators=(",", ":")))
' "$invocation_id" "$result" "$exec_main_status" "$qualifying_mode"
}
settle_terminal_invocation() { settle_control_terminal_invocation; }
current_terminal_evidence() { current_control_terminal_evidence "$@"; }
control_terminal_evidence_qualifies() {
  python -c 'import json,sys; d=json.load(sys.stdin); p=d["pinned_invocation"]; assert p["qualifying_receipt_count"] == p["receipt_count"] == 1; assert p["categories"] == ["engine_start"]'
}
capture_control_terminal_snapshot() {
  local cursor="$1" output_path="$2" unit=happyranch-tsnet-sidecar.service
  local invocation result exec_main_status terminal_evidence
  # Pin the invocation while the failed unit is still observable. Stopping the
  # unit settles any pending restart, but reset-failed is destructive to the
  # properties used for the exact invocation/receipt join and must happen only
  # after the validated snapshot is durable.
  invocation="$(systemctl_property "$unit" InvocationID)" || return 1
  [[ "$invocation" =~ ^[0-9a-f]{32}$ ]] || return 1
  sudo systemctl stop "$unit" || return 1
  result="$(systemctl_property "$unit" Result)" || return 1
  exec_main_status="$(systemctl_property "$unit" ExecMainStatus)" || return 1
  terminal_evidence="$(current_control_terminal_evidence "$cursor" "$invocation" "$result" "$exec_main_status")" || return 1
  control_terminal_evidence_qualifies <<<"$terminal_evidence" || return 1
  printf '%s\n' "$terminal_evidence" >"$output_path"
  sudo systemctl reset-failed "$unit" || return 1
}
systemctl_absent_value() {
  local unit="$1" property="$2" expected="$3" value status
  set +e
  value="$(systemctl show "$unit" -p "$property" --value 2>/dev/null)"
  status=$?
  set -e
  [[ "$value" == "$expected" ]] || return 1
  # systemctl returns nonzero for a unit which is genuinely not loaded.  The
  # exact absent value is evidence for that exit only; empty/prose output and
  # every other query failure remain fatal.
  (( status == 0 || status == 1 || status == 4 )) || return 1
  (( status == 0 )) || [[ "$value" == not-found || "$value" == inactive || "$value" == dead || "$value" == 0 ]]
}
unit_absent() {
  local unit="$1" unit_root="${N3_UNIT_ROOT:-}"
  systemctl_absent_value "$unit" LoadState not-found || return 1
  systemctl_absent_value "$unit" ActiveState inactive || return 1
  systemctl_absent_value "$unit" SubState dead || return 1
  local main_pid status
  set +e
  main_pid="$(systemctl show "$unit" -p MainPID --value 2>/dev/null)"
  status=$?
  set -e
  [[ -z "$main_pid" || "$main_pid" == 0 ]] || return 1
  (( status == 0 || status == 1 || status == 4 )) || return 1
  (( status == 0 )) || [[ "$main_pid" == 0 ]] || return 1
  [[ ! -e "$unit_root/etc/systemd/system/$unit" && ! -e "$unit_root/run/systemd/system/$unit" ]] || return 1
  [[ ! -e "$unit_root/etc/systemd/system/$unit.d" && ! -e "$unit_root/run/systemd/system/$unit.d" ]] || return 1
}
diagnostics="${N3_DIAGNOSTICS_DIR:-$(mktemp -d)}"
mkdir -p "$diagnostics"
printf 'subject=%s\n' "${PROOF_SUBJECT_SHA:-missing}" >"$diagnostics/bootstrap.txt"
[[ -n "${PACKAGE_TAR:-}" && -f "$PACKAGE_TAR" ]] || fail "PACKAGE_TAR missing"
[[ "${PROOF_SUBJECT_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || fail "PROOF_SUBJECT_SHA missing"
[[ "$(ps -p 1 -o comm= | xargs)" == systemd ]] || fail "PID 1 is not systemd"
systemctl is-system-running >/dev/null 2>&1 || [[ "$(systemctl is-system-running 2>/dev/null)" == degraded ]] || fail "system manager unavailable"
sudo -n true || fail "passwordless sudo unavailable"
sudo systemd-run --quiet --wait --collect --unit=happyranch-n3-qualification /bin/true || fail "transient units unavailable"

work="$(mktemp -d)"
evidence_driver="$PWD/app/linux/package/n3_evidence.py"
evidence_artifact="$diagnostics/execution-evidence.json"
run_id="$(cat /proc/sys/kernel/random/uuid)"
package_sha="$(sha256sum "$PACKAGE_TAR" | cut -d' ' -f1)"
python "$evidence_driver" init "$evidence_artifact" --git-head "$PROOF_SUBJECT_SHA" --package-sha256 "$package_sha" --run-id "$run_id"
headscale_pid=""; peer_pid=""; daemon_pid=""
cleanup() {
  local original_status=$? cleanup_failed=0
  set +e
  sudo systemctl stop happyranch-managed.target
  if [[ -n "${sidecar_ip:-}" ]] && [[ -n "$peer_pid" ]] && sudo kill -0 "$peer_pid" 2>/dev/null; then
    ! tsnet_open || cleanup_failed=1
    (( cleanup_failed != 0 )) || evidence cleanup virtual_admission_removed_while_peer_alive || cleanup_failed=1
  fi
  sudo systemctl disable happyranch-managed.target
  sudo systemctl reset-failed happyranch-connector.service happyranch-tsnet-sidecar.service happyranch-managed.target
  sudo rm -rf /etc/systemd/system/happyranch-tsnet-sidecar.service.d
  sudo rm -f /etc/systemd/system/happyranch-connector.service /etc/systemd/system/happyranch-tsnet-sidecar.service /etc/systemd/system/happyranch-managed.target
  sudo systemctl daemon-reload
  for pid in "$peer_pid" "$daemon_pid" "$headscale_pid"; do
    [[ -z "$pid" ]] || sudo kill "$pid"
  done
  for pid in "$peer_pid" "$daemon_pid" "$headscale_pid"; do
    if [[ -n "$pid" ]]; then
      # The primary failure may be the fixture exiting before teardown. Reap
      # it, but determine residue from liveness after the reap, not exit code.
      wait "$pid" 2>/dev/null
    fi
    [[ -z "$pid" ]] || ! sudo kill -0 "$pid" 2>/dev/null || cleanup_failed=1
  done
  sudo rm -f /usr/local/share/ca-certificates/happyranch-n3-ci.crt
  sudo update-ca-certificates >/dev/null 2>&1
  printf 'fixtures_reaped=%s\n' "$(( cleanup_failed == 0 ))" >"$diagnostics/cleanup-status.txt"
  sudo rm -rf /opt/happyranch /etc/happyranch /var/lib/happyranch-connector /var/lib/happyranch-tsnet-sidecar /run/happyranch-connector /run/happyranch-tsnet-sidecar /var/log/happyranch-connector /var/log/happyranch-tsnet-sidecar
  systemctl list-unit-files happyranch-managed.target happyranch-connector.service happyranch-tsnet-sidecar.service --no-legend 2>/dev/null | grep -q . && cleanup_failed=1
  for path in /opt/happyranch /etc/happyranch /var/lib/happyranch-connector /var/lib/happyranch-tsnet-sidecar /run/happyranch-connector /run/happyranch-tsnet-sidecar /var/log/happyranch-connector /var/log/happyranch-tsnet-sidecar /.happyranch-install-transaction.json /.happyranch-backup /.happyranch-units-backup; do
    sudo test ! -e "$path" || cleanup_failed=1
  done
  [[ -z "$(sudo find / -maxdepth 1 \( -name '.happyranch-stage-*' -o -name '.happyranch-tmp-*' \) -print -quit)" ]] || cleanup_failed=1
  for port in 18443 18765 18080 19090 15043 13478; do ! port_open "$port" || cleanup_failed=1; done
  for unit in happyranch-connector.service happyranch-tsnet-sidecar.service; do
    main_pid="$(systemctl show "$unit" -p MainPID --value 2>/dev/null)"
    [[ -z "$main_pid" || "$main_pid" == 0 ]] || cleanup_failed=1
  done
  (( cleanup_failed != 0 )) || evidence cleanup all_residue_absent || cleanup_failed=1
  rm -rf "$work"
  [[ ! -e "$work" ]] || cleanup_failed=1
  (( cleanup_failed != 0 )) || evidence cleanup task_work_removed || cleanup_failed=1
  if (( original_status == 0 && cleanup_failed == 0 )); then
    python "$evidence_driver" finalize "$evidence_artifact" || cleanup_failed=1
    python "$evidence_driver" validate "$evidence_artifact" --expected-subject "$PROOF_SUBJECT_SHA" --expected-run "$run_id" || cleanup_failed=1
  fi
  (( cleanup_failed == 0 )) || echo "n3-real-systemd: teardown residue" >&2
  trap - EXIT INT TERM
  (( original_status != 0 )) && exit "$original_status"
  exit "$cleanup_failed"
}
trap cleanup EXIT INT TERM

hs_url="https://github.com/juanfont/headscale/releases/download/v${HEADSCALE_VERSION}/headscale_${HEADSCALE_VERSION}_linux_amd64"
ts_url="https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VERSION}_amd64.tgz"
curl --fail --location --proto '=https' --tlsv1.2 "$hs_url" -o "$work/headscale"
echo "$HEADSCALE_SHA256  $work/headscale" | sha256sum --check --status || fail "Headscale checksum mismatch"
curl --fail --location --proto '=https' --tlsv1.2 "$ts_url" -o "$work/tailscale.tgz"
echo "$TAILSCALE_SHA256  $work/tailscale.tgz" | sha256sum --check --status || fail "Tailscale checksum mismatch"
chmod 0700 "$work/headscale"; tar -xzf "$work/tailscale.tgz" -C "$work"
ts_dir="$work/tailscale_${TAILSCALE_VERSION}_amd64"

mkdir -p "$work/hs" "$work/tls"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost -addext subjectAltName=DNS:localhost,IP:127.0.0.1 -keyout "$work/tls/key.pem" -out "$work/tls/cert.pem" >/dev/null 2>&1
chmod 0600 "$work/tls/key.pem"
cat >"$work/hs/config.yaml" <<EOF
server_url: https://127.0.0.1:18080
listen_addr: 127.0.0.1:18080
metrics_listen_addr: 127.0.0.1:19090
grpc_listen_addr: 127.0.0.1:15043
unix_socket: $work/hs/headscale.sock
noise:
  private_key_path: $work/hs/noise.key
prefixes:
  v4: 100.64.0.0/10
  v6: fd7a:115c:a1e0::/48
  allocation: sequential
database:
  type: sqlite3
  path: $work/hs/db.sqlite
tls_cert_path: $work/tls/cert.pem
tls_key_path: $work/tls/key.pem
dns:
  magic_dns: false
  base_domain: ci.invalid
derp:
  server:
    enabled: true
    region_id: 999
    region_code: ci
    region_name: CI
    stun_listen_addr: "127.0.0.1:13478"
    private_key_path: $work/hs/derp.key
  urls: []
  paths: []
  automatically_add_embedded_derp_region: true
policy:
  mode: file
  path: $work/hs/policy.json
EOF
printf '%s\n' '{"acls":[{"action":"accept","src":["*"],"dst":["*:*"]}]}' >"$work/hs/policy.json"
sudo install -m 0644 "$work/tls/cert.pem" /usr/local/share/ca-certificates/happyranch-n3-ci.crt
sudo update-ca-certificates >/dev/null
"$work/headscale" serve --config "$work/hs/config.yaml" >"$work/headscale.log" 2>&1 & headscale_pid=$!
wait_for "Headscale process" sudo kill -0 "$headscale_pid"
wait_for "Headscale HTTPS listener" port_open 18080
wait_for "Headscale health" curl --silent --fail --cacert "$work/tls/cert.pem" https://127.0.0.1:18080/health
"$work/headscale" users create ci --config "$work/hs/config.yaml"
peer_key="$("$work/headscale" preauthkeys create --user ci --reusable=false --expiration 10m --config "$work/hs/config.yaml")"
sidecar_key="$("$work/headscale" preauthkeys create --user ci --reusable=false --expiration 10m --config "$work/hs/config.yaml")"

sudo "$ts_dir/tailscaled" --state="$work/peer.state" --socket="$work/peer.sock" --tun=userspace-networking >"$work/peer.log" 2>&1 & peer_pid=$!
wait_for "synthetic peer local API" sudo test -S "$work/peer.sock"
sudo "$ts_dir/tailscale" --socket="$work/peer.sock" up --login-server=https://127.0.0.1:18080 --auth-key="$peer_key" --hostname=synthetic-peer-ci --accept-dns=false --timeout=30s

# N3 only needs a genuine reachable loopback daemon gate. Request admission
# and authorization-negative proof are deliberately deferred to N6.
python -m http.server 18765 --bind 127.0.0.1 >"$work/daemon.log" 2>&1 & daemon_pid=$!
wait_for "loopback daemon" port_open 18765
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin happyranch 2>/dev/null || true
sudo install -d -m 0700 -o happyranch -g happyranch /etc/happyranch
printf '%s\n' synthetic-daemon-token >"$work/daemon.token"
# Both plaintext LoadCredential sources remain root-custodied; service users
# receive only systemd's private staged copies.
sudo install -m 0600 -o root -g root "$work/daemon.token" /etc/happyranch/daemon.token

python - "$work" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
base=Path(sys.argv[1])
artifact=json.loads(Path('tests/contract/managed_remote_access/route-policy.json').read_text())
(base/'policy.json').write_text(json.dumps({'schema_version':1,'artifact_version':int(artifact['version']),'issued_at':(datetime.now(timezone.utc)-timedelta(seconds=10)).isoformat(),'max_age_seconds':3600,'revision':1,'state':'active','artifact':artifact}))
config={'tenant_id':'tenant-ci','home_id':'home-ci','connector_id':'connector-ci','daemon_port':18765,'daemon_token_path':'/etc/happyranch/daemon.token','policy_path':'/etc/happyranch/policy.json','state_path':'/var/lib/happyranch-connector/trust-state.json','system':True,'service_user':'happyranch','service_group':'happyranch','poll_seconds':0.2,'managed':{'bind_host':'127.0.0.1','bind_port':18443,'token_ttl_seconds':300,'credential_ttl_days':365}}
(base/'connector.json').write_text(json.dumps(config))
PY
sudo install -m 0600 -o happyranch -g happyranch "$work/connector.json" /etc/happyranch/connector.json
sudo install -m 0600 -o happyranch -g happyranch "$work/policy.json" /etc/happyranch/policy.json
printf '%s\n' "{\"StateDir\":\"/var/lib/happyranch-tsnet-sidecar\",\"ControlURL\":\"https://127.0.0.1:18080\",\"RoleIdentity\":\"home-sidecar-ci\",\"ExpectedPeers\":[\"synthetic-peer-ci\"],\"ListenAddr\":\":443\",\"ConnectorAddr\":\"127.0.0.1:18443\",\"DERPPolicy\":\"private-only\"}" >"$work/sidecar.json"
sudo install -m 0600 -o happyranch -g happyranch "$work/sidecar.json" /etc/happyranch/sidecar.json
printf '%s\n' "$sidecar_key" >"$work/enrollment.key"
# The system manager requires a root-custodied plaintext LoadCredential source;
# the unprivileged service receives only systemd's private staged copy.
sudo install -m 0600 -o root -g root "$work/enrollment.key" /etc/happyranch/enrollment.key
sudo env "PATH=$PATH" uv run python - "$PACKAGE_TAR" <<'PY'
import sys
from pathlib import Path
from runtime.remote_access.linux_package import install_linux_package
install_linux_package(Path(sys.argv[1]), Path('/'), system_service=True)
PY
[[ "$(stat -c %U:%G:%a /opt/happyranch)" == "root:root:755" ]] || fail "system-service payload root custody mismatch"
[[ "$(stat -c %U:%G:%a /opt/happyranch/bin)" == "root:root:755" ]] || fail "system-service binary directory custody mismatch"
for binary in /opt/happyranch/bin/happyranch-connector /opt/happyranch/bin/happyranch-tsnet-sidecar; do
  [[ "$(stat -c %U:%G:%a "$binary")" == "root:root:755" ]] || fail "system-service binary custody mismatch"
  sudo -u happyranch test -x "$binary" || fail "system-service user cannot execute packaged binary"
done
sudo systemctl daemon-reload
[[ "$(sudo stat -c %U:%G:%a /etc/systemd/system/happyranch-tsnet-sidecar.service.d/10-enrollment-credential.conf)" == "root:root:600" ]] || fail "transient credential drop-in custody mismatch"
[[ "$(sudo stat -c %U:%G:%a /etc/happyranch/daemon.token)" == "root:root:600" ]] || fail "daemon credential source custody mismatch"
[[ "$(sudo stat -c %U:%G:%a /etc/happyranch/enrollment.key)" == "root:root:600" ]] || fail "enrollment credential source custody mismatch"

# Acceptance-only AF_NETLINK A/B. The package is immutable; the candidate's
# sole semantic delta is this harness-created transient systemd drop-in.
acceptance_input_sha="$(printf '%s\n' "$package_sha" "$HEADSCALE_VERSION" "$HEADSCALE_SHA256" "$TAILSCALE_VERSION" "$TAILSCALE_SHA256" "synthetic-peer-ci" "443" "18443" | sha256sum | cut -d' ' -f1)"
acceptance_arms=(
  "ordering-a-control:A:control"
  "ordering-a-candidate:A:candidate"
  "ordering-b-candidate:B:candidate"
  "ordering-b-control:B:control"
)
capture_denial_matrix() {
  local arm_id="$1"
  # Execute bounded probes as the shipping service user before teardown. Only
  # fixed identifiers/classifications leave this process; exception prose,
  # paths, identities, credentials, and control responses are discarded.
  sudo timeout 15 systemd-run --quiet --wait --collect --pipe \
    --unit="happyranch-n3-denial-${arm_id}" \
    --property=User=happyranch --property=Group=happyranch \
    --property=NoNewPrivileges=yes --property=PrivateDevices=yes \
    --property=ProtectSystem=strict --property=ProtectHome=yes \
    --property=ReadWritePaths=/var/lib/happyranch-tsnet-sidecar \
    --property='RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK' \
    --property='CapabilityBoundingSet=' \
    /usr/bin/python3 - "$arm_id" >"$diagnostics/$arm_id-denial-matrix.json" <<'PY'
import errno, json, os, socket, sys

ERRNOS = {errno.EACCES:"EACCES", errno.EPERM:"EPERM", errno.ENOENT:"ENOENT",
          errno.ENODEV:"ENODEV", errno.EAFNOSUPPORT:"EAFNOSUPPORT",
          errno.ETIMEDOUT:"ETIMEDOUT", errno.ECONNREFUSED:"ECONNREFUSED", errno.EIO:"EIO"}
def measured(operation, probe):
    try:
        value = probe()
        if hasattr(value, "close"): value.close()
        return {"id":operation,"measured":True,"result":"allow","category":"none","errno":None}
    except OSError as exc:
        code = ERRNOS.get(exc.errno, "OTHER")
        category = "permission_denied" if exc.errno in (errno.EACCES, errno.EPERM) else "unavailable"
        if exc.errno == errno.ETIMEDOUT: category = "timeout"
        return {"id":operation,"measured":True,"result":"deny" if category == "permission_denied" else "unknown","category":category,"errno":code}
    except Exception:
        return {"id":operation,"measured":True,"result":"unknown","category":"operational_error","errno":"OTHER"}
def writable():
    path = "/var/lib/happyranch-tsnet-sidecar/probe-write"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd); os.unlink(path)
operations = [
    measured("address_family_netlink", lambda: socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)),
    measured("linux_capabilities", lambda: socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)),
    measured("device_access", lambda: open("/dev/net/tun", "rb", buffering=0)),
    measured("writable_paths", writable),
    measured("control_plane_operations", lambda: socket.create_connection(("127.0.0.1", 18080), timeout=2)),
]
json.dump({"schema":"happyranch.n3.sandbox-denial-matrix","version":1,
           "arm_id":sys.argv[1],"operations":operations}, sys.stdout, separators=(",", ":"))
PY
  python "$evidence_driver" validate-denial-matrix "$diagnostics/$arm_id-denial-matrix.json" --expected-arm "$arm_id"
}
preserve_candidate_failure() {
  candidate_failure_preserving=1
  local denial_path="$diagnostics/$current_acceptance_arm-denial-matrix.json"
  local terminal_path="$diagnostics/$current_acceptance_arm-candidate-terminal-evidence.json"
  [[ -n "$current_acceptance_arm" && -n "$candidate_failure_phase" ]] || return 1
  if [[ ! -s "$candidate_snapshot_path" ]]; then
    if [[ -z "$arm_journal_cursor" ]]; then
      write_candidate_preservation_failure cursor_unavailable
      return 1
    fi
    if [[ "$candidate_invocation_started" != 1 ]]; then
      write_candidate_preservation_failure invocation_unavailable
      return 1
    fi
    capture_candidate_snapshot || {
      write_candidate_preservation_failure snapshot_unavailable
      return 1
    }
  fi
  python - "$current_acceptance_arm" "$candidate_failure_phase" "$candidate_snapshot_path" "$terminal_path" <<'PY'
import json, pathlib, sys
arm, phase, snapshot_path, output_path = sys.argv[1:]
snapshot = json.loads(pathlib.Path(snapshot_path).read_text())
doc = {"schema":"happyranch.n3.candidate-terminal-evidence","version":1,
       "arm_id":arm,"phase":phase,"invocation_binding":"settled_current",
       "terminal_evidence":snapshot["terminal_evidence"],
       "denial_matrix":snapshot["denial_matrix"]}
pathlib.Path(output_path).write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
PY
  if ! python "$evidence_driver" validate-candidate-terminal "$terminal_path" --expected-arm "$current_acceptance_arm"; then
    rm -f "$candidate_snapshot_path" "$terminal_path"
    write_candidate_preservation_failure snapshot_invalid
    return 1
  fi
  candidate_failure_preserved=1
}
write_candidate_preservation_failure() {
  local failure_code="$1"
  candidate_preservation_failure_path="$diagnostics/$current_acceptance_arm-candidate-preservation-failure.json"
  python - "$current_acceptance_arm" "$candidate_failure_phase" "$failure_code" "$candidate_preservation_failure_path" <<'PY'
import json, pathlib, sys
arm, phase, code, output = sys.argv[1:]
pathlib.Path(output).write_text(json.dumps({"schema":"happyranch.n3.candidate-preservation-failure","version":1,
    "arm_id":arm,"phase":phase,"failure_code":code}, sort_keys=True, separators=(",", ":")) + "\n")
PY
  python "$evidence_driver" validate-candidate-preservation-failure "$candidate_preservation_failure_path" --expected-arm "$current_acceptance_arm"
}
capture_candidate_snapshot() {
  local denial_path="$diagnostics/$current_acceptance_arm-denial-matrix.json"
  [[ -n "$arm_journal_cursor" ]] || return 1
  if [[ ! -f "$denial_path" ]]; then
    capture_denial_matrix "$current_acceptance_arm" || return 1
  fi
  local invocation_id systemd_result exec_main_status terminal_evidence
  IFS=$'\t' read -r invocation_id systemd_result exec_main_status < <(settle_terminal_invocation) || return 1
  terminal_evidence="$(current_terminal_evidence "$arm_journal_cursor" "$invocation_id" "$systemd_result" "$exec_main_status" all)" || return 1
  candidate_snapshot_path="$diagnostics/$current_acceptance_arm-candidate-settled-snapshot.json"
  python - "$terminal_evidence" "$denial_path" "$candidate_snapshot_path" <<'PY'
import json, pathlib, sys
terminal, denial_path, output = sys.argv[1:]
pathlib.Path(output).write_text(json.dumps({"terminal_evidence":json.loads(terminal),
    "denial_matrix":json.loads(pathlib.Path(denial_path).read_text())}, sort_keys=True, separators=(",", ":")) + "\n")
PY
}
arm_cleanup() {
  local cleanup_complete=0 residue_root="${N3_RESIDUE_ROOT:-}"
  # These requests are deliberately idempotent: the pre-arm reset also runs
  # after a prior cleanup has removed the units.  The explicit process, port,
  # fixture, credential, transaction, and path checks below decide success.
  sudo systemctl stop happyranch-managed.target happyranch-tsnet-sidecar.service happyranch-connector.service || true
  sudo systemctl disable happyranch-managed.target || true
  sudo systemctl reset-failed happyranch-managed.target happyranch-tsnet-sidecar.service happyranch-connector.service || true
  sudo rm -rf /etc/systemd/system/happyranch-tsnet-sidecar.service.d
  sudo rm -f /etc/systemd/system/happyranch-managed.target /etc/systemd/system/happyranch-tsnet-sidecar.service /etc/systemd/system/happyranch-connector.service
  sudo systemctl daemon-reload || cleanup_complete=1
  sudo rm -rf /opt/happyranch /etc/happyranch /var/lib/happyranch-connector /var/lib/happyranch-tsnet-sidecar /run/happyranch-connector /run/happyranch-tsnet-sidecar /var/log/happyranch-connector /var/log/happyranch-tsnet-sidecar
  while read -r fixture_id; do
    [[ -z "$fixture_id" ]] || "$work/headscale" nodes delete --identifier "$fixture_id" --force --config "$work/hs/config.yaml" >/dev/null || cleanup_complete=1
  done < <("$work/headscale" nodes list --output json --config "$work/hs/config.yaml" | python -c 'import json,sys; print("\n".join(str(n["id"]) for n in json.load(sys.stdin) if n.get("givenName")=="home-sidecar-ci" or n.get("name")=="home-sidecar-ci"))')
  "$work/headscale" nodes list --output json --config "$work/hs/config.yaml" | python -c 'import json,sys; raise SystemExit(any(n.get("givenName")=="home-sidecar-ci" or n.get("name")=="home-sidecar-ci" for n in json.load(sys.stdin)))' || cleanup_complete=1
  for unit in happyranch-managed.target happyranch-tsnet-sidecar.service happyranch-connector.service; do
    unit_absent "$unit" || cleanup_complete=1
  done
  ! systemctl list-unit-files happyranch-managed.target happyranch-tsnet-sidecar.service happyranch-connector.service --no-legend 2>/dev/null | grep -q . || cleanup_complete=1
  ! port_open 18443 || cleanup_complete=1
  ! tsnet_open || cleanup_complete=1
  [[ ! -e "$residue_root/etc/happyranch/enrollment.key" && ! -e "$residue_root/etc/systemd/system/happyranch-tsnet-sidecar.service.d" ]] || cleanup_complete=1
  [[ ! -e "$residue_root/.happyranch-install-transaction.json" && ! -e "$residue_root/.happyranch-backup" && ! -e "$residue_root/.happyranch-units-backup" ]] || cleanup_complete=1
  [[ ! -e "$residue_root/opt/happyranch" && ! -e "$residue_root/etc/happyranch" && ! -e "$residue_root/var/lib/happyranch-connector" && ! -e "$residue_root/var/lib/happyranch-tsnet-sidecar" ]] || cleanup_complete=1
  [[ ! -e "$residue_root/run/happyranch-connector" && ! -e "$residue_root/run/happyranch-tsnet-sidecar" && ! -e "$residue_root/var/log/happyranch-connector" && ! -e "$residue_root/var/log/happyranch-tsnet-sidecar" ]] || cleanup_complete=1
  ! pgrep -f '(^|/)(happyranch-connector|happyranch-tsnet-sidecar)( |$)' >/dev/null || cleanup_complete=1
  [[ -z "$(sudo find "${residue_root:-/}" -maxdepth 1 \( -name '.happyranch-stage-*' -o -name '.happyranch-tmp-*' \) -print -quit)" ]] || cleanup_complete=1
  (( cleanup_complete == 0 ))
}
arm_reset() {
  local arm_id="$1" variant="$2"
  arm_cleanup || return 1
  sudo install -d -m 0700 -o happyranch -g happyranch /etc/happyranch
  sudo install -m 0600 -o root -g root "$work/daemon.token" /etc/happyranch/daemon.token
  sudo install -m 0600 -o happyranch -g happyranch "$work/connector.json" /etc/happyranch/connector.json
  sudo install -m 0600 -o happyranch -g happyranch "$work/policy.json" /etc/happyranch/policy.json
  sudo install -m 0600 -o happyranch -g happyranch "$work/sidecar.json" /etc/happyranch/sidecar.json
  local fresh_key
  fresh_key="$("$work/headscale" preauthkeys create --user ci --reusable=false --expiration 10m --config "$work/hs/config.yaml")"
  printf '%s\n' "$fresh_key" >"$work/enrollment.key"
  sudo install -m 0600 -o root -g root "$work/enrollment.key" /etc/happyranch/enrollment.key
  sudo env "PATH=$PATH" uv run python - "$PACKAGE_TAR" <<'PY'
import sys
from pathlib import Path
from runtime.remote_access.linux_package import install_linux_package
install_linux_package(Path(sys.argv[1]), Path('/'), system_service=True)
PY
  if [[ "$variant" == candidate ]]; then
    sudo install -d -m 0755 /etc/systemd/system/happyranch-tsnet-sidecar.service.d
    printf '%s\n' '[Service]' 'RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK' | sudo tee /etc/systemd/system/happyranch-tsnet-sidecar.service.d/90-ci-af-netlink.conf >/dev/null
  fi
  sudo systemctl daemon-reload
  printf 'arm=%s reset=complete cleanup_complete=true\n' "$arm_id" >>"$diagnostics/acceptance-arms.log"
}
for arm_spec in "${acceptance_arms[@]}"; do
  IFS=: read -r arm_id ordering variant <<<"$arm_spec"
  current_acceptance_arm="$arm_id"; current_acceptance_variant="$variant"
  candidate_failure_phase=pre_cursor; candidate_failure_preserved=0; candidate_failure_preserving=0
  arm_journal_cursor=""; candidate_snapshot_path=""; candidate_preservation_failure_path=""
  candidate_invocation_started=0
  [[ "$variant" != candidate ]] || candidate_boundary pre_cursor || fail "candidate pre-cursor boundary failed"
  arm_journal_cursor="$(current_journal_cursor)" || fail "current arm journal cursor unavailable"
  candidate_failure_phase=pre_reset
  [[ "$variant" != candidate ]] || candidate_boundary pre_reset || fail "candidate pre-reset boundary failed"
  candidate_failure_phase=setup_start
  arm_reset "$arm_id" "$variant" || fail "acceptance arm reset/setup failed"
  production_expected_peer_visible=0
  sudo test ! -e /var/lib/happyranch-tsnet-sidecar/credential.consumed || fail "current arm ExpectedPeers marker was not fresh"
  [[ "$variant" != candidate ]] || candidate_invocation_started=1
  if [[ "$ordering" == A ]]; then
    sudo systemctl start --no-block happyranch-connector.service
    sudo systemctl start happyranch-tsnet-sidecar.service || true
  else
    sudo systemctl start --no-block happyranch-tsnet-sidecar.service
    sudo systemctl start happyranch-connector.service || true
  fi
  if [[ "$variant" == control ]]; then
    sleep 2
    ! active happyranch-tsnet-sidecar.service || fail "shipping control unexpectedly READY"
    control_snapshot_path="$diagnostics/$arm_id-control-settled-snapshot.json"
    capture_control_terminal_snapshot "$arm_journal_cursor" "$control_snapshot_path" || fail "shipping control terminal snapshot unsafe or unavailable"
    redacted_control_evidence="$(<"$control_snapshot_path")"
    printf '{"arm_id":"%s","invocation_binding":"current","terminal_evidence":%s}\n' "$arm_id" "$redacted_control_evidence" >>"$diagnostics/control-terminal-receipts.jsonl"
    control_terminal_evidence_qualifies <<<"$redacted_control_evidence" || fail "shipping control current arm coarse receipt cardinality mismatch"
    python "$evidence_driver" diagnose "$evidence_artifact" --id "$run_id:$arm_id:engine_start" --category engine_start --phase engine_initialization --actor tsnet-sidecar --unit happyranch-tsnet-sidecar.service
    arm_result_args=(--control-category engine_start --control-phase engine_initialization)
  else
    candidate_boundary setup_start || fail "candidate setup/start boundary failed"
    candidate_failure_phase=ready
    if ! wait_for_candidate active happyranch-tsnet-sidecar.service; then
      fail "AF_NETLINK candidate did not become READY"
    fi
    candidate_boundary ready || fail "candidate READY boundary failed"
    # This arm-fresh production marker is committed only after TSNetEngine has
    # observed the sole configured ExpectedPeer. It precedes listener READY
    # and is independent of the synthetic peer's reverse status query below.
    if sudo test -f /var/lib/happyranch-tsnet-sidecar/credential.consumed; then
      production_expected_peer_visible=1
    fi
    candidate_failure_phase=expected_peers
    sidecar_ip="$(sudo "$ts_dir/tailscale" --socket="$work/peer.sock" status --json | python -c 'import json,sys; d=json.load(sys.stdin); print(next(ip for p in d.get("Peer",{}).values() if p.get("HostName")=="home-sidecar-ci" for ip in p.get("TailscaleIPs",[]) if ":" not in ip))')" || fail "candidate ExpectedPeers visibility failed"
    candidate_boundary expected_peers || fail "candidate ExpectedPeers boundary failed"
    candidate_failure_phase=listener
    tsnet_open || fail "candidate virtual listener unreachable"
    candidate_boundary listener || fail "candidate listener boundary failed"
    candidate_failure_phase=denial_matrix
    candidate_boundary denial_matrix || fail "candidate denial-matrix boundary failed"
    candidate_failure_phase=assertion
    (( production_expected_peer_visible == 1 )) || fail "candidate production ExpectedPeers observation missing"
    candidate_boundary assertion || fail "candidate assertion boundary failed"
    arm_result_args=(--ready)
    (( production_expected_peer_visible == 1 )) && arm_result_args+=(--expected-peer-visible)
    arm_result_args+=(--virtual-listener-reachable)
  fi
  if [[ "$variant" == candidate ]]; then
    candidate_failure_phase=cleanup
    capture_candidate_snapshot || fail "candidate settled snapshot unavailable before cleanup"
    candidate_boundary cleanup || fail "candidate cleanup boundary failed"
  fi
  arm_cleanup || fail "acceptance arm cleanup incomplete"
  # Receipt follows every production assertion, including cleanup.
  [[ "$variant" != candidate ]] || candidate_failure_phase=post_cleanup_assertion
  [[ "$variant" != candidate ]] || candidate_boundary post_cleanup_assertion || fail "candidate post-cleanup assertion boundary failed"
  acceptance_arm "$arm_id" "$ordering" "$variant" "${arm_result_args[@]}" || fail "acceptance arm assertion failed"
  current_acceptance_arm=""; current_acceptance_variant=""; candidate_failure_phase=""
done

# Restore one fresh candidate fixture for the existing lifecycle matrix. This
# remains harness-local and does not alter the packaged unit renderer.
arm_reset lifecycle-candidate candidate

# semantic evidence: startup
sudo mv /etc/happyranch/enrollment.key /etc/happyranch/enrollment.key.held
sudo systemctl start happyranch-managed.target || true
sleep 2
sudo systemctl stop happyranch-managed.target happyranch-tsnet-sidecar.service happyranch-connector.service
! active happyranch-tsnet-sidecar.service || fail "sidecar survived missing-credential startup"
absent /var/lib/happyranch-tsnet-sidecar/credential.consumed
[[ "$(systemctl show happyranch-tsnet-sidecar.service -p MainPID --value)" == 0 ]] || fail "sidecar process survived failed startup"
sudo "$ts_dir/tailscale" --socket="$work/peer.sock" status --json | python -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(any(p.get("HostName")=="home-sidecar-ci" for p in (d.get("Peer") or {}).values()))' || fail "failed-start TSNet identity remained visible"
evidence "startup" "process_absent"
evidence "startup" "tsnet_admission_absent"
diagnostic credential_input input_acquisition systemd happyranch-tsnet-sidecar.service
# The unit has Restart=on-failure.  End the deliberately failed credential
# transaction completely before restoring the one-use source, otherwise a
# queued restart can race the first real enrollment and consume its staging.
sudo systemctl reset-failed happyranch-tsnet-sidecar.service happyranch-connector.service
wait_for "failed credential staging cleanup" sudo test ! -e /run/credentials/happyranch-tsnet-sidecar.service
sudo mv /etc/happyranch/enrollment.key.held /etc/happyranch/enrollment.key

sudo systemctl start happyranch-managed.target
wait_for "connector READY" active happyranch-connector.service
wait_for "sidecar READY and ExpectedPeers" active happyranch-tsnet-sidecar.service
connector_ready="$(systemctl show happyranch-connector.service -p ActiveEnterTimestampMonotonic --value)"
sidecar_ready="$(systemctl show happyranch-tsnet-sidecar.service -p ActiveEnterTimestampMonotonic --value)"
[[ "$sidecar_ready" -le "$connector_ready" ]] || fail "connector reported composite READY before sidecar admission"
check_staged_credential() {
  local path="$1" directory="$2" name="$3"
  [[ "$path" == "$directory/$name" ]] || fail "staged credential provenance mismatch"
  [[ "$(readlink -f -- "$path")" == "$path" ]] || fail "staged credential escape mismatch"
  sudo test -f "$path" || fail "staged credential type mismatch"
  ! sudo test -L "$path" || fail "staged credential symlink mismatch"
  sudo -u happyranch test -r "$path" || fail "staged credential unreadable"
  ! sudo -u happyranch test -w "$path" || fail "staged credential service-writable"
  ! sudo -u happyranch test -w "$directory" || fail "staged credential directory service-writable"
  printf 'credential_observation name=%s file=%s directory=%s\n' \
    "$name" "$(sudo stat -c %U:%G:%a:%F "$path")" "$(sudo stat -c %U:%G:%a:%F "$directory")"
}
check_staged_credential /run/credentials/happyranch-connector.service/daemon.token /run/credentials/happyranch-connector.service daemon.token
check_staged_credential /run/credentials/happyranch-tsnet-sidecar.service/enrollment.key /run/credentials/happyranch-tsnet-sidecar.service enrollment.key
absent /etc/happyranch/enrollment.key
absent /etc/systemd/system/happyranch-tsnet-sidecar.service.d/10-enrollment-credential.conf
evidence "startup" "connector_staged_credential_service_readable_non_writable"
evidence "startup" "sidecar_staged_credential_service_readable_non_writable"
evidence "startup" "credential_source_retired"
evidence "startup" "credential_dropin_retired"
evidence "startup" "composite_ready_after_sidecar"
sudo systemctl stop happyranch-managed.target
sudo mv /var/lib/happyranch-tsnet-sidecar/credential.consumed "$work/credential.consumed.held"
sudo systemctl start happyranch-managed.target || true
sleep 2
! active happyranch-tsnet-sidecar.service || fail "missing consumed state started sidecar"
evidence "startup" "missing_consumed_state_failed_closed"
sudo mv "$work/credential.consumed.held" /var/lib/happyranch-tsnet-sidecar/credential.consumed
sudo systemctl reset-failed happyranch-tsnet-sidecar.service happyranch-connector.service
sudo systemctl start happyranch-managed.target
wait_for "credential-free stopped-service restart" active happyranch-tsnet-sidecar.service
absent /etc/happyranch/enrollment.key
absent /etc/systemd/system/happyranch-tsnet-sidecar.service.d/10-enrollment-credential.conf
evidence "recovery" "credential_free_stopped_restart"

# Re-enter after interruption between drop-in reload and source unlink.
sudo systemctl stop happyranch-managed.target
printf '%s\n' interrupted-retirement >"$work/enrollment.key"
sudo install -m 0600 -o root -g root "$work/enrollment.key" /etc/happyranch/enrollment.key
sudo systemctl start happyranch-managed.target
wait_for "interrupted retirement re-entry" active happyranch-tsnet-sidecar.service
absent /etc/happyranch/enrollment.key
evidence "recovery" "interrupted_retirement_reentry"

# A fresh enrollment is explicit and service-stopped; ordinary restarts never
# recreate this root-custodied source or transient LoadCredential drop-in.
sudo systemctl stop happyranch-managed.target
fresh_sidecar_key="$("$work/headscale" preauthkeys create --user ci --reusable=false --expiration 10m --config "$work/hs/config.yaml")"
printf '%s\n' "$fresh_sidecar_key" >"$work/enrollment.key"
sudo install -m 0600 -o root -g root "$work/enrollment.key" /etc/happyranch/enrollment.key
sudo /opt/happyranch/bin/happyranch-connector prepare-fresh-enrollment --source /etc/happyranch/enrollment.key --marker /var/lib/happyranch-tsnet-sidecar/credential.consumed --dropin /etc/systemd/system/happyranch-tsnet-sidecar.service.d/10-enrollment-credential.conf
sudo systemctl start happyranch-managed.target
wait_for "explicit fresh re-enrollment" active happyranch-tsnet-sidecar.service
absent /etc/happyranch/enrollment.key
absent /etc/systemd/system/happyranch-tsnet-sidecar.service.d/10-enrollment-credential.conf
evidence "recovery" "explicit_fresh_reenrollment"
sidecar_ip="$(sudo "$ts_dir/tailscale" --socket="$work/peer.sock" status --json | python -c 'import json,sys; d=json.load(sys.stdin); print(next(ip for p in d.get("Peer",{}).values() if p.get("HostName")=="home-sidecar-ci" for ip in p.get("TailscaleIPs",[]) if ":" not in ip))')"
wait_for "virtual TSNet listener" tsnet_open
evidence "admission" "tsnet_admission_reachable"
[[ "$(systemctl show happyranch-tsnet-sidecar.service -p MainPID --value)" != 0 ]] || fail "production sidecar absent"
evidence "active_flow" "production_process_active"
watchdog_before="$(systemctl show happyranch-connector.service -p WatchdogTimestampMonotonic --value)"
sleep 12
watchdog_after="$(systemctl show happyranch-connector.service -p WatchdogTimestampMonotonic --value)"
[[ "$watchdog_after" -gt "$watchdog_before" ]] || fail "connector watchdog did not follow current composite health"
active happyranch-tsnet-sidecar.service || fail "watchdog continued without sidecar health"
evidence "active_flow" "watchdog_composite_current"

sudo systemctl stop happyranch-tsnet-sidecar.service
wait_for "connector watchdog cessation" bash -c '! systemctl is-active --quiet happyranch-connector.service'
! tsnet_open || fail "admission survived sidecar health loss"
watchdog_stopped="$(systemctl show happyranch-connector.service -p WatchdogTimestampMonotonic --value)"
sleep 2
[[ "$(systemctl show happyranch-connector.service -p WatchdogTimestampMonotonic --value)" == "$watchdog_stopped" ]] || fail "watchdog refreshed after sidecar loss"
evidence "active_flow" "watchdog_ceased_on_sidecar_loss"
sudo systemctl start happyranch-managed.target
wait_for "composite restart after health loss" active happyranch-tsnet-sidecar.service
wait_for "virtual admission after health recovery" tsnet_open

# semantic evidence: partial_failure
old_pid="$(systemctl show happyranch-tsnet-sidecar.service -p MainPID --value)"
sudo kill -KILL "$old_pid"
wait_for "automatic Restart=" bash -c "test \"\$(systemctl show happyranch-tsnet-sidecar.service -p MainPID --value)\" != '$old_pid' && systemctl is-active --quiet happyranch-tsnet-sidecar.service"
evidence "partial_failure" "fresh_pid"
tsnet_open || fail "fresh process did not restore composite gates"
evidence "partial_failure" "fresh_composite_gates"

# semantic evidence: concurrency_reentry. A shipping-unit ExecStartPre barrier
# proves start has entered before stop is queued; stop must win after release.
sudo install -d -m 0755 /etc/systemd/system/happyranch-tsnet-sidecar.service.d
sudo tee /etc/systemd/system/happyranch-tsnet-sidecar.service.d/90-ci-barrier.conf >/dev/null <<EOF
[Service]
ExecStartPre=/bin/sh -c 'touch $work/start-entered; while test ! -e $work/start-release; do sleep .05; done'
EOF
sudo systemctl daemon-reload
sudo systemctl stop happyranch-managed.target
sudo systemctl start happyranch-managed.target & start_job=$!
wait_for "start barrier entered" test -e "$work/start-entered"
sudo systemctl stop happyranch-managed.target & stop_job=$!
systemctl list-jobs --no-legend | grep -q 'happyranch-managed.target' || fail "stop was not queued behind entered start"
evidence "concurrency_reentry" "start_then_stop_barrier"
: >"$work/start-release"; wait "$start_job" || true; wait "$stop_job"
! active happyranch-tsnet-sidecar.service || fail "start-then-stop did not stop"
evidence "concurrency_reentry" "stop_wins"

# Force the opposite ordering: production Stop completes while a new start is
# queued behind an ExecStopPost barrier, so stale admission cannot survive.
sudo tee /etc/systemd/system/happyranch-tsnet-sidecar.service.d/90-ci-barrier.conf >/dev/null <<EOF
[Service]
ExecStopPost=/bin/sh -c 'touch $work/stop-entered; while test ! -e $work/stop-release; do sleep .05; done'
EOF
sudo systemctl daemon-reload
sudo systemctl start happyranch-managed.target
sudo systemctl stop happyranch-managed.target & stop_job=$!
wait_for "stop barrier entered" test -e "$work/stop-entered"
! tsnet_open || fail "TSNet admission survived production Stop"
sudo systemctl start happyranch-managed.target & start_job=$!
systemctl list-jobs --no-legend | grep -q 'happyranch-managed.target' || fail "start was not queued behind entered stop"
evidence "concurrency_reentry" "stop_then_start_barrier"
: >"$work/stop-release"; wait "$stop_job"; wait "$start_job"
sudo rm -f /etc/systemd/system/happyranch-tsnet-sidecar.service.d/90-ci-barrier.conf
sudo systemctl daemon-reload

# semantic evidence: readiness_loss. Compare the real systemd monotonic
# inactive timestamps and probe the virtual TSNet listener from the real peer.
sudo systemctl stop happyranch-connector.service
wait_for "BindsTo readiness loss" bash -c '! systemctl is-active --quiet happyranch-tsnet-sidecar.service'
! tsnet_open || fail "virtual TSNet admission remained after connector loss"
sidecar_down="$(systemctl show happyranch-tsnet-sidecar.service -p InactiveEnterTimestampMonotonic --value)"
connector_down="$(systemctl show happyranch-connector.service -p InactiveEnterTimestampMonotonic --value)"
[[ "$sidecar_down" -le "$connector_down" ]] || fail "connector cleanup preceded TSNet admission removal"
evidence "readiness_loss" "tsnet_admission_removed_before_connector"
sudo systemctl stop happyranch-managed.target

# semantic evidence: revocation and shutdown. The packaged binary calls Stop
# twice on the same Sidecar and emits invocation-scoped receipts for its PID.
sudo systemctl start happyranch-managed.target
wait_for "shutdown admission" tsnet_open
shutdown_pid="$(systemctl show happyranch-tsnet-sidecar.service -p MainPID --value)"
sudo systemctl stop happyranch-managed.target
! tsnet_open || fail "TSNet admission survived target stop"
shutdown_receipts="$(sudo journalctl -u happyranch-tsnet-sidecar.service _PID="$shutdown_pid" --no-pager -o cat | grep "lifecycle_stop_complete run=$shutdown_pid invocation=" || true)"
[[ "$(printf '%s\n' "$shutdown_receipts" | grep -c .)" == 2 ]] || fail "same-instance Stop did not produce two receipts"
grep -qx "lifecycle_stop_complete run=$shutdown_pid invocation=1" <<<"$shutdown_receipts" || fail "missing first same-instance Stop receipt"
grep -qx "lifecycle_stop_complete run=$shutdown_pid invocation=2" <<<"$shutdown_receipts" || fail "missing second same-instance Stop receipt"
evidence "revocation" "stop_before_connector_cleanup"
evidence "revocation" "tsnet_admission_absent"
evidence "shutdown" "same_instance_stop_twice"
evidence "shutdown" "no_double_close"
[[ "$(systemctl show happyranch-tsnet-sidecar.service -p MainPID --value)" == 0 ]] || fail "sidecar residue after repeated shutdown"
evidence "shutdown" "no_residue"

sudo systemctl start happyranch-managed.target
wait_for "fresh recovery" active happyranch-tsnet-sidecar.service
# semantic evidence: recovery. Exercise the real installer checkpoint seam on
# both empty-root and live upgrade paths, then re-enter and prove fresh gates.
for boundary in payload_old_retained payload_published unit_published:happyranch-connector.service unit_published:happyranch-tsnet-sidecar.service unit_published:happyranch-managed.target; do
  sudo systemctl stop happyranch-managed.target
  sudo env "PATH=$PATH" uv run python - <<'PY'
from pathlib import Path
from runtime.remote_access.linux_package import uninstall_linux_package
uninstall_linux_package(Path('/'))
PY
  BOUNDARY="$boundary" sudo env "PATH=$PATH" BOUNDARY="$boundary" uv run python - "$PACKAGE_TAR" <<'PY'
import os, sys
from pathlib import Path
from runtime.remote_access.linux_package import install_linux_package
def fault(name):
    if name == os.environ['BOUNDARY']:
        raise RuntimeError('injected')
try:
    install_linux_package(Path(sys.argv[1]), Path('/'), system_service=True, fault=fault)
except RuntimeError:
    pass
else:
    raise SystemExit('fault did not fire')
PY
  absent /opt/happyranch
  for unit in happyranch-connector.service happyranch-tsnet-sidecar.service happyranch-managed.target; do absent "/etc/systemd/system/$unit"; done
  for path in /.happyranch-install-transaction.json /.happyranch-backup /.happyranch-units-backup; do absent "$path"; done
  [[ -z "$(sudo find / -maxdepth 1 \( -name '.happyranch-stage-*' -o -name '.happyranch-tmp-*' \) -print -quit)" ]] || fail "fresh transaction residue"
  sudo env "PATH=$PATH" uv run python - "$PACKAGE_TAR" <<'PY'
import sys
from pathlib import Path
from runtime.remote_access.linux_package import install_linux_package
install_linux_package(Path(sys.argv[1]), Path('/'), system_service=True)
PY
  sudo systemctl daemon-reload
  sudo systemctl start happyranch-managed.target
  wait_for "fresh $boundary composite startup" active happyranch-tsnet-sidecar.service
  wait_for "fresh $boundary virtual admission" tsnet_open
done
evidence "recovery" "fresh_install_rollback_reentry_each_checkpoint"
before_manifest="$(sudo sha256sum /opt/happyranch/manifest.json)"
sudo env "PATH=$PATH" BOUNDARY=payload_published uv run python - "$PACKAGE_TAR" <<'PY'
import os, sys
from pathlib import Path
from runtime.remote_access.linux_package import install_linux_package
def fault(name):
    if name == os.environ['BOUNDARY']:
        raise RuntimeError('injected')
try:
    install_linux_package(Path(sys.argv[1]), Path('/'), system_service=True, fault=fault)
except RuntimeError:
    pass
else:
    raise SystemExit('fault did not fire')
PY
[[ "$(sudo sha256sum /opt/happyranch/manifest.json)" == "$before_manifest" ]] || fail "upgrade rollback lost retained payload"
for unit in happyranch-connector.service happyranch-tsnet-sidecar.service happyranch-managed.target; do sudo test -f "/etc/systemd/system/$unit" || fail "upgrade rollback lost $unit"; done
evidence "recovery" "upgrade_rollback"
evidence "recovery" "retained_payload_units"
sudo env "PATH=$PATH" uv run python - "$PACKAGE_TAR" <<'PY'
import sys
from pathlib import Path
from runtime.remote_access.linux_package import install_linux_package
install_linux_package(Path(sys.argv[1]), Path('/'), system_service=True)
PY
absent /.happyranch-install-transaction.json
absent /.happyranch-backup
absent /.happyranch-units-backup
[[ -z "$(sudo find / -maxdepth 1 -name '.happyranch-stage-*' -print -quit)" ]] || fail "transaction stage residue"
evidence "recovery" "no_transaction_residue"
sudo systemctl daemon-reload
sudo systemctl start happyranch-managed.target
wait_for "upgrade recovery" active happyranch-tsnet-sidecar.service
wait_for "upgrade virtual admission" tsnet_open
evidence "recovery" "fresh_composite_gates"
echo N3_REAL_SYSTEMD_PASS
