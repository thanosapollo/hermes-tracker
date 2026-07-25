# External-agent access

Tracker supports individually revocable bearer credentials for trusted external agents. The configured `HERMES_TRACKER_AUTH_TOKEN` remains a full-scope compatibility/admin credential; do not distribute it to external agents.

## Credential operations

Run these commands on an operator-controlled host with `HERMES_TRACKER_DB` set to the deployed database path:

```sh
hermes-tracker-token issue --actor codex --scopes read,todo:create,comment:create
hermes-tracker-token list
hermes-tracker-token revoke --actor codex
```

`issue` prints the raw token exactly once. Store it only in the client’s protected environment/config. Tracker stores an scrypt verifier, a non-secret token prefix, scopes, timestamps, and the server-owned actor; listing never prints raw tokens or verifiers. Valid scopes are `read`, `repo:manage`, `todo:create`, `todo:update`, `todo:claim`, `todo:close`, `comment:create`, and `todo:link`.

Agent identity is derived from its credential. Agent supplied `X-Actor` and comment author fields are ignored for attribution. Master-token calls retain legacy caller attribution for existing trusted integrations.

## MCP

The maintained stdio wrapper uses only Tracker's versioned REST API, never SQLite or Kanban internals. Set `HERMES_TRACKER_TOKEN` and optionally `HERMES_TRACKER_BASE_URL`, then register the command `hermes-tracker-mcp` with a generic stdio MCP client or Codex MCP configuration. It exposes structured list/get/create/update/comment/link/claim/close tools and sends no actor override.

## Durable events

Selected mutations append a Tracker event in the same transaction: assigned TODOs, claims, closures, and comments containing `@tracker`. Routine edits do not wake consumers. A consumer polls `GET /api/v1/events?consumer=<stable-name>&after=<id>` and idempotently acknowledges each event with `POST /api/v1/events/<id>/ack?consumer=<stable-name>`. Each named consumer has its own delivery/ack state; offline consumers receive retained unacknowledged events later. Tracker never requires any agent runtime to be online.

This is a durable TODO ledger extension, not a public Kanban API, OAuth service, or in-process Hermes integration. The separate plugin consumer owns how selected events are surfaced.
