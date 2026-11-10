<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vecten/vecten-open/main/docs/images/vecten-open-dark.svg">
    <img src="https://raw.githubusercontent.com/vecten/vecten-open/main/docs/images/vecten-open-light.svg" width="320" alt="Vecten">
  </picture>
</p>

<p align="center">
  <a href="https://pypi.org/project/vecten-mcp/"><img src="https://img.shields.io/pypi/v/vecten-mcp" alt="PyPI"></a>
  <a href="https://github.com/vecten/vecten-open/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/vecten/vecten-open/ci.yml?branch=main&label=CI" alt="CI"></a>
  <a href="https://github.com/vecten/vecten-open/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-green" alt="Apache-2.0"></a>
  <a href="https://github.com/vecten/vecten-open/blob/main/CHANGELOG.md"><img src="https://img.shields.io/badge/status-alpha-orange" alt="alpha"></a>
</p>

# The Vecten MCP

**Your warehouse, served to Claude and your agents, as each person, with a receipt for every
answer.** An open-source Model Context Protocol (MCP) server for the Snowflake or Postgres
warehouse you already run.

## Who it is for

You have a warehouse and data applications on top of it. Now people want to ask questions from
Claude or another chat assistant, and you want agents to use the same data. What is missing is
an MCP interface: the open protocol those assistants use to reach tools and data. The Vecten MCP
is that interface, and it runs on your own cloud. It is one half of
[Vecten Open](https://github.com/vecten/vecten-open); the other half, a data platform that
builds a warehouse, you can leave out.

## Security at the core

**Who can see what is decided by your warehouse, not by the AI.** Each person gets their own
warehouse identity: a Postgres role or a Snowflake user. Every query runs as that identity, so
what their grants forbid, they cannot read, whatever the assistant asks. Queries are read-only,
with a row cap and a timeout, and the server treats questions and agent-written SQL as untrusted
data.

**The server holds no admin warehouse account.** It creates people's identities through one narrowly
granted login and never keeps their passwords. It refuses to start when that login's grants
differ from what is expected. `vecten mcp provisioner-script` prints the setup SQL, so your
warehouse administrator reads it before anything runs.

**You can prove what happened.** Every call is recorded with the person, their question, the
tool, the SQL and the tables and columns it touched. Every answer carries a receipt that anyone
signed in can check later with `verify_receipt`. Administrators see who asked what, and which
questions the data could not answer.

Sign-in is a password or your identity provider over OpenID Connect.
[Identity and access](https://github.com/vecten/vecten-open/blob/main/docs/mcp-identity.md) has
the full model, and [Connections, credentials, and compromise](https://github.com/vecten/vecten-open/blob/main/docs/mcp-identity.md#connections-credentials-and-compromise)
says what v0.1 does not protect against.

## Common definitions and metrics

An assistant left to write its own SQL will invent its own revenue. Here each metric is defined
once, in a semantic model kept in your repository (an [Apache Ossie](https://ossie.apache.org/)
model), and reviewed like code.

- **Start from what you have.** `vecten mcp import --dbt <path>` builds the datasets (the tables
  the model may read) from an existing dbt project; `vecten mcp introspect` drafts them from the
  warehouse catalog. You and your agent write the metrics.
- **Answers come from the definitions.** `query_metrics` turns a question into SQL from the
  shared definitions and returns the definitions it used. When the model cannot answer exactly,
  it refuses with a reason; it never returns an approximate number.
- **Numbers stay put.** Evaluation cases pin what each metric request must compile to, so a later
  edit cannot quietly change a number someone relies on.
- **Wrong answers become fixes.** A person who gets a wrong answer reports it. That opens a
  gap, a tracked record of what the model got wrong, and the fix comes back as a pull request
  with a regression case for you to review.

[Authoring the semantic model](https://github.com/vecten/vecten-open/blob/main/docs/semantic-model.md) ·
[Querying metrics](https://github.com/vecten/vecten-open/blob/main/docs/query-metrics.md) ·
[Correcting a business definition](https://github.com/vecten/vecten-open/blob/main/docs/feedback-loop.md)

## Install

You install the `vecten` CLI, not this package. `vecten` sets up the project, and the server runs
as a container from the `ghcr.io/vecten/vecten-mcp` image: on your laptop with Docker, and
deployed on your cloud. This package is the code inside that image.

    uv tool install vecten
    vecten init warehouse-assistant --name warehouse-assistant --users 5 \
      --has-warehouse yes --warehouse-url postgres://warehouse.example.com/analytics
    cd warehouse-assistant
    vecten next

A Snowflake address has the form `snowflake://account/database`. The address never holds a
password; `vecten secret set` stores a credential when a setup step asks for one. `vecten next` lists what
the project needs, in order: the setup SQL for your administrator, the semantic model, the first
people. On Postgres, `vecten up` then runs the server on your laptop at
`https://mcp.localhost:8443`. With an agent such as Claude Code, point it at the directory and it
runs `vecten next --json` and follows the items.

To connect, `vecten mcp connection --env prod` prints the deployed address. In Claude Code:
`claude mcp add --transport http vecten https://mcp.<domain>/mcp`. In Claude Desktop: Settings,
Connectors, add a custom connector with the same address. The first call opens the sign-in page.

## What v0.1 covers

This is an alpha.

| | v0.1 |
|---|---|
| Warehouses | Postgres, Snowflake. Support for Databricks, BigQuery and Redshift is planned. |
| Runs on | your laptop with Docker (Postgres only), AWS or Scaleway, deployed with Terraform through GitHub Actions |
| Snowflake | deployed only: Snowflake trusts the server's sign-in at its public address, so a laptop cannot stand in |
| Clients | Claude Desktop, Claude Code, and any client that speaks MCP over streamable HTTP |
| Metrics | one dataset per metric query; a question that needs a join gets a SQL starting point instead |

Minor releases in the 0.x series may break things; pin the minor.

## More

- [The MCP](https://github.com/vecten/vecten-open/blob/main/docs/mcp.md): how it fits, its tools, connecting a client
- [MCP tools](https://github.com/vecten/vecten-open/blob/main/docs/mcp-tools.md) · [audit records](https://github.com/vecten/vecten-open/blob/main/docs/mcp-audit.md) · [admin panel and usage analytics](https://github.com/vecten/vecten-open/blob/main/docs/admin-panel.md)
- [Deploying to AWS](https://github.com/vecten/vecten-open/blob/main/docs/deploy-aws.md) · [deploying to Scaleway](https://github.com/vecten/vecten-open/blob/main/docs/deploy-scaleway.md)
- [Security policy](https://github.com/vecten/vecten-open/blob/main/SECURITY.md) · [questions](https://github.com/vecten/vecten-open/discussions) · [issues](https://github.com/vecten/vecten-open/issues)

Apache-2.0, copyright 2026 Vecten. Released together with `vecten`, `vecten-platform` and
`vecten-shared` at one version.
