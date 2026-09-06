# HappyRanch web design system

This is the normative contract for reusable UI under `src/design-system/`.
Feature compositions keep using the stable `@/design-system/...` import paths.

## Tokens and boundaries

- `src/design-system/tokens/tokens.css` is the source of new raw colors.
- `brand-foreground` is the single semantic foreground for the HappyRanch
  lockup. Its explicit light/dark values are held to at least 4.5:1 against
  the live Sidebar (`bg-bg-subtle`) and Onboarding (`bg-surface-canvas`)
  backgrounds; it is intentionally distinct from the generic UI accent.
  Components and stories consume semantic utilities. The deterministic
  full-production-tree check in `scripts/verify-design-system.sh` rejects raw
  hex, actual default Tailwind palette scales and special colour utilities,
  arbitrary Tailwind colour values,
  CSS `rgb()`/`rgba()`/`hsl()`/`hsla()`/`oklch()`/`oklab()`/`color()` values,
  and complete named CSS colour values outside that authority. Supported
  colour functions are balanced and may contain nested channel functions such
  as `var()`. It recognizes only
  colour-capable utilities, properties, and JSX attributes, so issue references,
  entities, non-colour arbitrary utilities, and token utilities are not hits.
  The exact path/line/column/value/reason baseline is shrinking: moving,
  changing, duplicating, adding, or deleting a listed occurrence fails until
  the baseline is deliberately reconciled in review. A baseline row records
  temporary residue; it does not create visual or token authority.
- Primitives wrap basic interaction and Radix behavior. Patterns combine them
  into reusable product language. Layouts own reusable geometry.
- Components are pure props-in/events-out unless a documented role requires an
  isolation-safe local provider. Reuse them instead of feature-local copies.

## Storybook is the catalogue

Storybook supersedes the former committed JSON registry and in-app
`/__design__` route. There is no generated catalogue, registry generator,
route flag, daemon endpoint, MCP transport, or on-disk agent contract.
`npm run dev` and `npm run build` build only the product SPA.

```bash
npm run storybook
npm run build-storybook
```

The deterministic static output is `web/storybook-static/`, a local CI
artifact rather than a hosted service. Chromatic and similar services are not
used. `.storybook/main.ts` uses React-Vite, the application `@` alias, and
Essentials for controls/docs. `.storybook/preview.tsx` imports `src/styles.css`
and supplies only `MemoryRouter`, a network-disabled Query client, and
`TooltipProvider`. Stories use canned data and never contact a live daemon.
The theme toolbar applies the same `data-theme` contract as the app and defaults
to the light Pasture reference. The `Pasture/*` navigation is the specification
crosswalk. `PASTURE_REFERENCE_REQUIREMENTS` is the single authoritative 25-row
manifest, matching the 25 material sections in the rendered founder HTML. Every
row names a concrete story export and stable rendered semantic selectors.
Timeline and table, avatars/agent rows, and icon tiles are separate requirements.
The manifest also enforces the five page states, assistant dock, review-only
prototype-state selector, 42-entry retired-to-canonical rename map, 55-entry
screen-scoped composition inventory, and narrow-width contract. The existing
`Design System/*` stories remain the
source-to-story identity ledger and detailed component demonstrations. Manager
chrome and canvases use the warm Pasture surface treatment; canvas padding and
examples reflow at 390 CSS px.

## Authoring and enforced coverage

Use CSF `*.stories.tsx`. Each new reusable `.tsx` file in `primitives/`,
`patterns/`, or `layouts/` needs either:

1. an explicit `[source:path#Export] [story:path#Export]` ledger mapping. The
   mapped CSF module's default meta must use the exact runtime source-export
   object as `meta.component`, and the mapped named story must use Storybook's
   default component render (no custom `render` override); or
2. a component-specific technical exclusion below explaining why isolation is
   unsafe/misleading and where the behavior is covered instead.

