import type { Meta, StoryObj } from '@storybook/react';
import { useState } from 'react';
import { Button } from './primitives/Button';
import { Input } from './primitives/Input';
import { Label } from './primitives/Label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './primitives/Select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './primitives/Tabs';
import { Textarea } from './primitives/Textarea';
import { Tooltip, TooltipContent, TooltipTrigger } from './primitives/Tooltip';
import { AgentChip } from './patterns/AgentChip';
import { EmptyState } from './patterns/EmptyState';
import { FormField } from './patterns/FormField';
import { IdBadge } from './patterns/IdBadge';
import { KbdChip } from './patterns/KbdChip';
import { Markdown } from './patterns/Markdown';
import { Sparkline } from './patterns/Sparkline';
import { StatValue } from './patterns/StatValue';
import { StatusBadge } from './patterns/StatusBadge';

const meta = {
  title: 'Pasture/Overview',
  tags: ['autodocs'],
  excludeStories: /^PASTURE_/,
  parameters: {
    docs: { description: { component: 'The rendered Pasture component-library specification, expressed with shipped semantic tokens and reusable components. Stories are local and daemon-independent.' } },
  },
} satisfies Meta;
export default meta;
type Story = StoryObj<typeof meta>;

export type PastureReferenceRequirement = {
  id: string;
  storyExport: string;
  selectors: string[];
  semantics: string;
};

/** The 25 material sections rendered by the preserved founder HTML, in document order. */
export const PASTURE_REFERENCE_REQUIREMENTS: PastureReferenceRequirement[] = [
  { id: 'tokens', storyExport: 'Foundations', selectors: ['[data-token-grid]', '[data-layout-foundations]'], semantics: 'surface/semantic tokens plus radius, shadow, and responsive layout' },
  { id: 'identity', storyExport: 'IdentityAndTypography', selectors: ['[data-agent-ramp]', '[data-agent-roster]'], semantics: 'deterministic agent identity ramp and named agent row' },
  { id: 'type', storyExport: 'IdentityAndTypography', selectors: ['[data-type-ramp]'], semantics: 'display, heading, body, and mono typography' },
  { id: 'states', storyExport: 'FocusAndButtons', selectors: ['[data-focus-guidance]', 'button:disabled'], semantics: 'focus-visible and disabled states' },
  { id: 'buttons', storyExport: 'FocusAndButtons', selectors: ['[data-button-variants]', '[data-button-sizes]'], semantics: 'button variants and sizes' },
  { id: 'cards', storyExport: 'CardsBadgesAndChips', selectors: ['[data-card-grid]'], semantics: 'default and interactive card treatments' },
  { id: 'badges', storyExport: 'CardsBadgesAndChips', selectors: ['[data-badge-rollup]'], semantics: 'badges, tags, chips, and roll-ups' },
  { id: 'avatars', storyExport: 'IdentityAndTypography', selectors: ['[data-avatar-sizes] [data-avatar]'], semantics: 'avatar sizes and agent rows independently represented' },
  { id: 'icontile', storyExport: 'IdentityAndTypography', selectors: ['[data-icon-tiles] [data-icon-tile]'], semantics: 'icon tile sizes and semantic tones' },
  { id: 'forms', storyExport: 'FormControls', selectors: ['input[aria-invalid="true"]', '[data-form-selection]'], semantics: 'fields, validation, textarea, and selection' },
  { id: 'callout', storyExport: 'OverlaysAndRecovery', selectors: ['[data-callout-tones]'], semantics: 'information, warning, and danger callouts' },
  { id: 'confirm', storyExport: 'OverlaysAndRecovery', selectors: ['[data-confirm-action]'], semantics: 'consequential confirmation and recovery action' },
  { id: 'overlays', storyExport: 'OverlaysAndRecovery', selectors: ['[data-action-bar]'], semantics: 'popover-adjacent action bar composition' },
  { id: 'tooltip', storyExport: 'OverlaysAndRecovery', selectors: ['[data-tooltip-example]'], semantics: 'keyboard/hover tooltip trigger' },
  { id: 'stats', storyExport: 'StatsTimelineAndTable', selectors: ['[data-stat-grid]', '[role="meter"]'], semantics: 'stat values and utilization meter' },
  { id: 'timeline', storyExport: 'StatsTimelineAndTable', selectors: ['ol[data-timeline] li[data-timeline-event]'], semantics: 'ordered event timeline, independent of tables' },
  { id: 'tables', storyExport: 'StatsTimelineAndTable', selectors: ['table[data-table] thead', 'table[data-table] tbody'], semantics: 'responsive data table with header and body' },
  { id: 'prose', storyExport: 'ProseStatusAndProvenance', selectors: ['[data-prose-example]'], semantics: 'prose, properties, and code content' },
  { id: 'status', storyExport: 'ProseStatusAndProvenance', selectors: ['[data-provenance]', '[data-readiness]'], semantics: 'status, provenance, and readiness' },
  { id: 'pagestates', storyExport: 'PageStates', selectors: ['[role="tablist"]', '[data-page-state="empty"]', '[data-page-state="loading"]', '[data-page-state="error"]', '[data-page-state="unauthorized"]', '[data-page-state="populated"]'], semantics: 'empty/loading/error/unauthorized/populated page states' },
  { id: 'dock', storyExport: 'AssistantDockAndDevAffordance', selectors: ['[data-assistant-dock]'], semantics: 'assistant dock and composer' },
  { id: 'dev', storyExport: 'AssistantDockAndDevAffordance', selectors: ['[data-devstates] select', '[data-review-only="prototype"]'], semantics: 'visibly review-only prototype state selector' },
  { id: 'renames', storyExport: 'AssistantDockAndDevAffordance', selectors: ['table[data-rename-map] tbody tr'], semantics: 'detailed retired-to-canonical rename map' },
  { id: 'scoped', storyExport: 'AssistantDockAndDevAffordance', selectors: ['[data-screen-scoped] [data-composition]'], semantics: 'screen-scoped composition inventory' },
  { id: 'responsive', storyExport: 'AssistantDockAndDevAffordance', selectors: ['[data-responsive-contract]'], semantics: 'narrow-width wrapping and overflow contract' },
];

