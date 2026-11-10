# cli/agent-guide Specification

## Purpose
The agent-facing surface of the product: the intake `next` runs before a project exists, what `next` reports inside a project for both components and in what priority order, the shape of its items, the five skills that drive the CLI (`connector`, `model`, `semantic`, `operate` and `guide`), and how the know-how reaches the agent through the CLI, the rendered project and a published plugin.

## Requirements

### Requirement: next outside a project runs the intake

Run outside a project, `next` SHALL report ask items for the local intake, starting with `has_warehouse` ("Do you already have a data warehouse?") and ending with `name` and users. Each item SHALL name the `answer` key its answer feeds. `next` SHALL read recorded answers from `vecten-answers.yml` and report the first unanswered item. The local intake asks only what a laptop run needs; cloud, domain and GitHub are deploy questions asked inside the project.

#### Scenario: Empty directory

- **WHEN** `next --json` is run in a directory with no project
- **THEN** the first item is the `has_warehouse` question with kind `ask` and the question text verbatim

#### Scenario: Intake complete

- **WHEN** every intake answer has been recorded with `answer`
- **THEN** the only item is `init`, which reads `vecten-answers.yml`

### Requirement: Intake branches on the warehouse answer

When `has_warehouse` is no, `next` SHALL set `components` to both and ask, in order: sources to connect or start empty; `warehouse` ("Should the warehouse be Postgres, run for you, or a Snowflake account you already have?", default `postgres`); the project name (`name`); how many people and agents will use the warehouse. When it is yes, `next` SHALL set `components` to `mcp` only and ask `warehouse_url`, from which it takes `warehouse`, then the project name, then users.

#### Scenario: No warehouse yet

- **WHEN** the human answers no to `has_warehouse`
- **THEN** the next items are the sources question, then the warehouse question, then the project name question, then the users question
- **AND** after the users answer the only item is `init`

#### Scenario: Existing warehouse

- **WHEN** the human answers yes to `has_warehouse`
- **THEN** the next item asks for `warehouse_url`, the one after it asks for the project name, and the one after that asks for users
- **AND** the sources and warehouse questions are never reported

#### Scenario: Laptop trial without accounts

- **WHEN** a human with no cloud account, no domain and no GitHub account completes the local intake
- **THEN** `init` renders the project and `up` runs it on the laptop with none of those accounts

### Requirement: next asks the deploy questions after the local loop

Inside a project whose deploy layer has not been rendered, once the semantic model items are clear, `next` SHALL report the deploy questions in this order: cloud account and provider; registered domain; GitHub account. There is no cost or placement question. Once they are answered, `next` SHALL report `init --deploy` as a `command` item, and `answer` SHALL record these answers in the project's `vecten-answers.yml`.

#### Scenario: Local loop finished, no deploy answers

- **WHEN** every local item is clear and no cloud has been answered
- **THEN** the first item is the cloud question with kind `ask` and `cloud` as its answer key

#### Scenario: No cloud account

- **WHEN** the human has answered that they have no cloud account
- **THEN** the cloud item carries `estimate --size <size> --components <components> --warehouse <kind>`, with the project's own values, and `guide cloud` as what to show before the answer

#### Scenario: Human raises cost

- **WHEN** the human asks what it would cost to run cheaper
- **THEN** the agent shows the `minimal` figure from the same `estimate` and explains the isolation it gives up, and `profile` stays `standard` unless the human asks

#### Scenario: Deploy answers complete

- **WHEN** cloud, domain and GitHub are answered
- **THEN** the next item is `init --deploy` with kind `command`

#### Scenario: Deploy questions not asked early

- **WHEN** a wanted source is still unadded
- **THEN** no deploy question is reported

### Requirement: Intake items carry the guides

A "no" to the cloud, domain or GitHub question SHALL be recorded as the answer `none` (`answer cloud none`, `answer domain none`, `answer github_repository none`). A question answered `none` stays open, and `init --deploy` treats `none` as missing. Asked again, the item SHALL carry what the agent shows before asking: `estimate` with the project's `--size`, `--components` and `--warehouse`, and `guide cloud`, for the cloud, `guide domain` for the domain, `guide github` for GitHub.

