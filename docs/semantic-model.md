# Authoring the semantic model

The semantic model is what the MCP server's tools read: which tables exist, what each column
means, and how each business metric is computed. AI clients connected to the server ask for a
metric by name through its `query_metrics` tool, and the server compiles the definition written
here into SQL. The model lives in the project repository under `osi/` (short for Open Semantic
Interchange, the format's name before it moved to Apache as Ossie) as [Apache Ossie](https://github.com/apache/ossie) 0.1.1
documents. This guide covers how those documents are produced, the rules the validator enforces,
the Vecten-specific settings and the eval cases that pin metric definitions.

A project's `vecten.toml` lists its components. Projects with the `platform` component run
Vecten's own ingestion and dbt modelling; projects with only the `mcp` component (*MCP-only*)
put the server over a warehouse something else builds. Terms such as *mart* and *dataset* are
defined in the [glossary](GLOSSARY.md).

The *loader* below is the shared validation and loading code: `vecten mcp validate` and the
server run the same checks, so a model that validates on a laptop loads on the server, with one
exception. Only the server can check that each dataset's source table exists in the warehouse;
`vecten mcp validate` skips that check and says so.

## What the model is

Every JSON file under `osi/` is one Ossie document. The server and `vecten mcp validate` load
all of them, check each against the pinned 0.1.1 JSON schema and merge their datasets, metrics
and relationships by name into one model. Files whose name starts with a dot are skipped; any
other non-JSON file is an error, because dbt 1.12 also reads `osi/` (the dbt project's
`osi-paths` setting points there) and turns each document into dbt semantic models, failing on
a file it cannot parse.

- A name defined in two documents is an error naming both documents.
- A document whose `version` is not `0.1.1` is rejected.
- An extension package added with `vecten extension add` can contribute documents through its
  `vecten.osi` Python entry point (see [extensions](extensions.md)); they merge under the same
  rules.

The only way to change the model is a pull request that edits these files. The server and its
administration interfaces never write definitions.

The usual layout is two files: `osi/datasets.json`, generated, and `osi/metrics.json`, written
by hand.

## Datasets are generated

A dataset is a table or view the model can read, with its fields. When `osi/datasets.json` is
generated from dbt, never edit it by hand: descriptions and synonyms have one home, dbt's
`schema.yml`, and a re-run overwrites the edit. In platform projects CI also fails when the
committed file differs from a fresh generation. The file comes from one of three places:

- **`vecten dbt-build-local`**, for projects with the `platform` component. After a green local
  build it reads dbt's manifest and catalog and writes one dataset per model in the `marts`
  schema.
- **`vecten mcp import --dbt <path> [--schema marts]`**, for an MCP-only project over a warehouse
  that an existing dbt project builds. It runs the same generator over that project's
  `target/manifest.json` and `target/catalog.json`. Generate them with the production target
  (`dbt docs generate --target <prod>`), so that each model's schema is the one the warehouse
  really has. `--schema` names the schema holding the marts; it selects the models and names
  their source schema.
- **`vecten mcp introspect`**, for a warehouse without dbt. It drafts datasets from the
  warehouse catalog. With no dbt project behind it, this draft is the one `osi/datasets.json`
  you do edit by hand: review it, add descriptions and drop tables that are not marts.

What the generator writes for each mart:

| Generated | Comes from |
|---|---|
| `source` | `database.schema.alias`; the database is the `database` key of the `[warehouse]` table in `vecten.toml`, or the literal name `warehouse` when the key is unset; set the key whenever the deployed warehouse database has another name, or the server's source check fails. The local build runs against a throwaway Postgres container or, for Snowflake projects, an emulation over DuckDB, so its database name never reaches the file |
| field `description` | the column's description in `schema.yml` |
| `ai_context.synonyms` | the column's or model's `meta.osi.synonyms` |
| time dimension (`dimension.is_time: true`) | a name ending in `_at` or `_date`, or a date, time or timestamp type |
| dimension | a primary-key column, a name ending in `_id`, or a non-numeric type |
| fact (no `dimension` marker) | any other numeric column |
| `primary_key` | the column tested both `unique` and `not_null`, or `meta.osi.primary_key` |
| `snapshot_derived` mark | the mart descends from a dbt snapshot in the manifest |

The rows apply top to bottom: a column matching the time rule is a time dimension even when it
is also a key. A mart without a primary key is refused, because dbt needs an entity on every
semantic model.

When the loader reads any dataset, generated or hand-written, the `dimension` marker decides
each field's class: `dimension.is_time: true` is a time dimension, a `dimension` marker without
it is a dimension, and no marker is a fact (a value to aggregate).

## Metrics are hand-written

Metrics live in `osi/metrics.json`. Qualify every column with its dataset name:
`SUM(sales_pipeline.amount)`, not `SUM(amount)`. dbt converts each Ossie document on its own, so
in a metrics file separate from the datasets an unqualified column cannot be resolved by dbt.
The loader still accepts unqualified columns (rule 3 below), which only works when the metric
sits in the same document as its dataset.

Each metric reads exactly one dataset, its **home dataset**. The loader finds it in this order:

1. **Qualifiers.** `SUM(orders.total_amount)` has home `orders`, even when `invoices` also has a
   `total_amount` column. Every qualifier in one metric must name the same dataset.
2. **Override.** A Vecten extension with `"dataset": "orders"` names the home directly; every
   qualifier must still name that dataset.
3. **Coverage.** For unqualified columns, the one dataset whose fields include all of them is
   the home. `SUM(total_amount)` resolves to `orders` when only `orders` has `total_amount`. If
   no dataset or more than one fits, the model does not load, and the error names the metric
   and the candidates.

A metric that needs columns from two datasets is not expressible in this release (Vecten Open
0.1; `vecten version` prints yours): put the join in a dbt mart, or a warehouse view in a
project without dbt, and point the metric at it. The `relationships` in the model describe how
datasets join; when a request spans two datasets the server refuses with `join_required` and
returns a SQL skeleton with that join, which the client can complete and run through the
server's read-only `sql-exec` tool, outside the metric definitions.

## Dialects

Every field and metric expression lists one or more dialect entries. Postgres deployments
always use the `ANSI_SQL` entry. Snowflake deployments use the `SNOWFLAKE` entry when there is
one, and otherwise the `ANSI_SQL` entry, transpiled to Snowflake SQL (`CAST(x AS TEXT)` runs as
`CAST(x AS VARCHAR)`). An element with neither entry is a model error with the reason
`dialect_missing`. The Ossie schema has no Postgres dialect, so write portable `ANSI_SQL`.

The loader parses every expression and renders it again in the warehouse's own SQL, so the
functions below work on both warehouses even where the spelling differs: on Postgres,
`COUNT_IF(x > 1)` runs as `SUM(CASE WHEN x > 1 THEN 1 ELSE 0 END)` and `IF(...)` as a `CASE`.
One difference survives: Postgres has no `TRY_CAST`, so it runs as `CAST` and fails on a bad
value instead of returning null.

## Allowed functions

The server runs these expressions inside SQL it sends to the warehouse, so it accepts only a
known set: nothing that can read other tables, sleep or write. An expression may hold column
references, literals, arithmetic and comparison operators, `CASE` and `CAST`, and calls to
these functions only:

- Aggregates: `SUM`, `COUNT`, `AVG`, `MIN`, `MAX`, `COUNT_IF`, `PERCENTILE_CONT`,
  `PERCENTILE_DISC`
- Numbers: `ABS`, `ROUND`, `FLOOR`, `CEIL`
- Text: `UPPER`, `LOWER`, `CONCAT`, `LENGTH`, `TRIM`, `LTRIM`, `RTRIM`, `SUBSTRING`
- Nulls and conditions: `COALESCE`, `NULLIF`, `IF`, `CASE`
- Dates and types: `DATE_TRUNC`, `TIMESTAMP_TRUNC`, `EXTRACT`, `CAST`, `TRY_CAST`
- Window functions: `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `LAG`, `LEAD`

A subquery, a whole statement or any other function is a model error naming the construct
(`expression cannot contain Subquery`, `function PG_SLEEP is outside the expression
allow-list`). When a calculation needs something else, compute it as a column in a dbt mart
(or a warehouse view, without dbt).

## The Vecten extension

Ossie has no slot for Vecten's settings, and its vendor list is closed, so they ride in a
`custom_extensions` entry with `vendor_name: COMMON`, the list's vendor-neutral value, whose
`data` is a JSON **string** with a `vecten` key. Entries from other tools under `COMMON` without
that key are ignored.

```json
"custom_extensions": [
  {"vendor_name": "COMMON",
   "data": "{\"vecten\": {\"additivity\": \"semi_additive\", \"non_additive_over\": \"time\", \"rollup\": \"last\"}}"}
]
```

One element carries at most one such entry. A wrong value is a model error naming the key.

| On | Key | Values |
|---|---|---|
| metric | `dataset` | the home dataset's name |
| metric | `additivity` | `additive`, `non_additive` or `semi_additive` |
| metric | `non_additive_over` | `time`, the dimension a semi-additive metric must not be summed across |
| metric | `rollup` | `last` or `first`: which period's value stands for a coarser period |
| metric | `verified` | any JSON value recording who checked the definition and when, such as `{"by": "ana@example.com", "on": "2026-10-01"}`; `query_metrics` returns it with every answer |
| dataset | `snapshot_derived` | `true` or `false`, written by the generator |
| dataset | `grain` | `day`, `week`, `month`, `quarter` or `year`: one row per entity per this period. A request for a semi-additive metric needs both this key on its home dataset and a `time.grain` in the request; without either the server refuses with `rollup_not_allowed` |

## Additivity

Whether a metric's rows may be added up again decides what the AI client may do with them:
each `query_metrics` answer lists, per metric, the operations its rows allow (filter and sort
always; summing across dimensions only for `additive`; summing across everything but time for
`semi_additive`), and tells the client to ask again with a coarser `time.grain` in the request
(month instead of day, say) instead of adding rows up. The loader assigns each metric a class:

- `additive` when it aggregates only with `SUM`, `COUNT` or `COUNT(*)`;
- `non_additive` when it contains `COUNT(DISTINCT ...)`, `AVG`, a percentile, a window function
  or a division of aggregates, and for anything else;
- whatever the extension's `additivity` declares, which overrides the inference.

A balance is the usual `semi_additive` case: summing balances across accounts is fine, summing
one account's balance across months is not. Declare it with `non_additive_over: time` and a
`rollup` (`last` for a month-end balance), and give the dataset a `grain`. Every request for
such a metric must then name a `time.grain`; one coarser than the dataset's `grain` uses the
`rollup` rule.

`vecten mcp validate` warns, without failing, on a metric that declares no additivity and
either sums over a `snapshot_derived` dataset (probably semi-additive; the warning prints the
extension to add) or uses `DISTINCT`, `AVG`, a division or a window function.

## Eval cases

`evals/semantic.yaml` pins what metric requests must compile to, so a later edit cannot quietly
change a number someone already relies on. It lives outside `osi/` because dbt parses every
JSON file there. Each case holds a `query_metrics` request and the expected outcome.

| Request key | Holds |
|---|---|
| `metrics` | a list of metric names; all must share one home dataset |
| `dimensions` | optional list of field names to group by; `dataset.field` names a field of another dataset, which the server refuses with `join_required` |
| `time` | optional `{grain, start, end, dimension}`: `grain` is `day`, `week`, `month`, `quarter` or `year`; `start` (inclusive) and `end` (exclusive) are ISO dates; `dimension` names the time field when the home dataset has more than one |
| `filters` | optional list such as `[{field: country, op: "=", value: PL}]`; `op` is `=`, `!=`, `>`, `>=`, `<`, `<=` or `like` with one value, `in` with a list, `between` with a two-item list, or `is null` (with `value: false` for "is not null") |
| `order_by` | optional list such as `[{field: total_revenue, dir: desc}]`, naming a requested metric or dimension |
| `limit` | optional maximum row count, capped by the server |

A *gap* is a question the server could not answer well, recorded on the server from refusals
and user feedback. The optional `gap` key keeps the gap's id on the case that pins its fix,
so a later regression names the gap.

```yaml
cases:
  - name: revenue_by_month
    question: What was revenue by month?   # optional, the question behind the request
    gap: gap_01J...                         # optional, the gap this case came from
    request: {metrics: [total_revenue], time: {grain: month}}
    expect:
      outcome: compiled
      metrics: [total_revenue]
      dimensions: []
      grain: month
      sql_sha256: 3b1f...
  - name: revenue_in_poland
    request: {metrics: [total_revenue], dimensions: [country]}
    expect:
      outcome: compiled
      sql_sha256: 9c4e...
      value: {metric: total_revenue, where: {country: PL}, equals: 1250.0, tolerance: 0.01}
  - name: revenue_by_segment
    request: {metrics: [total_revenue], dimensions: [customers.segment]}
    expect: {outcome: refused, reason: join_required}
```

`outcome` is `compiled` or `refused`; a refusal's `reason` is one of the codes the server
returns: `unknown_metric`, `unknown_field`, `join_required` or `rollup_not_allowed`. The
optional `metrics`, `dimensions` and `grain` under `expect` restate the request, so an
accidental edit to the request fails the case.

`vecten mcp validate --evals` compiles every case without a warehouse and exits 6 (content
error; see [exit codes](exit-codes.md)) naming each case, and its `gap`, whose outcome, refusal
reason, restated request or hash no longer matches. `sql_sha256` is the SHA-256 of the SQL compiled for the project's warehouse,
with `%s` (Postgres) or `?` (Snowflake) placeholders where filter values go, so a Postgres and a
Snowflake project hash the same case differently. When a definition change is deliberate, read
the new SQL the failure prints, and if it is right, copy the new hash the same message prints
into the case.

An expected `value` needs data, so it is checked only inside `vecten dbt-build-local`, which
platform projects use to build dbt locally from sample data: the API responses each data source
connector saved with `vecten connector run <source> --record`. The compiled query runs and
`where` picks exactly one result row by its output columns, which are named after the
requested dimensions, the time field and the metrics; without `where` the result must have
one row. The named metric must be within `tolerance` (an absolute difference, default 0) of
`equals`. Elsewhere, including every MCP-only project, the value is skipped with a note.

## How the server loads the model

The server never reads the repository. Merging a pull request runs the project's ship workflow
(a GitHub Actions job running `vecten ship`), which copies a content bundle onto the server's
host: one archive holding the DAGs, the dbt project, `osi/` and `evals/` at one commit. The
server reads only `osi/` from it. Every three minutes the
server checks the bundle and validates it with the same code `vecten mcp validate` runs, plus
one check only the server can make: that every dataset `source` table exists in the warehouse.

- **Projects with the `platform` component** swap to the new model only once the Airflow job
  that runs dbt has built the bundle's commit; that job records the commit it built, and the
  server reads the record. A metric over a new mart column therefore never runs before the
  column exists. Until then model health reads `shipped, waiting for build`; if it stays there,
  look at the last run of the `dbt_build` DAG in Airflow.
- **MCP-only projects** swap as soon as the bundle validates, because Vecten cannot see when the
  external build finished. Merge a model change only after the warehouse has the columns it
  uses.
- **A bundle that fails validation** leaves the previous model serving; the errors appear in
  model health.

Model health is on the [admin panel](admin-panel.md)'s Model screen and in the `model_health`
admin tool; the admin API serves the same answer at
`GET https://mcp.<domain>/api/admin/model-health`, where `<domain>` is the environment's
`domain` in `deploy/envs/<env>.tfvars` (for `prod`, `[deploy] domain` in `vecten.toml`), sending
an administrator's access token as `Authorization: Bearer <token>`. `vecten login` stores that token as `access_token` under the
server's address in `~/.config/vecten/tokens.json` (`$XDG_CONFIG_HOME/vecten/tokens.json` when
that variable is set). It lasts an hour; any `vecten user` command, such as `vecten user list`,
refreshes an expired one. Administrators are the people listed under `[mcp] admins` in
`vecten.toml` (see [MCP identity](mcp-identity.md)). The answer holds:

- `status`: `ready`, `shipped, waiting for build`, `error` (the last check failed; the previous
  model, if there is one, keeps serving, and with none the tools answer that no model is
  ready) or `not loaded` (the server has not finished its first check since starting);
- `spec_version`, the Ossie version, and the `errors` and `warnings` of the last check;
- `candidate_commit`, the commit of the bundle last checked, whether or not it validated, and
  `live_commit`, the commit of the model being served.

Back to the [docs index](README.md).