export const PASTURE_REFERENCE_RENAMES = [
  ['.prov (work hours)', '.badge-prov / .prov-row / .visibility-card'], ['.achip / .pchip', '.chip-agent / .chip-file / .chip'],
  ['.log / .ev', '.timeline / .tl-ev / .logtable / .logrow'], ['.tool', '.chip-tag / .toolcall'], ['.rollup', '.rollup-status / .rollup-counts'],
  ['.pb', '.codeblock'], ['.modal / .scrim', '.modal / .modal-scrim'], ['.doc', '.doc'], ['.reflection / .doc .lead', '.pullquote'],
  ['.rv-search / .kb-search / .sk-search / .search-mono', '.field-search'], ['.seg / .ob-seg / .seg2', '.seg / .seg-sm'],
  ['.card override (health)', '.card-tight'], ['.danger-btn', '.btn-danger-outline'], ['.esc-confirm / .oc-confirm / .confirm', '.confirm'],
  ['gated notes / banners / notices', '.callout: dashed / filled / semantic tone'], ['flavour / review / severity pills', '.badge: ok / info / caution / danger / neutral'],
  ['.scard / .ustat / .stat / .pq-count', '.stat / .stat-strip'], ['.inp / .inp2 / .ob-slug', '.inp / .ta / .inp-addon'],
  ['avatar aliases', '.avatar: xs / sm / md / lg / xl / hero'], ['icon aliases', '.icontile: sm / md / lg / xl'],
  ['.savebar / .editbar / .draftbar / .commit', '.actionbar'], ['.alog / .ai / .dream', '.timeline'],
  ['spinner aliases', '.spinner: xs / lg / hero'], ['.prop ×7', '.prop (--propk)'], ['.crumb ×7 / .back / .ob-back', '.crumb / .crumbs'],
  ['empty-state aliases', '.empty'], ['progress / spark / meter aliases', '.meter'], ['popover aliases', '.popover'],
  ['.field / .edit-row / .efield / .fld', '.field / .fld'], ['.codebox / .cmd', '.codeblock'],
  ['error/recovery aliases', '.errpanel / .recovery'], ['.reply-bubble / .typing', '.typing'], ['table aliases', '.dtable (--cols)'],
  ['status-led aliases', '.estate'], ['checkbox aliases', '.checkrow / .checkbox'], ['prototype state switchers', '.devstates'],
  ['assistant aliases', '.dock / .convo / .turn / .composer / .chip / .exec-chip'], ['inline avatar gradients / raw identity hex', '--agent-1…6 via deterministic identity'],
  ['title="…"', '.tip[data-tip]'], ['no shared focus rule', ':focus-visible ring'], ['inline disabled opacity', '--disabled-opacity / .is-disabled'],
  ['.sched-row (undefined)', '.dtable'],
] as const;

