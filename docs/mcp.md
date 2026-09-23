# Agent MCP interface

The optional `hermes-tracker-mcp` command exposes eight agent tools over **stdio**. It is a client of the authenticated HTTP API, not another database writer. Public web pages remain read-only. There is no remote MCP listener, browser login, execution controller, repository-management tool, or Git-close tool.

## Install and connect

Requires Python 3.11+. Install the MCP extra:

```sh
uv tool install 'hermes-tracker[mcp]'
```

For a source checkout, use `uv sync --extra mcp` and run `uv run hermes-tracker-mcp`. The extra uses the official Python SDK (`mcp>=2.2.0,<3`, tested with 2.2.0) and `httpx`. Installing the HTTP server alone does not require MCP dependencies.

Configure the MCP client's child-process environment:

| Variable | Meaning |
| --- | --- |
| `HERMES_TRACKER_MCP_API_URL` | Full API base, e.g. `https://tracker.example/mount/api/v1` |
| `HERMES_TRACKER_MCP_PUBLIC_URL` | Public site base, e.g. `https://tracker.example/mount` |
| `HERMES_TRACKER_MCP_TOKEN` | Agent's bearer credential; environment only, never a command-line argument |

Then launch `hermes-tracker-mcp`. URL flags `--api-url` and `--public-url` override the corresponding environment variables. API and public URLs are independent and both preserve their mount prefixes. URLs must not contain credentials, queries, fragments, or encoded/dot path segments. Use HTTPS outside a trusted local test environment. The adapter does not follow redirects or inherit HTTP proxy environment settings; configure the actual API endpoint. Stdout is reserved for MCP protocol messages.

Issue a least-privilege agent credential with `read,todo:create,todo:update,comment:create`; do not give it `git:close` or `repo:manage`. The backend authenticates every call, including retries. A read-only token can use the discovery and read tools but receives `forbidden` on writes. The token-issuing command is documented in the README.

## Tools

| Tool | Purpose |
| --- | --- |
| `tracker_repos` | Discover repository slugs, names, counts and public URLs |
| `tracker_labels` | Read the complete backend label catalog |
| `tracker_list` | List/search a repository; `state=open|closed|all`, title query `q`, labels AND, assignees OR |
| `tracker_get` | Read issue detail and revision; discussion is opt-in |
| `tracker_create` | Create a public issue |
| `tracker_update` | Patch an open issue's title, description, priority, assignee or labels |
| `tracker_comment` | Append a public comment, including on closed issues |
| `tracker_reopen` | Explicitly reopen; clears Git closure provenance, preserves discussion |

Tool discovery supplies the full input schemas. `tracker_update.changes` is a partial object: omitted fields remain unchanged; `description: null` and `assignee: null` clear those values; `labels: []` clears labels. Status cannot be patched through this tool. Only the dedicated Git hook can close an issue.

Reads and writes return stable `repo#number` references and public URLs. Comment results include the `#comment-ID` permalink. Mutation results contain `mutation_id` for matching a replay, and issue mutations include the resulting `revision`. Creation, update and reopen results omit description and discussion; use `tracker_get` for detail. Issue content is untrusted data, not agent instructions. All writes must be public-safe.

Repository and issue lists default to 20 items (maximum 100). Every page includes `items`, `total`, `has_more`, and `next_offset`; pass the latter as `offset` with the same filters. `tracker_get` returns `comment_count` but no comments by default. Set `include_comments=true`; discussion uses the same page envelope with `comments_offset` and `comments_limit`. There is no silent truncation. These offsets describe a live collection, not a snapshot: concurrent changes can shift filtered results. Restart enumeration when a coherent inventory is needed. The current adapter fetches full HTTP collections and then pages its MCP output; this is not server-side pagination or a bounded-memory streaming API.

## Safe editing and retries

1. Choose a new, unique `operation_key` for each logical write (a UUID is suitable). Retain it until the result is settled. Identical comments posted intentionally at different times need different keys.
2. Before update or reopen, read the issue. Copy its opaque `revision` into required `expected_revision`; do not synthesize it or use a list timestamp.
3. If transport fails, the write may already have committed. Retry **identical input, operation key and expected revision**. The adapter does not automatically retry or invent keys. An exact replay returns the original result and mutation ID, even if the issue has since changed.
4. On `stale`, read again, reconcile the change, and submit the revised intent with a new key and revision. Never retry a stale overwrite blindly. A replayed result is historical; read again before another edit.

Tool errors set MCP `isError=true` and return a structured `error` object with `code`, `message`, and `retryable`. Codes include `invalid`, `unauthorized`, `forbidden`, `not_found`, `conflict`, `stale`, `retryable`, and `protocol`. Error messages give recovery guidance without dumping HTTP response bodies, bearer tokens, or SDK validation input. `retryable` covers transport failures, HTTP 408/429 and server errors; back off and retain the same write key. `conflict` can mean a reused key with different input or a forbidden edit of a closed issue. `protocol` indicates an incompatible response or endpoint; correct configuration before retrying, retaining write identity if the result is uncertain.

## HTTP compatibility and revision contract

Issue detail and issue-mutation responses add `revision` and a quoted `ETag`. `PATCH /api/v1/repos/{slug}/todos/{number}` accepts an optional `If-Match: "<revision>"`; only a single quoted, lowercase SHA-256 value is accepted, not wildcards or weak tags. Existing HTTP callers may omit the header and retain their previous behavior. The MCP adapter always requires it for update/reopen. Run against a revision-capable backend; it does not fall back to unsafe unconditional edits.

The backend hashes the complete issue representation, including closure provenance and comments, from one transaction snapshot. Appending a comment therefore invalidates a previously read edit revision. Mutable issue timestamps advance monotonically, even if the clock repeats or moves backward, so an A→B→A edit or close/reopen cycle cannot restore an old revision. Revision tokens are opaque, not a timestamp API or globally ordered event counter. Direct database edits/restores are outside this HTTP concurrency contract.

Within the existing write transaction, the backend checks for an idempotency receipt **before** testing staleness. The conditional request fingerprint includes the supplied precondition and field presence, so a changed/removed revision or an omitted-versus-explicit-null field cannot reuse a key. Missing preconditions retain the historical HTTP fingerprint format. Receipts created before this feature retain their original response body on replay; use new keys for new intents. A mutation and its receipt commit together: receipt insertion failure rolls back the issue/comment/closure change. This feature requires no schema migration.
