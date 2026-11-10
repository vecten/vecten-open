# cli/cli-lifecycle Specification

## Purpose
The lifecycle commands of the `vecten` CLI: `answer`, `init`, `github`, `up`, `down`, `bootstrap`, `deploy`, `destroy`, `secret`, `guide`, `estimate` and `update`, the `mcp`, `extension` and `user` command groups, and the machine-readable output and exit code contract that every command honours.

## Requirements

### Requirement: Every command supports --json

Every CLI command SHALL accept a `--json` flag and, when it is given, write its result to standard output as a single JSON document and nothing else. Human-readable output and JSON output SHALL be produced from the same result so that the two never disagree. The one exception is `dbt <args>`, which forwards dbt's own output unchanged.

#### Scenario: Agent reads a command result

- **WHEN** any command is invoked with `--json`
- **THEN** standard output is one parseable JSON document
- **AND** no progress text, prompts or decoration are mixed into standard output

#### Scenario: Interactive prompt under --json

- **WHEN** a command that would prompt is invoked with `--json` and the answer is not supplied by a flag or answers file
- **THEN** the command exits with a non-zero code and a JSON error naming the missing input instead of prompting

### Requirement: Stable documented exit codes

Every CLI command SHALL exit with a code from a documented, stable set, where 0 means success and each distinct failure class has its own non-zero code. The set MUST be listed in the project documentation and MUST NOT change between patch releases. The one exception is `dbt <args>`, which forwards dbt's exit code unchanged.

#### Scenario: Failure class is distinguishable

- **WHEN** a command fails because of invalid user input
- **THEN** its exit code differs from the code used for an infrastructure or tool failure
- **AND** the documented table maps the code to that failure class

### Requirement: answer records the intake

`answer <key> <value>` SHALL record one intake answer in `vecten-answers.yml` in the current directory, outside a project for the local intake and inside one for the deploy questions, and SHALL refuse an unknown key or a value outside that key's allowed answers. `next` reads this file, `init` consumes the local answers and `init --deploy` the deploy answers.

#### Scenario: Record an answer

- **WHEN** `answer cloud aws` is run inside a project whose local loop is clear
- **THEN** the project's `vecten-answers.yml` holds `cloud: aws`
- **AND** `next` no longer reports the cloud question

#### Scenario: Invalid answer

- **WHEN** `answer cloud digitalocean` is run
- **THEN** the command exits non-zero, lists the accepted answers `aws`, `scaleway` and `none`, and the file is unchanged

### Requirement: sources answer names connectors

The sources answer SHALL be `start empty`, `none`, or comma-separated connector names; `answer` and `init` SHALL refuse any other entry, naming it. Inside a rendered project with the `platform` component, `answer sources` SHALL also set `wanted` in `vecten.toml` to the names given, less those already declared as connectors. Inside an MCP-only project it SHALL refuse, saying sources belong to the `platform` component.

#### Scenario: Free text as a source

- **WHEN** `answer sources "demo, our CRM at https://api.demo.test/v1"` is run
- **THEN** the command exits non-zero naming `our CRM at https://api.demo.test/v1` as not a source name, and the file is unchanged

#### Scenario: Correct the sources after init

- **WHEN** `answer sources demo` is run inside a platform project whose `vecten.toml` lists `wanted = ["demo", "crm"]` and declares no connector
- **THEN** `vecten.toml` lists `wanted = ["demo"]` and `next` reports `Connect demo` and no `Connect crm`

### Requirement: init takes the intake

`init [dir]` SHALL take the intake answers as flags (`--cloud`, `--region`, `--warehouse`, `--name`, `--domain`, `--dns-zone`, `--github-repository`, `--users`, `--source` once per wanted source, `--size` overriding the derived tier) or from `--answers-file`, and SHALL render the project into the target directory, refusing, naming every missing answer, until the local answers are present: the name, `users`, and the sources or the `warehouse_url`. Region, warehouse and size take their defaults.

#### Scenario: Complete intake

- **WHEN** `init my-project` is run with cloud, name, domain, DNS zone, GitHub repository and users given and two `--source` flags
- **THEN** a rendered project exists in `my-project/` with the two sources wanted and its deploy layer

#### Scenario: Size from users

- **WHEN** `init` is given `--users 30` and no `--size`
- **THEN** `vecten.toml` records `size = "medium"` by the package thresholds

#### Scenario: Answers file

- **WHEN** `init --answers-file answers.yml` is run with the intake complete in the file
- **THEN** the project renders with no prompts

#### Scenario: Missing answer

- **WHEN** `init` is run without `--users` and without an answers file naming the users
- **THEN** `init` fails with a non-zero exit code and names `users` as missing
- **AND** nothing is rendered

