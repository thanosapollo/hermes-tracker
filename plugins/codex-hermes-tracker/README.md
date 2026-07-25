# Codex Hermes Tracker plugin

Example Codex plugin exposing Hermes Tracker's versioned HTTP API as local stdio MCP tools.

## Configuration

Set a scoped Tracker credential:

- `HERMES_TRACKER_TOKEN`: token value, or
- `HERMES_TRACKER_TOKEN_FILE`: mode-`0600` file containing the token. Default: `~/.config/hermes-tracker/token`.

Optional settings:

- `HERMES_TRACKER_URL`: API origin. Default: `http://127.0.0.1:8000`. Remote origins must use HTTPS.
- `HERMES_TRACKER_HOST`: explicit HTTP `Host` header for a reverse-proxy route.
- `HERMES_TRACKER_ACTOR`: public mutation identity. Default: `codex`.

The plugin does not read Tracker's SQLite database or import backend internals. Tracker comments are public project history: never send secrets, hidden reasoning, private paths, raw logs, or transient progress.
