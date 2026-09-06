import type { Meta, StoryObj } from '@storybook/react';
import { MessageBubble } from './MessageBubble';

const meta = {
  title: 'Design System/Coverage/MessageBubble', component: MessageBubble, tags: ['autodocs'],
  decorators: [(Story) => (
    <div className="grid max-w-3xl gap-4" data-radius-coverage="message-bubble">
      <Story />
      <MessageBubble variant="worker" seq={2} speaker="frontend_engineer" speakerRole="worker" timestamp="2026-09-05T12:01:00Z" body="Worker message" />
      <MessageBubble variant="manager" seq={3} speaker="engineering_manager" speakerRole="manager" timestamp="2026-09-05T12:02:00Z" body="Manager message" />
      <MessageBubble variant="decline" seq={4} speaker="founder" speakerRole="founder" timestamp="2026-09-05T12:03:00Z" declineReason="More evidence is required." />
    </div>
  )],
} satisfies Meta<typeof MessageBubble>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Coverage: Story = {
  args: { variant: 'founder', seq: 1, speaker: 'founder', speakerRole: 'founder', timestamp: '2026-09-05T12:00:00Z', body: 'Founder message' },
};
