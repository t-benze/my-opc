import type { Meta, StoryObj } from '@storybook/react';
import { MoreVertical, Save } from 'lucide-react';
import { Button } from './Button';

const meta = { title: 'Design System/Coverage/Button', component: Button, tags: ['autodocs'] } satisfies Meta<typeof Button>;
export default meta;
type Story = StoryObj;
export const Coverage: Story = {};

export const CompleteContract: Story = {
  render: () => (
    <div className="flex max-w-3xl flex-wrap items-center gap-3" data-button-complete-contract>
      <Button>Primary</Button>
      <Button variant="secondary">Secondary</Button>
      <Button variant="outline">Outline</Button>
      <Button variant="ghost">Ghost</Button>
      <Button variant="destructive">Delete permanently</Button>
      <Button variant="destructiveOutline">Revoke token</Button>
      <Button variant="link">Link</Button>
      <Button size="sm">Small</Button>
      <Button>Default</Button>
      <Button size="lg">Large</Button>
      <Button size="icon" aria-label="More actions"><MoreVertical /></Button>
      <Button loading>Saving</Button>
      <Button disabled>Disabled</Button>
      <Button><Save />Save</Button>
    </div>
  ),
};