### Requirement: init renders the deploy layer only from complete deploy answers

Cloud, region, domain, DNS zone and GitHub repository are the deploy answers. When all are given, `init` SHALL also render the deploy layer; otherwise it SHALL render the project without it, so a laptop trial needs no cloud, domain or GitHub account.

#### Scenario: Local answers only

- **WHEN** `init my-project` is run with name, users and two `--source` flags and no cloud, domain or GitHub repository
- **THEN** the project renders without `deploy/` and without the cloud workflows
- **AND** `up` runs it on the laptop

### Requirement: init takes the component answers

`init` SHALL also take `--components` (`platform`, `mcp`, or both; both by default), `--profile` (`standard` by default or `minimal`), `--has-warehouse` and `--warehouse-url`. With `--has-warehouse yes`, `init` SHALL set `components` to `mcp` only, SHALL require `--warehouse-url`, and SHALL refuse `--source`.

#### Scenario: Existing warehouse

- **WHEN** `init` is run with `--has-warehouse yes --warehouse-url postgres://host:5432/dw` and no `--source`
- **THEN** the project renders with `components = ["mcp"]` and the warehouse locator recorded for the `mcp` component

### Requirement: warehouse_url holds a locator, never a secret

`init` SHALL store only the locator part of `--warehouse-url`, kind, host, port, database and, for Snowflake, account, in `vecten.toml`, and SHALL refuse a URL that carries a password, naming `secret set WAREHOUSE_ADMIN_PASSWORD` as the place for it.

#### Scenario: Password in the URL

- **WHEN** `init` is run with `--warehouse-url postgres://admin:secret@host/dw`
- **THEN** it exits non-zero, says the password must go through `secret set`, and renders nothing

#### Scenario: Source given for an existing warehouse

- **WHEN** `init` is run with `--has-warehouse yes` and a `--source` flag
- **THEN** `init` exits non-zero and says sources belong to the `platform` component

#### Scenario: Minimal profile on a single component

- **WHEN** `init` is run with `--components mcp --profile minimal`
- **THEN** the project renders with one VM and one Postgres instance, which is what MCP-only means under either profile

### Requirement: init reads recorded answers

With no answer flags and no `--answers-file`, `init` SHALL read `vecten-answers.yml` from the target directory.

#### Scenario: Answers recorded with answer

- **WHEN** every intake answer has been recorded with `answer` and `init` is run with no flags
- **THEN** the project renders from `vecten-answers.yml`

### Requirement: init --deploy renders the deploy layer

Inside a rendered project, `init --deploy` SHALL read the deploy answers from flags or the project's `vecten-answers.yml`, and `profile` from `vecten.toml`, refuse naming each missing one, and render the deploy layer the `cli/scaffold` spec defines. It SHALL record the answers in `vecten.toml` and `.copier-answers.yml`, and bring the cloud sections into `docs/runbook.md` by the three-way merge `update` uses, so the human's edits stay. When those edits conflict with the sections, it SHALL leave the runbook as it is and name it in the result for a hand merge, without refusing the deploy layer. It SHALL leave every other file untouched.

#### Scenario: Deploy layer after the laptop loop

- **WHEN** `init --deploy` runs in a project rendered from local answers, with cloud, domain, DNS zone and GitHub repository recorded
- **THEN** `deploy/terraform/<cloud>/`, `deploy/envs/prod.tfvars` with no placeholder, and the cloud workflows exist
- **AND** connectors, DAGs, dbt models and `osi/` are unchanged
- **AND** `docs/runbook.md` holds the cloud's sections, its restore section among them

#### Scenario: Edited runbook

- **WHEN** `init --deploy` runs in a project whose runbook the human edited where the cloud sections go
- **THEN** the deploy layer renders, the runbook keeps the human's text, and the result names `docs/runbook.md` for a hand merge

#### Scenario: Deploy answer missing

- **WHEN** `init --deploy` runs with no domain recorded
- **THEN** it exits non-zero, names `domain` as missing and renders nothing

#### Scenario: Run again

- **WHEN** `init --deploy` runs a second time with the same answers
- **THEN** no file changes and the command exits 0

#### Scenario: Cloud changed

- **WHEN** `init --deploy --cloud scaleway` runs in a project whose deploy layer was rendered for `aws`
- **THEN** it exits non-zero and states that changing cloud is not supported by `init --deploy`

### Requirement: init writes answers and configuration

On success `init` SHALL write `.copier-answers.yml` holding the given answers, `vecten.toml` holding the project configuration, the `components` and the wanted source names, and, when the deploy answers are present, `deploy/envs/prod.tfvars` fully filled from them and from `profile`, into the project root.

