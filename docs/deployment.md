# Self-hosting Hermes Tracker

Run one service against one local SQLite database. Bind to loopback/private network. Expose only read-only HTML and static routes publicly; keep `/api` and `/docs` private.

```sh
HERMES_TRACKER_DB=/var/lib/hermes-tracker/hermes-tracker.db
HERMES_TRACKER_AUTH_TOKEN=protected-random-token
HERMES_TRACKER_CGIT_BASE=https://git.example.org
uvicorn hermes_tracker.app:app --host 127.0.0.1 --port 8000
```

Store admin, ordinary-client, and Git-closer credentials separately in protected service configuration. Configure a valid HTTPS cgit base before enabling Git closure. Never put tokens in source, command arguments, logs, or public issues.

## Upgrade

Reduced runtime accepts only its exact fresh schema. Migration/conversion is an offline deployment step, not automatic startup behavior.

1. Pin package revision.
2. Back up SQLite and verify `PRAGMA integrity_check` on copy.
3. Run approved offline converter when upgrading existing state.
4. Build before switching.
5. Verify `/health`, public pages, private authenticated API, issue data, and one ordinary mutation.
6. Keep previous package and backup until acceptance passes.

## Gitolite closure boundary

Hook/helper deployment is separate. Managed `post-receive` code must:

- process only configured canonical refs after accepted pushes;
- validate fast-forward ranges and ignore deletion/initial-history imports;
- parse commit messages without shell evaluation;
- call `/api/v1/git-closures` with dedicated `git:close` credential and stable idempotency key;
- retain failed exact ranges for replay without rolling back accepted Git pushes.

Tracker stores source repository, ref, full SHA, closure actor, and timestamp in a dedicated closure record. Public UI renders only linked `Closed in commit <hash>.` text. Hook receives no SQLite or administrative access.

After activation, verify duplicate delivery, missing/already-closed no-op, cross-repository target, and one canonical `Closes #N` journey.
