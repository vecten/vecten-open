# MCP audit records

This guide explains how administrators inspect and retain records of calls to the
[MCP server](GLOSSARY.md#mcp-model-context-protocol), and how extension authors attach
warehouse query evidence to those records. Complete the [identity setup](mcp-identity.md)
first. `mcp_state` is the server's private PostgreSQL database, separate from warehouse data.

Audit capture covers every authenticated tool call, built in or from an installed extension,
through the shared warehouse execution service; the acceptance tests exercise it against a
local PostgreSQL.

## Follow a receipt

Every authenticated tool invocation returns a [receipt](GLOSSARY.md#receipt) under
`_meta["vecten/receipt"]`, without changing the tool's content or output schema:

```json
{
  "id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
  "model": "project-commit-or-local-model-revision",
  "issued_at": 1792454400.0,
  "sql_sha256": null
}
```

`id` is a ULID, a time-sortable unique identifier, and the audit row's ID. `model` identifies
the loaded semantic model at the start of the call, or is null when no model is loaded.
`issued_at` is Unix time in seconds. `sql_sha256` is null for calls without prepared SQL,
including compiler refusals. For warehouse execution, it is the SHA-256 of the SQL after the execution service applies its row limit
and substitutes parameters, before audit redaction. The receipt comment is excluded from
that hash. The server continues to send parameters separately to the warehouse driver.
A warehouse error can have a hash because its statement was prepared before execution failed.

The warehouse statement begins with `/* vecten <receipt id> */`; Snowflake also receives
that ID in `QUERY_TAG`. Warehouse query history or configured PostgreSQL statement logs
provide an independent record that can be joined to the receipt. Enable and retain those
logs through your warehouse's normal administration procedures. Audit records do not include
query result data, and a receipt does not guarantee the asynchronous write has succeeded.

An administrator with read access to `mcp_state` can inspect a receipt:

```sql
SELECT id, at, body
FROM public.audit_log
WHERE id = '01ARZ3NDEKTSV4RRFFQ69G5FAV';
```

From an MCP client, the built-in [`verify_receipt` tool](mcp-tools.md) reads a receipt. Admins
can browse rows and their request chains in the [admin panel](admin-panel.md#gaps-and-the-audit)
or with the `search_audit` and `read_audit_row` admin tools.

## Audit row schema

`audit_log` has columns `id` (text), `at` (Unix seconds), and `body` (JSONB). Its partitioned
primary key is `(id, at)`. The server generates a fresh ID for each invocation.

| JSON field | Meaning |
|---|---|
| `id`, `at` | Receipt ID and invocation time. |
| `principal_id`, `principal_kind` | Authenticated [principal](GLOSSARY.md#principal), and `human` or `service`. |
| `client_id`, `session_id` | OAuth client and authenticated session. |
| `request_chain_id` | Correlation ID linking a compiler refusal to its SQL fallback. |
| `tool`, `plugin` | Tool name and installed extension distribution name; manually registered tools use their entry name. Unknown tools have a null plugin. |
| `arguments`, `question` | Supplied arguments and optional `question` argument, subject to redaction. |
| `sql`, `sql_sha256` | Prepared SQL with parameters substituted, or attempted SQL for a rejected statement; execution hash as described above. Stored SQL may be redacted. |
| `model_commit` | Model revision copied into the receipt. |
| `tables`, `columns` | Parsed SQL references, including denied columns and any SQL aliases. Empty when no SQL was recorded or parsing could not recover references. |
| `row_count` | Number of returned rows; null if no query completed. |
| `duration_ms` | Invocation duration in milliseconds, including audit preparation. |
| `error_class` | Exception class, without exception messages or driver credentials; null for successful calls. |
| `resolution` | `compiled`, `agent_sql`, `refused`, or `error`. Successful non-SQL tools use `compiled`; `sql-exec` uses `agent_sql`. |
| `refusal_reason` | Present for explicit refusals, such as `join_required`, `tool_not_available`, or `app_scope` (an analytics app's principal reached outside its declared datasets). |
| `on_behalf_of` | Present only on an analytics app principal's calls that send `X-Vecten-On-Behalf-Of`: the viewer's email as the app passed it. Recorded, never used to decide access. |
| `redacted` | Whether the global toggle or a denied-column value caused redaction. |

One record covers each invocation, including an authenticated attempt to invoke an unavailable
tool, argument validation failures, handler errors and cancellation. Requests rejected before
authentication do not become tool audit records. Authentication activity has separate access events.

`_meta["vecten/request_chain_id"]` carries the correlation ID. An explicit refusal from `query_metrics`, the tool that compiles named metrics into SQL, is associated with the next `sql-exec` (the tool for caller-authored SQL) in the same principal/client/session tuple.
The association is consumed once, expires after five minutes or session expiry (whichever
comes first), and is replaced by another metric request in that session. Discovery calls do
not consume it. Pending associations are in memory, limited to 1,024 sessions with the oldest
evicted first; restarting the server clears them. Concurrent refusals in one session associate
the next SQL call with the most recently completed refusal, so this is correlation evidence,
not proof that two calls express the same user intent.

## Delivery and access events

Tool records and their captured redaction policy enter a bounded queue of 1,024 waiting
records. The background writer redacts each record before persisting it or invoking a sink;
unredacted inputs remain only in process memory while queued. A redaction failure drops the
record through the same loss counter as a failed write.
Audit delivery does not wait on PostgreSQL in the tool response path. A full queue or failed
write drops the affected row, logs the failure category without input values, and increments the dropped count.
Rows are not retried; a process crash can lose queued rows. Shutdown allows up to two seconds
for the writer to finish. Receipt lookup can therefore briefly return no record, or permanently
return none after a loss. Authentication and tool work can still require a reachable state
database; asynchronous audit delivery does not make those operations offline-capable.

A human administrator can call `GET /api/admin/audit-status` on the MCP hostname with their
OAuth bearer token. The response's `data` object contains `queued`, `written`, and `dropped`.
These are process-local counters, reset on restart. `queued` excludes a row currently being
written. Investigate increases in `dropped` alongside server logs and independent warehouse
history. Installed `vecten.mcp.audit_sinks` hooks receive rows only after local persistence;
a sink failure is logged separately and does not undo the local record.

Logins, consents, revocations and admin calls write to `access_events`, a separate table with
`id`, `at`, `principal_id` (actor), `operation`, `subject`, and `outcome`. These access-event
writes are synchronous. Both tables share the retention and append-only protections below.

## Retention and redaction settings

Settings are stored in `mcp_state`. After signing in as a human administrator as described in
the [identity guide](mcp-identity.md#first-administrator-and-login), use the admin HTTP API on
the same MCP hostname with that session's OAuth bearer token. `GET /api/admin/settings`
returns explicit overrides; omitted settings use the defaults below. Send JSON to
`POST /api/admin/settings/set`:

| Setting | Default | Example request body |
|---|---|---|
| Audit retention | 90 days; permitted range 1–3,650 days | `{"key":"audit_retention_days","value":90}` |
| Question and argument redaction | Off | `{"key":"redact","value":true}` |

The server checks retention in its maintenance loop and runs it at most once per day after a
successful run. Failed maintenance is retried on the next loop. It drops fully expired daily
partitions and deletes expired rows from the fallback partition. A boundary-day partition is
kept until the whole day expires, so expiry is not immediate at an individual row's 90-day
anniversary. Shortening retention can remove old records at the next maintenance run.

The global redaction toggle replaces `question` and `arguments` with `[redacted]` on new
records. It does not hide SQL, table/column names, resolution, duration or the execution hash.
Existing records are immutable and are not rewritten when the setting changes.

Independently of that toggle, literals compared with columns on a caller's [group deny lists](mcp-identity.md#access-and-enforcement)
are replaced by placeholders in stored SQL and removed from arguments and questions.
Semantic field expressions and SQL column aliases are followed to find underlying column
names. Matching is conservative by column name: another column of the same name can also
have its compared value redacted. Other literals are retained for analysis. All referenced
column names remain visible in the audit, including denied ones. Unparseable or unbindable
query evidence, including opaque unsupported syntax, is stored as a placeholder with
question/arguments redacted. Redaction also handles PostgreSQL escape and dollar-quoted
strings, explicit derived-column aliases, predicates inside `EXPLAIN`, function comparisons
such as `starts_with`/`contains`, and simple `CASE` expressions. All literals within a
function or conditional expression involving a denied column are hidden; unrelated
expressions retain their values. When applying
deny-list redaction, stored SQL comments are removed so they cannot repeat sensitive values.
If cancellation or a failure interrupts the initial policy lookup, the record keeps identity
and error evidence but replaces question/arguments with `[redacted]`.

Deny lists control metadata visibility and audit privacy; warehouse permissions remain the
[enforcement point for data access](mcp-identity.md#access-and-enforcement).

## Append-only storage and extensions

Administrator setup creates partitioned audit/access-event tables owned by the separate
`mcp_state_maintenance` role. The runtime `mcp_state` role has INSERT and SELECT on the parent
tables, no UPDATE/DELETE/TRUNCATE or ownership, and cannot assume the maintenance role.
Startup rejects elevated permissions on the parents or their partitions. Retention calls the
bounded `vecten_audit.maintain(integer)` function, executing with its maintenance owner's
privileges. There is no admin HTTP operation or MCP tool to edit or delete individual records.
The packaged [warehouse setup script](../packages/vecten-shared/src/vecten_shared/sql/warehouse_setup.sql)
preserves legacy access events and can be rerun. Fresh local PostgreSQL containers run it
through `10-vecten.sh` during initialization. For an existing local data volume, after
`vecten up` has rendered the current setup files, identify that project's PostgreSQL container
with `docker ps` and rerun its initializer:

```sh
docker exec <local-postgres-container> bash /docker-entrypoint-initdb.d/10-vecten.sh
```

The container already holds the initializer's environment; do not paste credentials into the
command. On cloud hosts the deployment VM agent runs the same setup script as part of applying
a release. Follow `docs/runbook.md` in the generated project and obtain approval for that
shared-environment change before applying it.

Extension authors can use `current_request` from `vecten_mcp.transport` to obtain
`(runtime, authenticated_caller)` inside a handler. Execute warehouse queries through
`runtime.queries.execute(caller, sql, parameters, receipt)`; in a tool invocation it uses the
server-issued receipt and captures evidence before execution, even when the warehouse fails.
Inside a handler, pass `current_audit.get().receipt` as the receipt argument. `parameters` is
an ordered list of values, using `%s` placeholders for PostgreSQL and `?` for Snowflake; pass
`[]` when there are none. The return dictionary contains `rows`, `columns`, `sql`,
`sql_template`, `receipt`, `tables`, and `referenced_columns`. The existing runtime defaults
to at most 1,000 returned rows and a 30-second statement timeout. Use one warehouse statement
per invocation. Direct driver calls cannot be inferred by the
audit wrapper and should use that execution service instead.

`current_audit` from `vecten_mcp.audit_call` holds the call's receipt and audit context.
A compiler handler calls `current_audit.get().refuse(reason)` before returning a structured
refusal. The transport owns enqueueing; handlers should not enqueue another record or infer
identity, SQL evidence or receipts from client-supplied result fields. Handlers may return a
receipt in their content, but `_meta["vecten/receipt"]` is the authoritative transport receipt.

Back to the [docs index](README.md).