export const PASTURE_SCREEN_SCOPED_COMPOSITIONS = '.esc-card .heartbeat .snap .pulse-row .thread .t-led .resp-strip .decline-row .composer-box .rail-* .task-ref .trow .grp-head .chain .clink .cnode .ccard .subt .fanout .fo-* .xrow .xtree .ag-li .ag-hero .pend-card .exec-card .subnav .token-box .jrow .signal .kb-entry .kb-facet .scard .route .folder .art-* .week .band .legend-row .dream .moon .cand .roster .impact-row .pickset .wakes .ob-* .prq .dc-* .sk-entry .pq-* .rd-* .askill .pa-li .pred'.split(' ');

const Section = ({ eyebrow, title, children }: { eyebrow: string; title: string; children: React.ReactNode }) => (
  <section className="mx-auto grid w-full max-w-content gap-5 border-b border-border-subtle py-8 first:pt-0 last:border-0">
    <header><p className="text-caption font-semibold uppercase tracking-wide text-accent-text">{eyebrow}</p><h2 className="mt-1 font-display text-display text-text-primary">{title}</h2></header>
    {children}
  </section>
);
const Card = ({ children }: { children: React.ReactNode }) => <div className="rounded-lg border border-border-default bg-surface-raised p-5 shadow-sm">{children}</div>;

export const Foundations: Story = { render: () => <div>
  <div data-pasture-requirement="tokens"><Section eyebrow="Foundations" title="Tokens"><div data-token-grid className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{[
    ['Canvas', 'bg-surface-canvas'], ['Raised', 'bg-surface-raised'], ['Sunken', 'bg-surface-sunken'], ['Overlay', 'bg-surface-overlay'],
    ['Accent', 'bg-accent-default'], ['Success', 'bg-feedback-success'], ['Warning', 'bg-feedback-warning'], ['Danger', 'bg-feedback-danger'],
  ].map(([label, tone]) => <div key={label} className={`min-h-24 rounded-lg border border-border-default p-4 ${tone}`}><span className="text-caption font-semibold">{label}</span></div>)}</div></Section>
  <Section eyebrow="Foundations" title="Radius, shadow & layout"><div data-layout-foundations className="grid gap-4 sm:grid-cols-3"><div className="rounded border border-border-default bg-surface-raised p-5">Small radius</div><div className="rounded-lg border border-border-default bg-surface-raised p-5 shadow-sm">Card radius</div><div className="rounded-full border border-border-default bg-surface-raised p-5 text-center">Pill radius</div></div></Section></div>
</div> };

export const IdentityAndTypography: Story = { render: () => <div>
  <div data-pasture-requirement="identity"><Section eyebrow="Foundations" title="Agent identity ramp"><div data-agent-ramp className="flex flex-wrap gap-3"><AgentChip name="founder" role="founder" /><AgentChip name="engineering_manager" role="manager" /><AgentChip name="frontend_engineer" role="worker" /></div><div data-agent-roster className="mt-4 text-caption text-text-muted">founder · engineering_manager · frontend_engineer · qa_engineer · code_reviewer</div></Section></div>
  <div data-pasture-requirement="avatars"><Section eyebrow="Components" title="Avatars & agent rows"><div data-avatar-sizes className="flex flex-wrap items-end gap-3">{['xs', 'sm', 'md', 'lg', 'xl', 'hero'].map((size, index) => <div key={size} className="grid justify-items-center gap-1"><span data-avatar data-size={size} className={`grid rounded-full bg-accent-soft text-accent-text ${index > 3 ? 'size-12' : 'size-9'} place-items-center font-semibold`}>FE</span><span className="text-caption">{size}</span></div>)}</div></Section></div>
  <div data-pasture-requirement="icontile"><Section eyebrow="Components" title="Icon tiles"><div data-icon-tiles className="flex flex-wrap gap-3">{['accent', 'info', 'caution', 'danger'].map((tone) => <div key={tone} data-icon-tile data-tone={tone} className="grid size-12 place-items-center rounded-lg border border-border-default bg-surface-sunken" aria-label={`${tone} menu icon tile`}><span aria-hidden>☰</span></div>)}</div></Section></div>
  <div data-pasture-requirement="type"><Section eyebrow="Foundations" title="Typography"><Card><div data-type-ramp><p className="font-display text-display">Newsreader display, 30px</p><h3 className="mt-4 text-h2">Section heading</h3><p className="mt-2 text-body text-text-secondary">Hanken Grotesk carries dense interface copy clearly.</p><code className="mt-3 block text-mono-sm text-accent-text">TASK-6622 · JetBrains Mono</code></div></Card></Section></div>
</div> };

