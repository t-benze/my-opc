/**
 * Provider-agnostic data layer for compositions.
 *
 * Compositions never call TanStack Query or `lib/api` directly. They call
 * provider-aware hooks from `@/hooks/`, which forward to whatever
 * `ThreadsApi` (and future feature APIs) the surrounding provider installs.
 *
 * Two implementations exist:
 *
 * - `<AppProvider>` (production) — wires the real TanStack Query bodies in
 *   `_real-threads.ts` against the daemon.
 * - `<PrototypeProvider>` (designer sandbox) — wires the canned fixtures in
 *   `_mock-threads.ts` from `@/mocks/`.
 *
 * The hook signatures intentionally drop the `slug` argument so the same
 * composition file can render against either provider. The active slug is a
 * concern of the provider, not the consumer.
 *
 * See `web/DESIGN_SYSTEM.md` §8.
 */
import { createContext, useContext } from 'react';
import type {
  HealthResponse,
  OrgsListResponse,
  SettingsSnapshot,
  ThreadDetailResponse,
  ThreadMessagesPage,
  ThreadRecord,
} from '@/lib/api/types';
import type { threads as threadsApi } from '@/lib/api';
import type { tasks as tasksApi } from '@/lib/api';
import type { kb as kbApi } from '@/lib/api';
import type { audit as auditApi } from '@/lib/api';
import type { agents as agentsApi } from '@/lib/api';
import type { jobs as jobsApi } from '@/lib/api';
import type { DreamRecord, DreamKbCandidate } from '@/lib/api/dreams';
import type {
  AssignSkillRequest,
  AssignSkillResponse,
  CatalogSkillItem,
  CreateSkillRequest,
  CreateSkillResponse,
  EditSkillRequest,
  EditSkillResponse,
  SkillDetail,
  SkillStatusResponse,
  ValidateSkillResponse,
  ValidationEvent,
} from '@/lib/api/skills';
import type { ConversationSummary } from '@/lib/api/assistant';
import type { ThreadTaskSummary } from '@/lib/api/threads';
import type { workHours as workHoursApi } from '@/lib/api';
import type {
  AssistantRegisterBody,
  AssistantStatus,
  DashboardSummaryResponse,
  JobListResponse,
  JobRecord,
  KBEntry,
  TaskEvent,
  TaskRecord,
  TaskRecallNode,
} from '@/lib/api/types';

// ---------------------------------------------------------------------------
// Hook-shape primitives
// ---------------------------------------------------------------------------

export interface QueryLike<T> {
  data: T | undefined;
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
}

/** Minimal subset of TanStack's UseInfiniteQueryResult that consumers need.
 *  `data.pages` is the per-page payload; callers flatten as appropriate. */
export interface InfiniteQueryLike<TPage> {
  data: { pages: TPage[] } | undefined;
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
  fetchNextPage: () => Promise<unknown>;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
}

export interface MutationLike<TArgs, TResult> {
  mutateAsync: (args: TArgs) => Promise<TResult>;
  isPending: boolean;
}

// ---------------------------------------------------------------------------
// ThreadsApi — covers every hook ThreadsPage + its dialogs consume.
// ---------------------------------------------------------------------------

export type ComposeArgs = Parameters<typeof threadsApi.composeThread>[1];
export type ComposeResult = Awaited<ReturnType<typeof threadsApi.composeThread>>;

export type SendFollowUpArgs = Parameters<typeof threadsApi.sendThreadFollowUp>[2];
export type SendFollowUpResult = Awaited<ReturnType<typeof threadsApi.sendThreadFollowUp>>;

export type InviteArgs = Parameters<typeof threadsApi.inviteToThread>[2];
export type InviteResult = Awaited<ReturnType<typeof threadsApi.inviteToThread>>;

export type RemoveParticipantArgs = Parameters<typeof threadsApi.removeParticipantFromThread>[2];
export type RemoveParticipantResult = Awaited<ReturnType<typeof threadsApi.removeParticipantFromThread>>;

