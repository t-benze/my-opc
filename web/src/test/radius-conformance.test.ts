import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const designSystemRoot = join(here, "../design-system");
const tokenSource = join(designSystemRoot, "tokens/tokens.css");
const REFERENCE_SHA256 =
  "87e23c25c95b22bdd46570314f6c649667d787ef89128504dbb52905cd52045a";
const EXPECTED_DENOMINATOR = 23;

type RadiusClass =
  | "rounded"
  | "rounded-sm"
  | "rounded-md"
  | "rounded-lg"
  | "rounded-3xl"
  | "rounded-full";
type RadiusToken = "--radius-sm" | "--radius" | "--radius-lg" | "--radius-pill";
type RadiusRow = {
  id: string;
  component: string;
  region: string;
  referenceSelector: string;
  expected: RadiusClass;
  token: RadiusToken;
  source: string;
  startPattern: string;
  endPattern: string;
};
type ProductionObservation = {
  radius: RadiusClass;
  sourceProvenance: string;
};

/**
 * Concrete component/region denominator from the verified founder HTML and
 * seq. 61 local mappings. Expected values never derive from production.
 */
const RADIUS_CONTRACT: readonly RadiusRow[] = [
  {
    id: "button",
    component: "Button",
    region: "base and icon variants",
    referenceSelector: ".btn",
    expected: "rounded-sm",
    token: "--radius-sm",
    source: "primitives/Button.tsx",
    startPattern: "const\\s+buttonVariants\\s*=\\s*cva\\s*\\(",
    endPattern: "\\bvariants\\s*:",
  },
  {
    id: "input",
    component: "Input",
    region: "control",
    referenceSelector: ".inp",
    expected: "rounded-sm",
    token: "--radius-sm",
    source: "primitives/Input.tsx",
    startPattern: "<input\\b",
    endPattern: "\\bref\\s*=\\s*\\{\\s*ref\\s*\\}",
  },
  {
    id: "textarea",
    component: "Textarea",
    region: "control",
    referenceSelector: ".ta",
    expected: "rounded-sm",
    token: "--radius-sm",
    source: "primitives/Textarea.tsx",
    startPattern: "<textarea\\b",
    endPattern: "\\bref\\s*=\\s*\\{\\s*ref\\s*\\}",
  },
  {
    id: "select-trigger",
    component: "SelectTrigger",
    region: "trigger",
    referenceSelector: "select.inp",
    expected: "rounded-sm",
    token: "--radius-sm",
    source: "primitives/Select.tsx",
    startPattern: "const\\s+SelectTrigger\\s*=",
    endPattern: "SelectTrigger\\.displayName",
  },
  {
    id: "tooltip-content",
    component: "TooltipContent",
    region: "content",
    referenceSelector: ".tip::after",
    expected: "rounded-sm",
    token: "--radius-sm",
    source: "primitives/Tooltip.tsx",
    startPattern: "const\\s+TooltipContent\\s*=",
    endPattern: "TooltipContent\\.displayName",
  },
  {
    id: "mention-textarea",
    component: "MentionTextarea",
    region: "control",
    referenceSelector: ".ta",
    expected: "rounded-sm",
    token: "--radius-sm",
    source: "patterns/MentionTextarea.tsx",
    startPattern: "const\\s+DEFAULT_CLASSNAME\\s*=",
    endPattern: "export\\s+interface\\s+MentionTextareaProps",
  },
  {
    id: "sidebar-footer-account",
    component: "Sidebar footer account row",
    region: "interactive row",
    referenceSelector: ".nav-item",
    expected: "rounded-sm",
    token: "--radius-sm",
    source: "layouts/AppShell/Sidebar.tsx",
    startPattern: 'aria-label\\s*=\\s*"Account: You, Founder"',
    endPattern: '<span\\s+aria-hidden\\s*=\\s*"true"',
  },
  {
    id: "sidebar-nav-disabled",
    component: "SidebarNavItem disabled branch",
    region: "interactive row",
    referenceSelector: ".nav-item",
    expected: "rounded-sm",
    token: "--radius-sm",
    source: "layouts/AppShell/Sidebar.tsx",
    startPattern: "if\\s*\\(\\s*!enabled\\s*\\)",
    endPattern: 'aria-disabled\\s*=\\s*"true"',
  },
  {
    id: "sidebar-nav-enabled",
    component: "SidebarNavItem enabled branch",
    region: "interactive row",
    referenceSelector: ".nav-item",
    expected: "rounded-sm",
    token: "--radius-sm",
    source: "layouts/AppShell/Sidebar.tsx",
    startPattern: "return\\s*\\(\\s*<NavLink\\b",
    endPattern: "<Icon\\s+size\\s*=\\s*\\{16\\}",
  },
  {
    id: "status-badge",
    component: "StatusBadge",
    region: "pill shell",
    referenceSelector: ".tag",
    expected: "rounded-full",
    token: "--radius-pill",
    source: "patterns/StatusBadge.tsx",
    startPattern: "<span\\s+className=\\{\`text-mono-sm",
    endPattern: "\\$\\{cls\\}\`\\}",
  },
  {
    id: "agent-chip",
    component: "AgentChip",
    region: "role indicator",
    referenceSelector: ".chip-agent",
    expected: "rounded-full",
    token: "--radius-pill",
    source: "patterns/AgentChip.tsx",
    startPattern: 'aria-hidden="true"',
    endPattern: "\\$\\{DOT_BG\\[role\\]\\}\`",
  },
  {
    id: "tabs-segmented-shell",
    component: "Tabs",
    region: "segmented shell",
    referenceSelector: ".seg",
    expected: "rounded-full",
    token: "--radius-pill",
    source: "primitives/Tabs.tsx",
    startPattern: "segmented:\\s*",
    endPattern: "\\n\\s*\\}",
  },
  {
    id: "tabs-segmented-trigger",
    component: "Tabs",
    region: "segmented trigger",
    referenceSelector: ".seg button",
    expected: "rounded-full",
    token: "--radius-pill",
    source: "primitives/Tabs.tsx",
    startPattern: "segmented:\\s*\\n\\s*'text-text-muted",
    endPattern: "\\n\\s*\\}",
  },
  {
    id: "dialog-content",
    component: "Dialog",
    region: "content shell",
    referenceSelector: ".modal",
    expected: "rounded-lg",
    token: "--radius-lg",
    source: "primitives/Dialog.tsx",
    startPattern: "<DialogPrimitive\\.Content",
    endPattern: "\\{children\\}",
  },
  {
    id: "dropdown-subcontent-shell",
    component: "DropdownMenu",
    region: "subcontent shell",
    referenceSelector: ".popover",
    expected: "rounded-md",
    token: "--radius",
    source: "primitives/DropdownMenu.tsx",
    startPattern: "const\\s+DropdownMenuSubContent",
    endPattern: "DropdownMenuSubContent\\.displayName",
  },
  {
    id: "dropdown-content-shell",
    component: "DropdownMenu",
    region: "content shell",
    referenceSelector: ".popover",
    expected: "rounded-md",
    token: "--radius",
    source: "primitives/DropdownMenu.tsx",
    startPattern: "const\\s+DropdownMenuContent",
    endPattern: "DropdownMenuContent\\.displayName",
  },
  {
    id: "dropdown-subtrigger",
    component: "DropdownMenu",
    region: "subtrigger item",
    referenceSelector: ".popover .opt",
    expected: "rounded-sm",
    token: "--radius-sm",
    source: "primitives/DropdownMenu.tsx",
    startPattern: "const\\s+DropdownMenuSubTrigger",
    endPattern: "DropdownMenuSubTrigger\\.displayName",
  },
  {
    id: "dropdown-item",
    component: "DropdownMenu",
    region: "ordinary item",
    referenceSelector: ".popover .opt",
    expected: "rounded-sm",
    token: "--radius-sm",
    source: "primitives/DropdownMenu.tsx",
    startPattern: "const\\s+DropdownMenuItem",
    endPattern: "DropdownMenuItem\\.displayName",
  },
  {
    id: "dropdown-checkbox-item",
    component: "DropdownMenu",
    region: "checkbox item",
    referenceSelector: ".popover .opt",
    expected: "rounded-sm",
    token: "--radius-sm",
    source: "primitives/DropdownMenu.tsx",
    startPattern: "const\\s+DropdownMenuCheckboxItem",
    endPattern: "DropdownMenuCheckboxItem\\.displayName",
  },
  {
    id: "message-bubble",
    component: "MessageBubble",
    region: "founder, worker, manager, and decline shells",
    referenceSelector: ".hr-bubble",
    expected: "rounded-lg",
    token: "--radius-lg",
    source: "patterns/MessageBubble.tsx",
    startPattern: "const\\s+VARIANT_CONTAINER",
    endPattern: "system:\\s*''",
  },
  {
    id: "typing-bubble",
    component: "TypingBubble",
    region: "bubble shell",
    referenceSelector: ".reply-bubble, .typing",
    expected: "rounded-lg",
    token: "--radius-lg",
    source: "patterns/TypingBubble.tsx",
    startPattern: '<div\\s+className="border-border-default',
    endPattern: ">",
  },
  {
    id: "composer",
    component: "Composer",
    region: "interactive shell",
    referenceSelector: ".composer (seq. 61 local extension)",
    expected: "rounded-lg",
    token: "--radius-lg",
    source: "patterns/Composer.tsx",
    startPattern:
      '<div\\s+className="border-border-default bg-surface-raised focus-within',
    endPattern: ">",
  },
  {
    id: "inbox-row",
    component: "InboxRow",
    region: "interactive shell",
    referenceSelector: ".thread (seq. 61 interactive mapping)",
    expected: "rounded-sm",
    token: "--radius-sm",
    source: "patterns/InboxRow.tsx",
    startPattern: "const\\s+shellCls\\s*=",
    endPattern: "\\$\\{",
  },
] as const;

