/**
 * Tasks list — Direction-A Pasture, roots-only dense list.
 *
 * Group by Status / Agent / Thread. Each group renders a lightweight sans
 * label (dot + name + count) above a rounded bordered Pasture rows-card
 * (a-tasks mockup). Resolved groups are visually dimmed.
 * Status pills follow ds.css .tag (rounded-pill, led dot).
 *
 * Per founder ruling: NO in-list 'show subtasks' toggle. The list is
 * roots-only; execution subtasks live on the Task detail surface.
 *
 * Driven by GET /tasks/roots (roots-only invariant). Cursor pagination
 * via next_cursor with IntersectionObserver sentinel.
 *
 * THR-046 msg-11: wider full-width layout, cream canvas, STATUS/TASK split
 * columns, rounded column-header bar, rounded bordered group-section cards,
 * right-aligned group-by segmented control.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { AlertCircle, RefreshCw } from 'lucide-react';
import { Tabs, TabsList, TabsTrigger } from '@/design-system/primitives/Tabs';
import { Button } from '@/design-system/primitives/Button';
import { EmptyState } from '@/design-system/patterns/EmptyState';
import { ContentWrap } from '@/design-system/layouts/ContentWrap/ContentWrap';
import {
  TaskListColumnHeader,
  TaskListRow,
  severityRollupStatus,
} from './TaskListRow';
import { useTasksRootsInfinite, useTasksRoutes } from '@/hooks/tasks';
import type { TaskRecord } from '@/lib/api/types';
import { useOrgSlugOptional } from '@/lib/orgSlug';

type GroupBy = 'status' | 'agent' | 'thread';

const GROUP_BY_OPTIONS: { value: GroupBy; label: string }[] = [
  { value: 'status', label: 'Status' },
  { value: 'agent', label: 'Agent' },
  { value: 'thread', label: 'Thread' },
];

/**
 * Colored status dot per group. Status groups map to the same semantic tokens
 * StatusBadge uses; non-status groups (agent / thread) carry a neutral dot —
 * we never claim a status color for a dimension that has no single status.
 */
type GroupDot =
  | 'in_progress'
  | 'pending'
  | 'escalated'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'superseded'
  | 'neutral';

const DOT_COLOR: Record<GroupDot, string> = {
  in_progress: 'text-status-open',
  pending: 'text-status-archiving',
  escalated: 'text-status-escalated',
  completed: 'text-status-open',
  failed: 'text-status-abandoned',
  cancelled: 'text-status-archived',
  superseded: 'text-status-archived',
  neutral: 'text-text-muted',
};

const STATUS_DOT_KEYS = new Set<string>([
  'in_progress',
  'pending',
  'escalated',
  'completed',
  'failed',
  'cancelled',
  'superseded',
]);

function groupDot(key: string, by: GroupBy): GroupDot {
  if (by === 'status' && STATUS_DOT_KEYS.has(key)) return key as GroupDot;
  return 'neutral';
}

/**
 * Pasture group section label (a-tasks mockup .grp-head) — a lightweight
 * sans heading that sits ABOVE its rows-card: colored status dot + name +
 * a plain muted count (TASKS-04). Both derivations are pure client-side reads
 * of the already-loaded roots payload — no fetch, no fabrication.
 */
function GroupHeading({
  label,
  count,
  dot,
  dimmed,
}: {
  label: string;
  count: number;
  dot: GroupDot;
  dimmed?: boolean;
}): JSX.Element {
  return (
    <h2 className="mb-2 flex items-center gap-2 px-0.5">
      <span
        aria-hidden
        className={`inline-block h-2 w-2 shrink-0 rounded-full bg-current ${DOT_COLOR[dot]}`}
      />
      <span
        className={`text-sm font-semibold tracking-tight ${
          dimmed ? 'text-text-muted' : 'text-text-primary'
        }`}
      >
        {label}
      </span>
      <span className="text-text-muted text-xs tabular-nums">{count}</span>
    </h2>
  );
}

