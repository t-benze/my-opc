/**
 * MentionTextarea — controlled textarea with auto-grow + @-mention
 * autocomplete + Cmd/Ctrl+Enter submit. The shared core used by the
 * follow-up Composer (thread page) and the body field of NewThreadDialog
 * so both surfaces have the same typing experience.
 *
 * Pure props in / events out. No persistence — the caller controls
 * `value` and chooses whether to back it with state, a draft store, etc.
 *
 * `onSubmit(value)` fires on Enter (and on Cmd/Ctrl+Enter for power users
 * used to that keybind) when the mention popup is closed. Shift+Enter
 * inserts a literal newline. IME composition is respected — Enter while
 * composing CJK input commits the composition without triggering submit.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { MentionAutocomplete } from './MentionAutocomplete';
import type { AgentSummary } from '@/lib/api/agents';

const MAX_TEXTAREA_PX = 240;

function useAutoGrow(
  ref: React.RefObject<HTMLTextAreaElement>,
  value: string,
  maxPx = MAX_TEXTAREA_PX,
): void {
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, maxPx) + 'px';
  }, [ref, value, maxPx]);
}

function detectOpenMention(text: string, caret: number):
  | { query: string; tokenStart: number }
  | null {
  for (let i = caret - 1; i >= 0; i--) {
    const ch = text[i];
    if (ch === '@') return { query: text.slice(i + 1, caret), tokenStart: i };
    if (/\s/.test(ch)) return null;
  }
  return null;
}

const DEFAULT_CLASSNAME =
  'border-border-default bg-surface-raised text-body-lg text-text-primary placeholder:text-text-muted focus:border-accent-default w-full resize-none rounded-sm border px-3 py-2 focus:outline-none disabled:opacity-50';

export interface MentionTextareaProps {
  value: string;
  onChange: (next: string) => void;
  agents: AgentSummary[];
  /** Fires on Enter when the mention popup is closed. */
  onSubmit?: (value: string) => void;
  disabled?: boolean;
  placeholder?: string;
  rows?: number;
  autoFocus?: boolean;
  /** `id` for label association (FormField `htmlFor`). */
  id?: string;
  /** Used when no `id`/external label exists. */
  ariaLabel?: string;
  /** Lets a parent focus the textarea (e.g. the R keyboard shortcut). */
  registerFocus?: (focus: () => void) => void;
  className?: string;
}

export function MentionTextarea({
  value,
  onChange,
  agents,
  onSubmit,
  disabled,
  placeholder,
  rows = 3,
  autoFocus,
  id,
  ariaLabel,
  registerFocus,
  className,
}: MentionTextareaProps): JSX.Element {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useAutoGrow(textareaRef, value);

  const [mention, setMention] = useState<
    | { query: string; tokenStart: number; anchor: { x: number; y: number; width: number; height: number } }
    | null
  >(null);

  const mentionMatches = useMemo(() => {
    if (!mention) return [];
    const q = mention.query.toLowerCase();
    return agents.filter((a) => a.name.toLowerCase().startsWith(q)).slice(0, 8);
  }, [mention, agents]);

  const popupOpen = mentionMatches.length > 0;

  useEffect(() => {
    registerFocus?.(() => textareaRef.current?.focus());
  }, [registerFocus]);

  const refreshMention = useCallback(() => {
    const el = textareaRef.current;
    if (!el || disabled) { setMention(null); return; }
    const caret = el.selectionStart ?? 0;
    const m = detectOpenMention(value, caret);
    if (!m) { setMention(null); return; }
    const rect = el.getBoundingClientRect();
    setMention({ query: m.query, tokenStart: m.tokenStart, anchor: { x: rect.left, y: rect.top, width: rect.width, height: rect.height } });
  }, [value, disabled]);

  useEffect(() => { refreshMention(); }, [refreshMention]);

  const acceptMention = useCallback((agent: AgentSummary) => {
    if (!mention) return;
    const el = textareaRef.current;
    if (!el) return;
    const caret = el.selectionStart ?? 0;
    const before = value.slice(0, mention.tokenStart);
    const after = value.slice(caret);
    const inserted = `@${agent.name} `;
    const next = before + inserted + after;
    onChange(next);
    setMention(null);
    queueMicrotask(() => {
      const newCaret = (before + inserted).length;
      el.setSelectionRange(newCaret, newCaret);
      el.focus();
    });
  }, [value, mention, onChange]);

  return (
    <>
      <textarea
        ref={textareaRef}
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyUp={refreshMention}
        onClick={refreshMention}
        onKeyDown={(e) => {
          if (e.key !== 'Enter') return;
          // Mention popup owns Enter (selects the active match).
          if (popupOpen) return;
          // Shift+Enter inserts a newline.
          if (e.shiftKey) return;
          // IME composition: Enter commits the candidate, must not submit.
          // Safari fires keydown for the final Enter after composition with
          // keyCode=229; check both isComposing and the legacy keyCode.
          if (e.nativeEvent.isComposing || e.keyCode === 229) return;
          e.preventDefault();
          if (onSubmit && value.trim() && !disabled) {
            onSubmit(value);
          }
        }}
        placeholder={placeholder}
        disabled={disabled}
        rows={rows}
        autoFocus={autoFocus}
        aria-label={ariaLabel}
        className={className ?? DEFAULT_CLASSNAME}
      />
      {mention && (
        <MentionAutocomplete
          anchor={mention.anchor}
          matches={mentionMatches}
          onSelect={acceptMention}
          onDismiss={() => setMention(null)}
        />
      )}
    </>
  );
}