/**
 * Radius-bearing source lines in the approved files that are nested decoration,
 * native controls, or other independently mapped components. They are inventory
 * evidence, not visual failures and never enlarge the seq. 76 denominator.
 */
const NON_DENOMINATOR_SOURCE_LINES = new Map<string, readonly RegExp[]>([
  ["primitives/Select.tsx", [/max-h-96.*rounded-md/, /w-full.*rounded-sm/]],
  ["layouts/AppShell/Sidebar.tsx", [/h-6 w-6.*rounded-full/]],
  ["patterns/StatusBadge.tsx", [/h-1\.5 w-1\.5.*rounded-full/]],
  ["primitives/Tabs.tsx", [/data-\[state=active\].*rounded-md/]],
  ["primitives/Dialog.tsx", [/absolute top-3 right-3 rounded/]],
  [
    "patterns/MessageBubble.tsx",
    [/mx-auto.*rounded-full/, /max-w-full.*rounded-md/],
  ],
  ["patterns/TypingBubble.tsx", [/h-2 w-2 rounded-full/]],
  [
    "patterns/Composer.tsx",
    [/max-w-full.*rounded-md/, /h-9 w-9.*rounded-full/, /h-9 w-9.*rounded-lg/],
  ],
  [
    "patterns/InboxRow.tsx",
    [
      /bg-accent-soft.*rounded-full/,
      /w-0\.5 rounded-full/,
      /h-2 w-2.*rounded-full/,
      /items-center rounded-full/,
      /h-1\.5 w-1\.5.*rounded-full/,
    ],
  ],
]);
/** Exact pre-correction debt. This map must strictly shrink. */
const KNOWN_MISMATCHES = new Map<string, RadiusClass>();
/** No remaining exclusion is allowed to make a production-radius claim. */
const NON_DENOMINATOR_EXCLUSIONS = new Map<string, string>();

