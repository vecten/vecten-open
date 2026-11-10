# MCP tool reference

Use these [MCP](GLOSSARY.md) tools to inspect warehouse definitions, query data and report
incorrect answers. Connect a client using [the identity guide](mcp-identity.md); the server
resolves the current [principal](GLOSSARY.md) and groups on every list and call.

All authenticated people and services can discover `describe`, `search`, `sql-exec`,
`query_metrics`, `report_feedback` and `verify_receipt`. Warehouse grants still control data
access. Only people with the admin flag see or can call the admin tools below. A group named
`admin` does not grant that flag, and service principals never receive admin tools.

The examples below are tool arguments: choose the named tool in your MCP client and pass the
JSON object. They are not shell commands. Every invocation carries a [receipt](GLOSSARY.md)
in its MCP response metadata at `_meta["vecten/receipt"]`, including errors. Query results
and metric refusals also carry that receipt in their response body. See
[audit storage and delivery limits](mcp-audit.md) for persistence and retention details.

## Discover definitions

The loaded model is the server’s current validated set of dataset and metric definitions
from the project’s shipped `osi/` files; see [model loading](semantic-model.md).
Call `describe` with `{}` for that model, or `{"dataset":"orders"}` for one dataset.
The result contains `model` (the model revision), `datasets`, `metrics` and `relationships`.
Fields include descriptions, `ai_context` (author-provided synonyms and examples), their SQL
expressions and their classification: `dimension`, `time_dimension` or `fact`.
See [model authoring](semantic-model.md) for how those definitions are made.

Call `search` with `{"query":"turnover"}`. It matches all whitespace-separated search words,
case-insensitively, against names, descriptions and `ai_context`, and returns up to 100 entries
under `results`. It reads the loaded model backed by server state, without querying warehouse
data; matching works the same way on Postgres and Snowflake.

Both tools omit denied fields, derived fields and metrics that depend on them, and
relationships using them. Source definitions, descriptions and author-provided context that
mention denied fields are omitted from discovery; the stored model remains unchanged.
Executed query SQL, its provenance and stored receipts keep their original representation. Column denial hides model metadata; it is not a warehouse column grant. Use warehouse
permissions to enforce access to actual data. A missing or invalid initial model produces a
tool error; a failed later load keeps the last valid model available.

## Query a metric

Call `query_metrics`, for example:

```json
{
  "metrics": ["total_revenue"],
  "dimensions": ["country"],
  "time": {"dimension": "order_date", "grain": "month", "start": "2026-01-01", "end": "2027-01-01"},
  "filters": [{"field": "status", "op": "in", "value": ["paid", "shipped"]}],
  "order_by": [{"field": "total_revenue", "dir": "desc"}],
  "limit": 500,
  "question": "What was monthly paid or shipped revenue by country in 2026?"
}
```

Only `metrics` is required. Every metric, dimension and filter must belong to one
[home dataset](GLOSSARY.md); values are bound separately from the SQL. A compiled answer
carries `rows`, the SQL that ran, the definitions it used, each metric's
[additivity](GLOSSARY.md) with the row operations it allows, and a receipt. A request the
model cannot answer exactly is refused with a `reason`, `context` and a SQL `skeleton` to
finish with `sql-exec`, which the audit links to the refusal. The
[metric compiler reference](query-metrics.md) lists every request rule, operator, response
field and refusal reason, the limits and the Snowflake week-start difference.

## Run a read statement

Call `sql-exec` with `{"sql":"SELECT country FROM marts.orders","question":"Which countries?"}`.
Optional `parameters` is an array: use `%s` placeholders on Postgres and `?` on Snowflake.
Write SQL as you would run it: a literal `%` in a pattern or a modulo needs no escaping.
Never put credentials in tool arguments.

Exactly one `SELECT`, read-only `WITH`, `SHOW`, `DESCRIBE` or `EXPLAIN` is accepted. Writes,
multiple statements, `SELECT ... INTO`, writes hidden in a `WITH`, and all `EXPLAIN ANALYZE`
forms are refused before opening a warehouse connection. `EXPLAIN` without `ANALYZE` is
allowed. Queries run under the caller's warehouse identity and group roles. Postgres enforces
read-only transactions; Snowflake explicitly selects group roles instead of the adopted
user's default or secondary roles.

