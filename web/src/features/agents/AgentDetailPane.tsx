/**
 * AgentDetailPane — inline right detail/edit pane (Direction-A Pasture).
 *
 * Direction-A Pasture styling: font-display for agent name / section headings,
 * cards with shadow-pasture-sm + rounded-lg (18px), tag pills (rounded-full,
 * led dot), tabular-nums for counts/IDs.
 *
 * Sections: Header (agent identity), executor dropdown (live-derived from
 * useExecutorOptions — same source as AddAgentDialog, no hard-coded list),
 * repo/tool chips, system prompt collapsible, accountability metrics,
 * recent tasks/memory/jobs. Sticky save bar at bottom.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ChevronDown, ChevronRight, MessageCircle, Plus, X, AlertCircle } from 'lucide-react';
import { TaskCard } from '@/design-system/patterns/TaskCard';
import { EmptyState } from '@/design-system/patterns/EmptyState';
import { Button } from '@/design-system/primitives/Button';
import { ApiError } from '@/lib/api';
import {
  useAgentLearnings,
  useAgentsList,
  useAgentTasks,
  useManageAgentRepo,
  useSetAgentExecutor,
  useSetAgentModel,
} from '@/hooks/agents';
import { useTasksRoutes } from '@/hooks/tasks';
import { useJobsList } from '@/hooks/jobs';
import { useDensity } from '@/hooks/density';
import { AgentAvatar } from './AgentAvatar';
import { useExecutorOptions } from './useExecutorOptions';
import { TeamEscalationPolicyCard } from './TeamEscalationPolicyCard';

interface AgentDetailPaneProps {
  agentName: string;
  onClose: () => void;
  /** Called when the user clicks Start Thread for this agent. */
  onStartThread?: () => void;
}

/** Sparse dirty-state tracker: only keys that differ from the last-saved snapshot. */
interface DirtyState {
  executor?: string;
  /** Per-agent model string; empty string = unset/default (matches CLI clear semantics). */
  model?: string;
  /** Repos as a whole-dict replace: the current (possibly edited) map. */
  repos?: Record<string, string>;
  /** Names of repos removed since last save. */
  removedRepos?: Set<string>;
}

function useAccountabilityMetrics(agentName: string) {
  const tasksQuery = useAgentTasks(agentName);
  const tasks = tasksQuery.data?.tasks ?? [];
  const done = tasks.filter((t) => t.status === 'completed' || t.status === 'superseded').length;
  const total = tasks.length;
  // Acceptance rate = (APPROVE+PASS verdicts) / reviewed tasks.
  // review_verdict is not a first-class TaskRecord field (it's on the audit log);
  // the DERIVE route (D-2) that would compute it from the audit_log is not yet
  // built. v1 renders only task counts — real derived counts, never estimates.
  return { tasksQuery, done, total };
}