function groupKey(task: TaskRecord, by: GroupBy): string {
  switch (by) {
    case 'status':
      return task.status;
    case 'agent':
      return task.assigned_agent || 'Unassigned';
    case 'thread': {
      const threadId = (task as Record<string, unknown>).dispatched_from_thread_id;
      if (threadId && typeof threadId === 'string' && threadId.length > 0) {
        return threadId;
      }
      return 'No thread';
    }
  }
}

function groupLabel(key: string, by: GroupBy): string {
  if (by === 'status') {
    const map: Record<string, string> = {
      pending: 'Pending',
      in_progress: 'Active',
      escalated: 'Waiting on you',
      completed: 'Completed',
      failed: 'Failed',
      cancelled: 'Cancelled',
      superseded: 'Resolved',
    };
    return map[key] ?? key;
  }
  return key;
}

function isResolvedGroup(key: string, by: GroupBy): boolean {
  if (by === 'status') {
    // Terminal/dimmed set. `cancelled` is terminal (muted, calmer than
    // completed); `escalated` is an attention state and stays undimmed.
    return (
      key === 'completed' ||
      key === 'failed' ||
      key === 'cancelled' ||
      key === 'superseded'
    );
  }
  return false;
}

const GROUP_ORDER_STATUS: Record<string, number> = {
  escalated: 0,
  in_progress: 1,
  pending: 2,
  completed: 3,
  failed: 4,
  cancelled: 5,
  superseded: 6,
};

