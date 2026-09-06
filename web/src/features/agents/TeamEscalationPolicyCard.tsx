import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { AlertCircle } from 'lucide-react';
import { Button } from '@/design-system/primitives/Button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/design-system/primitives/Dialog';
import { ApiError } from '@/lib/api';
import {
  type AuthorityPolicyTemplate,
  useActivateTeamEscalationPolicyRelease,
  useCreateTeamEscalationPolicyRelease,
  useTeamEscalationPolicy,
  useTeamEscalationPolicyHistory,
  useTeamEscalationPolicyOutcomes,
} from '@/hooks/authorityPolicy';
import { useAgentsRoutes } from '@/hooks/agents';

export function TeamEscalationPolicyEntryCard({ agent }: { agent: { name: string; team: string; role: string } }): JSX.Element {
  const query = useTeamEscalationPolicy(agent);
  const routes = useAgentsRoutes();
  return (
    <PolicyShell>
      <h3 className="font-display text-text-primary text-base font-medium">Manager-only team escalation policy</h3>
      <p className="text-text-muted mt-1 text-xs">Engineering Manager access only.</p>
      {query.isLoading ? <p className="text-text-muted mt-3 text-xs">Loading active policy status…</p> : query.isError || !query.data ? <p role="alert" className="text-tier-red mt-3 text-xs">Could not load policy status.</p> : query.data.active ? <p className="text-text-muted mt-3 text-xs">Active v{query.data.active.release.version} · epoch {query.data.active.epoch} · {query.data.active.release.digest.slice(0, 12)}</p> : <p className="text-text-muted mt-3 text-xs">No active release. Canonical bootstrap is available.</p>}
      <Button asChild size="sm" className="mt-3"><Link to={routes.policy(agent.name)}>Open team escalation policy</Link></Button>
    </PolicyShell>
  );
}

