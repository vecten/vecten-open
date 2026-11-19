## ADDED Requirements

### Requirement: Group roles in a shared Snowflake account
With a shared Snowflake account a group's warehouse role SHALL be the group's role name followed by the environment's suffix, created and owned by the environment's `provisioner`, and the default group SHALL map to the environment's `analyst` role. The state database SHALL keep the names without the suffix, and the server SHALL append the suffix whenever it names a Snowflake role or user. A grant SHALL never target a role of another environment.

#### Scenario: Same group in two environments
- **WHEN** an admin creates the group `finance` in `dev` and in `prod` of a project whose environments share an account
- **THEN** the roles `FINANCE_ACME_DEV` and `FINANCE_ACME_PROD` exist, and a member of `dev`'s `finance` reads nothing in `ACME_PROD`

#### Scenario: Default group
- **WHEN** a person is provisioned in `dev` with a shared account and no group assigned
- **THEN** the person's Snowflake user holds `ANALYST_ACME_DEV`, and `mcp_state` records the default group's role as `analyst`
