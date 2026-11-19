## Why

A deployed environment has three defaults that an operator cannot change without editing generated files:

- Every new DAG starts unpaused, so the first ship can start DAGs that write to other systems or call a metered API before anyone has looked at them (issue #48).
- The Airflow sign-in page at `airflow.<domain>` is open to the whole internet. `/admin` on the MCP host already has an allow-list (`[mcp] admin_allow`), but Airflow has none (issue #54, item 1).
- Every VM's root volume is a fixed 30 GB, and it holds the images and Docker's named volumes (issue #54, item 2).

## What Changes

- `dags_paused_at_creation` in `deploy/envs/<env>.tfvars` (optional, `false` by default) makes that environment's new DAGs start paused. The laptop keeps starting DAGs paused whatever the setting says. `ship` reads the setting when it renders the host's Compose file.
- `[airflow] allow` in `vecten.toml` (optional, empty by default) lists the IPv4 or IPv6 networks that may reach a deployed `airflow.<domain>`. It is validated and normalised like `[mcp] admin_allow`. NGINX answers every other address with 403, except on Airflow's health path, which `ship` polls.
- `root_volume_size_gb` in `deploy/envs/<env>.tfvars` (optional, 30 by default) sizes every VM's root volume on AWS and Scaleway.
- When the Airflow login answers 403, `vecten issues --env <env>` says that the address may be outside `[airflow] allow`.

A DAG can also override the environment's setting for itself. That per-DAG override is a separate change to the DAG builders in `vecten-platform`.

Nothing changes for an existing project: each setting's default is the current behaviour.

## Impact

- Specs: `platform/platform-stack`, `shared/deploy`.
- Code: `vecten_shared.config`, `vecten.stack.compose`, `vecten.stack.nginx`, `vecten.deploy.bundle`, `vecten.deploy.tfvars`, `vecten.issues.airflow`, and the scaffolded Terraform root.
- Terraform: `terraform/aws` and `terraform/scaleway`, their `modules/host`, and their `tests/sizing.tftest.hcl`.
- Docs: `docs/orchestration.md`, `docs/operations.md`, `docs/admin-panel.md`, the rendered runbook, and the `connector` skill.
