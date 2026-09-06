#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

scanner="scripts/verify-design-system.sh"
fixture="scripts/fixtures/design-system-hex-scanner"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

run_scan() {
  DESIGN_SYSTEM_SOURCE_ROOT="$1" DESIGN_SYSTEM_HEX_ALLOWLIST="$2" \
    bash "$scanner" --scan-hex
}

cp -R "$fixture/src" "$scratch/src"
cp "$fixture/allowlist.tsv" "$scratch/allowlist.tsv"

receipt=$(run_scan "$scratch/src" "$scratch/allowlist.tsv")
grep -F 'Colour scan receipt: denominator=2 production files; hits=29; files=2' <<<"$receipt"
grep -F 'Colour scan files: App.tsx, theme.css' <<<"$receipt"

cp -R "$fixture/src" "$scratch/relocated-src"
sed '1i\\' "$scratch/relocated-src/App.tsx" > "$scratch/App.relocated"
mv "$scratch/App.relocated" "$scratch/relocated-src/App.tsx"
if run_scan "$scratch/relocated-src" "$scratch/allowlist.tsv" >"$scratch/relocated.out" 2>&1; then
  echo 'expected relocating an allowed literal within the same file to fail' >&2
  exit 1
fi
grep -F 'unlisted hit: App.tsx' "$scratch/relocated.out"
grep -F 'stale allowlist entry: App.tsx' "$scratch/relocated.out"

printf '\nexport const gradients = <div className="from-[#112233] via-[#223344] to-[#334455] divide-[#445566] placeholder-[#556677] ring-offset-[#667788]" />\n' >> "$scratch/src/App.tsx"
if run_scan "$scratch/src" "$scratch/allowlist.tsv" >"$scratch/arbitrary.out" 2>&1; then
  echo 'expected every arbitrary Tailwind color utility to fail when unlisted' >&2
  exit 1
fi
for value in '#112233' '#223344' '#334455' '#445566' '#556677' '#667788'; do
  grep -F "unlisted hit: App.tsx" "$scratch/arbitrary.out" | grep -F "$value"
done

# Restore the canonical fixture before the remaining mutation cases.
cp "$fixture/src/App.tsx" "$scratch/src/App.tsx"

printf '\nexport const added = <div className="border-[#445566]" />\n' >> "$scratch/src/App.tsx"
added_line=$(wc -l < "$scratch/src/App.tsx" | tr -d ' ')
if run_scan "$scratch/src" "$scratch/allowlist.tsv" >"$scratch/new.out" 2>&1; then
  echo 'expected an unlisted production hit to fail' >&2
  exit 1
fi
grep -F 'unlisted hit: App.tsx' "$scratch/new.out"
grep -F '#445566' "$scratch/new.out"

printf 'App.tsx\t%s\t46\t#445566\tfixture added hit\n' "$added_line" >> "$scratch/allowlist.tsv"
run_scan "$scratch/src" "$scratch/allowlist.tsv" >/dev/null

printf '\nexport const addedPalette = <div className="hover:text-rose-600" />\n' >> "$scratch/src/App.tsx"
if run_scan "$scratch/src" "$scratch/allowlist.tsv" >"$scratch/palette.out" 2>&1; then
  echo 'expected an unlisted default palette utility to fail' >&2
  exit 1
fi
grep -F 'text-rose-600' "$scratch/palette.out"

printf '\nexport const duplicate = <div className="border-[#445566]" />\n' >> "$scratch/src/App.tsx"
if run_scan "$scratch/src" "$scratch/allowlist.tsv" >"$scratch/duplicate.out" 2>&1; then
  echo 'expected a duplicated hit to fail' >&2
  exit 1
fi
grep -F 'unlisted hit: App.tsx' "$scratch/duplicate.out"

cp "$fixture/src/App.tsx" "$scratch/src/App.tsx"
cp "$fixture/allowlist.tsv" "$scratch/allowlist.tsv"
sed 's/#123456/#123457/' "$scratch/src/App.tsx" > "$scratch/App.next"
mv "$scratch/App.next" "$scratch/src/App.tsx"
if run_scan "$scratch/src" "$scratch/allowlist.tsv" >"$scratch/changed.out" 2>&1; then
  echo 'expected changed values and a stale allowlist row to fail' >&2
  exit 1
fi
grep -F 'unlisted hit: App.tsx' "$scratch/changed.out"
grep -F 'stale allowlist entry: App.tsx' "$scratch/changed.out"

cp "$fixture/src/App.tsx" "$scratch/src/App.tsx"
cp "$fixture/allowlist.tsv" "$scratch/allowlist.tsv"
sed 's/#010203/none   /' "$scratch/src/App.tsx" > "$scratch/App.next"
mv "$scratch/App.next" "$scratch/src/App.tsx"
if run_scan "$scratch/src" "$scratch/allowlist.tsv" >"$scratch/stale.out" 2>&1; then
  echo 'expected a deleted legacy hit to make its allowlist row stale' >&2
  exit 1
fi
grep -F 'stale allowlist entry: App.tsx' "$scratch/stale.out"

grep -v '#010203' "$scratch/allowlist.tsv" > "$scratch/allowlist-next.tsv"
mv "$scratch/allowlist-next.tsv" "$scratch/allowlist.tsv"
run_scan "$scratch/src" "$scratch/allowlist.tsv" >/dev/null

echo 'design-system hex scanner tests passed'
