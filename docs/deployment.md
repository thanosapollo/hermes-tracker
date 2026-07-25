# Self-hosting Hermes Tracker

Hermes Tracker is a stateful ASGI service. Run one application instance against one SQLite database, bind it to loopback or a private service network, and publish only the read-only UI through a TLS reverse proxy.

## Runtime

Required settings:

```sh
HERMES_TRACKER_DB=/var/lib/hermes-tracker/hermes-tracker.db
HERMES_TRACKER_AUTH_TOKEN=replace-with-a-protected-random-token
```

Start the service on a private listener:

```sh
uvicorn hermes_tracker.app:app --host 127.0.0.1 --port 8000
```

For a subpath deployment, set `HERMES_TRACKER_ROOT_PATH` and configure the proxy to strip that prefix before forwarding.

## Reverse proxy

- Terminate TLS at the edge.
- Forward public read-only HTML routes and static assets.
- Deny public access to `/api`, `/api/`, `/api/v1/...`, and `/docs` unless explicitly required.
- Permit mutation API traffic only from trusted networks or authenticated service-to-service routes.
- Keep application bearer authentication enabled behind the network policy.
- Set request-size, timeout, and rate limits appropriate to public traffic.
- Preserve the real client address only through a trusted-proxy configuration.

## Secrets

Provide `HERMES_TRACKER_AUTH_TOKEN` through a root- or service-owned environment file, secret manager, or equivalent protected mechanism. Do not place token values in Nix expressions, container images, unit files, shell history, logs, or Git.

Use individually revocable scoped credentials for external agents. See [`external-agent-access.md`](external-agent-access.md).

## State and backups

- Keep the SQLite database on persistent local storage.
- Stop the service before copying the database directly, or use SQLite's online `.backup` command while it runs.
- Store backups outside the live state directory.
- Restrict backup permissions.
- Run `PRAGMA integrity_check` on backup copies.
- Test restoration before relying on the backup policy.

Schema migrations are additive and run during application startup. Back up state before upgrading.

## Upgrade and rollback

1. Pin an immutable source revision or package version.
2. Back up and verify the database.
3. Build the candidate before replacing the running service.
4. Restart only the Tracker service.
5. Verify health, public reads, authenticated mutations, and expected data.
6. Keep the previous package and verified database backup until acceptance passes.

Application rollback and database restoration are separate operations. Never delete current state merely to roll back the executable.

## Smoke checks

```sh
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/
```

Also verify externally that public HTML GET/HEAD succeeds, UI mutation methods fail, and API routes are denied by the proxy. From a trusted API client, verify requests without a valid bearer token fail.
