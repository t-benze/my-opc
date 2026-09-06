import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { TeamEscalationPolicyCard } from './TeamEscalationPolicyCard';

const template = {
  title: 'Canonical policy',
  normative_text: 'Normative text',
  clauses: [{ id: 'esc-one', category: 'protected', condition: 'Stop.', action: 'escalate_to_founder' as const }],
  continuation_phrase: 'routine same-root follow-through of the already-completed slice',
};
const empty = {
  team: 'engineering' as const, target_manager: 'engineering_manager' as const,
  can_mutate: true as const, bootstrap_required: true as const,
  bootstrap_template: template,
};
const active = {
  ...empty,
  bootstrap_required: undefined,
  active: {
    activation_id: 'APA-active', epoch: 7, action: 'activate' as const,
    created_at: '2026-09-02T00:00:00Z', actor_attribution: 'shared local operator credential' as const,
    release: {
      id: 'APR-active', policy_id: 'APP-engineering', version: 3,
      ...template, digest: '1234567890abcdef', created_at: '2026-09-02T00:00:00Z',
      actor_attribution: 'shared local operator credential' as const,
    },
  },
};
const query = { data: empty as typeof empty | typeof active | undefined, isLoading: false, isError: false, error: null, refetch: vi.fn() };
const create = { mutateAsync: vi.fn(), isPending: false };
const activate = { mutateAsync: vi.fn(), isPending: false };
const history = { data: { pages: [{ items: [] as Array<Record<string, unknown>>, next_cursor: null as string | null }] }, isLoading: false, isError: false, error: null, fetchNextPage: vi.fn(), hasNextPage: false, isFetchingNextPage: false };
const outcomes = { data: { pages: [{ items: [] as Array<Record<string, unknown>>, next_cursor: null as string | null }] }, isLoading: false, isError: false, error: null, fetchNextPage: vi.fn(), hasNextPage: false, isFetchingNextPage: false };

vi.mock('@/hooks/authorityPolicy', () => ({
  useTeamEscalationPolicy: () => query,
  useCreateTeamEscalationPolicyRelease: () => create,
  useActivateTeamEscalationPolicyRelease: () => activate,
  useTeamEscalationPolicyHistory: () => history,
  useTeamEscalationPolicyOutcomes: () => outcomes,
}));

const agent = { name: 'engineering_manager', team: 'engineering', role: 'manager' };

