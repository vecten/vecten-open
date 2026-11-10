# Why Vecten Open, and when not

For the person comparing options: who this is for, the one thing it does that the alternatives do not, what it adds over each of them, and the cases it is not built for.

Last reviewed: 2026-11-06. Every line about another product is taken from that product's own
page on that date, linked; the products change, so check the link before you decide.

## Who it is for

A team that wants its own warehouse, in its own cloud account, with people and agents asking
questions of it, and that will develop the platform with an agent rather than buy a catalogue
of connectors and dashboards. The data engineer leads; the agent does the typing; the result is
a repository the team owns.

## What it does that the alternatives do not

Vecten Open is the open-source combination of two things that otherwise come separately:

- a data platform scaffold that an agent drives end to end: dlt ingestion, Apache Airflow
  orchestration and dbt modelling to a versioned schema convention, deployed by Terraform to AWS
  or Scaleway with Postgres or Snowflake, all through one CLI with a JSON contract and a
  `vecten next` loop;
- an MCP server over that warehouse, or over one you already run, in which every query runs as
  the person's own warehouse identity, every answer carries a receipt that the audit can verify,
  usage is analysed as a product feature, and a wrong answer becomes a gap that an agent turns
  into a reviewable fix.

Each half exists elsewhere in pieces. Nothing else ships both halves together, open source, on
Postgres and Snowflake, with the agent as the interface. Read the [agentic flow](agentic-flow.md) page for
the mechanisms and the measured difference the skills make.

## Vecten Open against the alternatives

Each alternative is described from its own page on the review date, followed by what Vecten
Open adds over it.

**Platform side**

- [Bruin](https://github.com/bruin-data/bruin) is a single Go binary that brings ingestion, SQL,
  Python and R transforms and data quality into one framework, run on a laptop, an EC2 instance
  or GitHub Actions. Vecten Open adds what comes after the pipeline: the warehouse deployed in
  your cloud account with Terraform, and the MCP that serves it to people under their own
  identity with an audit, so the platform gets used, not only built.
- [dlt and dltHub](https://github.com/dlt-hub/dlt): dlt is the ingestion library Vecten Open
  builds on, and dltHub (March 2026) runs dlt pipelines on a managed, agent-native platform with
  transformations, quality and observability. Vecten Open keeps dlt and keeps everything on your
  own cloud and in your own repository: no managed service in the path, and the orchestration,
  the models, the semantic model and the MCP are files you own.
- [Definite](https://www.definite.app/) centralises data, answers questions in plain English and
  ships data apps with pipelines, dashboards and agents in one platform, deployed privately in
  your cloud by the vendor. Vecten Open is open source under Apache-2.0: no vendor runs it for
  you unless you ask, nothing you may not change, and the agent you already use is the
  interface rather than a product's own.
- [Datacoves](https://datacoves.com/) is managed dbt and Apache Airflow, deployed into your own
  cloud with enterprise support. Vecten Open uses the same two tools, open and unmanaged, and
  adds the two things a managed stack does not: the agent loop that builds and maintains the
  platform, and the MCP layer that lets people ask questions of it.

**MCP and semantic-layer side**

- [TextQL](https://www.textql.com/) is an enterprise platform built around an ontology its own
  agent reasons over, aimed at regulated industries and sold by demo. Vecten Open is open
  source, runs in your account, lets any MCP client's agent ask, and keeps the semantic model as
  JSON in your repository that your own agent edits through pull requests.
- [Cube Core](https://github.com/cube-js/cube) is an open-source, headless semantic layer:
  metrics, dimensions, joins and access rules in code, exposed through SQL, REST and GraphQL to
  BI tools and agents. Vecten Open ships the semantic layer together with the warehouse it
  describes, the pipelines that fill it, one warehouse identity per person and the feedback loop
  that turns a wrong answer into a fix, so there is nothing to integrate.
- [Snowflake Cortex Agents](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents)
  and the Snowflake-managed MCP server run inside the Snowflake account under its access
  control. Vecten Open runs the same way on Postgres and on Snowflake, with the identity, audit
  and feedback loop in your repository rather than in one vendor's account, so the warehouse can
  change without the layer above it changing.
- [dbt MCP server](https://github.com/dbt-labs/dbt-mcp) gives agents the context of a dbt
  project in dbt Core, dbt Fusion and the dbt Platform. Vecten Open faces the other way: it
  serves the people who ask questions, as themselves, with a receipt per answer, and routes
  what the model could not answer back to the engineer's agent as a gap with evidence.
- [Lightdash](https://github.com/lightdash/lightdash) is an agentic BI platform: a context layer
  of trusted metrics, joins and permissions that powers dashboards, agents, data apps,
  embedding and MCP, with a Cloud as the recommended start. Vecten Open needs no BI application:
  the MCP client people already have is the interface, and the whole stack from ingestion to
  answer is open source and self-hosted.

## Not for you if

- You run on Azure or GCP, or your warehouse is BigQuery or Databricks: v0.1 deploys to AWS and
  Scaleway, with Postgres or Snowflake, and nothing else.
- You want Kubernetes: the platform is two virtual machines (or one) and managed databases.
- You need streaming: the sync DAGs are scheduled batch loads.
- You want a catalogue of ready connectors: Vecten Open ships none; sources are dlt verified
  sources, scaffolds, or code in your project.
- Your analysts will not use an MCP client: there is no dashboard product, only the admin panel.
- Your team will not run Terraform against its own cloud account, or will not lead an agent.

## Decide in five questions

1. Do you want the warehouse and the platform in your own account, as code you own? If no, pick
   a managed platform above.
2. Will the people who ask questions use an MCP client (Claude, or any other)? If no, pick a BI
   product.
3. Is your cloud AWS or Scaleway, and your warehouse Postgres or Snowflake? If no, not yet.
4. Will an agent do the building, with a data engineer leading it? If no, the loop and the
   skills are overhead.
5. Does per-person warehouse identity and a verifiable audit of every answer matter to you? If
   yes, this is the part nobody else ships open.

Back to the [docs index](README.md).
