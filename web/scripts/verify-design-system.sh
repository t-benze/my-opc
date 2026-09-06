#!/usr/bin/env bash
#
# CI gate for the web design system. Runs typecheck + lint + tests +
# deterministic Storybook static build + complete production-source colour scan.
#
# Exit codes:
#   0 — clean
#   1 — typecheck, lint, test, or build failure
#   3 — production colour baseline mismatch
#
# Run locally before pushing: `bash scripts/verify-design-system.sh`
set -euo pipefail

cd "$(dirname "$0")/.."

scan_colours() {
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
colour_properties = r"(?:color|background|border|fill|stroke|shadow|outline|decoration|accent|caret)"
colour_utilities = r"(?:bg|text|border|outline|ring|ring-offset|divide|decoration|accent|caret|fill|stroke|shadow|from|via|to|placeholder)"
palette = r"(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)"
named_colours = {
    "aliceblue", "antiquewhite", "aqua", "aquamarine", "azure", "beige", "bisque", "black",
    "blanchedalmond", "blue", "blueviolet", "brown", "burlywood", "cadetblue", "chartreuse",
    "chocolate", "coral", "cornflowerblue", "cornsilk", "crimson", "cyan", "darkblue", "darkcyan",
    "darkgoldenrod", "darkgray", "darkgreen", "darkgrey", "darkkhaki", "darkmagenta",
    "darkolivegreen", "darkorange", "darkorchid", "darkred", "darksalmon", "darkseagreen",
    "darkslateblue", "darkslategray", "darkslategrey", "darkturquoise", "darkviolet", "deeppink",
    "deepskyblue", "dimgray", "dimgrey", "dodgerblue", "firebrick", "floralwhite", "forestgreen",
    "fuchsia", "gainsboro", "ghostwhite", "gold", "goldenrod", "gray", "green", "greenyellow",
    "grey", "honeydew", "hotpink", "indianred", "indigo", "ivory", "khaki", "lavender",
    "lavenderblush", "lawngreen", "lemonchiffon", "lightblue", "lightcoral", "lightcyan",
    "lightgoldenrodyellow", "lightgray", "lightgreen", "lightgrey", "lightpink", "lightsalmon",
    "lightseagreen", "lightskyblue", "lightslategray", "lightslategrey", "lightsteelblue",
    "lightyellow", "lime", "limegreen", "linen", "magenta", "maroon", "mediumaquamarine",
    "mediumblue", "mediumorchid", "mediumpurple", "mediumseagreen", "mediumslateblue",
    "mediumspringgreen", "mediumturquoise", "mediumvioletred", "midnightblue", "mintcream",
    "mistyrose", "moccasin", "navajowhite", "navy", "oldlace", "olive", "olivedrab", "orange",
    "orangered", "orchid", "palegoldenrod", "palegreen", "paleturquoise", "palevioletred",
    "papayawhip", "peachpuff", "peru", "pink", "plum", "powderblue", "purple", "rebeccapurple",
    "red", "rosybrown", "royalblue", "saddlebrown", "salmon", "sandybrown", "seagreen",
    "seashell", "sienna", "silver", "skyblue", "slateblue", "slategray", "slategrey", "snow",
    "springgreen", "steelblue", "tan", "teal", "thistle", "tomato", "turquoise", "violet",
    "wheat", "white", "whitesmoke", "yellow", "yellowgreen",
}
# Any Tailwind utility (including variants and negative/important forms) whose
# arbitrary value is itself a hex color. Keeping the utility name structural
# covers the full color-capable vocabulary without treating issue references
# or HTML numeric entities as classes.
tailwind = re.compile(rf"(?<![\w-])(?:[a-z][\w-]*:)*!?-?[a-z][\w-]*-\[({hex_value})\](?![0-9a-fA-F])", re.IGNORECASE)
css_or_object = re.compile(rf"(?:^|[;{{,])\s*(?:--[\w-]+|[\w-]*(?:color|background|border|fill|stroke|shadow|outline|decoration|accent|caret)[\w-]*)\s*:\s*['\"]?({hex_value})(?![0-9a-fA-F])", re.IGNORECASE)
jsx_attribute = re.compile(rf"\b(?:color|fill|stroke)\s*=\s*(?:['\"]|{{\s*['\"])({hex_value})(?![0-9a-fA-F])", re.IGNORECASE)
jsx_colour_attribute = re.compile(r"\b(?:color|fill|stroke)\s*=\s*(?:['\"]|\{\s*['\"])([^'\"}\n]+)", re.IGNORECASE)
default_tailwind = re.compile(rf"(?<![\w-])(?:[a-z][\w-]*:)*!?-?({colour_utilities}-(?:(?:{palette})-\d{{2,3}}|black|white)(?:/\d{{1,3}})?)(?![\w-])", re.IGNORECASE)
arbitrary_tailwind = re.compile(rf"(?<![\w-])(?:[a-z][\w-]*:)*!?-?{colour_utilities}-\[([^\]\n]+)\]", re.IGNORECASE)
declaration = re.compile(rf"(?:^|[;{{,])\s*(?:--[\w-]+|[\w-]*{colour_properties}[\w-]*)\s*:\s*([^;}}\n]+)", re.IGNORECASE)
colour_function = re.compile(r"(?:rgb|rgba|hsl|hsla|oklch|oklab|color)\([^()]*\)", re.IGNORECASE)
word = re.compile(r"(?<![\w-])[a-z]+(?![\w-])", re.IGNORECASE)

def non_hex_value_matches(contents: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for match in default_tailwind.finditer(contents):
        found.append((match.start(1), match.group(1).lower()))
    for match in arbitrary_tailwind.finditer(contents):
        value = match.group(1)
        lowered = value.lower()
        normalized = lowered.replace("_", " ")
        functions = list(colour_function.finditer(normalized))
        if functions:
            for function in functions:
                found.append((match.start(1) + function.start(), lowered[function.start():function.end()]))
        elif lowered in named_colours:
            found.append((match.start(1), lowered))
    for match in declaration.finditer(contents):
        value = match.group(1)
        base = match.start(1)
        for function in colour_function.finditer(value):
            found.append((base + function.start(), function.group(0).lower()))
        for candidate in word.finditer(value):
            if candidate.group(0).lower() in named_colours:
                found.append((base + candidate.start(), candidate.group(0).lower()))
    for match in jsx_colour_attribute.finditer(contents):
        value = match.group(1)
        base = match.start(1)
        for function in colour_function.finditer(value):
            found.append((base + function.start(), function.group(0).lower()))
        if value.lower() in named_colours:
            found.append((base, value.lower()))
    return found

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
    matches.extend(non_hex_value_matches(contents))
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
    if value != value.lower() or any(character in value for character in "\t\r\n"):
        errors.append(f"invalid allowlist row {number}: value must be normalized lowercase scanner output")
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
print(f"Colour scan receipt: denominator={len(files)} production files; hits={len(hits)}; files={len(hit_files)}")
print("Colour scan files: " + (", ".join(hit_files) if hit_files else "(none)"))
if errors:
    print("FAIL: production colour baseline mismatch:", file=sys.stderr)
    for error in errors:
        print(f"  {error}", file=sys.stderr)
    sys.exit(3)
PY
}

if [ "${1:-}" = "--scan-hex" ]; then
  scan_colours
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

echo "==> Production colour baseline"
scan_colours

echo "All design-system checks passed."
