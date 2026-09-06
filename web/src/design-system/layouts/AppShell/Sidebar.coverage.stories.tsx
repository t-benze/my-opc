import type { Meta, StoryObj } from '@storybook/react';
import { Home } from 'lucide-react';
import { useLocation } from 'react-router-dom';
import { AppProvider } from '@/design-system/providers/AppProvider';
import { Sidebar, SidebarNavItem } from './Sidebar';

const meta = {
  title: 'Design System/Coverage/Sidebar',
  component: Sidebar,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  decorators: [(Story) => <AppProvider><Story /></AppProvider>],
} satisfies Meta<typeof Sidebar>;

export default meta;
type Story = StoryObj<typeof meta>;

/** No org context exercises every disabled nav call site plus the footer account row. */
export const Coverage: Story = {};

function EnabledNavFixture(): JSX.Element {
  const location = useLocation();
  return (
    <div className="bg-bg-subtle flex min-h-40 w-rail flex-col gap-3 p-3">
      <SidebarNavItem to="/sidebar-story-target" enabled icon={Home}>
        Story target
      </SidebarNavItem>
      <output aria-label="Current story route">{location.pathname}</output>
    </div>
  );
}

/** Isolates the enabled pointer/keyboard navigation branch with an observable route outcome. */
export const EnabledNavigation: Story = { render: () => <EnabledNavFixture /> };
