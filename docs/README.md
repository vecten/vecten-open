# Vecten Open documentation

Every page here reads on its own. Start with the first group if you are new; the other groups
are for running, building on and looking things up.

## Start here

- [The data warehouse](warehouse.md): Postgres or Snowflake, laptop and deployed, bring your own, and how dbt models the data.
- [Connectors](connectors.md): what a connector is here, the four kinds of source, credentials, runs and fixtures.
- [Orchestration](orchestration.md): what Apache Airflow does, the sync and dbt DAGs, reading Airflow, shipping, failures.
- [The MCP](mcp.md): the server people and agents ask questions through, its tools, connecting a client, and what keeps it safe.
- [The `vecten` CLI](cli.md): every command, the `--json` contract, updating a project.
- [Agentic flow and malleable software](agentic-flow.md): why the agent is the interface, before and after, and what the evaluations measured.
- [Why Vecten Open, and when not](why-vecten.md): who it is for, what it adds over the alternatives, and the cases it is not built for.

## Run it

- [Deploying to AWS](deploy-aws.md) and [Deploying to Scaleway](deploy-scaleway.md): what an environment is, the steps, the cost.
- [Operating a running platform](operations.md): `vecten issues`, evidence, alerts, the `operate` skill.
- [Admin panel and usage analytics](admin-panel.md): who asks what, and what is missing.

## Build on it

- [Authoring the semantic model](semantic-model.md): datasets, metrics, the allowed functions, eval cases.
- [Querying metrics](query-metrics.md): how `query_metrics` compiles a question into SQL.
- [Correcting a business definition](feedback-loop.md): from a reported gap to a merged fix.
- [Installing and writing extensions](extensions.md): connectors, tools and identity backends as packages.
- [Extension services](services.md): bots, portals and dashboards on their own hostnames: access, the sign-in gate, isolation, volumes and health.
- [Analytics apps](analytics-apps.md): dashboards an extension declares, who may open them and what data they read.
- [The agent guide](agent-guide.md): the `guide` skill's text, rendered from the package.
- [Skill evaluations](skill-evaluations.md): the harness, the runs and the results.

## Reference

- [MCP tool reference](mcp-tools.md), [MCP identity and access](mcp-identity.md), [MCP audit records](mcp-audit.md).
- [Exit codes](exit-codes.md) and the [glossary](GLOSSARY.md).
- The requirements the software is built to are published with it under `.ai/openspec/specs/`,
  one folder per capability (`cli/`, `platform/`, `mcp/`, `shared/`); each `spec.md` holds
  requirements with scenarios. They are the source of truth; [CONTRIBUTING](../CONTRIBUTING.md)
  says how a change to them happens.