#### Scenario: Files written after init

- **WHEN** `init` completes successfully from a complete intake
- **THEN** `.copier-answers.yml`, `vecten.toml` and `deploy/envs/prod.tfvars` exist
- **AND** `vecten.toml` records the chosen cloud, warehouse, `components` and `profile` and lists the wanted sources
- **AND** `prod.tfvars` holds the domain, DNS zone, region and GitHub repository with no placeholder left

### Requirement: github init and github push

`github init` SHALL create the repository named at intake under the human's GitHub login through the `gh` CLI, and push the project to it. `github push` SHALL commit the project's changes and push them. Both SHALL exit non-zero naming `gh auth login` when `gh` is missing or logged out.

When an environment has been deployed and the push moves `main`, `github push` SHALL wait for the `ship.yml` run that push starts and SHALL exit non-zero naming the run's URL when it does not succeed, or naming the workflow when no run appears within two minutes; the push itself stands either way. `--no-wait`, a branch other than `main`, or a push that moves nothing SHALL return once pushed.

#### Scenario: Push after the deploy

- **WHEN** `github push` runs on a deployed project with a changed `vecten.toml`
- **THEN** it returns after the ship run for the pushed commit succeeds, reporting the run's URL

#### Scenario: The ship fails

- **WHEN** the ship run for the pushed commit fails
- **THEN** `github push` exits non-zero naming the run's URL, and the commit is on GitHub

#### Scenario: First push

- **WHEN** `github init` runs right after `init`
- **THEN** the repository exists on GitHub and holds the rendered project

#### Scenario: Not logged in

- **WHEN** `github init` runs and `gh` is not logged in
- **THEN** the command exits non-zero and names `gh auth login`

### Requirement: guide prints the package's know-how

`guide` SHALL print the entry skill text, and `guide cloud`, `guide domain` and `guide github` SHALL print the setup guide for a cloud account, a domain with a delegated DNS zone, and a GitHub account and repository, each written for a non-technical reader. The text SHALL come from data files in the installed package, so it works with no project and no network.

#### Scenario: Guide outside a project

- **WHEN** `guide cloud --json` is run in an empty directory with no network
- **THEN** standard output is the cloud account guide as one JSON document

### Requirement: estimate prints approximate hosting cost

`estimate [--cloud <cloud>] [--size <tier>] [--components <list>] [--profile <profile>]` SHALL print the approximate monthly hosting cost per cloud and per sizing tier for the given components and profile, `small`, both components and `standard` when none is given, with the `minimal` figure shown beside it, together with the date the figures were gathered, from a table in the installed package.

#### Scenario: Estimate carries its date

- **WHEN** `estimate --json` is run
- **THEN** the output lists a figure for each of the two clouds and the date those figures were gathered

#### Scenario: Minimal estimate

- **WHEN** `estimate --profile minimal --json` is run
- **THEN** each cloud's figure is lower than the `standard` figure for the same tier, by the second VM and the second instance

### Requirement: up starts the local stack

`up` SHALL start the project's Docker Compose stack on the local machine with the profiles that `components` names plus the `local` profile, SHALL wait until every started front door answers behind NGINX, SHALL print the Airflow URL when `platform` is present and the MCP URL when `mcp` is present, and SHALL leave a record under `.vecten/` for `next`. With `platform` it SHALL also print the loopback Airflow API. `up` requires no cloud account or cloud credentials.

#### Scenario: Local stack on a laptop

- **WHEN** `up` is run in a rendered project with both components on a machine with Docker and no cloud credentials
- **THEN** the `platform`, `mcp` and `local` profiles are running in Docker
- **AND** the printed Airflow URL answers 401 without credentials and serves the Airflow UI with them
- **AND** the printed MCP URL serves the login page

#### Scenario: MCP-only project

- **WHEN** `up` is run in a project whose `components` is `mcp` only
- **THEN** no Airflow service is started and only the MCP URL is printed

#### Scenario: Airflow API for the Airflow MCP server

- **WHEN** `up --json` has started a project with the `platform` component
- **THEN** it prints `airflow_api` as `http://127.0.0.1:8482`, served without NGINX, and `airflow_user` as `admin`
- **AND** it names `AIRFLOW_ADMIN_PASSWORD` in `.env` as the password and never prints its value

#### Scenario: Docker not available

- **WHEN** `up` is run and Docker is not reachable
- **THEN** the command exits non-zero with an error that names Docker as the missing prerequisite

### Requirement: down stops the local stack

