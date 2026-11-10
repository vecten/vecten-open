# mcp/admin-api Specification

## Purpose
The HTTP API through which the CLI and the panel administer the `mcp` component: who may call it, how the first admins come to exist, which operations exist only here and never as MCP tools, and how every call is recorded.

## Requirements

### Requirement: Admin API requires a person with the admin flag
Every admin API call SHALL carry an access token issued by the server's own authorization server for a human principal whose admin flag is set. Service principals and unflagged people MUST be refused.

#### Scenario: Unflagged person
- **WHEN** a person without the admin flag calls the admin API with a valid token
- **THEN** the call is refused and recorded as a denied access event

### Requirement: First admins are seeded from vecten.toml
On first start the server SHALL read `[mcp] admins`, a list of email addresses in `vecten.toml`, and SHALL set the admin flag on each listed person at their first login. While no admin exists the server SHALL serve tool calls but refuse every admin operation, and `next` SHALL report it.

#### Scenario: Seeded admin logs in
- **WHEN** `[mcp] admins = ["jane@acme.com"]` and Jane logs in for the first time
- **THEN** her principal is created with the admin flag set

#### Scenario: Empty list
- **WHEN** `[mcp] admins` is empty and no principal has the flag
- **THEN** every admin API call is refused and `next` reports an `ask` item for the first admin's email

### Requirement: Mutations exist only on the admin API
Creating, adopting, disabling and deleting principals, setting the admin flag, creating and deleting groups, changing membership, granting and revoking datasets and schemas, editing deny lists and changing settings SHALL be admin API operations, reachable from the panel and the CLI, and MUST NOT be offered as MCP tools.

#### Scenario: Admin's agent asks to provision
- **WHEN** an admin's agent lists tools in an admin session
- **THEN** no tool creates, disables or grants, and the response to such a request names `vecten user` and `vecten grant`

### Requirement: Every call is an access event
Every admin API call SHALL write an access event with the acting principal, the operation, the subject and the outcome.

#### Scenario: Grant from the CLI
- **WHEN** an admin runs `vecten grant orders --group sales`
- **THEN** an access event names the admin, `grant`, `orders` and `sales`

### Requirement: Operations
The admin API SHALL offer the operations the panel and the CLI need: principals, service principals, groups and membership, dataset and schema grants, deny lists, gaps, model health, access health, usage summaries, connected clients, settings and an on-demand grant reconciliation.

#### Scenario: Operation set

- **WHEN** the admin API's operations are listed
- **THEN** they include create, list, disable, delete, set and clear the admin flag and confirm adoption for principals; create with credentials shown once, list, disable and delete for service principals; create, delete, list, add and remove members for groups; grant, revoke and list for datasets and schemas; set per group for deny lists; list, read with evidence, set status and confirm an unreviewed merged fix for gaps; read for model health, access health, usage and connected clients; read and set for settings; and reconcile

#### Scenario: Create a person
- **WHEN** the admin API receives a create for Jane Doe with her email
- **THEN** the principal exists keyed on her login identity, with the derived warehouse name, and the warehouse account is created or adoption is offered
