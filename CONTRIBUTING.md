# Contributing

Hermes Tracker favors small, auditable changes and behavior-focused tests.

Changes must improve filing, finding, discussing, assigning, or closing issues. Tracker is not a forge, Kanban board, CI service, deployment controller, or agent runtime. Git closure is its sole automatic lifecycle integration.

## Before sending a patch

```sh
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/hermes_tracker
uv build --wheel --sdist
```

Keep backend code independent from Hermes, Gitolite, cgit, and other agent runtimes. Integrations use the versioned HTTP API and never read Tracker's SQLite database.

Send patches to `patches@thanosapollo.org` with subject `[PATCH hermes-tracker] ...`. Report bugs and feature requests through the [public Tracker](https://todos.thanosapollo.org/r/hermes-tracker) or `bugs@thanosapollo.org` with subject `[BUG hermes-tracker] ...`.

Do not include credentials, private infrastructure, local paths, raw logs, generated databases, agent plans, or build artifacts in patches or public TODOs.