export type ArchiveArgs = Parameters<typeof threadsApi.archiveThread>[2];
export type ArchiveResult = Awaited<ReturnType<typeof threadsApi.archiveThread>>;

export type ResumeArgs = void;
export type ResumeResult = Awaited<ReturnType<typeof threadsApi.resumeThread>>;

export type AbortRepliesArgs = void;
export type AbortRepliesResult = Awaited<ReturnType<typeof threadsApi.abortReplies>>;

export type RenameThreadArgs = { subject: string };
export type RenameThreadResult = Awaited<ReturnType<typeof threadsApi.renameThread>>;

export type SetThreadPinArgs = { pinned: boolean };
export type SetThreadPinResult = Awaited<ReturnType<typeof threadsApi.setThreadPinned>>;

export interface ThreadsApi {
  // Reads
  useThreadsList: (
    params?: { status?: string; limit?: number },
  ) => QueryLike<{ threads: ThreadRecord[] }>;
  useThread: (threadId: string | undefined) => QueryLike<ThreadDetailResponse>;
  useThreadMessages: (
    threadId: string | undefined,
  ) => InfiniteQueryLike<ThreadMessagesPage>;
  /** Tasks dispatched from this thread, newest-first (THR-061). Read-only. */
  useThreadTasks: (
    threadId: string | undefined,
  ) => QueryLike<ThreadTaskSummary[]>;

  // SSE (no-op under mocks)
  useThreadsInboxSSE: () => void;
  useThreadTailSSE: (threadId: string | undefined) => void;

  // Mutations — `threadId` is a per-hook argument so the call shape mirrors
  // the existing TanStack Query hooks, just with `slug` stripped.
  useComposeThread: () => MutationLike<ComposeArgs, ComposeResult>;
  useSendFollowUp: (threadId: string) => MutationLike<SendFollowUpArgs, SendFollowUpResult>;
  useInviteAgent: (threadId: string) => MutationLike<InviteArgs, InviteResult>;
  useRemoveParticipant: (
    threadId: string,
  ) => MutationLike<RemoveParticipantArgs, RemoveParticipantResult>;
  useArchiveThread: (threadId: string) => MutationLike<ArchiveArgs, ArchiveResult>;
  useResumeThread: (threadId: string) => MutationLike<ResumeArgs, ResumeResult>;
  useAbortReplies: (threadId: string) => MutationLike<AbortRepliesArgs, AbortRepliesResult>;
  /** Founder-only inline rename (THR-209). Optimistic on the detail header,
   *  list rows refetch via invalidation. */
  useRenameThread: (threadId: string) => MutationLike<RenameThreadArgs, RenameThreadResult>;
  /** Founder-only durable pin/unpin (THR-209). Optimistic with rollback. */
  useSetThreadPinned: (threadId: string) => MutationLike<SetThreadPinArgs, SetThreadPinResult>;
}

// ---------------------------------------------------------------------------
// TasksApi — covers every hook TasksPage + its dialogs consume.
// ---------------------------------------------------------------------------

export type CancelTaskArgs = Parameters<typeof tasksApi.cancelTask>[2];
export type CancelTaskResult = Awaited<ReturnType<typeof tasksApi.cancelTask>>;

export type RevisitTaskArgs = Parameters<typeof tasksApi.revisitTask>[2];
export type RevisitTaskResult = Awaited<ReturnType<typeof tasksApi.revisitTask>>;

export type ResolveEscalationArgs = Parameters<typeof tasksApi.resolveEscalation>[2];
export type ResolveEscalationResult = Awaited<ReturnType<typeof tasksApi.resolveEscalation>>;

export type TasksListPage = { tasks: TaskRecord[]; next_cursor?: string | null };

export interface TaskTailSSECallbacks {
  onOpen?: () => void;
  onError?: (error: unknown) => void;
}

