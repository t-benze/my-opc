#!/usr/bin/env bash
#
# CI gate for the web design system. Runs typecheck + lint + tests +
# deterministic Storybook static build + complete production-source hex scan.
#
# Exit codes:
#   0 — clean
#   1 — typecheck, lint, test, or build failure
#   3 — production hex baseline mismatch
#
# Run locally before pushing: `bash scripts/verify-design-system.sh`
set -euo pipefail

cd "$(dirname "$0")/.."

scan_hex() {
  local source_root=${DESIGN_SYSTEM_SOURCE_ROOT:-src}
  local allowlist=${DESIGN_SYSTEM_HEX_ALLOWLIST:-scripts/design-system-hex-allowlist.tsv}

  python3 - "$source_root" "$allowlist" <<'PY'
from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import sys

source_root = Path(sys.argv[1])
allowlist_path = Path(sys.argv[2])
extensions = {".css", ".js", ".jsx", ".ts", ".tsx"}
excluded_directories = {"__snapshots__", "__tests__", "reports", "screenshots", "test"}

hex_value = r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F](?:[0-9a-fA-F]{2}){0,2})?"
# Any Tailwind utility (including variants and negative/important forms) whose
# arbitrary value is itself a hex color. Keeping the utility name structural
# covers the full color-capable vocabulary without treating issue references
# or HTML numeric entities as classes.
tailwind = re.compile(rf"(?<![\w-])(?:[a-z][\w-]*:)*!?-?[a-z][\w-]*-\[({hex_value})\](?![0-9a-fA-F])", re.IGNORECASE)
css_or_object = re.compile(rf"(?:^|[;{{,])\s*(?:--[\w-]+|[\w-]*(?:color|background|border|fill|stroke|shadow|outline|decoration|accent|caret)[\w-]*)\s*:\s*['\"]?({hex_value})(?![0-9a-fA-F])", re.IGNORECASE)
jsx_attribute = re.compile(rf"\b(?:color|fill|stroke)\s*=\s*(?:['\"]|{{\s*['\"])({hex_value})(?![0-9a-fA-F])", re.IGNORECASE)

def is_production_file(path: Path) -> bool:
    relative = path.relative_to(source_root)
    if path.suffix not in extensions:
        return False
    if any(part in excluded_directories for part in relative.parts[:-1]):
        return False
    if relative.as_posix() == "design-system/tokens/tokens.css":
        return False
    if ".test." in path.name or ".stories." in path.name:
        return False
    return True

files = sorted(path for path in source_root.rglob("*") if path.is_file() and is_production_file(path))
hits: list[tuple[str, int, int, str]] = []
for path in files:
    relative = path.relative_to(source_root).as_posix()
    contents = path.read_text(encoding="utf-8")
    matches: list[tuple[int, str]] = []
    for pattern in (tailwind, css_or_object, jsx_attribute):
        matches.extend((match.start(1), match.group(1).lower()) for match in pattern.finditer(contents))
    for offset, value in sorted(set(matches)):
        line = contents.count("\n", 0, offset) + 1
        previous_newline = contents.rfind("\n", 0, offset)
        column = offset - previous_newline
        hits.append((relative, line, column, value))
hits.sort()

allowed: list[tuple[str, int, int, str, str]] = []
errors: list[str] = []
for number, raw in enumerate(allowlist_path.read_text(encoding="utf-8").splitlines(), 1):
    if not raw or raw.startswith("# path"):
        continue
    fields = raw.split("\t")
    if len(fields) != 5 or not all(fields):
        errors.append(f"invalid allowlist row {number}: expected path, line, column, value, and specific reason")
        continue
    path, raw_line, raw_column, value, reason = fields
    try:
        line, column = int(raw_line), int(raw_column)
    except ValueError:
        errors.append(f"invalid allowlist row {number}: line and column must be positive integers")
        continue
    if line < 1 or column < 1:
        errors.append(f"invalid allowlist row {number}: line and column must be positive integers")
        continue
    if value != value.lower() or not re.fullmatch(hex_value, value):
        errors.append(f"invalid allowlist row {number}: color must be normalized lowercase hex")
        continue
    allowed.append((path, line, column, value, reason))

allowed_locations = [(path, line, column, value) for path, line, column, value, _reason in allowed]
unexpected = Counter(hits) - Counter(allowed_locations)
stale = Counter(allowed_locations) - Counter(hits)
reasons = {(path, line, column, value): reason for path, line, column, value, reason in allowed}
if len(reasons) != len(allowed):
    errors.append("invalid allowlist: duplicate source identity; each occurrence requires exactly one reason")
for (path, line, column, value), count in sorted(unexpected.items()):
    errors.extend([f"unlisted hit: {path}:{line}:{column}\t{value}"] * count)
for (path, line, column, value), count in sorted(stale.items()):
    reason = reasons[(path, line, column, value)]
    errors.extend([f"stale allowlist entry: {path}:{line}:{column}\t{value}\t{reason}"] * count)

hit_files = sorted({path for path, _line, _column, _value in hits})
print(f"Hex scan receipt: denominator={len(files)} production files; hits={len(hits)}; files={len(hit_files)}")
print("Hex scan files: " + (", ".join(hit_files) if hit_files else "(none)"))
if errors:
    print("FAIL: production hex baseline mismatch:", file=sys.stderr)
    for error in errors:
        print(f"  {error}", file=sys.stderr)
    sys.exit(3)
PY
}

if [ "${1:-}" = "--scan-hex" ]; then
  scan_hex
  exit $?
fi

echo "==> Typecheck"
npm run typecheck

echo "==> ESLint"
npm run lint

echo "==> Tests"
npm test -- --run

echo "==> Build Storybook"
npm run build-storybook

echo "==> Production hex baseline"
scan_hex

echo "All design-system checks passed."