Use controls/autodocs for real public variants. Interaction stories must be safe
to click/type. Add loading, empty, error, populated, auth, and permission states
only where the reusable unit owns them. Aggregate stories preserve representative
states, controls, and interaction examples; their custom renders do not satisfy
coverage. `storybook-coverage.test.ts` uses Vite's existing module graph to
import each mapped source and story module and compare runtime object identity.
It also requires a mapped named default-render story. The same test renders every
entry in `PASTURE_REFERENCE_REQUIREMENTS` and queries its declared DOM structure
and semantic attributes. Adversarial fixtures prove that a combined
"Timeline & tables" heading without a timeline and generic implementation
guidance without the prototype selector fail. This narrow contract fails
closed for missing modules/exports/meta, wrong or merely same-named bindings,
metadata-only references, and custom render overrides. It does not claim to
interpret JavaScript or TypeScript execution, and creates no generated registry
or runtime catalogue. Reference page states are explicit and fail-closed in the
same test: empty, loading, error/retry, unauthorized, and populated. Navigation
ordering and the 390px manager/canvas responsive contract are independently
asserted.
Unauthorized is a visual/copy proposal only because reusable units do not own
auth or permission decisions; no gate is simulated. Loading is a local skeleton
and retry is local state, so Storybook remains daemon-isolated.

## Coverage ledger

**44 reusable components: 41 story-covered, 3 justified exclusions.** Stories
preserve the former catalogue's descriptions, examples, variants, and token
visibility through titles, docs, controls, and representative renders.

