import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { Link, MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, test, vi } from 'vitest';
import { AppRoutes } from '@/routes';
import { renderWithProviders } from '@/test/render';
import { server } from '@/test/server';
import { AppProvider, makeQueryClient } from '@/design-system/providers/AppProvider';
import * as api from '@/lib/api';
import type { SSEOptions } from '@/lib/api';
import type { ActiveChainResponse, JobRecord, TaskEvent, TaskRecord } from '@/lib/api/types';

const SLUG = 'hk-macau-tourism';

afterEach(() => {
  vi.restoreAllMocks();
});

function mountAt(route: string) {
  server.use(
    http.get('/api/v1/orgs', () =>
      HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
    ),
  );
  return renderWithProviders(<AppRoutes />, { route });
}

/** A root task fixture with severity_rollup (roots endpoint field). */
function rootTask(overrides?: Partial<TaskRecord> & Record<string, unknown>): TaskRecord {
  return {
    task_id: 'TASK-0091',
    team: 'content',
    brief: 'Draft Hong Kong visa guide v2',
    status: 'completed',
    block_kind: null,
    parent_task_id: null,
    revisit_of_task_id: null,
    created_at: '2026-05-18T10:00:00Z',
    updated_at: '2026-05-18T10:06:12Z',
    closed_at: null,
    cancelled_at: null,
    session_timeout_seconds: null,
    severity_rollup: 'completed',
    ...overrides,
  } as TaskRecord;
}

const TASK = rootTask({ status: 'in_progress', severity_rollup: 'in_progress' });

const JOB: JobRecord = {
  id: 'JOB-0001',
  task_id: 'TASK-0091',
  agent_name: 'content_writer',
  title: 'Generate sitemap',
  rationale: 'SEO improvement.',
  script_text: 'python3 gen_sitemap.py',
  interpreter: 'bash',
  cwd_hint: null,
  status: 'completed',
  exit_code: 0,
  stdout_head: null,
  stderr_head: null,
  stdout_path: null,
  stderr_path: null,
  duration_ms: 800,
  started_at: '2026-05-18T10:02:00Z',
  finished_at: '2026-05-18T10:02:01Z',
  reviewed_at: null,
  reviewed_by: null,
  reject_reason: null,
  cwd_resolved: null,
  max_runtime_seconds: 300,
  max_output_bytes: 52428800,
  review_required: false,
  persistent: false,
  reason: null,
  created_at: '2026-05-18T10:01:00Z',
};

