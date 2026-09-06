import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { createMemoryRouter, Link, MemoryRouter, RouterProvider, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, test } from 'vitest';
import { AppProvider } from '@/design-system/providers/AppProvider';
import { AppRoutes } from '@/routes';
import { renderWithProviders } from '@/test/render';
import { server } from '@/test/server';
import type { JobRecord } from '@/lib/api/types';

const SLUG = 'hk-macau-tourism';
const NativeRequest = globalThis.Request;

afterEach(() => {
  globalThis.Request = NativeRequest;
});

const AGENTS_PAYLOAD = {
  agents: [
    {
      name: 'engineering_head',
      team: 'engineering',
      role: 'manager',
      executor: 'claude',
      model: 'claude-sonnet-4-20250514',
      description: 'Owns engineering.',
      repos: {},
      system_prompt: 'You are the engineering head.',
    },
    {
      name: 'support_agent',
      team: 'cx',
      role: 'worker',
      executor: 'codex',
      model: null,
      description: 'Handles support.',
      repos: { happyranch: 'https://github.com/t-benze/happyranch' },
      system_prompt: 'You are support.',
    },
  ],
};

function stubBaseHandlers() {
  server.use(
    http.get('/api/v1/orgs', () =>
      HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
    ),
    http.get(`/api/v1/orgs/${SLUG}/agents`, () => HttpResponse.json(AGENTS_PAYLOAD)),
    http.get(`/api/v1/orgs/${SLUG}/settings`, () =>
      HttpResponse.json({}),
    ),
    http.get(`/api/v1/orgs/${SLUG}/teams`, () =>
      HttpResponse.json({ teams: [] }),
    ),
    // Executor prereqs — required by AgentDetailPane's useExecutorOptions
    http.get('/api/v1/health/prereqs', () =>
      HttpResponse.json({
        prereqs: [
          { tool: 'claude', present: true, path: '/usr/local/bin/claude', hint: '' },
          { tool: 'codex', present: true, path: '/usr/local/bin/codex', hint: '' },
          { tool: 'opencode', present: false, path: null, hint: '' },
          { tool: 'pi', present: false, path: null, hint: '' },
        ],
      }),
    ),
    http.get('/api/v1/executors/runtime/profiles', () =>
      HttpResponse.json({ profiles: [] }),
    ),
  );
}

function stubDetailHandlers(agentTasks: unknown[] = []) {
  server.use(
    http.get(`/api/v1/orgs/${SLUG}/tasks`, () =>
      HttpResponse.json({ tasks: agentTasks }),
    ),
    http.get(
      `/api/v1/orgs/${SLUG}/agents/:agentName/memory/entries/`,
      () => HttpResponse.json({ entries: [] }),
    ),
    http.get(`/api/v1/orgs/${SLUG}/jobs/`, () =>
      HttpResponse.json({ jobs: [] }),
    ),
  );
}

function mountAt(route: string) {
  sessionStorage.setItem('happyranch.token', 'tok');
  return renderWithProviders(<AppRoutes />, { route });
}

function PolicyNavigationControls(): JSX.Element {
  const navigate = useNavigate();
  return <div className="sr-only">
    <Link to={`/orgs/${SLUG}/dashboard`}>Test destination</Link>
    <button onClick={() => navigate(-1)}>Test browser back</button>
    <button onClick={() => navigate(1)}>Test browser forward</button>
  </div>;
}

function mountPolicyRoute(entries: string[], initialIndex = entries.length - 1) {
  // React Router's data-memory history creates a Request with jsdom's
  // AbortSignal, which Node's undici Request rejects. Navigation loaders are
  // not used in this app, so omit that test-environment-only signal.
  globalThis.Request = class RouterTestRequest extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(input, init ? { ...init, signal: undefined } : init);
    }
  };
  sessionStorage.setItem('happyranch.token', 'tok');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([{
    path: '*',
    element: <AppProvider client={client}><PolicyNavigationControls /><AppRoutes /></AppProvider>,
  }], { initialEntries: entries, initialIndex });
  const view = render(<RouterProvider router={router} />);
  return { ...view, client };
}

function policyCacheEntries(client: QueryClient) {
  return client.getQueryCache().getAll().filter((query) => String(query.queryKey[0]).startsWith('team-escalation-policy'));
}

describe('AgentsPage — two-pane roster list', () => {
  test('renders the agent roster with role meta + description in left pane', async () => {
    stubBaseHandlers();
    // The first agent auto-selects on mount (AGENTS-01), so the detail pane
    // also queries — stub its endpoints to satisfy onUnhandledRequest:'error'.
    stubDetailHandlers();
    mountAt(`/orgs/${SLUG}/agents`);

    // support_agent is the second (un-selected) agent, so it appears once —
    // in the roster — making it a stable anchor for the initial wait.
    await waitFor(() =>
      expect(screen.getByText('support_agent')).toBeInTheDocument(),
    );
    // Names/role-meta/descriptions appear in the roster list inside the
    // left-pane <aside> (there are 2 <aside> elements: sidebar + roster).
    // The auto-selected first agent also renders its name + description in the
    // detail pane, so scope every roster assertion to the roster aside.
    const asides = document.querySelectorAll('aside');
    const rosterAside = asides[1]; // sidebar is [0], roster is [1]
    expect(rosterAside).toBeTruthy();
    expect(within(rosterAside!).getByText('engineering_head')).toBeInTheDocument();
    expect(within(rosterAside!).getByText('support_agent')).toBeInTheDocument();
    // AGENTS-02: meta line reconciled toward the Direction-A 'role · status'
    // form. `role` is on the roster payload (→ Manager / Worker); `status` is
    // NOT, so it is omitted rather than fabricated. The old 'team · executor'
    // meta is gone — support_agent's executor ('codex') no longer appears.
    expect(within(rosterAside!).getByText('Manager')).toBeInTheDocument();
    expect(within(rosterAside!).getByText('Worker')).toBeInTheDocument();
    expect(within(rosterAside!).queryByText(/codex/)).not.toBeInTheDocument();
    expect(within(rosterAside!).getByText('Owns engineering.')).toBeInTheDocument();
    expect(within(rosterAside!).getByText('Handles support.')).toBeInTheDocument();
  });

  test('AGENTS-02: each roster row renders a client-derived avatar-initial chip', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    mountAt(`/orgs/${SLUG}/agents`);

    await waitFor(() =>
      expect(screen.getByText('support_agent')).toBeInTheDocument(),
    );
    const rosterAside = document.querySelectorAll('aside')[1];
    expect(rosterAside).toBeTruthy();
    // Initials are derived client-side from the agent name (two-token →
    // first letter of each of the first two parts): engineering_head → 'EH',
    // support_agent → 'SA'. No backend field, no per-agent hardcoded map.
    expect(within(rosterAside!).getByText('EH')).toBeInTheDocument();
    expect(within(rosterAside!).getByText('SA')).toBeInTheDocument();
    // Honesty fence: no fabricated status value (e.g. the prototype's
    // 'active'/'idle') is invented for the absent `status` field.
    expect(within(rosterAside!).queryByText('active')).not.toBeInTheDocument();
    expect(within(rosterAside!).queryByText('idle')).not.toBeInTheDocument();
  });

  test('clicking an agent row loads detail in the right pane', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents`);

    await waitFor(() =>
      expect(screen.getByText('engineering_head')).toBeInTheDocument(),
    );

    // Rows are buttons now, not links
    const rowBtn = screen.getByRole('button', { name: /engineering_head/ });
    await user.click(rowBtn);

    // Detail pane renders with agent metadata — role pill + team name
    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });
    expect(
      screen.getByText(/No tasks where this agent was the assigned manager/),
    ).toBeInTheDocument();
  });

  test('auto-selects the first roster agent on mount and renders its detail pane', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    mountAt(`/orgs/${SLUG}/agents`);

    // AGENTS-01: with a non-empty roster, the FIRST agent is auto-selected.
    // The first agent (engineering_head) is a manager and the second
    // (support_agent) is a worker, so the detail-pane "manager" role pill
    // uniquely proves the first agent's detail pane rendered by default.
    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });
    // Detail pane is rendered, so no empty "Select an agent" pane appears.
    expect(screen.queryByText(/Select an agent/)).not.toBeInTheDocument();
  });

  test('empty roster renders the calm empty state without auto-selecting', async () => {
    stubBaseHandlers();
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/agents`, () =>
        HttpResponse.json({ agents: [] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/agents`);

    // Left pane: roster empty state. Right pane: calm "No agents yet" state.
    // Nothing is auto-selected, so the page renders without error.
    await waitFor(() => {
      expect(screen.getByText('No agents enrolled')).toBeInTheDocument();
    });
    expect(screen.getByText('No agents yet')).toBeInTheDocument();
  });

  test('AGENTS-03: roster header primary action reads "New agent" (Direction-A label)', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    mountAt(`/orgs/${SLUG}/agents`);

    await waitFor(() =>
      expect(screen.getByText('support_agent')).toBeInTheDocument(),
    );
    // The roster-header primary action aligns to the authoritative
    // Direction-A `a-agents` reference label "New agent" (was "Add agent").
    // The dialog is closed here, so the trigger is the only "New agent" button.
    expect(
      screen.getByRole('button', { name: 'New agent' }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Add agent' }),
    ).not.toBeInTheDocument();
  });

  test('New agent button opens dialog', async () => {
    stubBaseHandlers();
    // Auto-select (AGENTS-01) mounts the detail pane — stub its endpoints.
    stubDetailHandlers();
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents`);

    await user.click(screen.getByRole('button', { name: 'New agent' }));
    const dialog = await screen.findByRole('dialog');
    // The dialog title is also "New agent"; scope to the dialog so it does not
    // collide with the (now identically-labeled) trigger button.
    expect(within(dialog).getByText('New agent')).toBeInTheDocument();
  });
});

