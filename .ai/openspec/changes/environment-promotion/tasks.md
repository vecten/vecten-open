## 1. The order

- [x] 1.1 `[deploy] promotion` in `vecten_shared.config`: at least two names, no repeats, `prod` last, each with a tfvars file; absent means `["prod"]`; one helper returns the order and the environment before a given one
- [x] 1.2 The CLI refuses to write the key while `[project] scaffold_version` predates this release, naming `vecten update`
- [x] 1.3 Tests: validation, the default, the helper, the refusal
- [x] 1.4 `vecten answer deploy.promotion <envs>|none` records or removes the order, refusing what 1.1 and 1.2 refuse, with `vecten.toml` unchanged

## 2. Commit statuses and the shipped commit

- [x] 2.1 `vecten.deploy.github_env`: set a commit status, read one commit's status for a context, and find the commit among the default branch's last 200 whose `vecten/ship/<env>` `success` was set last
- [x] 2.2 `ship_env` inside Actions sets `pending`, then `success` once every host answered or `failure` otherwise, on the commit it ships; a commit that already carries `success` keeps it until the ship succeeds
- [x] 2.3 Tests against a mocked `gh`: each state, the rollback case, the 200-commit bound and its message

## 3. Shipping a given commit

- [x] 3.1 `ship --env <env> --commit <sha>` inside Actions: ancestry check against the remote default branch; for a later environment, the previous environment's status check; bundle and tfvars from the commit; `[environments.<env>]` facts from the working tree's head
- [x] 3.2 `ship --env <later env>` outside Actions refuses, naming `promote`; `ship --check` without `--env` reports the first environment and its facts
- [x] 3.3 `deploy --env <later env> [--commit]`: resolve the commit, dispatch the apply workflow with it, record it as `deployed_commit`; Terraform roots and tfvars from the commit
- [x] 3.4 `dispatch_and_follow` passes `commit`, and the result reports the commit that was shipped rather than the run's `head_sha`
- [x] 3.5 Tests: every refusal, the facts read from the head while content comes from the commit, the dispatch arguments

## 4. `vecten promote`

- [x] 4.1 `promote --env <env> [--commit <sha>]` and `promote --status`, with `--json`, on the documented exit codes
- [x] 4.2 Tests: default resolution, `--commit` rollback, first environment refused, environment outside the order refused, status output

## 5. Workflows

- [x] 5.1 `ship.yml`: the gate reports the first environment; the ship job passes `environment` and `commit: ${{ github.sha }}` and grants `statuses: write`
- [x] 5.2 `deploy-platform.yml` and `terraform-apply.yml`: a `commit` input (default: the run's own commit), checkout of the default branch with full history, `vecten` installed at the version the commit's `scaffold_version` names, `statuses: write` and `statuses: read`
- [x] 5.3 Render tests for both clouds: the inputs, the permissions, the version step, and that no workflow names `prod` as a literal outside its defaults
- [x] 5.4 The rendered workflows parse as YAML and every `needs.*.outputs` reference names an output the job declares

## 6. `next`

- [x] 6.1 `vecten.next.assemble` walks the order instead of `ENV = "prod"`; prerequisites, gate and post-deploy items name the walked environment; the gate of a later environment says it ships what the environment before it shipped
- [x] 6.2 Key renewal and handover items for every environment with facts
- [x] 6.3 Tests: no order gives today's items; `dev` first; `prod` once `dev` is clear; a `dev` key renewal while walking `prod`

## 7. Skills and docs

- [x] 7.1 `operate` skill: the order from `promote --status`, issues per environment, the promotion `ask`; re-render the plugin and `docs/agent-guide.md` with `uv run vecten dev render-plugin --repo .`
- [x] 7.2 `guide` skill: the promotion order in the deploy walk-through
- [x] 7.3 `docs/operations.md`: the order, `promote`, statuses, rollback by `--commit`, required reviewers as an option where the plan offers them
- [x] 7.4 Runbook template: adopting an order, and what a promoted run reads from where
- [x] 7.5 `docs/GLOSSARY.md`: "Promotion order" and "Promote"
- [x] 7.6 `CHANGELOG.md` under `[Unreleased]`

Note: `check_tfvars` keeps every environment's `snowflake_account` equal to `[warehouse] account`, because the hosts, the MCP and dbt read the account from `vecten.toml`. A Snowflake warehouse the platform builds takes an order once snowflake-shared-account tasks 3.3 and 3.4 land (the per-environment account in `check_tfvars` and on every host's runtime); that change relaxes the check together with its plumbing, and until then the guide (7.2) and the runbook (7.4) set up no order on such a project.

## 8. Proven only on a real repository and two deployed environments

- [ ] 8.1 A merge ships `dev` only and sets `vecten/ship/dev`
- [ ] 8.2 `vecten promote --env prod` ships the same commit to `prod`, with the CLI version that commit pins, while `main` has moved on
- [ ] 8.3 A dispatch naming a feature-branch commit, and one naming a commit `dev` never shipped, both fail before any host changes
- [ ] 8.4 With required reviewers on `prod` (a public repository or GitHub Enterprise), the dispatched promotion waits for approval and then ships
- [ ] 8.5 On a private repository without branch policies, the promotion still runs and its checks still refuse