describe('TasksPage — read path (roots endpoint)', () => {
  test('keeps initial loading distinct from empty', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, async () => {
        await new Promise(() => undefined);
        return HttpResponse.json({ tasks: [] });
      }),
    );

    mountAt(`/orgs/${SLUG}/tasks`);

    expect(screen.getByText('Loading…')).toBeInTheDocument();
    expect(screen.queryByText('No tasks')).not.toBeInTheDocument();
  });

  test('renders an initial request failure with a keyboard-actionable Retry, never empty', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    let requests = 0;
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () => {
        requests += 1;
        return requests === 1
          ? new HttpResponse(null, { status: 500 })
          : HttpResponse.json({ tasks: [TASK], next_cursor: null });
      }),
    );
    const user = userEvent.setup();

    mountAt(`/orgs/${SLUG}/tasks`);

    expect(await screen.findByText('Could not load tasks')).toBeInTheDocument();
    expect(screen.queryByText('No tasks')).not.toBeInTheDocument();
    const retry = screen.getByRole('button', { name: 'Retry' });
    retry.focus();
    expect(retry).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(await screen.findByText(/Draft Hong Kong visa guide/)).toBeInTheDocument();
    expect(requests).toBe(2);
  });

  test('reserves the empty state for a successful zero-row response', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [], next_cursor: null }),
      ),
    );

    mountAt(`/orgs/${SLUG}/tasks`);

    expect(await screen.findByText('No tasks')).toBeInTheDocument();
    expect(screen.queryByText('Could not load tasks')).not.toBeInTheDocument();
  });

  test('retains populated cached rows when a stale refetch fails and retries all loaded pages', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    const queryClient = makeQueryClient();
    queryClient.setQueryData(['tasks-roots-infinite', SLUG, undefined], {
      pages: [
        { tasks: [TASK], next_cursor: 'page-2' },
        { tasks: [rootTask({ task_id: 'TASK-0092', brief: 'Cached second page' })], next_cursor: null },
      ],
      pageParams: [undefined, 'page-2'],
    });
    let shouldFail = true;
    const requestedBefore: string[] = [];
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, ({ request }) => {
        const before = new URL(request.url).searchParams.get('before') ?? 'first';
        requestedBefore.push(before);
        if (shouldFail) return new HttpResponse(null, { status: 500 });
        return HttpResponse.json(
          before === 'first'
            ? { tasks: [TASK], next_cursor: 'page-2' }
            : { tasks: [rootTask({ task_id: 'TASK-0092', brief: 'Recovered second page' })], next_cursor: null },
        );
      }),
    );
    render(
      <MemoryRouter initialEntries={[`/orgs/${SLUG}/tasks`]}>
        <AppProvider client={queryClient}><AppRoutes /></AppProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText(/Draft Hong Kong visa guide/)).toBeInTheDocument();
    await act(() => queryClient.invalidateQueries({
      queryKey: ['tasks-roots-infinite', SLUG, undefined],
      exact: true,
    }));
    expect(await screen.findByText('Tasks may be out of date')).toBeInTheDocument();
    expect(screen.getByText('Cached second page')).toBeInTheDocument();
    expect(screen.queryByText('End of list')).not.toBeInTheDocument();

    shouldFail = false;
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Recovered second page')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('Tasks may be out of date')).not.toBeInTheDocument());
    expect(requestedBefore).toEqual(['first', 'first', 'page-2']);
  });

  test('retains the first page when fetching the next page fails and retries that page only', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    let intersect: IntersectionObserverCallback | undefined;
    vi.stubGlobal('IntersectionObserver', class {
      constructor(callback: IntersectionObserverCallback) { intersect = callback; }
      observe() {}
      disconnect() {}
      unobserve() {}
      takeRecords() { return []; }
      root = null;
      rootMargin = '';
      thresholds = [];
    });
    const requestedBefore: string[] = [];
    let nextAttempts = 0;
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, ({ request }) => {
        const before = new URL(request.url).searchParams.get('before') ?? 'first';
        requestedBefore.push(before);
        if (before === 'first') {
          return HttpResponse.json({ tasks: [TASK], next_cursor: 'page-2' });
        }
        nextAttempts += 1;
        return nextAttempts === 1
          ? new HttpResponse(null, { status: 500 })
          : HttpResponse.json({
              tasks: [rootTask({ task_id: 'TASK-0092', brief: 'Recovered next page' })],
              next_cursor: null,
            });
      }),
    );

    mountAt(`/orgs/${SLUG}/tasks`);
    expect(await screen.findByText(/Draft Hong Kong visa guide/)).toBeInTheDocument();
    await act(async () => intersect?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver));

    expect(await screen.findByText('Could not load more tasks')).toBeInTheDocument();
    expect(screen.getByText(/Draft Hong Kong visa guide/)).toBeInTheDocument();
    expect(screen.queryByText('End of list')).not.toBeInTheDocument();
    expect(screen.queryByText('Loading more…')).not.toBeInTheDocument();

    const retry = screen.getByRole('button', { name: 'Retry loading more tasks' });
    retry.focus();
    expect(retry).toHaveFocus();
    await userEvent.keyboard('{Enter}');

    expect(await screen.findByText('Recovered next page')).toBeInTheDocument();
    expect(screen.queryByText('Could not load more tasks')).not.toBeInTheDocument();
    expect(await screen.findByText('End of list')).toBeInTheDocument();
    expect(requestedBefore).toEqual(['first', 'page-2', 'page-2']);
  });

  test('provides a bounded mobile layout while retaining the desktop table', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK], next_cursor: null }),
      ),
    );

    mountAt(`/orgs/${SLUG}/tasks`);
    expect(await screen.findByText(/Draft Hong Kong visa guide/)).toBeInTheDocument();
    expect(screen.getByTestId('tasks-responsive-list')).toHaveAttribute('data-tasks-responsive-list');
    expect(screen.getByTestId('tasks-page-header')).toHaveClass('flex-col', 'sm:flex-row');
    expect(screen.getByText('Draft Hong Kong visa guide v2')).toBeInTheDocument();
    expect(document.querySelector('style')?.textContent).toContain('@media (max-width: 767px)');
  });

  test('fetches from /tasks/roots and renders fixture tasks', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    await waitFor(() =>
      expect(screen.getByText(/Draft Hong Kong visa guide/)).toBeInTheDocument(),
    );
  });

  test('renders group-by selector tabs', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    await waitFor(() => {
      expect(
        screen.getByRole('heading', { name: 'What the org is working on' }),
      ).toBeInTheDocument();
      expect(screen.getByRole('tab', { name: 'Status' })).toBeInTheDocument();
      expect(screen.getByRole('tab', { name: 'Agent' })).toBeInTheDocument();
      expect(screen.getByRole('tab', { name: 'Thread' })).toBeInTheDocument();
    });
  });

  test('groups tasks by status with group heading', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    await waitFor(() => {
      expect(screen.getByText(/Active/)).toBeInTheDocument();
    });
  });

  // TASKS-04: group-by control is a segmented control (not plain text tabs).
  test('renders the group-by control as a bordered segmented control (TASKS-04)', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    const tablist = await screen.findByRole('tablist', { name: 'Group by' });
    // Segmented = a grouped, bordered, rounded container — not plain text tabs.
    expect(tablist).toHaveClass('rounded-lg');
    expect(tablist).toHaveClass('border');
    // The active segment ('Status', the default) carries the accent fill.
    expect(screen.getByRole('tab', { name: 'Status' })).toHaveClass(
      'data-[state=active]:bg-accent-soft',
    );
  });

  // TASKS-04: group headers carry a count badge + a colored status dot, both
  // pure client-side derivations of the already-loaded roots payload.
  test('group headers carry a count badge and a colored status dot (TASKS-04)', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    const a = rootTask({
      task_id: 'TASK-0400',
      status: 'in_progress',
      severity_rollup: 'in_progress',
      brief: 'First running root',
    });
    const b = rootTask({
      task_id: 'TASK-0401',
      status: 'in_progress',
      severity_rollup: 'in_progress',
      brief: 'Second running root',
    });
    const c = rootTask({
      task_id: 'TASK-0402',
      status: 'pending',
      severity_rollup: 'pending',
      brief: 'Awaiting pickup',
    });
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [a, b, c] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    const inProgress = await screen.findByRole('heading', {
      name: /Active/,
    });
    // Count badge reflects the client-side group size (2 in_progress roots).
    expect(within(inProgress).getByText('2')).toBeInTheDocument();
    // Colored status dot uses the green 'open' token for in_progress.
    const dot = inProgress.querySelector('span[aria-hidden="true"]');
    expect(dot).not.toBeNull();
    expect(dot).toHaveClass('text-status-open');
    // The pending group shows a count of 1.
    const pending = screen.getByRole('heading', { name: /Pending/ });
    expect(within(pending).getByText('1')).toBeInTheDocument();
  });

  test('renders severity_rollup as inline subtitle in the title column', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    // Root is pending but has an escalated child → severity_rollup = 'escalated'
    // (Path B: escalated is the worst rollup severity).
    const taskWithRollup = rootTask({
      task_id: 'TASK-0100',
      status: 'pending',
      severity_rollup: 'escalated',
      brief: 'Root task that has a stuck child',
    });
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [taskWithRollup] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    await waitFor(() => {
      // STATUS column shows the primary status ('pending'), NOT the rollup.
      expect(screen.getByText('pending')).toBeInTheDocument();
      // The rollup renders inline in the TITLE column as "subtask escalated".
      expect(screen.getByText('subtask escalated')).toBeInTheDocument();
      expect(screen.getByText(/Root task that has a stuck child/)).toBeInTheDocument();
    });
  });

  // TASKS-05: root rows surface the worst-child rollup inline when a descendant
  // sits in a strictly-worse state than the root itself. Pure client-side
  // derivation of severity_rollup vs the root's own status; count-free (the
  // count-decorated design form "1 of 2 subtasks blocked" needs per-status
  // subtask counts that the roots payload does not carry — deferred).
  //
  // Also verifies the STATUS column carries only the compact primary status
  // (no block_kind qualifier) when the rollup matches.
  test('surfaces worst-child subtask rollup inline on root rows (TASKS-05)', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    // Root is in_progress but a descendant is escalated → severity_rollup='escalated'.
    const worseChild = rootTask({
      task_id: 'TASK-0500',
      status: 'in_progress',
      severity_rollup: 'escalated',
      brief: 'Root in progress with a stuck child',
    });
    // Root with no worse descendant (rollup === own status) → no inline rollup.
    const noWorseChild = rootTask({
      task_id: 'TASK-0501',
      status: 'in_progress',
      severity_rollup: 'in_progress',
      brief: 'Root in progress all subtasks fine',
    });
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [worseChild, noWorseChild] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    // The worse-child root names the worst descendant status inline, colored
    // with the escalated token.
    const rollup = await screen.findByText('subtask escalated');
    expect(rollup).toHaveClass('text-status-escalated');
    // The healthy root surfaces no inline rollup (no fabricated subtask state).
    expect(screen.queryByText('subtask in progress')).not.toBeInTheDocument();
    // STATUS column for the worse-child root shows compact primary 'in_progress'
    // (the task's own status, NOT the severity rollup).
    const worseRow = screen.getByText('TASK-0500').closest('a')!;
    expect(within(worseRow).getByText('in_progress')).toBeInTheDocument();
    // STATUS column for the no-worse-child root also shows compact 'in_progress'.
    const healthyRow = screen.getByText('TASK-0501').closest('a')!;
    expect(within(healthyRow).getByText('in_progress')).toBeInTheDocument();
  });

  test('stacks subtask rollup under title inside the title column', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    const taskWithRollup = rootTask({
      task_id: 'TASK-0502',
      status: 'in_progress',
      severity_rollup: 'escalated',
      brief:
        'A long root title that needs truncation before it can collide with the task id column',
    });
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [taskWithRollup] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);

    const rollup = await screen.findByText('subtask escalated');
    const title = screen.getByText(/A long root title/);
    // The rollup lives inside a nested context div, which is a child of the
    // outer title column (the title headline's parent).
    const contextRow = rollup.parentElement!;
    const titleColumn = contextRow.parentElement!;

    expect(titleColumn).toBe(title.parentElement);
    expect(titleColumn).toHaveClass('min-w-0');
    expect(titleColumn).toHaveClass('flex-1');
    expect(titleColumn).toHaveClass('flex-col');
    expect(titleColumn).toHaveClass('items-start');
    expect(title).toHaveClass('w-full');
    expect(title).toHaveClass('min-w-0');
    expect(rollup).not.toHaveClass('shrink-0');
    expect(rollup).toHaveClass('max-w-full');
    expect(rollup).toHaveClass('overflow-hidden');
    // Context row clips inside the title column.
    expect(contextRow).toHaveClass('max-w-full');
    expect(contextRow).toHaveClass('overflow-hidden');
    expect(contextRow).toHaveClass('whitespace-nowrap');
  });

  test('groups by thread on dispatched_from_thread_id, with no-thread bucket', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    const threaded = rootTask({
      task_id: 'TASK-0200',
      dispatched_from_thread_id: 'THR-0030',
      status: 'in_progress',
      severity_rollup: 'in_progress',
    });
    const unthreaded = rootTask({
      task_id: 'TASK-0201',
      team: 'engineering',
      status: 'pending',
      severity_rollup: 'pending',
    });
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [threaded, unthreaded] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    // Switch to the Thread group-by tab
    const user = userEvent.setup();
    const threadTab = await screen.findByRole('tab', { name: 'Thread' });
    await user.click(threadTab);
    await waitFor(() => {
      // THR-0030 appears as the group heading AND as the row's thread chip,
      // so multiple matches are expected; plus a "No thread" group heading.
      expect(screen.getAllByText('THR-0030').length).toBeGreaterThan(0);
      expect(screen.getByText('No thread')).toBeInTheDocument();
    });
  });

  test('renders supersede/revisit links from roots payload fields', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    const superseder = rootTask({
      task_id: 'TASK-0300',
      revisit_of_task_id: 'TASK-0299',
      direct_revisits: ['TASK-0301'],
      status: 'completed',
      severity_rollup: 'completed',
    });
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [superseder] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    await waitFor(() => {
      expect(screen.getByText(/supersedes/)).toBeInTheDocument();
      expect(screen.getByText(/TASK-0299/)).toBeInTheDocument();
      expect(screen.getByText(/superseded by/)).toBeInTheDocument();
      expect(screen.getByText(/TASK-0301/)).toBeInTheDocument();
    });

    // Lineage links carry correct hrefs
    const supersedesLink = screen.getByRole('link', { name: /supersedes TASK-0299/ });
    expect(supersedesLink).toHaveAttribute('href', `/orgs/${SLUG}/tasks/TASK-0299`);
    const supersededByLink = screen.getByRole('link', { name: /superseded by TASK-0301/ });
    expect(supersededByLink).toHaveAttribute('href', `/orgs/${SLUG}/tasks/TASK-0301`);
  });

  test('renders 0 count when query resolves to empty (no loading placeholder)', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    await waitFor(() => {
      // Empty state, not a loading indicator
      expect(screen.getByText(/No tasks match/)).toBeInTheDocument();
    });
  });
});