export const FocusAndButtons: Story = { render: () => <div data-pasture-requirement="states"><Section eyebrow="Components" title="Focus, disabled & buttons"><div data-pasture-requirement="buttons" className="grid gap-5"><div data-button-variants className="flex flex-wrap gap-3">{(['default', 'secondary', 'outline', 'ghost', 'destructive', 'destructiveOutline', 'link'] as const).map((variant) => <Button key={variant} variant={variant}>{variant}</Button>)}</div><div data-button-sizes className="flex flex-wrap items-center gap-3"><Button size="sm">Small</Button><Button>Default</Button><Button size="lg">Large</Button><Button size="icon" aria-label="More actions">⋮</Button><Button loading>Saving</Button><Button disabled>Disabled</Button></div><p data-focus-guidance className="text-caption text-text-muted">Use Tab to inspect the token-backed focus-visible ring.</p></div></Section></div> };

export const CardsBadgesAndChips: Story = { render: () => <div>
  <div data-pasture-requirement="cards"><Section eyebrow="Components" title="Cards"><div data-card-grid className="grid gap-4 md:grid-cols-2"><Card><h3 className="text-h2">Default card</h3><p className="mt-2 text-body text-text-secondary">Raised surface, quiet border, compact shadow.</p></Card><Card><h3 className="text-h2">Interactive card</h3><p className="mt-2 text-body text-text-secondary">Content density stays readable at narrow widths.</p></Card></div></Section></div>
  <div data-pasture-requirement="badges"><Section eyebrow="Components" title="Badges, tags, chips & roll-ups"><div data-badge-rollup className="flex flex-wrap items-center gap-3"><StatusBadge status="open" /><StatusBadge status="in_progress" /><StatusBadge status="blocked" /><StatusBadge status="failed" /><IdBadge id="THR-221" kind="thread" /><IdBadge id="TASK-6622" kind="task" /><KbdChip keys={['Ctrl', 'Enter']} /></div></Section></div>
</div> };

export const FormControls: Story = { render: () => <div data-pasture-requirement="forms"><Section eyebrow="Components" title="Form controls & selection"><div data-form-selection className="grid max-w-2xl gap-5 sm:grid-cols-2"><FormField label="Name" htmlFor="pasture-name"><Input id="pasture-name" placeholder="Pasture" /></FormField><FormField label="Validation" htmlFor="pasture-error" error="A name is required"><Input id="pasture-error" aria-invalid /></FormField><div className="grid gap-2"><Label htmlFor="pasture-notes">Notes</Label><Textarea id="pasture-notes" placeholder="Add a note…" /></div><div className="grid content-start gap-2"><Label>Status</Label><Select defaultValue="ready"><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="ready">Ready</SelectItem><SelectItem value="blocked">Blocked</SelectItem></SelectContent></Select></div></div></Section></div> };

export const OverlaysAndRecovery: Story = { render: () => <div>
  <div data-pasture-requirement="callout"><Section eyebrow="Components" title="Callouts"><div data-callout-tones className="grid gap-3"><div className="rounded-lg border border-feedback-info bg-info-soft p-4 text-body">Information stays calm and actionable.</div><div className="rounded-lg border border-feedback-warning bg-attention-soft p-4 text-body">Confirm consequential actions before continuing.</div><div className="rounded-lg border border-feedback-danger bg-danger-soft p-4 text-body">Error details remain visible.</div></div></Section></div>
  <div data-pasture-requirement="confirm"><Section eyebrow="Components" title="Confirm & recovery"><div data-confirm-action className="flex items-center justify-between gap-3 rounded-lg border border-feedback-danger bg-danger-soft p-4"><span>Confirm consequential actions before continuing.</span><Button variant="outline" size="sm">Retry</Button></div></Section></div>
  <div data-pasture-requirement="overlays"><Section eyebrow="Components" title="Popover & action bar"><div data-action-bar className="flex rounded-lg border border-border-default bg-surface-raised p-2 shadow-sm"><Button size="sm">Save</Button><Button size="sm" variant="ghost">Cancel</Button></div></Section></div>
  <div data-pasture-requirement="tooltip"><Section eyebrow="Components" title="Tooltip"><div data-tooltip-example><Tooltip defaultOpen><TooltipTrigger asChild><Button variant="ghost">Hover or focus</Button></TooltipTrigger><TooltipContent>Helpful, concise context</TooltipContent></Tooltip></div></Section></div>
</div> };