describe('AgentDetailPane — editable fields', () => {
  test('eligible manager detail shows only compact active status and dedicated-page control', async () => {
    stubBaseHandlers(); stubDetailHandlers();
    const manager = { ...AGENTS_PAYLOAD.agents[0], name: 'engineering_manager' };
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/agents`, () => HttpResponse.json({ agents: [manager] })),
      http.get(`/api/v1/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy`, () => HttpResponse.json({
        team: 'engineering', target_manager: 'engineering_manager', can_mutate: true,
        bootstrap_template: { title: 'Canonical policy', normative_text: 'Policy', clauses: [], continuation_phrase: 'routine same-root follow-through of the already-completed slice' },
        active: { epoch: 4, release: { version: 2, digest: 'abcdef1234567890' } },
      })),
    );
    mountAt(`/orgs/${SLUG}/agents/engineering_manager`);
    expect(await screen.findByText(/Active v2 · epoch 4 · abcdef123456/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open team escalation policy' })).toHaveAttribute('href', `/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy`);
    expect(screen.queryByLabelText('Title')).not.toBeInTheDocument();
    expect(screen.queryByText('Immutable release history')).not.toBeInTheDocument();
  });

  test('omits the team policy card from a worker detail pane', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    mountAt(`/orgs/${SLUG}/agents/support_agent`);
    expect((await screen.findAllByText('Handles support.')).length).toBeGreaterThan(0);
    expect(screen.queryByTestId('team-escalation-policy')).not.toBeInTheDocument();
  });

  test('shows executor selector and allows switching', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    // Wait for both agent data and executor data to load.
    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });
    // Wait for the executor select to render with the agent's current executor.
    await waitFor(() => {
      const sel = document.querySelector('select[aria-label="Executor"]') as HTMLSelectElement;
      expect(sel).toBeTruthy();
      expect(sel.value).toBe('claude');
    });
  });

  test('shows repo chips and Add repository button', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    mountAt(`/orgs/${SLUG}/agents/support_agent`);

    await waitFor(() => {
      expect(screen.getByText('happyranch')).toBeInTheDocument();
    });
    expect(screen.getByText('Add repository')).toBeInTheDocument();
  });

  test('shows system prompt collapsible', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    await waitFor(() => {
      expect(screen.getByText('System prompt')).toBeInTheDocument();
    });
    await user.click(screen.getByText('System prompt'));
    await waitFor(() => {
      expect(screen.getByText(/You are the engineering head/)).toBeInTheDocument();
    });
  });

  test('shows accountability metrics with real task counts', async () => {
    stubBaseHandlers();
    stubDetailHandlers([
      {
        task_id: 'TASK-001',
        brief: 'Test task',
        status: 'completed',
        team: 'engineering',
        assigned_agent: 'engineering_head',
        parent_task_id: null,
        revisit_of_task_id: null,
        created_at: '2026-06-01T00:00:00Z',
        updated_at: '2026-06-01T00:00:00Z',
        closed_at: null,
        cancelled_at: null,
        session_timeout_seconds: null,
        block_kind: null,
      },
      {
        task_id: 'TASK-002',
        brief: 'Pending task',
        status: 'pending',
        team: 'engineering',
        assigned_agent: 'engineering_head',
        parent_task_id: null,
        revisit_of_task_id: null,
        created_at: '2026-06-02T00:00:00Z',
        updated_at: '2026-06-02T00:00:00Z',
        closed_at: null,
        cancelled_at: null,
        session_timeout_seconds: null,
        block_kind: null,
      },
    ]);
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    await waitFor(() => {
      expect(screen.getByText('Accountability')).toBeInTheDocument();
      expect(screen.getByText('done')).toBeInTheDocument();
      expect(screen.getByText('tasks')).toBeInTheDocument();
    });
  });

  test('Start Thread button opens compose dialog with agent prefill', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });

    // Click the Start Thread button in the detail pane header
    const startBtn = screen.getByRole('button', { name: /Start Thread/i });
    expect(startBtn).toBeInTheDocument();
    await user.click(startBtn);

    // Dialog opens with recipients pre-filled to the selected agent
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('New thread')).toBeInTheDocument();
    const recipientsInput = within(dialog).getByLabelText(/^Recipients/i);
    expect(recipientsInput).toHaveValue('engineering_head');
  });

  test('Start Thread dialog double-click Send creates exactly one thread (synchronous in-flight guard)', async () => {
    stubBaseHandlers();
    stubDetailHandlers();

    // Delay the POST so a second submit can enter before resolution.
    // Use fireEvent.click (synchronous) instead of user.click (sequential)
    // so the second click arrives before React re-renders to disable the
    // button. Without the in-flight latch, this triggers two POSTs.
    let resolvePost: (v: unknown) => void;
    const postDeferred = new Promise((r) => { resolvePost = r; });
    let postCount = 0;
    let detailGetCount = 0;

    server.use(
      http.post(`/api/v1/orgs/${SLUG}/threads`, async () => {
        postCount++;
        await postDeferred;
        return HttpResponse.json(
          { thread_id: 'THR-AGENT-DBL', started_at: 'now', pending_replies: 1 },
          { status: 201 },
        );
      }),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-AGENT-DBL`, () => {
        detailGetCount++;
        return HttpResponse.json({
          thread_id: 'THR-AGENT-DBL',
          subject: 'Only one',
          status: 'open',
          started_at: 'now',
          archived_at: null,
          forwarded_from_id: null,
          forwarded_from_kind: null,
          turn_cap: 500,
          turns_used: 0,
          summary: null,
          transcript_path: null,
          participants: ['engineering_head'],
          messages: [],
        });
      }),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-AGENT-DBL/messages`, () =>
        HttpResponse.json({ messages: [] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-AGENT-DBL/tail`, () =>
        HttpResponse.text('', { headers: { 'content-type': 'text/event-stream' } }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads`, () =>
        HttpResponse.json({ threads: [] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads/events`, () =>
        HttpResponse.text('', { headers: { 'content-type': 'text/event-stream' } }),
      ),
    );
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });

    // Click Start Thread to open dialog
    await user.click(screen.getByRole('button', { name: /Start Thread/i }));

    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText(/^Subject$/i), 'Only one');
    // Recipients already pre-filled to engineering_head
    await user.type(within(dialog).getByLabelText(/^Body \(Markdown\)$/i), 'Body');

    // fireEvent.click is synchronous — two clicks in the same tick.
    // The first starts the delayed POST; the second must be rejected
    // by the synchronous in-flight latch before React re-renders.
    const sendBtn = within(dialog).getByRole('button', { name: /^Send$/i });
    fireEvent.click(sendBtn);
    fireEvent.click(sendBtn);

    // Release the deferred POST.
    resolvePost!({});

    // Assert exactly one POST reached the server.
    await waitFor(() => {
      expect(postCount).toBe(1);
    });
    // Assert exactly one navigation happened.
    await waitFor(() => {
      expect(detailGetCount).toBe(1);
    });
    // Navigated once to thread detail.
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: /Only one/i })).toBeInTheDocument(),
    );
  });

  test('Start Thread dialog MentionTextarea Enter + Send button race creates exactly one thread (cross-path guard)', async () => {
    stubBaseHandlers();
    stubDetailHandlers();

    // Delay the POST so cross-path submit can race before resolution.
    let resolvePost: (v: unknown) => void;
    const postDeferred = new Promise((r) => { resolvePost = r; });
    let postCount = 0;
    let detailGetCount = 0;

    server.use(
      http.post(`/api/v1/orgs/${SLUG}/threads`, async () => {
        postCount++;
        await postDeferred;
        return HttpResponse.json(
          { thread_id: 'THR-AGENT-CROSS', started_at: 'now', pending_replies: 1 },
          { status: 201 },
        );
      }),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-AGENT-CROSS`, () => {
        detailGetCount++;
        return HttpResponse.json({
          thread_id: 'THR-AGENT-CROSS',
          subject: 'Cross path',
          status: 'open',
          started_at: 'now',
          archived_at: null,
          forwarded_from_id: null,
          forwarded_from_kind: null,
          turn_cap: 500,
          turns_used: 0,
          summary: null,
          transcript_path: null,
          participants: ['engineering_head'],
          messages: [],
        });
      }),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-AGENT-CROSS/messages`, () =>
        HttpResponse.json({ messages: [] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-AGENT-CROSS/tail`, () =>
        HttpResponse.text('', { headers: { 'content-type': 'text/event-stream' } }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads`, () =>
        HttpResponse.json({ threads: [] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads/events`, () =>
        HttpResponse.text('', { headers: { 'content-type': 'text/event-stream' } }),
      ),
    );
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /Start Thread/i }));

    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText(/^Subject$/i), 'Cross path');
    // Recipients already pre-filled to engineering_head
    await user.type(within(dialog).getByLabelText(/^Body \(Markdown\)$/i), 'Body');

    const sendBtn = within(dialog).getByRole('button', { name: /^Send$/i });
    const bodyTextarea = within(dialog).getByLabelText(/^Body \(Markdown\)$/i);

    // MentionTextarea onSubmit fires on Enter (when popup is closed).
    // Fire Enter synchronously, then immediately click Send before React
    // re-renders to disable either path. The latch must reject the second.
    fireEvent.keyDown(bodyTextarea, { key: 'Enter' });
    fireEvent.click(sendBtn);

    resolvePost!({});

    await waitFor(() => {
      expect(postCount).toBe(1);
    });
    await waitFor(() => {
      expect(detailGetCount).toBe(1);
    });
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: /Cross path/i })).toBeInTheDocument(),
    );
  });

  test('Start Thread dialog posts compose body and navigates to new thread detail', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    let composeBody: unknown = null;
    server.use(
      http.post(`/api/v1/orgs/${SLUG}/threads`, async ({ request }) => {
        composeBody = await request.json();
        return HttpResponse.json(
          { thread_id: 'THR-AGENT-1', started_at: 'now', pending_replies: 1 },
          { status: 201 },
        );
      }),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-AGENT-1`, () =>
        HttpResponse.json({
          thread_id: 'THR-AGENT-1',
          subject: 'Hello from agents',
          status: 'open',
          started_at: 'now',
          archived_at: null,
          forwarded_from_id: null,
          forwarded_from_kind: null,
          turn_cap: 500,
          turns_used: 0,
          summary: null,
          transcript_path: null,
          participants: ['engineering_head'],
          messages: [],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-AGENT-1/messages`, () =>
        HttpResponse.json({ messages: [] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-AGENT-1/tail`, () =>
        HttpResponse.text('', { headers: { 'content-type': 'text/event-stream' } }),
      ),
      // Thread detail SSE and thread list
      http.get(`/api/v1/orgs/${SLUG}/threads`, () =>
        HttpResponse.json({ threads: [] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads/events`, () =>
        HttpResponse.text('', { headers: { 'content-type': 'text/event-stream' } }),
      ),
    );
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });

    // Click Start Thread
    await user.click(screen.getByRole('button', { name: /Start Thread/i }));

    const dialog = await screen.findByRole('dialog');
    // Recipients already prefilled — fill subject and body
    await user.type(within(dialog).getByLabelText(/^Subject$/i), 'Hello from agents');
    await user.type(within(dialog).getByLabelText(/^Body \(Markdown\)$/i), 'Let us begin');
    await user.click(within(dialog).getByRole('button', { name: /^Send$/i }));

    // Assert POST received the pre-filled recipient
    await waitFor(() => {
      expect(composeBody).toEqual({
        subject: 'Hello from agents',
        recipients: ['engineering_head'],
        body_markdown: 'Let us begin',
      });
    });
    // Navigated to thread detail — header shows subject
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: /Hello from agents/i })).toBeInTheDocument(),
    );
  });
});

describe('AgentDetailPane — save flow (executor switch)', () => {
  // Clear sessionStorage between tests
  beforeEach(() => {
    sessionStorage.clear();
  });

  test('shows save bar when executor is changed', async () => {
    let executorPutCalled = false;
    stubBaseHandlers();
    stubDetailHandlers();
    server.use(
      http.put(`/api/v1/orgs/${SLUG}/agents/engineering_head/executor`, async () => {
        executorPutCalled = true;
        return HttpResponse.json({
          agent: 'engineering_head',
          before: { org_executor: 'claude', workspace_executor: 'claude' },
          after: { org_executor: 'codex', workspace_executor: 'codex' },
          stale_files: [],
        });
      }),
    );
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    // Wait for detail pane to render.
    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });
    // Wait for the executor select to render with the agent's current executor.
    let executorSelect: HTMLSelectElement | null = null;
    await waitFor(() => {
      executorSelect = document.querySelector('select[aria-label="Executor"]');
      expect(executorSelect).toBeTruthy();
      expect(executorSelect!.value).toBe('claude');
    });
    // Switch to codex.
    await user.selectOptions(executorSelect!, 'codex');

    // Save bar should appear
    await waitFor(() => {
      expect(screen.getByText(/unsaved changes/)).toBeInTheDocument();
    });
    expect(screen.getByText('Save agent')).toBeInTheDocument();
    expect(screen.getByText('Reset')).toBeInTheDocument();

    // Click Save
    await user.click(screen.getByText('Save agent'));

    await waitFor(() => {
      expect(executorPutCalled).toBe(true);
    });
  });

  test('Reset reverts dirty state', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });
    // Wait for the executor select to render.
    let executorSelect: HTMLSelectElement | null = null;
    await waitFor(() => {
      executorSelect = document.querySelector('select[aria-label="Executor"]');
      expect(executorSelect).toBeTruthy();
      expect(executorSelect!.value).toBe('claude');
    });
    await user.selectOptions(executorSelect!, 'codex');

    // Save bar visible
    await waitFor(() => {
      expect(screen.getByText('Reset')).toBeInTheDocument();
    });

    // Click Reset
    await user.click(screen.getByText('Reset'));

    // Save bar hidden — executor back to claude
    await waitFor(() => {
      expect(screen.queryByText('Reset')).not.toBeInTheDocument();
    });
  });
});

describe('AgentDetailPane — save flow (repo management)', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  test('shows save bar when a repo is removed', async () => {
    let repoRemoveCalled = false;
    stubBaseHandlers();
    stubDetailHandlers();
    server.use(
      http.post(`/api/v1/orgs/${SLUG}/agents/support_agent/repos`, async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        if (body.action === 'remove') repoRemoveCalled = true;
        return HttpResponse.json({ ok: true });
      }),
    );
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/support_agent`);

    await waitFor(() => {
      expect(screen.getByText('happyranch')).toBeInTheDocument();
    });

    // Click X to remove the repo
    const removeBtn = screen.getByRole('button', { name: 'Remove happyranch' });
    await user.click(removeBtn);

    // Save bar should appear
    await waitFor(() => {
      expect(screen.getByText('Save agent')).toBeInTheDocument();
    });

    // Click Save
    await user.click(screen.getByText('Save agent'));

    await waitFor(() => {
      expect(repoRemoveCalled).toBe(true);
    });
  });

  test('edit mutation error preserves custom profile selection and error remains visible', async () => {
    // This test must select an available custom profile (not the built-in codex)
    // and directly assert that the exact custom selection remains displayed/selected
    // and the error remains visible after the failed save.
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/agents`, () =>
        HttpResponse.json(AGENTS_PAYLOAD),
      ),
      http.get(`/api/v1/orgs/${SLUG}/settings`, () =>
        HttpResponse.json({}),
      ),
      http.get(`/api/v1/orgs/${SLUG}/teams`, () =>
        HttpResponse.json({ teams: [] }),
      ),
      // Only claude is launchable; custom profile openclaw is present=true.
      http.get('/api/v1/health/prereqs', () =>
        HttpResponse.json({
          prereqs: [
            { tool: 'claude', present: true, path: '/usr/local/bin/claude', hint: '' },
            { tool: 'codex', present: false, path: null, hint: '' },
            { tool: 'opencode', present: false, path: null, hint: '' },
            { tool: 'pi', present: false, path: null, hint: '' },
          ],
        }),
      ),
      http.get('/api/v1/executors/runtime/profiles', () =>
        HttpResponse.json({
          profiles: [
            { name: 'openclaw', command: 'openclaw', adapter: 'pi', workspace_adapter_id: 'pi', adapter_id: 'pi', command_adapter_id: 'custom-adapter:openclaw', present: true, path: '/usr/bin/openclaw', envelope_policy: null },
          ],
        }),
      ),
      // Executor save fails with 500.
      http.put(`/api/v1/orgs/${SLUG}/agents/engineering_head/executor`, () =>
        HttpResponse.json({ detail: 'Internal error' }, { status: 500 }),
      ),
    );
    stubDetailHandlers();
    const user = userEvent.setup();
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/agents/engineering_head`,
    });

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });
    let executorSelect: HTMLSelectElement | null = null;
    await waitFor(() => {
      executorSelect = document.querySelector('select[aria-label="Executor"]');
      expect(executorSelect).toBeTruthy();
    });

    // Select the custom profile openclaw (not the built-in codex).
    await user.selectOptions(executorSelect!, 'openclaw');
    await waitFor(() => {
      expect(screen.getByText('Save agent')).toBeInTheDocument();
    });

    // Click Save — it fails.
    await user.click(screen.getByText('Save agent'));

    // Assert error is visible.
    await waitFor(() => {
      expect(screen.getByText(/Save error/)).toBeInTheDocument();
    });
    // Assert the custom profile selection (openclaw) is STILL displayed/selected.
    executorSelect = document.querySelector('select[aria-label="Executor"]') as HTMLSelectElement;
    expect(executorSelect.value).toBe('openclaw');
  });

  test('custom profile selectable in edit and sends PUT request', async () => {
    let executorPutBody: unknown = null;
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/agents`, () =>
        HttpResponse.json(AGENTS_PAYLOAD),
      ),
      http.get(`/api/v1/orgs/${SLUG}/settings`, () =>
        HttpResponse.json({}),
      ),
      http.get(`/api/v1/orgs/${SLUG}/teams`, () =>
        HttpResponse.json({ teams: [] }),
      ),
      // Executor prereqs — include a custom profile with present=true.
      http.get('/api/v1/health/prereqs', () =>
        HttpResponse.json({
          prereqs: [
            { tool: 'claude', present: true, path: '/usr/local/bin/claude', hint: '' },
            { tool: 'codex', present: false, path: null, hint: '' },
            { tool: 'opencode', present: false, path: null, hint: '' },
            { tool: 'pi', present: false, path: null, hint: '' },
          ],
        }),
      ),
      http.get('/api/v1/executors/runtime/profiles', () =>
        HttpResponse.json({
          profiles: [
            { name: 'openclaw', command: 'openclaw', adapter: 'pi', workspace_adapter_id: 'pi', adapter_id: 'pi', command_adapter_id: 'custom-adapter:openclaw', present: true, path: '/usr/bin/openclaw', envelope_policy: null },
          ],
        }),
      ),
      http.put(`/api/v1/orgs/${SLUG}/agents/engineering_head/executor`, async ({ request }) => {
        executorPutBody = await request.json();
        return HttpResponse.json({
          agent: 'engineering_head',
          before: { org_executor: 'claude', workspace_executor: 'claude' },
          after: { org_executor: 'openclaw', workspace_executor: 'openclaw' },
          stale_files: [],
        });
      }),
    );
    stubDetailHandlers();
    const user = userEvent.setup();
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/agents/engineering_head`,
    });

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });
    // Wait for the executor select to render.
    let executorSelect: HTMLSelectElement | null = null;
    await waitFor(() => {
      executorSelect = document.querySelector('select[aria-label="Executor"]');
      expect(executorSelect).toBeTruthy();
    });
    // The custom profile should be selectable.
    const customOpt = screen.getByRole('option', { name: 'openclaw (custom)' }) as HTMLOptionElement;
    expect(customOpt).toBeInTheDocument();
    expect(customOpt.disabled).toBe(false);

    // Select the custom profile.
    await user.selectOptions(executorSelect!, 'openclaw');
    await waitFor(() => {
      expect(screen.getByText('Save agent')).toBeInTheDocument();
    });

    // Click Save.
    await user.click(screen.getByText('Save agent'));
    await waitFor(() => {
      expect(executorPutBody).toEqual({ executor: 'openclaw' });
    });
  });

  test('unavailable custom profile is shown disabled in edit pane', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/agents`, () =>
        HttpResponse.json(AGENTS_PAYLOAD),
      ),
      http.get(`/api/v1/orgs/${SLUG}/settings`, () =>
        HttpResponse.json({}),
      ),
      http.get(`/api/v1/orgs/${SLUG}/teams`, () =>
        HttpResponse.json({ teams: [] }),
      ),
      http.get('/api/v1/health/prereqs', () =>
        HttpResponse.json({
          prereqs: [
            { tool: 'claude', present: true, path: '/usr/local/bin/claude', hint: '' },
            { tool: 'codex', present: false, path: null, hint: '' },
            { tool: 'opencode', present: false, path: null, hint: '' },
            { tool: 'pi', present: false, path: null, hint: '' },
          ],
        }),
      ),
      // Custom profile with present=false → should be disabled.
      http.get('/api/v1/executors/runtime/profiles', () =>
        HttpResponse.json({
          profiles: [
            { name: 'openclaw', command: 'openclaw', adapter: 'pi', workspace_adapter_id: 'pi', adapter_id: 'pi', command_adapter_id: 'custom-adapter:openclaw', present: false, path: null, envelope_policy: null },
          ],
        }),
      ),
    );
    stubDetailHandlers();
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/agents/engineering_head`,
    });

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });
    // Wait for the executor select to render.
    await waitFor(() => {
      const sel = document.querySelector('select[aria-label="Executor"]');
      expect(sel).toBeTruthy();
    });
    // The unavailable custom profile is in the disabled section.
    const unavailableOpt = screen.getByRole('option', { name: /openclaw.*unavailable/ });
    expect(unavailableOpt).toBeInTheDocument();
    expect((unavailableOpt as HTMLOptionElement).disabled).toBe(true);
    // Settings → Executors link is visible adjacent to the select.
    const settingsLink = screen.getByRole('link', { name: /Settings → Executors/i });
    expect(settingsLink).toBeInTheDocument();
    expect(settingsLink).toHaveAttribute('href', `/orgs/${SLUG}/settings/executors`);
  });

  test('stale agent executor remains visible but cannot be assigned', async () => {
    // Agent has executor "oldrunner" but the live data has only claude.
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/agents`, () =>
        HttpResponse.json({
          agents: [
            {
              name: 'stale_agent',
              team: 'engineering',
              role: 'worker',
              executor: 'oldrunner',
              model: null,
              description: 'Has a stale executor.',
              repos: {},
              system_prompt: 'stale',
            },
          ],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/settings`, () =>
        HttpResponse.json({}),
      ),
      http.get(`/api/v1/orgs/${SLUG}/teams`, () =>
        HttpResponse.json({ teams: [] }),
      ),
      http.get('/api/v1/health/prereqs', () =>
        HttpResponse.json({
          prereqs: [
            { tool: 'claude', present: true, path: '/usr/local/bin/claude', hint: '' },
          ],
        }),
      ),
      http.get('/api/v1/executors/runtime/profiles', () =>
        HttpResponse.json({ profiles: [] }),
      ),
    );
    stubDetailHandlers();
    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/agents/stale_agent`,
    });

    await waitFor(() => {
      expect(screen.getByText('worker')).toBeInTheDocument();
    });
    // Wait for the executor select to render.
    await waitFor(() => {
      const sel = document.querySelector('select[aria-label="Executor"]');
      expect(sel).toBeTruthy();
    });
    // The stale executor is visible as a disabled option.
    const staleOpt = screen.getByRole('option', { name: /oldrunner.*no longer registered/ });
    expect(staleOpt).toBeInTheDocument();
    expect((staleOpt as HTMLOptionElement).disabled).toBe(true);
    // The select's value is the stale executor (retained).
    const sel = document.querySelector('select[aria-label="Executor"]') as HTMLSelectElement;
    expect(sel.value).toBe('oldrunner');
  });

  test('edit refetch/removal: agent current executor disappears on refetch, stays disabled, no PUT on Save', async () => {
    // An agent whose current executor is a custom profile (openclaw).
    // Initially openclaw is available (present=true). After a refetch it
    // disappears from profiles entirely — the stale executor must remain
    // visible-disabled, and no dirty state means no PUT is emitted.
    let putCalled = false;
    sessionStorage.setItem('happyranch.token', 'tok');

    // Phase A handlers: openclaw is available.
    const phaseAProfiles = () =>
      HttpResponse.json({
        profiles: [
          { name: 'openclaw', command: 'openclaw', adapter: 'pi', workspace_adapter_id: 'pi', adapter_id: 'pi', command_adapter_id: 'custom-adapter:openclaw', present: true, path: '/usr/bin/openclaw', envelope_policy: null },
        ],
      });

    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/agents`, () =>
        HttpResponse.json({
          agents: [
            {
              name: 'custom_exec_agent',
              team: 'engineering',
              role: 'worker',
              executor: 'openclaw',
              model: null,
              description: 'Uses a custom executor.',
              repos: {},
              system_prompt: 'custom exec',
            },
          ],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/settings`, () =>
        HttpResponse.json({}),
      ),
      http.get(`/api/v1/orgs/${SLUG}/teams`, () =>
        HttpResponse.json({ teams: [] }),
      ),
      http.get('/api/v1/health/prereqs', () =>
        HttpResponse.json({
          prereqs: [
            { tool: 'claude', present: true, path: '/usr/local/bin/claude', hint: '' },
          ],
        }),
      ),
      http.get('/api/v1/executors/runtime/profiles', phaseAProfiles),
      http.put(`/api/v1/orgs/${SLUG}/agents/custom_exec_agent/executor`, async () => {
        putCalled = true;
        return HttpResponse.json({ ok: true });
      }),
    );
    stubDetailHandlers();

    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/agents/custom_exec_agent`,
    });

    await waitFor(() => {
      expect(screen.getByText('worker')).toBeInTheDocument();
    });
    // Phase A: openclaw is present and selectable.
    await waitFor(() => {
      const sel = document.querySelector('select[aria-label="Executor"]');
      expect(sel).toBeTruthy();
    });
    const selectableOpt = screen.getByRole('option', { name: 'openclaw (custom)' }) as HTMLOptionElement;
    expect(selectableOpt).toBeInTheDocument();
    expect(selectableOpt.disabled).toBe(false);

    // Phase B: update the runtime/profiles handler so openclaw disappears.
    server.use(
      http.get('/api/v1/executors/runtime/profiles', () =>
        HttpResponse.json({ profiles: [] }),
      ),
    );

    // Re-render at the same route to trigger fresh query fetches.
    const { unmount } = renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/agents/custom_exec_agent`,
    });
    await waitFor(() => {
      expect(screen.getByText('worker')).toBeInTheDocument();
    });

    // Phase B: openclaw is now stale — visible, disabled, and no Save bar.
    await waitFor(() => {
      const sel = document.querySelector('select[aria-label="Executor"]');
      expect(sel).toBeTruthy();
    });
    const staleOpt = screen.getByRole('option', { name: /openclaw.*no longer registered/ });
    expect(staleOpt).toBeInTheDocument();
    expect((staleOpt as HTMLOptionElement).disabled).toBe(true);
    // The select's value remains the stale executor.
    const sel = document.querySelector('select[aria-label="Executor"]') as HTMLSelectElement;
    expect(sel.value).toBe('openclaw');
    // No Save bar — nothing is dirty.
    expect(screen.queryByText('Save agent')).not.toBeInTheDocument();
    // No PUT was emitted.
    expect(putCalled).toBe(false);

    unmount();
  });

  test('edit dirty-selection guard: when selected custom profile becomes unavailable, Save errors and no PUT is emitted', async () => {
    // Agent whose current executor is 'claude' (NOT the custom profile).
    // User selects 'openclaw' (custom, present=true), making it dirty.
    // Then 'openclaw' becomes unavailable (present:false) via refetch.
    // Save must error with the guard message and never call PUT.
    let putCalled = false;
    sessionStorage.setItem('happyranch.token', 'tok');

    // Phase A handlers: openclaw is available (present=true).
    const phaseAProfiles = () =>
      HttpResponse.json({
        profiles: [
          { name: 'openclaw', command: 'openclaw', adapter: 'pi', workspace_adapter_id: 'pi', adapter_id: 'pi', command_adapter_id: 'custom-adapter:openclaw', present: true, path: '/usr/bin/openclaw', envelope_policy: null },
        ],
      });

    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/agents`, () =>
        HttpResponse.json({
          agents: [
            {
              name: 'claude_agent',
              team: 'engineering',
              role: 'worker',
              executor: 'claude',
              model: null,
              description: 'Uses claude.',
              repos: {},
              system_prompt: 'claude exec',
            },
          ],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/settings`, () =>
        HttpResponse.json({}),
      ),
      http.get(`/api/v1/orgs/${SLUG}/teams`, () =>
        HttpResponse.json({ teams: [] }),
      ),
      http.get('/api/v1/health/prereqs', () =>
        HttpResponse.json({
          prereqs: [
            { tool: 'claude', present: true, path: '/usr/local/bin/claude', hint: '' },
          ],
        }),
      ),
      http.get('/api/v1/executors/runtime/profiles', phaseAProfiles),
      http.put(`/api/v1/orgs/${SLUG}/agents/claude_agent/executor`, async () => {
        putCalled = true;
        return HttpResponse.json({ ok: true });
      }),
    );
    stubDetailHandlers();

    // ONE mount, ONE QueryClient — expose it to invalidate queries without
    // unmounting (which would reset dirty state).
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={[`/orgs/${SLUG}/agents/claude_agent`]}>
        <AppProvider client={qc}>
          <AppRoutes />
        </AppProvider>
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText('worker')).toBeInTheDocument();
    });
    // Wait for the executor select to render.
    let execSelect!: HTMLSelectElement;
    await waitFor(() => {
      execSelect = document.querySelector('select[aria-label="Executor"]') as HTMLSelectElement;
      expect(execSelect).toBeTruthy();
    });
    // Phase A: openclaw is selectable.
    const selectableOpt = screen.getByRole('option', { name: 'openclaw (custom)' }) as HTMLOptionElement;
    expect(selectableOpt).toBeInTheDocument();
    expect(selectableOpt.disabled).toBe(false);

    // Select the custom profile — makes dirty.executor = 'openclaw'.
    await user.selectOptions(execSelect, 'openclaw');
    await waitFor(() => {
      expect(screen.getByText('Save agent')).toBeInTheDocument();
    });
    // Confirm the select now shows openclaw.
    execSelect = document.querySelector('select[aria-label="Executor"]') as HTMLSelectElement;
    expect(execSelect.value).toBe('openclaw');

    // Phase B: openclaw becomes unavailable (present:false) via refetch.
    server.use(
      http.get('/api/v1/executors/runtime/profiles', () =>
        HttpResponse.json({
          profiles: [
            { name: 'openclaw', command: 'openclaw', adapter: 'pi', workspace_adapter_id: 'pi', adapter_id: 'pi', command_adapter_id: 'custom-adapter:openclaw', present: false, path: null, envelope_policy: null },
          ],
        }),
      ),
    );
    // Invalidate the runtime-profiles query to trigger a refetch while the
    // component stays mounted (dirty state is preserved).
    await act(async () => {
      await qc.invalidateQueries({ queryKey: ['runtime-profiles'] });
    });

    // Phase B: openclaw is now in the disabled/unavailable section.
    await waitFor(() => {
      const disabledOpt = screen.getByRole('option', { name: /openclaw.*unavailable/ });
      expect(disabledOpt).toBeInTheDocument();
      expect((disabledOpt as HTMLOptionElement).disabled).toBe(true);
    });
    // Save bar is still visible — the dirty selection is preserved.
    expect(screen.getByText('Save agent')).toBeInTheDocument();

    // Click Save — the dirty-selection guard at AgentDetailPane.onSave
    // must detect that the selected executor is no longer selectable
    // and error WITHOUT issuing a PUT.
    await user.click(screen.getByText('Save agent'));

    // Guard error: "Executor "openclaw" is no longer available."
    await waitFor(() => {
      expect(screen.getByText(/Save error/)).toBeInTheDocument();
      expect(screen.getByText(/no longer available/)).toBeInTheDocument();
    });
    // The dirty selection (openclaw) is retained — not cleared.
    execSelect = document.querySelector('select[aria-label="Executor"]') as HTMLSelectElement;
    expect(execSelect.value).toBe('openclaw');
    // The executor PUT was NEVER called.
    expect(putCalled).toBe(false);
  });
});

describe('AgentDetailPane — save flow (model editing)', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  test('renders current model in text input and allows edit + save', async () => {
    let modelPutCalled = false;
    let modelPutBody: unknown = null;
    stubBaseHandlers();
    stubDetailHandlers();
    server.use(
      http.put(`/api/v1/orgs/${SLUG}/agents/engineering_head/model`, async ({ request }) => {
        modelPutCalled = true;
        modelPutBody = await request.json();
        return HttpResponse.json({
          agent: 'engineering_head',
          before: 'claude-sonnet-4-20250514',
          after: 'gpt-5',
        });
      }),
    );
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });

    // Find the Model text input
    const modelInput = screen.getByRole('textbox', { name: 'Model' }) as HTMLInputElement;
    expect(modelInput.value).toBe('claude-sonnet-4-20250514');

    // Edit the model
    await user.clear(modelInput);
    await user.type(modelInput, 'gpt-5');

    // Save bar should appear
    await waitFor(() => {
      expect(screen.getByText(/unsaved changes/)).toBeInTheDocument();
    });

    // Click Save
    await user.click(screen.getByText('Save agent'));

    await waitFor(() => {
      expect(modelPutCalled).toBe(true);
    });
    expect(modelPutBody).toEqual({ model: 'gpt-5' });
  });

  test('empty input clears model (sends null)', async () => {
    let modelPutBody: unknown = null;
    stubBaseHandlers();
    stubDetailHandlers();
    server.use(
      http.put(`/api/v1/orgs/${SLUG}/agents/engineering_head/model`, async ({ request }) => {
        modelPutBody = await request.json();
        return HttpResponse.json({
          agent: 'engineering_head',
          before: 'claude-sonnet-4-20250514',
          after: null,
        });
      }),
    );
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });

    const modelInput = screen.getByRole('textbox', { name: 'Model' }) as HTMLInputElement;
    await user.clear(modelInput);

    await waitFor(() => {
      expect(screen.getByText('Save agent')).toBeInTheDocument();
    });

    await user.click(screen.getByText('Save agent'));

    await waitFor(() => {
      expect(modelPutBody).toEqual({ model: null });
    });
  });

  test('restoring original value clears dirty state', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });

    const modelInput = screen.getByRole('textbox', { name: 'Model' }) as HTMLInputElement;
    // change to something else
    await user.clear(modelInput);
    await user.type(modelInput, 'gpt-5');

    await waitFor(() => {
      expect(screen.getByText('Reset')).toBeInTheDocument();
    });

    // restore original value
    await user.clear(modelInput);
    await user.type(modelInput, 'claude-sonnet-4-20250514');

    await waitFor(() => {
      expect(screen.queryByText('Reset')).not.toBeInTheDocument();
    });
  });
});

describe('AgentsPage — route collision regression', () => {
  test('an agent literally named "pending" shows detail in right pane (not the tab)', async () => {
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/agents`, () =>
        HttpResponse.json({
          agents: [
            {
              name: 'pending',
              team: 'engineering',
              role: 'worker',
              executor: 'claude',
              description: 'Edge-case agent name.',
              repos: {},
              system_prompt: 'You are pending.',
            },
          ],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/tasks`, () =>
        HttpResponse.json({ tasks: [] }),
      ),
      http.get(
        `/api/v1/orgs/${SLUG}/agents/pending/memory/entries/`,
        () => HttpResponse.json({ entries: [] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () =>
        HttpResponse.json({ jobs: [] }),
      ),
    );
    mountAt(`/orgs/${SLUG}/agents/pending`);

    // Detail pane shows the "pending" agent's metadata, not the enrollments tab
    await waitFor(() =>
      expect(screen.getByText('worker')).toBeInTheDocument(),
    );
  });
});

describe('Team escalation policy dedicated route', () => {
  const manager = {
    name: 'engineering_manager', team: 'engineering', role: 'manager',
    executor: 'codex', model: null, description: 'Owns engineering.', repos: {}, system_prompt: 'Manager.',
  };
  const policyResponse = {
    team: 'engineering', target_manager: 'engineering_manager', can_mutate: true,
    bootstrap_required: true,
    bootstrap_template: { title: 'Canonical policy', normative_text: 'Policy', clauses: [], continuation_phrase: 'routine same-root follow-through of the already-completed slice' },
  };

  function stubPolicy() {
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy`, () => HttpResponse.json(policyResponse)),
      http.get(`/api/v1/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy/history`, () => HttpResponse.json({ items: [], next_cursor: null })),
      http.get(`/api/v1/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy/outcomes`, () => HttpResponse.json({ items: [], next_cursor: null })),
    );
  }

  test('eligible deep link resolves after roster eligibility and provides context/back link', async () => {
    stubBaseHandlers();
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/agents`, () => HttpResponse.json({ agents: [manager] })),
      http.get(`/api/v1/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy`, () => HttpResponse.json(policyResponse)),
      http.get(`/api/v1/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy/history`, () => HttpResponse.json({ items: [], next_cursor: null })),
      http.get(`/api/v1/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy/outcomes`, () => HttpResponse.json({ items: [], next_cursor: null })),
    );
    mountPolicyRoute([`/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy`]);
    expect(await screen.findByRole('heading', { level: 1, name: 'Team escalation policy' })).toBeInTheDocument();
    expect(screen.getByText('Engineering · Engineering Manager')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Back to Engineering Manager/ })).toHaveAttribute('href', `/orgs/${SLUG}/agents/engineering_manager`);
    expect(await screen.findByLabelText('Title')).toBeInTheDocument();
  });

  test('shipping back Link cancel preserves the exact dirty draft; confirm discards and navigates', async () => {
    stubBaseHandlers();
    server.use(http.get(`/api/v1/orgs/${SLUG}/agents`, () => HttpResponse.json({ agents: [manager] })));
    stubPolicy();
    const user = userEvent.setup();
    mountPolicyRoute([`/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy`]);
    const title = await screen.findByRole('textbox', { name: 'Title' });
    await user.clear(title);
    await user.type(title, 'Exact retained draft');
    await user.click(screen.getByRole('link', { name: /Back to Engineering Manager/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Discard unsaved policy changes?' });
    await user.click(within(dialog).getByRole('button', { name: 'Stay on page' }));
    expect(screen.getByRole('textbox', { name: 'Title' })).toHaveValue('Exact retained draft');
    expect(screen.getByRole('heading', { level: 1, name: 'Team escalation policy' })).toBeInTheDocument();

    await user.click(screen.getByRole('link', { name: /Back to Engineering Manager/ }));
    await user.click(await screen.findByRole('button', { name: 'Discard and continue' }));
    await waitFor(() => expect(screen.queryByRole('heading', { level: 1, name: 'Team escalation policy' })).not.toBeInTheDocument());
    expect(screen.queryByDisplayValue('Exact retained draft')).not.toBeInTheDocument();
  });

  test('memory history back cancel preserves draft and confirm completes the original POP; forward restores route cleanly', async () => {
    stubBaseHandlers();
    server.use(http.get(`/api/v1/orgs/${SLUG}/agents`, () => HttpResponse.json({ agents: [manager] })));
    stubDetailHandlers();
    stubPolicy();
    const user = userEvent.setup();
    mountPolicyRoute([
      `/orgs/${SLUG}/agents/engineering_manager`,
      `/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy`,
    ]);
    const title = await screen.findByRole('textbox', { name: 'Title' });
    await user.clear(title);
    await user.type(title, 'History-retained draft');
    await user.click(screen.getByRole('button', { name: 'Test browser back' }));
    await user.click(await screen.findByRole('button', { name: 'Stay on page' }));
    expect(screen.getByRole('textbox', { name: 'Title' })).toHaveValue('History-retained draft');
    await user.click(screen.getByRole('button', { name: 'Test browser back' }));
    await user.click(await screen.findByRole('button', { name: 'Discard and continue' }));
    await waitFor(() => expect(screen.queryByRole('heading', { level: 1, name: 'Team escalation policy' })).not.toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: 'Test browser forward' }));
    expect(await screen.findByRole('textbox', { name: 'Title' })).toHaveValue('Canonical policy');
  });

  test('refresh/hard unload is guarded separately while dirty', async () => {
    stubBaseHandlers();
    server.use(http.get(`/api/v1/orgs/${SLUG}/agents`, () => HttpResponse.json({ agents: [manager] })));
    stubPolicy();
    const user = userEvent.setup();
    mountPolicyRoute([`/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy`]);
    const title = await screen.findByRole('textbox', { name: 'Title' });
    await user.type(title, ' dirty');
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
  });

  test('worker deep link fails closed without any policy request or policy wording', async () => {
    stubBaseHandlers();
    let policyRequests = 0;
    server.use(
      http.all(`/api/v1/orgs/${SLUG}/agents/:agentName/team-escalation-policy*`, () => { policyRequests += 1; return new HttpResponse(null, { status: 500 }); }),
    );
    mountPolicyRoute([`/orgs/${SLUG}/agents/support_agent/team-escalation-policy`]);
    expect(await screen.findByText(/Not found/)).toBeInTheDocument();
    expect(screen.queryByText(/team escalation policy/i)).not.toBeInTheDocument();
    expect(policyRequests).toBe(0);
  });

  test.each([
    ['worker', [{ ...manager, name: 'engineering_manager', role: 'worker' }]],
    ['other manager', [{ ...manager, name: 'engineering_manager', team: 'support' }]],
    ['unknown target', [manager]],
    ['stale or deleted manager', []],
  ])('%s direct link has no request binding, cache entry/data, policy DOM, or payload', async (kind, roster) => {
    stubBaseHandlers();
    const target = kind === 'unknown target' ? 'missing_manager' : 'engineering_manager';
    server.use(http.get(`/api/v1/orgs/${SLUG}/agents`, () => HttpResponse.json({ agents: roster })));
    let networkRequests = 0;
    server.use(http.all(`/api/v1/orgs/${SLUG}/agents/:agentName/team-escalation-policy*`, () => {
      networkRequests += 1;
      return new HttpResponse(null, { status: 500 });
    }));
    const { client } = mountPolicyRoute([`/orgs/${SLUG}/agents/${target}/team-escalation-policy`]);
    expect(await screen.findByText(/Not found/)).toBeInTheDocument();
    expect(networkRequests).toBe(0);
    expect(policyCacheEntries(client)).toEqual([]);
    expect(document.body).not.toHaveTextContent(/team escalation policy|canonical policy|shared local operator|save immutable|activate/i);
  });

  test('unresolved and errored rosters fail closed, create no policy cache, and recover only after eligible roster refresh', async () => {
    stubBaseHandlers();
    let resolveRoster!: (value: Response) => void;
    let rosterAttempt = 0;
    server.use(http.get(`/api/v1/orgs/${SLUG}/agents`, () => {
      rosterAttempt += 1;
      if (rosterAttempt === 1) return new Promise<Response>((resolve) => { resolveRoster = resolve; });
      if (rosterAttempt === 2) return new HttpResponse(null, { status: 503 });
      return HttpResponse.json({ agents: [manager] });
    }));
    stubPolicy();
    const first = mountPolicyRoute([`/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy`]);
    expect(await screen.findByText('Loading agent…')).toBeInTheDocument();
    expect(policyCacheEntries(first.client)).toEqual([]);
    resolveRoster(new Response(null, { status: 503 }));
    expect(await screen.findByText(/Not found/)).toBeInTheDocument();
    expect(policyCacheEntries(first.client)).toEqual([]);
    first.unmount();

    server.use(http.get(`/api/v1/orgs/${SLUG}/agents`, () => HttpResponse.json({ agents: [manager] })));
    const recovered = mountPolicyRoute([`/orgs/${SLUG}/agents/engineering_manager/team-escalation-policy`]);
    expect(await screen.findByRole('textbox', { name: 'Title' })).toBeInTheDocument();
    expect(policyCacheEntries(recovered.client).length).toBe(3);
  });
});

describe('AgentsPage — pending tab', () => {
  test('lists pending enrollments and approves one', async () => {
    let approveCalled = false;
    stubBaseHandlers();
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/agents/enrollments`, () =>
        HttpResponse.json({
          enrollments: [
            {
              name: 'new_writer',
              team: 'content',
              role: 'worker',
              executor: 'claude',
              description: 'Drafts long-form posts.',
              status: 'pending',
              enrolled_by: 'content_manager',
              created_at: '2026-05-18T19:00:00Z',
            },
          ],
        }),
      ),
      http.post(`/api/v1/orgs/${SLUG}/agents/new_writer/approve`, () => {
        approveCalled = true;
        return HttpResponse.json({ ok: true });
      }),
    );
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents?view=pending`);

    await waitFor(() =>
      expect(screen.getByText('new_writer')).toBeInTheDocument(),
    );
    expect(screen.getByText(/team: content/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^Approve$/ }));
    await waitFor(() => expect(approveCalled).toBe(true));
  });

  test('reject opens a dialog and posts a reason', async () => {
    let rejectBody: unknown = null;
    stubBaseHandlers();
    server.use(
      http.get(`/api/v1/orgs/${SLUG}/agents/enrollments`, () =>
        HttpResponse.json({
          enrollments: [
            {
              name: 'new_writer',
              team: 'content',
              role: 'worker',
              executor: 'claude',
              description: 'Drafts long-form posts.',
              status: 'pending',
              enrolled_by: 'content_manager',
              created_at: '2026-05-18T19:00:00Z',
            },
          ],
        }),
      ),
      http.post(
        `/api/v1/orgs/${SLUG}/agents/new_writer/reject`,
        async ({ request }) => {
          rejectBody = await request.json();
          return HttpResponse.json({ ok: true });
        },
      ),
    );
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents?view=pending`);

    await waitFor(() =>
      expect(screen.getByText('new_writer')).toBeInTheDocument(),
    );
    await user.click(screen.getByRole('button', { name: /^Reject$/ }));

    // Dialog rendered.
    const dialog = await screen.findByRole('dialog');
    await user.type(
      within(dialog).getByPlaceholderText(/Reason \(optional\)/),
      'duplicate of seo_agent',
    );
    await user.click(within(dialog).getByRole('button', { name: /^Reject$/ }));
    await waitFor(() =>
      expect(rejectBody).toEqual({ reason: 'duplicate of seo_agent' }),
    );
  });
});

