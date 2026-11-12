## MODIFIED Requirements

### Requirement: Sync DAG behaviour

Each rendered sync DAG SHALL run one task per object, with an independent checkpoint per object and retries at object level, so a failure in one object does not block or reset the others. It SHALL emit an Airflow asset per object group for the dbt DAG to schedule on, only once every object of the run has synced; a run in which any object failed or did not run SHALL emit no asset and SHALL end failed. It SHALL post failures to the alert webhook when one is set. When the DAG file orders the objects in stages, each stage SHALL start once every object of the stage before it has synced, objects within a stage SHALL run in parallel, and the objects of a stage after a failed one SHALL not run.

#### Scenario: One object fails

- **WHEN** a sync DAG runs and one object's extraction fails
- **THEN** the other objects' tasks complete and keep their checkpoints
- **AND** the failed object is retried without re-extracting the succeeded ones

#### Scenario: No asset after a failed object

- **WHEN** a sync DAG runs and one object's extraction fails after its retries
- **THEN** the group's asset is not emitted, so the dbt DAG does not run on that sync
- **AND** the run ends failed, naming the failed object

#### Scenario: Ordered stages

- **WHEN** a sync DAG declares the stages `[["companies"], ["contacts"]]`
- **THEN** `contacts` starts only after `companies` has synced
- **AND** when `companies` fails, `contacts` does not run and the run's failure names it as not run

#### Scenario: A staged object's failure is keyed by the object

- **WHEN** an object in the second stage fails
- **THEN** its alert and its `vecten issues` entry carry the same id as for that object in a DAG without stages

## ADDED Requirements

### Requirement: Sync DAG options

`build_sync_dag` SHALL accept keyword options that the DAG file sets, each defaulting to the behaviour of a DAG without it: `stages` (the objects in order), `params` (run parameters), `pool` (an Airflow pool for the object tasks), `guard` (a function run before anything else), `timezone` (the IANA zone a cron schedule is read in) and `paused_at_creation` (whether the DAG starts paused, overriding the environment's default). Every declared run parameter SHALL be passed to the connector's `source()` as a keyword argument, with its default on a scheduled run and the value given on a manual one; a key the DAG does not declare SHALL NOT be passed. A guard that returns false SHALL skip the run with nothing synced, no asset emitted and no failure. An option the DAG cannot honour, such as an unknown timezone or stages naming an object twice, SHALL fail the DAG file's import with a message naming it.

#### Scenario: Defaults

- **WHEN** a DAG file calls `build_sync_dag` with none of the options
- **THEN** the DAG has no run parameters, no guard, the default pool, a UTC schedule and the environment's paused default

#### Scenario: Manual backfill

- **WHEN** a sync DAG declares a `since` parameter defaulting to null and a run is triggered with `since` set to `2026-01-01`
- **THEN** `source(since="2026-01-01")` is called for that run, and `source(since=None)` on the next scheduled run

#### Scenario: Halted source

- **WHEN** a sync DAG's guard returns false because a quota flag is set
- **THEN** the run succeeds with every other task skipped, no object is synced and no asset is emitted

#### Scenario: Local schedule

- **WHEN** a sync DAG is scheduled `0 6 * * *` with `timezone="Europe/Warsaw"`
- **THEN** its data intervals end at 06:00 in Warsaw, across daylight-saving changes

#### Scenario: Shared pool

- **WHEN** two sync DAGs name the same pool of one slot
- **THEN** their object tasks run one at a time across both DAGs, while discovery and the check run outside the pool

#### Scenario: Paused at creation

- **WHEN** a DAG file sets `paused_at_creation=True` and the DAG appears on an environment whose default is unpaused
- **THEN** the DAG starts paused, and the setting does not change a DAG that already exists
