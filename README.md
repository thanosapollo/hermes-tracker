# Hermes Tracker

Small issue tracker for software projects. It provides repositories, numbered issues, comments, labels, priority, assignee, search, and `open|closed` state. Public pages are read-only; agents mutate through the authenticated HTTP API or optional stdio MCP adapter.

Tracker is not a forge, Kanban board, CI service, deployment controller, or agent runtime.

## Agent interface

Install the `mcp` extra and launch `hermes-tracker-mcp` with an agent token in its environment. The adapter provides repository/label discovery, issue search/read/create/update, comments and explicit reopen. Writes use caller-owned retry keys; edits require the latest issue revision. It exposes no execution or Git-close tools.

See [MCP setup and interaction contracts](docs/mcp.md) for configuration, tool inputs, pagination, errors and safe retries. The HTTP API remains the authority and preserves compatibility with existing clients.

## Git closure

A dedicated Gitolite hook credential calls `POST /api/v1/git-closures` automatically after an accepted canonical-ref push. Tracker atomically closes an open issue, stores a dedicated closure record with full commit/source provenance, and stores an idempotency receipt. Public UI renders only `Closed in commit <hash>.` with the hash linked to cgit. Missing and already-closed targets are safe no-ops.

Existing `verification_output` remains durable API evidence across updates, Git closure, and reopen; it is not part of the public closure line.

Signed canonical commits use explicit `Fix|Close|Resolve` variants with `#N` or `repo-slug#N`. Ordinary mentions are inert. No LLM or manual reconciliation step exists. Hook parsing and deployment are separate from Tracker runtime.

Ordinary credentials have only `read`, `repo:manage`, `todo:create`, `todo:update`, and `comment:create`; they cannot assert Git authority.

## References in descriptions and comments

Use `commit:AbC1234` to link to the current repository's cgit commit view. The prefix is lowercase; hashes accept 7–64 hexadecimal characters (abbreviated or full SHA-1/SHA-256), preserving their exact spelling. Use standalone references separated from identifiers, paths, and Git revision suffixes; surrounding prose punctuation is allowed. Links do not verify object existence or type.

`tree:HASH` stays literal: cgit's tree view requires commit context, not a raw tree object ID. A local candidate tree is not a published commit; use an authored Markdown URL with a published commit ID to link its tree view.

Bare hashes and dates remain plain text. Markdown links keep their authored URLs; inline/fenced code and escaped references such as `\commit:AbC1234` stay literal. Links require a configured cgit base. Each comment's timestamp is a stable permalink.

## Run

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra dev
HERMES_TRACKER_DB=/tmp/hermes-tracker.db \
HERMES_TRACKER_AUTH_TOKEN=development-only-token \
HERMES_TRACKER_CGIT_BASE=https://git.example.org \
  uv run uvicorn hermes_tracker.app:app --host 127.0.0.1 --port 8000
```

Configuration:

- `HERMES_TRACKER_DB`: SQLite path; default `hermes_tracker.db`.
- `HERMES_TRACKER_AUTH_TOKEN`: administrative client token.
- `HERMES_TRACKER_ROOT_PATH`: optional reverse-proxy mount prefix.
- `HERMES_TRACKER_CGIT_BASE`: valid HTTPS cgit base; Git closure fails closed without it.

Each request owns one SQLite connection. Runtime accepts only the fresh reduced schema; existing installations require the separate offline converter before upgrade.

Private OpenAPI and Swagger routes are `/api/v1/openapi.json` and `/api/v1/docs`.

## Credentials

```sh
hermes-tracker-token issue --actor example-agent \
  --scopes read,todo:create,todo:update,comment:create
hermes-tracker-token list
hermes-tracker-token revoke --actor example-agent
```

Protect raw tokens. The Git closer receives a separate token with only `git:close`.

## Verify

```sh
uv sync --extra dev --extra mcp
uv run python -m pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/hermes_tracker
uv build --wheel --sdist
```

See [`docs/deployment.md`](docs/deployment.md), [`CONTRIBUTING.md`](CONTRIBUTING.md), and [`SECURITY.md`](SECURITY.md).
