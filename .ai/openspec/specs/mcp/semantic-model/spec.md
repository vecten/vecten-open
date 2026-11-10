# mcp/semantic-model Specification

## Purpose
The semantic model the tools read: Apache Ossie 0.1.1 documents under `osi/` in the project repository, datasets generated from dbt and metrics written by hand, merged, validated and indexed on every load, with a home-dataset rule for metrics.

## Requirements

### Requirement: Model is every Ossie 0.1.1 document under osi/
The server SHALL load every document under `osi/` in the project repository, validate each against the 0.1.1 JSON schema, and merge their datasets, metrics and relationships by name into one model. A name that appears in two documents MUST be a model error. The spec version SHALL be recorded with every load, and a document whose `version` is not 0.1.1 SHALL be rejected.

#### Scenario: Generated datasets and hand-written metrics
- **WHEN** `osi/` holds `datasets.json` with two datasets and `metrics.json` with one metric over the first
- **THEN** one model loads with two datasets and one metric
- **AND** the panel records the spec version alongside the load

#### Scenario: Duplicate name across documents
- **WHEN** `datasets.json` and a second document both define a dataset named `orders`
- **THEN** the load is rejected
- **AND** the panel reports the duplicate name and both documents

#### Scenario: Document fails schema validation
- **WHEN** a document is missing a required key such as a dataset's `primary_key`
- **THEN** the load is rejected
- **AND** the panel reports the schema error with its path in the document

### Requirement: Git is the source of truth, the shipped bundle is the delivery
The model SHALL be authored under `osi/` in the project repository and the only write path SHALL be a pull request. The server SHALL read the model from the content bundle the ship workflow places on its host, never by pulling the repository itself, and the server and panel MUST NOT write definitions to the model directly.

#### Scenario: Merge ships a new model
- **WHEN** a pull request editing `osi/` merges and the ship workflow completes
- **THEN** the server finds the new bundle, validates its documents and prepares them for the swap

### Requirement: The live model swaps only once the warehouse matches it
In a project with the `platform` component the server SHALL swap to a shipped bundle's model only when the environment's built commit, written by the dbt DAG, is at or past the bundle's commit, and SHALL show the bundle as "shipped, waiting for build" in model health until then. In an MCP-only project the swap SHALL happen as soon as the bundle validates. A scheduled check, every few minutes, SHALL notice both the bundle and the built commit, so no webhook is needed.

#### Scenario: Metric needs a column dbt has not built
- **WHEN** a bundle adds a metric over a new mart column and the dbt DAG has not yet built that commit
- **THEN** `query_metrics` keeps answering from the previous model and model health reads "shipped, waiting for build"

#### Scenario: Build catches up
- **WHEN** the dbt DAG writes the bundle's commit as the built commit
- **THEN** the server swaps to the new model within the check interval and the tools serve the new definitions from the next call

#### Scenario: MCP-only project
- **WHEN** a bundle ships to an MCP-only deployment and validates
- **THEN** the swap happens without waiting for any build

### Requirement: Failed validation keeps the previous model live
When a shipped model fails validation, the server SHALL keep serving the previously loaded model and SHALL report the failure in the panel.

#### Scenario: Broken model shipped
- **WHEN** a shipped bundle holds a document that fails validation
- **THEN** `describe`, `search` and `query_metrics` keep answering from the previous model
- **AND** the panel's model health shows the failure as an error

### Requirement: Field classification from the dimension marker
The loader SHALL classify each field: `dimension.is_time: true` makes a time dimension, a `dimension` marker without it makes a dimension, and no marker makes a fact. The classification MUST be visible in `describe` and used by the compiler.

#### Scenario: Three fields, three classes
- **WHEN** a dataset has `order_date` with `dimension.is_time: true`, `country` with a `dimension` marker, and `total_amount` with no marker
- **THEN** `describe` reports them as time dimension, dimension and fact respectively

### Requirement: Metric home dataset is resolved by column coverage
For every metric the loader SHALL collect the column identifiers in its expression. A qualified column (`dataset.column`) names the metric's home dataset directly, and every qualifier in one metric MUST name the same existing dataset. For unqualified names the loader SHALL resolve the unique dataset whose fields cover all of them as the home dataset; zero or more than one match MUST be a model error. Hand-written metrics SHALL qualify every column, because dbt converts each Ossie document alone.

