import { useState } from 'react';
import * as SelectPrimitive from '@radix-ui/react-select';
import {
  Activity,
  BookOpen,
  ChevronDown,
  Clock,
  type LucideIcon,
  ListChecks,
  MessageSquare,
  Package,
  Puzzle,
  ScrollText,
  Settings,
  Sparkles,
  Terminal,
  Users,
  Wallet,
  Home as HomeIcon,
} from 'lucide-react';
import { NavLink, useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  SelectContent,
  SelectItem,
  SelectSeparator,
} from '@/design-system/primitives/Select';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/design-system/primitives/Tooltip';
import { AddOrgDialog } from '@/features/orgs/AddOrgDialog';
import { useAgentsRoutes } from '@/hooks/agents';
import { useDashboardSummary } from '@/hooks/dashboard';

import { useKbRoutes } from '@/hooks/kb';
import { useOrgsList } from '@/hooks/orgs';
import { useTasksRoutes } from '@/hooks/tasks';
import { useThreadRoutes } from '@/hooks/threads';
import { useGlobalJump } from '@/hooks/global-jump';
import { useOrgSlugOptional } from '@/lib/orgSlug';

/**
 * IA-1: Grouped left sidebar + desktop window chrome, retiring the ~9-tab TopBar.
 *
 * THR-030 chrome alignment (BUG-01..08):
 *  - Top: context header (wordmark + org context line + caret) doubling as the
 *    org switcher — restyled from the native footer `<select>` (BUG-01/08).
 *  - Primary navigation: one flat, usage-ordered list of Home, Threads, Tasks,
 *    Jobs, Todos, Agents, Work Hours, Skills, Knowledge, Artifacts, Audit,
 *    Dreams, Usage, and Health.
 *  - Footer: Settings is permanently pinned above the static avatar + identity
 *    account row (BUG-07), separated from the primary list by the footer divider.
 *
 * Global search and the theme toggle moved to the AppBar (BUG-04/05/06).
 *
 * Jobs is reinstated as the approval queue per founder ruling (THR-030 seq 91,
 * TASK-907).
 */

const ADD_ORG_VALUE = '__add_org__';