describe('AgentDetailPane — recent jobs cross-link', () => {
  const JOB_FOR_AGENT: JobRecord = {
    id: 'JOB-0005',
    task_id: 'TASK-0010',
    agent_name: 'engineering_head',
    title: 'Run database vacuum',
    rationale: 'Reclaim disk space.',
    script_text: 'vacuumdb --all',
    interpreter: 'bash',
    cwd_hint: null,
    status: 'completed',
    exit_code: 0,
    stdout_head: null,
    stderr_head: null,
    stdout_path: null,
    stderr_path: null,
    duration_ms: 2000,
    started_at: '2026-05-20T08:00:00Z',
    finished_at: '2026-05-20T08:00:02Z',
    reviewed_at: null,
    reviewed_by: null,
    reject_reason: null,
    cwd_resolved: null,
    max_runtime_seconds: 300,
    max_output_bytes: 52428800,
    review_required: false,
    persistent: false,
    reason: null,
    created_at: '2026-05-20T07:59:00Z',
  };

  test('shows recent jobs in agent detail pane when data present', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/agents`, () => HttpResponse.json(AGENTS_PAYLOAD)),
      http.get(`/api/v1/orgs/${SLUG}/tasks`, () =>
        HttpResponse.json({ tasks: [] }),
      ),
      http.get(
        `/api/v1/orgs/${SLUG}/agents/engineering_head/memory/entries/`,
        () => HttpResponse.json({ entries: [] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () =>
        HttpResponse.json({ jobs: [JOB_FOR_AGENT] }),
      ),
    );

    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/agents/engineering_head`,
    });

    await waitFor(() =>
      expect(screen.getByText(/Recent jobs/i)).toBeInTheDocument(),
    );
    const link = screen.getByRole('link', { name: 'JOB-0005' });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute('href', `/orgs/${SLUG}/jobs/JOB-0005`);
    expect(screen.getByText(/Run database vacuum/)).toBeInTheDocument();
  });

  test('hides recent jobs section when agent has none', async () => {
    sessionStorage.setItem('happyranch.token', 'tok');
    server.use(
      http.get('/api/v1/orgs', () =>
        HttpResponse.json({ orgs: [{ slug: SLUG, root: '/x' }] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/agents`, () => HttpResponse.json(AGENTS_PAYLOAD)),
      http.get(`/api/v1/orgs/${SLUG}/tasks`, () =>
        HttpResponse.json({ tasks: [] }),
      ),
      http.get(
        `/api/v1/orgs/${SLUG}/agents/engineering_head/memory/entries/`,
        () => HttpResponse.json({ entries: [] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/jobs/`, () =>
        HttpResponse.json({ jobs: [] }),
      ),
    );

    renderWithProviders(<AppRoutes />, {
      route: `/orgs/${SLUG}/agents/engineering_head`,
    });

    await waitFor(() =>
      expect(screen.getByText('manager')).toBeInTheDocument(),
    );
    expect(screen.queryByText(/Recent jobs/i)).not.toBeInTheDocument();
  });
});

