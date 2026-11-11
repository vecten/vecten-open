---
name: model
description: >-
  Shape a Vecten Open source's raw tables into dbt staging views, snapshots, core entities and
  marts that follow the project's schema convention, then build and test them with
  `vecten dbt-build-local` until green so `osi/datasets.json` is regenerated. Use this skill
  whenever `vecten next --json` lists a `skill` item resolved by `model` (titles such as "Model
  `company` from `raw_hubspot.company`", "Snapshot staging_...", "No core model reads ...",
  "Describe ..."), and whenever the human asks to model, clean, transform or join loaded data or
  to build a table or mart for reporting, even if they never say "dbt".
---

# Modelling a source

A source is modelled when every raw table its sync declared in `sources.yml` has a staging
view, a snapshot and a core entity, the marts the human asked for exist, `vecten dbt-build-local` is green, and
`vecten next --json` lists no modelling item. The rules come from the project's schema
convention; this skill applies them in order and loops on the local build until it passes.

## When this skill runs

`vecten next --json` lists an item with `"kind": "skill"` and `"resolution": "model"`. Its
title says what is missing:

| Title | What is missing |
|---|---|
| ``Model `<table>` from `raw_<source>.<table>` `` | No staging model for a raw table the sync declared in `sources.yml`. |
| `Snapshot staging_<source>_<table>` | A staging model without its snapshot. |
| `No core model reads snapshot_<source>_<table>` | A snapshot no core entity reads. |
| `Describe <model>` or `Describe <model> (column <column>)` | A core or mart model, or one of its columns, without a description in `schema.yml`. |

It also runs when the human asks for a model or a mart in their own words. Work a whole source
per pass, staging through core, rather than one item at a time: the layers depend on each other
and the build checks them together. The command item `Build the dbt project locally` is
`vecten dbt-build-local`, which step 7 runs anyway.

## Ground rules

- Read `docs/schema-convention.md` in the project before writing a model; it is the contract
  this skill follows, and the project's `CLAUDE.md` summarises it. The convention tests enforce
  it at build time.
- The files that are yours: `dbt/models/staging/staging_*.sql`, `dbt/snapshots/snapshot_*.sql`,
  `dbt/models/core/*.sql`, `dbt/models/marts/*.sql` and the `schema.yml` beside core and marts.
- A dbt package from dbt hub or a public git repository, such as `dbt_utils`, goes in
  `dbt/project-packages.yml` with dbt's `packages:` shape: `package: dbt-labs/dbt_utils` with
  `version: 1.3.0`, one exact release, or `git: https://...` with `revision:` set to a full
  40-character commit id. Ask the human before adding one. Never edit `dbt/packages.yml`: it
  is regenerated before every run from that file. The build exits 6 and names the entry when a
  pin is a range, a tag or a branch, or when the package is private.
- The CLI owns `dbt/models/staging/sources.yml` (written by each sync), `osi/datasets.json`
  (written by the local build), `vecten.toml`, `.vecten/` and `dbt/target/`; never edit them.
  `osi/metrics.json` belongs to the `semantic` skill.
- Build only with `vecten dbt-build-local --json`. It replays the recorded fixtures into a
  disposable warehouse, runs the build in the two steps the convention tests need, and
  regenerates `osi/datasets.json`. A direct `dbt build` skips the second step and the
  regeneration. Exit codes: 0 OK, 1 INTERNAL (a bug in vecten: show the human and stop),
  2 USAGE, 3 PREREQUISITE, 4 REFUSED (this project has no platform component), 5 TOOL_FAILED
  (dbt failed), 6 CONFIG (the semantic documents). Every failure prints `"ok": false` with an
  `error` naming the next step.

## 1. Read the raw schema

```bash
vecten raw schema <source> --json
```

It prints `{"source": "<source>", "schema": "raw_<source>", "tables": [{"name": ...,
"columns": [{"name": ..., "type": ...}]}]}` from the local warehouse. Exit 3 with "the warehouse
is not reachable" means the stack is down: run `vecten up --json` and retry. An empty `tables`
list means no sync has landed: that is the `connector` skill's work.

The tables to model are the ones `dbt/models/staging/sources.yml` declares for the source, the
same tables the `Model ...` items name; `raw schema` lists more than that. dlt flattens a nested
object into columns of its parent named `<field>__<key>`, which you stage like any other column.
A nested list becomes a child table named `<table>__<field>`: the sync does not declare child
tables in `sources.yml`, so no staging model can read them through `source()`. Do not wait for
them to appear there; tell the human which nested lists are left out of the model. Ignore dlt's
`_dlt_load_id` and `_dlt_id` columns.

