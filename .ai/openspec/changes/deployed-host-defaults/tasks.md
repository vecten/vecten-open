## 1. Paused at creation, per environment

- [x] 1.1 `check_tfvars` accepts an optional `dags_paused_at_creation` and refuses any value but `true` or `false`
- [x] 1.2 `generate_compose` takes `dags_paused_at_creation`; the laptop's stack always says `True`, and a VM's stack follows the setting
- [x] 1.3 The bundle passes the environment's tfvars value when it renders the host's Compose file
- [x] 1.4 The scaffolded cloud root declares `dags_paused_at_creation` (nullable `bool`) without passing it to the module
- [x] 1.5 Tests: tfvars validation, the Compose environment per mode and setting, the shipped Compose file with and without the opt-in, the rendered root

## 2. Airflow allow-list

- [x] 2.1 `[airflow] allow` in `vecten.toml`, validated and normalised by the same function as `[mcp] admin_allow`; refused without the `platform` component
- [x] 2.2 NGINX renders `allow … deny all` on `location /` of the Airflow TLS server, keeps `location = /api/v2/monitor/health` open to reads that it forwards as exactly that path, and leaves the server unchanged when the list is empty
- [x] 2.3 The bundle passes the list for deployed hosts; `up` does not pass it
- [x] 2.4 `vecten issues --env` names the allow-list when the Airflow login answers 403
- [x] 2.5 Tests: config validation and round trip, NGINX rendering with and without the list, the shipped NGINX file, the issues client's 403 message, and a Docker-tier test of the 403 and the open health path

## 3. Root volume size

- [x] 3.1 `root_volume_size_gb` on the AWS and Scaleway modules (30 by default, whole number, at least 30; at most 16384 on AWS), passed to `modules/host`
- [x] 3.2 The scaffolded cloud root declares it nullable and passes it to the module
- [x] 3.3 `terraform test`: every host follows the variable, null keeps 30, and values below 30 are refused, on both clouds
- [x] 3.4 The static test of the rendered root counts it among the optional variables

## 4. Docs

- [x] 4.1 `docs/orchestration.md`: a section on deployed Airflow, covering the allow-list and paused at creation
- [x] 4.2 `docs/operations.md` and `docs/admin-panel.md`: the `issues --env` caveat and a cross-reference
- [x] 4.3 Runbook template: both clouds' "Choose before the first deploy" lists and the Airflow login paragraph
- [x] 4.4 `connector` skill: the deployed pause behaviour, with the plugin re-rendered
- [x] 4.5 `CHANGELOG.md` under `[Unreleased]`

## 5. Proven only by a real deploy

- [ ] 5.1 On AWS, a changed `root_volume_size_gb` grows the running VM's volume in place, and the guest file system takes the space after a reboot
- [ ] 5.2 On Scaleway, a changed `root_volume_size_gb` on a DEV1 server and on a POP2 server: applied in place or by replacing the server
- [ ] 5.3 A ship from a GitHub-hosted runner passes its health poll while `[airflow] allow` excludes the runner