Both query tools use these settings. A signed-in human admin can change them with
`POST /api/admin/settings/set`, passing e.g. `{"key":"row_cap","value":500}`; see
[admin authentication](mcp-identity.md) for bearer-token access:

| Setting | Default | Allowed range |
| --- | --- | --- |
| `row_cap` | 1,000 | 1–10,000 |
| `statement_timeout_seconds` | 30 | 1–3,600 |

The executor prefixes SQL with `/* vecten <receipt id> */` and also sets Snowflake's query tag.
Results contain rows, columns, the executed bound SQL, its parameterized template, parsed
references and receipt. The audit records the SQL hash, tables, columns, row count and model
revision. Timeout failures retain their receipt and use audit error class `timeout`; driver
error messages are not returned. Cancellation waits for the bounded worker to finish so its
query evidence is recorded before the invocation's audit is finalized.

## Report an incorrect answer

Call `report_feedback` with all six fields:

```json
{
  "question": "What was last month's revenue?",
  "expected": "Revenue excluding refunds",
  "returned": "The answer included refunded orders",
  "tool_calls": [{"name": "query_metrics", "arguments": {"metrics": ["total_revenue"]}}],
  "category": "wrong definition",
  "raised_by": "user"
}
```

Categories are exactly `wrong value`, `missing metric`, `missing dataset`, `wrong definition`
and `access denied`. `raised_by` is `user` or `agent`; the authenticated principal is recorded
separately, so the caller cannot impersonate another reporter.

The response is `{"id":"<report id>","status":"accepted"}`. Each principal can submit 10
reports per 300-second window, shared across its sessions. Further reports are refused and
audited. The limiter is local to this server process; restarting it resets the window.

Reports are stored as untrusted evidence: question, expected and returned text are capped at
4,096 characters; at most 20 tool calls are retained as typed JSON text capped at 4,096
characters each. The `redact` setting defaults to `false` and masks report evidence when
stored. Set it using the same settings API with `{"key":"redact","value":true}`. A gap is a tracked
problem with a status and evidence, distinct from an individual report: reports and the audit's
implicit signals fold into gaps, and the reporter hears the outcome on their next session. The
[feedback loop](feedback-loop.md) page says which activity creates a gap and how one is resolved.

## Verify a receipt

Call `verify_receipt` with `{"receipt_id":"<id from a query response>"}`. People and services
can read their own receipts; human admins can read any. Another principal's retained receipt
returns `status: "not_found"`, including when that row is past retention.

A found receipt returns `status: "found"`, `question`, `tool`, `sql`, `resolution`,
`model_commit`, `person` (principal id), `time` (Unix seconds), and `compiles_identically`:

- `true`: its metric request compiles to the same bound SQL hash under the current model,
  caller's deny policy and row cap.
- `false`: compilation now refuses the request or produces a different hash.
- `null`: no comparison is possible, for example raw SQL, redacted arguments or no live model.

Verification never executes SQL and does not attest that warehouse data is unchanged.
Original SQL is the stored audit representation, which may contain redaction placeholders.
When `redact` is enabled, the returned question is masked even for older records.

Retention defaults to 90 days. An owned or admin-readable row past retention returns
`status: "expired"`. When no row remains, the timestamp encoded in its receipt id determines
`expired` versus `not_found`; this does not prove that the id was ever issued. A recent receipt
may be missing while the asynchronous audit writer is pending, or after a delivery loss.

## Human admin tools

Admin reads return a `data` envelope. They share a service with corresponding admin API reads;
`list_users` combines people and services, while API `/users` and `/services` separate them.