export const StatsTimelineAndTable: Story = { render: () => <div>
  <div data-pasture-requirement="stats"><Section eyebrow="Data display" title="Stats & meters"><div data-stat-grid className="grid gap-4 sm:grid-cols-3"><Card><p className="text-caption text-text-muted">Tokens</p><StatValue value={3707054} format="tokens" /></Card><Card><p className="text-caption text-text-muted">Tasks</p><StatValue value={42} format="count" /></Card><Card><p className="text-caption text-text-muted">Utilization</p><div role="meter" aria-valuenow={67} aria-valuemin={0} aria-valuemax={100} className="mt-3 h-2 overflow-hidden rounded-full bg-surface-sunken"><div className="h-full w-2/3 bg-accent-default" /></div></Card></div></Section></div>
  <div data-pasture-requirement="timeline"><Section eyebrow="Data display" title="Timeline"><ol data-timeline className="grid gap-3 border-l border-border-default pl-5"><li data-timeline-event><strong>Reference rendered</strong><p className="text-caption text-text-muted">Founder HTML inspected in Chromium</p></li><li data-timeline-event><strong>Visual comparison</strong><p className="text-caption text-text-muted">Desktop and narrow evidence captured</p></li></ol></Section></div>
  <div data-pasture-requirement="tables"><Section eyebrow="Data display" title="Tables"><div className="overflow-x-auto rounded-lg border border-border-default bg-surface-raised"><table data-table className="w-full min-w-lg text-left text-body"><thead className="bg-surface-sunken text-caption uppercase text-text-muted"><tr><th className="p-3">Event</th><th className="p-3">Owner</th><th className="p-3">State</th></tr></thead><tbody><tr className="border-t border-border-subtle"><td className="p-3">Reference rendered</td><td className="p-3">frontend_engineer</td><td className="p-3"><StatusBadge status="completed" /></td></tr><tr className="border-t border-border-subtle"><td className="p-3">Visual comparison</td><td className="p-3">TASK-6622</td><td className="p-3"><StatusBadge status="in_progress" /></td></tr></tbody></table></div></Section></div>
</div> };

export const ProseStatusAndProvenance: Story = { render: () => <div>
  <div data-pasture-requirement="prose"><Section eyebrow="Content" title="Prose, properties & code"><Card><div data-prose-example><Markdown body={'### What the skill does\n\n- Preserves semantic tokens\n- Keeps examples local\n\n```ts\nnpm run build-storybook\n```'} /></div></Card></Section></div>
  <div data-pasture-requirement="status"><Section eyebrow="Content" title="Status, provenance & readiness"><div className="grid gap-3 sm:grid-cols-2"><Card><dl data-provenance className="grid grid-cols-2 gap-2 text-body"><dt className="text-text-muted">Source</dt><dd>Founder reference</dd><dt className="text-text-muted">Task</dt><dd><IdBadge id="TASK-6622" kind="task" /></dd></dl></Card><Card><div data-readiness><p className="text-caption text-text-muted">Validation readiness</p><div className="mt-3"><Sparkline data={[0.2, 0.4, 0.35, 0.7, 0.9]} variant="green" /></div></div></Card></div></Section></div>
</div> };

