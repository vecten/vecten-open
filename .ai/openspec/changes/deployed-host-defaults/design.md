## Context

A deployed host runs the Compose file and NGINX configuration that `ship` renders into the host's bundle (`vecten.deploy.bundle`). It renders them from `vecten.toml`, the environment's Terraform outputs and `deploy/envs/<env>.tfvars`, which `check_tfvars` already reads, for example to size the platform VM from `platform_size`. The VM agent (`deploy/vm/apply.py`, Python 3.9 standard library only, with a byte-identical copy in the Scaleway host module) only extracts the bundle and starts it. It never renders configuration.

`vecten.toml` has two kinds of content. The sections a person writes (`[deploy]`, `[mcp]`, `[warehouse]`) apply to the whole project. `[environments.<env>]` (`EnvironmentFacts`) holds the facts the CLI records about an environment, such as `bootstrapped`, `deployed_commit` and `cloud_ids`. The `cli/scaffold` and `shared/deploy` specs define it that way: facts a second clone must see.

The settings a person chooses for one environment live in `deploy/envs/<env>.tfvars`: `size`, `platform_size`, `instance_type_overrides`, `database_public_cidrs` and `domain`. The runbook's "Choose before the first deploy" lists them, and a new environment starts as a copy of `prod.tfvars`.

## Goals / Non-Goals

**Goals:**

- A per-environment choice to start new DAGs paused, off by default, so that existing projects see no change.
- An allow-list for the deployed Airflow hostname that is validated and rendered the way `[mcp] admin_allow` is.
- A root-volume size per environment on both clouds, 30 by default.

**Non-Goals:**

- A per-DAG pause override. A separate change adds it to the DAG builders in `vecten-platform` (`sync/airflow_dag.py`, `dbt/airflow_dag.py`).
- Host monitoring and a fields-only alert mode (#54 items 3 and 4).
- Unpausing DAGs from the CLI or the deploy workflow.

## Decisions

### `dags_paused_at_creation` lives in the environment's tfvars

The alternatives were a field on `[environments.<env>]` in `vecten.toml`, or an `[airflow]` key that applies to every environment.

- A project-wide key cannot express the cases the issue describes, such as staging scheduling at once while prod waits for someone to unpause, so the setting must be per environment.
- `[environments.<env>]` is per environment, but it is the CLI's record. `save_config` rewrites that table, and the specs describe it as facts the CLI records. A person's choice stored there would be the only hand-written value in a table the tools own.
- The tfvars file already holds every per-environment choice, and `ship` already reads it through `check_tfvars` when it builds a bundle (`platform_size` reaches `host.json` this way). The new value takes the same path and needs no new file and no new reader.

`check_tfvars` refuses any value other than `true` or `false`, because `ship` reads the file directly, without Terraform's type check. The scaffolded cloud root declares the variable, as a nullable `bool`, but does not pass it to the module. Terraform therefore type-checks it in a plan and prints no undeclared-variable warning, and the module stays free of a value it has no use for. The Snowflake root, which reads the same file, already ignores keys it does not declare.

### The setting is rendered into the bundle, not passed through `host.json`

`generate_compose` takes `dags_paused_at_creation` and writes the literal value into `AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION` in the Compose file of a VM bundle. The laptop's file always says `True`. Writing the literal leaves the VM agent's `HOST_KEYS` contract and the Python 3.9 agent unchanged. A rollback also restores the setting that was in force with the release it rolls back to.

Airflow reads the variable only when it first sees a DAG. Turning the setting on later leaves every existing DAG's paused state as it is. The docs say so.

### `[airflow] allow` mirrors `[mcp] admin_allow`

- **Validation.** One function, `allow_networks`, now serves both lists: `ipaddress.ip_network(strict=True)`, normalised to CIDR notation, because each entry becomes an NGINX `allow` line. Setting `allow` without the `platform` component is refused, because no host would serve Airflow.
- **Scope.** The list is project-wide, like `admin_allow`. A per-environment list was not asked for, and the office networks it holds are usually the same for every environment.
- **Rendering.** NGINX gets `allow <net>; … deny all;` in `location /` of the Airflow TLS server, from the same helper that renders the `/admin` location. With no list, the server block is unchanged, byte for byte.
- **Deployed hosts only.** `up` does not pass the list. The laptop's NGINX listens on loopback, and the client address it sees is Docker's bridge gateway, so any real list would lock the person out of their own stack.

### Callers of `airflow.<domain>` were checked before restricting it

- **`ship`'s health poll** (`deploy/shipping.py`) fetches `https://airflow.<domain>/api/v2/monitor/health` from wherever `ship` runs, usually a GitHub-hosted runner whose address no list can name. Restricting it would make every ship fail its poll and roll back. The health path therefore gets an exact-match `location = /api/v2/monitor/health` outside the list. Airflow already serves that path without a login, and it reports component states only (`platform-stack`, "Airflow's own login guards the Airflow host").
- **The VM agent** never calls Airflow. It judges health by the containers' Docker health checks, which call `localhost:8080` inside the container.
- **Airflow's tasks** reach the API server at `http://airflow-api-server:8080/execution/` on the Compose network, not through NGINX.
- **`vecten issues --env <env>`** signs in to `https://airflow.<domain>` from the person's machine. It is the one caller the list blocks on purpose. Its login error now says that a 403 may come from `[airflow] allow`.
- **certbot** answers the HTTP-01 challenge on port 80, which the list does not touch.

### `root_volume_size_gb`, plumbed like `platform_size`

The value travels from the tfvars to the thin root's nullable variable, then to the module's variable (`default = 30`, `nullable = false`, validated), then to `modules/host`, where it sets `root_block_device.volume_size` on AWS and `root_volume.size_in_gb` on Scaleway. The Scaleway host module had the same fixed 30. The unit is in the name, as with Scaleway's `volume_size_gb`.

The minimum is 30, because neither EBS nor SBS shrinks a volume, so a smaller value could not apply to a VM created with the default. AWS also caps the value at gp3's 16384 GiB. On Scaleway a DEV1 type boots from its local SSD, so the type's local storage caps the size. The module does not hard-code those per-type limits, and the runbook names them.

## Risks / Trade-offs

- [The setting lives in a Terraform variables file but Terraform does not read it] → The root declares it with a comment saying who reads it. `check_tfvars` validates it at every ship.
- [A ship from outside the list] → The health path stays open, so `ship` and the deploy workflow keep working. Only `vecten issues --env` needs a listed network, and the docs and the error say so.
- [A changed root-volume size on a running VM] → EBS grows a volume in place, but the guest file system sees the space only after it is grown, which cloud-init does at boot. On Scaleway, whether a changed size applies in place or replaces the server is not proven. The runbook says both and advises choosing the size before the first deploy. A real deploy is needed to prove either.
- [The default stays unpaused] → Opt-in by the maintainer's decision. No deployed step unpauses a DAG, so pausing by default would leave every deployed DAG idle until someone opens the Airflow UI.

## Migration Plan

None is needed. Every default is the current behaviour. A project picks up the new root variables with `vecten update`, which renders the root and moves the module ref together.
