# mcp/mcp-tools Specification

## Purpose
The tools the MCP endpoint exposes, how the list is built per session from the person's groups, and how additional tools are discovered as plugins.

## Requirements

### Requirement: Tool list is built per session from groups
One MCP endpoint SHALL serve everyone. The tool list offered in a session MUST be built from the groups of the authenticated person, and admin tools SHALL appear only in sessions of people with the admin flag.

#### Scenario: User session
- **WHEN** a person without the admin flag lists tools
- **THEN** the list contains `describe`, `search`, `sql-exec`, `query_metrics`, `report_feedback` and `verify_receipt`
- **AND** no admin tool is listed or callable

#### Scenario: Admin session
- **WHEN** a person with the admin flag lists tools
- **THEN** the list contains the user tools and the admin tools

### Requirement: describe tool
`describe` SHALL return datasets, fields with descriptions and `ai_context`, metric definitions and relationships from the loaded model, minus the columns denied for the caller's groups.

#### Scenario: Describe a dataset
- **WHEN** a person calls `describe` for `orders`
- **THEN** the response lists its fields with classification and descriptions, the metrics whose home dataset is `orders`, and its relationships
- **AND** denied columns are absent

### Requirement: search tool
`search` SHALL run a full-text search over the loaded model's dataset, field and metric names, descriptions and synonyms, minus denied columns, and SHALL behave the same on both warehouses because it is served from the state database.

#### Scenario: Search by synonym
- **WHEN** a field has synonym "turnover" in its `ai_context` and a person searches for "turnover"
- **THEN** the field is returned with its dataset and description

### Requirement: sql-exec statement policy
`sql-exec` SHALL accept exactly one statement whose kind is `SELECT`, `WITH`, `SHOW`, `DESCRIBE` or `EXPLAIN`, SHALL apply the row cap, and MUST reject anything else before it reaches the warehouse.

#### Scenario: Read-only statement
- **WHEN** a person submits a single `SELECT`
- **THEN** it runs as the person and returns at most the row cap rows

#### Scenario: Write or multi-statement
- **WHEN** a person submits `DELETE FROM orders` or two statements separated by a semicolon
- **THEN** the tool returns an error and nothing is sent to the warehouse

#### Scenario: Write hidden inside an allowed kind
- **WHEN** a person submits `EXPLAIN ANALYZE DELETE FROM orders`, a `WITH` whose body is an `INSERT`, `UPDATE` or `DELETE`, or a `SELECT ... INTO`
- **THEN** the tool returns an error and nothing is sent to the warehouse

### Requirement: Warehouse sessions are read-only and time-bounded
The statement policy is a parser check, and an adopted account may hold write grants, so the warehouse SHALL enforce it too. Every warehouse session the server opens for a principal, for `sql-exec` and `query_metrics` alike, SHALL be read-only and SHALL carry a statement timeout taken from a deployment setting with a stated default. On Postgres that is `default_transaction_read_only = on` and `statement_timeout`; on Snowflake, `STATEMENT_TIMEOUT_IN_SECONDS`.

#### Scenario: Parser bypass reaches the warehouse
- **WHEN** a statement that writes passes the parser on Postgres
- **THEN** the warehouse rejects it because the transaction is read-only

#### Scenario: Runaway query
- **WHEN** a statement runs longer than the configured timeout
- **THEN** the warehouse cancels it and the tool returns an error with the audit row's error class set to timeout

### Requirement: Snowflake sessions use the group roles
On Snowflake every session the server opens for a principal SHALL run under the roles of the principal's groups, set explicitly, and never under the user's default or secondary roles.

#### Scenario: Person with a broad default role
- **WHEN** an adopted Snowflake user whose default role is `SYSADMIN` runs `sql-exec`
- **THEN** the session runs under the roles of that person's groups and cannot use `SYSADMIN`

### Requirement: sql-exec carries a receipt
Every `sql-exec` response SHALL carry the same `receipt` shape as `query_metrics`, and the statement sent to the warehouse SHALL begin with the receipt comment.