export interface TasksApi {
  useTasksList: (params?: {
    status?: string;
    limit?: number;
  }) => QueryLike<{ tasks: TaskRecord[] }>;
  /** Cursor-paginated variant used by the inbox page's infinite scroll.
   *  Each page is the raw server payload (`tasks` + `next_cursor`); the
   *  caller flattens `data.pages` and asks `fetchNextPage()` from a
   *  scroll-observer sentinel. */
  useTasksInfiniteList: (params?: {
    status?: string;
  }) => InfiniteQueryLike<TasksListPage>;
  /** Roots-only list with per-root severity_rollup (design-overhaul §4.3). */
  useTasksRoots: (params?: {
    status?: string;
    limit?: number;
    assigned_agent?: string;
  }) => QueryLike<{ tasks: TaskRecord[] }>;
  /** Cursor-paginated roots-only variant for infinite scroll. */
  useTasksRootsInfinite: (params?: {
    status?: string;
    assigned_agent?: string;
  }) => InfiniteQueryLike<TasksListPage>;
  useTask: (taskId: string | undefined) => QueryLike<TaskRecord>;
  useTaskRecall: (taskId: string | undefined) => QueryLike<TaskRecallNode>;

  /** Subscribes; passes each event to `onEvent`. No-op under mocks. */
  useTaskTailSSE: (
    taskId: string | undefined,
    onEvent: (ev: TaskEvent) => void,
    callbacks?: TaskTailSSECallbacks,
  ) => void;

  useCancelTask: (taskId: string) => MutationLike<CancelTaskArgs, CancelTaskResult>;
  useRevisitTask: (taskId: string) => MutationLike<RevisitTaskArgs, RevisitTaskResult>;
  useResolveEscalation: (
    taskId: string,
  ) => MutationLike<ResolveEscalationArgs, ResolveEscalationResult>;
}

export interface TasksRoutes {
  inbox: () => string;
  detail: (taskId: string) => string;
  inboxForOrg: (slug: string) => string;
}

// KbApi — covers every hook KbPage + its drawer + (optional) compose dialog
// consume.
// ---------------------------------------------------------------------------

export type AddKBEntryArgs = Parameters<typeof kbApi.addKBEntry>[1];
export type AddKBEntryResult = Awaited<ReturnType<typeof kbApi.addKBEntry>>;

export interface KbApi {
  useKBList: (params?: {
    type?: string;
  }) => QueryLike<{ entries: KBEntry[] }>;
  useKBSearch: (
    q: string,
    params?: { limit?: number },
  ) => QueryLike<{ entries: KBEntry[] }>;
  useKBEntry: (entrySlug: string | undefined) => QueryLike<KBEntry>;
  useKBStats: () => QueryLike<{ entries: import('@/lib/api/kb').KBViewStat[] }>;
  /** Mutation is wired only under the real provider; mocks no-op. */
  useAddKBEntry: () => MutationLike<AddKBEntryArgs, AddKBEntryResult>;
}

export interface KbRoutes {
  inbox: () => string;
  detail: (entrySlug: string) => string;
  inboxForOrg: (slug: string) => string;
}

// ---------------------------------------------------------------------------
// DreamsApi — covers every hook the Dreams page + its detail drawer consume.
// ---------------------------------------------------------------------------

export interface DreamsApi {
  useDreamsList: (params?: {
    agent?: string;
    limit?: number;
  }) => QueryLike<{ dreams: DreamRecord[] }>;
  useDream: (dreamId: string | undefined) => QueryLike<DreamRecord & {
    transcript?: string;
    kb_candidates?: DreamKbCandidate[];
  }>;
  useAcceptCandidate: () => MutationLike<number, DreamKbCandidate>;
  useDismissCandidate: () => MutationLike<number, DreamKbCandidate>;
}

export interface DreamsRoutes {
  inbox: () => string;
  detail: (dreamId: string) => string;
  inboxForOrg: (slug: string) => string;
}

// ---------------------------------------------------------------------------
// OrgsApi — minimal read-only surface so the TopBar org dropdown works
// under both providers without TopBar reaching into `@/lib/api` itself.
// ---------------------------------------------------------------------------

export interface OrgsApi {
  useOrgsList: () => QueryLike<OrgsListResponse>;
}

// ---------------------------------------------------------------------------
// HealthApi — minimal daemon liveness probe consumed by the Dashboard page.
// ---------------------------------------------------------------------------

