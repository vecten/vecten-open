# Orchestration

For the person who wants to know what runs when, and where to look when it does not: what Apache Airflow does in a Vecten Open project, the two kinds of DAG, reading Airflow on the laptop, shipping, and failures.

## What Apache Airflow does here

A project with the `platform` component runs Apache Airflow 3 with the LocalExecutor, pinned by
the release and changed only by `vecten update`. It does two jobs: run each connector's sync on
its schedule, and run the dbt build once the syncs it depends on have landed. Everything Airflow
runs comes from the project repository: the DAGs under `orchestration/dags/`, the connectors
under `connectors/`, the dbt project under `dbt/`. Nothing is written in the Airflow UI.

## The sync DAGs

`vecten connector add <name>` renders one DAG per source, `sync_<name>`, from a template the
project owns. Each DAG:

- runs `@daily` in UTC, and runs its first sync as soon as it is unpaused rather than at the next
  midnight; it never backfills past runs (`catchup=False`) and runs one instance at a time;
- runs one task per object with an independent checkpoint per object, so a failure in one object
  neither blocks nor resets the others, and retries the failed object twice;
- creates the raw schema `raw_<name>` and dlt's staging schema before the object tasks start, so
  objects loading in parallel never race on creating them;
- emits an Airflow asset per object group, which is what the dbt DAG schedules on;
- posts a failure to the alert webhook when `vecten answer alert_webhook.<env>` has set one.

The same sync runs from a shell without Airflow: `vecten connector run <name>` syncs every
object (or `--object <x>` one of them), reports rows per object, exits non-zero when any object
fails, and writes a run record under `.vecten/` that `vecten next` reads. `--record` saves the
objects as fixtures for the offline test and the local dbt build; see [connectors](connectors.md).

## The dbt DAG

`dbt_build` is scheduled on the sync DAGs' assets, so it runs once every group it depends on has
updated. Its tasks run in this order: `deps` installs the `vecten_convention` package into the
container; `build` runs `dbt build`; `convention` runs the convention tests; `mark_built`
records the commit the build came from, which `vecten next` and the MCP's `model_health` read.
`freshness` runs `dbt source freshness` beside them. Each task retries once. The run's
`run_results.json` and `sources.json` are archived, and that is what `vecten issues` reads for
the `dbt` source. The [warehouse](warehouse.md) page says what the build produces.

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
