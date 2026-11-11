# The data warehouse

For the person deciding where the data lives: Postgres or Snowflake, the laptop stack against a deployed warehouse, bringing a warehouse you already run, and how dbt models the raw data into the schema the MCP reads.

## Postgres or Snowflake

A project that builds its own warehouse chooses one at the intake (`vecten answer warehouse
postgres` or `snowflake`), and the choice holds for the project's life.

- **Postgres** is the default and the shortest path: `vecten deploy` creates a managed instance
  in your cloud account (RDS on AWS, Managed Database for PostgreSQL on Scaleway), the laptop
  stack runs the same engine in Docker, and every component, the MCP included, works on the
  laptop and deployed alike. Each person who asks questions through the MCP gets a Postgres role
  of their own, and every query runs as that role.
- **Snowflake** keeps the data in an account you own: Terraform under `deploy/terraform/snowflake`
  creates the database, warehouse, roles and the loader user; `--snowflake-account` names the
  account at `vecten init`. Two things differ from Postgres. The laptop stack has no MCP, because
  Snowflake's External OAuth trusts the deployed MCP's issuer only; `vecten up` starts Apache
  Airflow alone and says so. And the MCP starts only once an ACCOUNTADMIN has run the
  provisioner script `vecten mcp provisioner-script` prints, so the first deploy ships no host
  until that run is recorded. Each person gets a Snowflake user of their own.

The [MCP identity and access](mcp-identity.md) page has the identity model for both engines.

## Local and deployed

`vecten up` runs the project on your laptop: Apache Airflow at `https://airflow.localhost:8443`,
the MCP at `https://mcp.localhost:8443`, and a Postgres container that keeps its data between
runs until `vecten down --volumes` removes it. Everything the laptop stack needs lives in the
project's `.env`, which the CLI writes with mode 0600 and never commits. A deployed environment
runs the same images on your cloud with the managed database; [Deploying to AWS](deploy-aws.md)
and [Deploying to Scaleway](deploy-scaleway.md) say what it is made of.

## Bringing your own warehouse

For a warehouse you already operate, initialize only the MCP component:

```sh
vecten init warehouse-assistant --name warehouse-assistant --users 5 \
  --has-warehouse yes --warehouse-url postgres://warehouse.example.com/analytics
cd warehouse-assistant
vecten up
vecten next --json
```

A Snowflake locator has the form `snowflake://account/database`. Locators never contain
passwords; use `vecten secret set` when the relevant setup step asks for a credential.
Before starting MCP against the existing warehouse, review the provisioner setup and
credentials in the [MCP identity guide](mcp-identity.md). The server checks the
provisioner's grants at startup and refuses to start when one is missing.

For a new warehouse, follow the README's try-it steps, add and record a source, then build models as
in the Modelling section below. `vecten next --json` orders the remaining work. Deploy questions
come after the local ingestion and modelling loop. Creating a warehouse and connecting an
existing one share the same CLI, but the latter has no connectors or dbt project.

## Modelling

dbt models the raw data into the schema convention, version 1, which every project carries as
`docs/schema-convention.md`. A project with the `platform` component has this layout:

    dbt/models/staging/   staging_<source>_<entity>: raw columns cast to types, plus _loaded_at and _source
    dbt/snapshots/        snapshot_<source>_<entity>: one per staging model, history of business columns
    dbt/models/core/      one table per entity, a singular noun (company, deal), with a tested primary key
    dbt/models/marts/     domain-prefixed tables (sales_pipeline) with a primary key; each one is a dataset in osi/
    osi/                  the Apache Ossie semantic model, JSON, read by dbt and by the MCP

The tests that enforce the convention, the identity macros and dbt's time spine ship in the
`vecten_convention` dbt package inside `vecten-platform`. `vecten dbt-build-local`, `vecten dbt`
and the dbt DAG write `dbt/packages.yml` with local paths before each run, so the file is
git-ignored.

### The project's own dbt packages

