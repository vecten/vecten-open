## MODIFIED Requirements

### Requirement: Operations
The admin API SHALL offer the operations the panel and the CLI need: principals, service principals, groups, their roles, bound roles and membership, dataset, schema and area grants, deny lists, gaps, model health, access health, usage summaries, connected clients, settings and an on-demand grant reconciliation.

#### Scenario: Operation set

- **WHEN** the admin API's operations are listed
- **THEN** they include create, list, disable, delete, set and clear the admin flag and confirm adoption for principals; create with credentials shown once, list, disable and delete for service principals; create with an optional role name, delete, list, add and remove members, and bind, confirm and unbind an existing role for groups; grant, revoke and list for datasets, schemas and areas; set per group for deny lists; list, read with evidence, set status and confirm an unreviewed merged fix for gaps; read for model health, access health with drift and missing bindings, usage and connected clients; read and set for settings, `reconcile_drift` among them; and reconcile

#### Scenario: Create a person
- **WHEN** the admin API receives a create for Jane Doe with her email
- **THEN** the principal exists keyed on her login identity, with the derived warehouse name, and the warehouse account is created or adoption is offered
