/**
 * Mock route builders — paths under `/__prototypes/threads-v2/...`.
 *
 * Mounted by `PrototypeProvider` so compositions navigate within the
 * sandbox subtree instead of jumping to the daemon-backed routes.
 */
import type { AgentsRoutes, JobsRoutes, ThreadRoutes } from './DataContext';

const PROTOTYPE_BASE = '/__prototypes/threads-v2';

export function useMockThreadRoutes(): ThreadRoutes {
  return {
    detail: (threadId: string) => `${PROTOTYPE_BASE}/${threadId}`,
    inbox: () => PROTOTYPE_BASE,
    // Sandbox has one mock org; switching is a no-op that keeps the URL in
    // the prototype subtree instead of jumping to /orgs/<slug>/threads.
    inboxForOrg: () => PROTOTYPE_BASE,
  };
}

export function useMockAgentsRoutes(): AgentsRoutes {
  // Prototype harness has no per-feature agents subtree; keep everything
  // pinned to the threads sandbox so links inside MockProvider don't
  // escape into the real /orgs/... tree.
  return {
    inbox: () => PROTOTYPE_BASE,
    pending: () => PROTOTYPE_BASE,
    detail: () => PROTOTYPE_BASE,
    policy: () => PROTOTYPE_BASE,
    inboxForOrg: () => PROTOTYPE_BASE,
  };
}

export function useMockJobsRoutes(): JobsRoutes {
  return {
    inbox: () => PROTOTYPE_BASE,
    detail: () => PROTOTYPE_BASE,
    inboxForOrg: () => PROTOTYPE_BASE,
  };
}
