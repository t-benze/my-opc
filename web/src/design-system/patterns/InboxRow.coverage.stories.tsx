import type { Meta, StoryObj } from '@storybook/react';
import { InboxRow } from './InboxRow';

const meta = {
  title: 'Design System/Coverage/InboxRow', component: InboxRow, tags: ['autodocs'],
  decorators: [(Story) => (
    <div className="grid max-w-2xl gap-3" data-radius-coverage="inbox-row">
      <Story />
      <InboxRow threadId="THR-DONE" subject="Archived default row" status="archived" needsYou={false} active={false} href="#done" onSelect={() => { window.location.hash = 'action-done'; }} />
      <InboxRow threadId="THR-LIVE" subject="Active thread row" status="open" needsYou active layout="thread" meta="2m" href="#live" onSelect={() => { window.location.hash = 'action-live'; }} />
      <InboxRow threadId="THR-ARCHIVED" subject="Archived thread row" status="archived" needsYou={false} active={false} layout="thread" fromDream meta="1h" href="#archived" onSelect={() => { window.location.hash = 'action-archived'; }} />
    </div>
  )],
} satisfies Meta<typeof InboxRow>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Coverage: Story = {
  args: { threadId: 'THR-OPEN', subject: 'Active default row', status: 'open', needsYou: true, active: true, fromDream: true, href: '#open', onSelect: () => { window.location.hash = 'action-open'; } },
};
