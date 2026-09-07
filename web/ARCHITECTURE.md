# HappyRanch web — architecture notes

Localhost React SPA bundled into the FastAPI daemon. See
`docs/superpowers/specs/2026-05-14-web-ui-design.md` for the full web-UI
design and `web/DESIGN_SYSTEM.md` for the design-system migration plan.

## Layers (strict)

1. **`src/lib/api/<X>.ts`** — Daemon route mirror. One TS module per
   `src/daemon/routes/<X>.py`. Pure functions over a shared `request()` helper.
   Returns typed objects. No React.
2. **`src/design-system/`** — Code-owned design system. Replaces the previous
   `src/components/` folder. Splits into:
   - **`tokens/tokens.css`** — single `@theme` block. Tailwind v4 derives all
     utilities from these CSS custom properties. It is the only raw-colour
     definition authority; the full production-tree scanner holds any legacy
     raw colour outside it to an exact, shrinking occurrence baseline.
   - **`primitives/`** — shadcn/ui-style components (Button, Dialog, …).
     Pure UI; allowed to import `@/lib/utils` only.
   - **`patterns/`** _(future)_ — composites of primitives (AgentChip,
     InboxRow, MessageBubble, …). Pure props in, JSX out.
   - **`layouts/`** _(future)_ — slot-based templates (AppShell, ThreadsLayout,
     DashboardLayout).
3. **`src/shared/<domain>/`** — Shared feature-safe modules. Hookful React
   components (dialogs, composers) that multiple feature folders need but
   that cannot live in `design-system/` (patterns are pure, no hooks) or
   `features/` (cross-feature imports forbidden). May import from
   `@/lib/`, `@/design-system/`, and `@/hooks/`. May NOT import from
   `@/features/`.
4. **`src/features/<domain>/`** — React feature folders. One folder per CLI
   domain. Owns pages, dialogs, and TanStack Query hooks. May import only from
   `@/lib/`, `@/design-system/`, `@/shared/`, and `@/hooks/`. **No
   cross-feature imports.**
5. **`src/lib/utils.ts`** — `cn` helper for class-name composition (used by
   primitives only).

## Boundary rule

> Every browser-callable daemon route maps 1:1 to one TS function in
> `src/lib/api/`. Features compose those functions through TanStack Query
> hooks. Features may not call `fetch` directly. Cross-feature imports are
> forbidden — share through `@/shared/`, `@/design-system/`, or `@/lib/`.
>
> `@/shared/` modules may import hooks, lib, and design-system but never
> `@/features/`. Primitives may not import patterns, layouts, hooks, or
> `@/lib/api`. Patterns may import primitives but not layouts.

## What is intentionally not in here

- Agent-callback endpoints (`/report-completion`, `/manage-agent`,
  `/manage-repo`, `/dispatch`, `/learning add|update|promote`, thread
  `/reply`, `/decline`, `/dispatch`, `/close-out`). Those are agent-subprocess
  only and would be a privilege-escalation if exposed in the browser.
- `--as-founder` impersonation surface for KB deletes. Stays TTY-gated in CLI.
- Multi-user concerns: login screens, account model, RBAC. Localhost only.
