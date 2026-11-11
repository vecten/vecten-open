## 1. Project dbt packages

- [x] 1.1 Tests: hub and git entries follow the generated `local:` entries; no file and an empty list add nothing; the `deps` step of `dbt-build-local` and the DAG writes them
- [x] 1.2 Tests: an unpinned hub version (range, list, partial, `*`, `~`), a hub entry without a version, a git revision that is not a full commit, a non-https or credentialed or Jinja URL, `local`/`private`/`tarball` and unknown keys, a duplicate, and a collision with `vecten_convention` or an extension's project name each exit 6 and write no `packages.yml`
- [x] 1.3 `project_packages` and the merge in `write_packages_yml`
- [x] 1.4 `vecten dbt deps` merges the file and refuses an unpinned entry before dbt runs
- [x] 1.5 The bundle ships `dbt/project-packages.yml` and not the generated files; the rendered `.gitignore` does not ignore it
- [x] 1.6 Network test: real `dbt deps` installs `dbt_utils` 1.3.0 from the hub and from git at its commit

## 2. `vecten dbt --env`

- [x] 2.1 Tests: every allowed prod command runs with the LOADER key, account, database and warehouse; every other command, a bare group and no command are refused without reading a secret
- [x] 2.2 Tests: other environments run every command, hooks and stored failures included
- [x] 2.3 Tests: prod `test --store-failures` and `--exclude-resource-type` refused; unit tests excluded and an inherited `DBT_STORE_FAILURES` dropped
- [x] 2.4 Tests: prod `test` and `source freshness` refused on hooks, `test` on `store_failures` and `store_failures_as`; compile, show and docs generate run beside them; the check parses with `--vars` and without the key; a failed parse stops the run
- [x] 2.5 Test with a real `dbt parse`: an `on-run-end` grant and a project-wide `+store_failures` are found
- [x] 2.6 Tests: the key never lands in a project file or in vecten's output, and is set under `DBT_ENV_SECRET_`
- [x] 2.7 Tests: Postgres refused before any cloud call; a project without deploy answers, a missing account, a misplaced or empty `--env`, `local`, and a missing loader key each refused with their own message
- [x] 2.8 `split_env`, `dbt_command`, `check_prod_command`, `check_prod_project` and `environment_dbt_env` in `vecten/dbt_env.py`; the `dbt` command routes a leading `--env` there
- [x] 2.9 The Snowflake refusal without `--env` names `--env` and `dbt-build-local`

## 3. Docs, specs and skills

- [x] 3.1 Delta specs for `platform/dbt-modelling` and `shared/deploy`
- [x] 3.2 `docs/warehouse.md`, `docs/cli.md` and the rendered runbook: project packages, what a host needs, `--env`, prod's limits, Postgres, scratch environments
- [x] 3.3 The `model` skill: project packages and `--env`; render the plugin and `docs/agent-guide.md`
- [x] 3.4 Changelog lines under `[Unreleased]`
