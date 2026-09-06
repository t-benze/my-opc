import { beforeEach, describe, expect, expectTypeOf, it, vi } from 'vitest';
import {
  decodeTeamEscalationPolicyResponse,
  getTeamEscalationPolicy,
  type TeamEscalationPolicyResponse,
} from './authorityPolicy';

vi.mock('./client', () => ({ request: vi.fn() }));
import { request } from './client';

const empty = {
  team: 'engineering',
  target_manager: 'engineering_manager',
  can_mutate: true,
  bootstrap_required: true,
  bootstrap_template: {
    title: 'Policy', normative_text: 'Text', clauses: [],
    continuation_phrase: 'server-authored phrase',
  },
} as const;

describe('team escalation policy response contract', () => {
  beforeEach(() => vi.mocked(request).mockReset());

  it('narrows can_mutate to literal true for immutable release creation', async () => {
    expectTypeOf<TeamEscalationPolicyResponse['can_mutate']>().toEqualTypeOf<true>();
    vi.mocked(request).mockResolvedValue(empty);

    await expect(getTeamEscalationPolicy('alpha', 'engineering_manager'))
      .resolves.toEqual(empty);
  });

  it('rejects a server response that withdraws release creation', () => {
    expect(() => decodeTeamEscalationPolicyResponse({
      ...empty,
      can_mutate: false,
    })).toThrow('Invalid team escalation policy response');
  });

  it('rejects a response that omits the server-authored bootstrap authority', () => {
    const { bootstrap_template: _omitted, ...withoutTemplate } = empty;
    expect(() => decodeTeamEscalationPolicyResponse(withoutTemplate))
      .toThrow('Invalid team escalation policy response');
  });
});