export function TeamEscalationPolicyCard({
  agent,
  onDirtyChange,
}: {
  agent: { name: string; team: string; role: string };
  onDirtyChange?: (dirty: boolean) => void;
}): JSX.Element {
  const query = useTeamEscalationPolicy(agent);
  const createRelease = useCreateTeamEscalationPolicyRelease();
  const activateRelease = useActivateTeamEscalationPolicyRelease();
  const history = useTeamEscalationPolicyHistory(agent);
  const outcomes = useTeamEscalationPolicyOutcomes(agent);
  const [draft, setDraft] = useState<AuthorityPolicyTemplate | null>(null);
  const [baseline, setBaseline] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<'activate' | { releaseId: string; version: number } | null>(null);
  const [savedInactive, setSavedInactive] = useState<{ id: string; version: number } | null>(null);

  const source = useMemo(() => query.data?.active?.release ?? query.data?.bootstrap_template, [query.data]);
  useEffect(() => {
    if (!source) return;
    const next = {
      title: source.title,
      normative_text: source.normative_text,
      clauses: source.clauses.map((clause) => ({ ...clause })),
      continuation_phrase: source.continuation_phrase,
    };
    setDraft(next);
    setBaseline(JSON.stringify(next));
    setMessage(null);
    setConfirm(null);
    setSavedInactive(null);
  }, [source]);

  const dirty = draft ? JSON.stringify(draft) !== baseline : false;
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  if (query.isLoading) return <PolicyShell><p className="text-text-muted text-sm">Loading team policy…</p></PolicyShell>;
  if (query.isError || !query.data || !draft) {
    return <PolicyShell><div role="alert" className="text-tier-red flex flex-wrap items-center gap-2 text-sm"><AlertCircle size={14} /><span>Could not load the team policy.</span><Button size="sm" variant="ghost" onClick={() => void query.refetch()}>Retry</Button></div></PolicyShell>;
  }

  const expected = query.data.bootstrap_template;
  const validationError = validateDraft(draft, expected);
  const active = query.data.active;
  const historyItems = history.data?.pages.flatMap((page) => page.items) ?? [];
  const outcomeItems = outcomes.data?.pages.flatMap((page) => page.items) ?? [];

  const save = async (andActivate: boolean) => {
    if (validationError) { setMessage(validationError); return; }
    setMessage(null);
    try {
      const saved = await createRelease.mutateAsync({
        agentName: agent.name,
        body: {
          ...draft,
          based_on_release_id: active?.release.id ?? null,
          request_id: crypto.randomUUID(),
        },
      });
      setBaseline(JSON.stringify(draft));
      setSavedInactive({ id: saved.release.id, version: saved.release.version });
      setMessage(`Immutable version ${saved.release.version} saved inactive.`);
      if (andActivate) {
        try {
          await activateRelease.mutateAsync({
            agentName: agent.name,
            body: {
              release_id: saved.release.id,
              expected_previous_epoch: active?.epoch ?? 0,
              request_id: crypto.randomUUID(),
              action: 'activate',
              acknowledge_shared_credential_attribution: true,
            },
          });
        } catch (error) {
          const api = error instanceof ApiError ? error : null;
          const detail = api?.message ? ` Server response: ${api.message}` : '';
          setMessage(`Immutable version ${saved.release.version} was saved inactive, but activation failed.${detail} Retry activation from this saved version after resolving the error.`);
        }
      }
    } catch (error) {
      const api = error instanceof ApiError ? error : null;
      if (api?.code === 'base_release_changed' || api?.status === 409) {
        setMessage('The active base changed. Your draft was preserved; reload before saving again.');
      } else if (api?.status === 422) {
        setMessage('The server rejected this policy contract. Review the highlighted canonical fields.');
      } else {
        setMessage('The policy could not be saved. Your draft was preserved; try again.');
      }
    }
  };

  const rollback = async (target: { releaseId: string; version: number }) => {
    setConfirm(null); setMessage(null);
    try {
      await activateRelease.mutateAsync({ agentName: agent.name, body: {
        release_id: target.releaseId, expected_previous_epoch: active?.epoch ?? 0,
        request_id: crypto.randomUUID(), action: 'reactivate_rollback',
        acknowledge_shared_credential_attribution: true,
      } });
      setMessage(`Older immutable version ${target.version} reactivated as a new epoch.`);
    } catch (error) {
      const api = error instanceof ApiError ? error : null;
      setMessage(api?.status === 409
        ? 'Rollback conflicted with a newer activation. Reload history and try again.'
        : 'Rollback failed without changing the active policy.');
    }
  };

  return (
    <PolicyShell>
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-text-primary text-base font-medium">Team escalation policy</h3>
          <p className="text-text-muted mt-1 text-xs">Owned by the Engineering team, not by this agent.</p>
        </div>
        <span className="bg-accent-soft text-accent-text rounded-full px-2 py-1 text-xs">Team-owned</span>
      </div>
      <div className="bg-tier-amber-soft text-text-secondary mt-3 rounded-md p-3 text-xs">
        Changes are attributed only to the <strong>shared local operator credential</strong>. Individual operator identity is not available.
      </div>
      {active ? <p className="text-text-muted mt-3 text-xs">Active v{active.release.version} · epoch {active.epoch} · {active.release.digest.slice(0, 12)}</p> : <p className="text-text-muted mt-3 text-xs">No active release. Start from the canonical server bootstrap template.</p>}
      {savedInactive && <p className="text-accent-text mt-1 text-xs">Saved inactive v{savedInactive.version} · {savedInactive.id}</p>}
      <label className="text-text-secondary mt-4 block text-xs font-medium">Title
        <input className="border-border-subtle bg-surface mt-1 w-full rounded-md border px-3 py-2 text-sm" value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
      </label>
      <label className="text-text-secondary mt-3 block text-xs font-medium">Normative policy
        <textarea className="border-border-subtle bg-surface mt-1 min-h-40 w-full rounded-md border px-3 py-2 font-mono text-xs" value={draft.normative_text} onChange={(e) => setDraft({ ...draft, normative_text: e.target.value })} />
      </label>
      <div className="mt-3 space-y-2">
        {draft.clauses.map((clause, index) => (
          <div key={clause.id} className="border-border-subtle bg-surface-sunken rounded-md border p-3">
            <div className="text-text-muted flex flex-wrap gap-2 font-mono text-xs"><span>{clause.id}</span><span>· {clause.category}</span><span>· {clause.action}</span></div>
            <textarea aria-label={`${clause.id} condition`} className="border-border-subtle bg-surface mt-2 min-h-20 w-full rounded border px-2 py-1 text-xs" value={clause.condition} onChange={(e) => setDraft({ ...draft, clauses: draft.clauses.map((item, i) => i === index ? { ...item, condition: e.target.value } : item) })} />
          </div>
        ))}
      </div>
      <label className="text-text-secondary mt-3 block text-xs font-medium">Canonical continuation phrase
        <input readOnly className="border-border-subtle bg-surface-sunken mt-1 w-full rounded-md border px-3 py-2 font-mono text-xs" value={draft.continuation_phrase} />
      </label>
      {(validationError || message) && <p role="status" className={`mt-3 text-xs ${validationError ? 'text-tier-red' : 'text-text-secondary'}`}>{validationError ?? message}</p>}
      <Dialog open={confirm !== null} onOpenChange={(open) => { if (!open) setConfirm(null); }}>
        <DialogContent aria-label="activate policy confirmation">
          <DialogHeader>
            <DialogTitle>Confirm policy activation</DialogTitle>
            <DialogDescription>{confirm === 'activate' ? 'Save a new immutable version and activate it?' : confirm ? `Reactivate immutable version ${confirm.version} as a new epoch?` : ''}</DialogDescription>
          </DialogHeader>
          <DialogFooter><Button size="sm" onClick={() => { if (confirm === 'activate') { setConfirm(null); void save(true); } else if (confirm) { void rollback(confirm); } }}>Confirm</Button><Button size="sm" variant="ghost" onClick={() => setConfirm(null)}>Cancel</Button></DialogFooter>
        </DialogContent>
      </Dialog>
      <div className="mt-4 flex flex-wrap gap-2">
        <Button size="sm" disabled={!dirty || !!validationError || createRelease.isPending} onClick={() => void save(false)}>Save immutable version</Button>
        <Button size="sm" disabled={!dirty || !!validationError || createRelease.isPending} onClick={() => setConfirm('activate')}>Save &amp; activate</Button>
        {message?.includes('base changed') && <Button size="sm" variant="ghost" onClick={() => window.location.reload()}>Reload base</Button>}
      </div>
      <section aria-labelledby="policy-history-heading" className="border-border-subtle mt-5 border-t pt-4">
        <h4 id="policy-history-heading" className="text-text-primary text-sm font-medium">Immutable release history</h4>
        {history.isLoading ? <p className="text-text-muted mt-2 text-xs">Loading history…</p> : history.isError && !historyItems.length ? <p role="alert" className="text-tier-red mt-2 text-xs">Could not load policy history.</p> : !historyItems.length ? <p className="text-text-muted mt-2 text-xs">No immutable releases yet.</p> : <ul className="mt-2 space-y-2">{historyItems.map((item) => <li key={`${item.release_id}-${item.activation?.id ?? 'inactive'}`} className="bg-surface-sunken rounded p-2 text-xs"><div>v{item.version} · {item.release_id} · {item.policy_digest.slice(0, 12)}</div><div className="text-text-muted">{item.activation ? `epoch ${item.activation.epoch} · ${item.activation.action}` : 'saved inactive'} · {item.actor_attribution}</div>{active && item.activation && item.release_id !== active.release.id && item.version < active.release.version && <Button size="sm" variant="ghost" onClick={() => setConfirm({ releaseId: item.release_id, version: item.version })}>Reactivate v{item.version}</Button>}</li>)}</ul>}
        {history.isError && historyItems.length > 0 && <p role="alert" className="text-tier-red mt-2 text-xs">Could not load more policy history. Loaded releases are preserved.</p>}
        {!history.isLoading && historyItems.length > 0 && <Button size="sm" variant="ghost" disabled={!history.hasNextPage || history.isFetchingNextPage} aria-label={history.isError ? 'Retry loading policy history' : 'Load more policy history'} onClick={() => void history.fetchNextPage()}>{history.isFetchingNextPage ? 'Loading more history…' : history.isError ? 'Retry loading history' : history.hasNextPage ? 'Load more history' : 'End of policy history'}</Button>}
      </section>
      <section aria-labelledby="policy-outcomes-heading" className="border-border-subtle mt-5 border-t pt-4">
        <h4 id="policy-outcomes-heading" className="text-text-primary text-sm font-medium">Manager self-evaluation outcomes</h4>
        {outcomes.isLoading ? <p className="text-text-muted mt-2 text-xs">Loading outcomes…</p> : outcomes.isError && !outcomeItems.length ? <p role="alert" className="text-tier-red mt-2 text-xs">Could not load outcomes.</p> : !outcomeItems.length ? <p className="text-text-muted mt-2 text-xs">No manager self-evaluation outcomes yet.</p> : <ul className="mt-2 space-y-2">{outcomeItems.map((item) => <li key={item.candidate_id} className="bg-surface-sunken rounded p-2 font-mono text-xs"><div>{item.disposition ?? 'pending'} · {item.disposition_code ?? 'no durable evaluation'}</div><div className="text-text-muted">task {item.root_task_id} · session {item.manager_session_id} · release {item.release_id ?? 'missing'}</div><div className={item.receipt_state === 'complete' ? 'text-tier-green' : 'text-tier-amber'}>{item.receipt_state}</div></li>)}</ul>}
        {outcomes.isError && outcomeItems.length > 0 && <p role="alert" className="text-tier-red mt-2 text-xs">Could not load more outcomes. Loaded outcomes are preserved.</p>}
        {!outcomes.isLoading && outcomeItems.length > 0 && <Button size="sm" variant="ghost" disabled={!outcomes.hasNextPage || outcomes.isFetchingNextPage} aria-label={outcomes.isError ? 'Retry loading evaluation outcomes' : 'Load more evaluation outcomes'} onClick={() => void outcomes.fetchNextPage()}>{outcomes.isFetchingNextPage ? 'Loading more outcomes…' : outcomes.isError ? 'Retry loading outcomes' : outcomes.hasNextPage ? 'Load more outcomes' : 'End of evaluation outcomes'}</Button>}
      </section>
    </PolicyShell>
  );
}

function validateDraft(draft: AuthorityPolicyTemplate, expected: AuthorityPolicyTemplate): string | null {
  if (!draft.title.trim() || !draft.normative_text.trim()) return 'Title and normative policy are required.';
  if (draft.continuation_phrase !== expected.continuation_phrase) return 'The canonical continuation phrase cannot be changed.';
  if (draft.clauses.length !== expected.clauses.length) return 'Every canonical clause is required.';
  for (let index = 0; index < expected.clauses.length; index += 1) {
    const actual = draft.clauses[index]; const canonical = expected.clauses[index];
    if (!actual || actual.id !== canonical.id || actual.category !== canonical.category || actual.action !== canonical.action) return 'Clause ids, order, categories, and actions are server-controlled.';
    if (!actual.condition.trim()) return `Clause ${actual.id} needs a condition.`;
  }
  return null;
}

function PolicyShell({ children }: { children: ReactNode }): JSX.Element {
  return <section data-testid="team-escalation-policy" className="bg-surface border-border-default shadow-pasture-sm rounded-lg border p-4">{children}</section>;
}
