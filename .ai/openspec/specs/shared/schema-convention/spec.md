# shared/schema-convention Specification

## Purpose
Version 1 of the schema convention every rendered project follows, the dbt tests that enforce it, the Ossie documents under `osi/`, and the convention's role as the contract between the `platform` and `mcp` components.

## Requirements

### Requirement: Versioned convention document in every project

Every rendered project SHALL contain `docs/schema-convention.md` stating the convention version it follows (version 1 at v0.1) and the pinned Apache Ossie spec version. The document SHALL change only by a version bump.

#### Scenario: Document present after init

- **WHEN** a project is rendered
- **THEN** `docs/schema-convention.md` exists and names convention version 1
- **AND** it names the Ossie spec version this repository pins

### Requirement: Schema names

Models SHALL land in schemas named `raw_<source>`, `staging`, `snapshots`, `core` and `marts`, and these names SHALL be the same on Postgres and Snowflake, independent of the profile's target schema.

#### Scenario: Schema names on Postgres

- **WHEN** a project builds against Postgres with a HubSpot source
- **THEN** raw tables are in `raw_hubspot` and models are in `staging`, `snapshots`, `core` and `marts`

#### Scenario: Schema names on Snowflake

- **WHEN** the same project builds against Snowflake
- **THEN** the schemas carry the same names with no target-schema prefix

### Requirement: Model naming rules

Model names SHALL use full words and single underscores: `staging_<source>_<entity>` for staging models, `snapshot_<source>_<entity>` for snapshots, singular nouns for core entities (`company`, `contact`, `deal`), and a domain prefix for marts (`sales_pipeline`). Full words and singular core nouns SHALL be stated in `CLAUDE.md` and followed by the `model` skill.

#### Scenario: Compliant names

- **WHEN** the project contains `staging_hubspot_company`, `snapshot_hubspot_company`, `company` and `sales_pipeline`
- **THEN** the naming test passes

### Requirement: Naming test checks what a test can check

A convention test SHALL fail the build on a model name that breaks the checkable part of the naming rules: the `staging_` and `snapshot_` prefixes followed by a source and an entity segment, lowercase letters, single underscores and no leading digit. The test does not claim to check full words or singularity.

#### Scenario: Wrong prefix or shape

- **WHEN** the project contains a staging model named `stg_hubspot_co` or one named `Staging__Hubspot_Company`
- **THEN** the naming test fails and names the offending model

#### Scenario: Plural core name

- **WHEN** the project contains a core model named `companies`
- **THEN** the naming test passes, because plurality is not testable
- **AND** the `model` skill, following `CLAUDE.md`, names the entity `company`

### Requirement: Descriptions required in core and marts

Every model in `core` and `marts` SHALL have a description, and every column of those models SHALL have a description. A dbt test SHALL fail the build when either is missing.

#### Scenario: Missing column description

- **WHEN** a core model declares a column with no description
- **THEN** the build fails and the failure names the model and column

#### Scenario: Staging model without descriptions

- **WHEN** a staging model has no descriptions
- **THEN** the description test does not fail, because it applies to core and marts only

### Requirement: Core entities declare a primary key

Every core entity SHALL declare a primary key column carrying `unique` and `not_null` tests. The build SHALL fail when a core model has no declared primary key or when either test is missing.

#### Scenario: Core model without a key

- **WHEN** a core model is added with no primary key declared
- **THEN** the convention test fails and names the model

#### Scenario: Duplicate keys at build time

- **WHEN** a core model's primary key contains duplicates
- **THEN** the `unique` test fails the build

### Requirement: Ossie documents are JSON under osi/

Semantic model documents SHALL live in the project's root `osi/` directory as JSON, spec version 0.1.1, with every dataset `source` resolving to a dbt model alias as `database.schema.alias`, and the dbt project SHALL point at it through `osi-paths`, which reads `../osi` unless `VECTEN_OSI_PATH` names another directory, so dbt parses the same files the `mcp` component loads. CI SHALL validate every document against the pinned Ossie schema and fail on an invalid one. The Vecten vendor extension is carried under `vendor_name: COMMON` because the pinned schema closes the vendor list.

#### Scenario: Valid Ossie document

- **WHEN** `osi/` contains a JSON document valid against the pinned Ossie schema whose dataset sources name mart models
- **THEN** dbt parses the project and CI validation passes

#### Scenario: dbt reads the root directory

- **WHEN** the dbt project under `dbt/` parses
- **THEN** it reads every document under the root `osi/` and no `dbt/osi/` exists

#### Scenario: Invalid Ossie document

- **WHEN** a document in `osi/` violates the pinned schema or names a source that is not a dbt model alias
- **THEN** CI fails and names the document and the violation