#### Scenario: Agent SQL with receipt
- **WHEN** a person runs a `SELECT` through `sql-exec`
- **THEN** the response's `receipt.id` is the audit row id and the warehouse sees the statement prefixed with `/* vecten <receipt id> */`

### Requirement: verify_receipt tool
`verify_receipt` SHALL take a receipt id and return the audit row's question, tool, SQL, resolution, model commit, person and time, and whether the SQL still compiles identically against the current model. A person MAY verify only receipts issued to them; an admin MAY verify any. A receipt older than the retention period SHALL return `expired`.

#### Scenario: Number in a slide
- **WHEN** a person verifies a receipt from last week whose metric definition has since changed
- **THEN** the response returns the original SQL and states that the current model compiles differently

#### Scenario: Someone else's receipt
- **WHEN** a non-admin verifies a receipt issued to another person
- **THEN** the tool returns not found

### Requirement: sql-exec records parsed tables and columns
For every `sql-exec` call the server SHALL parse the statement and record the referenced tables and columns in the audit row.

#### Scenario: Join across two tables
- **WHEN** a person runs a `SELECT` joining `orders` and `customers`
- **THEN** the audit row lists both tables and the columns referenced

### Requirement: report_feedback tool
`report_feedback` SHALL accept structured feedback with the question, what was expected, what was returned, the tool calls involved, a category from the fixed list of wrong value, missing metric, missing dataset, wrong definition and access denied, and who raised it, user or agent. The tool's input schema SHALL list both sets of accepted values.

#### Scenario: Agent reports on its own
- **WHEN** the agent could not serve a question and calls `report_feedback` with category missing metric and raised by agent
- **THEN** the report is accepted and acknowledged with an identifier

#### Scenario: Category outside the fixed list
- **WHEN** a call uses a category not in the list
- **THEN** the tool rejects it and names the allowed categories

### Requirement: Tool failures name their cause
A failed tool call SHALL be logged at error level with its receipt id and the underlying error's class, warehouse error code and message, with credentials redacted. The client SHALL get a short reason when the semantic model is not ready, when the caller's account has no warehouse access, when the warehouse refused the request, and when the arguments fail the tool's input schema; any other failure SHALL reach the client as a generic message.

#### Scenario: Model not ready
- **WHEN** a person calls `describe` while the semantic model has never become ready
- **THEN** the tool answers that the semantic model is not ready, that `sql-exec` still works, and that an administrator finds the reason in `vecten issues`

#### Scenario: Account without warehouse access
- **WHEN** a signed-in person whose principal is not provisioned, or is disabled, calls `sql_exec`
- **THEN** the tool answers that their account has no warehouse access yet and that an administrator runs `vecten user add` or enables them again

#### Scenario: Warehouse refusal
- **WHEN** Snowflake refuses a `query_metrics` statement with error 3107
- **THEN** the client is told the warehouse refused the request with code 3107 and Snowflake's message, and the server log holds the same with the receipt id

### Requirement: Admin tools
In admin sessions the endpoint SHALL offer tools to list gaps, read a gap with its evidence, set a gap's status, list users and groups, read usage summaries, read model health, and list connected clients. Creating, adopting, disabling or deleting principals, setting the admin flag, changing groups, granting or revoking, and editing deny lists MUST NOT be tools; they exist only on the admin API, because an admin's agent can be steered by data it reads.

#### Scenario: Admin reads a gap from Claude Desktop
- **WHEN** an admin calls the read-gap tool with a gap id
- **THEN** the response holds the gap's question, reason or category, datasets, columns, agent SQL, occurrence count, first and last seen, and status

### Requirement: Tools are discovered as plugins
Tools SHALL be discovered through the `vecten.mcp.tools` entry point group. A plugin package MUST declare its tools and the groups allowed to call them; the built-in tools SHALL be registered the same way, and the audit row SHALL record the plugin name.

#### Scenario: Project adds a corpus search tool
- **WHEN** a plugin package declaring a `crm-search` tool for the `sales` group is installed
- **THEN** members of `sales` see `crm-search` in their tool list and others do not
- **AND** audit rows for its calls carry the plugin's name