`down` SHALL stop the local Compose stack started by `up`. It SHALL exit successfully when no stack is running.

#### Scenario: Stop a running stack

- **WHEN** `down` is run while the local stack is running
- **THEN** no project containers remain running

#### Scenario: Nothing to stop

- **WHEN** `down` is run and no stack is running
- **THEN** the command exits 0 and reports that nothing was running

### Requirement: deploy refuses an incomplete environment file

Before running any Terraform, `deploy --env <env>` SHALL check that `deploy/envs/<env>.tfvars` holds every required per-environment value and SHALL refuse, naming each missing value, when any is absent. The required values are those the deploy spec lists for the environment file.

#### Scenario: Missing environment values

- **WHEN** `deploy --env prod` is run and the domain line has been removed from `deploy/envs/prod.tfvars`
- **THEN** the command exits non-zero before any Terraform runs and names `domain` as missing

### Requirement: bootstrap prepares an environment for CI

`bootstrap --env <env>` SHALL run from the laptop with the human's cloud login and create, through the provider, the environment's state bucket, its secrets store and the identities the project's GitHub repository deploys and plans with, then SHALL set the variables and secrets the workflows read. On AWS those identities are an OIDC trust and the roles it lets the workflows assume. On Scaleway, whose IAM offers no OIDC federation, `bootstrap` SHALL create the environment's resources and state projects, IAM applications for deploying, for pull-request plans and for each host, their policies and expiring keys, and SHALL store the deploy key as GitHub environment secrets, the plan key as repository secrets and each host's key in the `deploy` scope of the secrets store, as the `shared/deploy` spec states. On Scaleway it SHALL also find, through the Domains and DNS API, the organization's project holding the tfvars' `dns_zone`, grant the deploy key DNS rights there and record it, so the rendered tfvars need no edit; when no project of the organization holds the zone it SHALL exit 3 naming the zone and create nothing. It SHALL be safe to run again and SHALL record `bootstrapped = true` under `[environments.<env>]` in `vecten.toml`, with any non-secret ids the provider needs later.

#### Scenario: First bootstrap

- **WHEN** `bootstrap --env prod` runs with the human logged in to the cloud's CLI
- **THEN** the state bucket, secrets store and CI identities exist and the workflows' variables and secrets are set

#### Scenario: Second run

- **WHEN** `bootstrap --env prod` runs again
- **THEN** nothing is recreated and the command exits 0

#### Scenario: DNS zone missing on Scaleway

- **WHEN** `bootstrap --env prod` runs on Scaleway and no project of the organization holds the tfvars' `dns_zone`
- **THEN** the command exits 3 naming the zone, and nothing is created

#### Scenario: Key rotation on Scaleway

- **WHEN** `bootstrap --env prod` runs on Scaleway within 30 days of a key's expiry
- **THEN** that key gets a successor in the same place and the old key stays valid; a later `bootstrap` run deletes it: a deploy or plan key on the next run, so a workflow run already under way finishes with it, and a host's key on the first run after the host acknowledged its successor

#### Scenario: Key expiry warning on Scaleway

- **WHEN** a key `bootstrap` created on Scaleway expires within 30 days, by the expiry it recorded under `cloud_ids` in `vecten.toml`
- **THEN** `next` leads with `vecten bootstrap --env prod`, and `deploy --env prod` prints a warning naming the key and its expiry date and still deploys

### Requirement: deploy runs in GitHub Actions

Inside GitHub Actions, `deploy --env <env>` SHALL run Terraform plan and apply for the project's cloud and environment as a two-step apply, then SHALL ship each component in `components` to its host and start that component's profile there. With `--plan-only` it SHALL stop after the plan and change nothing.

#### Scenario: Apply workflow run

- **WHEN** the dispatched apply workflow runs `deploy --env prod` for a project with both components under the `standard` profile
- **THEN** Terraform plans and applies, the `platform` profile is shipped to the platform VM and the `mcp` profile to the MCP VM

#### Scenario: Minimal profile

- **WHEN** the dispatched apply workflow runs `deploy --env prod` for a project with `profile = "minimal"`
- **THEN** both profiles are shipped to the one VM and started there

### Requirement: ship sends content without Terraform

Inside GitHub Actions, `ship --env <env>` SHALL build the content bundle at the checked-out commit and ship it to every component host, running no Terraform. Outside GitHub Actions it SHALL dispatch the ship workflow and follow it. It SHALL refuse, naming `deploy --env <env>`, when the environment has never been deployed.

#### Scenario: Ship on merge

- **WHEN** the ship workflow runs `ship --env prod` after a merge to main
- **THEN** the bundle at that commit is on every host, each component's URL answers, and no cloud resource changed