#### Scenario: YAML document

- **WHEN** a file `osi/metrics.yml` is present instead of JSON
- **THEN** CI fails and names the file as not a JSON Ossie document

### Requirement: Datasets are generated, metrics are hand-written

`osi/datasets.json` SHALL be generated from the dbt manifest and catalog after a green `dbt-build-local` and MUST NOT be edited by hand: one dataset per mart model, fields from declared columns with their descriptions. `osi/metrics.json` SHALL hold the metrics and SHALL be written by hand, and every column in a metric expression SHALL be qualified with its dataset name (`sales_pipeline.amount`). A column description SHALL have one home, `schema.yml`; CI SHALL regenerate `osi/datasets.json` and fail when the committed file differs. Every mart SHALL declare a primary key, a column carrying `unique` and `not_null` tests or a composite key listed under the model's `meta.osi.primary_key`, because dbt requires an entity on every semantic model; the generator SHALL refuse a mart with neither and name it.

#### Scenario: Description edited in schema.yml

- **WHEN** a mart column's description is changed in `schema.yml` and `dbt-build-local` runs green
- **THEN** the corresponding field in `osi/datasets.json` carries the new description
- **AND** no file other than `schema.yml` was edited by hand

#### Scenario: Hand edit to the generated file

- **WHEN** `osi/datasets.json` is edited directly and committed
- **THEN** the CI freshness check regenerates the file, finds a difference and fails naming `osi/datasets.json`

#### Scenario: Mart without a primary key

- **WHEN** a mart declares no column with both `unique` and `not_null` and no `meta.osi.primary_key`
- **THEN** `dbt-build-local` fails naming the mart and both ways to declare its key
- **AND** `osi/datasets.json` is unchanged

#### Scenario: Metric added

- **WHEN** a metric is added to `osi/metrics.json` and validates
- **THEN** CI passes with `osi/datasets.json` unchanged

### Requirement: Convention meta keys on columns

A column's `meta.osi.synonyms` list in `schema.yml` SHALL become that field's `ai_context.synonyms` in the generated dataset. A column whose name ends in `_at` or `_date`, or whose catalog type is a date, time or timestamp type, SHALL be generated as a time dimension. A column in the primary key or whose name ends in `_id` SHALL be a dimension regardless of its type. Other columns SHALL be classified as dimension or fact from their catalog type: numeric types are facts, everything else is a dimension.

#### Scenario: Synonyms carried over

- **WHEN** the `revenue` column of a mart declares `meta.osi.synonyms: [turnover, sales]`
- **THEN** the generated field `revenue` has `ai_context.synonyms` of `turnover` and `sales`

#### Scenario: Time dimension by name

- **WHEN** a mart has columns `order_date`, `created_at` and `country`
- **THEN** `order_date` and `created_at` are generated with `dimension.is_time: true`
- **AND** `country` is generated as a dimension without the time marker

#### Scenario: Numeric key

- **WHEN** a mart has an integer column `deal_id` and a decimal column `amount`
- **THEN** `deal_id` is generated as a dimension
- **AND** `amount` is generated as a fact

### Requirement: Dialect-neutral identity macros

The project SHALL ship the identity macros `normalize_domain` and `normalize_email`, and each SHALL produce the same result for the same input on Postgres and Snowflake.

#### Scenario: Same input on both warehouses

- **WHEN** `normalize_email` is applied to ` John.Doe@Example.COM ` on Postgres and on Snowflake
- **THEN** both return `john.doe@example.com`

### Requirement: Staging models carry load metadata in UTC

Every staging model SHALL expose a `_loaded_at` column and a `_source` column, and every timestamp column in the project SHALL be in UTC. A convention test SHALL fail the build when a staging model lacks either column.

#### Scenario: Staging model missing _source

- **WHEN** a staging model has `_loaded_at` but no `_source`
- **THEN** the convention test fails and names the model

#### Scenario: Load metadata populated

- **WHEN** a staging model is built from a raw table
- **THEN** every row has a non-null `_loaded_at` in UTC and a `_source` naming the originating source

### Requirement: The convention is the contract between the components

A project that passes all convention tests SHALL produce an `osi/` model that the `mcp` component loads and validates without edits. A change to the convention SHALL be a version bump in `docs/schema-convention.md`, made in the same change as any generator change it requires.

#### Scenario: Fixture project loads

- **WHEN** a project rendered by this CLI with one modelled source passes its convention tests and its `osi/` directory is given to the `mcp` component
- **THEN** the model loads with no validation error and no edit to any document

#### Scenario: Convention change

- **WHEN** a rule in the convention is changed
- **THEN** the convention version in `docs/schema-convention.md` is bumped in the same change
- **AND** the generator change that rule requires lands in the same change
