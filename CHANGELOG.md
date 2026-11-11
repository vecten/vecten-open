# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions
follow [Semantic Versioning](https://semver.org/); in the 0.x series a minor release may break.

## [Unreleased]

- Deployed hosts: `dags_paused_at_creation = true` in `deploy/envs/<env>.tfvars` makes that
  environment's new DAGs start paused, `[airflow] allow` in `vecten.toml` limits which networks
  reach `airflow.<domain>` (Airflow's health path stays open for `vecten ship`), and
  `root_volume_size_gb` in the tfvars sizes every VM's root volume on AWS and Scaleway. Every
  default is the current behaviour.

## [0.1.0] - 2026-11-09

The first public release.

- The `vecten` CLI over two components: `platform` (dlt ingestion, Apache Airflow
  orchestration, dbt modelling to schema convention 1, an Apache Ossie semantic model) and
  `mcp` (the Model Context Protocol server over the warehouse, with one warehouse identity per
  person, an audit with receipts, usage analytics and the feedback loop).
- Deploys to AWS and Scaleway with Postgres or Snowflake, through GitHub Actions and
  Terraform; runs on a laptop with Docker.
- Five agent skills (`guide`, `connector`, `model`, `semantic`, `operate`), shipped in the
  package, vendored into every project and published as a Claude Code plugin, with their
  evaluations.
- Published as `vecten`, `vecten-platform`, `vecten-mcp` and `vecten-shared` on PyPI and as
  `ghcr.io/vecten/vecten-airflow` and `ghcr.io/vecten/vecten-mcp`.

[Unreleased]: https://github.com/vecten/vecten-open/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/vecten/vecten-open/releases/tag/v0.1.0