export function AgentDetailPane({ agentName, onClose, onStartThread }: AgentDetailPaneProps): JSX.Element {
  const { slug } = useParams<{ slug: string }>();
  const agentsQuery = useAgentsList();
  const { density } = useDensity();
  const taskRoutes = useTasksRoutes();
  const learningsQuery = useAgentLearnings(agentName);
  const jobsQuery = useJobsList({ agent: agentName, status: 'all', limit: 10 });
  const { done, total, tasksQuery } = useAccountabilityMetrics(agentName);

  const setExecutor = useSetAgentExecutor();
  const setModel = useSetAgentModel();
  const manageRepo = useManageAgentRepo();
  const executorOptions = useExecutorOptions();

  const agent = agentsQuery.data?.agents.find((a) => a.name === agentName);
  const repos = useMemo(() => agent?.repos ?? {}, [agent?.repos]);

  // --- Dirty state ---
  const [dirty, setDirty] = useState<DirtyState>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [showPrompt, setShowPrompt] = useState(false);
  const [repoAddName, setRepoAddName] = useState('');
  const [repoAddUrl, setRepoAddUrl] = useState('');
  const [showRepoAdd, setShowRepoAdd] = useState(false);

  // Reset dirty state when agent changes
  useEffect(() => {
    setDirty({});
    setSaveError(null);
    setSaving(false);
    setShowRepoAdd(false);
    setRepoAddName('');
    setRepoAddUrl('');
  }, [agentName]);

  const isDirty = dirty.executor !== undefined || dirty.model !== undefined || dirty.repos !== undefined;

  const displayExecutor = dirty.executor ?? agent?.executor ?? '—';
  const displayModel = dirty.model !== undefined ? dirty.model : (agent?.model ?? '');
  const displayRepos = dirty.repos ?? repos;

  // Is the agent's current executor (or the dirty value) available as a
  // selectable live option? Used to guard save + display.
  const currentExecutorName = agent?.executor ?? '';
  const liveSelectableNames = useMemo(
    () => new Set(executorOptions.selectable.map((o) => o.name)),
    [executorOptions.selectable],
  );
  const allLiveNames = useMemo(
    () => new Set([
      ...executorOptions.selectable.map((o) => o.name),
      ...executorOptions.unavailable.map((o) => o.name),
    ]),
    [executorOptions.selectable, executorOptions.unavailable],
  );
  // The current executor is "stale" when it is neither selectable nor
  // listed as unavailable — it has completely disappeared from the live
  // data (e.g., custom profile was removed). It must still be shown but
  // cannot be assigned.
  const currentExecutorIsStale =
    currentExecutorName !== '' && !allLiveNames.has(currentExecutorName);

  const onExecutorChange = useCallback((val: string) => {
    // Prevent selecting an unavailable option.
    if (!liveSelectableNames.has(val)) return;
    if (val === currentExecutorName) {
      setDirty((prev) => {
        const next = { ...prev };
        delete next.executor;
        return next;
      });
    } else {
      setDirty((prev) => ({ ...prev, executor: val }));
    }
    setSaveError(null);
  }, [currentExecutorName, liveSelectableNames]);

  const onModelChange = useCallback((val: string) => {
    const trimmed = val.trim();
    const currentAgentModel = agent?.model ?? '';
    if (trimmed === (currentAgentModel || '')) {
      setDirty((prev) => {
        const next = { ...prev };
        delete next.model;
        return next;
      });
    } else {
      setDirty((prev) => ({ ...prev, model: trimmed }));
    }
    setSaveError(null);
  }, [agent?.model]);

  const onRepoRemove = useCallback((key: string) => {
    setDirty((prev) => {
      const current = prev.repos ?? { ...repos };
      const next = { ...current };
      delete next[key];
      const removed = new Set(prev.removedRepos ?? []);
      removed.add(key);
      return { ...prev, repos: next, removedRepos: removed };
    });
    setSaveError(null);
  }, [repos]);

  const onRepoAdd = useCallback(() => {
    const name = repoAddName.trim();
    const url = repoAddUrl.trim();
    if (!name || !url) return;
    setDirty((prev) => {
      const current = prev.repos ?? { ...repos };
      return { ...prev, repos: { ...current, [name]: url } };
    });
    setRepoAddName('');
    setRepoAddUrl('');
    setShowRepoAdd(false);
    setSaveError(null);
  }, [repoAddName, repoAddUrl, repos]);

  const onSave = useCallback(async () => {
    if (!slug) return;
    setSaving(true);
    setSaveError(null);
    const errors: string[] = [];

    // Save executor if dirty — guard: don't send an executor that is no
    // longer selectable (e.g., refetched away or unavailable).
    if (dirty.executor && dirty.executor !== agent?.executor) {
      if (!liveSelectableNames.has(dirty.executor)) {
        errors.push(`Executor "${dirty.executor}" is no longer available.`);
      } else {
        try {
          await setExecutor.mutateAsync({
            agentName,
            body: { executor: dirty.executor },
          });
        } catch (err: unknown) {
          const e = err as { message?: string };
          errors.push(`Executor: ${e.message ?? 'save failed'}`);
          // Preserve dirty state on error — user can retry.
          setSaving(false);
          setSaveError(errors.join('; '));
          return;
        }
      }
    }

    // Save model if dirty
    if (dirty.model !== undefined) {
      const targetModel = dirty.model || null;
      const currentModel = agent?.model ?? null;
      if (targetModel !== currentModel) {
        try {
          await setModel.mutateAsync({
            agentName,
            body: { model: targetModel },
          });
        } catch (err: unknown) {
          const e = err as { message?: string };
          errors.push(`Model: ${e.message ?? 'save failed'}`);
        }
      }
    }

    // Save repo changes if dirty
    if (dirty.repos) {
      const original = agent?.repos ?? {};
      const current = dirty.repos;
      // Removals
      for (const key of dirty.removedRepos ?? []) {
        if (!(key in current) && key in original) {
          try {
            await manageRepo.mutateAsync({
              agentName,
              body: { action: 'remove', repo_name: key },
            });
          } catch (err: unknown) {
            const e = err as { message?: string };
            errors.push(`Repo ${key}: ${e.message ?? 'remove failed'}`);
          }
        }
      }
      // Adds
      for (const [key, url] of Object.entries(current)) {
        if (!(key in original)) {
          try {
            await manageRepo.mutateAsync({
              agentName,
              body: { action: 'add', repo_name: key, url },
            });
          } catch (err: unknown) {
            const e = err as { message?: string };
            errors.push(`Repo ${key}: ${e.message ?? 'add failed'}`);
          }
        }
      }
      // Updates (repo existed before but URL changed)
      for (const [key, url] of Object.entries(current)) {
        if (key in original && original[key] !== url) {
          try {
            await manageRepo.mutateAsync({
              agentName,
              body: { action: 'update', repo_name: key, url },
            });
          } catch (err: unknown) {
            const e = err as { message?: string };
            errors.push(`Repo ${key}: ${e.message ?? 'update failed'}`);
          }
        }
      }
    }

    if (errors.length > 0) {
      setSaveError(errors.join('; '));
    } else {
      setDirty({});
    }
    setSaving(false);
  }, [slug, dirty, agentName, agent, setExecutor, setModel, manageRepo, liveSelectableNames]);

  const onReset = useCallback(() => {
    setDirty({});
    setSaveError(null);
  }, []);

  // ⌘S keyboard shortcut
  useEffect(() => {
    if (!isDirty) return;
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 's') {
        e.preventDefault();
        void onSave();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [isDirty, onSave]);

  const learningsError =
    learningsQuery.isError && learningsQuery.error instanceof ApiError
      ? learningsQuery.error
      : null;

  return (
    <section className="flex h-full flex-col">
      {/* --- Header --- */}
      <header className="border-border-default flex items-start gap-3.5 border-b px-5 py-4">
        {/* AGENTS-04: detail-hero avatar anchor (Direction-A `a-agents`),
            reusing the roster's role-colored initial chip. */}
        <AgentAvatar name={agentName} role={agent?.role ?? null} size="lg" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-3">
            <h2 className="font-display text-text-primary truncate text-xl font-medium">
              {agentName}
            </h2>
            <span
              aria-hidden="true"
              className={`inline-block h-2 w-2 shrink-0 rounded-full ${
                agent?.role === 'manager'
                  ? 'bg-agent-manager'
                  : 'bg-agent-worker'
              }`}
            />
          </div>
          <div className="text-text-muted mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs">
            <span className="bg-surface-sunken border-border-default rounded-full border px-2 py-px text-xs font-medium">
              {agent?.role ?? '…'}
            </span>
            <span className="tabular-nums">
              {agent?.team ?? '—'}
            </span>
            {agent?.executor && (
              <>
                <span aria-hidden="true" className="text-text-muted">·</span>
                <span className="bg-accent-soft text-accent-text rounded-full px-2 py-px text-xs font-medium">
                  {agent.executor}
                </span>
              </>
            )}
            {agent?.model && (
              <>
                <span aria-hidden="true" className="text-text-muted">·</span>
                <span className="bg-surface-sunken border-border-default rounded-full border px-2 py-px text-xs font-medium">
                  {agent.model}
                </span>
              </>
            )}
          </div>
          {agent?.description && (
            <p className="text-text-secondary mt-2 text-sm leading-relaxed">{agent.description}</p>
          )}
        </div>
        {onStartThread ? (
          <Button size="sm" className="shrink-0" onClick={onStartThread}>
            <MessageCircle size={14} className="mr-1" aria-hidden="true" />
            Start Thread
          </Button>
        ) : (
          <Button variant="ghost" size="sm" className="shrink-0" onClick={onClose}>
            <X size={16} />
          </Button>
        )}
      </header>

      {/* --- Editable fields — Pasture card sections --- */}
      <div className="flex-1 space-y-5 overflow-y-auto px-5 py-4">
        {agent?.role === 'manager' && agent.team === 'engineering' && agent.name === 'engineering_manager' && (
          <TeamEscalationPolicyCard agent={{ name: agent.name, team: agent.team, role: agent.role }} />
        )}
        {/* Executor — live-derived dropdown (same source as AddAgentDialog) */}
        <section className="bg-surface border-border-default shadow-pasture-sm rounded-lg border p-4">
          <h3 className="text-overline text-text-muted mb-3 tracking-wider uppercase">
            Executor
          </h3>
          {executorOptions.state === 'loading' ? (
            <p className="text-text-muted text-sm">Loading executor list…</p>
          ) : executorOptions.state === 'error' ? (
            <p className="text-tier-red text-xs">
              Could not load executor list. Editing is disabled.
            </p>
          ) : (
            <select
              value={displayExecutor}
              onChange={(e) => onExecutorChange(e.target.value)}
              className="border-border-subtle bg-surface w-full max-w-xs rounded border p-2 text-sm"
              aria-label="Executor"
            >
              {/* Stale current executor — visible but not assignable */}
              {currentExecutorIsStale && (
                <option key={currentExecutorName} value={currentExecutorName} disabled>
                  {currentExecutorName} (current, no longer registered)
                </option>
              )}
              {/* Unavailable current executor that IS known but not launchable */}
              {!currentExecutorIsStale &&
                executorOptions.unavailable.some((o) => o.name === displayExecutor) && (
                <option key={displayExecutor} value={displayExecutor} disabled>
                  {displayExecutor} (current, unavailable — Settings → Executors)
                </option>
              )}
              {executorOptions.selectable.map((opt) => (
                <option key={opt.name} value={opt.name}>
                  {opt.name}
                  {opt.kind === 'custom' ? ' (custom)' : ''}
                </option>
              ))}
              {executorOptions.unavailable
                .filter((o) => o.name !== displayExecutor)
                .length > 0 && (
                <>
                  <option disabled>── unavailable ──</option>
                  {executorOptions.unavailable
                    .filter((o) => o.name !== displayExecutor)
                    .map((opt) => (
                      <option key={opt.name} value={opt.name} disabled>
                        {opt.name}
                        {opt.kind === 'custom'
                          ? ' (custom, unavailable — Settings → Executors)'
                          : ' (not registered — Settings → Executors)'}
                      </option>
                    ))}
                </>
              )}
            </select>
          )}
          <p className="text-text-muted mt-2 text-xs">
            Takes effect on this agent's next task.
            {currentExecutorIsStale && (
              <>
                {' '}
                The current executor &ldquo;{currentExecutorName}&rdquo; is no longer
                registered and cannot be assigned to new agents.
              </>
            )}
          </p>
          {/* Settings → Executors navigation link for stale/unavailable executors */}
          {slug && (currentExecutorIsStale || executorOptions.unavailable.length > 0) && (
            <p className="text-text-muted mt-1 text-xs">
              {currentExecutorIsStale
                ? 'The current executor is no longer registered.'
                : 'Unavailable executors need to be registered.'}{' '}
              <Link to={`/orgs/${slug}/settings/executors`} className="text-accent-text underline">
                Settings → Executors
              </Link>
            </p>
          )}
        </section>

        {/* Model — freeform text input (executor-dependent, not a fixed enum) */}
        <section className="bg-surface border-border-default shadow-pasture-sm rounded-lg border p-4">
          <h3 className="text-overline text-text-muted mb-3 tracking-wider uppercase">
            Model
          </h3>
          <input
            type="text"
            value={displayModel}
            onChange={(e) => onModelChange(e.target.value)}
            placeholder={agent?.executor ? `Default for ${agent.executor}` : 'Unset'}
            className="border-border-subtle bg-surface w-full max-w-xs rounded-md border px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-accent-soft"
            aria-label="Model"
          />
          <p className="text-text-muted mt-2 text-xs">
            Empty = use default model. Takes effect on this agent's next task.
          </p>
        </section>

        {/* System prompt — READ-ONLY card */}
        {agent?.system_prompt && (
          <section className="bg-surface border-border-default shadow-pasture-sm rounded-lg border">
            <button
              type="button"
              onClick={() => setShowPrompt(!showPrompt)}
              className="text-text-secondary hover:text-text-primary flex w-full items-center gap-2 px-4 py-3 text-xs font-medium tracking-wider uppercase transition-colors"
            >
              {showPrompt ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              System prompt
            </button>
            {showPrompt && (
              <div className="border-border-default border-t px-4 pb-4">
                <pre className="bg-surface-sunken border-border-subtle mt-3 max-h-48 overflow-auto rounded-md border p-3 font-mono text-xs whitespace-pre-wrap">
                  {agent.system_prompt}
                </pre>
                <div className="text-text-muted mt-2 flex items-center gap-1.5 text-xs">
                  <AlertCircle size={12} />
                  <span>
                    Read-only. Updating system prompt from the web UI requires a
                    founder-facing route.
                  </span>
                </div>
              </div>
            )}
          </section>
        )}

        {/* Description — READ-ONLY */}
        {agent?.description && (
          <section className="bg-surface border-border-default shadow-pasture-sm rounded-lg border p-4">
            <h3 className="text-overline text-text-muted mb-2 tracking-wider uppercase">
              Description
            </h3>
            <p className="text-text-secondary text-sm leading-relaxed">{agent.description}</p>
            <div className="text-text-muted mt-2 flex items-center gap-1.5 text-xs">
              <AlertCircle size={12} />
              <span>Read-only — no founder-facing update route for description.</span>
            </div>
          </section>
        )}

        {/* Repo chips — rounded-full tag pattern */}
        <section className="bg-surface border-border-default shadow-pasture-sm rounded-lg border p-4">
          <h3 className="text-overline text-text-muted mb-3 tracking-wider uppercase">
            Repositories
          </h3>
          <div className="mb-3 flex flex-wrap gap-1.5">
            {Object.entries(displayRepos).map(([key, url]) => (
              <span
                key={key}
                className="bg-surface-sunken border-border-default text-text-secondary inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium"
              >
                <span className="bg-agent-worker inline-block h-1.5 w-1.5 shrink-0 rounded-full" />
                <a
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="hover:text-accent-text transition-colors"
                >
                  {key}
                </a>
                <button
                  type="button"
                  onClick={() => onRepoRemove(key)}
                  className="text-text-muted hover:text-tier-red ml-0.5 transition-colors"
                  aria-label={`Remove ${key}`}
                >
                  <X size={12} />
                </button>
              </span>
            ))}
            {Object.keys(displayRepos).length === 0 && (
              <span className="text-text-muted text-xs">No repositories configured.</span>
            )}
          </div>
          {showRepoAdd ? (
            <div className="bg-surface-sunken border-border-default space-y-2 rounded-lg border p-3">
              <input
                className="border-border-subtle bg-surface w-full rounded-md border px-2.5 py-1.5 text-xs"
                placeholder="Repo name (e.g. happyranch)"
                value={repoAddName}
                onChange={(e) => setRepoAddName(e.target.value)}
              />
              <input
                className="border-border-subtle bg-surface w-full rounded-md border px-2.5 py-1.5 text-xs"
                placeholder="Git URL"
                value={repoAddUrl}
                onChange={(e) => setRepoAddUrl(e.target.value)}
              />
              <div className="flex gap-2">
                <Button size="sm" onClick={onRepoAdd} disabled={!repoAddName.trim() || !repoAddUrl.trim()}>
                  Add
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setShowRepoAdd(false)}>
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setShowRepoAdd(true)}
            >
              <Plus size={14} className="mr-1" />
              Add repository
            </Button>
          )}
        </section>

        {/* Accountability metrics — display font, card */}
        <section className="bg-surface border-border-default shadow-pasture-sm rounded-lg border p-4">
          <h3 className="text-overline text-text-muted mb-3 tracking-wider uppercase">
            Accountability
          </h3>
          {tasksQuery.isLoading ? (
            <p className="text-text-muted text-xs">Loading…</p>
          ) : tasksQuery.isError ? (
            <p className="text-tier-red text-xs">
              Failed to load task counts.
            </p>
          ) : (
            <div className="flex items-baseline gap-3">
              <span className="font-display text-text-primary text-2xl font-medium tabular-nums">
                {total}
              </span>
              <span className="text-text-secondary text-sm tabular-nums">
                tasks
              </span>
              <span aria-hidden="true" className="text-text-muted">·</span>
              <span className="font-display text-text-primary text-2xl font-medium tabular-nums">
                {done}
              </span>
              <span className="text-text-secondary text-sm tabular-nums">
                done
              </span>
            </div>
          )}
        </section>

        {/* Recent tasks */}
        <section>
          <h3 className="text-overline text-text-muted mb-3 tracking-wider uppercase">
            Recent tasks
          </h3>
          {tasksQuery.isLoading ? (
            <p className="text-text-muted text-xs">Loading tasks…</p>
          ) : tasksQuery.data && tasksQuery.data.tasks.length > 0 ? (
            <ul className="space-y-2">
              {tasksQuery.data.tasks.map((t) => (
                <li key={t.task_id}>
                  <TaskCard
                    task={t}
                    to={taskRoutes.detail(t.task_id)}
                    density={density}
                    taskRoutes={taskRoutes}
                  />
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-text-muted text-xs">
              No tasks where this agent was the assigned manager.
            </p>
          )}
        </section>

        {/* Learnings */}
        <section>
          <h3 className="text-overline text-text-muted mb-3 tracking-wider uppercase">
            Learnings
          </h3>
          {learningsQuery.isLoading ? (
            <p className="text-text-muted text-xs">Loading learnings…</p>
          ) : learningsError?.status === 412 ? (
            <p className="text-text-muted text-xs">
              This workspace hasn't been migrated to the per-entry memory
              layout yet. Run <code>happyranch memory reindex</code> from the
              CLI to upgrade.
            </p>
          ) : learningsError ? (
            <p className="text-tier-red text-xs">
              Failed to load learnings ({learningsError.status}).
            </p>
          ) : learningsQuery.data && learningsQuery.data.entries.length > 0 ? (
            <ul className="space-y-2">
              {learningsQuery.data.entries.map((e) => (
                <li
                  key={e.id}
                  className="border-border-default bg-surface shadow-pasture-sm rounded-lg border p-3"
                >
                  <div className="flex items-center gap-2 text-xs">
                    <span className="text-text-muted font-mono tabular-nums">{e.id}</span>
                    <span className="text-text-muted">·</span>
                    <span className="text-text-muted">{e.topic}</span>
                  </div>
                  <p className="text-text-primary mt-1 text-sm font-medium">{e.title}</p>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState
              title="No learnings"
              body="This agent has not filed any learnings yet."
            />
          )}
        </section>

        {/* Recent jobs — object-ID click-through */}
        {jobsQuery.data && jobsQuery.data.jobs.length > 0 && (
          <section>
            <h3 className="text-overline text-text-muted mb-3 tracking-wider uppercase">
              Recent jobs
            </h3>
            <ul className="space-y-1.5 text-sm">
              {jobsQuery.data.jobs.map((j) => (
                <li
                  key={j.id}
                  className="border-border-default bg-surface shadow-pasture-sm rounded-lg border px-3 py-2"
                >
                  {slug ? (
                    <Link
                      to={`/orgs/${slug}/jobs/${j.id}`}
                      className="text-accent-text font-mono text-xs tabular-nums hover:underline"
                    >
                      {j.id}
                    </Link>
                  ) : (
                    <span className="font-mono text-xs tabular-nums">{j.id}</span>
                  )}
                  <span className="text-text-primary ml-2">{j.title}</span>
                  <span className="text-text-muted ml-2 text-xs">
                    <span className="bg-surface-sunken border-border-default rounded-full border px-1.5 py-px text-xs font-medium">
                      {j.status}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>

      {/* --- Sticky save bar — Pasture border/background --- */}
      {isDirty && (
        <footer className="border-border-default bg-surface-sunken flex items-center justify-between gap-3 border-t px-4 py-3">
          <div className="flex items-center gap-2">
            {saveError && (
              <div className="text-tier-red flex items-center gap-1.5 text-xs">
                <AlertCircle size={12} />
                <span>Save error: {saveError}</span>
              </div>
            )}
            {!saveError && (
              <p className="text-text-muted text-xs">
                You have unsaved changes. <kbd className="bg-surface border-border-default rounded border px-1.5 py-px font-mono text-xs">⌘S</kbd> to save.
              </p>
            )}
          </div>
          <div className="flex gap-2">
            <Button variant="ghost" size="sm" onClick={onReset}>
              Reset
            </Button>
            <Button size="sm" onClick={onSave} disabled={saving}>
              {saving ? 'Saving…' : 'Save agent'}
            </Button>
          </div>
        </footer>
      )}
    </section>
  );
}
