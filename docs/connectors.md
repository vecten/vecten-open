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

## Test connector parsing offline

An output fixture such as `connectors/demo/fixtures/people.jsonl` holds the rows the connector already
transformed. To exercise the transformation in CI, give the connector a reader that Vecten
can replace with saved inputs. For example, in `connectors/demo/connector.py`:

```python
import dlt
from vecten_platform.sync.input_fixtures import live_inputs


def fetch_people():
    # Replace this example with the live read, including credentials and pagination.
    yield {"userId": 7}


def source(*, inputs=live_inputs):
    @dlt.resource(name="people")
    def people():
        for row in inputs(fetch_people):
            yield {"user_id": row["userId"]}

    return dlt.source(lambda: [people], name="demo")()
```

The example selects `people` with `--object people`. Put pagination inside `fetch_people`
and call `inputs(fetch_people)` once. The callback can yield individual records or lists of
records, one page at a time. Keep the parsing you want to test after that call. During a
regular sync, `live_inputs` calls the callback. During replay, Vecten supplies the saved values.

Each selected object can replay one upstream read. For example, a `deals` resource may
first read a parent `companies` resource to get company IDs, then fetch deals for each ID.
If both resources call `inputs(...)`, record the transformed deal rows with
`vecten connector run demo --object deals --record` instead.

1. Run `vecten connector run demo --object people --record --record-inputs` against a
   source whose original payloads are safe to save. A sample holds up to 200 records. A list page contributes its row count, and Vecten
   trims the final page to fit. An empty page uses one slot. Vecten writes `connectors/demo/fixtures/inputs/people.json`. The input file
   contains the original fields, including fields your transformation drops. Inspect it
   before committing it. Vecten refuses input recording when the connector defines
   `redact_fixture`, because that hook operates on transformed output.
2. From the project root, run `pytest connectors/demo/test_demo.py` (the test generated
   by `vecten connector add`) or `vecten dbt-build-local`. Generated CI installs the connector
   dependencies declared in `vecten.toml` for pytest and the local dbt build. Both pass the
   inputs through the current connector with Python network access blocked. Changed row
   values, counts or order fail the check. In the example, reading the wrong key fails even
   though the saved output still contains a valid `user_id`.
3. After an intentional change to the output, re-record with both flags. Review the diff
   in `connectors/demo/fixtures/` before accepting the new expectation. Projects with only output fixtures keep loading
   those rows directly into the local warehouse.

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
