## Why

The two DAG builders fit the default project only. `build_sync_dag` runs every object in
parallel, calls `source()` with no arguments and reads a cron string in UTC; it cannot order a
child object after its parent, take run parameters, share a pool, skip a run on a "halted"
flag, or run an extension's groups on their own schedules. `dbt_build` always runs the same full
build, waits for every sync group, and emits nothing another DAG can wait on. Projects replace
the rendered DAG files by hand to get any of this.

A sync DAG also publishes its asset when an object failed, so `dbt_build` builds on a partial
sync. That is a defect, and it is fixed here as well.

## What Changes

- A sync DAG publishes its group's asset only after `check` has found every object synced, so
  a run with a failed object no longer triggers `dbt_build`.
- `build_sync_dag` takes keyword options: `stages`, `params`, `pool`, `guard`, `timezone` and
  `paused_at_creation`. Each defaults to today's behaviour.
- An extension's `vecten.connectors` declaration takes `groups`, each with its objects, cron
  `schedule` and `timezone`; `connector add` renders one DAG per group on its schedule.
- `dbt_build` has run parameters `select`, `exclude` and `full_refresh`. `build_dbt_dag` takes
  their defaults, a `trigger` of `all` or `any`, and `paused_at_creation`; `dbt_assets` takes
  the groups to wait on. A green build emits the `dbt_build` asset. A narrowed build leaves the
  built commit alone.

The per-environment default for whether new DAGs start paused is a separate change; the
builders' `paused_at_creation` overrides it for one DAG.

## Capabilities

### Modified Capabilities

- `platform/connectors`: sync DAG behaviour (asset only after every object synced, stages) and
  the sync DAG options.
- `platform/dbt-modelling`: the dbt DAG's schedule, run parameters and built asset.
- `shared/extension-points`: groups and schedules in an extension connector's declaration.

## Impact

- `packages/vecten-platform/src/vecten_platform/sync/airflow_dag.py`, `.../sync/alerts.py`,
  `.../dbt/airflow_dag.py`, `.../dbt/runner.py`
- `packages/vecten-shared/src/vecten_shared/issues.py` (which task ids are per-object sync
  tasks), `packages/vecten/src/vecten/issues/airflow.py`
- `packages/vecten/src/vecten/connectors/extensions.py`, `add.py`, `render.py`,
  `templates/sync_dag.py.j2`
- The rendered `orchestration/dags/*.py` of existing projects are unchanged and keep working.
