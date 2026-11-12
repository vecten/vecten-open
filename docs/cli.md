# The `vecten` CLI

For anyone running Vecten Open from a shell or an agent: every command, what it does, and the contract every command keeps.

## The `--json` contract

Every command takes `--json` and then prints one JSON object: `ok` (true or false), `data` (the
command's result) and `message` (one line for a person), or `error` when it failed. The exit code
names the failure class and the set is stable within a minor release: see [exit codes](exit-codes.md).
The one exception is `vecten dbt <args>`, which forwards dbt's own output and exit code.

An agent runs `vecten next --json` to learn what the project needs now, in priority order, and
runs it again after every step; the [agentic flow](agentic-flow.md) page explains the loop.

## Commands

| Command | What it does | Status |
|---|---|---|
| `answer` | Record one answer to an `ask` item: intake and deploy answers in `vecten-answers.yml`, where `none` records no cloud account, domain or GitHub account yet; `access.<connector>`, `mcp.*` and `alert_webhook.<env>` land in `vecten.toml`. | v0.1 |
| `next` | What is missing, in priority order, as `command`, `skill` and `ask` items: the intake questions outside a project, the local loop, deploy and post-deploy items inside one; reads files, run records and git only. | v0.1 |
| `init` | Render a project from the intake; `--deploy` adds the deploy layer later. On a Snowflake warehouse the platform builds, `--snowflake-account` or the `snowflake_account` answer names its ORGANIZATION-ACCOUNT, which both `deploy/envs/prod.tfvars` and `[warehouse] account` in `vecten.toml` get. | v0.1 |
| `up` | Start the local stack for the components in `vecten.toml`; on Snowflake it leaves the MCP out, which runs only deployed there. | v0.1 |
| `down` | Stop the local stack; `--volumes` also deletes its data. | v0.1 |
| `guide` | Print the entry skill or a setup guide (`cloud`, `domain`, `github`). | v0.1 |
| `estimate` | Approximate monthly hosting cost per cloud and tier. | v0.1 |
| `connector` | `add` wires a dlt source into the project; `run` syncs it locally. | v0.1 |
| `raw` | `schema <source>` dumps the raw tables and columns for the model skill. | v0.1 |
| `dbt` | Passes its arguments to dbt against the project's profile; output and exit code are dbt's own, the one command outside the `--json` contract. Without `--env` it runs against the laptop stack's Postgres. A leading `--env <env>` runs against that deployed environment's Snowflake warehouse as its `LOADER` user, with the key from the environment's secrets store; on `prod` only commands that build nothing run, and a Postgres project is refused. Both write `dbt/packages.yml`, merging the project's pinned packages from `dbt/project-packages.yml`; see [warehouse](warehouse.md#dbt-against-a-deployed-environment). | v0.1 |
| `dbt-build-local` | Builds and tests the dbt project with no credentials on a disposable Postgres container or the Snowflake emulation, then regenerates `osi/datasets.json`; `--check` fails when the committed file is stale. | v0.1 |
| `secret` | `set <name>` stores a secret from the prompt or stdin; `list` shows names only. | v0.1 |
| `github` | `init` creates the repository and pushes; `push` commits and pushes. | v0.1 |
| `bootstrap` | `--env <env>` prepares an environment for CI from the laptop: state bucket, CI identity, GitHub environment. See [bootstrap in detail](#bootstrap). | v0.1 |
| `deploy` | `--env <env>` creates or updates an environment with Terraform and ships every host; `--plan-only` plans. See [deploy in detail](#deploy). | v0.1 |
| `ship` | `--env <env>` sends the project's content to every host of a deployed environment and rolls a host back when it does not come up healthy. See [ship in detail](#ship). | v0.1 |
| `destroy` | `--env <env>` destroys an environment after a final snapshot of every database; `--confirm <env>` is required without a terminal. See [destroy in detail](#destroy). | v0.1 |
| `user` | `add-admin <firstname> --env <env>` creates a warehouse administrator login and stores its password in the secrets store. See [user add-admin in detail](#user-add-admin). | v0.1 |
| `login` | `vecten login` authenticates the CLI using device authorization or `--browser`. | v0.1 |
| `logout` | `vecten logout` revokes the cached CLI session and removes local tokens. | v0.1 |
| `group` | `vecten group` manages MCP groups and their members. | v0.1 |
| `service` | `vecten service` manages independent service principals and issues client credentials. | v0.1 |
| `grant` | `vecten grant` records dataset or schema access for a group; see the current Postgres limitations in the identity guide. | v0.1 |
| `revoke` | `vecten revoke` removes a desired group grant. | v0.1 |
| `reconcile` | `vecten reconcile` reapplies desired warehouse grants. | v0.1 |
| `deny` | `vecten deny` stores per-group column metadata exclusions for later semantic discovery tools. | v0.1 |
| `mcp` | `validate` checks every Ossie document under `osi/` the way the server will and records the result for `next`; source existence needs a warehouse and is skipped on a laptop. `connection` prints the deployed MCP URL and how to connect a client. `vecten mcp import --dbt <path>` writes `osi/datasets.json` from an existing dbt project and `vecten mcp introspect` drafts one from the warehouse's catalog. `vecten mcp admin-link --env <env> [--email <admin>]` issues a set-password link for an address in `[mcp] admins` of a deployed environment in password mode; it goes through the secrets store as `MCP_ADMIN_LINK`, and the MCP accepts it once, within 15 minutes. A laptop's MCP keeps the startup `MCP_ADMIN_SETUP_TOKEN` instead. | v0.1 |
| `issues` | What is broken in a running platform: Airflow task failures, dbt test and freshness failures and open semantic gaps, in one shape whose `kind` and `resolution` say which skill, command or question fixes each. Without `--env` it reads the laptop stack; with `--env <env>` the deployed Airflow, the environment's bucket and the admin API. `vecten issues show <id>` prints one issue's redacted, capped evidence, marked untrusted. Changes nothing; see [operations](operations.md). | v0.1 |
| `extension` | Install a Python extension and pin its dependencies; see [extension authoring](extensions.md). | v0.1 |
| `lock` | Resolve `requirements.txt` (one exact pin per extension), one lock per component (`requirements.lock` for the CLI, `requirements-airflow.lock`, `requirements-mcp.lock`) and `extensions.lock.json` together, or change nothing. Each lock starts with a hash of its inputs, which `up` and the bundle build check; existing pins stay unless the inputs need a move, and `--upgrade <package>` lets one move. `--json` lists packages pinned differently across locks. See [dependencies](extensions.md#dependencies-and-locks). | v0.1 |
| `update` | Merge the installed release into the project using the saved pristine scaffold; preserve owner edits and refuse conflicts without changing files. | v0.1 |
| `version` | Print the vecten version and pinned stack versions. | v0.1 |


Identity commands are described in the [MCP identity guide](mcp-identity.md):
`vecten user add`, `vecten user list`, `vecten user disable`, `vecten user delete`,
`vecten user set-admin`, and `vecten user password-link` manage people through the admin API.
`vecten group add`, `vecten group list`, `vecten group delete`, `vecten group add-member`, and
`vecten group remove-member` manage group membership. `vecten service add`,
`vecten service list`, `vecten service disable`, and `vecten service delete` manage services.
`vecten mcp provisioner-script` prints administrator-run setup SQL, and
`vecten mcp introspect` drafts a semantic model from catalog metadata.

## Command examples

Every command below supports `--json`. `vecten dbt` forwards dbt output and its exit code.
Use `<command> --help` for flags. These are examples, not a script to run in order; cloud
commands need an explicit `--env` and the owner's approval.

| Command | Example and purpose |
|---|---|
| Intake | `vecten answer name my-project`; `vecten init my-project --name my-project --users 5 --source hubspot` |
| Guidance | `vecten guide`; `vecten estimate`; `vecten next --json`; `vecten version` |
| Local stack | `vecten up`; `vecten down`; use `--volumes` only to delete local data |
| Ingestion | `vecten connector add demo --from rest`; `vecten connector run demo --record`; `vecten raw schema demo --json` |
| Modelling | `vecten dbt ls`; `vecten dbt-build-local`; `vecten dbt-build-local --check` |
| Semantic files | `vecten mcp validate`; `vecten mcp connection` prints the configured endpoint, not proof of a working query session |
| Secrets | `vecten secret set DEMO_TOKEN` prompts privately; `vecten secret list` prints names |
| GitHub | `vecten github init`; `vecten github push` commit and push the project |
| Environment | `vecten bootstrap --env sandbox`; `vecten deploy --env sandbox --plan-only`; `vecten deploy --env sandbox` |
| Content delivery | `vecten ship --env sandbox` ships to an already deployed environment |
| Warehouse administration | `vecten user add-admin alex --env sandbox` creates a warehouse administrator |
| Teardown | `vecten destroy --env sandbox --confirm sandbox` takes snapshots, then destroys infrastructure |
| Extensions | `vecten extension add acme-resolution==1.2.0` installs and records an extension; `vecten lock` re-resolves the locks after a `[dependencies]` change, `vecten lock --upgrade httpx` moves one package; see [author contracts and private indexes](extensions.md) |
| Upgrade | `vecten update --json` applies the installed release to this project |

## Deploy commands in detail

### bootstrap

`--env <env>` prepares an environment for CI from the laptop. On AWS, with your AWS login, it creates the Terraform state bucket (private, versioned, encrypted, TLS-only), the GitHub Actions OIDC provider, the permissions boundary every host role carries, the deploy role that only jobs declaring `environment: <env>` in the project's repository may assume, and a plan role for pull requests and the default branch that reads only what a plan refreshes; then it creates that GitHub environment when absent, deployable from the default branch only (it warns when the repository's plan offers no branch rules, and never rewrites an existing environment), sets its `VECTEN_AWS_ROLE_ARN`, `VECTEN_AWS_REGION` and `VECTEN_STATE_BUCKET` variables and the repository's `VECTEN_AWS_PLAN_ROLE_ARN_<ENV>` (the environment name uppercased, `-` as `_`), and records `bootstrapped` and `state_bucket` in `vecten.toml`. The OIDC subjects use the repository's name as GitHub spells it. On Scaleway, with your Scaleway login, it creates the environment's resources and state projects, the state bucket in the latter, and IAM applications with their policies and keys for deploying, for pull-request plans and for each host, none holding IAM rights; it creates the GitHub environment as on AWS, stores the deploy key as its secrets `VECTEN_SCW_ACCESS_KEY` and `VECTEN_SCW_SECRET_KEY`, the plan key as the repository's `VECTEN_SCW_PLAN_ACCESS_KEY_<ENV>` and `VECTEN_SCW_PLAN_SECRET_KEY_<ENV>`, and each host's key in the `deploy` scope of Secret Manager, sets the environment's `VECTEN_SCW_PROJECT_ID`, `VECTEN_SCW_STATE_PROJECT_ID`, `VECTEN_SCW_REGION` and `VECTEN_STATE_BUCKET`, and records the ids as `cloud_ids` in `vecten.toml`, the project holding the DNS zone among them, which it finds through the Domains and DNS API and refuses (exit 3) without. Keys expire after 365 days, and a rerun within 30 days of an expiry rotates them. Safe to run again.

### deploy

`--env <env>` creates or updates an environment. Inside GitHub Actions it runs Terraform as a two-step plan and apply on the project's cloud root (state at `<env>/<cloud>.tfstate` in the state bucket, locked by an S3 lock object), writes the outputs to `.vecten/environments/<env>.json` and the state bucket, and ships every host. On a project whose platform builds a Snowflake warehouse it also plans and applies `deploy/terraform/snowflake` (state at `<env>/snowflake.tfstate`), signing in as the deploy user from `DEPLOY_SNOWFLAKE_USER` and `DEPLOY_SNOWFLAKE_PRIVATE_KEY` in the secrets store, and generates the LOADER user's key pair into `SNOWFLAKE_LOADER_PRIVATE_KEY` when it is absent; only the public key reaches Terraform. With the MCP on such a project it ships no host until `vecten mcp provisioner-script --env <env> --confirm-run` is recorded, because the MCP starts only once an ACCOUNTADMIN has created its Snowflake identity ([docs/mcp-identity.md](mcp-identity.md#a-snowflake-warehouse-the-platform-builds)). From a laptop it refuses unless the checked-out commit is on the default branch and `vecten.toml` and `deploy/` hold nothing unpushed, then dispatches the apply workflow, follows it and records the run's commit as `deployed_commit` in `vecten.toml`. On Scaleway, after the apply it hands each VM its key through the server's user data, deletes the entry once the VM acknowledges the key, and deletes the databases' public endpoint when `database_public_cidrs` is empty. `--plan-only` plans and applies nothing, in Actions or locally with your cloud login, and writes the plan to `.vecten/plans/<env>.txt`; on a Snowflake project the first plan generates the loader key pair and stores its private key in the secrets store, because the plan needs the public key; with `--cloud-only` it plans the cloud root alone and reads no Snowflake secret, as the pull-request plan does, which on Scaleway also skips the refresh, because the plan key reads nothing outside the state. `--check` reports `bootstrapped` and `deployed` from `vecten.toml` with no cloud credentials. Refuses an environment that is not bootstrapped or whose tfvars lack a required value.

### ship

`--env <env>` sends the project's content to every host of a deployed environment, through SSM on AWS, and on Scaleway by uploading each host's bundle and moving the pointer object its VM's watcher pulls, then waiting for the watcher's status object; it polls each component's health URL and rolls a host back when it does not answer healthy (the VM agent rolls back by itself when it fails after switching releases); inside GitHub Actions it ships, from a laptop it dispatches the ship workflow and follows it. On a platform-built Snowflake with the MCP whose identity script is not yet recorded, it refuses from the laptop and ships nothing inside Actions. `--check` reports `bootstrapped` and `deployed` from `vecten.toml` with no cloud credentials. Never runs Terraform.

### destroy

`--env <env>` destroys an environment from the laptop with your cloud login once you type the environment's name back, or pass it as `--confirm <env>` (with `--json`, without a terminal or inside GitHub Actions `--confirm` is required); there is no `--yes`. It first takes a final snapshot of every Postgres instance the environment's outputs name and prints each identifier, then lifts deletion protection with an apply of `destroying=true` targeted at whichever of the RDS instances and the artifacts bucket the state still holds (skipped when none remain), refused when that plan would create or delete anything, and runs `terraform destroy`; a root whose state is already empty is reported, not touched. `--delete-data <env>` takes no snapshot and also destroys the Snowflake database, which is otherwise kept. On Scaleway the snapshots go through the Managed Databases API and are the databases' only guard, since those offer no deletion protection, and the protection-lifting apply targets the artifacts bucket alone. The state bucket and the secrets stay, except on Scaleway the secrets Terraform created; the laptop clears `deployed_commit` in `vecten.toml`.

### user add-admin

`add-admin <firstname> --env <env>` creates the warehouse login `admin_<firstname>` with `rds_superuser` and CONNECT on `warehouse`, over the instance's master credential through an SSM port-forwarding tunnel from the laptop (needs the AWS CLI and `session-manager-plugin`); on Scaleway, which grants no superuser, it makes the login a member of `warehouse_owner` and `loader` and connects to the instance's public endpoint, refusing until `database_public_cidrs` in the tfvars opens it, with the server checked against the instance's certificate authority; the password goes to `ADMIN_<FIRSTNAME>_PASSWORD` in the secrets store and only its id is printed. An existing login keeps its password, unless that secret is missing, in which case it gets a new one. Refused over an existing warehouse and on Snowflake.

## Updating a project

Install the desired Vecten release in the same environment used for the project (after
publication, for example `uv tool install --force vecten==<version>`), then run
`vecten update --json` from the project or a subdirectory. The four packages are `vecten`, `vecten-platform`, `vecten-mcp` and `vecten-shared`.
Their internal dependencies are pinned to the same release. Installing the CLI brings its
platform/shared dependencies; the MCP runtime image supplies `vecten-mcp`. In a source checkout, use that checkout's CLI with `uv run --project
/path/to/checkout vecten update --json`.

The installed release is the target; `update` does not download a release or deploy anything.
Commit or back up your project first. The command merges newly rendered files with your
edits using `.copier-answers.yml` and `.copier-baseline.json`. Commit both metadata files;
the baseline contains pristine generated files, never connector records or secret values.
DAGs and marts you added are outside the template and remain untouched. Deleted templates
stay deleted unless the release also changes them, which requires a decision.

The result names `from_version`, `to_version`, `changed`, `conflicts` and whether it initialized
legacy metadata (`adopted_baseline`). On a conflict it exits 4, lists the paths and changes
nothing, including version pins. Back up your edited file, compare it with the pristine copy
in the baseline and the new release's template, reconcile the overlapping edit, and retry.
For a file you want to rebuild from upstream, restore its old pristine contents before
retrying, then reapply your backed-up changes. For example, `jq -r '.files["README.md"]'
.copier-baseline.json` prints a pristine README (the command adds a final newline).
The installed template directory is printed by `python -c "from vecten.scaffold.render import
template_root; print(template_root())"` in the CLI's Python environment; templates are under
its `template/` subdirectory and use the saved answers when rendered. Never delete the metadata to suppress a conflict.

Internal projects created before upgrade metadata existed can initialize it using the same
CLI release that created them. An older project without that metadata is refused by a newer
CLI, because reconstructing its old templates from a new release could lose edits.

Terraform roots move to the release's module ref. Compose files, image tags and the
`vecten_convention` dbt package are generated from the installed packages, rather than copied
into the project; the next `up`, `dbt-build-local` or approved `ship` uses those versions.
After updating, review the diff, run `vecten dbt-build-local --check` when the platform is
present and `vecten mcp validate`, and commit the result. Infrastructure changes still need
a reviewed Terraform plan and an explicitly approved deployment.

Back to the [docs index](README.md).
