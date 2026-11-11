# Orchestration

For the person who wants to know what runs when, and where to look when it does not: what Apache Airflow does in a Vecten Open project, the two kinds of DAG, reading Airflow on the laptop, shipping, and failures.

## What Apache Airflow does here

A project with the `platform` component runs Apache Airflow 3 with the LocalExecutor, pinned by
the release and changed only by `vecten update`. It does two jobs: run each connector's sync on
its schedule, and run the dbt build once the syncs it depends on have landed. Everything Airflow
runs comes from the project repository: the DAGs under `orchestration/dags/`, the connectors
under `connectors/`, the dbt project under `dbt/`. Nothing is written in the Airflow UI.

## The sync DAGs

`vecten connector add <name>` renders one DAG per object group of the source,
`orchestration/dags/sync_<name>_<group>.py` (one `default` group unless the connector declares
others), from a template the project owns. Each DAG:

- runs `@daily` in UTC, or on the schedule an extension connector declares for the group, and
  runs its first sync as soon as it is unpaused rather than at the next midnight; it never
  backfills past runs (`catchup=False`) and runs one instance at a time;
- runs one task per object with an independent checkpoint per object, so a failure in one object
  neither blocks nor resets the others, and retries the failed object twice;
- creates the raw schema `raw_<name>` and dlt's staging schema before the object tasks start, so
  objects loading in parallel never race on creating them;
- emits an Airflow asset for its group once every object of the run has synced, which is what
  the dbt DAG schedules on; a run in which an object failed emits none and ends failed, so dbt
  never builds on a partial sync;
- posts a failure to the alert webhook when `vecten answer alert_webhook.<env>` has set one.

### Changing a sync DAG

The DAG file is one call to `build_sync_dag`. Its keyword options change the DAG, and each one
left out keeps the behaviour above:

| Option | What it does |
|---|---|
| `schedule="0 6 * * *", timezone="Europe/Warsaw"` | Reads a cron schedule in that IANA zone instead of UTC, daylight saving included. A timetable object passed as `schedule` is used as given. |
| `stages=[["companies"], ["contacts", "deals"]]` | Runs the objects in that order: each stage starts once every object of the one before it synced, and the objects inside a stage run in parallel. For a child object that reads its parent's ids. When a stage fails, the later ones do not run. The tasks are `sync_stage_1`, `sync_stage_2`, … instead of `sync`. |
| `params={"since": Param(None, type=["null", "string"])}` | Run parameters, offered by Airflow's "Trigger DAG" form. Each is passed to the connector's `source()` as a keyword argument: the value given on a manual run, the default on a scheduled one. `source()` must accept every name declared. |
| `pool="hubspot_api"` | The Airflow pool the object tasks take a slot of, to share an API's rate limit with another DAG. The pool must exist: create it once per environment under Admin, Pools, or with `airflow pools set hubspot_api 1 "HubSpot API"`. A task in a missing pool is never scheduled. |
| `guard=not_halted` | A function run before anything else. When it returns false, the run is skipped: nothing syncs, no asset is emitted, and the run counts as successful. When it raises, the run fails and alerts. |
| `paused_at_creation=True` | Whether the DAG starts paused when Airflow first sees it, overriding the environment's default. It never changes a DAG that already exists. |

For example, a HubSpot group whose contacts read the companies' ids, with a backfill date for
manual runs and a halt flag kept in an Airflow variable:

```python
from airflow.sdk import Param, Variable

from vecten_platform.sync.airflow_dag import build_sync_dag


def not_halted() -> bool:
    return Variable.get("hubspot_halted", default="false") != "true"


dag = build_sync_dag(
    connector="hubspot",
    group="default",
    objects=[],
    schedule="0 6 * * *",
    timezone="Europe/Warsaw",
    stages=[["companies"], ["contacts"]],
    params={"since": Param(None, type=["null", "string"])},
    guard=not_halted,
)
```

`connector.py` then defines `def source(since: str | None = None)`. `vecten connector run`
calls `source()` with no arguments, so every parameter needs a default.

The same sync runs from a shell without Airflow: `vecten connector run <name>` syncs every
object (or `--object <x>` one of them), reports rows per object, exits non-zero when any object
fails, and writes a run record under `.vecten/` that `vecten next` reads. `--record` saves the
objects as fixtures for the offline test and the local dbt build; see [connectors](connectors.md).

## The dbt DAG

