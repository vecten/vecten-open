<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/vecten-open-dark.svg">
    <img src="docs/images/vecten-open-light.svg" width="320" alt="Vecten">
  </picture>
</p>

<p align="center">
  <a href="https://pypi.org/project/vecten/"><img src="https://img.shields.io/pypi/v/vecten" alt="PyPI"></a>
  <a href="https://github.com/vecten/vecten-open/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/vecten/vecten-open/ci.yml?branch=main&label=CI" alt="CI"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.14-blue" alt="Python 3.14"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-green" alt="Apache-2.0"></a>
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/status-alpha-orange" alt="alpha"></a>
</p>

# Vecten Open

**A secure hub for all of your company's data, served over MCP to the agentic interface you
already use.** Ask Claude a question and get an answer from your own warehouse, as yourself,
with a receipt that proves how it was answered. Runs on your own cloud.

Vecten Open is two open-source pieces behind one CLI, `vecten`. The **platform** builds the
warehouse: dlt ingestion, Apache Airflow orchestration, dbt models to one schema convention,
on Postgres or Snowflake, deployed to AWS or Scaleway with Terraform. The **MCP** serves it to
agents over the Model Context Protocol, the open protocol assistants such as Claude use to reach
data, with one warehouse identity per person and an audit of every answer. Both work on an
existing warehouse too. An agent does the building; you lead it.

## Five promises

1. **Open source.** Apache-2.0. No licence key, no telemetry. Clone it, fork it, sell it.
2. **Malleable.** We ship the starting point, not the finished thing. Three answers (cloud,
   warehouse, name) give you a running platform; then it is yours to reshape, with nothing you
   may not touch. Whoever needs a new source or metric asks an agent and gets a pull request.
   → [Agentic flow and malleable software](docs/agentic-flow.md)
3. **A data platform.** All of your data in one place, one definition of each number, every
   source modelled the same way. → [Warehouse](docs/warehouse.md) · [Connectors](docs/connectors.md) · [Orchestration](docs/orchestration.md)
4. **For the agentic workplace.** Your agent is the interface: setup, extension, use and
   maintenance all go through `vecten` commands with a JSON contract and `vecten next`, the loop
   that says what the project needs now. Five shipped skills carry the know-how, and their
   [evaluations](docs/skill-evaluations.md) are public. → [The CLI](docs/cli.md)
5. **Secure by the warehouse's own rules.** Every query runs as the person asking, under their
   own warehouse identity; every answer carries a receipt; a wrong answer becomes a tracked gap
   and the gap becomes a fix. Admins see who asked what and which questions failed.
   → [The MCP](docs/mcp.md)

## Already have a data warehouse?

Then you may need only the MCP. If you run Snowflake or Postgres and your chat assistants and
agents cannot reach that data yet, the MCP serves it to them on its own, without the platform
half, and with the same per-person identity, audit and metric definitions:

    uv tool install vecten
    vecten init warehouse-assistant --name warehouse-assistant --users 5 \
      --has-warehouse yes --warehouse-url postgres://warehouse.example.com/analytics
    cd warehouse-assistant && vecten next

[The MCP on your own warehouse](packages/vecten-mcp/README.md) walks through it. Support for
Databricks, BigQuery and Redshift is planned.

## Documentation

- Start here: [the data warehouse](docs/warehouse.md) · [connectors](docs/connectors.md) · [orchestration](docs/orchestration.md) · [the MCP](docs/mcp.md) · [the `vecten` CLI](docs/cli.md) · [agentic flow and malleable software](docs/agentic-flow.md) · [why Vecten Open, and when not](docs/why-vecten.md)
- Run it: [deploying to AWS](docs/deploy-aws.md) · [deploying to Scaleway](docs/deploy-scaleway.md) · [operating a running platform](docs/operations.md) · [admin panel and usage analytics](docs/admin-panel.md)
- Build on it: [authoring the semantic model](docs/semantic-model.md) · [querying metrics](docs/query-metrics.md) · [correcting a business definition](docs/feedback-loop.md) · [extensions](docs/extensions.md) · [the agent guide](docs/agent-guide.md) · [skill evaluations](docs/skill-evaluations.md)
- Reference: [MCP tools](docs/mcp-tools.md) · [identity and access](docs/mcp-identity.md) · [audit records](docs/mcp-audit.md) · [exit codes](docs/exit-codes.md) · [glossary](docs/GLOSSARY.md) · [the full index](docs/README.md)

