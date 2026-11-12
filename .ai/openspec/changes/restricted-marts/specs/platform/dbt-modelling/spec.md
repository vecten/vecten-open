## MODIFIED Requirements

### Requirement: Layered layout

The dbt project SHALL be laid out as `staging/<source>_<entity>` models, one snapshot per staging model by default, then `core`, then `marts`, so data flows staging to snapshots to core to marts. A core entity MAY read several snapshots; every snapshot SHALL feed at least one core model. A restricted area declared in `vecten.toml` SHALL add `core_<area>` and `marts_<area>` beside `core` and `marts`, configured as model folders with `+schema` set to the area schema. A core model whose rows the default group must not read SHALL go to `core_<area>`, and the `model` skill SHALL say so.

#### Scenario: Layout after one modelled source

- **WHEN** a HubSpot `company` entity has been modelled
- **THEN** a `staging_hubspot_company` model, a `snapshot_hubspot_company` snapshot and a `company` core model exist
- **AND** the core model reads from the snapshot, not from raw

#### Scenario: Layout with an area

- **WHEN** `[areas.finance]` is declared and a finance mart is modelled from invoices only finance may read
- **THEN** the invoice entity is in `core_finance` and the mart is in `marts_finance`

## ADDED Requirements

### Requirement: Rebuilds keep their grants on Snowflake

The rendered `dbt_project.yml` of a Snowflake project SHALL set `+copy_grants: true` for every model, so that a relation dbt replaces keeps the privileges of the relation it replaces, in the same statement. `vecten update` SHALL add the setting to an existing project. The credential-free local build SHALL build with the same setting, or SHALL turn it off for the emulation only when the emulation cannot accept it.

#### Scenario: Dataset grant across a rebuild

- **WHEN** the `sales` group holds a dataset grant on `marts.orders` and the dbt DAG rebuilds `orders` with `CREATE OR REPLACE`
- **THEN** the `sales` role can read the rebuilt `orders` before any reconciliation runs

#### Scenario: Setting turned off by a model

- **WHEN** a Snowflake model sets `copy_grants: false`
- **THEN** the convention test fails naming the model
