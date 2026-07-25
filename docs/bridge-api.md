# Bridge-safe API contract

Hermes Tracker remains independent of Hermes and Kanban. An authenticated client may attach generic external references and may identify its actor with `X-Actor`; the backend stores that actor only with an idempotency receipt and does not derive or enforce profile identity.

## Idempotent mutations

The following `POST /api/v1` mutations accept `Idempotency-Key` and `X-Actor` headers:

- create TODO;
- append comment;
- create typed TODO link;
- claim TODO;
- close TODO.

Every successful covered mutation returns `X-Mutation-ID`, including requests without an idempotency key. For a keyed request, the operation scope, canonical request body, and actor are hashed. Repeating the same operation and key with identical input returns the original HTTP status, response body, and mutation ID without another side effect. Reusing it with different input returns `409 Conflict`.

The side effect and completed receipt are committed in one SQLite transaction. `BEGIN IMMEDIATE` serializes keyed mutations before checking the receipt, so concurrent identical retries cannot both create the side effect. Keys are scoped to the concrete operation and TODO/repository identity; callers may reuse a key for an unrelated operation, but globally unique keys are recommended.

## External references

`POST /api/v1/repos/{slug}/todos/{number}/external-references` creates a reference with:

- `system`: generic source system, for example `hermes-kanban`;
- `external_id`: stable identifier in that system;
- optional `url`;
- optional string metadata.

`(system, external_id)` is globally unique and can belong to only one TODO. References are returned by the corresponding `GET .../external-references` endpoint and in TODO detail as `external_references`.

For a narrowly scoped legacy repair, `PATCH /api/v1/repos/{slug}/todos/{number}/external-references/{reference_id}/kanban-board-identity` accepts `{"board": "<board>"}`. It only operates on one `hermes-kanban` reference whose `external_id` is a canonical Kanban task ID and whose metadata has no `board`. It preserves all other metadata. Repeating the same board returns the existing reference without a write; a different pre-existing board returns `409 Conflict`. Clients must verify the target Kanban card before calling this endpoint and read the TODO back afterward.

The plugin-consumable machine fixture is [`contracts/plugin-bridge-v1.json`](contracts/plugin-bridge-v1.json). Generated OpenAPI at `/openapi.json` remains authoritative for field validation.

## Database migration and rollback

Startup applies numbered additive migrations recorded in `schema_migrations`. The original v1 tables and columns are not rewritten. The new tables are `mutation_receipts`, `external_references`, and `repository_aliases`, so an older binary can open the upgraded database and continue reading its original data after rollback (it simply ignores the new tables).

When an existing repository row has the legacy slug `agentum-todos`, migration adds `hermes-tracker` as its canonical alias without changing the row or TODO foreign keys. New API/list/public links expose `hermes-tracker`; `/r/agentum-todos` and its TODO paths return permanent `308` redirects. Public repository sync sees only the canonical slug and therefore does not create a second tracker repository when cgit changes names.

Before production migration, retain the existing SQLite backup procedure in `docs/deployment.md`. Rolling back the executable does not require rolling back the database because all schema changes are additive and the stored legacy repository slug remains present.
