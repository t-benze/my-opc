import type { Meta, StoryObj } from '@storybook/react';
import { Composer } from './Composer';

const meta = {
  title: 'Design System/Coverage/Composer',
  component: Composer,
  tags: ['autodocs'],
  decorators: [
    (Story) => (
      <div className="grid max-w-2xl gap-5" data-radius-coverage="composer">
        <Story />
        <Composer agents={[]} threadId="THR-ABORT" orgSlug="storybook" onSend={() => undefined} abortReplies={{ active: true, isPending: false, onAbort: () => undefined }} />
        <Composer agents={[]} threadId="THR-ERROR" orgSlug="storybook" onSend={() => undefined} errorMessage="Message could not be sent; your draft is preserved." />
      </div>
    ),
  ],
} satisfies Meta<typeof Composer>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Coverage: Story = {
  args: {
    agents: [],
    threadId: 'THR-READY',
    orgSlug: 'storybook',
    onSend: () => { window.location.hash = 'composer-sent'; },
    helper: 'Message the thread — all participants see it',
  },
};
