## ADDED Requirements

### Requirement: next reports the provisioner-script rerun after an upgrade

`vecten mcp provisioner-script --env <env> --confirm-run` on Snowflake SHALL record under `[environments.<env>]` the version of the access procedures the confirmed script installs, and, for every environment including `local`, in the run record of the confirmation. Once a confirmed run counts for a deployed environment on Snowflake with the `mcp` component, `next` SHALL report a `command` item `vecten mcp provisioner-script --env <env>` while the highest version recorded for it is older than the one the installed release's MCP requires; a confirmation that recorded no version counts as older. The item SHALL come after the environment's Snowflake identity and owed ship, before its other MCP items, and SHALL hold none of them back, because the MCP serves with older procedures and refuses only area grants and drift revocation. In an MCP-only project the same item SHALL replace the preparation item in the local loop once the script was confirmed. On Postgres no version is recorded and no rerun is reported, because the MCP reads the catalogs itself.

#### Scenario: Deployment upgraded from a release that recorded no version

- **WHEN** `prod` on a platform over Snowflake was deployed and its script confirmed by an earlier release, and `vecten update` has run
- **THEN** `next` reports `vecten mcp provisioner-script --env prod` before the first user `ask`, and the items after it are those reported without it

#### Scenario: Rerun confirmed

- **WHEN** an ACCOUNTADMIN has run the printed script and `vecten mcp provisioner-script --env prod --confirm-run` recorded the current version
- **THEN** `next` reports `vecten github push` and then no rerun item for `prod`

#### Scenario: Postgres

- **WHEN** a deployed Postgres environment's script was confirmed by an earlier release
- **THEN** `next` reports no rerun item