#### Scenario: Never deployed

- **WHEN** `ship --env prod` runs before any `deploy --env prod`
- **THEN** it exits non-zero naming `deploy --env prod`

### Requirement: deploy from the laptop dispatches CI

Outside GitHub Actions, `deploy --env <env>` SHALL dispatch the repository's apply workflow for the environment, follow it to completion, exit with its outcome, and on success record the deployed commit under `[environments.<env>]` in `vecten.toml`. It SHALL refuse, naming `bootstrap --env <env>`, when the environment has not been bootstrapped.

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

### Requirement: destroy requires the environment name as confirmation

`destroy --env <env>` SHALL run Terraform destroy for the environment, from the laptop with the human's cloud login, only after the environment name is supplied as confirmation: typed back interactively, or passed as `--confirm <env>` for non-interactive use such as CI teardown. With `--json` or without a terminal, `destroy` without `--confirm` SHALL exit 2 naming `--confirm <env>` instead of prompting; inside GitHub Actions it SHALL refuse without `--confirm`. A bare `--yes` that skips naming the environment MUST NOT exist. `destroy` SHALL leave the environment's state bucket in place. Run from the laptop, it SHALL clear in `vecten.toml` the facts whose subject lived in the destroyed `state` database (the first user, the admin link, the connector grants) and, on a Snowflake root with the `mcp` component, the provisioner script's confirmation, and say so.

#### Scenario: Confirmed destroy

- **WHEN** `destroy --env staging` is run and the user types `staging`
- **THEN** a final snapshot identifier is printed, then the environment's resources are destroyed
- **AND** the state bucket still exists

#### Scenario: Redeploy after a destroy that kept Snowflake

- **WHEN** `destroy --env prod --confirm prod` ran on a platform and MCP project over Snowflake, and `deploy --env prod` runs again without restoring `state`
- **THEN** the deploy ships no host until `vecten mcp provisioner-script --env prod` has run again, whose last query prints a `DROP` for each user and role `PROVISIONER` owns, and `next` walks the first user again

#### Scenario: Snowflake account that hosted an earlier install

- **WHEN** `vecten mcp provisioner-script` runs for a Snowflake database while `PROVISIONER` still holds grants on another database, warehouse or `VECTEN_ACCESS` procedure from an earlier install
- **THEN** a query in the script prints a `REVOKE … FROM ROLE PROVISIONER` for each such grant and a `DROP SCHEMA IF EXISTS <database>.VECTEN_ACCESS` for the earlier install's procedures, and the script sets the `VECTEN_EXTERNAL_OAUTH` integration's issuer, JWKS URL and audience to this deployment's even when the integration already existed

#### Scenario: Wrong confirmation

- **WHEN** the user types anything other than `staging`
- **THEN** nothing is destroyed and the command exits non-zero

#### Scenario: JSON without confirmation

- **WHEN** `destroy --env staging --json` is run without `--confirm`
- **THEN** the command exits 2 with a JSON error naming `--confirm staging`, and nothing is destroyed

### Requirement: destroy snapshots before it deletes

`destroy` SHALL take a final snapshot of every managed Postgres instance and print its identifier before destroying, unless `--delete-data <env>` names the environment.

#### Scenario: Snapshot kept

- **WHEN** `destroy --env prod --confirm prod` runs
- **THEN** a snapshot identifier per instance is printed before any resource is destroyed

#### Scenario: Non-interactive teardown in CI

- **WHEN** `destroy --env sandbox --confirm sandbox` is run
- **THEN** the environment's resources are destroyed without a prompt

### Requirement: secret set and secret list

`secret set <name>` SHALL store a secret through the provider, reading the value from a prompt or standard input and refusing a value passed as a command argument. Without `--env` it SHALL write `.env` for the local provider; with `--env <env>` it SHALL write the environment's cloud secrets store, and with `--from-local` it SHALL take the value from `.env` without printing it. `secret list` SHALL print secret names and never values.

#### Scenario: Local secret

- **WHEN** `secret set HUBSPOT_API_KEY` is run with the value on standard input and no `--env`
- **THEN** `.env` holds the value and the local stack reads it on the next start

#### Scenario: Cloud secret

- **WHEN** `secret set HUBSPOT_API_KEY --env prod` is run with the value on standard input
- **THEN** the value is in the `prod` environment's secrets store

#### Scenario: Copy to the cloud

- **WHEN** `secret set HUBSPOT_API_KEY --env prod --from-local` is run
- **THEN** the `.env` value is in the `prod` secrets store, never appears in the command's output, and the copy is recorded by name under `.vecten/`

