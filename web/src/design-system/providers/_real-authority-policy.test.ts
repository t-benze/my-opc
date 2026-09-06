import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-router-dom', () => ({ useParams: () => ({ slug: 'alpha' }) }));
vi.mock('@/lib/api/authorityPolicy', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/authorityPolicy')>(
    '@/lib/api/authorityPolicy',
  );
  return { ...actual, getTeamEscalationPolicy: vi.fn(),
    createTeamEscalationPolicyRelease: vi.fn(), activateTeamEscalationPolicyRelease: vi.fn(),
    getTeamEscalationPolicyHistory: vi.fn(), getTeamEscalationPolicyOutcomes: vi.fn() };
});

import * as api from '@/lib/api/authorityPolicy';
import { realAuthorityPolicyApi } from './_real-authority-policy';

const manager = { name: 'engineering_manager', team: 'engineering', role: 'manager' };
const bootstrapTemplate = {
  title: 'Policy', normative_text: 'text', clauses: [],
  continuation_phrase: 'routine same-root follow-through of the already-completed slice',
};
const empty = {
  team: 'engineering' as const,
  target_manager: 'engineering_manager' as const,
  can_mutate: true as const,
  bootstrap_required: true as const,
  bootstrap_template: bootstrapTemplate,
};
const active = {
  ...empty,
  bootstrap_required: undefined,
  active: {
    activation_id: 'APA-1', epoch: 1, action: 'bootstrap' as const,
    created_at: '2026-09-02T00:00:00Z',
    actor_attribution: 'shared local operator credential' as const,
    release: {
      id: 'APR-1', policy_id: 'engineering/pre-escalation-authority', version: 1,
      title: 'Policy', normative_text: 'text', clauses: [],
      continuation_phrase: 'routine same-root follow-through of the already-completed slice',
      digest: 'digest', created_at: '2026-09-02T00:00:00Z',
      actor_attribution: 'shared local operator credential' as const,
    },
  },
};

function setup(agent = manager) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children);
  return { client, hook: renderHook(() => realAuthorityPolicyApi.useTeamEscalationPolicy(agent), { wrapper }) };
}

function setupHistory() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children);
  return renderHook(() => realAuthorityPolicyApi.useTeamEscalationPolicyHistory(manager), { wrapper });
}

function setupOutcomes() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children);
  return renderHook(() => realAuthorityPolicyApi.useTeamEscalationPolicyOutcomes(manager), { wrapper });
}

function setupMutations() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children);
  const hook = renderHook(() => ({
    create: realAuthorityPolicyApi.useCreateTeamEscalationPolicyRelease(),
    activate: realAuthorityPolicyApi.useActivateTeamEscalationPolicyRelease(),
  }), { wrapper });
  return { client, hook };
}

function setupHistoryWithMutations() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children);
  const hook = renderHook(() => ({
    history: realAuthorityPolicyApi.useTeamEscalationPolicyHistory(manager),
    outcomes: realAuthorityPolicyApi.useTeamEscalationPolicyOutcomes(manager),
    create: realAuthorityPolicyApi.useCreateTeamEscalationPolicyRelease(),
    activate: realAuthorityPolicyApi.useActivateTeamEscalationPolicyRelease(),
  }), { wrapper });
  return { client, hook };
}

beforeEach(() => vi.clearAllMocks());