function StatefulPageStates(): JSX.Element { const [loading, setLoading] = useState(false); return <div data-pasture-requirement="pagestates"><Section eyebrow="Patterns" title="Page states"><Tabs defaultValue="empty"><TabsList variant="underline" className="h-auto flex-wrap">{['empty', 'loading', 'error', 'unauthorized', 'populated'].map((state) => <TabsTrigger key={state} value={state} variant="underline" className="whitespace-normal">{state[0].toUpperCase() + state.slice(1)}</TabsTrigger>)}</TabsList><TabsContent forceMount value="empty"><div data-page-state="empty" className="h-64"><EmptyState title="Nothing here yet" body="Create the first item when you are ready." cta={{ label: 'Create item', onClick: () => undefined }} /></div></TabsContent><TabsContent forceMount value="loading"><div data-page-state="loading"><Card><div className="grid animate-pulse gap-3"><div className="h-4 w-1/3 rounded bg-surface-subtle"/><div className="h-16 rounded bg-surface-subtle"/></div></Card></div></TabsContent><TabsContent forceMount value="error"><div data-page-state="error" className="rounded-lg border border-feedback-danger bg-danger-soft p-5"><h3 className="text-h2">Could not load this view</h3><Button className="mt-4" variant="outline" onClick={() => setLoading(!loading)}>{loading ? 'Retrying…' : 'Retry'}</Button></div></TabsContent><TabsContent forceMount value="unauthorized"><div data-page-state="unauthorized" className="rounded-lg border border-border-default bg-surface-raised p-5"><h3 className="text-h2">Permission required</h3><p className="mt-2 text-body text-text-secondary">This proposal demonstrates copy only; auth remains product-owned.</p></div></TabsContent><TabsContent forceMount value="populated"><div data-page-state="populated"><Card><h3 className="text-h2">Three active items</h3><p className="mt-2 text-body text-text-secondary">Representative populated content.</p></Card></div></TabsContent></Tabs></Section></div>; }
export const PageStates: Story = { render: () => <StatefulPageStates /> };

export const AssistantDockAndDevAffordance: Story = { render: () => <div>
  <div data-pasture-requirement="dock"><Section eyebrow="Patterns" title="Assistant dock"><div data-assistant-dock className="ml-auto max-w-md rounded-lg border border-border-default bg-surface-raised p-4 shadow-lg"><p className="text-caption font-semibold uppercase text-accent-text">Ranch assistant</p><p className="mt-2 text-body">How can I help with this catalogue?</p><div className="mt-4 flex gap-2"><Input aria-label="Ask the assistant" placeholder="Ask a question…" /><Button>Send</Button></div></div></Section></div>
  <div data-pasture-requirement="dev"><Section eyebrow="Reference notes" title="Dev affordance"><Card><div data-devstates data-review-only="prototype" className="flex flex-wrap items-center gap-3 border border-dashed border-accent-default p-4 print:hidden"><Label htmlFor="pasture-prototype-state">prototype · review only</Label><select id="pasture-prototype-state" className="rounded border border-border-default bg-surface-raised px-3 py-2"><option>Default</option><option>Empty</option><option>Loading</option><option>Error</option></select></div><p className="mt-3 text-caption text-text-muted">Replaces six product-looking prototype switchers; never ship this chrome as product UI.</p></Card></Section></div>
  <div data-pasture-requirement="renames"><Section eyebrow="Reference notes" title="Rename map"><div className="overflow-x-auto rounded-lg border border-border-default"><table data-rename-map data-rename-count={PASTURE_REFERENCE_RENAMES.length} className="w-full min-w-lg text-left text-caption"><thead className="bg-surface-sunken"><tr><th className="p-3">Retired</th><th className="p-3">Canonical</th></tr></thead><tbody>{PASTURE_REFERENCE_RENAMES.map(([retired, canonical]) => <tr key={retired} className="border-t border-border-subtle"><td className="p-3 font-mono">{retired}</td><td className="p-3 font-mono text-accent-text">{canonical}</td></tr>)}</tbody></table></div></Section></div>
  <div data-pasture-requirement="scoped"><Section eyebrow="Reference notes" title="Staying screen-scoped"><Card><p className="text-body text-text-secondary">These feature compositions remain screen-owned; they are not reusable library APIs.</p><div data-screen-scoped className="mt-4 flex flex-wrap gap-2">{PASTURE_SCREEN_SCOPED_COMPOSITIONS.map((name) => <code data-composition key={name} className="rounded bg-surface-sunken px-2 py-1 text-mono-sm">{name}</code>)}</div></Card></Section></div>
  <div data-pasture-requirement="responsive" data-responsive-contract="wrap-at-390-and-scroll-wide-tables"><Section eyebrow="Reference notes" title="Responsive content"><Card><p className="text-body text-text-secondary">At 390 CSS px, controls and inventories wrap while data and rename tables retain horizontal scrolling.</p></Card></Section></div>
</div> };
