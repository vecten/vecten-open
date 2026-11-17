## ADDED Requirements

### Requirement: next reports the provisioner-script rerun after an upgrade

`vecten mcp provisioner-script --env <env> --confirm-run` on Snowflake SHALL record under `[environments.<env>]` the version of the access procedures the confirmed script installs, and, for every environment including `local`, in the run record of the confirmation. Once a confirmed run counts for a deployed environment on Snowflake with the `mcp` component, `next` SHALL report a `command` item `vecten mcp provisioner-script --env <env>` while the highest version recorded for it is older than the one the installed release's MCP requires; a confirmation that recorded no version counts as older. The item SHALL come after the environment's Snowflake identity and owed ship, before its other MCP items, and SHALL hold none of them back, because the MCP serves with older procedures, and only area grants, drift revocation and the listing of relations no model builds wait for the rerun. In an MCP-only project the same item SHALL replace the preparation item in the local loop once the script was confirmed. On Postgres no version is recorded and no rerun is reported, because the MCP reads the catalogs itself.

#### Scenario: Deployment upgraded from a release that recorded no version

- **WHEN** `prod` on a platform over Snowflake was deployed and its script confirmed by an earlier release, and `vecten update` has run
- **THEN** `next` reports `vecten mcp provisioner-script --env prod` before the first user `ask`, and the items after it are those reported without it

#### Scenario: Rerun confirmed

- **WHEN** an ACCOUNTADMIN has run the printed script and `vecten mcp provisioner-script --env prod --confirm-run` recorded the current version
- **THEN** `next` reports `vecten github push` and then no rerun item for `prod`

#### Scenario: Postgres

- **WHEN** a deployed Postgres environment's script was confirmed by an earlier release
- **THEN** `next` reports no rerun item

### Requirement: next reports a deployed environment's items beside the local loop

Once an environment of the promotion order has deployed, `next` SHALL report its post-deploy items, walking the order as it does once the local loop is clear, also while local loop items are outstanding. They SHALL follow the local loop items, so local work in progress is finished before the push among them ships it. An environment not yet deployed SHALL get no deploy question, prerequisite or deploy gate while local loop items are outstanding, and the walk SHALL stop at it. An item reported by both the local loop and an environment SHALL be reported once, in the local loop's place.

#### Scenario: Deployed project whose stack never ran on this machine

- **WHEN** `prod` is deployed and the provisioner script is due again, and this clone has never run `vecten up`
- **THEN** `next` reports `vecten up` and the other local loop items first, then `vecten mcp provisioner-script --env prod` and `prod`'s other post-deploy items

#### Scenario: Later environment not yet deployed

- **WHEN** `[deploy] promotion = ["dev", "prod"]`, `dev` is deployed with its items clear, `prod` is not bootstrapped, and a wanted source is unadded
- **THEN** `next` reports the local loop items and no `bootstrap --env prod`
