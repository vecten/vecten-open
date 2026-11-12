## Why

A project with two environments has no supported "dev first, then prod" path (issue #37).

- The rendered `ship.yml` runs on every push to `main` and ships `prod`. Other environments ship only when someone dispatches them.
- Every GitHub environment is restricted to the default branch, and `deploy` refuses a commit that is not on it, so a long-lived `dev` branch cannot deploy at all.
- `vecten ship --env <env>` from the laptop dispatches the default branch's head, so nothing ships an older commit, such as the one `dev` has been running.
- `vecten next` is hard-coded to `prod` (`ENV = "prod"` in `vecten.next.assemble`), and the `operate` skill reads `vecten issues --env prod` only.

A merge therefore reaches production before it has run anywhere else. Today's workaround is to edit `ship.yml` by hand.

## What Changes

- `[deploy] promotion` in `vecten.toml` declares an order of environments, for example `["dev", "prod"]`. Without it the order is `["prod"]`, which is today's behaviour.
- A push to the default branch ships the first environment of the order. The rendered `ship.yml` reads the order when it runs, so changing the order needs no re-render.
- Every ship from CI records its outcome on the commit as a GitHub commit status, `vecten/ship/<env>`.
- `vecten promote --env <env>` ships a later environment the newest commit that the environment before it shipped, or a given `--commit` that it shipped. CI refuses a commit that is not on the default branch or that the environment before it did not ship. `ship --env <env>` from the laptop refuses a later environment and names `promote`.
- `deploy --env <env>` of a later environment applies and ships the same promoted commit, so infrastructure changes also run first in the environment before it.
- A promoted run builds everything from the promoted commit, including the content, `vecten.toml`, the tfvars, the Terraform roots and the CLI version that commit pins. It reads the facts the CLI records about environments from the default branch.
- `next` walks the environments in order, each through its bootstrap, deploy and first-user items.
- The `operate` skill reads issues for every environment in the order, and asks the human before it promotes a fix.
- The branch restriction stays: every environment deploys from the default branch only. Promotion moves commits along the order; it adds no branch per environment.

Nothing changes for a project that declares no order.

## Impact

- Specs: `shared/deploy`, `cli/cli-lifecycle`, `cli/agent-guide`, `shared/issues`.
- Code: `vecten_shared.config` (`[deploy] promotion`), `vecten.deploy.run` (`ship_env`, `deploy_env`), `vecten.deploy.github_env` (dispatch with a commit, commit statuses), a new `vecten promote` command, `vecten.next.assemble` and `vecten.next.deploy`, the `operate` and `guide` skills.
- Workflows: the rendered `ship.yml`, `deploy-platform.yml` and `terraform-apply.yml`.
- Docs: `docs/operations.md`, the rendered runbook, the agent guide.
- Independent of #34. With #34 an environment's Snowflake names come from the promoted commit's `vecten.toml`, which is the same file everywhere.