// THR-037 Change B Phase 2: the status-GROUP header maps must speak the Path-B
// vocabulary. `escalated` is a first-class attention group (red dot, surfaced
// early); `cancelled` is a calm terminal group (muted dot, dimmed/terminal set);
// `blocked` is fully retired from this presentation surface.
describe('TasksPage — Path-B status group vocabulary (THR-037 Change B Phase 2)', () => {
  function mountStatuses(tasks: TaskRecord[]) {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks }),
      ),
    );
    return mountAt(`/orgs/${SLUG}/tasks`);
  }

  test('escalated group renders the red attention dot + a proper label and sorts early', async () => {
    const running = rootTask({
      task_id: 'TASK-0600',
      status: 'in_progress',
      severity_rollup: 'in_progress',
      brief: 'A healthy running root',
    });
    const escalated = rootTask({
      task_id: 'TASK-0601',
      status: 'escalated',
      severity_rollup: 'escalated',
      brief: 'A root escalated to the founder',
    });
    mountStatuses([running, escalated]);

    // Proper display label (not a raw-lowercase fallback). THR-046 msg-11:
    // "Waiting on you" is the escalated attention group label.
    const escalatedHeading = await screen.findByRole('heading', {
      name: /Waiting on you/,
    });
    // Red attention dot — the SAME token StatusBadge uses for escalated.
    const dot = escalatedHeading.querySelector('span[aria-hidden="true"]');
    expect(dot).not.toBeNull();
    expect(dot).toHaveClass('text-status-escalated');

    // Sorts EARLY: the escalated attention group precedes the in_progress group
    // in document order (first-class attention, surfaced near the top).
    const activeHeading = screen.getByRole('heading', { name: /Active/ });
    expect(
      escalatedHeading.compareDocumentPosition(activeHeading) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // Escalated is an ATTENTION state, NOT dimmed/terminal. THR-061 a-tasks:
    // the group label sits above its rows-card, so the dimming lives on the
    // heading's wrapper (its parent), not on the rows-card section.
    expect(escalatedHeading.parentElement).not.toHaveClass('opacity-60');
  });

  test('cancelled group renders the muted/terminal treatment and is in the dimmed set', async () => {
    const cancelled = rootTask({
      task_id: 'TASK-0602',
      status: 'cancelled',
      severity_rollup: 'cancelled',
      brief: 'A cancelled root',
    });
    mountStatuses([cancelled]);

    const cancelledHeading = await screen.findByRole('heading', {
      name: /Cancelled/,
    });
    // Muted/terminal dot — the SAME token StatusBadge uses for cancelled
    // (mirrors superseded).
    const dot = cancelledHeading.querySelector('span[aria-hidden="true"]');
    expect(dot).not.toBeNull();
    expect(dot).toHaveClass('text-status-archived');

    // Cancelled sits in the terminal/dimmed set (calmer than completed).
    // Dimming is on the heading's wrapper (a-tasks: label above rows-card).
    expect(cancelledHeading.parentElement).toHaveClass('opacity-60');
  });

  test('no `blocked` group label or dot path remains on this surface', async () => {
    // Render the full Path-B vocabulary; no surface should fall back to the
    // retired `blocked` label or its dot token.
    const tasks = [
      rootTask({ task_id: 'TASK-0610', status: 'in_progress', severity_rollup: 'in_progress' }),
      rootTask({ task_id: 'TASK-0611', status: 'escalated', severity_rollup: 'escalated' }),
      rootTask({ task_id: 'TASK-0612', status: 'cancelled', severity_rollup: 'cancelled' }),
      rootTask({ task_id: 'TASK-0613', status: 'completed', severity_rollup: 'completed' }),
    ];
    mountStatuses(tasks);

    await screen.findByRole('heading', { name: /Active/ });
    // No retired `blocked` group heading.
    expect(screen.queryByRole('heading', { name: /Blocked/ })).toBeNull();
    // No retired blocked dot token anywhere in the rendered surface.
    expect(document.querySelector('.text-status-blocked')).toBeNull();
  });
});

