import type { Meta, StoryObj } from '@storybook/react';
import { TypingBubble } from './TypingBubble';

const meta = {
  title: 'Design System/Coverage/TypingBubble', component: TypingBubble, tags: ['autodocs'],
  decorators: [(Story) => (
    <div className="grid max-w-lg gap-4" data-radius-coverage="typing-bubble">
      <Story />
      <TypingBubble agentName="code_reviewer" status="queued" startedAt={null} />
    </div>
  )],
} satisfies Meta<typeof TypingBubble>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Coverage: Story = {
  args: { agentName: 'frontend_engineer', status: 'working', startedAt: '2026-09-05T12:00:00Z', nowMs: Date.parse('2026-09-05T12:00:12Z') },
};
