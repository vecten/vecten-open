## ADDED Requirements

### Requirement: promote

`vecten promote --env <env> [--commit <sha>]` SHALL ship to `<env>` a commit that the environment before it in the promotion order shipped: by default the one it shipped last, as the `shared/deploy` spec's "Every ship records its outcome on the commit" defines it, or the given `--commit` when that environment shipped it. It SHALL refuse an environment outside the order or first in it, and SHALL refuse, before dispatching, a commit the environment before it did not ship, naming both. It SHALL dispatch the ship workflow on the default branch with the environment and the commit, follow the run, and exit with its outcome, reporting the commit and the run's URL. `vecten promote --status` SHALL list each environment of the order with the commit it shipped last, and whether the environment before it shipped last a different commit, which a promotion would bring.

#### Scenario: Promote what dev runs

- **WHEN** `[deploy] promotion = ["dev", "prod"]` and `vecten promote --env prod` runs after `dev` shipped `a3f9c2e`
- **THEN** the ship workflow runs for `prod` with `commit: a3f9c2e`, and the command reports `a3f9c2e` and the run's URL once it succeeds

#### Scenario: Roll production back

- **WHEN** `vecten promote --env prod --commit 5c20e81` runs and `dev` shipped `5c20e81` earlier
- **THEN** `prod` ships `5c20e81`

#### Scenario: First environment

- **WHEN** `vecten promote --env dev` runs and `dev` is first in the order
- **THEN** the command exits non-zero, saying that a push to the default branch ships `dev`

#### Scenario: Status

- **WHEN** `vecten promote --status --json` runs after `dev` shipped `b71d004` and `prod` last shipped `a3f9c2e`
- **THEN** the output lists `dev` with `b71d004` and `prod` with `a3f9c2e`, marked as behind `dev`

## MODIFIED Requirements

### Requirement: ship sends content without Terraform

Inside GitHub Actions, `ship --env <env> [--commit <sha>]` SHALL build the content bundle at the given commit, the checked-out commit by default, ship it to every component host, running no Terraform, and refuse a commit as the `shared/deploy` spec's "A later environment ships only a commit the environment before it shipped" states. Outside GitHub Actions it SHALL dispatch the ship workflow for the default branch's head and follow it, and it SHALL refuse an environment after the first in the promotion order, naming `vecten promote --env <env>`. It SHALL refuse, naming `deploy --env <env>`, when the environment has never been deployed. Without `--env`, `ship --check` SHALL report the first environment of the order with its facts.

#### Scenario: Ship on merge

- **WHEN** the ship workflow runs `ship --env prod` after a merge to main in a project with no promotion order
- **THEN** the bundle at that commit is on every host, each component's URL answers, and no cloud resource changed

#### Scenario: Never deployed

- **WHEN** `ship --env prod` runs before any `deploy --env prod`
- **THEN** it exits non-zero naming `deploy --env prod`

#### Scenario: Later environment from the laptop

- **WHEN** `[deploy] promotion = ["dev", "prod"]` and `ship --env prod` runs outside GitHub Actions
- **THEN** it exits non-zero naming `vecten promote --env prod`, and dispatches nothing

#### Scenario: Gate of the ship workflow

- **WHEN** `ship --check` runs without `--env` in a project with `[deploy] promotion = ["dev", "prod"]`
- **THEN** it reports `dev` with whether `dev` is bootstrapped and deployed

### Requirement: deploy from the laptop dispatches CI

Outside GitHub Actions, `deploy --env <env> [--commit <sha>]` SHALL dispatch the repository's apply workflow for the environment, follow it to completion, exit with its outcome, and on success record the deployed commit under `[environments.<env>]` in `vecten.toml`. For the first environment of the promotion order, or an environment outside it, the workflow SHALL apply and ship the default branch's head. For a later environment it SHALL apply and ship the commit `vecten promote` would choose, or the given `--commit`, and refuse, naming the environment before it, when that environment has shipped nothing. It SHALL refuse, naming `bootstrap --env <env>`, when the environment has not been bootstrapped.

#### Scenario: Full deploy

- **WHEN** `deploy --env prod` is run from the laptop after `bootstrap --env prod`
- **THEN** the apply workflow runs on GitHub and cloud resources exist for `prod`
- **AND** each component is on its host with its profile running
- **AND** `airflow.<domain>` and `mcp.<domain>` answer for the components present

#### Scenario: Not bootstrapped

- **WHEN** `deploy --env prod` is run before `bootstrap --env prod`
- **THEN** the command exits non-zero and names `bootstrap --env prod`

#### Scenario: Plan only

- **WHEN** `deploy --env prod --plan-only` is run
- **THEN** a plan is shown and no resources are created or changed

#### Scenario: Unknown environment

- **WHEN** `deploy --env nosuch` is run and `deploy/envs/nosuch.tfvars` does not exist
- **THEN** the command exits non-zero and names the missing environment file

#### Scenario: Deploy a later environment

- **WHEN** `[deploy] promotion = ["dev", "prod"]`, `dev` shipped `a3f9c2e` last, the default branch's head is `b71d004`, and `deploy --env prod` runs
- **THEN** the apply workflow applies `prod`'s roots from `a3f9c2e` and ships `a3f9c2e`, and `vecten.toml` records `deployed_commit = "a3f9c2e"` for `prod`

#### Scenario: Earlier environment never shipped

- **WHEN** `deploy --env prod` runs with `[deploy] promotion = ["dev", "prod"]` and no commit carries a `success` status for `dev`
- **THEN** the command exits non-zero naming `dev`, and dispatches nothing

### Requirement: github init and github push

`github init` SHALL create the repository named at intake under the human's GitHub login through the `gh` CLI, and push the project to it. `github push` SHALL commit the project's changes and push them. Both SHALL exit non-zero naming `gh auth login` when `gh` is missing or logged out.

When the first environment of the promotion order has been deployed and the push moves `main`, `github push` SHALL wait for the `ship.yml` run that push starts and SHALL exit non-zero naming the run's URL when it does not succeed, or naming the workflow when no run appears within two minutes; the push itself stands either way. `--no-wait`, a branch other than `main`, or a push that moves nothing SHALL return once pushed.

#### Scenario: Push after the deploy

- **WHEN** `github push` runs on a deployed project with a changed `vecten.toml`
- **THEN** it returns after the ship run for the pushed commit succeeds, reporting the run's URL

#### Scenario: Push with a promotion order

- **WHEN** `github push` runs with `[deploy] promotion = ["dev", "prod"]` and `dev` deployed
- **THEN** it returns after the ship run ships the pushed commit to `dev`, and names `vecten promote --env prod` as the way on to `prod`

#### Scenario: The ship fails

- **WHEN** the ship run for the pushed commit fails
- **THEN** `github push` exits non-zero naming the run's URL, and the commit is on GitHub

#### Scenario: First push

- **WHEN** `github init` runs right after `init`
- **THEN** the repository exists on GitHub and holds the rendered project

#### Scenario: Not logged in

- **WHEN** `github init` runs and `gh` is not logged in
- **THEN** the command exits non-zero and names `gh auth login`