export interface HealthApi {
  useHealth: () => QueryLike<HealthResponse>;
}

// ---------------------------------------------------------------------------
// AssistantApi — the global (non-org-scoped) System Assistant surface:
// status poll + init/register/repair mutations + the A-mode WebSocket session.
// ---------------------------------------------------------------------------

export interface AssistantApi {
  useAssistantStatus: (enabled: boolean) => QueryLike<AssistantStatus>;
  useInitAssistant: () => MutationLike<{ reconfigure: boolean }, AssistantStatus>;
  useRegisterAssistant: () => MutationLike<AssistantRegisterBody, AssistantStatus>;
  useRepairAssistant: () => MutationLike<void, AssistantStatus>;
  /**
   * Opens the A-mode WebSocket — the structured `TurnFrame` stream that drives
   * the thread-style dock. Imperative and real-only; the mock rejects.
   */
  openAModeSession: () => Promise<WebSocket>;
  /**
   * Multi-conversation switcher (THR-056 STEP-B). N conversations live UNDER
   * the one runtime-global assistant. The list is newest-first; exactly one is
   * `active` at a time and the A-mode WS attaches to it on (re)connect.
   *
   * `enabled` gates the poll so the list is only fetched while the dock is
   * open and the assistant is configured. Every mutation invalidates the list
   * cache so the `active` flag reflects the server after new/switch/delete.
   */
  useListConversations: (enabled: boolean) => QueryLike<ConversationSummary[]>;
  useCreateConversation: () => MutationLike<void, ConversationSummary>;
  useActivateConversation: () => MutationLike<string, { success: boolean }>;
  useRenameConversation: () => MutationLike<
    { id: string; title: string },
    { success: boolean }
  >;
  useDeleteConversation: () => MutationLike<string, { success: boolean }>;
}

// ---------------------------------------------------------------------------
// AgentsApi — minimal read-only roster used by the Composer for
// @-mention autocomplete. Lives on DataContext so prototypes can swap
// in canned fixtures.
// ---------------------------------------------------------------------------

export type ApproveAgentArgs = Parameters<typeof agentsApi.approveAgent>[1];
export type ApproveAgentResult = Awaited<ReturnType<typeof agentsApi.approveAgent>>;

export type RejectAgentArgs = Parameters<typeof agentsApi.rejectAgent>[2];
export type RejectAgentResult = Awaited<ReturnType<typeof agentsApi.rejectAgent>>;

export type CreateAgentArgs = Parameters<typeof agentsApi.createAgent>[1];
export type CreateAgentResult = Awaited<ReturnType<typeof agentsApi.createAgent>>;

export type SetAgentExecutorArgs = Parameters<typeof agentsApi.setAgentExecutor>[2];
export type SetAgentExecutorResult = Awaited<ReturnType<typeof agentsApi.setAgentExecutor>>;

export type SetAgentModelArgs = Parameters<typeof agentsApi.setAgentModel>[2];
export type SetAgentModelResult = Awaited<ReturnType<typeof agentsApi.setAgentModel>>;

export type ManageAgentRepoArgs = Parameters<typeof agentsApi.manageAgentRepo>[2];
export type ManageAgentRepoResult = Awaited<ReturnType<typeof agentsApi.manageAgentRepo>>;

export interface AgentsApi {
  useAgentsList: () => QueryLike<{ agents: import('@/lib/api/agents').AgentSummary[] }>;
  /** Pending enrollments — `status` filter narrows the file scan. */
  useEnrollmentsList: (
    params?: { status?: 'pending' | 'approved' },
  ) => QueryLike<{ enrollments: import('@/lib/api/agents').AgentEnrollment[] }>;
  /** Read-only learnings list for the agent detail drawer. */
  useAgentLearnings: (
    agentName: string | undefined,
  ) => QueryLike<{ entries: import('@/lib/api/agents').MemoryEntrySummary[] }>;
  /** Tasks where this agent was the assigned (manager) agent. */
  useAgentTasks: (
    agentName: string | undefined,
  ) => QueryLike<{ tasks: TaskRecord[] }>;