#### Scenario: The item says how to answer no

- **WHEN** `next --json` reports the cloud, domain or GitHub question
- **THEN** the cloud item lists `none` among its allowed answers, and the domain and GitHub items say "Answer none if you do not have one yet."

#### Scenario: No domain

- **WHEN** the human has answered that they have no registered domain, recorded as `answer domain none`
- **THEN** the domain item is reported again and carries `guide domain` as what to show

### Requirement: next item shape

Every item `next` reports SHALL carry a kind (`command`, `skill` or `ask`), a title and a resolution: the command line, the skill name, or for an `ask` the question text with its allowed answers and the `answer` keys it feeds. An `ask` acted on by a command instead of a recorded answer, the deploy gate and the first user, SHALL carry the question as its title and the command as its resolution, with no answer key. An agent SHALL be able to act on an item without interpreting it.

#### Scenario: Ask item

- **WHEN** `next --json` reports the domain question
- **THEN** the item has kind `ask`, the question text, and `domain` and `dns_zone` as the answer keys

#### Scenario: Ask resolved by a command

- **WHEN** `next --json` reports the deploy gate
- **THEN** the item has kind `ask`, the confirmation question as its title, `deploy --env prod` as its resolution and no answer key

### Requirement: next reads files and run records only

`next [--json]` SHALL read only local files: `vecten-answers.yml`, the project files (`vecten.toml`, `connectors/`, `osi/`, the dbt project with its `sources.yml`, the names in `.env`, never their values), the local git repository, and the CLI's run records under `.vecten/`. It SHALL NOT open a warehouse connection, call the `mcp` component, or call a cloud or GitHub API.

#### Scenario: No warehouse needed

- **WHEN** `next` is run with the local stack down and no credentials
- **THEN** it reports from files and run records alone and exits 0

#### Scenario: Gaps never appear

- **WHEN** the deployed `mcp` component holds open gaps
- **THEN** `next` reports nothing about them, because gaps live in `mcp_state` and are reached through the admin tools

### Requirement: next reports ingestion state first

Inside a project with the `platform` component, `next [--json]` SHALL report ingestion items before modelling items, in this order: the local stack never started; each `wanted` source not yet added; connectors declared in `vecten.toml` with no source directory; connectors with no `access.groups` answer, as an `ask` item; sources with no recorded successful local run; sources with no recorded fixtures.

#### Scenario: Stack first

- **WHEN** a freshly rendered project has never started the stack
- **THEN** the first item is `up`, and nothing about GitHub is reported yet

#### Scenario: Stack before sources

- **WHEN** the local stack has never been started and a wanted source is unadded
- **THEN** `up` is reported before the wanted source

#### Scenario: Access asked after add

- **WHEN** `hubspot` is declared with no `access.groups` and has not been run locally
- **THEN** `next` reports an `ask` item asking which groups may read the HubSpot raw data, with the groups already declared across connectors listed and `default` offered, resolved by `vecten answer`
- **AND** the item is reported before the local run item

### Requirement: next reports modelling state in priority order

After the ingestion items, `next` SHALL report, in this order: tables declared in `sources.yml` with no staging model; staging models with no snapshot; staging models whose snapshot no core model reads; core or mart models or columns without descriptions; a failed or missing `dbt-build-local` record.

#### Scenario: Several categories outstanding

- **WHEN** a project has a declared connector with no directory and a staging model with no snapshot
- **THEN** the missing source directory is reported before the missing snapshot

#### Scenario: Fresh project

- **WHEN** `next` is run right after `init` with two wanted sources
- **THEN** the first item is `up`
- **AND** the next two are the wanted sources, each resolved by the `connector` skill

### Requirement: next reports semantic model state