| Reusable export | Story or exclusion | Representative states / variants | Tokens |
|---|---|---|---|
| `Button` | [source:primitives/Button.tsx#Button] [story:primitives/Button.coverage.stories.tsx#Coverage] Primitives / Button States | default/secondary/outline/ghost/destructive/destructive-outline/link; default/sm/lg/icon; loading, disabled, icon, focus, `asChild` | `components.button` |
| `Dialog` | [source:primitives/Dialog.tsx#Dialog] [story:primitives/Dialog.coverage.stories.tsx#Coverage] Primitives / Dialog Interaction | trigger → portal | `components.dialog` |
| `Drawer` | [source:primitives/Drawer.tsx#Drawer] [story:primitives/Drawer.coverage.stories.tsx#Coverage] Primitives / Drawer Interaction | trigger → drawer | `components.drawer` |
| `DropdownMenu` | [source:primitives/DropdownMenu.tsx#DropdownMenu] [story:primitives/DropdownMenu.coverage.stories.tsx#Coverage] Primitives / Dropdown Menu Interaction | trigger, populated actions | `components.dropdown_menu` |
| `Input` | [source:primitives/Input.tsx#Input] [story:primitives/Input.coverage.stories.tsx#Coverage] Primitives / Input States | empty, populated, disabled | `components.input` |
| `Label` | [source:primitives/Label.tsx#Label] [story:primitives/Label.coverage.stories.tsx#Coverage] Primitives / Label State | associated label | `components.label` |
| `Select` | [source:primitives/Select.tsx#Select] [story:primitives/Select.coverage.stories.tsx#Coverage] Primitives / Select Interaction | selected/open options | `components.select` |
| `SubTabBar` | [source:primitives/SubTabBar.tsx#SubTabBar] [story:primitives/SubTabBar.coverage.stories.tsx#Coverage] Primitives / Sub Tab Bar States | active/inactive navigation | `components.subtabbar` |
| `Tabs` | [source:primitives/Tabs.tsx#Tabs] [story:primitives/Tabs.coverage.stories.tsx#Coverage] Primitives / Tabs Variants | pills, underline, segmented | `components.tabs` |
| `Textarea` | [source:primitives/Textarea.tsx#Textarea] [story:primitives/Textarea.coverage.stories.tsx#Coverage] Primitives / Textarea States | empty, populated/disabled | `components.textarea` |
| `Tooltip` | [source:primitives/Tooltip.tsx#Tooltip] [story:primitives/Tooltip.coverage.stories.tsx#Coverage] Primitives / Tooltip Interaction | open, hover/focus | `components.tooltip` |
| `AgentChip` | [source:patterns/AgentChip.tsx#AgentChip] [story:patterns/AgentChip.coverage.stories.tsx#Coverage] Patterns / Agent Chip Roles | founder, manager, worker | `components.agent_chip` |
| `AuditRow` | [source:patterns/AuditRow.tsx#AuditRow] [story:patterns/AuditRow.coverage.stories.tsx#Coverage] Patterns / Audit Row Density | complete audit/job fixtures; comfortable, compact, expandable | `components.audit_row` |
| `CommandPalette` | [source:patterns/CommandPalette.tsx#CommandPalette] [story:patterns/CommandPalette.coverage.stories.tsx#Coverage] Patterns / Command Palette Populated | open, populated, searchable | `components.dialog` |
| `Composer` | [source:patterns/Composer.tsx#Composer] [story:patterns/Composer.coverage.stories.tsx#Coverage] Patterns / Composer States | ready, abort, error/draft; 18px local-extension shell | input/button, `--radius-lg` |
| `CrescentMoonBadge` | [source:patterns/CrescentMoonBadge.tsx#CrescentMoonBadge] [story:patterns/CrescentMoonBadge.coverage.stories.tsx#Coverage] Patterns / Crescent Moon Badge State | present | `components.badge` |
| `EmptyState` | [source:patterns/EmptyState.tsx#EmptyState] [story:patterns/EmptyState.coverage.stories.tsx#Coverage] Patterns / Empty State With Action | empty with CTA | `components.empty_state` |
| `FilterSidebar` | [source:patterns/FilterSidebar.tsx#FilterSidebar] [story:patterns/FilterSidebar.coverage.stories.tsx#Coverage] Patterns / Filter Sidebar Interaction | all/selected, counts | `components.filter_sidebar` |
| `FormField` | [source:patterns/FormField.tsx#FormField] [story:patterns/FormField.coverage.stories.tsx#Coverage] Patterns / Form Field States | normal, error | input/label tokens |
| `HelpSheet` | [source:patterns/HelpSheet.tsx#HelpSheet] [story:patterns/HelpSheet.coverage.stories.tsx#Coverage] Patterns / Help Sheet Interaction | open shortcuts | dialog/kbd tokens |
| `IdBadge` | [source:patterns/IdBadge.tsx#IdBadge] [story:patterns/IdBadge.coverage.stories.tsx#Coverage] Patterns / Id Badge Kinds | thread, task | `components.badge` |
| `InboxRow` | [source:patterns/InboxRow.tsx#InboxRow] [story:patterns/InboxRow.coverage.stories.tsx#Coverage] Patterns / Inbox Row States | default/thread × active/open/archived; 8px interactive shell, nested pills preserved | `components.inbox_row`, `--radius-sm` |
| `KbdChip` | [source:patterns/KbdChip.tsx#KbdChip] [story:patterns/KbdChip.coverage.stories.tsx#Coverage] Patterns / Kbd Chip Combinations | key, chord | `components.kbd_chip` |
| `Markdown` | [source:patterns/Markdown.tsx#Markdown] [story:patterns/Markdown.coverage.stories.tsx#Coverage] Patterns / Markdown Content | heading/list/emphasis/code | typography/code |
| `MentionAutocomplete` | [source:patterns/MentionAutocomplete.tsx#MentionAutocomplete] [story:patterns/MentionAutocomplete.coverage.stories.tsx#Coverage] Patterns / Mention Autocomplete Populated | populated portal/listbox | surface/border |
| `MentionTextarea` | [source:patterns/MentionTextarea.tsx#MentionTextarea] [story:patterns/MentionTextarea.coverage.stories.tsx#Coverage] Patterns / Mention Textarea Interaction | editable mention | `components.textarea` |
| `Mermaid` | [source:patterns/Mermaid.tsx#default] [story:patterns/Mermaid.coverage.stories.tsx#Coverage] Patterns / Mermaid Diagram | loading → diagram | `components.code_block` |
| `MessageBubble` | [source:patterns/MessageBubble.tsx#MessageBubble] [story:patterns/MessageBubble.coverage.stories.tsx#Coverage] Patterns / Message Bubble Variants | founder, worker, manager, decline, system; non-system shells remain 18px | `components.message_bubble`, `--radius-lg` |
| `PageHeader` | [source:patterns/PageHeader.tsx#PageHeader] [story:patterns/PageHeader.coverage.stories.tsx#Coverage] Patterns / Page Header With Actions | metadata/action | heading/caption |
| `RecipientsInput` | [source:patterns/RecipientsInput.tsx#RecipientsInput] [story:patterns/RecipientsInput.coverage.stories.tsx#Coverage] Patterns / Recipients Input Interaction | editable prefix | mention/surface |
| `Sparkline` | [source:patterns/Sparkline.tsx#Sparkline] [story:patterns/Sparkline.coverage.stories.tsx#Coverage] Patterns / Sparkline Variants | default/green/yellow/red | semantic tiers |
| `StatValue` | [source:patterns/StatValue.tsx#StatValue] [story:patterns/StatValue.coverage.stories.tsx#Coverage] Patterns / Stat Value Formats | token/count, right/inline | `components.stat_value` |
| `StatusBadge` | [source:patterns/StatusBadge.tsx#StatusBadge] [story:patterns/StatusBadge.coverage.stories.tsx#Coverage] Patterns / Status Badge States | all lifecycle states | `components.badge` |
| `TaskCard` | [source:patterns/TaskCard.tsx#TaskCard] [story:patterns/TaskCard.coverage.stories.tsx#Coverage] Patterns / Task Card Density | populated/active; comfortable, compact; injected routes | badge/card |
| `ThreadHeader` | [source:patterns/ThreadHeader.tsx#ThreadHeader] [story:patterns/ThreadHeader.coverage.stories.tsx#Coverage] Patterns / Thread Header States | open/dream/action, archived | thread layout |
| `TraceTree` | [source:patterns/TraceTree.tsx#TraceTree] [story:patterns/TraceTree.coverage.stories.tsx#Coverage] Patterns / Trace Tree Density | recursive cost fixture; comfortable, compact | `components.trace_tree` |
| `TypingBubble` | [source:patterns/TypingBubble.tsx#TypingBubble] [story:patterns/TypingBubble.coverage.stories.tsx#Coverage] Patterns / Typing Bubble States | working, queued; shell remains 18px | info/muted, `--radius-lg` |
| `AppBar` | [excluded:AppBar] Reads live shell/org/navigation contexts and hosts product commands; AppShell/route tests cover it. | shell context in tests | topbar/grid |
| `ErrorBoundary` | [excluded:ErrorBoundary] Lifecycle capture/reset is not a static catalogue unit; component and route tests cover error/recovery. | normal/error/reset in tests | feedback |
| `Sidebar` | [source:layouts/AppShell/Sidebar.tsx#Sidebar] [story:layouts/AppShell/Sidebar.coverage.stories.tsx#Coverage] Layouts / Sidebar Branches | disabled navigation and footer account; focused enabled-navigation story | sidebar/grid |
| `TopBar` | [excluded:TopBar] Reads prototype/org route state; prototype/AppShell tests cover its complete shell contract. | shell context in tests | topbar/grid |
| `ContentWrap` | [source:layouts/ContentWrap/ContentWrap.tsx#ContentWrap] [story:layouts/ContentWrap/ContentWrap.coverage.stories.tsx#Coverage] Layouts / Content Wrap Responsive | bounded responsive content | layout content/wrap |
| `DashboardLayout` | [source:layouts/DashboardLayout.tsx#DashboardLayout] [story:layouts/DashboardLayout.coverage.stories.tsx#Coverage] Layouts / Dashboard Layout Populated | four populated slots | layout grid |
| `ThreadsLayout` | [source:layouts/ThreadsLayout.tsx#ThreadsLayout] [story:layouts/ThreadsLayout.coverage.stories.tsx#Coverage] Layouts / Threads Layout Populated | inbox/detail columns | threads grid |

## Frontend readiness matrix

| State | Applicability / evidence |
|---|---|
| Interaction | Applicable: portal/menu/select/tabs/filter/composer/mention stories are safe locally. |
| Loading | Applicable only to Mermaid's local render transition; backend loading is N/A to pure units. |
| Empty | Applicable to EmptyState and blank input examples. |
| Error | Applicable to FormField validation and Composer draft-preserving error. |
| Populated | Applicable across all three layers. |
| Auth | N/A: reusable units do not own authentication; context consumers are excluded and app-tested. |
| Permission | N/A: feature/shell owners authorize before passing props; context consumers are excluded. |

## Deterministic verification and acceptance

`scripts/verify-design-system.sh` runs typecheck, lint, unit coverage, the static
Storybook build, and full-tree raw-colour enforcement. `scripts/local_ci.sh web|all` and the
GitHub Web gate explicitly run SPA and Storybook builds once each. Storybook is
not a product prebuild hook, preventing duplicate builds.

- [ ] Stable imports and shipped component behavior remain unchanged.
- [ ] No `/__design__`, route flag, registry, generator, freshness hook, or stale metadata remains.
- [ ] Every reusable unit has meaningful discoverable coverage or a justified exclusion.
- [ ] Autodocs/controls, semantic tokens, themes, and safe local providers work.
- [ ] Every rendered Pasture reference section and its responsive 390 px presentation is represented by a discoverable `Pasture/*` story.
- [ ] No live daemon, hosted visual service, or product behavior is introduced.
- [ ] Lint, typecheck, unit tests, SPA build, Storybook build, design-system verification, browser evidence, and Node 24 local CI pass.