describe('AgentDetailPane — Start Thread Reflection affordance (THR-106)', () => {
  test('Reflection button appears in Start Thread dialog for a single-agent start', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });

    // Click the Start Thread button
    const startBtn = screen.getByRole('button', { name: /Start Thread/i });
    await user.click(startBtn);

    // Dialog opens with Reflection button
    const dialog = await screen.findByRole('dialog');
    const reflectionBtn = within(dialog).getByRole('button', { name: /^Reflection$/i });
    expect(reflectionBtn).toBeInTheDocument();
  });

  test('clicking Reflection posts canned compose body and navigates to new thread detail', async () => {
    stubBaseHandlers();
    stubDetailHandlers();
    let composeBody: unknown = null;
    server.use(
      http.post(`/api/v1/orgs/${SLUG}/threads`, async ({ request }) => {
        composeBody = await request.json();
        return HttpResponse.json(
          { thread_id: 'THR-REFL-1', started_at: 'now', pending_replies: 1 },
          { status: 201 },
        );
      }),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-REFL-1`, () =>
        HttpResponse.json({
          thread_id: 'THR-REFL-1',
          subject: 'Reflection - engineering_head',
          status: 'open',
          started_at: 'now',
          archived_at: null,
          forwarded_from_id: null,
          forwarded_from_kind: null,
          turn_cap: 500,
          turns_used: 0,
          summary: null,
          transcript_path: null,
          participants: ['engineering_head'],
          messages: [],
        }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-REFL-1/messages`, () =>
        HttpResponse.json({ messages: [] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-REFL-1/tail`, () =>
        HttpResponse.text('', { headers: { 'content-type': 'text/event-stream' } }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads`, () =>
        HttpResponse.json({ threads: [] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads/events`, () =>
        HttpResponse.text('', { headers: { 'content-type': 'text/event-stream' } }),
      ),
    );
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /Start Thread/i }));

    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /^Reflection$/i }));

    // Assert POST body has the canned reflection content
    await waitFor(() => {
      expect(composeBody).toEqual({
        subject: 'Reflection - engineering_head',
        recipients: ['engineering_head'],
        body_markdown:
          'Run self-reflection (hr:reflection) on your recent work and post your opening reflection report.',
      });
    });
    // Navigated to thread detail — header shows subject
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: /Reflection - engineering_head/i })).toBeInTheDocument(),
    );
  });

  test('Reflection double-click creates exactly one thread (synchronous in-flight guard)', async () => {
    stubBaseHandlers();
    stubDetailHandlers();

    let resolvePost: (v: unknown) => void;
    const postDeferred = new Promise((r) => { resolvePost = r; });
    let postCount = 0;
    let detailGetCount = 0;

    server.use(
      http.post(`/api/v1/orgs/${SLUG}/threads`, async () => {
        postCount++;
        await postDeferred;
        return HttpResponse.json(
          { thread_id: 'THR-REFL-DBL', started_at: 'now', pending_replies: 1 },
          { status: 201 },
        );
      }),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-REFL-DBL`, () => {
        detailGetCount++;
        return HttpResponse.json({
          thread_id: 'THR-REFL-DBL',
          subject: 'Reflection - engineering_head',
          status: 'open',
          started_at: 'now',
          archived_at: null,
          forwarded_from_id: null,
          forwarded_from_kind: null,
          turn_cap: 500,
          turns_used: 0,
          summary: null,
          transcript_path: null,
          participants: ['engineering_head'],
          messages: [],
        });
      }),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-REFL-DBL/messages`, () =>
        HttpResponse.json({ messages: [] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads/THR-REFL-DBL/tail`, () =>
        HttpResponse.text('', { headers: { 'content-type': 'text/event-stream' } }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads`, () =>
        HttpResponse.json({ threads: [] }),
      ),
      http.get(`/api/v1/orgs/${SLUG}/threads/events`, () =>
        HttpResponse.text('', { headers: { 'content-type': 'text/event-stream' } }),
      ),
    );
    const user = userEvent.setup();
    mountAt(`/orgs/${SLUG}/agents/engineering_head`);

    await waitFor(() => {
      expect(screen.getByText('manager')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /Start Thread/i }));

    const dialog = await screen.findByRole('dialog');
    const reflectionBtn = within(dialog).getByRole('button', { name: /^Reflection$/i });

    // fireEvent.click is synchronous — two clicks in the same tick.
    fireEvent.click(reflectionBtn);
    fireEvent.click(reflectionBtn);

    // Release the deferred POST.
    resolvePost!({});

    // Assert exactly one POST reached the server.
    await waitFor(() => {
      expect(postCount).toBe(1);
    });
    await waitFor(() => {
      expect(detailGetCount).toBe(1);
    });
    // Navigated once to thread detail.
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: /Reflection - engineering_head/i })).toBeInTheDocument(),
    );
  });
});
