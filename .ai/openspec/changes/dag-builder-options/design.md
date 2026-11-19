## Context

`build_sync_dag` and `build_dbt_dag` build every DAG a project runs. A project renders
`orchestration/dags/sync_<connector>_<group>.py` with `vecten connector add` and
`orchestration/dags/dbt_build.py` with `vecten init`; both files call a builder with the
defaults. Airflow 3.3.2 (task SDK 1.3.2), pinned by `versions.toml`, is the API every choice
below was read from; Airflow is installed only in the Airflow image, so the DAG-structure tests
run inside it (`test_sync_dag_in_image.py`, `test_dbt_dag_in_image.py`).

## Goals / Non-Goals

**Goals:**

- Every option is a keyword argument whose default reproduces today's DAG, so the rendered
  files and existing projects keep working unchanged.
- A sync DAG never lets `dbt_build` start on a partial sync.
- `vecten issues` and the failure alert keep keying an object's failure by the object, whatever
  shape the sync DAG takes.

**Non-Goals:**

- The per-environment default for whether a new DAG starts paused. A separate change sets it in
  the stack; `paused_at_creation` here overrides it for one DAG.
- Recording group schedules in `vecten.toml`, or ordered stages in an extension's declaration.
- Creating Airflow pools. A pool lives in Airflow's metadata database, and the DAG file names it.

## Decisions

### The asset follows `check`

The sync DAG was `sync >> publish(all_done) >> check(all_done)`: `publish` emitted the asset
whatever the objects did, and `check` failed the run afterwards. It is now
`sync >> check(all_done) >> publish(all_success)`. `check` still runs after a failed object and
names it; `publish` then ends `upstream_failed`, emits no asset event, and as the leaf marks the
run failed.

- *`publish` with `none_failed`, kept beside `check`*: also skips the asset after a failure, but
  leaves two tasks deciding whether the run is good, and a stage that never expanded counts as
  neither failed nor successful there.
- *Setting the outlet event from inside `publish` only when the states are green*: an outlet is
  emitted on task success, so this needs the same state read `check` already does.

### Ordered objects: `stages`

`stages=[["companies"], ["contacts", "deals"]]` names the objects in order. Each stage is a
mapped task over a literal list, `sync_stage_1`, `sync_stage_2`, …, with the default
`all_success` rule, so a stage starts once every object of the one before it synced, and after a
failure the later stages end `upstream_failed`. `check` waits on every stage task, because a
stage after a failed one is done at once while the failed stage may still be running; it reports
those objects as "not run after an earlier stage failed". Without `stages` the DAG keeps the one
`sync` task over the discovered objects. `objects` may be empty or list the same objects.

`vecten_shared.issues.is_sync_task` names the per-object tasks (`sync` and `sync_stage_<n>`), and
the alert callback and `vecten issues` use it, so `sync.deals` is the issue key in either shape.

- *A dependency map, `after={"contacts": ["companies"]}`*: says what depends on what, but object
  tasks must stay mapped for the map-index key, so it would compile to the same waves; the
  explicit list says what runs.
- *One mapped task with `max_active_tis_per_dagrun=1`*: serial, but Airflow does not promise map
  index order, and a child would still run after its parent failed.
- *One unmapped task per object*: exact edges, but drops the map index that `vecten issues` and
  the alert key on.

### Run parameters: `params`

`params` becomes the DAG's Airflow `params` (plain values or `airflow.sdk.Param`), so the UI's
trigger form offers them and validates their types. The `discover` and object tasks pass the
declared names to `source()` as keyword arguments, read from `get_current_context()["params"]`.
Only declared names are passed: with `core.dag_run_conf_overrides_params` (the default) a run's
conf can carry keys the DAG never declared. A scheduled run gets the defaults.

- *Reading `dag_run.conf` directly*: no form, no validation, and every undeclared key would reach
  `source()`.

### `pool`

The object tasks (`sync` or each stage) take `pool=<name>`; `discover`, `check` and `publish`
stay in `default_pool`, so a narrow pool shared with another DAG is spent on API calls only.
Without the option no `pool` is set. Airflow 3.3.2 never schedules a task whose pool does not
exist ("Tasks using non-existent pool … will not be scheduled"), so the docs say to create it.

- *`pool` in `default_args`*: would also hold slots for the bookkeeping tasks.

### `guard`

