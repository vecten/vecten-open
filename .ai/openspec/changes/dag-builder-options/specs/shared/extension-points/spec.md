## ADDED Requirements

### Requirement: Extension connectors declare their object groups

A connector an extension declares through `vecten.connectors` MAY declare `groups`, mapping each group name to its objects, a cron `schedule` (default `@daily`) and an IANA `timezone` (default UTC), or to a list of objects alone. `connector add` SHALL declare the connector in `vecten.toml` with those groups' objects and SHALL render one sync DAG per group on its own schedule and timezone; rendering a declared connector again SHALL take the schedules from the installed extension. A connector that declares no groups SHALL get the one `default` group on `@daily`, as before. A declaration whose groups cannot render a DAG (an invalid group name, objects that are not a list of strings, a schedule that is not a single-line string, an unknown timezone or an unknown key) SHALL be refused with an error naming the extension and the connector, before anything is written.

#### Scenario: Groups on their own schedules

- **WHEN** an extension declares connector `attio` with a `daily` group on `0 6 * * *` and an `hourly` group on `@hourly` in `Europe/Warsaw`, and `connector add attio` runs
- **THEN** `orchestration/dags/sync_attio_daily.py` and `sync_attio_hourly.py` exist with those schedules and that timezone
- **AND** `vecten.toml` declares `attio` with both groups and their objects

#### Scenario: No groups declared

- **WHEN** an extension declares a connector without `groups` and `connector add` runs
- **THEN** one `default` group is declared and its DAG runs `@daily` in UTC

#### Scenario: A group that cannot render

- **WHEN** an extension declares a group whose timezone is `Mars/Olympus`
- **THEN** `connector add` exits with the configuration error code and the project is unchanged
