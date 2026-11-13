## MODIFIED Requirements

### Requirement: Versioned convention document in every project

Every rendered project SHALL contain `docs/schema-convention.md` stating the convention version it follows (version 2 from this change, a superset of version 1) and the pinned Apache Ossie spec version. The document SHALL change only by a version bump. A project that follows version 1 SHALL pass every version 2 test unchanged.

#### Scenario: Document present after init

- **WHEN** a project is rendered
- **THEN** `docs/schema-convention.md` exists and names convention version 2
- **AND** it names the Ossie spec version this repository pins

#### Scenario: Version 1 project after the upgrade

- **WHEN** a project built under version 1, with no area declared, runs the version 2 tests
- **THEN** every convention test passes

### Requirement: Schema names

Models SHALL land in schemas named `raw_<source>`, `staging`, `snapshots`, `core` and `marts`, or in the schemas of a restricted area declared in `vecten.toml`, `core_<area>` and `marts_<area>`, where `<area>` matches `[a-z][a-z0-9]*`. These names SHALL be the same on Postgres and Snowflake, independent of the profile's target schema.

#### Scenario: Schema names on Postgres

- **WHEN** a project builds against Postgres with a HubSpot source
- **THEN** raw tables are in `raw_hubspot` and models are in `staging`, `snapshots`, `core` and `marts`

#### Scenario: Schema names on Snowflake

- **WHEN** the same project builds against Snowflake
- **THEN** the schemas carry the same names with no target-schema prefix

#### Scenario: Area schemas

- **WHEN** `vecten.toml` declares `[areas.finance]` and models are configured with `+schema: marts_finance` and `+schema: core_finance`
- **THEN** they build into `marts_finance` and `core_finance` on both warehouses

## ADDED Requirements

### Requirement: Restricted areas follow their layer's rules

A model in `core_<area>` SHALL follow every rule for `core`, and a model in `marts_<area>` every rule for `marts`: naming, descriptions, a declared primary key and, for marts, one generated dataset whose source names the area schema. The naming test SHALL fail a model in a `core_<name>` or `marts_<name>` schema whose area `vecten.toml` does not declare, naming the model and the missing `[areas.<name>]` entry. `vecten-platform` SHALL pass the declared areas to dbt.

#### Scenario: Undeclared area

- **WHEN** a model is configured with `+schema: marts_fiance` and `vecten.toml` declares only `[areas.finance]`
- **THEN** the naming test fails naming the model and `[areas.fiance]`

#### Scenario: Mart in an area becomes a dataset

- **WHEN** `marts_finance.finance_revenue` has descriptions and a primary key and `dbt-build-local` passes
- **THEN** `osi/datasets.json` holds a `finance_revenue` dataset whose source is `<database>.marts_finance.finance_revenue`

#### Scenario: Core model in an area without a key

- **WHEN** a model in `core_finance` declares no primary key
- **THEN** the convention test fails and names the model

### Requirement: Models declare no grants

A convention test SHALL fail the build when any model, snapshot or seed of the project or of an extension sets dbt's `grants` config, naming the node. On Snowflake it SHALL also fail when a model sets `copy_grants` to `false`.

#### Scenario: Grants config on a mart

- **WHEN** a mart sets `grants: {select: ['analyst']}`
- **THEN** the convention test fails naming the mart

#### Scenario: Copy grants turned off

- **WHEN** a Snowflake project's mart sets `copy_grants: false`
- **THEN** the convention test fails naming the mart