  useCreateAgent: () => MutationLike<CreateAgentArgs, CreateAgentResult>;
  useApproveAgent: () => MutationLike<ApproveAgentArgs, ApproveAgentResult>;
  useRejectAgent: () => MutationLike<
    { agentName: string; body?: { reason?: string } },
    RejectAgentResult
  >;
  useSetAgentExecutor: () => MutationLike<
    { agentName: string; body: SetAgentExecutorArgs },
    SetAgentExecutorResult
  >;
  useSetAgentModel: () => MutationLike<
    { agentName: string; body: SetAgentModelArgs },
    SetAgentModelResult
  >;
  useManageAgentRepo: () => MutationLike<
    { agentName: string; body: ManageAgentRepoArgs },
    ManageAgentRepoResult
  >;
}

export interface AuthorityPolicyApi {
  useTeamEscalationPolicy: (agent: {
    name: string;
    team: string;
    role: string;
  } | undefined) => QueryLike<import('@/lib/api/authorityPolicy').TeamEscalationPolicyResponse> & {
    refetch: () => Promise<unknown>;
  };
  useCreateTeamEscalationPolicyRelease: () => MutationLike<
    { agentName: string; body: import('@/lib/api/authorityPolicy').CreateAuthorityPolicyReleaseRequest },
    import('@/lib/api/authorityPolicy').CreateAuthorityPolicyReleaseResponse
  >;
  useActivateTeamEscalationPolicyRelease: () => MutationLike<
    { agentName: string; body: { release_id: string; expected_previous_epoch: number; request_id: string;
      action: 'activate' | 'reactivate_rollback'; acknowledge_shared_credential_attribution: true } },
    unknown
  >;
  useTeamEscalationPolicyHistory: (agent: { name: string; team: string; role: string } | undefined) =>
    InfiniteQueryLike<import('@/lib/api/authorityPolicy').AuthorityPolicyHistoryResponse>;
  useTeamEscalationPolicyOutcomes: (agent: { name: string; team: string; role: string } | undefined) =>
    InfiniteQueryLike<import('@/lib/api/authorityPolicy').AuthorityPolicyOutcomesResponse>;
}

export interface AgentsRoutes {
  inbox: () => string;
  pending: () => string;
  detail: (agentName: string) => string;
  policy: (agentName: string) => string;
  inboxForOrg: (slug: string) => string;
}

// ---------------------------------------------------------------------------
// TeamsApi — minimal read-only roster driving the Add Agent team dropdown.
// ---------------------------------------------------------------------------

export interface TeamsApi {
  useTeamsList: () => QueryLike<{ teams: import('@/lib/api/teams').TeamSummary[] }>;
}

// ---------------------------------------------------------------------------
// JobsApi — founder-facing surface for background jobs (formerly script requests).
// ---------------------------------------------------------------------------

export type RejectJobArgs = Parameters<typeof jobsApi.rejectJob>[2];
export type RejectJobResult = Awaited<ReturnType<typeof jobsApi.rejectJob>>;

export type RunJobArgs = Parameters<typeof jobsApi.runJob>[2];
export type RunJobResult = Awaited<ReturnType<typeof jobsApi.runJob>>;

export type StopJobResult = Awaited<ReturnType<typeof jobsApi.stopJob>>;

export type JobOutputResult = Awaited<ReturnType<typeof jobsApi.getJobOutput>>;

export interface JobsApi {
  useJobsList: (params?: {
    status?: string;
    agent?: string;
    task_id?: string;
    review_required?: string;
    persistent?: string;
    limit?: number;
  }) => QueryLike<JobListResponse>;
  useJob: (jobId: string | undefined) => QueryLike<JobRecord>;
  useJobOutput: (jobId: string | undefined) => QueryLike<JobOutputResult>;
  useRejectJob: () => MutationLike<{ jobId: string; body: RejectJobArgs }, RejectJobResult>;
  useRunJob: () => MutationLike<{ jobId: string; body: RunJobArgs }, RunJobResult>;
  useStopJob: () => MutationLike<{ jobId: string }, StopJobResult>;
}

