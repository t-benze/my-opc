# Team escalation policy operator contract

The web surface is currently available only for the roster-confirmed
`engineering/engineering_manager/manager` tuple. The server is authoritative;
workers and every other manager receive the same `policy_surface_not_available`
404 and their Agent response and DOM contain no policy surface.

`GET .../team-escalation-policy/history` and `/outcomes` accept an opaque
server cursor and `1 <= limit <= 50`. The first request omits `cursor`; its
response cursor binds every later page to that initial snapshot and to a
deterministic newest-first keyset. Rows inserted after page one therefore do
not shift, duplicate, or hide rows in the in-progress traversal.
The UI keeps independent cursors for the two lists and exposes keyboard-native
Load more controls, explicit loading/error/empty/end states, and lossless
append of each server page. A later-page failure preserves every loaded row and
leaves an independent keyboard-native retry for that stream's failed cursor;
retry appends the page once without resetting or duplicating earlier rows.
History omits policy prose and prompts. Outcomes show only durable identity
pins and causal task/result/session/thread/hook/envelope receipts. Missing or
corrupt joins are `receipt_incomplete`, never inferred success. Raw evaluator
responses, rationale, proposed reason, prompts, policy content, credentials,
and secrets are never projected.

Save creates an immutable inactive release. Save & activate remains an explicit
founder-authorized action. The authenticated route independently revalidates
the exact Engineering-manager allowlist and closed request contract, then uses
the transaction-owning store's immutable release/team linkage, request replay,
idempotency, audit, and expected-previous-epoch CAS fences. A first activation becomes epoch 1
`bootstrap`; a rollback creates a new `reactivate_rollback` epoch pointing to
an older immutable release. Conflicts leave the saved release inactive and
require a refresh. All mutations truthfully attribute only the `shared local
operator credential`.

Landing this code, redeploying it, and creating/activating a production policy
are distinct events. This delivery does
not create or activate a production policy and does not deploy anything.
