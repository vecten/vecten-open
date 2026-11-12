## ADDED Requirements

### Requirement: Promotion order

`[deploy] promotion` in `vecten.toml` SHALL declare the order in which code reaches environments, as a list of at least two environment names, without repeats, each with its own `deploy/envs/<env>.tfvars`, and with `prod` as the last name. A chain of any length SHALL be accepted, such as `["dev", "prod"]` or `["dev", "staging", "prod"]`; each environment after the first promotes from the one before it. Without the key the order SHALL be `["prod"]`. A push to the default branch ships the first environment of the order; each later environment ships only by promotion; an environment outside the order ships only when dispatched, as it does today. The CLI SHALL refuse to write the key while the project's workflows were rendered by an earlier release, naming `vecten update`.

#### Scenario: No order declared

- **WHEN** `vecten.toml` declares no `[deploy] promotion`
- **THEN** a push to the default branch ships `prod`, as it did before the key existed

#### Scenario: Invalid order

- **WHEN** `[deploy] promotion = ["dev", "staging"]` names no `prod`, `["prod", "staging"]` puts `prod` before another environment, or the list names an environment without a tfvars file
- **THEN** every command reading `vecten.toml` exits non-zero naming the key and the problem

#### Scenario: Three environments

- **WHEN** `[deploy] promotion = ["dev", "staging", "prod"]` and a change is merged
- **THEN** the push ships `dev`, `vecten promote --env staging` ships the commit `dev` shipped, and `vecten promote --env prod` ships the commit `staging` shipped

#### Scenario: Workflows older than the order

- **WHEN** a person records an order in a project whose `[project] scaffold_version` predates promotion
- **THEN** the command exits non-zero naming `vecten update`, and `vecten.toml` is unchanged

### Requirement: A later environment ships only a commit the environment before it shipped

The apply and ship workflows SHALL take a `commit` input beside `environment`, run on the default branch, and check out that commit. Inside GitHub Actions, before any Terraform or ship, the CLI SHALL refuse a commit that is not an ancestor of the remote default branch, and, for an environment after the first in the order, a commit whose `vecten/ship/<previous env>` status is not `success`, naming the commit and the environment. A promoted run SHALL build its bundle and read the environment's tfvars and Terraform roots from the promoted commit, SHALL read the facts under `[environments.<env>]` from the default branch's head, and SHALL install the `vecten` version the promoted commit's `[project] scaffold_version` names.

#### Scenario: Promotion of what dev shipped

- **WHEN** `[deploy] promotion = ["dev", "prod"]`, commit `a3f9c2e` shipped to `dev` with status `success`, and the ship workflow is dispatched for `prod` with `commit: a3f9c2e`
- **THEN** the bundle at `a3f9c2e` is on every `prod` host, built by the `vecten` version `a3f9c2e` pins

#### Scenario: Commit dev never shipped

- **WHEN** the ship workflow is dispatched for `prod` with a commit whose `vecten/ship/dev` status is missing or `failure`
- **THEN** the run exits non-zero naming the commit and `dev`, and no host changes

#### Scenario: Commit off the default branch

- **WHEN** the ship workflow is dispatched with a commit that only a feature branch holds
- **THEN** the run exits non-zero naming the commit and the default branch, and no host changes

#### Scenario: Facts recorded after the promoted commit

- **WHEN** `prod` was first deployed after commit `a3f9c2e` was made, so `a3f9c2e`'s `vecten.toml` records no deploy for `prod`, and `a3f9c2e` is promoted to `prod`
- **THEN** the run reads `prod`'s facts from the default branch's head and ships

### Requirement: Every ship records its outcome on the commit

Every ship that runs in GitHub Actions SHALL set the commit status `vecten/ship/<env>` on the commit it ships: `pending` when it starts, `success` once every host it ships answers its health poll, and `failure` otherwise. The newest commit on the default branch, among its last 200, whose status for an environment is `success` SHALL be what that environment shipped last.

#### Scenario: Ship succeeds

- **WHEN** the ship workflow ships commit `a3f9c2e` to `dev` and both hosts answer
- **THEN** `a3f9c2e` carries `vecten/ship/dev` with state `success`

#### Scenario: Ship rolls back

- **WHEN** a ship of `b71d004` to `dev` fails its health poll and the hosts restore `a3f9c2e`
- **THEN** `b71d004` carries `vecten/ship/dev` with state `failure`, and `a3f9c2e` is what `dev` shipped last

## MODIFIED Requirements

### Requirement: Content ships on merge without Terraform

A rendered ship workflow SHALL run on every push to the default branch and ship the first environment of the promotion order, `prod` when none is declared, once that environment is deployed; it SHALL read the order from the pushed commit's `vecten.toml` when it runs, so changing the order needs no re-render. It SHALL run `vecten ship --env <env>`, which builds one content bundle, the DAGs, the dbt project, `osi/` and `evals/` at that commit, ships it to every component host through the cloud's `ship` mechanism, and runs no Terraform and creates nothing billable. The bundle SHALL record its commit, and every host SHALL keep the previous bundle to roll back to when the health poll fails.

#### Scenario: Merged metric fix reaches both hosts

- **WHEN** a pull request editing `osi/metrics.json` and a dbt mart merges to main in a project with no promotion order
- **THEN** the ship workflow places the bundle at that commit on `prod`'s platform host and MCP host without anyone dispatching `deploy`

#### Scenario: Merge with a promotion order

- **WHEN** `[deploy] promotion = ["dev", "prod"]` and a pull request merges to main
- **THEN** the ship workflow ships the merged commit to `dev`, and no `prod` host changes

#### Scenario: Ship before deploy

- **WHEN** a push to main happens before the first environment of the order has ever been deployed
- **THEN** the ship workflow exits without shipping and says the environment is not deployed

#### Scenario: Health poll fails after ship

- **WHEN** a shipped bundle leaves a component's URL unanswered or not healthy (Airflow: HTTP 200 with a healthy metadatabase; the MCP: HTTP 200 from `/health`)
- **THEN** the host restores the previous bundle and the workflow fails naming the component

#### Scenario: Agent fails after the swap

- **WHEN** the VM agent fails after it has pointed `current` at the new bundle
- **THEN** the agent itself restores the previous bundle and NGINX, and the workflow fails naming the step and the commit it rolled back to

#### Scenario: A container fails at startup

- **WHEN** the MCP container exits at startup and restarts in a loop, so `compose up` fails
- **THEN** `vecten ship` and the workflow print, after the failure, the last log lines of each failing container with its service name, every secret value the agent injected masked and the rest redacted, without anyone opening a session on the VM

#### Scenario: Compose fails before a container runs

- **WHEN** `docker compose up` fails on a host before any container is failing, as when an image cannot be pulled or built
- **THEN** `vecten ship` and the workflow print, after the failure, the last lines of compose's own output, masked and redacted the same way
