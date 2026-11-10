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
again after changing that list. Run a source from the shell with `vecten connector run <source>`, record
its fixtures with `--record` so the rendered test replays offline (it reloads the object from the start,
so the fixture is complete), and inspect what landed with `vecten raw schema <source> --json`. Fixtures
are yours; the product ships none. They hold real records from the source (up to 200 per object) and are
committed to the project, so check them for personal data before the repository is shared. A verified
source that needs extra Python packages lists them in the declaration's `dependencies`; the Airflow image
installs them, and on the laptop install them where the CLI runs, for example
`uv tool install vecten --with <package>`.

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