#### Scenario: Value as an argument

- **WHEN** `secret set HUBSPOT_API_KEY abc123` is run
- **THEN** the command exits non-zero and says the value must come from the prompt or standard input

### Requirement: update bumps versions and re-renders only project-owned templates

`update` SHALL move the project to a newer `vecten` release by bumping the pins the project carries, the scaffold version in `.copier-answers.yml`, the Terraform module `ref`, the `vecten_convention` dbt package version and the image tags, and SHALL re-render only the project-owned files the scaffold still templates, preserving the user's edits where they do not conflict and reporting conflicts.

#### Scenario: Release bump

- **WHEN** `update` is run in a project rendered from an older release
- **THEN** the version pins move to the new release
- **AND** files the user edited and the scaffold did not change are untouched
- **AND** conflicting files are listed for manual resolution

#### Scenario: Module-only fix

- **WHEN** the new release changes only a Terraform module
- **THEN** `update` changes only the `ref` in the thin root module

### Requirement: user commands

`vecten user add --email <email> --name "<Firstname Lastname>" [--warehouse-name <name>]`, `vecten user disable <id>`, `vecten user delete <id>`, `vecten user list`, `vecten user set-admin <id> [--clear]` and `vecten user password-link <id>` SHALL act through the `mcp` component's admin API with the person's token from `vecten login`. A name starting with `admin_` or `svc_` SHALL be refused. Admins come from `[mcp] admins` at their first login, or from `user set-admin`.

#### Scenario: What add does

- **WHEN** `vecten user add` runs with an email and a name
- **THEN** it creates the principal keyed on the login identity for that email, derives the warehouse name unless `--warehouse-name` is given, provisions the identity or reports that adoption needs confirmation, and records `first_user_added` for the environment

#### Scenario: Add a person

- **WHEN** `vecten user add --email jane@acme.com --name "Jane Doe"` runs against a deployed `mcp` component
- **THEN** the person exists as a principal with the warehouse identity `jane_doe`
- **AND** a record under `.vecten/` names the provisioned user

#### Scenario: Adoption needs confirmation

- **WHEN** `vecten user add` finds an unbound `jane_doe` in the warehouse
- **THEN** the command prints the account's roles and last login and exits asking for `--adopt`
- **AND** with `--adopt` the identity is adopted

#### Scenario: Reserved name refused

- **WHEN** `vecten user add --email x@acme.com --name "Admin Jane" --warehouse-name admin_jane` runs
- **THEN** the command exits non-zero and names `vecten user add-admin`

#### Scenario: List shows no secrets

- **WHEN** `vecten user list --json` runs
- **THEN** the output lists emails, names, warehouse names, groups, admin flag and status and no credential

### Requirement: user add records who awaits a password link

Under password sign-in, `vecten user add` SHALL record the added person's principal id and email under `.vecten/` for the environment, unless `vecten mcp admin-link` already issued that address a link. `vecten user password-link <id>` SHALL mark the entry once the MCP issued the link, and `vecten user disable` and `vecten user delete` SHALL drop it, so `next` reports only links still owed.

#### Scenario: Password link recorded

- **WHEN** `[mcp] auth = "password"` and `vecten user add --email grace@acme.test --name "Grace Hopper" --env prod` succeeds
- **THEN** a record under `.vecten/` holds Grace's principal id and email with no link issued
- **AND** after `vecten user password-link <that id> --env prod` succeeds, the record says the link was issued

#### Scenario: The first admin adds themselves

- **WHEN** `vecten mcp admin-link --env prod` issued `ada@acme.test` a link and `vecten user add --email ada@acme.test --name "Ada Lovelace" --env prod` succeeds
- **THEN** `next` reports no password link for `ada@acme.test`

#### Scenario: Person removed before the link

- **WHEN** Grace's link was not issued and `vecten user delete <her id> --env prod` succeeds
- **THEN** the record no longer lists Grace and `next` reports no password link for her

### Requirement: login obtains a person's token

`vecten login` SHALL obtain a token for the person through the device authorization grant, or a loopback redirect when a browser is available, from the deployment's authorization server, cache it outside the project, and `vecten logout` SHALL discard it. Every `user`, `group` and `grant` command SHALL carry it.

#### Scenario: Login from a laptop

- **WHEN** `vecten login --env prod` runs
- **THEN** the person completes the flow in a browser and the token is cached outside the project

#### Scenario: Command without login

- **WHEN** `vecten user list` runs with no cached token
- **THEN** the command exits non-zero naming `vecten login`

### Requirement: login records the project's session