After the modelling items, `next` SHALL report, in this order: `osi/datasets.json` missing or older than the last `dbt-build-local` record, resolved by `dbt-build-local`; `osi/metrics.json` holding no metric, resolved by the `semantic` skill, which a project with the `platform` component reports only once `osi/datasets.json` holds a dataset; the last `vecten mcp validate` record failed, missing or stale, resolved by the `semantic` skill. In an MCP-only project the first item is replaced by the warehouse admin credential not stored, then `osi/datasets.json` being empty, resolved by `vecten mcp introspect`, then a dataset in `osi/datasets.json` with an empty or missing description or no primary key, resolved by the `semantic` skill.

#### Scenario: Stale validation

- **WHEN** `vecten mcp validate` passed and a file under `osi/` changed afterwards
- **THEN** `next` reports the validation item again, because the `mcp-validate` record under `.vecten/` holds a digest of `osi/` that no longer matches

#### Scenario: Stale datasets

- **WHEN** a mart changed and `dbt-build-local` passed but `osi/datasets.json` predates that record
- **THEN** `next` reports the stale file with `dbt-build-local` as the resolution

#### Scenario: No metrics

- **WHEN** `osi/datasets.json` is fresh and `osi/metrics.json` holds no metric
- **THEN** `next` reports the missing metrics with the `semantic` skill as the resolution

#### Scenario: Platform started empty

- **WHEN** a project with the `platform` component has no source, so `osi/datasets.json` holds no dataset, and `osi/metrics.json` holds no metric
- **THEN** `next` does not report the missing metrics, so after validation the loop reaches the deploy questions

#### Scenario: Existing warehouse with no datasets

- **WHEN** an MCP-only Postgres project has no warehouse admin credential stored and an empty `osi/datasets.json`
- **THEN** `next` reports `secret set WAREHOUSE_ADMIN_PASSWORD` as a `command` item, then `vecten mcp introspect` as a `command` item, then the `semantic` skill to refine the draft

#### Scenario: Existing Snowflake warehouse with no datasets

- **WHEN** an MCP-only Snowflake project has an empty `osi/datasets.json`
- **THEN** `next` reports `vecten mcp introspect` as a `command` item with no `secret set` item before it, and asks for no local provisioner key before the provisioner script, which generates its own

#### Scenario: Drafted datasets not yet refined

- **WHEN** `vecten mcp introspect` has written twelve datasets to `osi/datasets.json` and the `semantic` skill has given eleven of them a description and a primary key
- **THEN** `next` reports the `semantic` skill to refine the drafted datasets, before the metrics item
- **AND** once the twelfth has both, `next` no longer reports it

#### Scenario: Existing warehouse not yet prepared

- **WHEN** an MCP-only project has no record that the provisioner script was run and its credentials stored
- **THEN** `next` reports in the local loop, before the deploy questions, a `command` item printing `vecten mcp provisioner-script` for the admin to run
- **AND** reports the `secret set <name> --env <env>` calls for the provisioner credentials with the deploy prerequisites, after `bootstrap --env <env>` and before `github push`, because the cloud secrets store exists only after bootstrap
- **AND** records both under `[environments.<env>]` once done

#### Scenario: Local loop done

- **WHEN** every source is run, recorded, staged, snapshotted, modelled and described, the datasets are fresh, metrics exist, validation passed, and no deploy question is answered
- **THEN** the first item is the cloud question
- **AND** no deploy ask is reported yet

### Requirement: next reports deploy prerequisites

After the deploy questions and `init --deploy`, while the project is not deployed, `next` SHALL report in this order: the project not on GitHub, resolved by `github init`; `prod` not bootstrapped, resolved by `bootstrap --env prod`; in an MCP-only project, the provisioner credentials, resolved by `secret set <name> --env prod`; each secret in `.env` not yet copied to `prod`, resolved by `secret set <name> --env prod --from-local`; changes not pushed to GitHub, resolved by `github push`.

#### Scenario: GitHub after the deploy layer

- **WHEN** `init --deploy` has rendered the deploy layer and the project is not on GitHub
- **THEN** the first item is `github init`, before `bootstrap --env prod`

#### Scenario: Secret not in the cloud

- **WHEN** `HUBSPOT_API_KEY` is in `.env` and has not been copied to `prod`
- **THEN** `next` reports it with `secret set HUBSPOT_API_KEY --env prod --from-local` as the resolution