describe('TasksPage — Direction-A list reshape (THR-030 TASKS-01/02/03)', () => {
  function mountTasks(tasks: TaskRecord[]) {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks }),
      ),
    );
    return mountAt(`/orgs/${SLUG}/tasks`);
  }

  // TASKS-03: page eyebrow (derived from loaded list data) + serif title.
  test('renders serif title and a derived eyebrow with root/waiting/failed counts', async () => {
    const running = rootTask({
      task_id: 'TASK-0400',
      status: 'in_progress',
      severity_rollup: 'in_progress',
    });
    const escalated = rootTask({
      task_id: 'TASK-0401',
      status: 'escalated',
      severity_rollup: 'escalated',
    });
    const failed = rootTask({
      task_id: 'TASK-0402',
      status: 'failed',
      severity_rollup: 'failed',
    });
    mountTasks([running, escalated, failed]);

    // Serif title replaces the bare "Tasks" heading.
    expect(
      await screen.findByRole('heading', { name: 'What the org is working on' }),
    ).toBeInTheDocument();

    // Eyebrow derives from loaded list data: 3 roots · 1 waiting on you
    // (escalated) · 1 failed (rollup). Wait for the roots query to populate
    // (the static header renders before the fetch resolves).
    await waitFor(() =>
      expect(screen.getByText(/ROOT TASKS/)).toHaveTextContent('3 ROOT TASKS'),
    );
    const eyebrow = screen.getByText(/ROOT TASKS/);
    expect(eyebrow).toHaveTextContent('SUBTASKS ROLL UP');
    expect(eyebrow).toHaveTextContent('1 WAITING ON YOU');
    expect(eyebrow).toHaveTextContent('1 FAILED');
  });

  // TASKS-01: column header row aligned above the rows. THR-041: STATUS · TASK · TITLE · AGENT · THREAD · UPDATED.
  test('renders the STATUS · TASK · TITLE · AGENT · THREAD · UPDATED column header row', async () => {
    mountTasks([
      rootTask({ task_id: 'TASK-0410', status: 'in_progress', severity_rollup: 'in_progress' }),
    ]);
    await waitFor(() => {
      expect(screen.getByText('STATUS')).toBeInTheDocument();
    });
    expect(screen.getByText('TASK')).toBeInTheDocument();
    expect(screen.getByText('TITLE')).toBeInTheDocument();
    expect(screen.getByText('AGENT')).toBeInTheDocument();
    expect(screen.getByText('THREAD')).toBeInTheDocument();
    expect(screen.getByText('UPDATED')).toBeInTheDocument();
    // Verify the DOM order: STATUS before TASK, TASK before TITLE.
    // THR-061 a-tasks: column header is a bordered surface-page card (matching
    // the group row-cards below), replacing the old sunken bar.
    const headerDivs = document.querySelectorAll('[class*="rounded-xl"][class*="bg-surface-page"] > div');
    const labels = Array.from(headerDivs).map((d) => d.textContent);
    expect(labels).toEqual(['STATUS', 'TASK', 'TITLE', 'AGENT', 'THREAD', 'UPDATED']);
  });

  // TASKS-02: agent rendered as AgentChip (avatar idiom), thread as a chip,
  // task ID as a monospace IdBadge, row click-through preserved to the detail route.
  test('renders task ID as a monospace IdBadge between status and title', async () => {
    mountTasks([
      rootTask({
        task_id: 'TASK-0410',
        assigned_agent: 'dev_agent',
        dispatched_from_thread_id: 'THR-0030',
        status: 'in_progress',
        severity_rollup: 'in_progress',
        brief: 'Reshape the tasks list rows',
      }),
    ]);
    await waitFor(() => {
      expect(screen.getByText('TASK-0410')).toBeInTheDocument();
    });
    // Task ID renders as a monospace IdBadge (tinted, not plain text).
    const taskId = screen.getByText('TASK-0410');
    expect(taskId).toHaveClass('font-mono');
    expect(taskId).toHaveClass('text-id-task');
    // The task ID is inside the row Link (the whole row is clickable) but the
    // IdBadge itself renders without a `to` prop, so it does NOT create a
    // nested anchor — the span's direct parent is a div.COL.taskId, not an <a>.
    expect(taskId.parentElement?.tagName).not.toBe('A');
  });

  // THR-041: long titles truncate cleanly with ellipsis so they cannot
  // overlap the Agent/Thread/Updated columns.
  // THR-049 msg-9 + task/TASK-1223: STATUS column renders compact primary
  // task status only — no block_kind derived qualifier ('waiting on subtasks'
  // / 'waiting on jobs') and no severity rollup. Both the waiting qualifier
  // and worst-child rollup render as second-line context in the TITLE column.
  // The reduced scope covers the founder-reported case where status=in_progress,
  // block_kind=delegated, severity_rollup=in_progress — the waiting qualifier
  // was still leaking into the STATUS column through StatusBadge.
  //
  // Case 1: status='in_progress' + block_kind='delegated' + severity_rollup='in_progress'
  //   → STATUS shows compact 'in_progress', TITLE shows 'waiting on subtasks'.
  test('STATUS compact for delegated in_progress task — waiting qualifier in TITLE context (THR-049)', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    const delegated = rootTask({
      task_id: 'TASK-0800',
      status: 'in_progress',
      block_kind: 'delegated',
      severity_rollup: 'in_progress',
      brief: 'Delegated root waiting on its children',
    });
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [delegated] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    await waitFor(() => {
      expect(screen.getByText('TASK-0800')).toBeInTheDocument();
    });
    const row = document.querySelector('a[href*="/tasks/TASK-0800"]') as HTMLElement;
    // STATUS column: compact 'in_progress' only, no 'waiting on subtasks'.
    const statusCell = row.querySelector('.whitespace-nowrap') as HTMLElement;
    const statusText = statusCell?.textContent ?? '';
    expect(statusText).toContain('in_progress');
    expect(statusText).not.toContain('waiting on subtasks');
    expect(statusText).not.toContain('waiting on jobs');
    // TITLE column: 'waiting on subtasks' appears as second-line context.
    expect(within(row).getByText('waiting on subtasks')).toBeInTheDocument();
    // No rollup line when rollup matches own status.
    expect(within(row).queryByText('subtask')).not.toBeInTheDocument();
  });

  test('STATUS compact when rollup worse than own status — rollup in TITLE context', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    const aggravated = rootTask({
      task_id: 'TASK-0801',
      status: 'in_progress',
      block_kind: null,
      severity_rollup: 'escalated',
      brief: 'Root in progress with an escalated child',
    });
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [aggravated] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    await waitFor(() => {
      expect(screen.getByText('TASK-0801')).toBeInTheDocument();
    });
    const row = document.querySelector('a[href*="/tasks/TASK-0801"]') as HTMLElement;
    // STATUS column: compact primary 'in_progress' only.
    const statusCell = row.querySelector('.whitespace-nowrap') as HTMLElement;
    const statusText = statusCell?.textContent ?? '';
    expect(statusText).toContain('in_progress');
    expect(statusText).not.toContain('escalated');
    // TITLE column: 'subtask escalated' appears as second-line context.
    const rollup = within(row).getByText('subtask escalated');
    expect(rollup).toHaveClass('text-status-escalated');
  });

  test('STATUS compact when delegated + worse rollup — both waiting and rollup in TITLE', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    const both = rootTask({
      task_id: 'TASK-0802',
      status: 'in_progress',
      block_kind: 'delegated',
      severity_rollup: 'escalated',
      brief: 'Root waiting AND has escalated child',
    });
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [both] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    await waitFor(() => {
      expect(screen.getByText('TASK-0802')).toBeInTheDocument();
    });
    const row = document.querySelector('a[href*="/tasks/TASK-0802"]') as HTMLElement;
    // STATUS column: compact 'in_progress' only.
    const statusCell = row.querySelector('.whitespace-nowrap') as HTMLElement;
    const statusText = statusCell?.textContent ?? '';
    expect(statusText).toContain('in_progress');
    expect(statusText).not.toContain('waiting');
    expect(statusText).not.toContain('escalated');
    // TITLE column: both 'waiting on subtasks' and 'subtask escalated' appear.
    expect(within(row).getByText('waiting on subtasks')).toBeInTheDocument();
    expect(within(row).getByText('subtask escalated')).toBeInTheDocument();
  });

  test('STATUS compact for in_progress + blocked_on_job — waiting on jobs in TITLE', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    const waitingJob = rootTask({
      task_id: 'TASK-0803',
      status: 'in_progress',
      block_kind: 'blocked_on_job',
      severity_rollup: 'in_progress',
      brief: 'Root waiting on a job',
    });
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [waitingJob] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/tasks`);
    await waitFor(() => {
      expect(screen.getByText('TASK-0803')).toBeInTheDocument();
    });
    const row = document.querySelector('a[href*="/tasks/TASK-0803"]') as HTMLElement;
    const statusCell = row.querySelector('.whitespace-nowrap') as HTMLElement;
    const statusText = statusCell?.textContent ?? '';
    expect(statusText).toContain('in_progress');
    expect(statusText).not.toContain('waiting');
    expect(within(row).getByText('waiting on jobs')).toBeInTheDocument();
  });

  test('truncates long titles with ellipsis and keeps status on one line', async () => {
    const longBrief = 'A'.repeat(500) + ' should be clipped';
    mountTasks([
      rootTask({
        task_id: 'TASK-0440',
        assigned_agent: 'dev_agent',
        dispatched_from_thread_id: 'THR-0030',
        status: 'in_progress',
        severity_rollup: 'in_progress',
        brief: longBrief,
      }),
    ]);
    await waitFor(() => {
      expect(screen.getByText('TASK-0440')).toBeInTheDocument();
    });
    // The title span should carry the truncate class.
    const titleSpan = screen.getByText((content, element) => {
      return element?.tagName === 'SPAN' && content.startsWith('AAAA');
    });
    expect(titleSpan).toHaveClass('truncate');
    // The status badge cell in the data row carries whitespace-nowrap so the
    // pill cannot wrap. The header also has whitespace-nowrap (via COL.status),
    // so we scope to the data row specifically.
    const statusCell = document.querySelector(
      'a[href*="/tasks/TASK-0440"] .whitespace-nowrap',
    );
    expect(statusCell).not.toBeNull();
    expect(statusCell!.textContent).toContain('in_progress');
  });

  // THR-041: status column now only shows the StatusBadge (no task ID).
  // Row click-through to the detail route is preserved.
  test('renders agent as an AgentChip avatar and thread as an inline chip', async () => {
    mountTasks([
      rootTask({
        task_id: 'TASK-0420',
        assigned_agent: 'dev_agent',
        dispatched_from_thread_id: 'THR-0030',
        status: 'in_progress',
        severity_rollup: 'in_progress',
        brief: 'Reshape the tasks list rows',
      }),
    ]);
    await waitFor(() => {
      expect(screen.getByText('dev_agent')).toBeInTheDocument();
    });
    // Agent is the AgentChip idiom (role-colored dot), not plain text.
    expect(document.querySelector('.bg-agent-worker')).not.toBeNull();
    // Thread reference renders as an inline (tinted) chip.
    expect(screen.getByText('THR-0030')).toBeInTheDocument();
    // Row click-through to the detail route is preserved.
    const rowLink = screen.getByRole('link', { name: /Reshape the tasks list rows/ });
    expect(rowLink).toHaveAttribute('href', `/orgs/${SLUG}/tasks/TASK-0420`);
  });

  // TASKS-02 honesty fence: missing agent/thread render a neutral fallback,
  // never a fabricated identity.
  test('renders neutral em-dash fallbacks when agent and thread are absent', async () => {
    mountTasks([
      rootTask({
        task_id: 'TASK-0430',
        assigned_agent: null,
        status: 'pending',
        severity_rollup: 'pending',
        brief: 'Unassigned, no thread',
      }),
    ]);
    await waitFor(() => {
      expect(screen.getByText('Unassigned, no thread')).toBeInTheDocument();
    });
    // No fabricated agent chip for this row.
    expect(document.querySelector('.bg-agent-worker')).toBeNull();
    // Both the agent and thread cells fall back to an em-dash.
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
  });
});

// THR-046 msg-11: wider layout, cream canvas, rounded column header,
// rounded bordered group-section cards, right-aligned group-by control,
// "Waiting on you" escalation label, "Active" in_progress label.
describe('TasksPage — THR-046 msg-11 layout reshape', () => {
  function mountTasks(tasks: TaskRecord[]) {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks }),
      ),
    );
    return mountAt(`/orgs/${SLUG}/tasks`);
  }

  test('column header is a rounded bordered surface-page card', async () => {
    mountTasks([
      rootTask({ task_id: 'TASK-0700', status: 'in_progress', severity_rollup: 'in_progress' }),
    ]);
    await waitFor(() => {
      expect(screen.getByText('STATUS')).toBeInTheDocument();
    });
    // THR-061 a-tasks: the column header is the first bordered surface-page card
    // in the <main> scroll area — it precedes the group row-cards in DOM order.
    const header = document.querySelector('[class*="rounded-xl"][class*="bg-surface-page"]');
    expect(header).not.toBeNull();
    expect(header).toHaveClass('rounded-xl');
    expect(header).toHaveClass('bg-surface-page');
    expect(header).toHaveClass('border');
  });

  test('each group section is a rounded bordered card', async () => {
    mountTasks([
      rootTask({ task_id: 'TASK-0710', status: 'escalated', severity_rollup: 'escalated' }),
      rootTask({ task_id: 'TASK-0711', status: 'completed', severity_rollup: 'completed' }),
    ]);
    await waitFor(() => {
      expect(screen.getByText('TASK-0710')).toBeInTheDocument();
    });
    // Group sections inside <main> are bordered rounded cards.
    const sections = document.querySelectorAll('main section');
    expect(sections.length).toBeGreaterThanOrEqual(2);
    sections.forEach((s) => {
      expect(s).toHaveClass('rounded-xl');
      expect(s).toHaveClass('border');
    });
  });

  test('group-by control is right-aligned in the header flex row', async () => {
    mountTasks([
      rootTask({ task_id: 'TASK-0720', status: 'in_progress', severity_rollup: 'in_progress' }),
    ]);
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'What the org is working on' })).toBeInTheDocument();
    });
    // The header contains a flex row with justify-between — the title (left)
    // and the group-by tabs (right) are siblings.
    const headerFlex = document.querySelector('header .flex.items-start.justify-between');
    expect(headerFlex).not.toBeNull();
    const tablist = headerFlex!.querySelector('[role="tablist"]');
    expect(tablist).not.toBeNull();
  });

  test('escalated group renders as "Waiting on you" with red attention dot', async () => {
    mountTasks([
      rootTask({
        task_id: 'TASK-0730',
        status: 'escalated',
        severity_rollup: 'escalated',
        brief: 'A root escalated for attention',
      }),
    ]);
    const heading = await screen.findByRole('heading', {
      name: /Waiting on you/,
    });
    // Red attention dot.
    const dot = heading.querySelector('span[aria-hidden="true"]');
    expect(dot).not.toBeNull();
    expect(dot).toHaveClass('text-status-escalated');
    // Not dimmed (dimming lives on the heading's wrapper — a-tasks label
    // above rows-card).
    expect(heading.parentElement).not.toHaveClass('opacity-60');
  });

  test('in_progress group renders as "Active" with green status dot', async () => {
    mountTasks([
      rootTask({
        task_id: 'TASK-0740',
        status: 'in_progress',
        severity_rollup: 'in_progress',
        brief: 'Active root task',
      }),
    ]);
    const heading = await screen.findByRole('heading', {
      name: /Active/,
    });
    const dot = heading.querySelector('span[aria-hidden="true"]');
    expect(dot).not.toBeNull();
    expect(dot).toHaveClass('text-status-open');
    // Count badge present.
    expect(within(heading).getByText('1')).toBeInTheDocument();
  });
});

describe('TaskDetailPage — jobs cross-link', () => {
  function stubHandlers(jobs: JobRecord[]) {
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${TASK.task_id}`, () =>
        HttpResponse.json(TASK),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${TASK.task_id}/recall`, () =>
        HttpResponse.json({
          task_id: TASK.task_id,
          assigned_agent: null,
          brief: TASK.brief,
          status: TASK.status,
          output_summary: null,
          children: [],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () =>
        HttpResponse.json({ jobs }),
      ),
    );
  }

  test('shows jobs section when task has jobs', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    stubHandlers([JOB]);
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    await waitFor(() =>
      expect(screen.getByText(/Jobs from this task/i)).toBeInTheDocument(),
    );
    const link = screen.getByRole('link', { name: 'JOB-0001' });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute('href', `/orgs/${SLUG}/jobs/JOB-0001`);
    expect(screen.getByText(/Generate sitemap/)).toBeInTheDocument();
    expect(screen.getByText(/completed/)).toBeInTheDocument();
  });

  test('hides jobs section when task has no jobs', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    stubHandlers([]);
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    await waitFor(() =>
      expect(screen.getByText(/Activity/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/Jobs from this task/i)).not.toBeInTheDocument();
  });
});

describe('TaskDetailPage — workflow chain timeline', () => {
  const ACTIVE_CHAIN: ActiveChainResponse = {
    step_index: 1,
    first_leg_expect_verdict: null,
    legs: [
      { agent: 'senior_dev', prompt: 'review the PR', expect_verdict: 'APPROVE' },
      { agent: 'qa_engineer', prompt: 'run QA suite', expect_verdict: 'PASS' },
    ],
    step_audit_id: 14,
  };

  const TASK_DETAIL_ENVELOPE = {
    task: TASK,
    results: [],
    audit_log: [],
    revisit_chain: [],
    direct_revisits: [],
    predecessor_prior_status: null,
    blocked_on_jobs: null,
  };

  function stubHandlers(
    active_chain: ActiveChainResponse | null,
    taskOverrides?: Partial<TaskRecord> & Record<string, unknown>,
    blocked_on_jobs?: unknown,
  ) {
    const detailTask = { ...TASK, ...taskOverrides } as TaskRecord;
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${detailTask.task_id}`, () =>
        HttpResponse.json({
          ...TASK_DETAIL_ENVELOPE,
          task: detailTask,
          active_chain,
          blocked_on_jobs: blocked_on_jobs ?? null,
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${detailTask.task_id}/recall`, () =>
        HttpResponse.json({
          task_id: detailTask.task_id,
          assigned_agent: null,
          brief: detailTask.brief,
          status: detailTask.status,
          output_summary: null,
          children: [],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () =>
        HttpResponse.json({ jobs: [] }),
      ),
    );
  }

  test('renders the chain timeline when active_chain is set', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    stubHandlers(ACTIVE_CHAIN);
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    expect(await screen.findByText(/Workflow chain/i)).toBeInTheDocument();
    expect(screen.getByText('senior_dev')).toBeInTheDocument();
    expect(screen.getByText('qa_engineer')).toBeInTheDocument();
    expect(screen.getByText(/APPROVE/)).toBeInTheDocument();
  });

  test('does not render the chain timeline when active_chain is null', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    stubHandlers(null);
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    await waitFor(() =>
      expect(screen.getByText(/Activity/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/Workflow chain/i)).not.toBeInTheDocument();
  });

  test('renders blocked chain node when task is escalated', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    // Path B: a genuine escalation is the top-level `escalated` status.
    stubHandlers(
      { ...ACTIVE_CHAIN, step_index: 0 },
      { status: 'escalated', block_kind: null },
    );
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    expect(await screen.findByText(/Workflow chain/i)).toBeInTheDocument();
    // The blocked node should show "Blocked on: escalation"
    expect(screen.getByText(/Blocked on:/)).toBeInTheDocument();
    expect(screen.getByText(/escalation/)).toBeInTheDocument();
  });

  test('renders blocked chain node with job IDs from blocked_on_jobs', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    // Path B: a task waiting on a job is in_progress + blocked_on_job.
    stubHandlers(
      { ...ACTIVE_CHAIN, step_index: 1 },
      { status: 'in_progress', block_kind: 'blocked_on_job' },
      [{ job_id: 'JOB-0042', status: 'pending' }],
    );
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    expect(await screen.findByText(/Workflow chain/i)).toBeInTheDocument();
    expect(screen.getByText(/Blocked on:/)).toBeInTheDocument();
    expect(screen.getByText(/JOB-0042/)).toBeInTheDocument();
  });

  test('falls through to generic job copy for pending_review (gate removed)', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    // pending_review is no longer accepted by parseActiveFanout (THR-012 msg 129/131).
    // The fan-out approval copy should NOT render; generic job IDs fall through.
    stubHandlers(
      { ...ACTIVE_CHAIN, step_index: 0 },
      {
        status: 'in_progress',
        block_kind: 'blocked_on_job',
        active_fanout: JSON.stringify({ status: 'pending_review', width: 5 }),
      },
      [{ job_id: 'JOB-0099', status: 'pending' }],
    );
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    expect(await screen.findByText(/Workflow chain/i)).toBeInTheDocument();
    expect(screen.getByText(/Blocked on:/)).toBeInTheDocument();
    // Must NOT show fan-out approval copy — pending_review is rejected
    expect(
      screen.queryByText(/awaiting approval to spawn/),
    ).not.toBeInTheDocument();
    // Generic job ID copy shows instead
    expect(screen.getByText(/JOB-0099/)).toBeInTheDocument();
  });

  test('renders generic job wait for ordinary blocked_on_job without fan-out', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    // Ordinary blocked_on_job: no active_fanout at all. Must still render
    // generic job-waiting copy, not fan-out approval language.
    stubHandlers(
      { ...ACTIVE_CHAIN, step_index: 0 },
      {
        status: 'in_progress',
        block_kind: 'blocked_on_job',
        active_fanout: undefined,
      },
      [{ job_id: 'JOB-0077', status: 'pending' }],
    );
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    expect(await screen.findByText(/Workflow chain/i)).toBeInTheDocument();
    expect(screen.getByText(/Blocked on:/)).toBeInTheDocument();
    // Must show generic job ID
    expect(screen.getByText(/JOB-0077/)).toBeInTheDocument();
    // Must NOT show fan-out approval copy
    expect(
      screen.queryByText(/awaiting approval/),
    ).not.toBeInTheDocument();
  });

  test('renders waiting-on-subtasks copy for active spawned fan-out', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    // Active spawned fan-out: delegated block_kind with active_fanout.status='spawned'.
    // Should render width-aware delegation copy, not fan-out approval nor job IDs.
    stubHandlers(
      { ...ACTIVE_CHAIN, step_index: 0 },
      {
        status: 'in_progress',
        block_kind: 'delegated',
        active_fanout: JSON.stringify({ status: 'spawned', width: 3 }),
      },
      null,
    );
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    expect(await screen.findByText(/Workflow chain/i)).toBeInTheDocument();
    expect(screen.getByText(/Blocked on:/)).toBeInTheDocument();
    // Must show width-aware delegation copy
    expect(
      screen.getByText(/waiting on 3 subtasks/),
    ).toBeInTheDocument();
    // Must NOT show fan-out approval copy
    expect(
      screen.queryByText(/awaiting approval/),
    ).not.toBeInTheDocument();
    // Must NOT show generic delegation copy
    expect(screen.queryByText(/^delegation$/)).not.toBeInTheDocument();
  });
});

describe('TaskDetailPage — fan-out status band (TASK-1717)', () => {
  interface BandStubOpts {
    taskOverrides?: Partial<TaskRecord> & Record<string, unknown>;
    audit_log?: unknown[];
    recallChildren?: unknown[];
    jobs?: JobRecord[];
  }

  function reviewJob(overrides?: Partial<JobRecord>): JobRecord {
    return {
      ...JOB,
      id: 'JOB-APPROVAL',
      title: 'Approve fan-out (spawn 2 subtasks)',
      status: 'pending',
      review_required: true,
      exit_code: null,
      ...overrides,
    };
  }

  function stubBand(opts: BandStubOpts) {
    const detailTask = { ...TASK, ...opts.taskOverrides } as TaskRecord;
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${detailTask.task_id}`, () =>
        HttpResponse.json({
          task: detailTask,
          results: [],
          audit_log: opts.audit_log ?? [],
          revisit_chain: [],
          direct_revisits: [],
          predecessor_prior_status: null,
          active_chain: null,
          blocked_on_jobs: null,
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${detailTask.task_id}/recall`, () =>
        HttpResponse.json({
          task_id: detailTask.task_id,
          assigned_agent: null,
          brief: detailTask.brief,
          status: detailTask.status,
          output_summary: null,
          children: opts.recallChildren ?? [],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () =>
        HttpResponse.json({ jobs: opts.jobs ?? [] }),
      ),
    );
  }

  function mountDetail() {
    sessionStorage.setItem('happyranch.token', 'tok');
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
  }

  test('pending_review: fan-out band does NOT render (gate removed per THR-012)', async () => {
    stubBand({
      taskOverrides: {
        status: 'in_progress',
        block_kind: 'blocked_on_job',
        active_fanout: JSON.stringify({
          status: 'pending_review',
          width: 2,
          children_details: [
            { agent: 'content_writer', prompt: 'Draft the intro section' },
            { agent: 'seo_specialist', prompt: 'Audit the target keywords' },
          ],
        }),
      },
      jobs: [reviewJob()],
      recallChildren: [],
    });
    mountDetail();

    // pending_review is no longer accepted by parseActiveFanout —
    // the fan-out band should NOT render.
    // The status band region should not exist at all.
    await screen.findByText(/Execution subtasks/i);
    expect(
      screen.queryByRole('region', { name: 'Fan-out status' }),
    ).not.toBeInTheDocument();
  });

  test('running: progress counts come from recall children and child task links are preserved', async () => {
    stubBand({
      taskOverrides: {
        status: 'in_progress',
        block_kind: 'delegated',
        active_fanout: JSON.stringify({
          status: 'spawned',
          width: 3,
          children_ids: ['TASK-C1', 'TASK-C2', 'TASK-C3'],
        }),
      },
      recallChildren: [
        {
          task_id: 'TASK-C1',
          assigned_agent: 'content_writer',
          brief: 'Child one brief',
          status: 'completed',
          output_summary: 'done one',
          children: [],
        },
        {
          task_id: 'TASK-C2',
          assigned_agent: 'content_writer',
          brief: 'Child two brief',
          status: 'in_progress',
          output_summary: null,
          children: [],
        },
        {
          task_id: 'TASK-C3',
          assigned_agent: 'content_writer',
          brief: 'Child three brief',
          status: 'pending',
          output_summary: null,
          children: [],
        },
      ],
    });
    mountDetail();

    const band = await screen.findByRole('region', { name: 'Fan-out status' });
    // Terminal count (1 completed) of width 3, derived from recall statuses.
    expect(within(band).getByText(/Running fan-out — 1 of 3 done/)).toBeInTheDocument();
    expect(
      within(band).getByText(/1 of 3 complete · 1 running · 1 queued/),
    ).toBeInTheDocument();
    // Backed metadata: width + constant join mode.
    expect(within(band).getByText('all-terminal')).toBeInTheDocument();

    // Child task links preserved in the execution-subtasks list.
    const subtasks = screen
      .getByText('Execution subtasks')
      .closest('section') as HTMLElement;
    const c1 = within(subtasks).getByRole('link', { name: 'TASK-C1' });
    expect(c1).toHaveAttribute('href', `/orgs/${SLUG}/tasks/TASK-C1`);
    expect(within(subtasks).getByRole('link', { name: 'TASK-C3' })).toBeInTheDocument();
  });

  test('joined: renders from the fanout_join audit row even when active_fanout is cleared', async () => {
    stubBand({
      taskOverrides: {
        status: 'completed',
        block_kind: null,
        active_fanout: undefined, // cleared after join
      },
      audit_log: [
        { action: 'fanout_spawned', payload: { width: 2 } },
        {
          action: 'fanout_join',
          payload: { width: 2, children_ids: ['TASK-J1', 'TASK-J2'] },
        },
      ],
      recallChildren: [
        {
          task_id: 'TASK-J1',
          assigned_agent: 'content_writer',
          brief: 'Joined child one',
          status: 'completed',
          output_summary: 'ok',
          children: [],
        },
        {
          task_id: 'TASK-J2',
          assigned_agent: 'content_writer',
          brief: 'Joined child two',
          status: 'failed',
          output_summary: 'boom',
          children: [],
        },
      ],
    });
    mountDetail();

    const band = await screen.findByRole('region', { name: 'Fan-out status' });
    expect(within(band).getByText(/Fan-out joined — 1 of 2 succeeded/)).toBeInTheDocument();
    expect(within(band).getByText(/1 subtask did not succeed/)).toBeInTheDocument();
    expect(within(band).getByText('all-terminal')).toBeInTheDocument();
    // Inspectable child rows preserved in the execution-subtasks list.
    const subtasks = screen
      .getByText('Execution subtasks')
      .closest('section') as HTMLElement;
    expect(within(subtasks).getByRole('link', { name: 'TASK-J1' })).toBeInTheDocument();
  });

  test('regular non-fan-out task renders NO fan-out band', async () => {
    stubBand({
      taskOverrides: { status: 'completed', block_kind: null },
      audit_log: [{ action: 'task_started', payload: {} }],
      recallChildren: [],
    });
    mountDetail();

    await waitFor(() =>
      expect(screen.getByText(/Activity/i)).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole('region', { name: 'Fan-out status' }),
    ).not.toBeInTheDocument();
  });

  test('ordinary blocked_on_job (no fan-out) renders NO fan-out band', async () => {
    stubBand({
      taskOverrides: {
        status: 'in_progress',
        block_kind: 'blocked_on_job',
        active_fanout: undefined,
      },
      audit_log: [],
      jobs: [{ ...JOB, id: 'JOB-XYZ', status: 'pending' }],
      recallChildren: [],
    });
    mountDetail();

    await waitFor(() =>
      expect(screen.getByText(/Activity/i)).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole('region', { name: 'Fan-out status' }),
    ).not.toBeInTheDocument();
    // No fan-out copy anywhere on the page.
    expect(screen.queryByText(/Awaiting approval to spawn/)).not.toBeInTheDocument();
  });
});

describe('TaskDetailPage — execution subtasks', () => {
  function stubHandlers() {
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${TASK.task_id}`, () =>
        HttpResponse.json(TASK),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${TASK.task_id}/recall`, () =>
        HttpResponse.json({
          task_id: TASK.task_id,
          assigned_agent: 'content_writer',
          brief: TASK.brief,
          status: TASK.status,
          output_summary: null,
          children: [
            {
              task_id: 'TASK-0092',
              assigned_agent: 'content_writer',
              brief: 'Section 4: currency policy',
              status: 'completed',
              output_summary: 'Wrote section 4.',
              children: [],
            },
          ],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () =>
        HttpResponse.json({ jobs: [] }),
      ),
    );
  }

  test('shows execution subtasks from recall tree', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    stubHandlers();
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    await waitFor(() => {
      expect(screen.getByText(/Execution subtasks/i)).toBeInTheDocument();
    });
    expect(screen.getAllByText('TASK-0092').length).toBeGreaterThan(0);
    expect(screen.getAllByText('content_writer').length).toBeGreaterThan(0);
  });
});

describe('TaskDetailPage — full-page surface', () => {
  function stubHandlers() {
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK] }),
      ),
      // Detail endpoint returns the envelope; useTask selects response.task.
      http.get(`/api/v1/orgs/${SLUG}/tasks/${TASK.task_id}`, () =>
        HttpResponse.json({
          task: TASK,
          results: [],
          audit_log: [],
          revisit_chain: [],
          direct_revisits: [],
          predecessor_prior_status: null,
          active_chain: null,
          blocked_on_jobs: null,
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${TASK.task_id}/recall`, () =>
        HttpResponse.json({
          task_id: TASK.task_id,
          assigned_agent: null,
          brief: TASK.brief,
          status: TASK.status,
          output_summary: null,
          children: [],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () => HttpResponse.json({ jobs: [] })),
    );
  }

  test('renders the task body with a "‹ All tasks" back link to the roots list', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    stubHandlers();
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });

    // Wait for the data-driven Brief section (gated on task.data.brief) — the
    // task id heading renders synchronously from the route param, so awaiting
    // it would not wait for the detail fetch.
    expect(
      await screen.findByRole('heading', { name: 'Brief' }),
    ).toBeInTheDocument();

    // Full-page body renders: task id heading + brief content, no drawer overlay.
    expect(
      screen.getByRole('heading', { name: new RegExp(TASK.task_id) }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByText(/Draft Hong Kong visa guide/).length,
    ).toBeGreaterThan(0);

    // Back-nav returns to the roots list.
    const backLink = screen.getByRole('link', { name: /‹ All tasks/ });
    expect(backLink).toHaveAttribute('href', `/orgs/${SLUG}/tasks`);
  });
});