A successful `vecten login` SHALL leave a record under `.vecten/` naming the session it saved for the environment, without any token; `vecten logout` and `destroy` SHALL remove that record. When the MCP rejects a cached session, the command SHALL discard the session from the cache and exit 3 naming `vecten login`.

#### Scenario: Login record

- **WHEN** `vecten login --env prod` completes
- **THEN** the project's login record for `prod` names the cached session and holds no token

#### Scenario: Session the MCP no longer knows

- **WHEN** `vecten user add` runs with a cached session that the MCP answers with 401
- **THEN** the command exits 3 naming `vecten login`, the session is gone from the cache, and `next` reports `vecten login --env <env>`

### Requirement: group and grant commands

`vecten group add <group>`, `vecten group list`, `vecten grant <dataset> --group <group>`, `vecten grant --schema <schema> --group <group>` and `vecten revoke` with the same forms SHALL act through the admin API with the same effect as the panel: a group maps to one warehouse role, a dataset grant becomes `GRANT SELECT` on the dataset's source for that role, and a schema grant covers the schema's tables including future ones. Grants are recorded as desired state and reconciled by the server.

#### Scenario: Grant from the CLI

- **WHEN** `vecten grant orders --group sales` runs
- **THEN** the `sales` warehouse role holds `SELECT` on the source of `orders`
- **AND** the panel shows the grant

#### Scenario: Schema grant from the CLI

- **WHEN** `vecten grant --schema raw_hubspot --group sales` runs
- **THEN** the `sales` warehouse role can read every table in `raw_hubspot`, including one created later

### Requirement: mcp validate

`vecten mcp validate` SHALL load every document under `osi/`, merge them and validate the merged model with the same validator the `mcp` component runs at load time. It SHALL exit 0 on a valid model and non-zero naming the document and the error otherwise, with no warehouse connection required for the schema and home-dataset checks. It SHALL leave a record of the outcome under `.vecten/` for `next`.

#### Scenario: Valid model

- **WHEN** `vecten mcp validate` runs on a project whose `osi/` documents merge without error
- **THEN** the command exits 0 and reports the dataset and metric counts

#### Scenario: Duplicate name across documents

- **WHEN** `osi/datasets.json` and an extension document both define a dataset named `orders`
- **THEN** the command exits non-zero and names both documents and `orders`

### Requirement: mcp import

`vecten mcp import --dbt <path>` SHALL run the shared generator over the dbt project at `<path>`, using its manifest and catalog, and write `osi/datasets.json` into the current project. It SHALL NOT create or modify `osi/metrics.json`.

#### Scenario: Import an existing dbt project

- **WHEN** `vecten mcp import --dbt ../analytics` runs in an MCP-only project
- **THEN** `osi/datasets.json` holds one dataset per mart model of that project
- **AND** `osi/metrics.json` is unchanged

#### Scenario: Missing catalog

- **WHEN** the dbt project at `<path>` has a manifest but no catalog
- **THEN** the command exits non-zero and says to run `dbt docs generate` first

### Requirement: issues commands

`vecten issues [--env <env>] [--json]` SHALL list the environment's open issues in the shared issue shape from the three sources, using the person's cloud login for the bucket, the Airflow credential from the secrets store and the admin API token from `vecten login`, and `vecten issues show <id>` SHALL print one issue with its capped, redacted evidence. Neither SHALL change anything.

#### Scenario: List

- **WHEN** `vecten issues --env prod --json` runs
- **THEN** the output is a list of issue items, each with kind, title, resolution and evidence pointers

#### Scenario: Show

- **WHEN** `vecten issues show iss_airflow_hubspot_sync_deals_KeyError --env prod` runs
- **THEN** the output holds the run id, the error class, the log excerpt with secrets replaced, the log location and `untrusted: true`

### Requirement: mcp provisioner-script

`vecten mcp provisioner-script` SHALL print, for an MCP-only project over an existing warehouse, the SQL an admin runs there to create the roles the identity contract names with exactly their grants, followed by the `secret set` commands that store the provisioner and, on Postgres, impersonator credentials. On Snowflake, unless `--public-key` names a key the administrator keeps, it SHALL generate the provisioner's key pair into the laptop's store as `MCP_PROVISIONER_PRIVATE_KEY` once, fill its public half into the SQL, and name `secret set MCP_PROVISIONER_PRIVATE_KEY --env <env> --from-local` as the command that copies it to an environment.

#### Scenario: Script for Postgres

- **WHEN** `vecten mcp provisioner-script` runs in an MCP-only Postgres project
- **THEN** the output is SQL that creates `analyst`, `provisioner` and `impersonator` with exactly the contract's grants, sets the default privileges on the modelled schemas and the log prefix, states the `REVOKE CONNECT`s, and is followed by the two `secret set` commands

