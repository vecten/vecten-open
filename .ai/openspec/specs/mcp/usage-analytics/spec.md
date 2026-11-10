# mcp/usage-analytics Specification

## Purpose
Usage analytics as a product: panel views and admin tools over the audit and gaps. Publishing the same tables as an Ossie model over the MCP is deferred past v0.1, because it would need a server identity querying `mcp_state`, against the rule that every query runs as the person.

## Requirements

### Requirement: Panel views over audit and gaps
The panel SHALL show questions per day and per user, top metrics and datasets, fields never used, fields with no description, refusal reasons over time, and open gaps, computed from the audit and gaps tables and rendered as tables, with no charting library.

#### Scenario: Fields never used
- **WHEN** a field in the loaded model has never appeared in any audit row's column list
- **THEN** it is listed under fields never used

#### Scenario: Refusal reasons over time
- **WHEN** an admin opens the refusal view for the last 30 days
- **THEN** counts per reason code per day are shown

### Requirement: Analytics read the state database, never the warehouse
The panel views and the admin usage tools SHALL be computed from `mcp_state`. The server MUST NOT write audit data into the customer warehouse.

#### Scenario: Warehouse inspected after use
- **WHEN** the customer warehouse is inspected after a period of use
- **THEN** it contains no audit, access event or gap tables created by the product

### Requirement: Admin usage tools mirror the panel views
The admin tools SHALL offer the same views as the panel, questions per day and per user, top metrics and datasets, fields never used, fields with no description, refusal reasons over time and open gaps, as read-only tools, respecting the redaction toggle.

#### Scenario: Admin asks which questions failed this week
- **WHEN** an admin in Claude Desktop calls the usage tool for refusals by reason over the last seven days
- **THEN** the result is computed from the deployment's own audit, with questions redacted when the toggle is on