#### Scenario: Local stack passwords stay local

- **WHEN** `.env` holds the passwords `up` generated for the local stack
- **THEN** `next` reports no item copying them to `prod`, because the cloud environment generates its own

### Requirement: The deploy is gated by an ask item

Only when every item above is clear and the project is not deployed, `next` SHALL report a single ask item to confirm the deploy to `prod`, stating that it creates billable resources, resolved by `deploy --env prod` after the human's yes.

#### Scenario: Deploy not offered early

- **WHEN** a wanted source is still unadded
- **THEN** no deploy item is reported

### Requirement: next reports a push after deploy

After a successful deploy, `next` SHALL report `github push` before any other item whenever the local repository has changes not pushed to GitHub, because an answer recorded after the deploy changes `vecten.toml` and the platform reads it from GitHub. Changes to `[mcp]` reach the MCP when the ship that the push starts has finished.

#### Scenario: Answer recorded after the deploy

- **WHEN** the deploy has succeeded and `answer mcp.auth password` has changed `vecten.toml`
- **THEN** the first item is `github push`, before the admin item

### Requirement: next reports MCP items after deploy

After a successful deploy of a project with the `mcp` component, `next` SHALL report, once each and in this order: the OIDC configuration `ask` unless password is opted in; the first admin's email `ask` unless `[mcp] admins` names one; the sign-in items below; the first user `ask`; under password sign-in, a `command` `vecten user password-link <id> --env <env>` per person `vecten user add` added whose link was not issued, naming the person's email; a `command` per connector whose access is not applied; the MCP URL and connection `command`; the alert webhook `ask`. The OIDC, admin, callback registration and webhook asks SHALL name the `answer` keys they feed. Once all are recorded, `next` SHALL report nothing.

