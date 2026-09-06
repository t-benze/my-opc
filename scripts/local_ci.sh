#!/usr/bin/env bash
# Local CI wrapper — mirrors GitHub Actions commands as closely as practical.
# Runs from the repo root. GitHub CI remains authoritative; this is pre-push
# feedback only, not a replacement for the full matrix.
#
# Node runtime contract: the web/all targets must run under effective Node.js
# major exactly 24 (the repository .nvmrc declaration), matching the GitHub
# "Web (Node 24)" job. The wrapper verifies this before any npm/uv work and
# exits nonzero otherwise — it never runs npm under a different Node major.
#
# Usage:
#   scripts/local_ci.sh [TARGET]
#
# Targets:
#   python       uv sync --frozen; uv run pytest tests/ -v -n 4
#   web          cd web; npm ci; design-system colour gate; npm run lint;
#                npm run typecheck; npm run build; npm run build-storybook;
#                npx vitest run
#   integration  uv sync --frozen; uv run pytest tests/ -v -m integration
#   all          python + web (default; mirrors GitHub PR CI)
#   help         Show this help
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Advisory only: inode pressure can make mktemp, pytest, and Node tooling fail
# even when byte capacity remains. This preflight never cleans up files and
# never blocks CI when df is unavailable or its output cannot be parsed.
observe_tmp_inodes() {
  local inode_path="${TMPDIR:-/tmp}" output total used free percent
  output="$(df -Pi "$inode_path" 2>/dev/null | awk 'NR==2 {print $2, $3, $4, $5}' || true)"
  read -r total used free percent <<<"$output"
  percent="${percent%%%}"
  if [[ "$total" =~ ^[0-9]+$ ]] && [[ "$used" =~ ^[0-9]+$ ]] \
      && [[ "$free" =~ ^[0-9]+$ ]] && [[ "$percent" =~ ^[0-9]+$ ]]; then
    echo "=== Temporary-filesystem inode advisory ==="
    echo "path=${inode_path} used=${used} free=${free} total=${total} percent=${percent}%"
    if [ "$percent" -ge 90 ]; then
      echo -e "${YELLOW}WARNING: temporary-filesystem inode use is at or above 90%; this advisory does not authorize cleanup.${NC}" >&2
    fi
  else
    echo -e "${YELLOW}WARNING: temporary-filesystem inode measurement unavailable; continuing fail-open.${NC}" >&2
  fi
  return 0
}

# ── Node.js runtime contract (web/all targets) ───────────────────────────
# The repository declares its exact Node line in .nvmrc (Node 24), matching
# the GitHub "Web (Node 24)" job (.github/workflows/ci.yml). The web and all
# targets must run under effective Node major 24 exactly — never "whatever is
# installed" — otherwise they are not GitHub-Web parity and can pass/fail for
# the wrong reason. The guard runs before any uv/npm work and exits nonzero
# when Node 24 cannot be verified.

NODE_DECLARATION_FILE="${REPO_ROOT}/.nvmrc"

file_contains_nul() {
  # Byte-safe NUL detection. Bash command substitution ($(...)) silently
  # drops embedded NUL bytes, so a value that has already passed through a
  # shell variable can no longer reveal a NUL. This helper scans a FILE's
  # raw byte stream (via ``tr`` + ``wc -c`` in a pipe — never a shell
  # variable) and returns 0 (success) when a NUL byte is present, 1
  # otherwise. Every caller must reject NUL-bearing input BEFORE any command
  # substitution normalizes it.
  #
  # $1: path to the file to scan.
  local path="$1"
  [ -f "$path" ] || return 1
  # Compare the raw byte count against the byte count after deleting NUL
  # (\000). If they differ, at least one NUL byte was present. Both counts
  # come from raw byte streams (no command substitution), so an embedded NUL
  # is never silently discarded.
  [ "$(wc -c < "$path")" -gt "$(tr -d '\000' < "$path" | wc -c)" ]
}

node_declared_major() {
  # $1: raw .nvmrc content. Prints the declared major ("24") only when the
  # declaration is the exact bare token "24" with at most ONE trailing LF
  # (that single LF is stripped). A CRLF line ending ("24\r\n"), a lone
  # trailing CR ("24\r"), or any other value — empty, prefixed (">=24",
  # "v24x"), suffixed ("24.x", "24garbage"), or whitespace-separated
  # ("2 4") — prints empty. The CR is deliberately NOT stripped so CR
  # forms fail the equality below.
  local raw="${1:-}"
  raw="${raw%$'\n'}"
  if [ "$raw" = "24" ]; then
    printf '%s\n' "24"
  else
    printf '\n'
  fi
}