describe('TaskDetailPage — property grid (TASKDET-03)', () => {
  // Detail task carrying every property-grid field that has REAL backing in the
  // TaskRecord payload: status, assigned_agent, dispatched_from_thread_id,
  // created_at. Executor / Churn / Priority have no backing field and are
  // honestly omitted (see TaskDetailPage PropertyRail doc-comment).
  const DETAIL_TASK = {
    ...TASK,
    assigned_agent: 'content_writer',
    dispatched_from_thread_id: 'THR-0030',
    created_at: '2026-05-18T10:00:00Z',
  } as TaskRecord;

  function stubHandlers(jobs: JobRecord[], task: TaskRecord = DETAIL_TASK) {
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [task] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${task.task_id}`, () =>
        HttpResponse.json({
          task,
          results: [],
          audit_log: [],
          revisit_chain: [],
          direct_revisits: [],
          predecessor_prior_status: null,
          active_chain: null,
          blocked_on_jobs: null,
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${task.task_id}/recall`, () =>
        HttpResponse.json({
          task_id: task.task_id,
          assigned_agent: task.assigned_agent,
          brief: task.brief,
          status: task.status,
          output_summary: null,
          children: [],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () => HttpResponse.json({ jobs })),
    );
  }

  async function mountAndGetRail() {
    sessionStorage.setItem('happyranch.token', 'tok');
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${DETAIL_TASK.task_id}`,
    });
    return (await screen.findByRole('complementary', {
      name: /task status and properties/i,
    })) as HTMLElement;
  }

  test('renders a labeled property grid of the backed fields', async () => {
    const rail = await (async () => {
      stubHandlers([JOB]);
      return mountAndGetRail();
    })();

    // Backed fields — each label/value renders inside the rail.
    expect(within(rail).getByText('Status')).toBeInTheDocument();
    expect(within(rail).getByText('Assignee')).toBeInTheDocument();
    expect(within(rail).getByText('content_writer')).toBeInTheDocument();
    expect(within(rail).getByText('Thread')).toBeInTheDocument();
    const threadLink = within(rail).getByRole('link', { name: 'THR-0030' });
    expect(threadLink).toHaveAttribute(
      'href',
      `/orgs/${SLUG}/threads/THR-0030`,
    );
    expect(within(rail).getByText('Job')).toBeInTheDocument();
    const jobLink = within(rail).getByRole('link', { name: 'JOB-0001' });
    expect(jobLink).toHaveAttribute('href', `/orgs/${SLUG}/jobs/JOB-0001`);
    expect(within(rail).getByText('Created')).toBeInTheDocument();
  });

  test('wraps multiple job links within the property rail in returned order (THR-137)', async () => {
    const jobs = [
      { ...JOB, id: 'JOB-0338' },
      { ...JOB, id: 'JOB-0337' },
      { ...JOB, id: 'JOB-0336' },
      { ...JOB, id: 'JOB-0335' },
    ];
    stubHandlers(jobs);
    const rail = await mountAndGetRail();

    const jobRow = within(rail).getByText('Job').closest('div') as HTMLElement;
    const links = within(jobRow).getAllByRole('link');
    expect(links.map((link) => link.textContent)).toEqual(jobs.map((job) => job.id));
    expect(links.map((link) => link.getAttribute('href'))).toEqual(
      jobs.map((job) => `/orgs/${SLUG}/jobs/${job.id}`),
    );

    // jsdom cannot assert geometry; lock the Job-only shrink-and-wrap contract instead.
    const value = jobRow.querySelector('dd') as HTMLElement;
    expect(value).toHaveClass('min-w-0', 'flex-1');
    expect(value).not.toHaveClass('min-w-max');
    expect(value.firstElementChild).toHaveClass('flex', 'flex-wrap');
  });

  test('honestly omits fields with no backing payload (Executor / Churn / Priority)', async () => {
    stubHandlers([JOB]);
    const rail = await mountAndGetRail();
    expect(within(rail).queryByText('Executor')).toBeNull();
    expect(within(rail).queryByText('Churn')).toBeNull();
    expect(within(rail).queryByText('Priority')).toBeNull();
  });

  test('keeps a long assignee identifier fully available with wrap-safe styling', async () => {
    const longAssignee = `frontend_${'engineer'.repeat(30)}`;
    stubHandlers([], { ...DETAIL_TASK, assigned_agent: longAssignee });
    const rail = await mountAndGetRail();
    const identity = within(rail).getByText(longAssignee);
    expect(identity).toHaveClass('min-w-0', 'break-all');
    expect(identity).not.toHaveClass('truncate');
  });

  // THR-137: a long in_progress + delegated status badge must stay readable in
  // the narrow property rail. jsdom cannot prove geometry, so the test asserts
  // the responsive layout contract (wrap-capable row, min-content value cell,
  // no-wrap badge wrapper) plus the visible qualifier text.
  test('keeps a long in_progress + delegated status badge readable in the narrow rail (THR-137)', async () => {
    const WAITING_TASK = {
      ...DETAIL_TASK,
      status: 'in_progress',
      block_kind: 'delegated',
    } as TaskRecord;
    stubHandlers([], WAITING_TASK);
    const rail = await mountAndGetRail();

    // The waiting qualifier is rendered and readable inside the rail.
    const qualifier = within(rail).getByText('· waiting on subtasks');
    expect(qualifier).toBeInTheDocument();

    // Responsive layout contract: the row can wrap so the value is not squeezed.
    const statusRow = within(rail).getByText('Status').closest('div') as HTMLElement;
    expect(statusRow).toHaveClass('flex-wrap');
    const value = qualifier.closest('dd') as HTMLElement;
    expect(value).toHaveClass('min-w-0');
    // The badge remains complete and can wrap as a unit at narrow widths.
    expect(value.querySelector('span.inline-flex')).not.toBeNull();
  });

  test('omits the Thread and Job rows when those fields are absent', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${TASK.task_id}`, () =>
        HttpResponse.json({
          task: TASK,
          results: [],
          audit_log: [],
          revisit_chain: [],
          direct_revisits: [],
          predecessor_prior_status: null,
          active_chain: null,
          blocked_on_jobs: null,
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${TASK.task_id}/recall`, () =>
        HttpResponse.json({
          task_id: TASK.task_id,
          assigned_agent: null,
          brief: TASK.brief,
          status: TASK.status,
          output_summary: null,
          children: [],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () => HttpResponse.json({ jobs: [] })),
    );
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    const rail = (await screen.findByRole('complementary', {
      name: /task status and properties/i,
    })) as HTMLElement;
    // No thread / no jobs → those rows are absent (not fabricated).
    expect(within(rail).queryByText('Thread')).toBeNull();
    expect(within(rail).queryByText('Job')).toBeNull();
    // Always-present backed fields still render.
    expect(within(rail).getByText('Status')).toBeInTheDocument();
    expect(within(rail).getByText('Created')).toBeInTheDocument();
  });
});

describe('TaskDetailPage — escalation reason', () => {
  const ESCALATION_NOTE = 'Agent exhausted failure-round bound after 5 attempts';

  function stubDetailHandlers(
    overrides: Partial<TaskRecord> & Record<string, unknown>,
  ) {
    const detailTask = { ...TASK, ...overrides } as TaskRecord;
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${detailTask.task_id}`, () =>
        HttpResponse.json({
          task: detailTask,
          results: [],
          audit_log: [],
          revisit_chain: [],
          direct_revisits: [],
          predecessor_prior_status: null,
          active_chain: null,
          blocked_on_jobs: null,
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${detailTask.task_id}/recall`, () =>
        HttpResponse.json({
          task_id: detailTask.task_id,
          assigned_agent: null,
          brief: detailTask.brief,
          status: detailTask.status,
          output_summary: null,
          children: [],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () =>
        HttpResponse.json({ jobs: [] }),
      ),
    );
  }

  test('displays escalation reason for a Path B escalated task with a note', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    stubDetailHandlers({
      status: 'escalated',
      block_kind: null,
      note: ESCALATION_NOTE,
    });
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    // Wait for the data-driven Brief section to confirm detail fetch completed.
    expect(
      await screen.findByRole('heading', { name: 'Brief' }),
    ).toBeInTheDocument();
    // Escalation reason banner is visible.
    expect(screen.getByText(/Escalation reason:/)).toBeInTheDocument();
    expect(screen.getByText(ESCALATION_NOTE)).toBeInTheDocument();
    // The escalated action set (Continue) is present because the task is escalated.
    expect(screen.getByRole('button', { name: /^Continue$/ })).toBeInTheDocument();
  });

  test('displays escalation reason for a legacy blocked+escalated task with a note', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    stubDetailHandlers({
      status: 'blocked',
      block_kind: 'escalated',
      note: 'Legacy escalation: budget override required',
    });
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    expect(
      await screen.findByRole('heading', { name: 'Brief' }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Escalation reason:/)).toBeInTheDocument();
    expect(
      screen.getByText('Legacy escalation: budget override required'),
    ).toBeInTheDocument();
    // The escalated action set (Continue) is present for the legacy form too.
    expect(screen.getByRole('button', { name: /^Continue$/ })).toBeInTheDocument();
  });

  test('does not display escalation reason for a non-escalated task with a note', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    // A completed task with a note — note belongs to a prior failure, not escalation.
    stubDetailHandlers({
      status: 'completed',
      block_kind: null,
      note: 'Some note from a prior escalation',
    });
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    expect(
      await screen.findByRole('heading', { name: 'Brief' }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Escalation reason:/)).not.toBeInTheDocument();
    // No escalation-only Continue action for non-escalated tasks.
    expect(
      screen.queryByRole('button', { name: /^Continue$/ }),
    ).not.toBeInTheDocument();
  });

  test('does not display escalation reason for an escalated task with empty note', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    stubDetailHandlers({
      status: 'escalated',
      block_kind: null,
      note: '',
    });
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${TASK.task_id}`,
    });
    expect(
      await screen.findByRole('heading', { name: 'Brief' }),
    ).toBeInTheDocument();
    // Empty note → no escalation reason banner.
    expect(screen.queryByText(/Escalation reason:/)).not.toBeInTheDocument();
    // Continue action is still present (task is escalated, just no note).
    expect(screen.getByRole('button', { name: /^Continue$/ })).toBeInTheDocument();
  });
});

describe('TaskDetailPage — superseded by link', () => {
  const SUPERSEDED_TASK = {
    ...TASK,
    status: 'superseded',
    block_kind: null,
    note: 'Resolved: superseded by continuation TASK-SUCC',
  } as TaskRecord;

  function stubDetail(overrides: Record<string, unknown>) {
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${SUPERSEDED_TASK.task_id}`, () =>
        HttpResponse.json({
          task: SUPERSEDED_TASK,
          results: [],
          audit_log: [],
          revisit_chain: [],
          direct_revisits: [],
          predecessor_prior_status: null,
          active_chain: null,
          blocked_on_jobs: null,
          ...overrides,
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${SUPERSEDED_TASK.task_id}/recall`, () =>
        HttpResponse.json({
          task_id: SUPERSEDED_TASK.task_id,
          assigned_agent: null,
          brief: SUPERSEDED_TASK.brief,
          status: SUPERSEDED_TASK.status,
          output_summary: null,
          children: [],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () =>
        HttpResponse.json({ jobs: [] }),
      ),
    );
  }

  test('shows superseded-by link when task has superseded_by_task_id', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    stubDetail({ superseded_by_task_id: 'TASK-SUCC' });
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${SUPERSEDED_TASK.task_id}`,
    });
    // Wait for data-driven content (Brief section).
    expect(
      await screen.findByRole('heading', { name: 'Brief' }),
    ).toBeInTheDocument();
    // The superseded-by link appears in the lineage metadata.
    const link = screen.getByRole('link', { name: 'TASK-SUCC' });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute(
      'href',
      `/orgs/${SLUG}/tasks/TASK-SUCC`,
    );
    // The label text is present.
    expect(screen.getByText(/superseded by/)).toBeInTheDocument();
  });

  test('does not show superseded-by link when superseded_by_task_id is null', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    stubDetail({ superseded_by_task_id: null });
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${SUPERSEDED_TASK.task_id}`,
    });
    expect(
      await screen.findByRole('heading', { name: 'Brief' }),
    ).toBeInTheDocument();
    // No superseded-by link or label.
    expect(screen.queryByText(/superseded by/)).not.toBeInTheDocument();
    // The task id heading still renders.
    expect(
      screen.getByRole('heading', { name: new RegExp(SUPERSEDED_TASK.task_id) }),
    ).toBeInTheDocument();
  });

  test('does not show superseded-by link for a non-superseded task (omitted key)', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    // Override the task to in_progress — no supersession.
    const normalTask = { ...TASK, status: 'in_progress' } as TaskRecord;
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [TASK] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${normalTask.task_id}`, () =>
        HttpResponse.json({
          task: normalTask,
          results: [],
          audit_log: [],
          revisit_chain: [],
          direct_revisits: [],
          predecessor_prior_status: null,
          active_chain: null,
          blocked_on_jobs: null,
          superseded_by_task_id: null,
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/${normalTask.task_id}/recall`, () =>
        HttpResponse.json({
          task_id: normalTask.task_id,
          assigned_agent: null,
          brief: normalTask.brief,
          status: normalTask.status,
          output_summary: null,
          children: [],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () =>
        HttpResponse.json({ jobs: [] }),
      ),
    );
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/tasks/${normalTask.task_id}`,
    });
    expect(
      await screen.findByRole('heading', { name: 'Brief' }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/superseded by/)).not.toBeInTheDocument();
  });
});

describe('TaskDetailPage — Activity route isolation (TASK-4827)', () => {
  const FIRST_TASK = rootTask({
    task_id: 'TASK-4809',
    brief: 'First task activity fixture',
    status: 'in_progress',
    severity_rollup: 'in_progress',
  });
  const SECOND_TASK = rootTask({
    task_id: 'TASK-4819',
    brief: 'Second task activity fixture',
    status: 'in_progress',
    severity_rollup: 'in_progress',
  });

  function event(action: string, payload: Record<string, string>): TaskEvent {
    return {
      timestamp: '2026-08-08T00:00:00Z',
      type: 'audit',
      action,
      payload,
    };
  }

  test('drops prior-task events on route navigation and ignores a late old callback', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    const tasksById = new Map([
      [FIRST_TASK.task_id, FIRST_TASK],
      [SECOND_TASK.task_id, SECOND_TASK],
    ]);
    const tails = new Map<string, SSEOptions<unknown>>();
    vi.spyOn(api, 'subscribeSSE').mockImplementation((path, options) => {
      const taskId = path.split('/').at(-2);
      if (taskId) tails.set(taskId, options);
      return new Promise<void>(() => {});
    });
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/roots`, () =>
        HttpResponse.json({ tasks: [FIRST_TASK, SECOND_TASK] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks/:taskId`, ({ params }) => {
        const task = tasksById.get(params.taskId as string);
        return HttpResponse.json({
          task,
          results: [],
          audit_log: [],
          revisit_chain: [],
          direct_revisits: [],
          predecessor_prior_status: null,
          active_chain: null,
          blocked_on_jobs: null,
        });
      }),
      http.get(`/api/v1/orgs/${SLUG}/tasks/:taskId/recall`, ({ params }) => {
        const task = tasksById.get(params.taskId as string) as TaskRecord;
        return HttpResponse.json({
          task_id: task.task_id,
          assigned_agent: null,
          brief: task.brief,
          status: task.status,
          output_summary: null,
          children: [],
        });
      }),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () => HttpResponse.json({ jobs: [] })),
    );
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={[`/orgs/${SLUG}/tasks/${FIRST_TASK.task_id}`]}>
        <AppProvider client={makeQueryClient()}>
          <Link to={`/orgs/${SLUG}/tasks/${SECOND_TASK.task_id}`}>Open second task</Link>
          <AppRoutes />
        </AppProvider>
      </MemoryRouter>,
    );

    await screen.findByRole('heading', { name: 'Activity' });
    await waitFor(() => expect(tails.get(FIRST_TASK.task_id)).toBeDefined());
    const firstTail = tails.get(FIRST_TASK.task_id);
    act(() => firstTail?.onMessage(event('task_4809_activity', { source: 'TASK-4809' })));
    expect(screen.getByText('task_4809_activity')).toBeInTheDocument();

    await user.click(screen.getByRole('link', { name: 'Open second task' }));
    await screen.findByRole('heading', { name: new RegExp(SECOND_TASK.task_id) });
    await waitFor(() => expect(tails.get(SECOND_TASK.task_id)).toBeDefined());
    expect(screen.queryByText('task_4809_activity')).not.toBeInTheDocument();
    expect(screen.getByText(`Loading events for ${SECOND_TASK.task_id}…`)).toBeInTheDocument();

    act(() => firstTail?.onMessage(event('task_4809_late_activity', { source: 'late TASK-4809' })));
    expect(screen.queryByText('task_4809_late_activity')).not.toBeInTheDocument();

    const secondTail = tails.get(SECOND_TASK.task_id);
    act(() => {
      secondTail?.onOpen?.();
      secondTail?.onMessage(event('task_4819_activity', { source: 'TASK-4819' }));
    });
    expect(screen.getByText('task_4819_activity')).toBeInTheDocument();
    expect(screen.queryByText('task_4809_activity')).not.toBeInTheDocument();
  });
});
