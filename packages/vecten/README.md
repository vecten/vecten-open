# vecten

An MCP (Model Context Protocol) server that lets people and agents ask questions of your
company's data, with one warehouse identity per person and a verifiable audit of every answer,
plus a data platform (dlt, Apache Airflow, dbt) that builds the warehouse behind it, on your own
cloud, if you have none yet. Both sit behind one CLI, `vecten`, and an agent does the building.

- **Deploy a starting point.** `vecten init` renders a project: dlt sources, Apache Airflow DAGs,
  dbt models to a versioned schema convention, a semantic model, Terraform for AWS or Scaleway
  with Postgres or Snowflake. `vecten up` runs it on your laptop; `vecten deploy` in your cloud.
- **Develop it with your agent.** Every change goes through `vecten` commands with a JSON
  contract, and `vecten next --json` tells the agent what the project needs next. Five skills
  ship with the CLI and as a Claude Code plugin.
- **Serve people through the MCP.** Claude Desktop, Claude Code or any MCP client connects;
  every query runs as the person's own warehouse user; every answer carries a receipt; a wrong
  answer becomes a gap, and the gap becomes a pull request.

## Try it on your laptop

Prerequisites: Python 3.14 and Docker. No cloud account, domain or GitHub account is needed.

    uv tool install vecten
    vecten init my-project --name my-project --users 5 --source hubspot
    cd my-project
    vecten up        # prints https://airflow.localhost:8443 and https://mcp.localhost:8443
    vecten down

## What v0.1 does and does not do

An alpha release. It deploys to AWS and Scaleway, with Postgres or Snowflake, one warehouse per
deployment, with GitHub Actions as the CI. Azure, GCP, BigQuery and Databricks are outside it;
there is no analyst dashboard, no Kubernetes deployment and no connector catalogue. Minor
releases in the 0.x series may break things; pin the minor.

Documentation, deep dives and the source: [https://github.com/vecten/vecten-open](https://github.com/vecten/vecten-open). Apache-2.0, copyright 2026 Vecten.
