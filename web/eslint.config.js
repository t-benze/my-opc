// eslint.config.js — flat-config replacement for .eslintrc.
//
// Mirrors web/DESIGN_SYSTEM.md §10 with two intentional deviations:
//
//   1. `eslint-plugin-react` exposes its flat preset at `react.configs.flat
//      .recommended`, not `react.configs.recommended`. The spec name is
//      suggestive; we use the flat-config one.
//
//   2. `tailwindcss/no-arbitrary-value` is scoped to features/patterns
//      only. The shadcn-derived primitives under `src/design-system/
//      primitives/` carry upstream-canonical arbitrary values (min-w-[8rem]
//      on popovers, bg-black/60 on dialog scrim, etc). Forbidding them in
//      the primitive layer would mean forking shadcn templates with no
//      design payoff — the design system's job is to contain those tokens
//      in one place, which is what /primitives/ already is. Patterns and
//      features stay strict.
//
// `eslint-plugin-tailwindcss@3.18.3` declares peer support for `tailwindcss
// ^4.0.0`; running this config against Tailwind v4 (`@theme`-based) works
// in our smoke tests. If a future plugin update breaks parser-level on v4
// CSS, the verify-script's hex-code grep is the load-bearing backstop.

import tseslint from "typescript-eslint";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import tailwind from "eslint-plugin-tailwindcss";

const frozenFeatureArbitraryValues = [
  {
    file: "src/features/dreams/DreamsPage.tsx",
    value: "text-[10px]",
    count: 1,
    reason: "Known live status-pill residue; replacement is owned by the approved text-scale cleanup.",
  },
  {
    file: "src/features/dreams/DreamDetailPane.tsx",
    value: "text-[10px]",
    count: 1,
    reason: "Known live status-pill residue; replacement is owned by the approved text-scale cleanup.",
  },
  {
    file: "src/features/work-hours-config/WakesView.tsx",
    value: "text-[10px]",
    count: 1,
    reason: "Known live status-pill residue; replacement is owned by the approved text-scale cleanup.",
  },
  {
    file: "src/features/schedule/SchedulePage.tsx",
    value: "text-[10px]",
    count: 1,
    reason: "Known retired-source residue; removal is owned by the approved retirement cleanup.",
  },
];

const localBaselinePlugin = {
  rules: {
    "baselined-no-arbitrary-value": {
      meta: {
        ...tailwind.rules["no-arbitrary-value"].meta,
        schema: [{
          type: "object",
          required: ["value"],
          properties: { value: { type: "string" } },
          additionalProperties: false,
        }],
      },
      create(context) {
        const [{ value }] = context.options;
        const filteredContext = Object.create(context);
        Object.defineProperty(filteredContext, "report", {
          value(descriptor) {
            if (descriptor.data?.classname !== value) {
              context.report(descriptor);
            }
          },
        });
        return tailwind.rules["no-arbitrary-value"].create(filteredContext);
      },
    },
    "frozen-tailwind-arbitrary-baseline": {
      meta: {
        type: "problem",
        schema: [{
          type: "object",
          required: ["value", "count", "reason"],
          properties: {
            value: { type: "string" },
            count: { type: "integer", minimum: 0 },
            reason: { type: "string", minLength: 1 },
          },
          additionalProperties: false,
        }],
        messages: {
          changed: "Frozen arbitrary-value baseline for '{{value}}' expected {{expected}} occurrence(s), found {{actual}}. {{reason}}",
        },
      },
      create(context) {
        const [{ value, count, reason }] = context.options;
        return {
          Program(node) {
            const source = context.sourceCode.text;
            const actual = source.split(value).length - 1;
            if (actual !== count) {
              context.report({
                node,
                messageId: "changed",
                data: { value, expected: count, actual, reason },
              });
            }
          },
        };
      },
    },
  },
};