function readRadiusToken(property: RadiusToken): string {
  const source = readFileSync(tokenSource, "utf8").replace(
    /\/\*[\s\S]*?\*\//g,
    "",
  );
  const definitions = [
    ...source.matchAll(/(?:^|\n)\s*(--[a-z0-9-]+)\s*:\s*([^;]+?)\s*;/g),
  ].filter((match) => match[1] === property);
  if (definitions.length !== 1)
    throw new Error(`${property}: expected exactly one token definition`);
  const value = definitions[0][2].trim();
  const alias = /^var\((--[a-z0-9-]+)\)$/.exec(value)?.[1];
  return alias ? readRadiusToken(alias as RadiusToken) : value;
}

function regionBounds(
  row: RadiusRow,
  source: string,
): readonly [number, number] {
  const startMatch = new RegExp(row.startPattern).exec(source);
  if (!startMatch)
    throw new Error(`${row.id}: missing start anchor in ${row.source}`);
  const start = startMatch.index;
  const endMatch = new RegExp(row.endPattern).exec(
    source.slice(start + startMatch[0].length),
  );
  if (!endMatch)
    throw new Error(`${row.id}: missing end anchor in ${row.source}`);
  const end = start + startMatch[0].length + endMatch.index;
  return [start, end];
}

function observeRadiusInSource(row: RadiusRow, source: string): RadiusClass {
  const [start, end] = regionBounds(row, source);
  const tokens =
    source.slice(start, end).match(/\brounded(?:-[a-z0-9[\].]+)?\b/g) ?? [];
  const unique = [...new Set(tokens)];
  if (tokens.length === 0 || unique.length !== 1)
    throw new Error(
      `${row.id}: expected one authoritative radius across the region, found ${tokens.join(", ") || "none"}`,
    );
  return unique[0] as RadiusClass;
}

function withoutComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, (comment) => " ".repeat(comment.length))
    .replace(/(^|\n)\s*\/\/[^\n]*/g, (comment) => " ".repeat(comment.length));
}

function discoverProductionObservations(
  sourceOverrides: ReadonlyMap<string, string> = new Map(),
): Map<string, ProductionObservation> {
  const rowsBySource = new Map<string, RadiusRow[]>();
  for (const row of RADIUS_CONTRACT) {
    const rows = rowsBySource.get(row.source) ?? [];
    rows.push(row);
    rowsBySource.set(row.source, rows);
  }
  const observations = new Map<string, ProductionObservation>();
  for (const [sourcePath, rows] of rowsBySource) {
    const source =
      sourceOverrides.get(sourcePath) ??
      readFileSync(join(designSystemRoot, sourcePath), "utf8");
    const searchable = withoutComments(source);
    const bounds = rows.map((row) => ({
      row,
      bounds: regionBounds(row, source),
    }));
    for (const match of searchable.matchAll(
      /\brounded(?:-[a-z0-9[\].]+)?\b/g,
    )) {
      const index = match.index;
      const owners = bounds.filter(
        ({ bounds: [start, end] }) => index >= start && index < end,
      );
      if (owners.length > 1)
        throw new Error(
          `${sourcePath}: ambiguous radius region at byte ${index}: ${owners.map(({ row }) => row.id).join(", ")}`,
        );
      if (owners.length === 1) continue;
      const lineStart = source.lastIndexOf("\n", index) + 1;
      const nextNewline = source.indexOf("\n", index);
      const line = source.slice(
        lineStart,
        nextNewline < 0 ? source.length : nextNewline,
      );
      const excluded = (
        NON_DENOMINATOR_SOURCE_LINES.get(sourcePath) ?? []
      ).filter((pattern) => pattern.test(line));
      if (excluded.length > 1)
        throw new Error(
          `${sourcePath}: ambiguous non-denominator radius at byte ${index}`,
        );
      if (excluded.length === 0) {
        const lineNumber = source.slice(0, index).split("\n").length;
        const identity = `unmapped:${sourcePath}:${lineNumber}`;
        if (observations.has(identity))
          throw new Error(`${identity}: duplicate discovered radius region`);
        observations.set(identity, {
          radius: match[0] as RadiusClass,
          sourceProvenance: `${sourcePath}:${lineNumber}:${line.trim()}`,
        });
      }
    }
    for (const row of rows) {
      observations.set(row.id, {
        radius: observeRadiusInSource(row, source),
        sourceProvenance: `${row.source} / ${row.startPattern} .. ${row.endPattern}`,
      });
    }
  }
  return observations;
}