describe('team escalation policy query gate', () => {
  it.each([
    ['create', 'APR-2'],
    ['activate', 'APR-1'],
  ] as const)('refreshes exact policy and history caches after successful %s', async (kind, releaseId) => {
    vi.mocked(api.createTeamEscalationPolicyRelease).mockResolvedValue({ release: { id: releaseId } } as never);
    vi.mocked(api.activateTeamEscalationPolicyRelease).mockResolvedValue({ activation: { id: 'APA-2' } } as never);
    const { client, hook } = setupMutations();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    const variables = kind === 'create'
      ? { agentName: 'engineering_manager', body: {} as never }
      : { agentName: 'engineering_manager', body: {} as never };

    await hook.result.current[kind].mutateAsync(variables);

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['team-escalation-policy', 'alpha', 'engineering_manager'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['team-escalation-policy-history', 'alpha', 'engineering_manager'] });
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ['team-escalation-policy-outcomes', 'alpha', 'engineering_manager'] });
  });

  it('does not invalidate loaded caches when a mutation fails', async () => {
    vi.mocked(api.createTeamEscalationPolicyRelease).mockRejectedValue(new Error('save failed'));
    const { client, hook } = setupMutations();
    client.setQueryData(['team-escalation-policy-history', 'alpha', 'engineering_manager'], { pages: ['loaded'] });
    const invalidate = vi.spyOn(client, 'invalidateQueries');

    await expect(hook.result.current.create.mutateAsync({ agentName: 'engineering_manager', body: {} as never })).rejects.toThrow('save failed');

    expect(invalidate).not.toHaveBeenCalled();
    expect(client.getQueryData(['team-escalation-policy-history', 'alpha', 'engineering_manager'])).toEqual({ pages: ['loaded'] });
  });

  it.each([
    ['create', 'APR-created', {}],
    ['activate', 'APR-activated', { action: 'activate' }],
    ['activate', 'APR-rollback', { action: 'reactivate_rollback' }],
  ] as const)('visibly refreshes history after successful %s mutation for %s', async (kind, releaseId, body) => {
    vi.mocked(api.getTeamEscalationPolicyHistory)
      .mockResolvedValueOnce({ items: [{ release_id: 'APR-old' }] as never, next_cursor: null })
      .mockResolvedValueOnce({ items: [{ release_id: releaseId }, { release_id: 'APR-old' }] as never, next_cursor: null });
    vi.mocked(api.getTeamEscalationPolicyOutcomes)
      .mockResolvedValue({ items: [{ candidate_id: 'AUTH-stable' }] as never, next_cursor: null });
    vi.mocked(api.createTeamEscalationPolicyRelease).mockResolvedValue({ release: { id: releaseId } } as never);
    vi.mocked(api.activateTeamEscalationPolicyRelease).mockResolvedValue({ activation: { id: 'APA-new' } } as never);
    const { hook } = setupHistoryWithMutations();
    await waitFor(() => expect(hook.result.current.history.data?.pages[0].items[0].release_id).toBe('APR-old'));
    await waitFor(() => expect(hook.result.current.outcomes.data?.pages).toHaveLength(1));

    await hook.result.current[kind].mutateAsync({ agentName: 'engineering_manager', body: body as never });

    await waitFor(() => expect(hook.result.current.history.data?.pages[0].items.map((row) => row.release_id)).toEqual([releaseId, 'APR-old']));
    expect(api.getTeamEscalationPolicyHistory).toHaveBeenCalledTimes(2);
    expect(api.getTeamEscalationPolicyOutcomes).toHaveBeenCalledTimes(1);
    expect(hook.result.current.outcomes.data?.pages[0].items.map((row) => row.candidate_id)).toEqual(['AUTH-stable']);
  });

  it.each([
    { name: 'dev_agent', team: 'engineering', role: 'worker' },
    { name: 'content_manager', team: 'content', role: 'manager' },
    { name: 'guessed', team: 'engineering', role: 'manager' },
  ])('creates no request or cache entry for $name', (agent) => {
    const { client, hook } = setup(agent);
    expect(hook.result.current.isLoading).toBe(false);
    expect(api.getTeamEscalationPolicy).not.toHaveBeenCalled();
    expect(client.getQueryCache().getAll()).toHaveLength(0);
  });

  it('exposes loading then active release-creation state for the eligible tuple', async () => {
    let resolve!: (value: typeof active) => void;
    vi.mocked(api.getTeamEscalationPolicy).mockReturnValue(
      new Promise((done) => { resolve = done; }),
    );
    const { hook } = setup();
    expect(hook.result.current.isLoading).toBe(true);
    resolve(active);
    await waitFor(() => expect(hook.result.current.data).toEqual(active));
    expect(hook.result.current.data?.can_mutate).toBe(true);
  });

  it('exposes the empty release-creation state for the eligible tuple', async () => {
    vi.mocked(api.getTeamEscalationPolicy).mockResolvedValue(empty);
    const { hook } = setup();
    await waitFor(() => expect(hook.result.current.data).toEqual(empty));
    expect(hook.result.current.data?.can_mutate).toBe(true);
  });

  it('exposes sanitized query errors', async () => {
    vi.mocked(api.getTeamEscalationPolicy).mockRejectedValue(new Error('unavailable'));
    const { hook } = setup();
    await waitFor(() => expect(hook.result.current.isError).toBe(true));
    expect(hook.result.current.error).toBeInstanceOf(Error);
  });

  it('uses the server cursor to reach page two exactly once', async () => {
    vi.mocked(api.getTeamEscalationPolicyHistory)
      .mockResolvedValueOnce({ items: [{ release_id: 'APR-2' }] as never, next_cursor: 'history-cursor' })
      .mockResolvedValueOnce({ items: [{ release_id: 'APR-1' }] as never, next_cursor: null });
    const hook = setupHistory();
    await waitFor(() => expect(hook.result.current.data?.pages).toHaveLength(1));
    await hook.result.current.fetchNextPage();
    await waitFor(() => expect(hook.result.current.data?.pages).toHaveLength(2));
    expect(api.getTeamEscalationPolicyHistory).toHaveBeenNthCalledWith(1, 'alpha', 'engineering_manager', undefined);
    expect(api.getTeamEscalationPolicyHistory).toHaveBeenNthCalledWith(2, 'alpha', 'engineering_manager', 'history-cursor');
    expect(hook.result.current.data?.pages.flatMap((page) => page.items).map((row) => row.release_id)).toEqual(['APR-2', 'APR-1']);
    expect(hook.result.current.hasNextPage).toBe(false);
  });

  it('preserves history page one across cursor failure and native retry appends page two once', async () => {
    vi.mocked(api.getTeamEscalationPolicyHistory)
      .mockResolvedValueOnce({ items: [{ release_id: 'APR-2' }] as never, next_cursor: 'history-cursor' })
      .mockRejectedValueOnce(new Error('page two unavailable'))
      .mockResolvedValueOnce({ items: [{ release_id: 'APR-1' }] as never, next_cursor: null });
    const hook = setupHistory();
    await waitFor(() => expect(hook.result.current.data?.pages).toHaveLength(1));
    await hook.result.current.fetchNextPage();
    await waitFor(() => expect(hook.result.current.isError).toBe(true));
    expect(hook.result.current.data?.pages.flatMap((page) => page.items).map((row) => row.release_id)).toEqual(['APR-2']);
    await hook.result.current.fetchNextPage();
    await waitFor(() => expect(hook.result.current.data?.pages).toHaveLength(2));
    expect(hook.result.current.data?.pages.flatMap((page) => page.items).map((row) => row.release_id)).toEqual(['APR-2', 'APR-1']);
    expect(api.getTeamEscalationPolicyHistory).toHaveBeenNthCalledWith(2, 'alpha', 'engineering_manager', 'history-cursor');
    expect(api.getTeamEscalationPolicyHistory).toHaveBeenNthCalledWith(3, 'alpha', 'engineering_manager', 'history-cursor');
    expect(hook.result.current.hasNextPage).toBe(false);
  });

  it('preserves outcomes page one across cursor failure and native retry appends page two once', async () => {
    vi.mocked(api.getTeamEscalationPolicyOutcomes)
      .mockResolvedValueOnce({ items: [{ candidate_id: 'AUTH-2' }] as never, next_cursor: 'outcome-cursor' })
      .mockRejectedValueOnce(new Error('page two unavailable'))
      .mockResolvedValueOnce({ items: [{ candidate_id: 'AUTH-1' }] as never, next_cursor: null });
    const hook = setupOutcomes();
    await waitFor(() => expect(hook.result.current.data?.pages).toHaveLength(1));
    await hook.result.current.fetchNextPage();
    await waitFor(() => expect(hook.result.current.isError).toBe(true));
    expect(hook.result.current.data?.pages.flatMap((page) => page.items).map((row) => row.candidate_id)).toEqual(['AUTH-2']);
    await hook.result.current.fetchNextPage();
    await waitFor(() => expect(hook.result.current.data?.pages).toHaveLength(2));
    expect(hook.result.current.data?.pages.flatMap((page) => page.items).map((row) => row.candidate_id)).toEqual(['AUTH-2', 'AUTH-1']);
    expect(api.getTeamEscalationPolicyOutcomes).toHaveBeenNthCalledWith(2, 'alpha', 'engineering_manager', 'outcome-cursor');
    expect(api.getTeamEscalationPolicyOutcomes).toHaveBeenNthCalledWith(3, 'alpha', 'engineering_manager', 'outcome-cursor');
    expect(hook.result.current.hasNextPage).toBe(false);
  });
});