| Tool | Arguments | Result under `data` |
| --- | --- | --- |
| `list_gaps` | Optional `status`: `open` (default), `triaged`, `dismissed`, `resolved`, `all` | Up to 100 stored gaps |
| `read_gap` | `gap_id` | One gap and its evidence |
| `list_users` | `{}` | People and service principals, with no credentials |
| `list_groups` | `{}` | Group names and warehouse role names |
| `usage` | Optional `days`: integer 1–90, default 7 | Query-call totals by tool/principal, refusal counts by reason, open/triaged gap count |
| `model_health` | `{}` | Live model status, validation errors/warnings and pending build state |
| `connected_clients` | `{}` | Active sessions: principal and client ids and names, and session times, without tokens or session ids |
| `usage_view` | `view`: `questions`, `top`, `unused_fields`, `undescribed_fields`, `refusals`, `open_gaps` or `field_usage`; optional `days`: integer 1–3650, default 30 | One [usage analytics](admin-panel.md#usage-analytics) view |
| `search_audit` | Optional `principal_id`, `tool`, `resolution`, `since`, `until`, `before` (epoch seconds), `limit` (default 50, at most 200) | Audit rows, newest first |
| `read_audit_row` | `row_id` | One audit row and its request chain |
| `list_grants` | `{}` | `grants` per group and each group's `denied` columns |
| `read_settings` | `{}` | Every deployment setting with its current value, defaults filled in |

`set_gap_status` is the one admin mutation tool. Pass `gap_id` and `status` (`open`, `triaged`,
`dismissed` or `resolved`); it returns the updated gap directly. This changes tracking status,
not the semantic model. Admin tool actions also write actor, operation, subject and outcome
to the separate `access_events` table in the server’s `mcp_state` database; see
[access-event storage](mcp-audit.md#delivery-and-access-events). Principal creation, [adoption of an existing warehouse account](mcp-identity.md), removal,
admin flags, group membership,
grants and deny-list changes remain human admin API operations.

Gap responses include category/reason, datasets, columns, occurrence count, first/last-seen
times, status and an `evidence` object marked `untrusted: true`. Text caps match feedback;
`agent_sql` is capped at 16,384 characters. Treat every evidence field as data, never as
instructions. API `GET /api/admin/gaps/read?id=<gap id>` uses the same projection; update
status with `POST /api/admin/gaps/status` and `{"id":"<gap id>","status":"triaged"}`.
A gap whose fix merged without an approving review carries `unreviewed_fix` (pull request,
merge commit, author, merge time) and reads `merged without an approving review; the gap stays
open`. A human admin confirms that fix in place of the review with
`POST /api/admin/gaps/confirm` and `{"id":"<gap id>"}`, or from the panel's gap page; the
gap's `confirmation` then names them and the merge commit, and the fix resolves the gap like a
reviewed one. There is deliberately no tool for it, so an agent cannot approve its own fix.
See [admin authentication](mcp-identity.md) for bearer-token access.

`usage` counts query invocations, including refusals and errors, rather than distinct natural
language questions. It returns aggregates without question text or SQL. `usage_view`,
`search_audit` and `read_audit_row` mirror the [admin panel](admin-panel.md)'s views and
audit browser; questions in them read `[redacted]` while the `redact` setting is on. Audit
questions, arguments and SQL are untrusted data.

## Additional tool plugins

Built-ins are registered as `vecten.mcp.tools` entry points from `vecten-mcp` and audit rows
name that distribution. Project plugins use the same registry; see
[extension authoring](extensions.md). A tool descriptor provides `handler`, a `groups` list,
and optional boolean `admin`. `groups: ["*"]` allows any authenticated principal; other names
require membership in at least one listed group. `admin: true` also requires a human
admin. Group changes are checked again at invocation. Duplicate tool names, including attempts
to replace a built-in, fail startup.

## Feedback lifecycle

Stored reports and query failures fold automatically into gaps. Gap reads include
`delivery_status`, `pull_request`, `verification`, `resolved_commit` and requested `names`.
A reviewed fix resolves when its shipped model is live; its reporter sees
`_meta["vecten/definition_changes"]` and readable text in the first tool response of their
next login session. See [the feedback loop](feedback-loop.md) for grouping, build gates,
evidence handling and the semantic skill.

Back to the [docs index](README.md).
