## 1. Asset only after every object synced

- [x] 1.1 Test: a sync DAG's `publish` runs after `check` and only on its success; a `dag.test()` run with a failed object emits no asset event
- [x] 1.2 Order the sync DAG `sync >> check(all_done) >> publish(all_success)`
- [x] 1.3 The connector end-to-end test expects `publish` to end `upstream_failed` after a failed object

## 2. Sync DAG options

- [x] 2.1 `stages`: one mapped task per stage, `check` over every stage, objects after a failed stage reported as not run
- [x] 2.2 `vecten_shared.issues.is_sync_task`, used by the alert callback and `vecten issues`
- [x] 2.3 `params` as Airflow params, the declared ones passed to `source()`
- [x] 2.4 `pool` on the object tasks only
- [x] 2.5 `guard` as a short-circuit task before `discover`
- [x] 2.6 `timezone` for a cron string schedule, refused beside a timetable
- [x] 2.7 `paused_at_creation` as `is_paused_upon_creation`
- [x] 2.8 Structure tests per option and `dag.test()` runs for guard, params and stages

## 3. Extension groups

- [x] 3.1 Parse and validate `groups` in an extension connector's declaration
- [x] 3.2 Record the groups' objects in `vecten.toml` and render each group's DAG with its schedule and timezone, on add and on a re-render
- [x] 3.3 The stub extension declares a group with a schedule and timezone

## 4. dbt DAG options

- [x] 4.1 `build_args` with `select`, `exclude` and `full_refresh`; `BUILD_STEP` is its default
- [x] 4.2 Run parameters on the dbt DAG, passed to the build step
- [x] 4.3 `trigger` of `all` or `any`; `dbt_assets(config, groups)`
- [x] 4.4 A `publish` task emitting the `dbt_build` asset with the selection as its extra
- [x] 4.5 `mark_built` skipped on a narrowed build
- [x] 4.6 `paused_at_creation`
- [x] 4.7 Structure tests and `dag.test()` runs for a full, a narrowed and a failed build

## 5. Docs

- [x] 5.1 `docs/orchestration.md`: the options of both DAGs and the asset rule
- [x] 5.2 `docs/extensions.md`: `groups` in the `vecten.connectors` declaration
- [x] 5.3 The `connector` skill's step on the scheduled sync; render the plugin and the agent guide
- [x] 5.4 Changelog
