## MODIFIED Requirements

### Requirement: dbt runs under Airflow from the shipped bundle

The rendered project SHALL include an Airflow DAG that builds the dbt project from the shipped content bundle against the environment's warehouse, using the same profile mechanism as the CLI and resolving dbt dependencies through `vecten-platform` first. It SHALL be scheduled on the Airflow assets the sync DAGs emit: by default once every group's asset has updated, or, as the DAG file chooses, on the assets of the groups it names and once any one of them has updated. It SHALL archive `run_results.json` and `sources.json` to the bucket per run, after a green build of the whole project SHALL write the bundle's commit as the environment's built commit, and after any green build SHALL emit the `dbt_build` asset, whose event records the run's selection, for other DAGs to schedule on.

#### Scenario: Scheduled build

- **WHEN** the dbt DAG runs on the deployed stack
- **THEN** the dbt project builds against the environment's warehouse, the run's success or failure is visible in Airflow, its results are archived to the bucket, and on success the built commit advances to the bundle's commit

#### Scenario: Freshness

- **WHEN** the dbt DAG runs source freshness on `_loaded_at` and a source is stale
- **THEN** `sources.json` in the bucket records it and `vecten issues` lists it resolved by `connector`

#### Scenario: Build on any group

- **WHEN** the DAG file builds on the `hourly` and `daily` groups with `trigger="any"` and the `hourly` sync completes
- **THEN** the dbt DAG runs without waiting for the `daily` sync

#### Scenario: A misspelt group

- **WHEN** the DAG file names a group no connector declares
- **THEN** the DAG file fails to import with a message naming the group and the declared ones

#### Scenario: Built signal

- **WHEN** the build and the convention tests pass
- **THEN** the `dbt_build` asset is emitted, and a DAG scheduled on it runs
- **AND** when the build fails, no `dbt_build` asset is emitted

## ADDED Requirements

### Requirement: dbt DAG run parameters

The dbt DAG SHALL offer the run parameters `select` and `exclude`, each a space-separated list of dbt node selectors, and `full_refresh`, a boolean, and SHALL pass them to `dbt build`. The DAG file SHALL set their defaults, which are an empty selection, no exclusion and no full refresh, so a scheduled run builds every node. A selector that starts with `-` SHALL be refused. A build whose `select` or `exclude` is not empty SHALL leave the built commit unchanged, since it does not build the whole project. The DAG file SHALL be able to override whether the DAG starts paused when it first appears, as the sync DAG options do.

#### Scenario: Intraday refresh

- **WHEN** the dbt DAG is triggered with `select` set to `tag:intraday`
- **THEN** dbt builds only the nodes tagged `intraday`, the convention tests run, the `dbt_build` asset is emitted with that selection, and the built commit does not change

#### Scenario: Full refresh

- **WHEN** the dbt DAG is triggered with `full_refresh` set to true
- **THEN** `dbt build` runs with `--full-refresh` over every node and the built commit advances on success

#### Scenario: A selector that is a flag

- **WHEN** the dbt DAG is triggered with `select` set to `--vars {}`
- **THEN** the build task fails without running dbt
