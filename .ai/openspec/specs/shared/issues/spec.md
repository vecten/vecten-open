# shared/issues Specification

## Purpose
Operations the agent can run: one issue shape shared by every component, read through `vecten issues`, with evidence an agent can act on and a resolution that routes to the skill that owns the fix, so a running platform is as agent-native as the build was.

## Requirements

### Requirement: One issue shape

An issue SHALL carry an `id`, a `source`, the `kind`, `title` and `resolution` of a `next` item, `occurrences`, `first_seen`, `last_seen`, and an `evidence` object of typed, length-capped fields marked `untrusted: true`. The shape SHALL live in `vecten-shared` and SHALL be the same for every source.

#### Scenario: Agent reads an issue

- **WHEN** an agent reads an issue of kind `skill`
- **THEN** it can dispatch the named skill without interpreting the evidence

### Requirement: Ids are derived, not stored

Platform issues SHALL be derived when read, grouped by DAG, task and error class for Airflow and by test name for dbt, with ids built from those keys, so the same failure yields the same id on every read and no issue table exists for them. Semantic gaps SHALL keep their state in `mcp_state` and carry their gap id.

#### Scenario: Same failure twice

- **WHEN** the HubSpot `deals` task fails on two nights with the same error class
- **THEN** `issues` lists one item with `occurrences` 2 and the same id both times

### Requirement: Three sources at v0.1

`vecten issues --env <env>` SHALL read Airflow task failures from the Airflow REST API with task logs from the environment's bucket, dbt test and source freshness failures from the `run_results.json` and `sources.json` the dbt DAG archives per run, and semantic gaps from the admin API, each through an adapter registered internally. On a laptop it SHALL read the local Airflow and the `.vecten/` records, so the loop can be evaluated without a cloud.

#### Scenario: Failed sync, failed test, open gap

- **WHEN** a sync task has failed, a dbt test has failed and a gap is open
- **THEN** `issues --json` lists three items resolved by `connector`, `model` and `semantic`

#### Scenario: Laptop

- **WHEN** `vecten issues` runs with no `--env` in a project where `up` is running
- **THEN** it reads the local Airflow and reports the same shape

### Requirement: The MCP's health is read as issues

In a project with the MCP, `issues` SHALL also read from the admin API a semantic model that is not ready and grants the last reconciliation could not apply, each as an `ask` issue whose evidence holds the MCP's errors. A model still waiting for its build SHALL not be listed.

#### Scenario: Model never became ready

- **WHEN** the MCP's semantic model reports `source_missing` for a dataset
- **THEN** `issues --env prod` lists one `ask` issue naming the error and the dataset, and it leaves the list once the model is ready

### Requirement: Evidence is redacted, capped and untrusted

Each adapter SHALL redact secret values and credential-shaped strings from evidence, keep only the lines around the error, cap every field's length, and mark the evidence object `untrusted: true`. `issues show <id>` SHALL be the only path a skill reads evidence through before acting.

#### Scenario: Token in a log

- **WHEN** a failed task's log contains an API token
- **THEN** the evidence excerpt shows a placeholder in its place

### Requirement: Non-code resolutions

An issue whose fix is not code SHALL resolve to a `command`, such as `secret set <NAME> --env <env>` after a revoked credential, or to an `ask`, such as waiting out an upstream outage.

#### Scenario: Revoked API key

- **WHEN** a sync fails with an authentication error from the source
- **THEN** the issue's kind is `command` with `secret set` as the resolution

### Requirement: Platform issues resolve on green, gaps on the swap

A platform issue SHALL be considered resolved when its source runs green at or after the commit that carries the fix. A gap SHALL resolve as the feedback-loop spec states.

#### Scenario: Fix shipped and green

- **WHEN** a connector fix merges, ships, and the next scheduled run of that DAG succeeds
- **THEN** the issue no longer appears in `issues`

### Requirement: Failures alert through an optional webhook

The sync and dbt DAGs SHALL post each failure to the webhook named by the `ALERT_WEBHOOK_URL` secret when it is set, with the issue id and the line "ask your agent: `vecten issues show <id>`". After deploy, `next` SHALL offer to set it once.

#### Scenario: Alert text

- **WHEN** a sync task fails and the webhook is set
- **THEN** the message names the DAG, the task, the issue id and the `issues show` command

#### Scenario: No webhook

- **WHEN** the secret is not set
- **THEN** no request is made and the failure is still listed by `issues`