To use a package from [dbt hub](https://hub.getdbt.com) or a public git repository, such as
`dbt_utils`, list it in `dbt/project-packages.yml` and commit that file. It has dbt's
`packages:` shape:

```yaml
packages:
  - package: dbt-labs/dbt_utils
    version: 1.3.0
  - git: https://github.com/dbt-labs/dbt-utils.git
    revision: e164c645238ca98f21b2ecc558912b94b3d75a9f   # a full commit id
    subdirectory: some_package                            # optional
```

Before each run, vecten appends these entries to the generated `dbt/packages.yml`, after its
own `local:` entries. This happens on the laptop (`vecten dbt`, `vecten dbt-build-local`), in
CI, and on the deployed host, where the dbt DAG's `deps` task installs them. The file ships in
the bundle with the rest of `dbt/`. Each machine runs `dbt deps` on its own, so every entry
must name exactly one release or one commit. These entries are refused, with exit 6 before dbt
runs:

- a hub `version` that is not one `X.Y.Z` release, such as a range or `1.3`;
- a git `revision` that is not a full 40-character commit id, such as a tag or a branch;
- a git URL that is not `https://`, or that carries a user, a token or Jinja;
- a `local:`, `tarball:` or `private:` entry (a local dbt package belongs in an
  [extension](extensions.md));
- a package listed twice, or one that installs under the name of a generated entry
  (`vecten_convention` or an extension's dbt project).

Private git packages are not supported, because the hosts hold no git credentials. A hub
package's own dependencies are resolved from the version ranges that package declares.

A deployed host installs the packages on every dbt DAG run. It needs outbound HTTPS to
`hub.getdbt.com`, and for git packages to `github.com` and `codeload.github.com`, and the
Airflow image needs `git`, which it has. The AWS and Scaleway modules allow all outbound traffic
from the hosts, so nothing needs configuring. If the hub or GitHub cannot be reached, the
`deps` task fails, the build does not run, and `vecten issues --env <env>` lists the failure.

### Commands

- `vecten dbt-build-local` builds and tests the project with no warehouse credentials. It needs
  recorded fixtures for every connector (`vecten connector run <source> --record`) and, for a
  Postgres project, Docker, which runs a disposable Postgres container; a Snowflake project needs
  nothing, because it builds on a Snowflake emulation over DuckDB. It replays the fixtures into
  `raw_<source>`, runs `dbt build` and then the convention tests, and after a green build
  regenerates `osi/datasets.json`, checks `osi/` with `dbt parse` and records the result in
  `.vecten/records/dbt-build-local.json`. dbt builds against an empty Ossie directory, because
  `osi/datasets.json` is the build's output, not its input.
  With `--check` it never touches `osi/datasets.json` and exits 6 when the committed file is
  stale; the rendered CI runs it that way. A dbt failure exits 5, names the failing models and tests, and
  leaves `osi/datasets.json` as it was. A mart without a primary key, or an `osi/` document dbt
  cannot parse, exits 6 and names the mart or the document.
- `vecten dbt <args>` passes its arguments to dbt unchanged, against the laptop stack's Postgres
  (`vecten up` first), and exits with dbt's code. It writes `dbt/packages.yml` before every
  invocation; run `vecten dbt deps` first on a fresh checkout, and remember that
  `vecten dbt build` does not run the convention tests as a second step. The laptop stack has no
  Snowflake, so on a Snowflake project it refuses and points at `--env` and at
  `vecten dbt-build-local`.
- `vecten dbt --env <env> <args>` runs dbt against a deployed environment's Snowflake warehouse;
  see the next section.

### dbt against a deployed environment

`vecten dbt --env <env> <args>` runs dbt against the environment's own Snowflake warehouse.
`--env <env>` must come first, before dbt's arguments. dbt signs in as the environment's
`LOADER` service user, the identity the dbt DAG builds with. Its private key,
`SNOWFLAKE_LOADER_PRIVATE_KEY`, is read from the environment's secrets store with your cloud
login, and the database and `<PROJECT>_<ENV>_WH` warehouse come from `vecten.toml`. The key
reaches dbt only through the process environment. It is never written to `profiles.yml` or any
other file, and dbt masks it in its logs. Run `vecten dbt --env <env> deps` first on a fresh
checkout.

On `prod`, only commands that build nothing run: `clean`, `compile`, `debug`, `deps`,
`docs generate`, `docs serve`, `ls`/`list`, `parse`, `show`, `source freshness` and `test`.
Every other command exits 4 before any secret is read, including `run`, `build`, `seed`,
`snapshot`, `clone`, `retry`, `run-operation` and any command not on that list. The dbt DAG
builds `prod`. Some things would make an allowed command write, so on `prod`:

- `test --store-failures` is refused, and a `DBT_STORE_FAILURES` set in your shell is ignored;
- `test` leaves out unit tests, because each one creates a temporary table (they need no prod
  data, and `vecten dbt-build-local` runs them), and `--exclude-resource-type` is refused because
  it would bring them back;
- before `test` or `source freshness`, which execute the project's `on-run-start` and
  `on-run-end` hooks, vecten runs `dbt parse` without the key. It refuses when the project or any
  of its packages declares such a hook, or, for `test`, when any test stores its failures
  (`store_failures`, or `store_failures_as: table` or `view`). `compile`, `show` and
  `docs generate` never execute hooks, so they still run.

`LOADER` has write grants: no Snowflake role reads raw through marts without them. On `prod`,
vecten's checks are what keep a run read-only, and they cannot see SQL that Jinja runs while
dbt compiles, such as `run_query` in a model or macro, or Jinja passed to `show --inline`. Treat
`--env prod` as a guard against accidents, not as a permission boundary. Anyone who can run it
can also read the `LOADER` key.

Any other environment runs every dbt command. That is the way to a scratch or CI database on
Snowflake: deploy another environment, in its own Snowflake account because one account serves
one environment, and run `vecten dbt --env <name> build` there. CI that needs no real Snowflake
keeps running `vecten dbt-build-local --check` on the emulation.

A Postgres warehouse is deployed on the environment's private network, which the laptop cannot
reach, so `--env` on a Postgres project exits 4. The dbt DAG builds it, and
`vecten issues --env <env>` reports its failures.
- `vecten mcp validate` checks every document under `osi/` the way the MCP server will load it:
  the pinned Ossie schema, duplicate names across documents, a usable dialect for every
  expression, relationships and each metric's home dataset. It warns when a metric declares no
  additivity and either sums over a snapshot-derived dataset or uses `DISTINCT`, `AVG`, a division
  or a window function. Whether each dataset's
  source table exists needs a warehouse connection: `vecten mcp validate` skips it and says so;
  the server checks it when it loads the model. Errors exit 6. With `--evals` it also compiles
  every case in `evals/semantic.yaml` without a warehouse and exits 6 naming each case whose
  outcome or SQL hash no longer matches, with the new SQL.
- `vecten mcp import --dbt <path> [--schema marts]` writes `osi/datasets.json` from an existing
  dbt project's `target/manifest.json` and `target/catalog.json`, with the generator
  `vecten dbt-build-local` uses: one dataset per model in the mart schema, each source named with
  `[warehouse] database` from `vecten.toml`. Run `dbt docs generate` with the production target
  first so the model schemas match. It never touches `osi/metrics.json`. A missing artifact exits
  3; a mart without a primary key or a catalog entry exits 6.

The `osi/` rule: `osi/datasets.json` is generated from dbt by `vecten dbt-build-local` and never
edited by hand; descriptions live in dbt's `schema.yml`. `osi/metrics.json` is written by hand,
with every column qualified by its dataset (`SUM(sales_pipeline.amount)`). Every mart declares a
primary key, one column tested `unique` and `not_null` or a composite key under
`meta.osi.primary_key`, because dbt requires an entity on every semantic model. To rename or drop
a mart that a metric uses, edit or remove the metric in `osi/metrics.json`, then run
`vecten dbt-build-local`, which regenerates `osi/datasets.json`. The
[semantic model guide](semantic-model.md) covers the home-dataset rule, the allowed
functions, additivity, eval cases and how the server loads a shipped model.

## The semantic model

The MCP answers questions from the Apache Ossie documents under `osi/`: `datasets.json`,
generated from dbt, says what tables and columns exist; `metrics.json`, written by hand, says
what the business measures mean. [Authoring the semantic model](semantic-model.md) covers both,
and the [feedback loop](feedback-loop.md) covers how a wrong definition gets fixed through a
pull request.

Back to the [docs index](README.md).
