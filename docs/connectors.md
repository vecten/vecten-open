# Connectors

For the person or agent who wants data from a system in the warehouse: what a connector is in a Vecten Open project, the four ways one gets in, and how credentials, runs and fixtures work.

A connector is a [dlt](https://dlthub.com) source that the project owns: Python under
`connectors/<name>/connector.py`, wired to a sync DAG in Apache Airflow, a dbt `sources.yml`
entry and an offline test. It lands its objects in the warehouse schema `raw_<name>`; the
[warehouse](warehouse.md) page says what happens from there. `vecten connector add` wires a source into the project; it never writes one.
Four kinds of source exist, and only these:

## Four kinds of source

- A **dlt verified source** by name: `vecten connector add hubspot` copies dlt's HubSpot source into
  `connectors/hubspot/source/` and writes the wrapper, the sync DAG, the dbt `sources.yml` entry and the
  offline test.
- A **dlt generic source** from a scaffold: `--from rest` (`rest_api`), `--from sql` (`sql_database`) or
  `--from filesystem`, rendered with placeholders for the agent or you to fill in.
- An **extension connector** from a package listed under `extensions` in `vecten.toml`, found through the
  `vecten.connectors` entry point group and declared like any other connector.
- A source **written in the project**, which starts from a generic scaffold so it gets the same DAG, entry,
  test and declaration as every other source.

Vecten ships no connectors of its own. Custom connectors for a company are a paid engagement, not a
package in this repository.

## Credentials, runs and fixtures

Credentials never touch `connectors/` or `vecten.toml`: store each with `vecten secret set <NAME>` (value
from the prompt or standard input) and read it in connector code with `secret("<NAME>")`. The stack
passes every secret named in a connector's declaration into Airflow on the next `vecten up`, so run it
again after changing that list. Run a source from the shell with `vecten connector run <source>`
and inspect the tables with `vecten raw schema <source> --json`. A verified
source that needs extra Python packages lists them in the declaration's `dependencies`; the Airflow image
installs them, and on the laptop install them where the CLI runs, for example
`uv tool install vecten --with <package>`.

### Record a sample or supply invented rows

`vecten connector run shop --object orders --record --limit 3` restarts the selected local
object from the beginning, replaces its raw table data with the sample and saves
`connectors/shop/fixtures/orders.jsonl`. The default and maximum row limit is 200. The fixture contains real records (up to 200 per object).
Review it for personal data before you commit it. Once committed, the project uses it in CI.
For incremental sources the next ordinary run uses the cursor saved by the sample load.

For a paginated API, request pages lazily. The runner stops asking for rows at the limit.
An API may already have billed a whole page or a request started in advance. To cap that work
inside the connector, read `runtime().record_limit` from `vecten_platform.sync` and request
at most the remaining number of rows on each call. Ordinary runs receive `None`.
Define `redact_fixture(object_name, row)` in `connector.py` to return a replacement row, for
example `return {**row, "email": "sample@example.test"}`. The hook runs before recording writes
the fixture and loads the sample into the local warehouse. Keep the original values of the resource's primary key and incremental cursor. The dlt
library uses them to identify rows and track incremental progress. Use invented fixtures
when those values also need redaction.

For a source that must never be recorded, set `record = false` in its `[[connectors]]` entry
in `vecten.toml`. An extension author can supply `"record": False` in the
dictionary returned by the package's `vecten.connectors` entry point.
Write one invented JSON object per line in a file named for each resource. For example,
`connectors/shop/fixtures/orders.jsonl` can contain `{"id": 1, "amount": 12}`.
Run `vecten connector check-fixtures shop` to check filenames and rows without calling the
source. With `groups = { daily = ["orders"] }` in the same connector entry,
the only declared object is orders, so the check requires exactly `orders.jsonl`. A group
with an empty list discovers its objects at runtime. In that case the offline check can
detect missing explicitly named files and validate rows, but cannot reject extra filenames.
Run `pytest connectors/shop` or `vecten dbt-build-local` to replay the files.

## Extensions

A connector that several projects share, or that a company keeps private, is an extension: a
Python package listed under `extensions` in `vecten.toml`, installed with `vecten extension add`,
exposing its source through the `vecten.connectors` entry point group. [Installing and writing
extensions](extensions.md) has the author contract and the private-index setup.

## How a connector gets into the project

An agent working with the `connector` skill (vendored into every project under
`skills/connector/SKILL.md`) does it in five steps: agree the name and read the source's API
documentation; `vecten connector add <name>` (a verified source) or `--from rest|sql|filesystem`
(a scaffold), then write the connector code; hand `vecten secret set <NAME>` to the human, who
types the credential at the hidden prompt; `vecten connector run <name> --record` for the first
sync and its fixtures; `vecten raw schema <name> --json` to show what landed and
`vecten answer access.<name> <group>` to record who may read it. `vecten next --json` lists each
of these as a command item until the source is connected.

Back to the [docs index](README.md).