function assertRadiusContract(
  rows: readonly RadiusRow[],
  baseline: ReadonlyMap<string, RadiusClass>,
  observations: ReadonlyMap<string, ProductionObservation>,
): void {
  const ids = rows.map(({ id }) => id);
  if (
    rows.length !== EXPECTED_DENOMINATOR ||
    new Set(ids).size !== EXPECTED_DENOMINATOR
  )
    throw new Error(
      `radius denominator must contain exactly ${EXPECTED_DENOMINATOR} unique rows`,
    );
  const unmapped = [...observations.keys()].filter((id) => !ids.includes(id));
  if (unmapped.length)
    throw new Error(
      `production observation has no denominator row: ${unmapped.join(", ")}`,
    );
  for (const row of rows) {
    const observation = observations.get(row.id);
    if (!observation)
      throw new Error(`${row.id}: production observation missing`);
    if (!observation.sourceProvenance.startsWith(`${row.source} / `))
      throw new Error(`${row.id}: production source provenance changed`);
    const observed = observation.radius;
    const frozen = baseline.get(row.id);
    if (observed === row.expected) {
      if (frozen)
        throw new Error(
          `${row.id}: stale mismatch baseline must be deleted after correction`,
        );
    } else if (!frozen)
      throw new Error(`${row.id}: current mismatch omitted from baseline`);
    else if (frozen !== observed)
      throw new Error(
        `${row.id}: observed mismatch drifted from ${frozen} to ${observed}`,
      );
  }
  for (const id of baseline.keys())
    if (!ids.includes(id))
      throw new Error(`${id}: baseline entry has no denominator row`);
}

const productionObservations = new Map(discoverProductionObservations());

function withObservedRadius(
  observations: ReadonlyMap<string, ProductionObservation>,
  id: string,
  radius: RadiusClass,
): Map<string, ProductionObservation> {
  const changed = new Map(observations);
  const current = changed.get(id);
  if (!current) throw new Error(`${id}: cannot mutate absent observation`);
  changed.set(id, { ...current, radius });
  return changed;
}