export default tseslint.config(
  {
    ignores: [
      "dist",
      "node_modules",
      "scripts/**",
      "vitest.setup.ts",
      "vite.config.ts",
      "**/*.config.js",
      "**/*.config.ts",
    ],
    // The v3 plugin cannot discover a config through Tailwind v4's package
    // exports. Keep one shared empty compatibility config so each linted file
    // reuses the plugin cache instead of rebuilding its v3 model from scratch.
    settings: {
      tailwindcss: {
        config: {},
        // Preserve the plugin defaults and add the repository's shadcn helper.
        // v3 inspects named call expressions in both .ts and .tsx files.
        callees: ["classnames", "clsx", "ctl", "cva", "tv", "cn"],
      },
    },
  },

  // Base TS + React (non-type-checked — the `tsc --noEmit` step covers full
  // type checking; running typescript-eslint's type-aware rules under
  // project=tsconfig.json doubles the wall-clock and adds zero unique
  // findings on this codebase).
  {
    files: ["src/**/*.{ts,tsx}"],
    extends: [
      ...tseslint.configs.recommended,
      react.configs.flat.recommended,
      react.configs.flat["jsx-runtime"],
    ],
    plugins: {
      "react-hooks": reactHooks,
    },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    settings: {
      react: { version: "detect" },
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "@typescript-eslint/no-explicit-any": "error",
      // Blocks `style` on both DOM nodes and component props — the design
      // system's single inline-style escape hatch is "don't".
      "react/forbid-component-props": ["error", { forbid: ["style"] }],
      "react/forbid-dom-props": ["error", { forbid: ["style"] }],
      // Existing files use legacy fragment / unescaped apostrophe patterns
      // we don't want to churn in this PR.
      "react/no-unescaped-entities": "off",
      // TypeScript supplies prop validation; prop-types is a JS-era check.
      "react/prop-types": "off",
      // The remaining `any`s in callbacks are typed unknown elsewhere; the
      // typecheck step catches genuine type holes.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },

  // Cross-feature import boundary — features may only import sanctioned roots.
  // Prototypes are deliberately exempt: their whole purpose is to re-render a
  // feature's composition against mock data (see PR 3 — `prototypes/threads-v2`
  // imports `ThreadsPage` to prove the harness keeps the page as the single
  // source of truth).
  {
    files: ["src/features/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-imports": ["error", {
        patterns: [
          {
            group: ["@/features/*/*", "@/features/*/**"],
            message: "Cross-feature imports are forbidden. Share through @/design-system/ or @/hooks/.",
          },
          {
            group: ["@/components", "@/components/*"],
            message: "@/components is deleted. Use @/design-system/primitives or /patterns.",
          },
          {
            // Deep imports into lib/api modules are forbidden; the bare
            // module `@/lib/api` (re-exporting `ApiError`) and the pure-types
            // module `@/lib/api/types` stay allowed.
            group: ["@/lib/api/*", "!@/lib/api/types"],
            message: "Compositions must use hooks from @/hooks/, not call lib/api directly.",
          },
        ],
      }],
    },
  },

  // Primitives may not import patterns/layouts/hooks/lib.
  {
    files: ["src/design-system/primitives/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-imports": ["error", {
        patterns: [
          { group: ["@/design-system/patterns/*"], message: "primitives may not import patterns" },
          { group: ["@/design-system/layouts/*"], message: "primitives may not import layouts" },
          { group: ["@/hooks/*"], message: "primitives are pure UI" },
          // The `cn` helper at `@/lib/utils` is allowed; ban everything else
          // under @/lib via a narrow pattern instead of a wildcard group.
          { group: ["@/lib/api", "@/lib/api/*", "@/lib/auth", "@/lib/orgSlug"], message: "primitives are pure UI — no data/auth deps" },
        ],
      }],
    },
  },

  // Patterns compose primitives (and sibling patterns) into props-in/JSX-out
  // composites. They may NOT reach up the layer stack (layouts, hooks,
  // features) or pull data/auth directly — those arrive as props. This is the
  // mechanical backstop for the layer-violation REVISE class: a pre-push
  // checklist runs once, but eslint runs on every push, including revises
  // (TaskCard -> useTasksRoutes slipped the checklist and was only caught at
  // REVISE r3).
  //
  // `allowTypeImports: true` on the lib group is load-bearing: patterns keep
  // their compile-erased `import type … from '@/lib/api/types'|'@/lib/api/agents'`
  // (8 patterns rely on these) while runtime VALUE imports from the same group
  // are still caught. Sibling-pattern composition (`@/design-system/patterns/*`)
  // is deliberately NOT banned — e.g. RecipientsInput imports MentionAutocomplete.
  {
    files: ["src/design-system/patterns/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-imports": ["error", {
        patterns: [
          { group: ["@/design-system/layouts/*"], message: "patterns may not import layouts" },
          { group: ["@/hooks/*"], message: "patterns are pure props-in/JSX-out — no hooks (data arrives as props)" },
          { group: ["@/features/*", "@/features/*/**"], message: "patterns may not reach up into features" },
          { group: ["@/lib/api", "@/lib/api/*", "@/lib/auth", "@/lib/orgSlug"], message: "patterns take data/auth via props, not direct imports", allowTypeImports: true },
        ],
      }],
    },
  },

  // Tailwind class hygiene (scoped — see header comment for why primitives
  // get a pass on no-arbitrary-value).
  //
  // eslint-plugin-tailwindcss v3's class-order resolver hangs against our
  // Tailwind v4 CSS-first setup, which intentionally has no v3-style config.
  // Keep the correctness rules below; disable only the incompatible ordering
  // rule until the plugin supports this setup.
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { tailwindcss: tailwind },
    rules: {
      "tailwindcss/no-contradicting-classname": "error",
      "tailwindcss/classnames-order": "off",
      // no-custom-classname is overly aggressive against our token-aliased
      // utilities (text-h1, text-overline, bg-surface-sunken etc.); the
      // hex-code grep in scripts/verify-design-system.sh is the real
      // safety rail.
      "tailwindcss/no-custom-classname": "off",
    },
  },

  // Strict no-arbitrary-value for the feature surface only. The
  // design-system tree is exempt:
  //   - primitives wrap upstream shadcn templates whose canonical class
  //     lists include arbitrary values (min-w-[8rem] on Radix popovers,
  //     bg-black/60 on dialog scrims).
  //   - patterns implement DESIGN.md component tokens that occasionally
  //     map to arbitrary values (KbdChip's inset box-shadow per the
  //     `components.kbd_chip.box_shadow` spec).
  // Features compose patterns and therefore should NEVER need to reach
  // for arbitrary values — that's where the rule earns its keep.
  {
    files: ["src/features/**/*.{ts,tsx}"],
    plugins: { tailwindcss: tailwind },
    rules: {
      "tailwindcss/no-arbitrary-value": "error",
    },
  },

  // Keep the instrument green while four pre-existing text-scale residues are
  // removed by their separately approved cleanup. Each exception is bound to
  // one exact file/value/count: additions fail, and deletions deliberately make
  // the baseline stale so this list must strictly shrink with the cleanup.
  ...frozenFeatureArbitraryValues.map(({ file, value, count, reason }) => ({
    files: [file],
    plugins: { local: localBaselinePlugin },
    rules: {
      "tailwindcss/no-arbitrary-value": "off",
      "local/baselined-no-arbitrary-value": ["error", { value }],
      "local/frozen-tailwind-arbitrary-baseline": ["error", { value, count, reason }],
    },
  })),
);