effective_node_major() {
  # Prints the effective `node --version` major (e.g. "24") only when the
  # output is canonical complete Node output — v<major>.<minor>.<patch> with
  # all-numeric components, no extra tokens, and at most ONE trailing LF.
  # Malformed output (v24x, v24.14garbage, v24.14, leading junk, trailing
  # whitespace, extra newline, CRLF "v24.0.0\r\n", lone trailing CR
  # "v24.0.0\r", or an embedded NUL byte) or a missing node prints empty.
  local ver tmp
  # Capture `node --version` to a FILE first so its raw bytes can be scanned
  # for an embedded NUL before Bash command substitution would silently
  # discard it (turning "v24.14.0\0\n" into "v24.14.0\n"). The file
  # write/read is byte-exact; only after the NUL scan does the content enter
  # a Bash variable.
  tmp="$(mktemp "${TMPDIR:-/tmp}/node-version.XXXXXX")" || { printf '\n'; return 0; }
  node --version >"$tmp" 2>/dev/null || true
  if file_contains_nul "$tmp"; then
    rm -f "$tmp"
    printf '\n'
    return 0
  fi
  # The trailing sentinel keeps any extra newlines so only a single normal
  # line terminator (not "v24.0.0\n\n" or trailing whitespace) is accepted.
  # The CR is deliberately NOT stripped: a CRLF or lone-CR output leaves a
  # trailing "\r" that the canonical regex rejects.
  ver="$(cat "$tmp"; printf x)"
  rm -f "$tmp"
  ver="${ver%x}"
  ver="${ver%$'\n'}"
  if [[ "$ver" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  else
    printf '\n'
  fi
}

try_select_node() {
  # Best-effort: ask a conventional local version manager (nvm) to select the
  # declared Node major. Selection changes the current shell's PATH, so the
  # caller MUST re-verify the effective node version afterwards.
  local decl="$1"
  local nvm_script=""
  if [ -n "${NVM_DIR:-}" ] && [ -f "${NVM_DIR}/nvm.sh" ]; then
    nvm_script="${NVM_DIR}/nvm.sh"
  elif [ -n "${HOME:-}" ] && [ -f "${HOME}/.nvm/nvm.sh" ]; then
    nvm_script="${HOME}/.nvm/nvm.sh"
  fi
  if [ -n "$nvm_script" ]; then
    # shellcheck disable=SC1090,SC1091
    if . "$nvm_script" >/dev/null 2>&1; then
      if command -v nvm >/dev/null 2>&1; then
        if nvm use "$decl" >/dev/null 2>&1; then
          return 0
        fi
      fi
    fi
  fi
  return 1
}

ensure_node_declared() {
  # Fail-fast Node runtime guard. Exits nonzero (before any uv/npm work) when
  # the effective Node major does not exactly match the repository declaration.
  local declared_major effective_major raw
  # Reject an embedded NUL at the byte level BEFORE command substitution would
  # silently discard it (turning a declaration of "24\0\n" into "24\n" that
  # would then pass). The scan reads the file's raw bytes directly, so a NUL
  # anywhere in the declaration fails closed here.
  if file_contains_nul "$NODE_DECLARATION_FILE"; then
    echo -e "${RED}ERROR: repository Node declaration (${NODE_DECLARATION_FILE}) is missing or malformed.${NC}" >&2
    echo "Expected a single Node major (e.g. \"24\")." >&2
    exit 1
  fi
  # The trailing sentinel keeps extra newlines so a declaration like "24\n\n"
  # (whitespace beyond the line terminator) is rejected instead of being
  # collapsed to "24" by command substitution.
  raw="$(cat "$NODE_DECLARATION_FILE" 2>/dev/null || true; printf x)"
  raw="${raw%x}"
  declared_major="$(node_declared_major "$raw")"
  if [ -z "$declared_major" ]; then
    echo -e "${RED}ERROR: repository Node declaration (${NODE_DECLARATION_FILE}) is missing or malformed.${NC}" >&2
    echo "Expected a single Node major (e.g. \"24\")." >&2
    exit 1
  fi

  effective_major="$(effective_node_major)"
  if [ -n "$effective_major" ] && [ "$effective_major" = "$declared_major" ]; then
    return 0
  fi

  if try_select_node "$declared_major"; then
    effective_major="$(effective_node_major)"
    if [ -n "$effective_major" ] && [ "$effective_major" = "$declared_major" ]; then
      return 0
    fi
  fi

  echo -e "${RED}ERROR: effective Node.js ${effective_major:-<none>} does not match the repository declaration (Node ${declared_major} from ${NODE_DECLARATION_FILE}).${NC}" >&2
  echo "The web/all targets must run under Node ${declared_major} exactly to match GitHub CI" >&2
  echo "(Web (Node 24)); they never run under a different Node major." >&2
  echo "Remediation:" >&2
  echo "  - Install Node ${declared_major} so it resolves on PATH, or select it with a" >&2
  echo "    version manager, e.g.: nvm install ${declared_major} && nvm use ${declared_major}" >&2
  echo "  - Then re-run: scripts/local_ci.sh web  (or all)" >&2
  exit 1
}

run_python() {
  echo -e "${GREEN}=== Python unit tests ===${NC}"
  uv sync --frozen
  uv run pytest tests/ -v -n 4
}

run_web() {
  ensure_node_declared
  echo -e "${GREEN}=== Web CI ===${NC}"
  cd web
  npm ci
  echo -e "${YELLOW}--- Design-system colour gate ---${NC}"
  bash scripts/verify-design-system-colour-gate.sh
  echo -e "${YELLOW}--- Lint ---${NC}"
  npm run lint
  echo -e "${YELLOW}--- Typecheck ---${NC}"
  npm run typecheck
  echo -e "${YELLOW}--- Build ---${NC}"
  npm run build
  echo -e "${YELLOW}--- Storybook static build ---${NC}"
  npm run build-storybook
  echo -e "${YELLOW}--- Test (non-watch) ---${NC}"
  npx vitest run
}

run_integration() {
  echo -e "${GREEN}=== Python integration tests ===${NC}"
  uv sync --frozen
  uv run pytest tests/ -v -m integration
}

run_all() {
  observe_tmp_inodes
  ensure_node_declared
  run_python
  echo ""
  run_web
}

show_help() {
  echo "Usage: scripts/local_ci.sh [TARGET]"
  echo ""
  echo "Local CI wrapper — mirrors GitHub Actions commands as closely as practical."
  echo "GitHub CI remains authoritative; this is pre-push feedback only."
  echo ""
  echo "Targets:"
  echo "  python       Run Python unit tests"
  echo "               (uv sync --frozen + uv run pytest tests/ -v -n 4)"
  echo "  web          Run Web CI"
  echo "               (npm ci + colour gate + lint + typecheck + build + build-storybook + vitest run)"
  echo "  integration  Run Python integration tests"
  echo "               (uv run pytest tests/ -v -m integration)"
  echo "  all          Default: runs python + web (mirrors GitHub PR CI)"
  echo "  help         Show this help"
  echo ""
  echo "Caveats:"
  echo "  - Web/all targets require effective Node.js major exactly 24 (the"
  echo "    repository .nvmrc declaration, matching the GitHub Web (Node 24)"
  echo "    job); the wrapper verifies this before any work and exits nonzero"
  echo "    otherwise."
  echo "  - Python tests use the installed uv + Python interpreter, not the"
  echo "    GHA 3.12/3.13/3.14 matrix."
  echo "  - Integration tests spawn an isolated daemon per test (tmp"
  echo "    HAPPYRANCH_DAEMON_HOME + ephemeral port), so a production"
  echo "    daemon does not conflict. Both share machine RAM."
  echo "  - Web CI runs vitest run (non-watch mode), matching GHA behavior."
  echo "  - uv sync --frozen ensures lockfile parity; run 'uv lock' first if"
  echo "    you've changed pyproject.toml."
}

case "${1:-all}" in
  python)       run_python ;;
  web)          run_web ;;
  integration)  run_integration ;;
  all)          run_all ;;
  help|-h|--help) show_help ;;
  *)
    echo -e "${RED}Unknown target: $1${NC}" >&2
    show_help
    exit 1
    ;;
esac
