/**
 * Composer — compact single-line 18px-rounded input with an INLINE attach icon and
 * a circular send button (THR-061 a-thread-detail). Per DESIGN.md
 * `components.textarea` + `components.button.primary`. Used at the foot of the
 * threads detail pane.
 *
 * The broadcast helper copy lives in the PLACEHOLDER ("Message the thread —
 * all participants see it"), so there is no separate helper line under the
 * input; only a send-error surfaces below the pill. The attach affordance is a
 * small inline paperclip icon (still labelled "Attach files") — the capability
 * is preserved, only the chrome is compacted.
 *
 * Draft persistence: useThreadDraft stores partial messages in localStorage
 * keyed by (orgSlug, threadId) with a 300ms debounce. Drafts survive
 * navigation and are cleared on successful send.
 *
 * Auto-grow, @-mention autocomplete, Enter-to-send (Shift+Enter for new
 * line) live in MentionTextarea so the same typing experience is reused
 * by other surfaces (e.g. NewThreadDialog). The optional abort-reply control
 * sits INSIDE the input pill as an icon-only trailing action next to the
 * circular send button (THR-099 Phase A, founder seq57 — reversing the earlier
 * "moved OUT" decision): a compact neutral/light-fill, thin-outline square
 * button with a centered red stop-square icon. It renders only while replies
 * are in flight and is owned entirely by props (the parent holds the
 * thread-level abort mutation).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, Paperclip, Square, X } from 'lucide-react';
import { MAX_THREAD_ATTACHMENTS, REMOVE_ATTACHMENT_LABEL } from '@/lib/threadAttachments';
import { MentionTextarea } from './MentionTextarea';
import type { AgentSummary } from '@/lib/api/agents';

const DRAFT_CAP_CHARS = 65_536;
const DRAFT_DEBOUNCE_MS = 300;

interface DraftHandle {
  draft: string;
  setDraft: (next: string) => void;
  clearDraft: () => void;
}

export interface PendingAttachment {
  id: string;
  file: File;
}

function useThreadDraft(orgSlug: string, threadId: string): DraftHandle {
  const key = `happyranch:draft:${orgSlug}:${threadId}`;
  const [draft, setDraftState] = useState<string>(() => {
    try { return localStorage.getItem(key) ?? ''; } catch { return ''; }
  });
  const timer = useRef<number | null>(null);

  // Re-read when key changes (org/thread switch).
  useEffect(() => {
    try { setDraftState(localStorage.getItem(key) ?? ''); } catch { setDraftState(''); }
  }, [key]);

  // Cancel any pending debounce write on unmount.
  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);

  const setDraft = useCallback((next: string) => {
    setDraftState(next);
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      try {
        if (next.length > DRAFT_CAP_CHARS) { console.debug('draft cap exceeded; skipping persist'); return; }
        if (next === '') localStorage.removeItem(key);
        else localStorage.setItem(key, next);
      } catch (e) {
        console.debug('draft persist failed', e);
      }
    }, DRAFT_DEBOUNCE_MS);
  }, [key]);

  const clearDraft = useCallback(() => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    try { localStorage.removeItem(key); } catch { /* ignore */ }
    setDraftState('');
  }, [key]);

  return { draft, setDraft, clearDraft };
}

interface ComposerProps {
  disabled?: boolean;
  pending?: boolean;
  /** Optional error message (typically from a failed send). */
  errorMessage?: string | null;
  /** Helper text shown below the textarea when no error is set. */
  helper?: string;
  /** Placeholder for the textarea. Defaults to a reasonable copy. */
  placeholder?: string;
  /**
   * Called with the markdown when the user presses Send or Enter (Shift+Enter
   * inserts a newline instead). May return a Promise; if it rejects (or a sync
   * impl throws), the draft is preserved so the user can retry without retyping.
   *
   * Broadcast model: the send is always delivered to all thread participants;
   * no per-message addressing is needed.
   */
  onSend: (markdown: string, attachments: PendingAttachment[]) => unknown | Promise<unknown>;
  attachments?: PendingAttachment[];
  onAttachmentsChange?: (attachments: PendingAttachment[]) => void;
  /** Lets a parent focus the textarea (e.g. the R keyboard shortcut). */
  registerFocus?: (focus: () => void) => void;

  // Required
  agents: AgentSummary[];
  threadId: string;
  /**
   * Active org slug — keys the localStorage draft alongside threadId. Passed
   * down from the rendering feature (patterns stay pure props-in/JSX-out and
   * must not call @/lib/orgSlug directly).
   */
  orgSlug: string;
  /**
   * Optional abort-reply trailing action rendered INSIDE the input pill, to the
   * left of the circular send button (THR-099 Phase A) — an icon-only compact
   * square button (red stop-square). It renders ONLY while `active` (replies in
   * flight); it is disabled and its accessible name/tooltip read "Aborting…"
   * while `isPending`, otherwise "Abort reply" (no visible text either way).
   * `onAbort` aborts EVERY in-flight reply (the parent owns the thread-level
   * mutation — the pattern stays pure). Other Composer surfaces (NewThreadDialog,
   * the assistant dock) omit this prop.
   */
  abortReplies?: { active: boolean; isPending: boolean; onAbort: () => void };
}