describe("Pasture radius conformance contract", () => {
  test("pins authoritative token resolution per concrete row", () => {
    expect(REFERENCE_SHA256).toBe(
      "87e23c25c95b22bdd46570314f6c649667d787ef89128504dbb52905cd52045a",
    );
    const expected = new Map<RadiusToken, string>([
      ["--radius-sm", "8px"],
      ["--radius", "12px"],
      ["--radius-lg", "18px"],
      ["--radius-pill", "999px"],
    ]);
    expect(
      new Map(
        [...new Set(RADIUS_CONTRACT.map(({ token }) => token))].map((token) => [
          token,
          readRadiusToken(token),
        ]),
      ),
    ).toEqual(expected);
  });
  test("pins approved-pill provenance to the verified standalone selectors", () => {
    expect(
      RADIUS_CONTRACT.find(({ id }) => id === "status-badge")
        ?.referenceSelector,
    ).toBe(".tag");
    expect(
      RADIUS_CONTRACT.find(({ id }) => id === "agent-chip")?.referenceSelector,
    ).toBe(".chip-agent");
    expect(
      RADIUS_CONTRACT.find(({ id }) => id === "tabs-segmented-shell")
        ?.referenceSelector,
    ).toBe(".seg");
  });
  test("pins the complete denominator and shrinking baseline", () => {
    expect(RADIUS_CONTRACT).toHaveLength(EXPECTED_DENOMINATOR);
    expect(KNOWN_MISMATCHES).toHaveLength(0);
    expect(NON_DENOMINATOR_EXCLUSIONS.size).toBe(0);
    expect(KNOWN_MISMATCHES.has("button")).toBe(false);
    expect(() =>
      assertRadiusContract(
        RADIUS_CONTRACT,
        KNOWN_MISMATCHES,
        productionObservations,
      ),
    ).not.toThrow();
  });
  test("requires strict baseline shrink after a correction", () => {
    const staleBaseline = new Map(KNOWN_MISMATCHES);
    staleBaseline.set("sidebar-footer-account", "rounded");
    staleBaseline.set("sidebar-nav-disabled", "rounded");
    staleBaseline.set("sidebar-nav-enabled", "rounded");
    expect(() =>
      assertRadiusContract(RADIUS_CONTRACT, staleBaseline, productionObservations),
    ).toThrow(/stale mismatch baseline/);
    expect(() =>
      assertRadiusContract(RADIUS_CONTRACT, KNOWN_MISMATCHES, productionObservations),
    ).not.toThrow();
  });
  test.each([
    "status-badge",
    "agent-chip",
    "tabs-segmented-shell",
    "tabs-segmented-trigger",
    "dialog-content",
    "dropdown-subcontent-shell",
    "dropdown-content-shell",
    "dropdown-subtrigger",
    "dropdown-item",
    "dropdown-checkbox-item",
    "message-bubble",
    "typing-bubble",
    "button",
  ])("rejects protected-region drift for %s", (id) => {
    const drifted = withObservedRadius(
      productionObservations,
      id,
      "rounded-3xl",
    );
    expect(() =>
      assertRadiusContract(RADIUS_CONTRACT, KNOWN_MISMATCHES, drifted),
    ).toThrow(new RegExp(`${id}: current mismatch omitted`));
  });
  test.each([
    ["message-bubble", "rounded-sm"],
    ["typing-bubble", "rounded-sm"],
    ["composer", "rounded-3xl"],
    ["inbox-row", "rounded-lg"],
  ] as const)("protects the independent final mapping for %s", (id, wrongRadius) => {
    const drifted = withObservedRadius(
      productionObservations,
      id,
      wrongRadius,
    );
    expect(() =>
      assertRadiusContract(RADIUS_CONTRACT, KNOWN_MISMATCHES, drifted),
    ).toThrow(new RegExp(`${id}: current mismatch omitted`));
  });
  test("rejects Composer and InboxRow wrong values or reintroduced baselines", () => {
    const inboxWrong = withObservedRadius(
      productionObservations,
      "inbox-row",
      "rounded-md",
    );
    expect(() =>
      assertRadiusContract(RADIUS_CONTRACT, KNOWN_MISMATCHES, inboxWrong),
    ).toThrow(/inbox-row: current mismatch omitted/);
    const stale = new Map(KNOWN_MISMATCHES);
    stale.set("composer", "rounded-3xl");
    expect(() =>
      assertRadiusContract(RADIUS_CONTRACT, stale, productionObservations),
    ).toThrow(/composer: stale mismatch baseline/);
  });
  test("fails closed on provenance, observation, and denominator changes", () => {
    const remapped = new Map(productionObservations);
    const input = remapped.get("input")!;
    remapped.set("input", { ...input, sourceProvenance: "wrong/source" });
    expect(() =>
      assertRadiusContract(RADIUS_CONTRACT, KNOWN_MISMATCHES, remapped),
    ).toThrow(/source provenance changed/);
    const missing = new Map(productionObservations);
    missing.delete("button");
    expect(() =>
      assertRadiusContract(RADIUS_CONTRACT, KNOWN_MISMATCHES, missing),
    ).toThrow(/production observation missing/);
    expect(() =>
      assertRadiusContract(
        [...RADIUS_CONTRACT.slice(0, -1), RADIUS_CONTRACT[0]],
        KNOWN_MISMATCHES,
        productionObservations,
      ),
    ).toThrow(/23 unique rows/);
    expect(() =>
      assertRadiusContract(
        RADIUS_CONTRACT.slice(0, -1),
        KNOWN_MISMATCHES,
        productionObservations,
      ),
    ).toThrow(/23 unique rows/);
  });
  test("discovers an unmapped radius added to an actual approved production source", () => {
    const sourcePath = "patterns/AgentChip.tsx";
    const productionSource = readFileSync(
      join(designSystemRoot, sourcePath),
      "utf8",
    );
    const changedSource = `${productionSource}\nexport const RadiusDiscoveryFixture = () => (\n  <div className="rounded-sm">new visual region</div>\n);\n`;
    const discovered = discoverProductionObservations(
      new Map([[sourcePath, changedSource]]),
    );
    expect([...discovered.keys()]).toContain(
      `unmapped:${sourcePath}:${changedSource.split("\n").length - 2}`,
    );
    expect(() =>
      assertRadiusContract(RADIUS_CONTRACT, KNOWN_MISMATCHES, discovered),
    ).toThrow(/production observation has no denominator row/);
  });
  test("fails when an approved production source region disappears", () => {
    const sourcePath = "patterns/AgentChip.tsx";
    const productionSource = readFileSync(
      join(designSystemRoot, sourcePath),
      "utf8",
    );
    const changedSource = productionSource.replace("rounded-full", "");
    expect(() =>
      discoverProductionObservations(new Map([[sourcePath, changedSource]])),
    ).toThrow(/agent-chip: expected one authoritative radius.*none/);
  });
  test("rejects silent mismatch-baseline growth", () => {
    const grown = new Map(KNOWN_MISMATCHES);
    grown.set("button", "rounded-md");
    expect(() =>
      assertRadiusContract(RADIUS_CONTRACT, grown, productionObservations),
    ).toThrow(/button: stale mismatch baseline/);
  });
});
