# Deploying to AWS

For the person who runs `vecten deploy`: what an environment on AWS is made of, the steps in order, what it costs, and where to go once it runs.

## What you get

A deployed environment runs the project in your own AWS account: an EC2 virtual machine per
component (one for both under the `minimal` profile), private RDS Postgres instances,
`airflow.<domain>` and `mcp.<domain>` in Route 53 with Let's Encrypt certificates, and an S3
bucket for bundles, artifacts and logs. GitHub Actions runs Terraform and ships the project,
signing in to AWS over OIDC, which exchanges a short-lived token GitHub issues to each job for
temporary AWS credentials, so no AWS key is stored in GitHub.

## Before you start

Use an AWS account that holds this project and nothing else. The role GitHub Actions deploys
with carries AWS's `PowerUserAccess` policy, which reaches every resource in the account outside
IAM, so a workflow run or a leaked deploy credential could read or change any other workload
in the same account. One account per project, or per environment, keeps that reach to the
project.

You need an AWS account that is a member of an AWS Organization, not its management account,
a Route 53 hosted zone for the domain, and the GitHub CLI `gh`, logged in. `vecten next`
offers each step when it is due; in order:

## Steps

1. `vecten init --deploy` renders the deploy layer from the deploy answers:
   `deploy/envs/prod.tfvars`, the Terraform root `deploy/terraform/aws`, which references the
   module in this repository's `terraform/aws` at the release's tag, and the workflows.
2. `vecten github init` creates the GitHub repository and pushes.
3. `vecten bootstrap --env prod`, with your own AWS login (`aws sso login`), creates the
   Terraform state bucket, the OIDC trust, the deploy and plan roles, and the GitHub
   environment `prod` with its variables.
4. `vecten secret set <NAME> --env prod --from-local` copies each secret the connectors read
   from `.env` to AWS Secrets Manager. A project whose platform builds a Snowflake warehouse
   also stores the Snowflake deploy user as `DEPLOY_SNOWFLAKE_USER` and
   `DEPLOY_SNOWFLAKE_PRIVATE_KEY`; that user's default role holds CREATE DATABASE, CREATE
   WAREHOUSE, CREATE ROLE, CREATE USER and MANAGE GRANTS on the account.
5. `vecten github push` commits and pushes, because CI deploys what GitHub holds.
6. `vecten deploy --env prod` dispatches the apply workflow from the laptop and follows it; the
   workflow runs Terraform and ships every host. Pushing the `deployed_commit` it records in
   `vecten.toml` turns on shipping on merge.

After the first deploy, every push to the default branch ships the DAGs, the dbt project,
`osi/` and `evals/` without Terraform, and a pull request that changes `deploy/` gets the
Terraform plan as a comment. `vecten user add-admin <firstname> --env prod` creates a warehouse
admin login, and `vecten destroy --env prod` tears the environment down after a final snapshot
of each database. The rendered project's `docs/runbook.md` covers running the environment:
what bootstrap creates, secret scopes, admin access, restore and destroy.

## Cost

`vecten estimate --cloud aws --json` prints the approximate monthly hosting cost for a
size and profile, from list prices gathered on the date it names. On 2026-10-16, both components
on Postgres:

| Size | Standard profile | Minimal profile |
|---|---|---|
| small | $120 | $105 |
| medium | $245 | $195 |

The `minimal` profile puts both components on one VM. Your own bill depends on region, traffic
and what the connectors load; the command is the estimate the project ships with, not a quote.

## Operating it

[Operating a running platform](operations.md) covers shipping, issues, rollback and key rotation;
the [admin panel](admin-panel.md) covers usage analytics; the rendered project's `docs/runbook.md`
covers restore and destroy for this cloud.

Back to the [docs index](README.md).