export function Sidebar(): JSX.Element {
  const { slug: urlSlug } = useParams<{ slug: string }>();
  const contextSlug = useOrgSlugOptional();
  const activeSlug = urlSlug ?? contextSlug ?? null;
  const navigate = useNavigate();
  const location = useLocation();
  const isPrototype = location.pathname.startsWith('/__prototypes');
  const orgsQuery = useOrgsList();
  const [addOrgOpen, setAddOrgOpen] = useState(false);
  const routes = useThreadRoutes();
  const tasksRoutes = useTasksRoutes();
  const agentsRoutes = useAgentsRoutes();
  const kbRoutes = useKbRoutes();

  // BUG-08: Day-N comes from the SAME dashboard-summary query DashboardPage
  // consumes — React Query dedupes by key, so this is not a new data path.
  // The hook self-gates on the active org slug, so non-org routes issue no
  // fetch.
  const summary = useDashboardSummary().data;
  const orgAgeDays = summary?.org_age_days;

  // Global jump chords — reused from TopBar verbatim
  useGlobalJump('d', () => {
    if (activeSlug && !isPrototype) navigate(`/orgs/${activeSlug}/dashboard`);
  });
  useGlobalJump('i', () => {
    if (activeSlug && !isPrototype) navigate(routes.inboxForOrg(activeSlug));
  });
  useGlobalJump('t', () => {
    if (activeSlug && !isPrototype) navigate(tasksRoutes.inboxForOrg(activeSlug));
  });
  useGlobalJump('k', () => {
    if (activeSlug && !isPrototype) navigate(kbRoutes.inboxForOrg(activeSlug));
  });
  useGlobalJump('l', () => {
  });
  useGlobalJump('a', () => {
    if (activeSlug && !isPrototype) navigate(`/orgs/${activeSlug}/audit`);
  });
  useGlobalJump('g', () => {
    if (activeSlug && !isPrototype) navigate(agentsRoutes.inboxForOrg(activeSlug));
  });

  const switchEnabled = !orgsQuery.isLoading && (orgsQuery.data?.orgs.length ?? 0) > 0;

  const orgs = orgsQuery.data?.orgs ?? [];

  const sidebarLink = (path: string, enabled: boolean) => ({
    to: enabled && activeSlug ? `/orgs/${activeSlug}/${path}` : '#',
    enabled: enabled && !!activeSlug && !isPrototype,
  });

  const onOrgChange = (target: string): void => {
    if (target === ADD_ORG_VALUE) {
      setAddOrgOpen(true);
      return;
    }
    if (!target || target === activeSlug) return;
    // Stay on the same section; strip any detail suffix so we land on the
    // section inbox, not a stale detail route.
    const sectionMatch = activeSlug
      ? location.pathname.match(new RegExp(`^/orgs/${activeSlug}/([^/]+)`))
      : null;
    const section = sectionMatch?.[1];
    navigate(section ? `/orgs/${target}/${section}` : `/orgs/${target}/dashboard`);
  };

  return (
    <aside
      role="navigation"
      aria-label="Primary navigation"
      className="border-border bg-bg-subtle w-rail-narrow md:w-rail flex h-full shrink-0 flex-col border-r"
    >
      {/* Context header — wordmark + org context line + caret, doubling as the
          org switcher (BUG-01/08). Keeps the existing org-switch route logic. */}
      <section aria-label="Organization switcher">
        <SelectPrimitive.Root
          value={activeSlug ?? undefined}
          onValueChange={onOrgChange}
          disabled={!switchEnabled}
        >
          <SelectPrimitive.Trigger asChild aria-label="Active org">
            <button
              type="button"
              className="border-border border-l-accent hover:bg-bg-raised focus-visible:ring-accent flex w-full items-center gap-2 border-b border-l-2 px-2 py-3 text-left transition-colors focus-visible:ring-2 focus-visible:outline-none disabled:cursor-not-allowed md:px-4"
            >
              <Brandmark />
              {/* TASK-5987: below md the rail collapses to icons only — the
                  wordmark + context line are hidden but stay in the DOM; the
                  combobox keeps its "Active org" label. */}
              <span className="min-w-0 flex-1 flex-col max-md:hidden md:flex">
                <span className="font-['Baloo_2',sans-serif] text-[1rem] leading-tight font-extrabold tracking-[-0.03em]">
                  <span className="text-brand-foreground">Happy</span>
                  <span className="text-fg">Ranch</span>
                </span>
                {/* Context line — "Day N · <team>" (BUG-08). Day-N is the real
                    org_age_days from the dashboard summary; the team is the active
                    org slug. On a brand-new org (org_age_days 0/undefined) the day
                    token degrades away, leaving the bare slug — never "Day 0". */}
                <span className="text-fg-subtle truncate text-[0.7rem] leading-tight">
                  {activeSlug ? (
                    orgAgeDays && orgAgeDays > 0 ? (
                      <>
                        <span className="text-fg-muted">Day {orgAgeDays}</span> ·{' '}
                        <span>{activeSlug}</span>
                      </>
                    ) : (
                      <span>{activeSlug}</span>
                    )
                  ) : (
                    'No org'
                  )}
                </span>
              </span>
              <ChevronDown size={14} aria-hidden="true" className="text-fg-muted hidden shrink-0 md:block" />
            </button>
          </SelectPrimitive.Trigger>
          <SelectContent>
            {orgs.map((o) => (
              <SelectItem key={o.slug} value={o.slug}>
                {o.slug}
              </SelectItem>
            ))}
            {!isPrototype && (
              <>
                <SelectSeparator />
                <SelectItem value={ADD_ORG_VALUE}>+ Add org…</SelectItem>
              </>
            )}
          </SelectContent>
        </SelectPrimitive.Root>
      </section>

      <nav aria-label="Primary navigation items" className="mt-3 flex flex-col gap-0.5 px-3">
        <SidebarNavItem {...sidebarLink('dashboard', true)} icon={HomeIcon}>
          Home
        </SidebarNavItem>
        <SidebarNavItem
          to={routes.inboxForOrg(activeSlug ?? '')}
          enabled={!!activeSlug && !isPrototype}
          icon={MessageSquare}
        >
          Threads
        </SidebarNavItem>
        <SidebarNavItem {...sidebarLink('tasks', true)} icon={ListChecks}>
          Tasks
        </SidebarNavItem>
        <SidebarNavItem {...sidebarLink('jobs', true)} icon={Terminal}>
          Jobs
        </SidebarNavItem>
        <SidebarNavItem {...sidebarLink('todos', true)} icon={ListChecks}>
          Todos
        </SidebarNavItem>
        <SidebarNavItem {...sidebarLink('agents', true)} icon={Users}>
          Agents
        </SidebarNavItem>
        <SidebarNavItem {...sidebarLink('work-hours', true)} icon={Clock}>
          Work Hours
        </SidebarNavItem>
        <SidebarNavItem {...sidebarLink('skills', true)} icon={Puzzle}>
          Skills
        </SidebarNavItem>
        <SidebarNavItem {...sidebarLink('kb', true)} icon={BookOpen}>
          Knowledge
        </SidebarNavItem>
        <SidebarNavItem {...sidebarLink('artifacts', true)} icon={Package}>
          Artifacts
        </SidebarNavItem>
        <SidebarNavItem {...sidebarLink('audit', true)} icon={ScrollText}>
          Audit
        </SidebarNavItem>
        <SidebarNavItem {...sidebarLink('dreams', true)} icon={Sparkles}>
          Dreams
        </SidebarNavItem>
        <SidebarNavItem {...sidebarLink('usage', true)} icon={Wallet}>
          Usage
        </SidebarNavItem>
        <SidebarNavItem {...sidebarLink('health', true)} icon={Activity}>
          Health
        </SidebarNavItem>
      </nav>

      {/* Footer — static account row (BUG-07). */}
      <div className="border-border mt-auto flex flex-col gap-1 border-t px-3 py-3">
        <SidebarNavItem {...sidebarLink('settings', true)} icon={Settings}>
          Settings
        </SidebarNavItem>
        {/* Account row — avatar + identity. Identity is static chrome (no user
            profile is loaded client-side). It remains keyboard reachable as
            account context after the footer-pinned Settings item. */}
        <div
          tabIndex={0}
          aria-label="Account: You, Founder"
          className="focus-visible:ring-accent flex items-center gap-2.5 rounded-sm px-2 py-1.5 focus-visible:ring-2 focus-visible:outline-none"
        >
          <span
            aria-hidden="true"
            className="bg-accent text-accent-fg inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[0.65rem] font-semibold"
          >
            YT
          </span>
          <span className="min-w-0 flex-col leading-tight max-md:hidden md:flex">
            <span className="text-fg truncate text-sm">You</span>
            <span className="text-fg-subtle truncate text-xs">Founder</span>
          </span>
        </div>
      </div>

      {!isPrototype && <AddOrgDialog open={addOrgOpen} onOpenChange={setAddOrgOpen} />}
    </aside>
  );
}

