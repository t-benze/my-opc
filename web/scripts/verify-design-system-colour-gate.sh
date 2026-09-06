#!/usr/bin/env bash
# Shared shipping gate: keep the deterministic scanner regression suite and
# the real production-tree scan inseparable across local and GitHub Web CI.
set -euo pipefail

cd "$(dirname "$0")/.."

bash scripts/test-design-system-hex-scanner.sh
bash scripts/verify-design-system.sh --scan-hex
