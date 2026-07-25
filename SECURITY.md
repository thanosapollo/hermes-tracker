# Security policy

Hermes Tracker exposes a public read-only UI and a private authenticated mutation API. Treat every TODO field, comment, author, command, commit reference, and repository record as public content.

## Reporting a vulnerability

Do not open a public TODO for an undisclosed vulnerability. Email `bugs@thanosapollo.org` with subject `[SECURITY hermes-tracker] ...` and include the affected version, reproduction, and impact. Never send real credentials or production data.

## Supported version

Security fixes target current `master` until a stable release series is announced.

## Deployment baseline

- Bind the application to loopback or a private service network.
- Terminate TLS at a reverse proxy.
- Deny public access to mutation and interactive API routes.
- Keep bearer authentication enabled behind network controls.
- Use scoped, revocable agent credentials.
- Keep tokens and SQLite state outside source control.
- Back up and integrity-check persistent state before upgrades.

See [`docs/deployment.md`](docs/deployment.md) for the generic self-hosting checklist.
