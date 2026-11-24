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

Airflow runs a separate sync task for each object (a named dlt resource in the source).
For example, if a metered API reports that the account has exhausted its credits,
raise `TerminalConnectorError` from `vecten_platform.sync` inside that resource.
Its task fails on that attempt and sends its configured failure alert. If you raise it
from an HTTP error, the alert and `vecten issues` use the terminal exception’s class and
message, so you can distinguish exhausted credits from an HTTP error that Airflow retried.
Raise ordinary exceptions for temporary failures so Airflow can retry.

For a source billed per page or saved search, have the resource read one unit of work each
time it is called. Read its starting position with `unit_cursor(default)`. Yield the rows,
then call `finish_unit(next_cursor, more=...)` and return. The next call starts only after
dlt loads both the rows and their cursor into the warehouse. In this example, the upstream
supplies the next offset even on the last page, so tomorrow's run can resume there.

```python
from vecten_platform.sync import finish_unit, unit_cursor

@dlt.resource(write_disposition="append")
def purchases():
    page = api.read_purchases(offset=unit_cursor(0))
    yield from page.rows
    finish_unit(page.next_offset, more=page.has_more)
```

Use an append or merge resource. The cursor is a string, integer, or null. Requesting
another unit requires a changed cursor. A failure can repeat the unit in progress, so size
units to match the upstream's billing granularity. A recording samples the first unit
within the usual row limit. Airflow publishes the group's asset after its object tasks finish.

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

Each selected object can replay one call to `inputs(...)`, including all pages from that call. For example, a `deals` resource may
first read a parent `companies` resource to get company IDs, then fetch deals for each ID.
If both resources call `inputs(...)`, record the transformed deal rows with
`vecten connector run demo --object deals --record`. The generated connector test checks
that the saved rows load. Keep using `--record` alone to refresh deals. The input replay
steps below apply to a single reader.

1. Run `vecten connector run demo --object people --record --record-inputs` against a
   source whose original payloads are safe to save. The output stops at `--limit` rows (200
   by default), and the input sample keeps what the connector read to produce them, up to
   200 records. A list page contributes its row count, and Vecten trims the final page to
   fit. The input file records the limit, and replay stops the output at the same row as
   the recording did. An empty page uses one slot. Vecten writes `connectors/demo/fixtures/inputs/people.json`. The input file
   contains the original fields, including fields your transformation drops. Inspect it
   before committing it. Vecten refuses input recording when the connector defines
   `redact_fixture`, because that hook operates on transformed output.
2. From the project root, run `pytest connectors/demo/test_demo.py` (the test generated
   by `vecten connector add`) or `vecten dbt-build-local`. Generated CI installs the connector
   dependencies declared in `vecten.toml` for connectors with input fixtures. It resolves
   registered in-project packages from their checked-in paths. If replay needs a registered
   Git package, directly or through another dependency, or needs the project's private index,
   run input replay locally in the project environment. Generated CI has no credentials for
   those sources. Output-only fixtures work without installing connector dependencies.
   For input fixtures, both pytest and the local dbt build pass the inputs through the current connector with Python network access blocked. Changed row
   values, counts or order fail the check. Derive timestamps and IDs from the saved input
   if the transformation needs to reproduce them during replay. In the example, reading the wrong key fails even
   though the saved output still contains a valid `user_id`.
3. After an intentional change to the output, re-record with both flags. Review the diff
   in `connectors/demo/fixtures/` before accepting the new expectation. Projects with only output fixtures keep loading
   those rows directly into the local warehouse.

If a refresh produces no output rows, an existing output fixture and its companion files stay
together. When there is no previous output fixture, input recording can still capture a case
where the transformation filters out every input. Replay then expects zero output rows.

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
