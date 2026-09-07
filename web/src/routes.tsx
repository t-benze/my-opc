import {
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useParams,
  useSearchParams,
} from 'react-router-dom';
import { AppBar } from '@/design-system/layouts/AppShell/AppBar';
import { ErrorBoundary } from '@/design-system/layouts/AppShell/ErrorBoundary';
import { Sidebar } from '@/design-system/layouts/AppShell/Sidebar';
import { useOrgsList } from '@/hooks/orgs';
import { OrgProvider } from '@/lib/orgSlug';
import { AgentsPage } from '@/features/agents/AgentsPage';
import { TeamEscalationPolicyPage } from '@/features/agents/TeamEscalationPolicyPage';
import { ArtifactsPage } from '@/features/artifacts/ArtifactsPage';
import { JobsPage } from '@/features/jobs/JobsPage';
import { JobDetailPage } from '@/features/jobs/JobDetailPage';
import { CommandPaletteHost } from '@/host/CommandPaletteHost';
import { HelpDrawerHost } from '@/host/HelpDrawerHost';
import { AssistantDockHost } from '@/features/system-assistant/AssistantDockHost';
import { AuditPage } from '@/features/audit/AuditPage';
import { SkillsPage } from '@/features/skills/SkillsPage';
import { SkillValidationPage } from '@/features/skills/SkillValidationPage';
import { SkillDetailPage } from '@/features/skills/SkillDetailPage';
import { CustomSkillsPage } from '@/features/skills/CustomSkillsPage';
import { CustomSkillCreatePage } from '@/features/skills/CustomSkillCreatePage';
import { CustomSkillDetailPage } from '@/features/skills/CustomSkillDetailPage';
import { DashboardPage } from '@/features/dashboard/DashboardPage';
import { KbPage } from '@/features/kb/KbPage';
import { TasksPage } from '@/features/tasks/TasksPage';
import { TaskDetailPage } from '@/features/tasks/TaskDetailPage';
import { UsagePage } from '@/features/usage/UsagePage';
import { HealthPage } from '@/features/health/HealthPage';
import { OnboardingPage } from '@/features/onboarding/OnboardingPage';
import { DreamsPage } from '@/features/dreams/DreamsPage';
import { OverviewPage as WorkHoursOverviewPage } from '@/features/work-hours-config/OverviewPage';
import { WakesView as WorkHoursWakesView } from '@/features/work-hours-config/WakesView';
import { AgentDetailPage as WorkHoursAgentDetailPage } from '@/features/work-hours-config/AgentDetailPage';
import { SettingsPage } from '@/features/settings/SettingsPage';
import { ThreadsPage } from '@/features/threads/ThreadsPage';
import { TodosPage } from '@/features/todos/TodosPage';
import { PROTOTYPES_DISABLED, prototypeRoutes } from '@/prototypes';

function RootRedirect(): JSX.Element {
  const orgsQuery = useOrgsList();
  if (orgsQuery.isLoading) {
    return <div className="text-fg-muted p-6">Loading…</div>;
  }
  const first = orgsQuery.data?.orgs[0]?.slug;
  if (!first) {
    return <Navigate to="/onboarding" replace />;
  }
  return <Navigate to={`/orgs/${first}/dashboard`} replace />;
}

function OrgLayout(): JSX.Element {
  return (
    <OrgProvider>
      <Outlet />
    </OrgProvider>
  );
}

