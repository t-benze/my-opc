/**
 * InboxRow — two-line row in the threads inbox. Per DESIGN.md
 * `components.inbox_row`. Pure prop-driven: the parent provides `href` (the
 * destination) and `onSelect` (the SPA-navigate callback); the row itself
 * doesn't know about react-router.
 *
 * Rendered as an `<a>` so middle-click / cmd-click open in a new tab,
 * right-click exposes "Copy link address", and assistive tech announces it
 * as a link. Plain primary clicks call `onSelect()` after suppressing the
 * default full-page reload, so the composition can drive SPA routing.
 *
 * Two layouts, selected by `layout` (default keeps the historical shape so the
 * design gallery and any non-thread consumer are untouched):
 *
 *   - `default` — two-line row: a leading needs-you accent dot + subject on
 *     line-1 with right-pinned status/dream pills; IdBadge + AgentChip on
 *     line-2. Renders the `active`/`done` status pills (THREADS-05).
 *
 *   - `thread`  — THR-099 id-first single-line row (a-threads `.thread`): a
 *     leading STATUS-driven dot (open=green accent, archived=grey), then the
 *     mono thread id, the serif subject, the status BADGE routed through the
 *     shared `semanticTone` vocabulary (open→info/blue, archived→neutral/grey),
 *     an inline `from dream` pill, and `last <last_speaker>`; the relative
 *     timestamp (`meta`) is right-aligned. Line-2 (multi-participant list) is
 *     intentionally omitted — `participants` is not on the thread-LIST payload
 *     (honesty fence); only the backed `last_speaker` is shown.
 *
 * The finer Direction-A states (waiting-on-you / review / merged / live / idle)
 * are intentionally absent — no field on the thread-list payload backs them.
 *
 * Founder-approved interactive-row mapping: bg-surface border-border-default
 * rounded-sm (8px) with shadow-pasture-sm. Active row uses accent-muted + left
 * marker; nested status/dream pills and indicators retain their own radii.
 */
import type { ReactNode } from 'react';
import { AgentChip } from './AgentChip';
import { CrescentMoonBadge } from './CrescentMoonBadge';
import { IdBadge } from './IdBadge';
import { toneClass } from './semanticTone';

interface InboxRowProps {
  threadId: string;
  subject: string;
  lastSpeaker?: { name: string; role: 'manager' | 'worker' | 'founder' };
  meta?: ReactNode;
  status: 'open' | 'archived';
  needsYou: boolean;
  active: boolean;
  /** Composed-from-dream marker — renders an additive "from dream" pill. */
  fromDream?: boolean;
  /**
   * Row model. `default` is the historical two-line shape (unchanged);
   * `thread` is the THR-099 id-first single-line thread-list row.
   */
  layout?: 'default' | 'thread';
  /** Destination URL for the row. Used as the `<a href>`. */
  href: string;
  /**
   * SPA-navigate handler. Invoked on plain primary clicks after
   * `preventDefault()`. Modifier clicks (cmd/ctrl/shift/middle/right) skip
   * `onSelect` and fall through to default anchor behaviour.
   */
  onSelect?: () => void;
  /**
   * THR-209: optional sibling control (e.g. a pin toggle) rendered beside
   * the row anchor. A sibling, never nested inside the `<a>` — interactive
   * inside interactive is invalid HTML and breaks assistive tech.
   */
  pinControl?: ReactNode;
}

const FROM_DREAM_PILL =
  'bg-accent-soft text-accent-text inline-flex items-center gap-1 rounded-full px-2 py-px text-xs leading-relaxed font-semibold';