#### Scenario: Unique home dataset
- **WHEN** metric `total_revenue` is `SUM(total_amount)` and only `orders` has a `total_amount` field
- **THEN** the metric's home dataset is `orders`

#### Scenario: Column in several datasets
- **WHEN** a metric's columns are covered by both `orders` and `invoices` and no override is present
- **THEN** the load reports a model error naming the metric and both datasets

#### Scenario: Qualified columns name the home
- **WHEN** metric `total_revenue` is `SUM(orders.total_amount)` and both `orders` and `invoices` have a `total_amount` field
- **THEN** the metric's home dataset is `orders` and no error is reported

### Requirement: Home dataset override through a vendor extension
A metric SHALL be bound to its home dataset only by its qualifiers, by the coverage rule or by a `custom_extensions` entry with `vendor_name: COMMON` whose `data` is a JSON string holding `{"vecten": {"dataset": "<name>"}}`. When the entry is present it names the home dataset in place of the coverage rule, and every qualifier in the metric MUST still name that dataset.

#### Scenario: Override resolves ambiguity
- **WHEN** a metric's columns exist in `orders` and `invoices` and its `custom_extensions` names `orders`
- **THEN** the metric's home dataset is `orders` and no error is reported

### Requirement: Dialect selection per expression
For each field and metric expression the loader SHALL use the `SNOWFLAKE` dialect entry on a Snowflake deployment when present, otherwise the `ANSI_SQL` entry transpiled to the deployment's dialect; Postgres deployments always use `ANSI_SQL`. An expression with neither MUST be a model error.

#### Scenario: ANSI fallback
- **WHEN** a field has only an `ANSI_SQL` expression on a Snowflake deployment
- **THEN** the loaded expression is the ANSI expression transpiled to Snowflake

#### Scenario: No usable dialect
- **WHEN** a field has only an `MDX` expression on a Postgres deployment
- **THEN** the load reports a model error naming the field and the reason `dialect_missing`

### Requirement: Load-time checks against the warehouse
On every load the server SHALL verify that every dataset `source` exists in the warehouse and that every expression parses, and SHALL show the results in the panel as errors and warnings. An unchanged model's sources SHALL be checked again at most once an hour, because on Snowflake each check resumes the compute warehouse, which bills until it suspends.

#### Scenario: Source table missing
- **WHEN** a dataset's `source` names a table the warehouse does not have
- **THEN** model health shows an error naming the dataset and source

### Requirement: Additivity is inferred and may be overridden
On every load the server SHALL assign each metric an additivity class: `additive` when its expression aggregates only with `SUM`, `COUNT` or `COUNT(*)`; `non_additive` when it contains `COUNT(DISTINCT)`, `AVG`, a percentile, a window function or a division of aggregates; otherwise `non_additive`. A `custom_extensions` entry with `vendor_name: COMMON` whose `data` is a JSON string holding `{"vecten": {...}}` with `additivity`, optionally `non_additive_over` and `rollup`, SHALL override the inference.

#### Scenario: Inferred from a sum
- **WHEN** `total_revenue` is `SUM(total_amount)` with no extension
- **THEN** it loads as `additive`

#### Scenario: Inferred from a ratio
- **WHEN** `product_share` is `SUM(product_amount) / SUM(total_amount)`
- **THEN** it loads as `non_additive`

#### Scenario: Declared semi-additive
- **WHEN** `account_balance` is `SUM(balance)` and its extension declares `semi_additive` over `time` with rollup `last`
- **THEN** it loads as `semi_additive` with that rule

### Requirement: Generator proposes semi-additivity from snapshot lineage
When a mart model descends from a `snapshot_` model in the dbt manifest, the generator SHALL write a `vendor_name: COMMON` extension on the dataset whose `data` marks it `{"vecten": {"snapshot_derived": true}}`, and the validator SHALL warn on every summed metric whose home dataset carries that mark and no additivity declaration.

