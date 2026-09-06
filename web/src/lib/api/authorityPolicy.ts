import { request } from './client';

export interface AuthorityPolicyClause {
  id: string;
  category: string;
  condition: string;
  action: 'escalate_to_founder' | 'continue_same_root';
}

export interface TeamEscalationPolicyResponse {
  team: 'engineering';
  target_manager: 'engineering_manager';
  /** Server authorization remains authoritative for both mutations. */
  can_mutate: true;
  bootstrap_required?: true;
  bootstrap_template: AuthorityPolicyTemplate;
  active?: {
    activation_id: string;
    epoch: number;
    action: 'bootstrap' | 'activate' | 'reactivate_rollback';
    created_at: string;
    actor_attribution: 'shared local operator credential';
    release: {
      id: string;
      policy_id: string;
      version: number;
      title: string;
      normative_text: string;
      clauses: AuthorityPolicyClause[];
      continuation_phrase: string;
      digest: string;
      created_at: string;
      actor_attribution: 'shared local operator credential';
    };
  };
}

export interface AuthorityPolicyHistoryResponse {
  items: Array<{ release_id: string; policy_id: string; version: number;
    policy_digest: string; release_created_at: string;
    activation: null | { id: string; epoch: number; action: string; digest: string; created_at: string };
    actor_attribution: 'shared local operator credential' }>;
  next_cursor: string | null;
}

export interface AuthorityPolicyOutcomesResponse {
  items: Array<{ candidate_id: string; root_task_id: string; manager_session_id: string;
    causal_event_id: string; causal_result_id: string | null; release_id: string | null; activation_id: string | null;
    activation_epoch: number | null; policy_version: string; policy_digest: string;
    prompt_id: string; prompt_version: string; prompt_digest: string;
    provider_id: string | null; executor_kind: string | null; model_id: string;
    model_version: string; model_digest: string; disposition: string | null;
    disposition_code: string | null; evaluation_created_at: string | null;
    evaluator_contract: { id: string; version: string; digest: string };
    terminal_hook_outcome: string | null; thread_id: string | null;
    envelope: null | { id: string; state: string; consumed_at: string | null };
    receipt_state: 'complete' | 'receipt_incomplete' }>;
  next_cursor: string | null;
}

export interface AuthorityPolicyTemplate {
  title: string;
  normative_text: string;
  clauses: AuthorityPolicyClause[];
  continuation_phrase: string;
}

export interface CreateAuthorityPolicyReleaseRequest extends AuthorityPolicyTemplate {
  based_on_release_id: string | null;
  request_id: string;
}

export interface CreateAuthorityPolicyReleaseResponse {
  release: TeamEscalationPolicyResponse['active'] extends infer _T ? {
    id: string; policy_id: string; version: number; title: string;
    normative_text: string; clauses: AuthorityPolicyClause[];
    continuation_phrase: string; digest: string; created_at: string;
    actor_attribution: 'shared local operator credential';
  } : never;
  activated: false;
  validation: { canonical: true; digest: string };
}

export function decodeTeamEscalationPolicyResponse(
  value: unknown,
): TeamEscalationPolicyResponse {
  if (
    !value ||
    typeof value !== 'object' ||
    (value as { can_mutate?: unknown }).can_mutate !== true ||
    !(value as { bootstrap_template?: unknown }).bootstrap_template ||
    typeof (value as { bootstrap_template: { title?: unknown } }).bootstrap_template.title !== 'string' ||
    typeof (value as { bootstrap_template: { normative_text?: unknown } }).bootstrap_template.normative_text !== 'string' ||
    !Array.isArray((value as { bootstrap_template: { clauses?: unknown } }).bootstrap_template.clauses) ||
    typeof (value as { bootstrap_template: { continuation_phrase?: unknown } }).bootstrap_template.continuation_phrase !== 'string'
  ) {
    throw new Error('Invalid team escalation policy response');
  }
  return value as TeamEscalationPolicyResponse;
}

export const getTeamEscalationPolicy = (
  slug: string,
  agentName: string,
): Promise<TeamEscalationPolicyResponse> =>
  request<unknown>(`/orgs/${slug}/agents/${agentName}/team-escalation-policy`)
    .then(decodeTeamEscalationPolicyResponse);

export const createTeamEscalationPolicyRelease = (
  slug: string,
  agentName: string,
  body: CreateAuthorityPolicyReleaseRequest,
): Promise<CreateAuthorityPolicyReleaseResponse> =>
  request(`/orgs/${slug}/agents/${agentName}/team-escalation-policy/releases`, {
    method: 'POST', body,
  });

export const activateTeamEscalationPolicyRelease = (
  slug: string,
  agentName: string,
  body: { release_id: string; expected_previous_epoch: number; request_id: string;
    action: 'activate' | 'reactivate_rollback'; acknowledge_shared_credential_attribution: true },
): Promise<unknown> => request(
  `/orgs/${slug}/agents/${agentName}/team-escalation-policy/activations`,
  { method: 'POST', body },
);

export const getTeamEscalationPolicyHistory = (slug: string, agentName: string, cursor?: string) =>
  request<AuthorityPolicyHistoryResponse>(`/orgs/${slug}/agents/${agentName}/team-escalation-policy/history?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);

export const getTeamEscalationPolicyOutcomes = (slug: string, agentName: string, cursor?: string) =>
  request<AuthorityPolicyOutcomesResponse>(`/orgs/${slug}/agents/${agentName}/team-escalation-policy/outcomes?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);

export const isEligiblePolicyManager = (agent: {
  name: string;
  team: string;
  role: string;
} | undefined): boolean =>
  // Structurally reusable seam; the current Engineering allowlist remains explicit.
  agent?.name === 'engineering_manager' &&
  agent.team === 'engineering' &&
  agent.role === 'manager';