export function TasksPage(): JSX.Element {
  const [groupBy, setGroupBy] = useState<GroupBy>('status');
  const [isRetrying, setIsRetrying] = useState(false);
  const [nextPageError, setNextPageError] = useState(false);
  const routes = useTasksRoutes();
  const orgSlug = useOrgSlugOptional();
  const queryClient = useQueryClient();
  const tasksQuery = useTasksRootsInfinite();

  const allTasks = useMemo(
    () => tasksQuery.data?.pages.flatMap((p) => p.tasks) ?? [],
    [tasksQuery.data],
  );

  // Page eyebrow — derived ONLY from already-loaded roots-list fields
  // (no extra fetch, no fabrication). "Waiting on you" = roots escalated to
  // the founder (THR-037 Change B: the top-level `escalated` status); "Failed"
  // uses the same severity rollup the rows display. "Subtasks roll up" is a
  // static, honest descriptor of the roots payload (it carries severity_rollup).
  const eyebrow = useMemo(() => {
    const waitingOnYou = allTasks.filter(
      (t) => t.status === 'escalated',
    ).length;
    const failed = allTasks.filter(
      (t) => severityRollupStatus(t) === 'failed',
    ).length;
    return [
      `${allTasks.length} ROOT TASKS`,
      'SUBTASKS ROLL UP',
      `${waitingOnYou} WAITING ON YOU`,
      `${failed} FAILED`,
    ].join(' · ');
  }, [allTasks]);

  // Group tasks by the active dimension, sorted by group priority then recency.
  const groups = useMemo(() => {
    const map = new Map<string, TaskRecord[]>();
    for (const t of allTasks) {
      const k = groupKey(t, groupBy);
      const list = map.get(k);
      if (list) list.push(t);
      else map.set(k, [t]);
    }
    // Sort within each group by updated_at desc (most recent first).
    for (const [, tasks] of map) {
      tasks.sort(
        (a, b) =>
          new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
      );
    }
    // Sort groups: by explicit order for status, alpha for others.
    const entries = [...map.entries()];
    if (groupBy === 'status') {
      entries.sort(
        (a, b) =>
          (GROUP_ORDER_STATUS[a[0]] ?? 99) -
          (GROUP_ORDER_STATUS[b[0]] ?? 99),
      );
    } else {
      entries.sort((a, b) => a[0].localeCompare(b[0]));
    }
    return entries;
  }, [allTasks, groupBy]);

  // Sentinel observer for infinite scroll.
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const { fetchNextPage, hasNextPage, isFetchingNextPage } = tasksQuery;
  const loadNextPage = useCallback(async () => {
    setNextPageError(false);
    try {
      const result = await fetchNextPage();
      const failed =
        typeof result === 'object' &&
        result !== null &&
        'isFetchNextPageError' in result &&
        result.isFetchNextPageError === true;
      setNextPageError(failed);
    } catch {
      setNextPageError(true);
    }
  }, [fetchNextPage]);
  useEffect(() => {
    const node = sentinelRef.current;
    if (!node || !hasNextPage || nextPageError) return;
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && !isFetchingNextPage) {
          void loadNextPage();
        }
      },
      { rootMargin: '200px' },
    );
    obs.observe(node);
    return () => obs.disconnect();
  }, [hasNextPage, isFetchingNextPage, loadNextPage, nextPageError]);

  const isLoading = tasksQuery.isLoading;
  const hasUsableTasks = allTasks.length > 0;
  const retry = async () => {
    setIsRetrying(true);
    try {
      await queryClient.refetchQueries({
        queryKey: ['tasks-roots-infinite', orgSlug, undefined],
        exact: true,
      });
    } finally {
      setIsRetrying(false);
    }
  };

  return (
    <div className="bg-surface-canvas flex h-full flex-col">
      {/* Pinned page header: eyebrow + title (left), group-by selector (right).
          EM ruling (THR-099 plan flag #3): the header STAYS pinned — it is not
          folded into the scroll body. Its inner content is capped by the shared
          <ContentWrap> so the header columns align exactly above the scroll-body
          columns at the 1180 `max-w-content` cap with matching 26px horizontal
          padding. ContentWrap's overflow-y-auto is inert on this shrink-0,
          content-height header (it never scrolls); we reuse it purely to share
          the identical cap+padding as the body without hand-rolling arbitrary
          values, which eslint forbids in the feature layer. The full-width
          border-b stays on the outer <header>. */}
      <header className="border-border-default bg-surface-page shrink-0 border-b">
        <ContentWrap>
        <div data-testid="tasks-page-header" className="flex flex-col items-start justify-between gap-4 sm:flex-row">
          <div className="min-w-0">
            <p className="text-text-muted text-xs font-medium tracking-wide uppercase">
              {eyebrow}
            </p>
            <h1 className="font-display text-text-primary mt-1 text-2xl font-medium">
              What the org is working on
            </h1>
          </div>
          <Tabs
            className="shrink-0"
            value={groupBy}
            onValueChange={(v) => setGroupBy(v as GroupBy)}
          >
            <TabsList
              aria-label="Group by"
              className="border-border-default bg-surface-sunken gap-0.5 rounded-lg border p-0.5"
            >
              {GROUP_BY_OPTIONS.map((opt) => (
                <TabsTrigger
                  key={opt.value}
                  value={opt.value}
                  className="data-[state=active]:bg-accent-soft data-[state=active]:text-accent-text rounded-md px-3 py-1"
                >
                  {opt.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </div>
        </ContentWrap>
      </header>

      {/* Scroll body — same <ContentWrap> cap as the pinned header so its
          columns sit directly under the header's columns (1180 max-w-content,
          26px horizontal padding). <main> is the flex sizer; ContentWrap owns
          the scroll surface. */}
      <main className="min-h-0 flex-1">
        <ContentWrap>
        {isLoading ? (
          <p className="text-text-muted py-6 text-center text-sm">Loading…</p>
        ) : tasksQuery.isError && !hasUsableTasks ? (
          <EmptyState
            icon={<AlertCircle size={32} className="text-feedback-danger" />}
            title="Could not load tasks"
            body="The server returned an error. You can try again."
            cta={{ label: isRetrying ? 'Retrying…' : 'Retry', onClick: retry }}
          />
        ) : allTasks.length === 0 ? (
          <EmptyState title="No tasks" body="No tasks match the current filters." />
        ) : (
          <div className="space-y-6">
            {tasksQuery.isError && !nextPageError && (
              <div
                role="alert"
                className="border-feedback-danger bg-danger-soft flex flex-wrap items-center justify-between gap-3 rounded-lg border p-4"
              >
                <div className="flex min-w-0 items-center gap-2">
                  <AlertCircle className="text-feedback-danger shrink-0" size={18} aria-hidden />
                  <div>
                    <p className="text-text-primary text-sm font-medium">Tasks may be out of date</p>
                    <p className="text-text-secondary text-xs">Some task updates could not be loaded.</p>
                  </div>
                </div>
                <Button size="sm" variant="outline" onClick={retry} loading={isRetrying}>
                  <RefreshCw size={14} aria-hidden />
                  Retry
                </Button>
              </div>
            )}
            <style>{`@media (max-width: 767px) {
              [data-tasks-responsive-list] > div:first-child { display: none; }
              [data-tasks-responsive-list] section li > div > a {
                display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: .5rem .75rem; align-items: center;
              }
              [data-tasks-responsive-list] section li > div > a > div { width: auto; min-width: 0; }
              [data-tasks-responsive-list] section li > div > a > div:nth-child(1) { grid-column: 1; grid-row: 1; }
              [data-tasks-responsive-list] section li > div > a > div:nth-child(2) { grid-column: 2; grid-row: 1; }
              [data-tasks-responsive-list] section li > div > a > div:nth-child(3) { grid-column: 1 / -1; grid-row: 2; overflow: visible; }
              [data-tasks-responsive-list] section li > div > a > div:nth-child(3) > span { white-space: normal; overflow: visible; text-overflow: clip; }
              [data-tasks-responsive-list] section li > div > a > div:nth-child(4) { grid-column: 1; grid-row: 3; }
              [data-tasks-responsive-list] section li > div > a > div:nth-child(5) { grid-column: 2; grid-row: 3; }
              [data-tasks-responsive-list] section li > div > a > div:nth-child(6) { grid-column: 2; grid-row: 4; justify-self: end; }
            }`}</style>
            <div data-testid="tasks-responsive-list" data-tasks-responsive-list>
              <TaskListColumnHeader />
            {groups.map(([key, tasks]) => {
              const dimmed = isResolvedGroup(key, groupBy);
              return (
                <div key={key} className={dimmed ? 'opacity-60' : ''}>
                  <GroupHeading
                    label={groupLabel(key, groupBy)}
                    count={tasks.length}
                    dot={groupDot(key, groupBy)}
                    dimmed={dimmed}
                  />
                  <section className="border-border-default bg-surface-page rounded-xl border">
                    <ul>
                      {tasks.map((t) => (
                        <li key={t.task_id}>
                          <TaskListRow
                            task={t}
                            to={routes.detail(t.task_id)}
                            taskRoutes={routes}
                          />
                        </li>
                      ))}
                    </ul>
                  </section>
                </div>
              );
            })}
            </div>
            <div ref={sentinelRef} aria-hidden className="h-1" />
            {isFetchingNextPage && (
              <p className="text-text-muted py-3 text-center text-sm">
                Loading more…
              </p>
            )}
            {nextPageError && (
              <div role="alert" className="border-feedback-danger bg-danger-soft flex flex-wrap items-center justify-between gap-3 rounded-lg border p-4">
                <div className="flex min-w-0 items-center gap-2">
                  <AlertCircle className="text-feedback-danger shrink-0" size={18} aria-hidden />
                  <div>
                    <p className="text-text-primary text-sm font-medium">Could not load more tasks</p>
                    <p className="text-text-secondary text-xs">Loaded tasks are still available.</p>
                  </div>
                </div>
                <Button size="sm" variant="outline" aria-label="Retry loading more tasks" onClick={() => void loadNextPage()} loading={isFetchingNextPage}>
                  <RefreshCw size={14} aria-hidden />
                  Retry
                </Button>
              </div>
            )}
            {!tasksQuery.isError && !nextPageError && !hasNextPage && allTasks.length > 0 && (
              <p className="text-text-muted py-4 text-center text-xs">
                End of list
              </p>
            )}
          </div>
        )}
        </ContentWrap>
      </main>
    </div>
  );
}
