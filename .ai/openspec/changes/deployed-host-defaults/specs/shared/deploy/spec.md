## ADDED Requirements

### Requirement: Root volume size per environment

Every VM's root volume SHALL be sized by `root_volume_size_gb` in the environment's tfvars: GiB on AWS, where the volume is an encrypted gp3 volume, and GB on Scaleway. The setting SHALL be optional, with a default of 30. Both cloud modules SHALL refuse a value below 30 or one that is not a whole number, because neither EBS nor SBS shrinks a volume. The AWS module SHALL also refuse a value above 16384, the gp3 maximum. The thin root SHALL pass the tfvars value to the module, and a missing value SHALL leave the module's default.

#### Scenario: Default

- **WHEN** an environment whose tfvars do not set `root_volume_size_gb` is deployed
- **THEN** every VM's root volume is 30 GB

#### Scenario: Larger volume

- **WHEN** `deploy/envs/prod.tfvars` sets `root_volume_size_gb = 80` and `deploy --env prod` runs on AWS
- **THEN** the plan sets every host's root volume to 80 GiB, still encrypted gp3

#### Scenario: Too small

- **WHEN** the tfvars set `root_volume_size_gb = 20`
- **THEN** the plan fails on the variable's validation and nothing is applied

## MODIFIED Requirements

### Requirement: Per-environment configuration and state

Each environment SHALL have its own `deploy/envs/<env>.tfvars`, carrying domain and DNS zone, region, GitHub repository, components and profile, and the Snowflake account for Snowflake projects, and its own Terraform state in a per-environment bucket in the chosen cloud's object storage. The same file SHALL carry the environment's optional settings, among them `platform_size`, `root_volume_size_gb` and `dags_paused_at_creation`. `ship` SHALL read the last of these, and Terraform does not use it. On Scaleway that bucket SHALL sit in a state project of its own, apart from the environment's resources project, so that no VM key, which reaches the resources project only, can reach the state; `bootstrap` SHALL claim each project by a marker in its description and refuse a project of the expected name that carries another. `prod` is filled from the deploy answers by `init` or `init --deploy`; other environments copy it.

#### Scenario: Prod from the deploy answers

- **WHEN** the deploy layer has been rendered by `init` or `init --deploy`
- **THEN** `deploy/envs/prod.tfvars` is complete and `deploy --env prod` needs no edit to it

#### Scenario: Optional settings differ between environments

- **WHEN** `deploy/envs/staging.tfvars` sets `dags_paused_at_creation = true` and `root_volume_size_gb = 60`, and `deploy/envs/prod.tfvars` sets neither
- **THEN** staging's DAGs start paused on 60 GB root volumes, while prod's start unpaused on 30 GB volumes