## Try it on your laptop

Python 3.14 and Docker; no cloud account, domain or GitHub account.

    uv tool install vecten
    vecten init my-project --name my-project --users 5 --source hubspot
    cd my-project
    vecten up        # Apache Airflow at https://airflow.localhost:8443, the MCP at https://mcp.localhost:8443
    vecten down

Plain HTTP on `http://airflow.localhost:8480` redirects to HTTPS, and the Airflow API is on
`http://127.0.0.1:8482` for the agent's Airflow MCP server. On Snowflake the laptop stack runs
Airflow alone, because the MCP's Snowflake login works deployed only. With an agent: point
Claude Code at an empty directory, say what you want, and it runs `vecten next --json` and
follows the items. When it is time for the cloud, [deploy to AWS](docs/deploy-aws.md) or
[to Scaleway](docs/deploy-scaleway.md): `vecten estimate` tells you what it will cost first.

## What v0.1 does and does not do

This is an alpha. v0.1 deploys to AWS and Scaleway, with Postgres or Snowflake, one warehouse
per deployment, GitHub Actions as the CI. Azure, GCP, other CI hosts, BigQuery and Databricks
are outside it. No analyst dashboard, no Kubernetes, no built-in model inference, no connector
catalogue: sources use dlt or your own code. Metric queries operate on one dataset. Minor
releases in the 0.x series may break things; pin the minor. The [exit codes](docs/exit-codes.md)
are stable within a minor release.

| | v0.1 |
|---|---|
| Python | 3.14 (`uv tool install` fetches it) |
| Host | a POSIX laptop with Docker; images for linux/amd64 and linux/arm64 |
| Clouds | AWS, Scaleway (Terraform 1.16, run by GitHub Actions) |
| Warehouses | Postgres 17 (managed by the cloud), Snowflake (your account) |
| Stack | Apache Airflow 3.3, dbt 1.12, dlt 1.31, an Apache Ossie semantic model |

## Using it safely

The MCP runs every query as the person's own warehouse identity, read-only, with a row cap and a
timeout, and treats questions, tool arguments and SQL as untrusted data. Keep the admin flag to
the people who administer the server, and give agents the groups their people have.
[Identity and access](docs/mcp-identity.md) has the model; [SECURITY.md](SECURITY.md) says how
to report a problem. Vecten collects no usage data and switches off dlt's and dbt's.

## Community

Questions: [Discussions](https://github.com/vecten/vecten-open/discussions). Bugs:
[Issues](https://github.com/vecten/vecten-open/issues). [Contributing](CONTRIBUTING.md) ·
[Security](SECURITY.md) · [Code of conduct](CODE_OF_CONDUCT.md) · [Changelog](CHANGELOG.md).
Run it yourself, or [let Vecten run it for you](https://vecten.com/).

Built on [dlt](https://github.com/dlt-hub/dlt), [Apache Airflow](https://airflow.apache.org/),
[dbt](https://github.com/dbt-labs/dbt), the [Model Context Protocol](https://modelcontextprotocol.io/)
and [Apache Ossie](https://ossie.apache.org/). Apache Airflow and Apache Ossie are trademarks of
the Apache Software Foundation; dbt is a trademark of dbt Labs; Snowflake is a trademark of
Snowflake Inc. Licensed under [Apache-2.0](LICENSE). Copyright 2026 Vecten.