#### Scenario: Balance mart
- **WHEN** `account_balance` sums a column of a mart built from `snapshot_bank_account` and declares nothing
- **THEN** `vecten mcp validate` warns that the metric is probably semi-additive over time and names the extension to add

### Requirement: Validator warns on undeclared risky expressions
The validator SHALL warn on a metric whose expression contains `DISTINCT`, `AVG`, a division or a window function and no additivity declaration.

#### Scenario: Undeclared average
- **WHEN** `avg_order_value` is `AVG(total_amount)` with no extension
- **THEN** validation passes with a warning naming the metric

### Requirement: Product-side evals live outside osi/ and run in validate
A project MAY hold `evals/semantic.yaml`, each case holding a `query_metrics` request and either the expected outcome `compiled` with the metrics, dimensions, grain and SQL SHA-256, or `refused` with the reason code. `vecten mcp validate --evals` SHALL compile every case against the model without a warehouse and fail on any mismatch. The file MUST NOT live under `osi/`, which dbt parses as Ossie documents.

#### Scenario: Case passes
- **WHEN** a case expects `compiled` for `total_revenue` by month and the compile matches the stored hash
- **THEN** validate reports the case as passed

#### Scenario: Definition drifts
- **WHEN** the metric expression changes and the stored hash no longer matches
- **THEN** validate fails naming the case and shows the new SQL so the hash can be updated deliberately

#### Scenario: Value check needs data
- **WHEN** a case also carries an expected value with a tolerance
- **THEN** the value is checked only inside `dbt-build-local`, where fixture data exists, and skipped elsewhere with a note

### Requirement: Expressions pass an allow-list
Every field and metric expression SHALL be parsed with sqlglot and accepted only when it uses column references, literals, operators and functions from the documented list, with no subquery, no statement other than an expression and no function outside the list. A rejected expression SHALL be a model error naming the construct.

#### Scenario: Subquery in a metric
- **WHEN** a metric expression contains `(SELECT ...)`
- **THEN** validation fails naming the subquery

#### Scenario: Unknown function
- **WHEN** an expression calls a function outside the documented list
- **THEN** validation fails naming the function

### Requirement: Validator is shared with the CLI
`vecten mcp validate` and the server SHALL run the same validation: the 0.1.1 schema, the multi-document merge, the home-dataset rule, dialect coverage, and source existence when a warehouse connection is available. A model that validates on a laptop with a warehouse connection MUST load on the server without further errors; when source existence was skipped, it is the only check the server may still fail.

#### Scenario: Validated locally, loaded on the server
- **WHEN** `vecten mcp validate` passes on a checkout and the same commit is merged
- **THEN** the server's reload succeeds with no model error

#### Scenario: Validation without a warehouse
- **WHEN** `vecten mcp validate` runs with no warehouse connection
- **THEN** every check except source existence runs
- **AND** the output states that source existence was skipped

### Requirement: Generator emits datasets from a dbt project
`vecten mcp import --dbt <path>` and the `platform` component's `dbt-build-local` SHALL run the same generator over a dbt manifest and catalog and write `osi/datasets.json`, one dataset per mart model, with database and schema names from `vecten.toml`, never from the local build target. It MUST NOT emit metrics.

#### Scenario: Dataset fields
- **WHEN** the generator writes a dataset
- **THEN** its `source` is `database.schema.alias`, its fields carry descriptions from `schema.yml`, `is_time` on columns named with `_at` or `_date`, dimension or fact from the catalog type, and synonyms from `meta.osi.synonyms`

#### Scenario: Conventional dbt project
- **WHEN** the generator runs over a project with two mart models whose columns carry descriptions and one `order_date` column
- **THEN** `osi/datasets.json` holds two datasets with described fields and `order_date` marked `is_time`
- **AND** the output passes `vecten mcp validate` unchanged

#### Scenario: Local build names differ
- **WHEN** `dbt-build-local` built against a disposable Postgres named `local_dw`
- **THEN** `osi/datasets.json` names the production database from `vecten.toml`, not `local_dw`

#### Scenario: Metrics untouched
- **WHEN** the generator runs in a project whose `osi/metrics.json` holds three metrics
- **THEN** `osi/metrics.json` is unchanged and `osi/datasets.json` contains no metric