The sign-in items SHALL be reported only while an admin call remains to be made (the first user, a password link, or a connector's access) and `[mcp] admins` names someone. Before the first user is added: with OIDC configured, an `ask` to register `https://mcp.<domain>/oidc/callback` with the identity provider and store any client secret with `vecten secret set MCP_OIDC_CLIENT_SECRET --env <env>`, feeding `answer oidc_registered.<env> yes`, until it is recorded; in password mode, the `command` `vecten mcp admin-link --env <env>` until a link was issued. Then, once sign-in is set up, the `command` `vecten login --env <env>` while this machine holds no usable MCP session for the environment. `next` SHALL read the session from the local token cache and the project's login record only, and a cache it cannot read SHALL count as no session. A cached session SHALL count only when the project's login record names it as the one `vecten login` saved from this project for the environment, no token refresh is left unfinished, and its access token is unexpired or it holds a refresh token.

#### Scenario: OIDC callback registration

- **WHEN** the deploy has succeeded, the OIDC issuer and client id and an admin are configured, and the callback registration is not recorded
- **THEN** the first item asks the human to register `https://mcp.<domain>/oidc/callback` and store any client secret, feeding `answer oidc_registered.<env> yes`
- **AND** once it is recorded, the next item is `vecten login --env <env>`, before the first user

#### Scenario: Password mode

- **WHEN** `[mcp] auth = "password"`, an admin is configured and no admin link was issued for the environment
- **THEN** the first item is `vecten mcp admin-link --env <env>`, and once a link was issued, `vecten login --env <env>`

#### Scenario: Signed in

- **WHEN** this machine's token cache holds the session `vecten login --env <env>` saved from this project for `https://mcp.<domain>`, and its access token is unexpired
- **THEN** `next` reports no sign-in item

#### Scenario: Session left by another project

- **WHEN** the token cache holds a session for `https://mcp.<domain>` that `vecten login` saved from another project, and this project has no login record for the environment
- **THEN** `next` reports `vecten login --env <env>` before the first user

#### Scenario: Session that can no longer refresh

- **WHEN** the project's session has an expired access token and either no refresh token or a refresh left unfinished
- **THEN** `next` reports `vecten login --env <env>`

#### Scenario: Password link for an added person

- **WHEN** `[mcp] auth = "password"` and `vecten user add --email grace@acme.test --name "Grace Hopper" --env prod` has added a person
- **THEN** the next item is `vecten user password-link <the principal id add returned> --env prod`, titled with `grace@acme.test`
- **AND** once that command has issued the link, `next` no longer reports it

#### Scenario: OIDC and the first admin

- **WHEN** the deploy has succeeded and neither OIDC nor an admin is configured
- **THEN** the OIDC item feeds `answer mcp.oidc_issuer <url>` and `answer mcp.oidc_client_id <id>` and names `answer mcp.auth password` as the way to opt out
- **AND** the admin item after it feeds `answer mcp.admins <email[,email]>`

#### Scenario: Alert webhook

- **WHEN** the connection item has been shown and the alert webhook was not asked for the environment
- **THEN** `next` reports an `ask` feeding `answer alert_webhook.<env> yes|no`, whose title says to run `secret set ALERT_WEBHOOK_URL --env <env>` first on yes

#### Scenario: First user

- **WHEN** the deploy has succeeded, OIDC is configured and its callback registered, an admin is seeded, this machine holds a session, and `first_user_added` is not set for the environment
- **THEN** the first item asks who should have access, with `vecten user add --email <email> --name "<name>" --env <env>` as the resolution

#### Scenario: Connector access applied

- **WHEN** a user has been added and `hubspot` declares `access.groups = ["sales"]` with no apply record under `.vecten/`
- **THEN** `next` reports a `command` item whose resolution creates `sales` if needed and runs `vecten grant --schema raw_hubspot --group sales`
- **AND** the item disappears once `vecten.toml` records the access as applied for the environment

#### Scenario: Connection instructions

- **WHEN** a user has been added and the connection item has not been shown
- **THEN** `next` reports `mcp connection --env <env>` as a `command` item whose output is the MCP URL and the steps to connect a client
- **AND** `mcp connection` records the item shown only for an environment that has deployed; for any other it prints the URL and says it recorded nothing

#### Scenario: Nothing left

- **WHEN** the deploy has succeeded, a user is provisioned and, under password sign-in, given a password link, every connector's access is applied, the connection item was shown, the alert webhook was answered and every change is pushed
- **THEN** `next --json` returns an empty item list and exit code 0

### Requirement: Each next item names its resolution

Every item `next` reports SHALL carry the CLI command or the skill name that resolves it, so an agent can dispatch without interpreting the item.

#### Scenario: Unrun source

- **WHEN** `next --json` reports a source with no successful local run
- **THEN** the item names `connector run <source>` as the resolution

#### Scenario: Unmodelled source table

- **WHEN** `next --json` reports a table declared in `sources.yml` with no staging model
- **THEN** the item names the `model` skill as the resolution

### Requirement: connector skill

The `connector` skill SHALL walk the user through setting up a source, a wanted one from the intake or a new one the human names: credentials through `secret set`, a pass through the API documentation and endpoints with the user, the choice of dlt path, `connector add`, the source implementation, `connector run --record`, and a `raw schema` check before finishing.

#### Scenario: Wanted source

- **WHEN** the skill is dispatched for a wanted source from the intake
- **THEN** it ends with the source declared in `vecten.toml`, recorded fixtures, and tables in `raw_<source>`

#### Scenario: API with no verified source

- **WHEN** the skill is given an API that dlt has no verified source for
- **THEN** it asks for the credentials and stores them with `secret set`, goes through the documented endpoints with the user, scaffolds a `rest` source, fills in pagination and incrementality, runs it locally with `--record` and confirms tables in `raw_<source>` through `raw schema`

#### Scenario: Credentials never in files

- **WHEN** the transcript of a `connector` skill run is inspected
- **THEN** every credential went through `secret set` and none appears in `connectors/` or `vecten.toml`

### Requirement: model skill

The `model` skill SHALL read `raw schema --json`, write staging, snapshot and core models in the convention with descriptions and tests, run `dbt-build-local`, and SHALL NOT stop until the convention tests pass and `osi/datasets.json` is regenerated.

#### Scenario: One raw source to model

- **WHEN** the skill runs against a source with tables in `raw_<source>` and no models
- **THEN** staging, snapshot and core models exist for the source, with descriptions and primary key tests
- **AND** `dbt-build-local` passes including the convention tests and leaves a fresh `osi/datasets.json`

### Requirement: model skill reads dbt through the dbt MCP server

The `model` skill SHALL use the local tools of the dbt MCP server, pinned in the versions table, for lineage, node details and the dbt CLI tools (`parse`, `compile`, `list`, `show`, `docs`), and, when the server offers them, the codegen tools, over the manifest `dbt-build-local` leaves behind.

#### Scenario: Lineage from the local manifest

- **WHEN** the skill needs a model's lineage or details
- **THEN** it calls the dbt MCP server's `get_lineage_dev` or `get_node_details_dev` over the local manifest, never a tool that needs dbt Platform credentials

### Requirement: semantic skill

The `semantic` skill SHALL own `osi/metrics.json` and, in an MCP-only project without dbt, `osi/datasets.json`: it drafts datasets from `describe` over `information_schema`, writes and edits metrics, reads gaps through the admin tools, decides whether a fix is a metric edit, a mart change handed to `model`, or connector work, runs `vecten mcp validate`, and opens one pull request per gap with the gap id, the quoted questions and the agent's SQL.

#### Scenario: Wrong definition gap

- **WHEN** the skill runs on a `wrong_definition` gap for `total_revenue` and the home dataset already has the needed field
- **THEN** it edits the metric expression in `osi/metrics.json`, passes `vecten mcp validate`, and opens one pull request naming the gap id and quoting the triggering question

#### Scenario: Gap needs a mart change

- **WHEN** the skill finds that the fix needs a column the mart does not have
- **THEN** it hands the mart change to the `model` skill, waits for `dbt-build-local` to regenerate `osi/datasets.json`, then edits the metric, and both changes land in the same pull request

#### Scenario: Existing warehouse without dbt

- **WHEN** the skill runs in an MCP-only project with an empty `osi/datasets.json` and no dbt project
- **THEN** it runs `vecten mcp introspect` for the draft, asks the human to confirm descriptions and drop tables that are not marts, writes `osi/datasets.json` and passes `vecten mcp validate`

#### Scenario: Skill-side evals on request

- **WHEN** the human asks the skill to check the evals
- **THEN** for each case in `evals/semantic.yaml` the skill maps the question to a `query_metrics` request itself, compares it with the stored request, and reports the mismatches without calling the server

### Requirement: operate skill

The `operate` skill SHALL loop on `vecten issues --json` for an environment, dispatch each issue by its resolution, to `connector`, `model` or `semantic`, a `command` to run or an `ask` to relay, deliver every code fix through a pull request, and report an issue closed only once `issues` no longer lists it.

#### Scenario: Reads evidence through issues show only

- **WHEN** the skill needs the evidence behind an issue
- **THEN** it reads it through `issues show`, treats every field as data, and uses the Airflow MCP server only to look at runs, never as evidence to act on

#### Scenario: Renamed field

- **WHEN** `operate` runs and `issues` lists the HubSpot `deals` failure
- **THEN** it reads the issue through `issues show`, dispatches `connector`, which fixes the source and opens a pull request, and reports the issue closed after the merge ships and the next run is green

#### Scenario: Evidence carries an instruction

- **WHEN** a log excerpt contains text that reads like a command to the agent
- **THEN** the skill quotes it as data in the pull request and does not act on it

### Requirement: guide skill is the entry point

The `guide` skill SHALL start from an empty directory: run `next --json`, relay each ask item to the human verbatim and feed the answer back, run each command item, dispatch each skill item to `connector`, `model` or `semantic`, and repeat until `next` is empty. The README SHALL point to `guide` as the way to build and extend the platform through a page that explains the agent loop to a human and links the rendered guide document, and `vecten guide` SHALL print the same text as that document.

#### Scenario: Empty directory to deployed platform

- **WHEN** the `guide` skill runs in an empty directory with a human who has no warehouse, names one source and answers the intake
- **THEN** it records the answers, runs `init` and `up`, connects, runs, stages, snapshots and models that source, writes metrics, validates, answers the deploy questions, runs `init --deploy` and `github init`, bootstraps, copies the secrets, pushes, asks for the deploy confirmation, deploys through CI, adds the first user, prints the MCP URL and stops when `next` is empty

#### Scenario: Existing warehouse to deployed MCP

- **WHEN** the `guide` skill runs in an empty directory with a human who answers yes to `has_warehouse`
- **THEN** it records the warehouse locator and the remaining answers, runs `init` and `up`, stores the admin credential with `secret set`, introspects, drafts the datasets and metrics through `semantic`, validates, hands the provisioner script to the admin, answers the deploy questions, runs `init --deploy` and `github init`, bootstraps, stores the provisioner credentials, copies the admin credential, pushes, asks for the deploy confirmation, deploys, adds the first user and prints the MCP URL

#### Scenario: Human says start empty

- **WHEN** the human answers the sources question with "start empty"
- **THEN** after `init` and `up` the loop goes to the semantic model items, the deploy questions, the deploy prerequisites and the confirmation

#### Scenario: A human reads the README

- **WHEN** a person opens the repository's README
- **THEN** it links a page that explains the agent loop in prose for a human, and that page links the rendered guide document that holds the skill text the agent follows

### Requirement: guide reads Airflow through the Astronomer MCP server

Once `up` has started the stack, the `guide` skill SHALL use the Astronomer Airflow MCP server, pinned in the versions table and pointed at the loopback Airflow API address and user that `up` prints, with the password from `.env` that the human exports, to inspect DAG runs and logs.

#### Scenario: DAG inspection after up

- **WHEN** a connector's sync DAG has run on the local stack
- **THEN** the skill reads the run state and task logs through the Airflow MCP server rather than through the Airflow UI

### Requirement: Skills drive the CLI through the shell

Each skill SHALL act on the project through the CLI's `--json` commands run in a shell. The product MUST NOT provide an MCP server or other wrapper around the CLI for agent use; the `mcp` component serves data, never the CLI. Skills MAY use the third-party MCP servers of Airflow and dbt named in the versions table for reading state those tools own.

#### Scenario: Skill actions

- **WHEN** the transcript of a skill run is inspected
- **THEN** every state read and state change goes through a `vecten` command
- **AND** no file that the CLI owns is edited directly by the skill

### Requirement: Skills vendored and published

The five skills, `connector`, `model`, `semantic`, `operate` and `guide`, SHALL be vendored into every rendered project under `skills/` and SHALL also be published as a Claude Code plugin. The vendored copies, the plugin, the guide document in the repository's `docs/` and the text `vecten guide` prints SHALL all be rendered from the same files in the package, so they carry the same skill text at any release. The README SHALL NOT embed the skill text.

#### Scenario: Project without the plugin

- **WHEN** an agent works in a rendered project with the plugin not installed
- **THEN** the skills under `skills/` are usable from the project alone

#### Scenario: Plugin install

- **WHEN** the plugin is installed into Claude Code
- **THEN** `connector`, `model`, `guide` and `semantic` are available as skills

#### Scenario: Only the CLI

- **WHEN** an agent has the CLI installed and neither the plugin nor a project
- **THEN** `vecten guide` gives it the same entry skill text the plugin carries

#### Scenario: MCP servers come with the project

- **WHEN** the plugin's files are listed
- **THEN** they hold the skills and the manifest and no `.mcp.json`; the Airflow and dbt MCP servers come with a platform project's `.mcp.json`

#### Scenario: Guide document drifts from the skill

- **WHEN** the repository's guide document differs from the package's guide skill text and the render check runs
- **THEN** the check names the guide document as out of sync and exits non-zero, and the render command rewrites it from the skill

#### Scenario: Guide document absent

- **WHEN** the render command runs in a checkout whose `docs/` has no guide document
- **THEN** it writes the document from the skill and reports it as changed