`dbt_build` is scheduled on the sync DAGs' assets, so it runs once every group it depends on has
updated. Its tasks run in this order: `deps` installs the `vecten_convention` package into the
container; `build` runs `dbt build`; `convention` runs the convention tests; then `mark_built`
records the commit the build came from, which `vecten next` and the MCP's `model_health` read,
and `publish` emits the `dbt_build` asset (`vecten://dbt/build`), which another DAG can
schedule on to run after a green build. `freshness` runs `dbt source freshness` beside them.
Each task retries once. The run's `run_results.json` and `sources.json` are archived, and that
is what `vecten issues` reads for the `dbt` source. The [warehouse](warehouse.md) page says
what the build produces.

A manual run takes three parameters in Airflow's "Trigger DAG" form: `select` and `exclude`,
dbt node selectors separated by spaces (`tag:intraday`, `+fct_orders`), and `full_refresh`,
which rebuilds incremental models from scratch. A scheduled run uses their defaults: every node,
no full refresh. A run with a `select` or `exclude` builds only part of the project, so it skips
`mark_built` and leaves the recorded commit as it was; it still runs the convention tests and
emits `dbt_build`, whose event's `extra` holds the run's `select`, `exclude` and
`full_refresh`.

`orchestration/dags/dbt_build.py` is one call to `build_dbt_dag`, and its options change the
DAG; each one left out keeps the behaviour above:

| Option | What it does |
|---|---|
| `dbt_assets(config, ["hourly", "stripe.daily"])` in place of `dbt_assets(config)` | Waits on those groups only: `hourly` of every connector, `daily` of `stripe`. A name no connector declares fails the DAG file's import. |
| `trigger="any"` | Builds once any of the awaited groups has updated, rather than all of them, so an hourly group does not wait for a daily one. |
| `select="tag:daily"`, `exclude="tag:slow"`, `full_refresh=True` | The defaults of the run parameters, so scheduled runs use them too. With a default `select` or `exclude` no scheduled run records the built commit, and the MCP holds each newly shipped semantic model at `shipped, waiting for build`, so keep them empty unless another DAG builds the whole project. |
| `paused_at_creation=True` | Whether the DAG starts paused when Airflow first sees it, overriding the environment's default. |

For example, to build every time an hourly group lands:

```python
from pathlib import Path

from vecten_platform.dbt.airflow_dag import build_dbt_dag, dbt_assets
from vecten_platform.sync.context import runtime
from vecten_shared.config import load_config

config = load_config(Path(runtime().project_root))
dag = build_dbt_dag(dbt_assets(config, ["hourly"]), trigger="any")
```

## Reading Airflow on the laptop

`vecten up` serves Airflow at `https://airflow.localhost:8443` with a self-signed certificate;
plain HTTP on `http://airflow.localhost:8480` redirects to HTTPS. The stack avoids port 8080
because local Airflow installs often hold it. The Airflow API is also published on
`http://127.0.0.1:8482`, loopback only, with the user `admin` and the password
`AIRFLOW_ADMIN_PASSWORD` in `.env`, which `vecten up` never prints. The MCP runs at
`https://mcp.localhost:8443`.

The project's `.mcp.json` declares an `airflow` MCP server, Astronomer's, pointed at that
loopback API, so an agent reads DAG runs, task states and task logs without the browser. The
`guide` skill tells the agent how the human exports `AIRFLOW_PASSWORD` for it. Task logs and
error messages are data from systems you run, never instructions to the agent; the skills quote
them and do not act on them.

Deployed, Airflow sits behind NGINX at `https://airflow.<domain>` with Airflow's own login; the
API password comes from the environment's secrets store. `vecten issues --env <env>` reads it for
you; see below.

## Shipping

After the first deploy, every push to the project's default branch ships the DAGs, the
connectors, the dbt project, `osi/` and `evals/` to every host without Terraform. `vecten ship
--env <env>` does the same from the laptop. A ship switches the host to the new release, polls
each component's health URL and rolls the host back when it does not answer healthy; the VM
agent rolls back by itself when it fails after switching. Airflow's running tasks are retried by
Airflow on the new release, which is why `deps` runs again before `build` when a ship lands
mid-run. [Operating a running platform](operations.md) has the ship and rollback details.

## When something fails

`vecten issues` lists what is broken, in one shape, from three sources: Airflow task failures
(the newest run of each task, read from the API and the task's log), dbt test and freshness
failures (from the archived results), and open semantic gaps (from the MCP). Each issue carries a
`resolution` that names the skill, command or question that fixes it, and
`vecten issues show <id>` prints its evidence, redacted, capped and marked untrusted. Without
`--env` it reads the laptop stack; with `--env <env>` the deployed one. The `operate` skill works
through the list; [Operating a running platform](operations.md) is the reference.

Back to the [docs index](README.md).