Then check that every declared table has its fixture, `connectors/<source>/fixtures/<table>.jsonl`,
and read a few of its lines to see real values: date formats, empty strings, URLs, units. A
declared table without a fixture cannot be built: `vecten dbt-build-local` replays fixtures
only, so its staging view fails on a missing relation however the model is written. Stop there,
tell the human which table has no fixture, and hand the source back to the `connector` skill,
which records it or drops that object from the connector. Never loop on the build for it.

When a table's or column's meaning is unclear, ask the human: every description you write later
must be true.

## 2. The dbt MCP server

When `.mcp.json` declares a `dbt` server and your client has loaded it, use its local tools to
read the project. It reads the `manifest.json` that `vecten dbt-build-local` leaves under
`dbt/target/` (the build runs `docs generate` after it goes green), so run the build once before
relying on lineage, and again after changing models.

- `get_lineage_dev`: a model's upstream and downstream from the local manifest, filtered by node
  type and depth.
- `get_node_details_dev`: a model, seed, snapshot or source from the local manifest, with its
  columns, description and SQL.
- The dbt CLI tools `parse`, `compile`, `list`, `show` and `docs`, for checking that the project
  parses, reading compiled SQL and listing nodes.

That is quicker and more reliable than parsing `manifest.json` by hand. `generate_source`,
`generate_model_yaml` and `generate_staging_model` appear only when the dbt project has the
`dbt-codegen` package, which this project installs only if `dbt/project-packages.yml` lists it;
when the server offers them, their output is a draft to rewrite to the convention. Leave `build`, `run` and `test` to
`vecten dbt-build-local`, which replays the fixtures, runs the convention's second step and
regenerates the semantic model.

Without the server, read the files and write the models by hand; nothing below depends on it.

## 3. Staging: one view per declared table

One staging view per table `sources.yml` declares for the source:
`dbt/models/staging/staging_<source>_<table>.sql`, with `<table>` exactly the declared table's
name: `next` pairs each declared table with its staging model by that file name, so any other
name leaves the `Model ...` item open. Read from `{{ source('<source>', '<table>') }}`.

- Cast every column to an explicit type, whatever the raw type says. dlt lands ISO dates as
  text and infers other types from the first records it sees, which a later sync can contradict:
  `cast(created_at as timestamp)`, `cast(close_date as date)`; identifiers to `integer` or
  `bigint`, money to `decimal(18, 2)`, flags to `boolean`.
- Rename to the convention: `id` becomes `<entity>_id`, timestamps end in `_at`, dates in
  `_date`, no abbreviations.
- Normalise identities with the package macros rather than by hand:
  `{{ vecten_convention.normalize_domain('domain') }}`,
  `{{ vecten_convention.normalize_email('email') }}`.
- Add `current_timestamp as _loaded_at` and `'<source>' as _source`.
- Leave out dlt's bookkeeping columns.

```sql
select
    cast(id as integer) as deal_id,
    cast(company_id as integer) as company_id,
    title,
    stage,
    cast(amount as decimal(18, 2)) as amount,
    cast(created_at as timestamp) as created_at,
    cast(close_date as date) as close_date,
    cast(is_won as boolean) as is_won,
    current_timestamp as _loaded_at,
    'demo' as _source
from {{ source('demo', 'deal') }}
```

## 4. Snapshots: one per staging model

`dbt/snapshots/snapshot_<source>_<table>.sql`, the staging model's name with `snapshot_` in
place of `staging_`, which is how `next` pairs them; `strategy='check'` keyed on the entity's id,
with `check_cols` listing the business columns only. `_loaded_at` changes on every build, so a
snapshot checking it would version every row every day.

```sql
{% snapshot snapshot_demo_deal %}
{{ config(
    unique_key='deal_id',
    strategy='check',
    check_cols=['company_id', 'title', 'stage', 'amount', 'created_at', 'close_date', 'is_won'],
    schema='snapshots',
) }}
select * from {{ ref('staging_demo_deal') }}
{% endsnapshot %}
```

## 5. Core: one entity per snapshot

`dbt/models/core/<entity>.sql`, named with a singular noun in full words (`company`, `deal`,
`contact`) even when the raw table is plural. It selects the business columns from the
snapshot's current rows through `ref('snapshot_...')`, which is how `next` sees the snapshot is
read:

```sql
select deal_id, company_id, title, stage, amount, created_at, close_date, is_won
from {{ ref('snapshot_demo_deal') }}
where dbt_valid_to is null
```

Declare it in `dbt/models/core/schema.yml`: a description on the model, every column it builds
listed with a description, and the key column tested `unique` and `not_null`. Say what a value
means, its unit and when it is null; the semantic model and the people asking questions read
these words.

