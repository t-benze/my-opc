import { useData } from '@/design-system/providers/DataContext';
export type { AuthorityPolicyTemplate } from '@/lib/api/authorityPolicy';

export const useTeamEscalationPolicy: ReturnType<typeof useData>['authorityPolicy']['useTeamEscalationPolicy'] =
  (agent) => useData().authorityPolicy.useTeamEscalationPolicy(agent);
export const useCreateTeamEscalationPolicyRelease = () =>
  useData().authorityPolicy.useCreateTeamEscalationPolicyRelease();
export const useActivateTeamEscalationPolicyRelease = () =>
  useData().authorityPolicy.useActivateTeamEscalationPolicyRelease();
export const useTeamEscalationPolicyHistory = (agent: { name: string; team: string; role: string }) =>
  useData().authorityPolicy.useTeamEscalationPolicyHistory(agent);
export const useTeamEscalationPolicyOutcomes = (agent: { name: string; team: string; role: string }) =>
  useData().authorityPolicy.useTeamEscalationPolicyOutcomes(agent);
