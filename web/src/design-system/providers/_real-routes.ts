/**
 * Real route builders — paths under `/orgs/:slug/threads/...`.
 *
 * Reads the active slug from `OrgSlugContext` (populated by `<OrgProvider>`
 * inside `routes.tsx`). Called by compositions through
 * `@/hooks/threads.useThreadRoutes()`.
 *
 * Uses the **optional** slug variant so layout chrome (`TopBar`) can call us
 * from above the `<OrgProvider>` boundary — e.g. on the `/` index route
 * before redirect — without throwing. When no slug is in scope, `inbox` and
 * `detail` return `'#'`, which renders the NavLink as inert.
 */
import { useOrgSlugOptional } from '@/lib/orgSlug';
import type { AgentsRoutes, DreamsRoutes, JobsRoutes, KbRoutes, TasksRoutes, ThreadRoutes } from './DataContext';

export function useRealThreadRoutes(): ThreadRoutes {
  const slug = useOrgSlugOptional();
  return {
    detail: (threadId: string) => (slug ? `/orgs/${slug}/threads/${threadId}` : '#'),
    inbox: () => (slug ? `/orgs/${slug}/threads` : '#'),
    inboxForOrg: (target: string) => `/orgs/${target}/threads`,
  };
}

export function useRealTasksRoutes(): TasksRoutes {
  const slug = useOrgSlugOptional();
  return {
    detail: (taskId: string) => (slug ? `/orgs/${slug}/tasks/${taskId}` : '#'),
    inbox: () => (slug ? `/orgs/${slug}/tasks` : '#'),
    inboxForOrg: (target: string) => `/orgs/${target}/tasks`,
  };
}

export function useRealKbRoutes(): KbRoutes {
  const slug = useOrgSlugOptional();
  return {
    detail: (entrySlug: string) =>
      slug ? `/orgs/${slug}/kb/${entrySlug}` : '#',
    inbox: () => (slug ? `/orgs/${slug}/kb` : '#'),
    inboxForOrg: (target: string) => `/orgs/${target}/kb`,
  };
}

export function useRealAgentsRoutes(): AgentsRoutes {
  const slug = useOrgSlugOptional();
  return {
    inbox: () => (slug ? `/orgs/${slug}/agents` : '#'),
    // Tab state rides on a query param so we never reserve a static path
    // segment that would shadow a real agent slug under `agents/:agent_name`.
    pending: () => (slug ? `/orgs/${slug}/agents?view=pending` : '#'),
    detail: (agentName: string) =>
      slug ? `/orgs/${slug}/agents/${agentName}` : '#',
    policy: (agentName: string) =>
      slug ? `/orgs/${slug}/agents/${agentName}/team-escalation-policy` : '#',
    inboxForOrg: (target: string) => `/orgs/${target}/agents`,
  };
}

export function useRealDreamsRoutes(): DreamsRoutes {
  const slug = useOrgSlugOptional();
  return {
    detail: (dreamId: string) =>
      slug ? `/orgs/${slug}/dreams/${dreamId}` : '#',
    inbox: () => (slug ? `/orgs/${slug}/dreams` : '#'),
    inboxForOrg: (target: string) => `/orgs/${target}/dreams`,
  };
}

export function useRealJobsRoutes(): JobsRoutes {
  const slug = useOrgSlugOptional();
  return {
    inbox: () => (slug ? `/orgs/${slug}/jobs` : '#'),
    detail: (jobId: string) => (slug ? `/orgs/${slug}/jobs/${jobId}` : '#'),
    inboxForOrg: (target: string) => `/orgs/${target}/jobs`,
  };
}
