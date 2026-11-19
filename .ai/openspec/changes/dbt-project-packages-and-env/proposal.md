## Why

Two gaps in running dbt, both reported by a client.

- A project cannot add a dbt package such as `dbt_utils` (issue 44). `dbt/packages.yml` is
  regenerated before every dbt run with `local:` entries only, git-ignored and left out of the
  bundle, so anything a project writes there is lost. Projects copy macros in by hand instead.
- `vecten dbt` refuses every Snowflake project (issue 53), because the laptop stack has no
  Snowflake, and there is no `--env`. A Snowflake project cannot compile, test or debug against
  a deployed environment from the CLI, and CI has only the emulation, which does not cover every
  Snowflake feature. People generate `packages.yml` and call dbt with hand-built variables.

## What Changes

- A committed `dbt/project-packages.yml` lists the project's own dbt packages: hub packages
  pinned to one release and public git repositories pinned to a commit. Resolution appends them
  after the generated `local:` entries, on the laptop (`vecten dbt`, `dbt-build-local`), in CI,
  and on the hosts (the dbt DAG's `deps` task). Unpinned, private, local or tarball entries,
  duplicates and collisions with a generated entry exit 6 before dbt runs.
- `vecten dbt --env <env> <args>` runs dbt against that environment's Snowflake warehouse as its
  `LOADER`, with the key from the environment's secrets store passed through the process
  environment only. On `prod` only commands that build nothing run; every command runs on any
  other environment.
- On `prod`: an allow-list of dbt commands (deny by default); `test --store-failures` and
  `test --exclude-resource-type` refused; unit tests left out of `test`; and before `test` or
  `source freshness`, a `dbt parse` that refuses a project with on-run hooks or tests that store
  their failures.
- `--env` on a Postgres project is refused: the deployed warehouse is on a private network.
- Without `--env`, behaviour is unchanged; the Snowflake refusal now names `--env`.

## Capabilities

### Modified Capabilities

- `platform/dbt-modelling`: the pass-through's one argument of its own, dependency resolution
  with project packages, pinned project packages, dbt against a deployed environment, and prod
  runs that build nothing.
- `shared/deploy`: the platform host keeps outbound HTTPS to dbt hub and GitHub.

`cli/cli-lifecycle` is unchanged: `vecten dbt` already refuses with vecten's exit codes before
dbt starts, and forwards dbt's code once it does.

## Impact

- `packages/vecten-platform/src/vecten_platform/dbt/deps.py`: project packages, validation and
  the merge.
- `packages/vecten/src/vecten/dbt_env.py` (new) and `.../cli/commands/dbt.py`: `--env`.
- Docs: `docs/warehouse.md`, `docs/cli.md`, the rendered runbook, the `model` skill and the
  rendered plugin.
- No change to `runner.py`, `airflow_dag.py`, Terraform, the images or `vecten.toml`.