function AppShell(): JSX.Element {
  const location = useLocation();
  return (
    <div className="flex h-full flex-row">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <AppBar />
        <main className="flex-1 overflow-hidden">
          <ErrorBoundary resetKey={location.pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
      <CommandPaletteHost />
      <HelpDrawerHost />
      <AssistantDockHost />
    </div>
  );
}

export function AppRoutes(): JSX.Element {
  return (
    <Routes>
      {/* Prototype routes mount OUTSIDE AppShell so the TopBar + nav inside
          `PrototypesLayout` run under `<PrototypeProvider>`'s QueryClient
          and OrgSlugContext — keeping mock-only behaviour fully isolated
          from the daemon-backed routes. */}
      {!PROTOTYPES_DISABLED && prototypeRoutes()}
      <Route element={<AppShell />}>
        <Route index element={<RootRedirect />} />
        {/* Onboarding is GLOBAL (not org-scoped): the Welcome/create/success
            shell drives the container-level /orgs list + create routes, so a
            slug is not yet chosen. Mounted in AppShell like the org-less index. */}
        <Route path="onboarding" element={<OnboardingPage />} />
        <Route path="/orgs/:slug" element={<OrgLayout />}>
          <Route index element={<NavigateToHome />} />
          <Route path="dashboard" element={<DashboardPage />} />
          <Route path="threads" element={<ThreadsPage />} />
          <Route path="threads/:thread_id" element={<ThreadsPage />} />
          <Route path="tasks" element={<TasksPage />} />
          <Route path="tasks/:task_id" element={<TaskDetailPage />} />
          <Route path="todos" element={<TodosPage />} />
          <Route path="todos/:scheduleId" element={<TodosPage />} />
          <Route path="kb" element={<KbPage />} />
          <Route path="kb/*" element={<KbPage />} />

          <Route path="audit" element={<AuditPage />} />
          <Route path="skills" element={<SkillsPage />} />
          {/* Static `new`/`validation` rank above the dynamic `:skillId` in
              react-router v6, but keep them declared first for readability. */}
          <Route path="skills/validation" element={<SkillValidationPage />} />
          <Route path="skills/custom" element={<CustomSkillsPage />} />
          <Route path="skills/custom/new" element={<CustomSkillCreatePage />} />
          <Route path="skills/custom/:skillId" element={<CustomSkillDetailPage />} />
          <Route path="skills/:skillId" element={<SkillDetailPage />} />
          <Route path="agents" element={<AgentsPage />} />
          <Route path="agents/:agent_name" element={<AgentsPage />} />
          <Route path="agents/:agent_name/team-escalation-policy" element={<TeamEscalationPolicyPage />} />
          <Route path="jobs" element={<JobsPage />} />
          <Route path="jobs/:job_id" element={<JobDetailPage />} />
          <Route path="health" element={<HealthPage />} />
          <Route path="usage" element={<UsagePage />} />
          {/* THR-061 seq79: Spend renamed to Usage. Keep the old /spend
              bookmark (and the dashboard deep-link) working via redirect. */}
          <Route path="spend" element={<SpendRedirect />} />
          <Route path="dreams" element={<DreamsPage />} />
          {/* THR-035: the standalone Schedule surface folded into Work Hours.
              Old bookmarks redirect to the Wakes view; the wake list now lives
              as the `?view=wakes` tab of the Work Hours surface. */}
          <Route path="schedule" element={<ScheduleRedirect />} />
          <Route path="work-hours" element={<WorkHoursSurface />} />
          <Route path="work-hours/:agent" element={<WorkHoursAgentDetailPage />} />
          <Route path="artifacts" element={<ArtifactsPage />} />
          <Route path="settings/*" element={<SettingsPage />} />
        </Route>
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}

function NavigateToHome(): JSX.Element {
  const { slug } = useParams<{ slug: string }>();
  return <Navigate to={`/orgs/${slug}/dashboard`} replace />;
}

/**
 * Work Hours surface (THR-035): one route hosting two in-page tabs. The `Wakes`
 * tab (the wake-execution list folded in from the retired Schedule surface) is
 * selected by `?view=wakes`; everything else shows the config Overview. Using a
 * query param rather than a `work-hours/wakes` sub-route avoids any ranking
 * dependency against the sibling `work-hours/:agent` detail route.
 */
function WorkHoursSurface(): JSX.Element {
  const [searchParams] = useSearchParams();
  return searchParams.get('view') === 'wakes' ? (
    <WorkHoursWakesView />
  ) : (
    <WorkHoursOverviewPage />
  );
}

/** Redirect the retired /schedule surface to the Work Hours Wakes view. */
function ScheduleRedirect(): JSX.Element {
  const { slug } = useParams<{ slug: string }>();
  return <Navigate to={`/orgs/${slug}/work-hours?view=wakes`} replace />;
}

/** THR-061 seq79: /spend was renamed to /usage. Redirect the old path so
 *  existing bookmarks and the dashboard "This week's burn" deep-link resolve. */
function SpendRedirect(): JSX.Element {
  const { slug } = useParams<{ slug: string }>();
  return <Navigate to={`/orgs/${slug}/usage`} replace />;
}

function NotFound(): JSX.Element {
  return (
    <div className="text-fg-muted p-6">
      Not found. <a href="/" className="text-accent hover:underline">Go home</a>.
    </div>
  );
}
