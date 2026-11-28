---
spec_version: 1.1.0
okf_version: "0.2"
base: http://localhost/
title: Vecten Open documentation
description: Index of the Vecten Open documentation, converted from the Markdown files in docs/ of the repository.
---

# Vecten Open documentation

Every page here reads on its own. Start with the first group if you are new; the other groups
are for running, building on and looking things up.

## Start here

- [The data warehouse](/concepts/warehouse/): Postgres or Snowflake, laptop and deployed, bring your own, and how dbt models the data.
- [Connectors](/concepts/connectors/): what a connector is here, the four kinds of source, credentials, runs and fixtures.
- [Orchestration](/concepts/orchestration/): what Apache Airflow does, the sync and dbt DAGs, reading Airflow, shipping, failures.
- [The MCP](/concepts/mcp/): the server people and agents ask questions through, its tools, connecting a client, and what keeps it safe.
- [The `vecten` CLI](/concepts/cli/): every command, the `--json` contract, updating a project.
- [Agentic flow and malleable software](/concepts/agentic-flow/): why the agent is the interface, before and after, and what the evaluations measured.
- [Why Vecten Open, and when not](/concepts/why-vecten/): who it is for, what it adds over the alternatives, and the cases it is not built for.

## Run it

- [Deploying to AWS](/concepts/deploy-aws/) and [Deploying to Scaleway](/concepts/deploy-scaleway/): what an environment is, the steps, the cost.
- [Operating a running platform](/concepts/operations/): `vecten issues`, evidence, alerts, the `operate` skill.
- [Admin panel and usage analytics](/concepts/admin-panel/): who asks what, and what is missing.

## Build on it

- [Authoring the semantic model](/concepts/semantic-model/): datasets, metrics, the allowed functions, eval cases.
- [Querying metrics](/concepts/query-metrics/): how `query_metrics` compiles a question into SQL.
- [Correcting a business definition](/concepts/feedback-loop/): from a reported gap to a merged fix.
- [Installing and writing extensions](/concepts/extensions/): connectors, tools and identity backends as packages.
- [Extension services](/concepts/services/): bots, portals and dashboards on their own hostnames: access, the sign-in gate, isolation, volumes and health.
- [Analytics apps](/concepts/analytics-apps/): dashboards an extension declares, who may open them and what data they read.
- [Extensions from a private git repository](https://github.com/vecten/vecten-open/blob/main/docs/extensions-private-git.md): a team's shared extensions by tag, with the credential only on laptops and in CI.
- [The agent guide](/concepts/agent-guide/): the `guide` skill's text, rendered from the package.
- [Skill evaluations](/concepts/skill-evaluations/): the harness, the runs and the results.

## Reference

- [MCP tool reference](/concepts/mcp-tools/), [MCP identity and access](/concepts/mcp-identity/), [MCP audit records](/concepts/mcp-audit/).
- [Exit codes](/concepts/exit-codes/) and the [glossary](/concepts/glossary/).
- The requirements the software is built to are published with it under `.ai/openspec/specs/`,
  one folder per capability (`cli/`, `platform/`, `mcp/`, `shared/`); each `spec.md` holds
  requirements with scenarios. They are the source of truth; [CONTRIBUTING](https://github.com/vecten/vecten-open/blob/main/CONTRIBUTING.md)
  says how a change to them happens.