#### Scenario: Confirming the run

- **WHEN** `vecten mcp provisioner-script --confirm-run` runs after the administrator ran the script
- **THEN** it records the run and prints only that confirmation and the commands that follow, not the SQL

#### Scenario: Script for Snowflake

- **WHEN** it runs in an MCP-only Snowflake project after `init --deploy`
- **THEN** the output creates `analyst` and `provisioner` and includes `CREATE SECURITY INTEGRATION` for External OAuth with the deployment's issuer, JWKS URL and audience filled in
- **AND** the provisioner's public key in the SQL is the public half of the `MCP_PROVISIONER_PRIVATE_KEY` the run generated locally, and a second run keeps that key

### Requirement: mcp introspect

`vecten mcp introspect` SHALL connect to the existing warehouse with the admin locator from `vecten.toml`, read `information_schema` for the schemas named, and write a draft `osi/datasets.json` with one dataset per table, fields typed from the catalog and an empty description on each dataset, marked for the human, for the `semantic` skill to refine. On Postgres it SHALL sign in with the password `secret set WAREHOUSE_ADMIN_PASSWORD` stored. On Snowflake, which gives a service user no password and meets a person's password with MFA, it SHALL sign in with a key pair it generates into the laptop's store as `WAREHOUSE_ADMIN_PRIVATE_KEY` on first use, and, while Snowflake refuses that key, SHALL exit 3 printing the `ALTER USER … SET RSA_PUBLIC_KEY` statement that registers it. Neither credential SHALL leave the laptop.

#### Scenario: Snowflake introspection before the key is registered

- **WHEN** `vecten mcp introspect` runs for the first time in an MCP-only Snowflake project
- **THEN** it stores `WAREHOUSE_ADMIN_PRIVATE_KEY` locally, and when Snowflake refuses the key it exits 3 naming `ALTER USER "<login>" SET RSA_PUBLIC_KEY = '<public key>'`, with no private key material in the output

#### Scenario: Draft from an existing schema

- **WHEN** `vecten mcp introspect --schema analytics` runs against a warehouse with twelve tables in `analytics`
- **THEN** `osi/datasets.json` holds twelve datasets with typed fields and an empty description each, `next` reports the `semantic` skill to refine them, and `vecten mcp validate` reports the missing descriptions as warnings

### Requirement: extension add

`vecten extension add <package>` SHALL record the package under `extensions` in `vecten.toml`, add it to the project's requirements, and wire every entry point group it declares among `vecten.connectors`, `vecten.dbt_packages`, `vecten.dags`, `vecten.osi`, `vecten.mcp.tools`, `vecten.mcp.auth`, `vecten.mcp.provisioning`, `vecten.mcp.audit_sinks` and `vecten.skills`. A package declaring none of these groups SHALL be refused; an unknown group is reported, not refused.

#### Scenario: Extension with several parts

- **WHEN** `vecten extension add vecten-ext-entity-resolution` runs and the package declares `vecten.dbt_packages`, `vecten.dags` and `vecten.osi`
- **THEN** `vecten.toml` lists the extension, the requirements include it, and `next` reports no unwired part

#### Scenario: Package without entry points

- **WHEN** `vecten extension add requests` runs
- **THEN** the command exits non-zero and lists the nine groups an extension may declare

### Requirement: user add-admin

`vecten user add-admin <firstname>` SHALL create the warehouse account `admin_<firstname>` with full warehouse privileges, from the laptop over the environment's master credential, never through the `mcp` component: through the cloud's tunnel on AWS, and on Scaleway, which offers none, over the instance's opt-in public endpoint, refusing while `database_public_cidrs` in tfvars is empty. On an MCP-only project over an existing warehouse it SHALL refuse with a message that the admin already has an account there.

#### Scenario: Admin account on a bootstrapped warehouse

- **WHEN** `vecten user add-admin jane --env prod` runs on a project with the `platform` component
- **THEN** `admin_jane` exists in the warehouse with full privileges
- **AND** the `mcp` component has no principal named `admin_jane`

#### Scenario: Scaleway without a public endpoint

- **WHEN** `vecten user add-admin jane --env prod` runs on a Scaleway project whose `deploy/envs/prod.tfvars` leaves `database_public_cidrs` empty
- **THEN** the command exits non-zero naming `database_public_cidrs`, and nothing is created

#### Scenario: Existing warehouse

- **WHEN** `vecten user add-admin jane` runs on a project rendered with `--has-warehouse yes`
- **THEN** the command exits non-zero and says the admin already has an account on the existing warehouse