`guard` is a function of no arguments. It runs as the first task, `guard`, a
`@task.short_circuit` (`ShortCircuitOperator`, `ignore_downstream_trigger_rules=True`). A falsy
return skips every downstream task, including the `all_done` `check`, so the run ends successful
with nothing synced and no asset; an exception fails the task and alerts like any other.

- *Raising `AirflowSkipException` from `discover`*: `check` (`all_done`) would still run and fail
  on the missing object list.
- *A sensor that waits for the flag to clear*: holds a worker slot and delays rather than skips.

### `timezone`

A cron string becomes `CronDataIntervalTimetable(cron, timezone=<zone>)`, the timetable it was
before with `"UTC"`; `timezone` names the IANA zone, checked with `zoneinfo` when the DAG file
is parsed. The data-interval timetable is kept because it is what lets a new DAG run its latest
whole interval at once. A timetable passed as `schedule` is used as given, and `timezone` beside
it is refused rather than ignored.

- *`CronTriggerTimetable`*: runs at the boundary rather than for the interval before it, which
  changes when a fresh deploy first syncs.

### `paused_at_creation`

Both builders map it to the DAG's `is_paused_upon_creation`. `None`, the default, leaves the
environment's `AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION` in charge. Airflow 3.3.2 applies it
only when the DAG's row is first created (`_create_orm_dags`), so it never unpauses or pauses an
existing DAG.

### Extension groups

An extension's connector declaration may carry
`groups={"daily": {"objects": [...], "schedule": "0 6 * * *", "timezone": "Europe/Warsaw"}}`,
or `{"daily": [...]}` for objects alone. `connector add` records the objects under the
connector's `groups` in `vecten.toml`, as for any connector, and renders each group's DAG file
with its `schedule` and `timezone`. The schedule lives in the rendered DAG file, which the
project owns; a re-render of a declared extension connector takes it from the installed
extension, and a group the extension does not declare gets `@daily`. A declaration whose group
name, objects, schedule or zone cannot render a DAG is refused as `CONFIG`; the schedule's cron
syntax is left to Airflow, since `croniter` is not a CLI dependency.

- *A schedule per group in `vecten.toml`*: changes the shared config schema of every project.

### dbt run parameters

`dbt_build` declares Airflow params `select` and `exclude` (strings of space-separated dbt
selectors, as dbt's own `--select` takes them) and `full_refresh` (boolean). `build_dbt_dag`'s
keywords of the same names set their defaults, and `build_args` turns them into
`dbt build [--select …] --exclude <convention tests> […] [--full-refresh]`; with the defaults
that is the old `BUILD_STEP`, which stays as `build_args()`. A token that starts with `-` is
refused, since a run parameter must not add dbt flags.

A narrowed build (a non-empty `select` or `exclude`) skips `mark_built`: the built commit tells
the MCP's model service that the bundle as a whole is built, and it swaps in a newly shipped
semantic model only then, so a narrowed build must not claim it.

- *Array params*: the trigger form shows one textarea line per item, less like dbt's CLI.

### Which syncs trigger the build

`trigger="all"` keeps the list schedule, which Airflow turns into `AssetAll`; `trigger="any"`
schedules on `AssetAny` of the same assets. `dbt_assets(config, groups)` keeps the groups named
as `<group>` (that group of every connector) or `<connector>.<group>`, and refuses a name no
connector has, which would otherwise leave the build waiting on nothing it names.

- *Any asset condition expression*: more than the issue asks; a hand-written DAG can still pass
  one.

### The built asset

A `publish` task after `convention` emits `Asset(name="dbt_build", uri="vecten://dbt/build")`;
its event's `extra` holds the run's `select`, `exclude` and `full_refresh`, so a DAG scheduled on
it can tell a narrowed build. It sits beside `mark_built` rather than after it, so a narrowed
build still emits it.

- *The outlet on `convention` or `mark_built`*: `convention` names a test step, and `mark_built`
  is skipped on a narrowed build.

## Risks / Trade-offs

- A project that relied on `dbt_build` running after a partial sync now waits for the next
  green sync. That is the intent; the changelog says so.
- A sync DAG with stages has no `sync` task; Airflow's run history shows the stage tasks from
  the first run with stages on.
- A pool named in a DAG file and missing in Airflow leaves the object tasks queued; Airflow logs
  a warning but nothing fails.

## Migration Plan

None. Existing DAG files call the builders with the defaults, and `connector add` renders the
same file for every connector without declared groups.