export interface JobsRoutes {
  inbox: () => string;
  detail: (jobId: string) => string;
  inboxForOrg: (slug: string) => string;
}

// ---------------------------------------------------------------------------
// AuditApi — read-only audit-log surface for the Audit feature page.
// ---------------------------------------------------------------------------

/** One page of the audit list — mirrors the daemon's paginated response. */
export type AuditListPage = Awaited<ReturnType<typeof auditApi.listAudit>>;

export interface AuditApi {
  /** Keyset-paginated audit list (most-recent-first). `data.pages` carries
   *  each fetched page; callers flatten across pages before day-grouping. */
  useAuditList: (params?: {
    task_id?: string | null;
    agent?: string | null;
    action?: string | null;
    since?: string | null;
    limit?: number;
  }) => InfiniteQueryLike<AuditListPage>;
}

// ---------------------------------------------------------------------------
// SkillsApi — read-only Skills surface (THR-092). Slice 1: Catalog list
// (Bundled / Custom filter → daemon `?filter=`). Slice 2: single-skill detail
// + per-agent effective/provenance. Guidance visibility only.
// ---------------------------------------------------------------------------

export interface SkillsApi {
  useSkillsCatalog: (params?: {
    filter?: 'Bundled' | 'Custom';
  }) => QueryLike<{ items: CatalogSkillItem[] }>;
  /** Single-skill detail (source, validation, per-agent assignments[]).
   *  Backs the Slice-2 Skill Detail + provenance surface. */
  useSkillDetail: (skillId: string | undefined) => QueryLike<SkillDetail>;
  /** Author / import a user-authored custom skill (Slice-3). Runs the
   *  technical validate guard synchronously; a content-validation failure
   *  still persists an editable draft (`validation.ok=false`), so the
   *  mutation resolves rather than rejects on that path. */
  useCreateSkill: () => MutationLike<CreateSkillRequest, CreateSkillResponse>;
  /** Re-run the technical validate guard for an existing draft (Slice-3). */
  useValidateSkill: () => MutationLike<
    { skillId: string },
    ValidateSkillResponse
  >;
  /** Edit a user-authored custom skill and re-validate (Slice-4). The PATCH
   *  can never mint/alter a system_contract (no policy_class field). A
   *  content-validation failure still persists an editable draft
   *  (`validation.ok=false`), so the mutation resolves rather than rejects on
   *  that path (spec v3 §9.5 / §9.1a). */
  useEditSkill: () => MutationLike<
    { skillId: string; body: EditSkillRequest },
    EditSkillResponse
  >;
  /** Per-agent assignment status for one skill (Slice-5). Drives the custom-
   *  skill assignment table: each agent's assigned/effective state +
   *  materialized version. This is the authoritative assignment source, apart
   *  from the detail fetch's `assignments[]`. */
  useSkillStatus: (skillId: string | undefined) => QueryLike<SkillStatusResponse>;
  /** Assign / unassign one skill for one agent (Slice-5). The commit of the
   *  config-review queue calls this once per queued change. The request body
   *  verb (`allow`/`remove`) is transport only — the UI reads guidance-
   *  visibility labels (Assign / Unassign). Changing an assignment changes what
   *  an agent is SHOWN as guidance, never the tools or commands it can use. */
  useAssignSkill: () => MutationLike<
    { agentId: string; skillId: string; body: AssignSkillRequest },
    AssignSkillResponse
  >;
  /** Runtime Validation event list (Slice-6). Read-only. Filterable by skill,
   *  agent, source, time (`since`), and severity — each maps 1:1 to the daemon
   *  query params. Returns the events plus the endpoint's `label` (the surface
   *  title, "Runtime Validation" — never "Audit"). */
  useSkillValidation: (params?: {
    skill?: string;
    agent?: string;
    source?: string;
    since?: string;
    severity?: string;
    limit?: number;
  }) => QueryLike<{ events: ValidationEvent[]; label: string }>;
}