export function InboxRow({
  threadId,
  subject,
  lastSpeaker,
  meta,
  status,
  needsYou,
  active,
  fromDream = false,
  layout = 'default',
  href,
  onSelect,
  pinControl,
}: InboxRowProps): JSX.Element {
  const handleClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
    if (e.defaultPrevented) return;
    if (e.button !== 0) return; // ignore non-primary clicks
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; // open-in-new-tab et al.
    if (!onSelect) return;
    e.preventDefault();
    onSelect();
  };

  const shellCls = `group relative block w-full rounded-sm border px-3 py-2 text-left no-underline transition-colors ${
    active
      ? 'bg-accent-muted border-accent-muted shadow-pasture-sm'
      : 'bg-surface border-border-default shadow-pasture-sm hover:border-border-strong'
  }`;
  const activeMarker = active && (
    <span
      aria-hidden="true"
      className="bg-accent absolute inset-y-1 left-0 w-0.5 rounded-full"
    />
  );

  if (layout === 'thread') {
    // id-first single-line row (a-threads `.thread`). Status-driven leading dot
    // (open=green accent, archived=grey); status badge routed through the shared
    // semanticTone vocabulary; inline `from dream` + `last <last_speaker>`.
    const dotCls = status === 'open' ? 'bg-accent' : 'bg-border-strong';
    const rowEl = (
      <a
        href={href}
        onClick={handleClick}
        aria-current={active ? 'page' : undefined}
        className={shellCls}
      >
        {activeMarker}
        <div className="flex items-center gap-2.5">
          <span
            aria-hidden="true"
            className={`inline-block h-2 w-2 shrink-0 rounded-full ${dotCls}`}
          />
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1">
            <IdBadge id={threadId} kind="thread" />
            <span className="font-display text-body-sm text-text-primary truncate font-medium">
              {subject}
            </span>
            <span
              className={`inline-flex items-center rounded-full px-2 py-px text-xs leading-relaxed font-semibold ${toneClass(status)}`}
            >
              {status}
            </span>
            {fromDream && (
              <span className={FROM_DREAM_PILL}>
                <CrescentMoonBadge className="h-3 w-3" />
                from dream
              </span>
            )}
            {lastSpeaker && (
              <span className="text-caption text-text-muted inline-flex items-center gap-1">
                last
                <span className="text-text-secondary font-mono">
                  {lastSpeaker.name}
                </span>
              </span>
            )}
          </div>
          {meta && (
            <span className="text-caption text-text-muted shrink-0 whitespace-nowrap">
              {meta}
            </span>
          )}
        </div>
      </a>
    );
    return pinControl ? (
      <div className="flex items-center gap-1">
        <div className="min-w-0 flex-1">{rowEl}</div>
        {pinControl}
      </div>
    ) : (
      rowEl
    );
  }

  const statusLabel = status === 'open' ? 'active' : 'done';
  const statusPillCls =
    status === 'open'
      ? 'bg-accent-soft text-accent-text'
      : 'bg-surface-sunken border border-border-default text-text-muted';

  const rowEl = (
    <a
      href={href}
      onClick={handleClick}
      aria-current={active ? 'page' : undefined}
      className={shellCls}
    >
      {activeMarker}
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          {needsYou && (
            <span
              aria-label="needs you"
              className="bg-accent inline-block h-1.5 w-1.5 shrink-0 rounded-full"
            />
          )}
          <span className="text-body-sm text-text-primary truncate font-medium">
            {subject}
          </span>
        </div>
        <span className="flex shrink-0 items-center gap-1">
          {fromDream && (
            <span className={FROM_DREAM_PILL}>
              <CrescentMoonBadge className="h-3 w-3" />
              from dream
            </span>
          )}
          <span
            className={`inline-flex items-center rounded-full px-2 py-px text-xs leading-relaxed font-semibold ${statusPillCls}`}
          >
            {statusLabel}
          </span>
        </span>
      </div>
      <div className="text-caption text-text-muted mt-1 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <IdBadge id={threadId} kind="thread" />
          {lastSpeaker && (
            <>
              <span aria-hidden="true">·</span>
              <AgentChip name={lastSpeaker.name} role={lastSpeaker.role} />
            </>
          )}
        </div>
        {meta && <span className="shrink-0">{meta}</span>}
      </div>
    </a>
  );

  return pinControl ? (
    <div className="flex items-center gap-1">
      <div className="min-w-0 flex-1">{rowEl}</div>
      {pinControl}
    </div>
  ) : (
    rowEl
  );
}