export function Composer({
  disabled,
  pending,
  errorMessage,
  helper,
  placeholder,
  registerFocus,
  onSend,
  attachments = [],
  onAttachmentsChange,
  agents = [],
  threadId = '',
  orgSlug,
  abortReplies,
}: ComposerProps): JSX.Element {
  const { draft, setDraft, clearDraft } = useThreadDraft(orgSlug, threadId);
  const canSend = Boolean(draft.trim() || attachments.length);

  const removeAttachment = (id: string) => {
    onAttachmentsChange?.(attachments.filter((item) => item.id !== id));
  };

  const submit = async () => {
    if (!canSend || disabled || pending) return;
    try {
      await onSend(draft, attachments);
      clearDraft();
      onAttachmentsChange?.([]);
    } catch {
      // Composition surfaces via errorMessage; draft is preserved for retry.
    }
  };

  // Broadcast copy rides in the placeholder (a-thread-detail): the helper prop
  // is used as the placeholder when the caller gave no explicit one, so the
  // compact input carries the broadcast semantics without a separate line.
  const composerPlaceholder =
    placeholder ??
    (disabled ? 'Thread is closed.' : (helper ?? 'Write a message…'));

  return (
    <div className="flex flex-col gap-2">
      {/* Pending attachment chips — above the pill so the input stays compact. */}
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {attachments.map((item) => (
            <span
              key={item.id}
              className="border-border-subtle bg-surface-raised text-caption inline-flex max-w-full items-center gap-2 rounded-md border px-2 py-1"
            >
              <span className="max-w-64 truncate">{item.file.name}</span>
              <button
                type="button"
                className="text-text-muted hover:text-text"
                aria-label={REMOVE_ATTACHMENT_LABEL}
                onClick={() => removeAttachment(item.id)}
                disabled={disabled || pending}
              >
                <X className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Compact rounded input: inline attach icon + textarea + circular send.
          Founder-approved 18px local extension — reads as a single-line input and
          grows gracefully for the rare Shift+Enter multi-line draft. */}
      <div className="border-border-default bg-surface-raised focus-within:border-accent-default flex items-end gap-1 rounded-lg border py-1 pr-1 pl-2 transition-colors">
        <label
          className="text-text-muted hover:text-text-secondary hover:bg-surface-hover mb-0.5 inline-flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-full transition-colors"
          title="Attach files"
        >
          <Paperclip className="h-4 w-4" aria-hidden="true" />
          <input
            aria-label="Attach files"
            type="file"
            multiple
            className="sr-only"
            disabled={disabled || pending}
            onChange={(event) => {
              const files = Array.from(event.currentTarget.files ?? []).slice(
                0,
                MAX_THREAD_ATTACHMENTS,
              );
              onAttachmentsChange?.([
                ...attachments,
                ...files.map((file) => ({
                  id: `${file.name}-${file.size}-${file.lastModified}`,
                  file,
                })),
              ].slice(0, MAX_THREAD_ATTACHMENTS));
              event.currentTarget.value = '';
            }}
          />
        </label>
        <MentionTextarea
          value={draft}
          onChange={setDraft}
          agents={agents}
          onSubmit={() => { submit(); }}
          disabled={disabled || pending}
          rows={1}
          placeholder={composerPlaceholder}
          ariaLabel="Compose follow-up"
          registerFocus={registerFocus}
          className="text-body text-text-primary placeholder:text-text-muted w-full resize-none bg-transparent py-1.5 focus:outline-none disabled:opacity-50"
        />
        {/* Abort reply — icon-only trailing action INSIDE the pill, left of the
            circular send (THR-099 abort-reply correction). Renders only while
            replies are in flight. A compact neutral/light-fill, thin-outline
            square button (matching the send button's h-9 footprint) whose
            centered icon is a red stop-square; it carries NO visible text (the
            "Abort reply" / "Aborting…" copy lives in the accessible name +
            tooltip, not on-screen). One click still aborts EVERY in-flight
            reply — the parent owns the thread-level mutation. */}
        {abortReplies?.active && (
          <button
            type="button"
            onClick={abortReplies.onAbort}
            disabled={abortReplies.isPending}
            aria-label={abortReplies.isPending ? 'Aborting…' : 'Abort reply'}
            title={abortReplies.isPending ? 'Aborting…' : 'Abort reply'}
            className="border-border-default bg-surface-raised text-feedback-danger hover:border-feedback-danger hover:bg-danger-soft mb-0.5 inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border transition-colors disabled:opacity-50"
          >
            <Square className="h-4 w-4" aria-hidden="true" />
          </button>
        )}
        <button
          type="button"
          onClick={submit}
          disabled={disabled || !canSend || pending}
          aria-label="Send"
          title="Send (Enter)"
          className="bg-accent text-accent-fg hover:bg-accent-hover disabled:bg-surface-hover disabled:text-text-muted mb-0.5 inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full transition-colors"
        >
          <ArrowRight className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>

      {/* Send error surfaces below the pill; the broadcast copy is the placeholder. */}
      {errorMessage && (
        <span className="text-caption text-feedback-danger">{errorMessage}</span>
      )}
    </div>
  );
}
