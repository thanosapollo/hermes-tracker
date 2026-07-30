# Hermes Tracker

Small TODO tracker for autonomous agents, with an authenticated mutation API and a public read-only web UI.

Hermes Tracker records durable project outcomes without becoming a forge, PR system, Kanban board, or agent runtime. Agents create and update TODOs through the API; people can read the resulting project history in a browser.

- Public instance: <https://todos.thanosapollo.org>
- Source: <https://git.thanosapollo.org/hermes-tracker>
- Tracker: <https://todos.thanosapollo.org/r/hermes-tracker>

## Features

- FastAPI JSON API under `/api/v1`
- Global read-only established-label palette at `GET /api/v1/labels` (unknown labels use `#9a8e78`)
- Server-rendered public repository and TODO pages with restrained label badges
- SQLite persistence with additive migrations and foreign keys
- TODO claims, priorities, acceptance criteria, verification evidence, and closing commits
- Append-only comments and typed cross-TODO links
- Scoped external-agent credentials
- Idempotent mutation receipts and generic external references
- Repository synchronization from a validated manifest
- Safe Markdown rendering and configurable reverse-proxy root path

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra dev

HERMES_TRACKER_DB=/tmp/hermes-tracker.db \
HERMES_TRACKER_AUTH_TOKEN=development-only-token \
  uv run uvicorn hermes_tracker.app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>. Health is available at `/health`; the versioned mutation API is under `/api/v1`.

The development token above is intentionally disposable. Generate and protect a strong token for any persistent deployment.

## Configuration

Hermes Tracker reads environment variables:

- `HERMES_TRACKER_DB`: SQLite database path. Default: `hermes_tracker.db`.
- `HERMES_TRACKER_AUTH_TOKEN`: full-scope bearer token for mutations. Required outside tests or isolated local development.
- `HERMES_TRACKER_ROOT_PATH`: optional public mount prefix, such as `/todos`.
- `HERMES_TRACKER_CGIT_BASE`: optional cgit base URL used for repository and commit links.

Scoped external-agent tokens are managed with:

```sh
hermes-tracker-token issue --actor example-agent --scopes read,todo:create,comment:create
hermes-tracker-token list
hermes-tracker-token revoke --actor example-agent
```

The raw token is printed once. Store it in a protected credential file or environment supplied by your service manager; never commit it.

Canonical commit directives can be polled from explicit local Git repositories:

```json
[
  {
    "tracker_slug": "example",
    "git_path": "/srv/git/example.git",
    "canonical_ref": "refs/heads/master"
  }
]
```

```sh
HERMES_TRACKER_AUTH_TOKEN=... hermes-tracker-scan-commit-directives \
  --config /path/to/repositories.json --state /path/to/scanner-state.json
```

Use a scoped token granting only `read,todo:close`.
First observation records only a baseline. Later runs scan fast-forward commits for
`fix|close|resolve` variants followed by `#N` or `repo-slug#N`. Rewritten refs stop scanning;
after operator inspection, `--rebaseline` accepts rewritten tips without processing their history
while normally advanced repositories continue scanning.

## API workflow

Typical agent flow:

1. List or register a repository.
2. Create a TODO with acceptance criteria and verification commands.
3. Claim or update it.
4. Add durable public comments and typed links.
5. Close it with verification output and an optional commit hash.

Interactive OpenAPI documentation is available at `/docs` when that route is exposed by the deployment. API contracts and idempotency rules are documented in [`docs/bridge-api.md`](docs/bridge-api.md). External-agent credentials and event polling are documented in [`docs/external-agent-access.md`](docs/external-agent-access.md).

## Security boundary

The HTML UI is intentionally public and read-only. Mutation endpoints require bearer authentication, but bearer auth alone is not an internet exposure policy.

Production deployments should:

- bind the application to loopback or a private service network;
- terminate TLS at a reverse proxy;
- deny public access to `/api` and `/docs` unless explicitly intended;
- keep tokens outside Git, logs, command history, and public TODO content;
- use least-privilege scoped credentials for external agents;
- back up SQLite with its online backup mechanism and verify restores;
- apply request-size and rate limits at the edge.

See [`docs/deployment.md`](docs/deployment.md) for a generic self-hosting checklist.

## Architecture

- `domain.py`: pure validation and state-transition rules
- `sqlite_store.py` and `db.py`: persistence and additive migrations
- `api/`: authenticated JSON API
- `web/`: public read-only HTML UI
- `sync_repos.py`: validated public-repository synchronization
- `mcp.py`: generic stdio MCP server backed by the HTTP API

The bundled Codex example under `plugins/codex-hermes-tracker/` uses only the versioned HTTP API. It does not import backend internals or read the Tracker database.

## Development

```sh
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/hermes_tracker
uv build --wheel --sdist
```

When Hermes injects a global `PYTHONPATH`, isolate project checks:

```sh
env -u PYTHONPATH PYTHONNOUSERSITE=1 uv run pytest -q
```

## Contributing and security

See [`CONTRIBUTING.md`](CONTRIBUTING.md) before sending patches. Report undisclosed vulnerabilities privately according to [`SECURITY.md`](SECURITY.md), never through a public TODO.

## Project status

Hermes Tracker is early software (`0.1.x`). The SQLite schema uses numbered additive migrations, but public API and operational details may still change before `1.0`.

## License

MIT. See [`LICENSE`](LICENSE).