function Brandmark(): JSX.Element {
  return (
    <svg
      viewBox="0 0 100 100"
      width="22"
      height="22"
      aria-hidden="true"
      className="text-brand-foreground shrink-0"
    >
      <g transform="rotate(-7 50 44)">
        <path
          d="M50 26 C68 26 78 34 78 44 C78 54 66 60 50 60 C34 60 22 54 22 44 C22 34 32 26 50 26 Z"
          fill="none"
          stroke="currentColor"
          strokeWidth="6.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </g>
      <ellipse cx="41" cy="59" rx="6.2" ry="5" fill="none" stroke="currentColor" strokeWidth="5" />
      <path
        d="M44 63 C50 78 70 82 80 71 C85 65 83 59 77 60"
        fill="none"
        stroke="currentColor"
        strokeWidth="6.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function SidebarNavItem({
  to,
  enabled,
  children,
  icon: Icon,
  tooltip,
}: {
  to: string;
  enabled: boolean;
  children: React.ReactNode;
  icon: LucideIcon;
  tooltip?: string;
}): JSX.Element {
  if (!enabled) {
    const span = (
      <span
        className="text-fg-subtle flex cursor-not-allowed items-center justify-center gap-2.5 rounded-sm px-2 py-1.5 text-sm md:justify-start"
        aria-disabled="true"
      >
        <Icon size={16} aria-hidden="true" className="shrink-0" />
        <span className="sr-only md:not-sr-only">{children}</span>
      </span>
    );
    if (!tooltip) return span;
    return (
      <Tooltip>
        <TooltipTrigger asChild>{span}</TooltipTrigger>
        <TooltipContent>{tooltip}</TooltipContent>
      </Tooltip>
    );
  }
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        `flex items-center gap-2.5 rounded-sm border-l-2 border-l-transparent px-2 py-1.5 text-sm transition-colors focus-visible:ring-2 focus-visible:outline-none focus-visible:ring-accent justify-center md:justify-start ${
          isActive
            ? 'border-l-accent bg-bg-raised text-fg font-medium'
            : 'text-fg-muted hover:bg-bg-raised hover:text-fg'
        }`
      }
    >
      <Icon size={16} aria-hidden="true" className="shrink-0" />
      {/* TASK-5987: sr-only below md keeps the accessible name when the rail
          collapses to icons; md+ restores the visible label. */}
      <span className="sr-only md:not-sr-only">{children}</span>
    </NavLink>
  );
}
