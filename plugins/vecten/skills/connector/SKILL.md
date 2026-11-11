---
name: connector
description: >-
  Connect a data source to a Vecten Open project end to end with the `vecten` CLI: choose a dlt
  verified source or a rest, sql or filesystem scaffold, have the human store the credential with
  `vecten secret set`, write the connector code from the API documentation, run and record the
  first sync, show the raw tables and record who may read them. Use this skill whenever
  `vecten next --json` lists a `skill` item resolved by `connector` (titles such as "Connect
  hubspot"), and whenever the human names a system to load data from (a CRM, a REST API, a
  database, files in a bucket), even if they never say "connector" or "dlt".
---

# Connecting a source

A source is connected when its code lives in `connectors/<name>/connector.py`, a local sync has
landed its objects in the warehouse schema `raw_<name>`, fixtures of that sync are recorded, the
human has said who may read the raw data, and `vecten next --json` lists nothing more for it.
This skill walks there in order. The human talks; you run the `vecten` commands in a shell.

## When this skill runs

- `vecten next --json` lists an item with `"kind": "skill"` and `"resolution": "connector"`.
  Its title is `Connect <source>`; the word after "Connect" is the source the human asked for
  during the intake.
- The human names a source to load, in any words ("pull our deals from Pipedrive", "load the
  billing database").

`next` also prints command items for a connector that is already declared: `Add the <name>
connector files`, `Run <name> locally`, `Record fixtures for <name>`. Those are plain commands;
this skill explains what to do when one of them fails.

## Ground rules

- Read and change the project only through `vecten` commands run with `--json`. Each prints one
  JSON document: `"ok": true` with its data, or `"ok": false` with `error`, `code` and `code_name`.
  The exit code says how to react:

  | Exit | Name | What to do |
  |---|---|---|
  | 0 | OK | Continue. |
  | 1 | INTERNAL | A bug in vecten. Show the human the `error` and stop. |
  | 2 | USAGE | The command line was wrong; the `error` says how. Fix it and rerun. From `vecten secret set`, "the secret value is empty" means your shell has no terminal for the hidden prompt: do not retry, ask the human to run it in theirs (step 3). |
  | 3 | PREREQUISITE | Something is missing first; the `error` names the command that provides it. |
  | 4 | REFUSED | The project's state forbids it; the `error` says why. Do not force it. |
  | 5 | TOOL_FAILED | dlt or the source failed; the document carries the per-object outcomes. |
  | 6 | CONFIG | A project file is invalid; the `error` names it. |

- The files that are yours: `connectors/<name>/connector.py` and any helper module you add
  beside it. The CLI owns `vecten.toml`, `.env`, `dbt/models/staging/sources.yml`, `.vecten/`
  and `osi/datasets.json`; never edit them, because the CLI writes them and reads them back as
  its own record of the project.
- A credential reaches the project one way only: the human types it at the hidden prompt of
  `vecten secret set <NAME>`. See step 3 for why and how.

## 1. Agree the name and read the documentation

The source name becomes the schema `raw_<name>`, the DAG ids and the secret names, so pick it
once: lowercase letters, digits and underscores, starting with a letter, at most 63 characters.
Use the name from the `Connect <source>` item; otherwise agree a short one with the human
(`hubspot`, `billing_db`).

Ask the human for:

- a link to the API documentation, or the file that holds it, and read it before writing code;
- which objects matter (for a CRM: companies and deals, not every endpoint). Every extra object
  spends the API's rate limit and lands a table nobody models;
- for a database, the schema and the tables; for files, the bucket and the file layout.

From the documentation note: the base URL, the authentication scheme, the pagination style,
the filter for "changed since" (incremental loading), the documented rate limit, the record key
of each object, and where the records sit in a response (`data`, `results`, a bare list).

## 2. Choose the dlt path and add the connector

Try the source's own name first:

```bash
vecten connector add <name> --json
```

It wires, in this order, a connector an installed extension provides, then a dlt verified
source. A verified source is the better path whenever it exists: its pagination, schema and
incremental state are maintained by the dlt project, and the code you would otherwise write is
already there under `connectors/<name>/source/`.

Exit 4 with "neither a dlt verified source nor a connector of an installed extension" means
there is none; start from a generic scaffold instead, chosen by what the source is:

- `vecten connector add <name> --from rest --json` for an HTTP API that returns JSON;
- `vecten connector add <name> --from sql --json` for a database reachable by an SQLAlchemy URL;
- `vecten connector add <name> --from filesystem --json` for CSV files in a bucket or a folder.

A generic scaffold is code you write and the project owns, so it costs more care than a verified
source; that is why it is the fallback.

Read the JSON the command prints:

- `files`: what it wrote under `connectors/<name>/`, including `test_<name>.py`, which replays
  the fixtures offline, and an empty `fixtures/` directory. The replay loads the fixtures
  straight into the raw tables and never runs `connector.py`, so it does not catch a code
  change that breaks a live sync: `vecten connector run <name> --json` is the live proof;
- `dags`: the sync DAG, `orchestration/dags/sync_<name>_default.py`;
- `secrets`: the secret names the code reads, used in step 3;
- `dependencies`: Python packages a verified source needs. The Airflow image installs them; for
  `vecten connector run` on this machine, ask the human to install them where vecten runs, for
  example `uv tool install vecten --with <package>`.

Exit 4 with "already exists" means the directory is there: edit it rather than adding again.
`vecten.toml` now declares the connector, and the `Connect <name>` item is gone from `next`.

## 3. Credentials

Use exactly the names in `secrets`. A generic scaffold declares one: `<NAME>_API_KEY` for rest
(whatever the API calls it: token, key, password), `<NAME>_DATABASE_URL` for sql and
`<NAME>_BUCKET_URL` for filesystem. These names are declared in `vecten.toml`, and the local
Airflow receives only declared names; a name you invent works from the laptop and fails in
Airflow.

Tell the human which secret to set and what value belongs in it ("your Demo API token, from
Settings, API in Demo"), then have them type it:

```bash
vecten secret set DEMO_API_KEY
```

The command prompts with hidden input and stores the value in `.env`. When your shell is the
human's own terminal, run it and let them type. A shell without a terminal gives the prompt no
keyboard: the command reads an empty standard input and exits 2 with "the secret value is
empty". Then do not retry; ask the human to run the command in their own terminal in the project
folder. Either way, confirm by name only:

```bash
vecten secret list --json
```

`names` lists the secret; values are never printed.

Why this is the only way: anything you see lands in the conversation transcript, anything on a
command line lands in shell history and process lists, and anything in a file can be committed.
So never ask for the value in chat, never read it from the clipboard, a file or an environment
variable, never pipe it into `vecten secret set` and never pass it as an argument (the command
refuses that with exit 2), and never write it into `connector.py`. If the human pastes a
credential into the conversation anyway, do not repeat or use it: tell them it is now in the
transcript, ask them to rotate it at the provider, and have them set the new value at the prompt.

When the API needs a second credential (a client id beside a client secret), the scaffold
declares only the first. Ask the human to add the second name to that connector's `secrets`
list in `vecten.toml`, then set it the same way.

## 4. Write the connector code

Skip this step for a verified or extension source, except to keep only the objects the human
chose: return `_source(...).with_resources("<object>", "<object>")` from `source()`.

For a generic scaffold, open `connectors/<name>/connector.py`. It defines `source()`, which
returns a dlt source; replace every `TODO` from the documentation. For `--from rest` the
scaffold is a dlt `rest_api_source` config:

- `client.base_url`: the API root, ending in `/`; resource paths then have no leading `/`.
- `client.auth`: keep the credential behind `secret("<NAME>")` with a name from step 3.
  Bearer: `{"type": "bearer", "token": secret("DEMO_API_KEY")}`. A key in a header:
  `{"type": "api_key", "name": "X-Api-Key", "api_key": secret("DEMO_API_KEY"), "location": "header"}`.
- `client.paginator`: the documented style. A next URL in the body: `json_link` with
  `next_url_path`; a cursor in the body: `{"type": "cursor", "cursor_path": "<path>",
  "cursor_param": "<param>"}`; page numbers: `page_number`; offsets: `offset`; a `Link`
  header: `header_link`.
- `resources`: one entry per object the human chose. Name each after its entity in the singular
  (`company` for `/companies`): the name becomes the raw table, the fixture file and the object
  that `--object` selects. Set `endpoint.path`, `endpoint.data_selector` when records are nested
  in the response, and `primary_key` when it is not `id`.
- Incremental loading: put the API's "changed since" filter under `endpoint.params` as
  `{"type": "incremental", "cursor_path": "<updated-at field>", "initial_value": "<ISO time>"}`.
  With `write_disposition: merge` and a primary key, a daily sync fetches only what changed.
  Pick an `initial_value` early enough to cover the history the human wants.
- A page size parameter, when the API has one, under `endpoint.params`.

For `--from sql`, set the schema and `table_names`; the connection URL stays in the secret. For
`--from filesystem`, set `file_glob` and yield one resource per file family.

Put no warehouse host, warehouse name or credential in the code: the destination comes from the
CLI. Once you change the scaffold, the next run records the connector as `custom` in
`vecten.toml`; that is the CLI marking the code as yours, not an error.

## 5. Run and record the first sync

The sync needs the local warehouse. Exit 3 with "the warehouse is not reachable" means the stack
is down: run `vecten up --json`, then retry.

Before the first run, tell the human what a fixture is: `--record` writes up to 200 real
records per object to `connectors/<name>/fixtures/<object>.jsonl`. Those files are committed to
the project and replayed by the connector test and by `vecten dbt-build-local`, so they are real
data in git. Ask which objects hold personal data they do not want there.

After any later change to `connector.py`, run `vecten connector run <name> --json` again before
calling the change done. Neither the connector test nor `vecten dbt-build-local` runs the code,
so a green CI says nothing about it.

Every object the connector keeps needs a fixture. `vecten dbt-build-local` replays fixtures
only, so an object synced without one leaves a table that `sources.yml` declares, `next` asks to
model, and the local build cannot build. To leave personal data out, drop that object from the
connector instead: remove its entry from `resources` in `connector.py` (for a verified or
extension source, leave it out of `.with_resources(...)`), so it is neither synced nor declared.
Then record everything the connector keeps:

```bash
vecten connector run <name> --record --json
```

Ask before the first run, because a table a sync has landed stays declared in
`dbt/models/staging/sources.yml`, a file the CLI writes and you never edit. When an object the
human now wants out was already synced or recorded, drop it from `connector.py`, delete its
`connectors/<name>/fixtures/<object>.jsonl`, run `vecten connector run <name> --record --json`
again, and tell the human that its table entry stays in `sources.yml` until they remove it by
hand.

Read the result. `objects` maps each object to `{"ok", "rows", "error"}`, `tables` lists
every raw table of the source that `dbt/models/staging/sources.yml` now declares, and `recorded`
says whether fixtures were written. On exit 5 the same document carries `objects`: fix the
failed object and rerun only it with `--object <object> --record`; the run record keeps the
other objects' outcomes.

| What you see | What it means | What to do |
|---|---|---|
| Exit 3, "secret X is not set" | The human has not set it | Step 3 for that name. |
| `401` or `403` in an object's `error` | The value is wrong, expired or lacks a scope | Ask the human to set it again with `vecten secret set <NAME>` and to check its permissions at the provider; change no code first. |
| `429` in an `error` | The rate limit was hit | Request the largest page size the documentation allows, so the sync needs fewer requests; wait out the `Retry-After` delay before rerunning, stay within the documented limit, and run objects one at a time with `--object`. |
| `404` in an `error` | The path or base URL is wrong | Check `base_url` ends in `/` and the path against the documentation. |
| Exit 5, "source() failed" | `connector.py` raised before any object ran | Read the exception and fix the code. |
| Exit 2, "has no object" | The `--object` name is not a resource | Use one of the names the error lists. |
| An object with `"rows": 0` | It yielded nothing, so a recording keeps no fixture for it | Confirm the scope with the human: is that object empty in their account, does the credential cover it, is `initial_value` too recent, is `data_selector` right. Never invent records. |

## 6. Show the raw tables

```bash
vecten raw schema <name> --json
```

It prints `{"source": "<name>", "schema": "raw_<name>", "tables": [{"name": ..., "columns":
[{"name": ..., "type": ...}]}]}`. Show the human each table with its columns and types, and
point out what dlt did: nested lists became child tables named `<table>__<field>`, the
`_dlt_load_id` and `_dlt_id` columns are dlt's bookkeeping, and dates often land as text, which
the `model` skill casts. Ask whether these are the objects and fields they expected; a missing
field usually means a wrong `data_selector` or a resource that was not added. An empty `tables`
list means no sync has landed yet: back to step 5.

## 7. Who may read the raw data

`vecten next --json` now asks `Who may read the <name> raw data?`. Put the question to the
human word for word, offer the groups listed in the item's `allowed`, and record the answer,
comma-separated for several groups:

```bash
vecten answer access.<name> <groups> --json
```

## 8. The scheduled sync

Tell the human how the source runs from here. `vecten connector add` wrote a daily sync DAG
with catch-up off. In the local Airflow that `vecten up` starts, the DAG appears paused, because
new DAGs start paused; once unpaused, its first scheduled run is at the next midnight UTC, not
straight away. To load now, `vecten connector run <name>` syncs from this machine. In a deployed
environment the DAG starts unpaused and first runs at the midnight UTC after it ships, unless
that environment's `deploy/envs/<env>.tfvars` sets `dags_paused_at_creation = true`; then it
starts paused and runs only once someone unpauses it in that environment's Airflow.

## 9. Finish

Run `vecten next --json`. The source is done when no item names it: no `Connect <name>`,
`Add the <name> connector files`, `Run <name> locally`, `Record fixtures for <name>` or `Who may
read the <name> raw data?`. The next items are usually `Model <table> from raw_<name>.<table>`,
which belong to the `model` skill; go back to the loop that dispatched you.