```yaml
version: 2
models:
  - name: deal
    description: Current version of every deal from the demo source, one row per deal.
    columns:
      - name: deal_id
        description: Deal identifier in the demo source.
        data_tests: [unique, not_null]
      - name: amount
        description: Deal value in the source currency.
```

## 6. Marts: when the human asks

The MCP serves marts only: each mart becomes one dataset in `osi/datasets.json`, and core alone
gives the people asking questions nothing. Once core is green, ask the human which questions
people will ask, and propose a mart for them.

- `dbt/models/marts/<domain>_<subject>.sql`, domain first (`sales_pipeline`,
  `finance_invoice`), built from core models only.
- In `dbt/models/marts/schema.yml`: a description on the model and every column, and a key,
  either a column tested `unique` and `not_null` or, for a composite key,
  `meta.osi.primary_key: [country, stage]` in the model's `config`.
- `meta.osi.synonyms` on a column or model that people call by other names, so the MCP finds it:

```yaml
      - name: amount
        description: Deal value in the source currency.
        config:
          meta: {osi: {synonyms: [value, deal size]}}
```

## 7. Build until green

```bash
vecten dbt-build-local --json
```

It starts a disposable warehouse (a Postgres container, which needs Docker, on a Postgres
project; the in-process Snowflake emulation, without Docker, on a Snowflake project), replays
every connector's fixtures, builds and tests the project, then writes `osi/datasets.json` from
the marts. The document carries `ok`,
`replayed` (tables per connector), `failed` (one entry per failing node: `name`, `status`,
`message`), `datasets_changed` and `stale`. React to the exit code:

- **0**: green. `datasets_changed: true` means the build rewrote `osi/datasets.json`;
  `false` means nothing the datasets are generated from changed, so the file is already fresh.
- **3**: a prerequisite. "no fixtures for <source>" names
  `vecten connector run <source> --record`, which writes real records into the project to be
  committed, so hand the source to the `connector` skill, which tells the human first. "Docker
  is needed" means asking the human to start Docker.
- **5**: dbt failed. Fix each node `failed` names, then build again. Repeat until exit 0. A
  staging node failing because its raw relation does not exist means a declared table has no
  fixture: stop and hand back as step 1 says, rather than building again.
- **6**: the semantic documents. "cannot be generated" lists what in the marts' `schema.yml`
  blocks the dataset; "dbt cannot parse" names an `osi/` document, usually a metric in
  `osi/metrics.json` that reads a mart you renamed or removed. Metrics are the `semantic`
  skill's; tell the human which metric and why.

The four convention tests fail as nodes of those names, each message reading
`model <name>: <reason>`:

| Test | What a failure means | Fix |
|---|---|---|
| `convention_model_naming` | A model's name breaks its layer's rule, or it lands outside `staging`, `snapshots`, `core` and `marts`. | Rename the file and every `ref` to it. |
| `convention_descriptions` | A core or mart model or column has no description, or a built column is not declared in `schema.yml`. | Declare and describe it. |
| `convention_core_primary_key` | A core model has no column tested both `unique` and `not_null`. | Test the key column. |
| `convention_staging_load_metadata` | A staging model lacks `_loaded_at` or `_source`. | Add both columns. |

A failing data test is information about the data: a `unique` failure on a key means the
staging model or the snapshot key is wrong, or the source really repeats ids, which the human
needs to hear. Fix the model; never silence a failure by deleting a test, a description or a
column from `schema.yml`, and never touch `osi/datasets.json` or the fixtures.

When a test fails only on a deployed environment's data in a Snowflake project, as `operate`
reports it, read that data with `vecten dbt --env <env> test --select <test>` or
`vecten dbt --env <env> show --select <model>`. `--env` comes first. On `prod` it runs only
commands that build nothing and exits 4 on anything else; never try to build `prod` this way.
The `dbt_build` DAG builds it once the fix merges and ships. The fix is still proved with
`vecten dbt-build-local --json`. A Postgres project exits 4 on `--env`.

## 8. Validate the semantic model

After a green build:

```bash
vecten mcp validate --json
```

It checks every document under `osi/` the way the server will. Exit 0 means valid; show the
human any `warnings` it lists. Exit 6 lists the errors: one in `osi/datasets.json` means
rebuilding, one in `osi/metrics.json` belongs to the `semantic` skill.

## 9. Finish

Run `vecten next --json`. The source is modelled when it lists no `model` item for it and no
`Build the dbt project locally`. Show the human what now exists: the staging, snapshot, core and
mart models, the tests that pass, and the datasets `osi/datasets.json` now holds. Then go back
to the loop that dispatched you.
