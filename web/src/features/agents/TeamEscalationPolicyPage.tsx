import { Link, useParams } from 'react-router-dom';
import { Button } from '@/design-system/primitives/Button';
import { useAgentsList, useAgentsRoutes } from '@/hooks/agents';
import { isEligiblePolicyManager } from '@/hooks/authorityPolicy';
import { TeamEscalationPolicyCard } from './TeamEscalationPolicyCard';

export function TeamEscalationPolicyPage(): JSX.Element {
  const { agent_name: agentName } = useParams<{ agent_name: string }>();
  const agents = useAgentsList();
  const routes = useAgentsRoutes();

  if (agents.isLoading) return <div className="text-text-muted p-6">Loading agent…</div>;
  const agent = agents.data?.agents.find((item) => item.name === agentName);
  const eligibleAgent = agent?.team && agent.role
    ? { name: agent.name, team: agent.team, role: agent.role }
    : undefined;
  if (agents.isError || !isEligiblePolicyManager(eligibleAgent)) {
    return <div className="text-text-muted p-6">Not found. <Link className="text-accent-text underline" to={routes.inbox()}>Back to agents</Link>.</div>;
  }
  const policyAgent = eligibleAgent!;

  return (
    <div className="bg-surface-canvas h-full overflow-y-auto">
      <main className="mx-auto w-full max-w-4xl p-4 sm:p-6" aria-labelledby="team-policy-page-heading">
        <Button asChild variant="ghost" size="sm"><Link to={routes.detail(policyAgent.name)}>← Back to Engineering Manager</Link></Button>
        <header className="mt-4 mb-5">
          <h1 id="team-policy-page-heading" className="font-display text-text-primary text-2xl font-medium">Team escalation policy</h1>
          <p className="text-text-muted mt-1 text-sm">Engineering · Engineering Manager</p>
        </header>
        <TeamEscalationPolicyCard agent={policyAgent} />
      </main>
    </div>
  );
}