describe('TeamEscalationPolicyCard', () => {
  beforeEach(() => {
    query.data = empty; query.isLoading = false; query.isError = false; query.error = null;
    query.refetch.mockReset();
    create.mutateAsync.mockReset(); activate.mutateAsync.mockReset();
    history.data.pages = [{ items: [], next_cursor: null }]; history.isLoading = false; history.isError = false; history.hasNextPage = false; history.isFetchingNextPage = false; history.fetchNextPage.mockReset();
    outcomes.data.pages = [{ items: [], next_cursor: null }]; outcomes.isLoading = false; outcomes.isError = false; outcomes.hasNextPage = false; outcomes.isFetchingNextPage = false; outcomes.fetchNextPage.mockReset();
  });

  it('retries a load error and recovers through loading to loaded data on the same mount', async () => {
    query.data = undefined; query.isError = true;
    const view = render(<TeamEscalationPolicyCard agent={agent} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load');
    query.refetch.mockImplementationOnce(() => {
      query.isError = false; query.isLoading = true;
      view.rerender(<TeamEscalationPolicyCard agent={agent} />);
      return Promise.resolve();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(query.refetch).toHaveBeenCalledOnce();
    expect(screen.getByText('Loading team policy…')).toBeInTheDocument();
    query.isLoading = false; query.data = empty;
    view.rerender(<TeamEscalationPolicyCard agent={agent} />);
    expect(await screen.findByText('Team-owned')).toBeInTheDocument();
  });

  it('labels team ownership, bootstrap and shared-credential limitation', async () => {
    render(<TeamEscalationPolicyCard agent={agent} />);
    expect(await screen.findByText('Team-owned')).toBeInTheDocument();
    expect(screen.getByText(/not by this agent/i)).toBeInTheDocument();
    expect(screen.getByText(/shared local operator credential/i)).toBeInTheDocument();
    expect(screen.getByText(/No active release/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save & activate' })).toBeDisabled();
  });

  it('preserves dirty draft on conflict and saves an immutable inactive version', async () => {
    create.mutateAsync.mockRejectedValueOnce(new ApiError(409, 'base_release_changed', {}));
    render(<TeamEscalationPolicyCard agent={agent} />);
    const title = await screen.findByLabelText('Title');
    fireEvent.change(title, { target: { value: 'Edited policy' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save immutable version' }));
    await waitFor(() => expect(screen.getByText(/active base changed/i)).toBeInTheDocument());
    expect(title).toHaveValue('Edited policy');

    create.mutateAsync.mockResolvedValueOnce({
      release: { id: 'APR-new', version: 2 }, activated: false,
      validation: { canonical: true, digest: 'digest' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save immutable version' }));
    await waitFor(() => expect(screen.getByText(/Saved inactive v2/)).toBeInTheDocument());
    expect(activate.mutateAsync).not.toHaveBeenCalled();
  });

  it('rejects blank editable policy text before any request', async () => {
    render(<TeamEscalationPolicyCard agent={agent} />);
    fireEvent.change(await screen.findByLabelText('Normative policy'), { target: { value: ' ' } });
    expect(screen.getByRole('status')).toHaveTextContent('Title and normative policy are required.');
    expect(screen.getByRole('button', { name: 'Save immutable version' })).toBeDisabled();
    expect(create.mutateAsync).not.toHaveBeenCalled();
  });

  it('renders an active release and enables actions only for a dirty valid draft', async () => {
    query.data = active;
    render(<TeamEscalationPolicyCard agent={agent} />);
    expect(await screen.findByText(/Active v3 · epoch 7 · 1234567890ab/)).toBeInTheDocument();
    const save = screen.getByRole('button', { name: 'Save immutable version' });
    const saveAndActivate = screen.getByRole('button', { name: 'Save & activate' });
    expect(save).toBeDisabled(); expect(saveAndActivate).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Edited policy' } });
    expect(save).toBeEnabled(); expect(saveAndActivate).toBeEnabled();
    expect(screen.getByText('No immutable releases yet.')).toBeInTheDocument();
  });

  it('renders immutable history, receipt-incomplete outcomes, and confirms rollback', async () => {
    query.data = active;
    history.data.pages[0].items = [{ release_id: 'APR-old', policy_id: 'p', version: 2,
      policy_digest: 'abcdef1234567890', release_created_at: '2026-09-01',
      actor_attribution: 'shared local operator credential', activation: {
        id: 'APA-old', epoch: 2, action: 'activate', digest: 'd', created_at: '2026-09-01',
      } }];
    outcomes.data.pages[0].items = [{ candidate_id: 'AUTH-1', disposition: null,
      disposition_code: null, root_task_id: 'TASK-1', manager_session_id: 'sess-1',
      release_id: null, receipt_state: 'receipt_incomplete' }];
    activate.mutateAsync.mockResolvedValueOnce({});
    render(<TeamEscalationPolicyCard agent={agent} />);
    expect(await screen.findByText(/v2 · APR-old/)).toBeInTheDocument();
    expect(screen.getByText('receipt_incomplete')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Reactivate v2' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('Reactivate immutable version 2');
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(activate.mutateAsync).toHaveBeenCalledWith(expect.objectContaining({
      body: expect.objectContaining({ release_id: 'APR-old', expected_previous_epoch: 7,
        action: 'reactivate_rollback' }),
    })));
  });

  it('loads independent second pages without duplicates or loss', async () => {
    query.data = active;
    history.data.pages = [{ items: [{ release_id: 'APR-2', policy_id: 'p', version: 2, policy_digest: '2'.repeat(64), release_created_at: 'x', actor_attribution: 'shared local operator credential', activation: null }], next_cursor: 'history-cursor' }];
    outcomes.data.pages = [{ items: [{ candidate_id: 'AUTH-2', disposition: 'continue_same_root', disposition_code: 'continue_same_root', root_task_id: 'TASK-2', manager_session_id: 'sess-2', release_id: 'APR-2', receipt_state: 'complete' }], next_cursor: 'outcome-cursor' }];
    history.hasNextPage = true; outcomes.hasNextPage = true;
    const view = render(<TeamEscalationPolicyCard agent={agent} />);
    fireEvent.click(screen.getByRole('button', { name: 'Load more policy history' }));
    expect(history.fetchNextPage).toHaveBeenCalledOnce();
    history.data.pages.push({ items: [{ release_id: 'APR-1', policy_id: 'p', version: 1, policy_digest: '1'.repeat(64), release_created_at: 'x', actor_attribution: 'shared local operator credential', activation: null }], next_cursor: null });
    history.hasNextPage = false;
    view.rerender(<TeamEscalationPolicyCard agent={agent} />);
    expect(screen.getByText(/v2 · APR-2/)).toBeInTheDocument();
    expect(screen.getByText(/v1 · APR-1/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Load more evaluation outcomes' }));
    expect(outcomes.fetchNextPage).toHaveBeenCalledOnce();
    expect(screen.getByRole('button', { name: 'Load more policy history' })).toBeDisabled();
  });

  it('preserves each stream and retries its failed cursor independently', async () => {
    query.data = active;
    history.data.pages = [{ items: [{ release_id: 'APR-2', policy_id: 'p', version: 2, policy_digest: '2'.repeat(64), release_created_at: 'x', actor_attribution: 'shared local operator credential', activation: null }], next_cursor: 'history-cursor' }];
    outcomes.data.pages = [{ items: [{ candidate_id: 'AUTH-2', disposition: 'continue_same_root', disposition_code: 'continue_same_root', root_task_id: 'TASK-2', manager_session_id: 'sess-2', release_id: 'APR-2', receipt_state: 'complete' }], next_cursor: 'outcome-cursor' }];
    history.hasNextPage = true; outcomes.hasNextPage = true;
    history.isError = true; outcomes.isError = true;
    render(<TeamEscalationPolicyCard agent={agent} />);
    expect(screen.getByText(/v2 · APR-2/)).toBeInTheDocument();
    expect(screen.getByText(/task TASK-2/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading policy history' }));
    expect(history.fetchNextPage).toHaveBeenCalledOnce();
    expect(outcomes.fetchNextPage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading evaluation outcomes' }));
    expect(outcomes.fetchNextPage).toHaveBeenCalledOnce();
  });

  it.each([
    [new ApiError(422, 'invalid_policy', {}), /server rejected this policy contract/i],
    [new ApiError(500, 'internal_error', {}), /policy could not be saved/i],
  ])('shows the bounded save failure without a receipt', async (error, copy) => {
    create.mutateAsync.mockRejectedValueOnce(error);
    render(<TeamEscalationPolicyCard agent={agent} />);
    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'Edited policy' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save immutable version' }));
    expect(await screen.findByText(copy)).toBeInTheDocument();
    expect(screen.queryByText(/Saved inactive v/)).not.toBeInTheDocument();
  });

  it('confirms activation and preserves the exact release/CAS payload', async () => {
    query.data = active;
    create.mutateAsync.mockResolvedValueOnce({
      release: { id: 'APR-new', version: 4 }, activated: false,
      validation: { canonical: true, digest: 'digest' },
    });
    activate.mutateAsync.mockResolvedValueOnce({});
    render(<TeamEscalationPolicyCard agent={agent} />);
    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'Edited policy' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save & activate' }));
    expect(screen.getByRole('dialog', { name: 'activate policy confirmation' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(activate.mutateAsync).toHaveBeenCalledWith({
      agentName: 'engineering_manager',
      body: {
        release_id: 'APR-new', expected_previous_epoch: 7, request_id: expect.any(String),
        action: 'activate', acknowledge_shared_credential_attribution: true,
      },
    }));
    expect(create.mutateAsync).toHaveBeenCalledWith(expect.objectContaining({
      body: expect.objectContaining({ based_on_release_id: 'APR-active' }),
    }));
  });

  it('retains the durable inactive receipt when post-create activation fails', async () => {
    query.data = active;
    create.mutateAsync.mockResolvedValueOnce({
      release: { id: 'APR-durable', version: 4 }, activated: false,
      validation: { canonical: true, digest: 'digest' },
    });
    activate.mutateAsync.mockRejectedValueOnce(new ApiError(503, 'activation_unavailable', {}));
    render(<TeamEscalationPolicyCard agent={agent} />);
    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'Edited policy' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save & activate' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(await screen.findByText('Saved inactive v4 · APR-durable')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(/was saved inactive, but activation failed/i);
    expect(screen.getByRole('status')).toHaveTextContent(/Retry activation from this saved version/i);
    expect(screen.getByRole('status')).not.toHaveTextContent(/could not be saved/i);
  });
});