// ---------------------------------------------------------------------------
// DashboardApi — aggregated summary surface for the Dashboard page.
// Escalation resolution reuses `TasksApi.useResolveEscalation`, so this
// surface only carries the summary read.
// ---------------------------------------------------------------------------

export interface DashboardApi {
  useDashboardSummary: () => QueryLike<DashboardSummaryResponse>;
}

// ---------------------------------------------------------------------------
// SettingsApi — read-only System + Org settings surface (Phase 1).
// Phase 2 adds useUpdateOrgSettings (mutation) for editable Org fields.
// ---------------------------------------------------------------------------

export interface SettingsApi {
  useSettings: () => QueryLike<SettingsSnapshot>;
  useUpdateOrgSettings: () => MutationLike<
    import('@/lib/api/types').OrgSettingsPatch,
    import('@/lib/api/types').SettingsSnapshot
  >;
  useDaemonCapacity: () => QueryLike<import('@/lib/api/types').DaemonCapacitySnapshot>;
  useUpdateDaemonCapacity: () => MutationLike<
    import('@/lib/api/types').DaemonCapacityWrite,
    import('@/lib/api/types').DaemonCapacitySnapshot
  >;
  /** Next-wakes preview for an agent's resolved effective schedule
   *  (work-hours config UI, THR-035). Self-gates when `agent` is undefined. */
  useNextWakes: (
    agent: string | undefined,
    count?: number,
  ) => QueryLike<import('@/lib/api/types').NextWakesResponse>;
}

// ---------------------------------------------------------------------------
// WorkHoursApi — read-only work-hours list for the Schedule feature page.
// ---------------------------------------------------------------------------

export interface WorkHoursApi {
  useWorkHoursList: (params?: {
    agent?: string;
    limit?: number;
  }) => QueryLike<Awaited<ReturnType<typeof workHoursApi.listWorkHours>>>;
}

/**
 * Per-feature URL builders. Compositions consume these via the
 * provider-aware `useThreadRoutes()` hook in `@/hooks/threads` instead of
 * hardcoding `/orgs/${slug}/...` — so the same JSX renders under
 * `/orgs/:slug/threads/...` (production) AND
 * `/__prototypes/threads-v2/...` (sandbox) without forking.
 */
export interface ThreadRoutes {
  /** Detail-pane URL for a given thread_id. */
  detail: (threadId: string) => string;
  /** Inbox URL for the active context. */
  inbox: () => string;
  /**
   * Inbox URL when switching to a specific org. Used by the TopBar org
   * dropdown so the user lands in the right place regardless of which
   * provider is mounted. Under the real provider this is
   * `/orgs/<slug>/threads`; under the prototype it stays inside the
   * sandbox subtree, ignoring the slug.
   */
  inboxForOrg: (slug: string) => string;
}

export interface DataContextValue {
  orgs: OrgsApi;
  agents: AgentsApi;
  authorityPolicy: AuthorityPolicyApi;
  audit: AuditApi;
  threads: ThreadsApi;
  tasks: TasksApi;
  kb: KbApi;
  teams: TeamsApi;
  health: HealthApi;
  assistant: AssistantApi;
  jobs: JobsApi;
  dashboard: DashboardApi;
  settings: SettingsApi;
  dreams: DreamsApi;
  skills: SkillsApi;
  workHours: WorkHoursApi;
  /**
   * Provider-supplied React hook that returns the active feature's route
   * builders. A hook (not a plain object) so the implementation can read
   * the current URL via `useParams` / `useLocation`.
   */
  useThreadRoutes: () => ThreadRoutes;
  useTasksRoutes: () => TasksRoutes;
  useKbRoutes: () => KbRoutes;
  useAgentsRoutes: () => AgentsRoutes;
  useJobsRoutes: () => JobsRoutes;
  useDreamsRoutes: () => DreamsRoutes;
}

export const DataContext = createContext<DataContextValue | null>(null);

export function useData(): DataContextValue {
  const ctx = useContext(DataContext);
  if (!ctx) {
    throw new Error(
      'useData must be inside <AppProvider> or <PrototypeProvider>.',
    );
  }
  return ctx;
}
