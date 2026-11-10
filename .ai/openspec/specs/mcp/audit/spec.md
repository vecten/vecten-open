# mcp/audit Specification

## Purpose
The record of every tool call and every access event: what was asked, what ran, how it was resolved, and how long it is kept.

## Requirements

### Requirement: One audit row per tool call
Every tool call SHALL write one row to the state database with a ULID id that is also the receipt id, timestamp, user, client id, session id, request chain id, tool, arguments, the natural-language question when the client supplies it, SQL and its SHA-256, the loaded model commit so a gap's evidence names the definition that produced the answer, tables, columns, row count, duration, error class, resolution and plugin name.

#### Scenario: Successful sql-exec
- **WHEN** a person runs a `SELECT` through `sql-exec`
- **THEN** one row exists with the SQL, the parsed tables and columns, the row count, the duration and resolution `agent_sql`

#### Scenario: Tool call that errors
- **WHEN** a tool call fails in the warehouse
- **THEN** one row exists with the error class and resolution `error`

### Requirement: Resolution values
The `resolution` of an audit row SHALL be one of `compiled`, `agent_sql`, `refused` and `error`.

#### Scenario: Compiled metric query
- **WHEN** `query_metrics` compiles and runs
- **THEN** the row's resolution is `compiled`

### Requirement: Request chain links a refusal to its fallback
The request chain id SHALL link a `query_metrics` refusal to the `sql-exec` call that follows it in the same session, so that the pair can be read as one request.

#### Scenario: Reading a chain
- **WHEN** an admin queries the audit for a chain id
- **THEN** the refusal row and the agent SQL row that followed it are returned together

### Requirement: Audit writes never block a response
Audit rows SHALL be queued and flushed asynchronously. A flush that fails MUST itself be logged and counted, and the count SHALL be visible to admins.

#### Scenario: State database briefly unavailable
- **WHEN** the state database is unreachable during a tool call
- **THEN** the tool response is returned without waiting on the audit
- **AND** the dropped flush is logged and the dropped counter increments

### Requirement: Audit table is append-only
The audit table SHALL be append-only. No tool, panel page or admin API MUST update or delete an audit row other than retention expiry.

#### Scenario: Admin tries to edit a row
- **WHEN** an admin attempts to change or remove an audit row through the panel or admin API
- **THEN** no such operation is offered and a direct request is rejected

### Requirement: The server writes the audit through an insert-only role
The server process SHALL connect to `mcp_state` with a runtime role that holds `INSERT` and `SELECT` on the audit and access event tables and no `UPDATE`, `DELETE` or ownership, so append-only holds at the database. Retention expiry SHALL run under a separate maintenance role that drops expired partitions. Warehouse query history, joinable through the receipt comment, SHALL be documented as the independent record.

#### Scenario: Compromised process
- **WHEN** code running as the server process attempts to update or delete an audit row
- **THEN** the database refuses it

#### Scenario: Retention job
- **WHEN** the retention job runs
- **THEN** it drops expired partitions under the maintenance role and the runtime role is unchanged

### Requirement: Retention with a 90-day default
The deployment SHALL have a retention setting for audit rows, defaulting to 90 days, after which rows are removed.

#### Scenario: Default retention
- **WHEN** a deployment has not changed the retention setting
- **THEN** audit rows older than 90 days are removed

### Requirement: Redaction toggle for question and arguments
The deployment SHALL have a redaction toggle, default off, that when on stores the natural-language question and the arguments text redacted.

#### Scenario: Redaction on
- **WHEN** redaction is on and a person makes a tool call with a question
- **THEN** the audit row stores a redacted question and arguments while keeping tool, tables, columns, resolution and duration

### Requirement: Audit records every column and redacts values tied to denied columns
The audit row's column list SHALL name every column the statement references, denied or not, so an access review of who read a column is complete. Column names are not personal data; values are. Every literal the statement compares with a column denied for the person's groups SHALL be replaced by a placeholder in the stored SQL and arguments. Other literals SHALL be kept, because the loop uses the agent's SQL as the draft of a definition.

#### Scenario: Denied column referenced
- **WHEN** a person's SQL selects `customers.email`, which is denied for their group
- **THEN** the audit row's column list includes `customers.email`

#### Scenario: Value compared with a denied column
- **WHEN** a person's SQL filters `WHERE email = 'jane@acme.com' AND status = 'paid'` and `email` is denied for their group
- **THEN** the stored SQL holds a placeholder in place of `'jane@acme.com'`
- **AND** it keeps `'paid'`

### Requirement: Access events in a separate table
Logins, consents, revocations and admin actions SHALL be written to a separate access events table with timestamp, actor, event type and subject.

#### Scenario: Admin disables a person
- **WHEN** an admin disables a person's warehouse user
- **THEN** an access event records the admin, the action and the person
