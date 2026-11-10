# Deploying to Scaleway

For the person who runs `vecten deploy`: what an environment on Scaleway is made of, the steps in order, what it costs, and where to go once it runs.

## What you get

A Scaleway environment runs the project in two projects of your Scaleway organization. The
resources project holds an Instance per component (one for both under the `minimal` profile),
Managed Database for PostgreSQL instances on a private network, Secret Manager, an Object
Storage bucket, and `airflow.<domain>` and `mcp.<domain>` in Domains and DNS with Let's Encrypt
certificates; the state project holds only the Terraform state bucket. GitHub Actions runs
Terraform and ships the project as on AWS. Scaleway IAM offers no OIDC federation, so the jobs
sign in with scoped API keys that `vecten bootstrap` stores in GitHub: the deploy key as
`VECTEN_SCW_ACCESS_KEY` and `VECTEN_SCW_SECRET_KEY`, secrets of the GitHub environment that only
jobs declaring it receive, and a plan key, which reads the Terraform state and writes only its
lock objects, as repository secrets. Scaleway offers no instance identity and no run-command
API either: each VM holds an API key of its own, scoped to its secrets and its part of the
bucket, and pulls its bundles from the bucket.

## Before you start

You need a Scaleway organization and an administrator's API key, such as the Organization
Owner's, a Domains and DNS zone for the domain, the Scaleway CLI `scw` logged in with
`scw init`, and the GitHub CLI `gh`, logged in. `vecten next` offers each step when it is due;
in order:

## Steps

1. `vecten init --deploy` renders the deploy layer from the deploy answers:
   `deploy/envs/prod.tfvars`, the Terraform root `deploy/terraform/scaleway`, which references
   the module in this repository's `terraform/scaleway` at the release's tag, and the workflows.
2. `vecten github init` creates the GitHub repository and pushes.
3. `vecten bootstrap --env prod`, with your own Scaleway login, creates the two projects, the
   state bucket, the IAM applications with their policies and keys, and the GitHub environment
   `prod` with its variables and the deploy key. It finds the project holding the DNS zone
   itself and refuses when no project of the organization holds it. Keys expire after 365
   days; running bootstrap again within 30 days of an expiry rotates them.
4. `vecten secret set <NAME> --env prod --from-local` copies each secret the connectors read
   from `.env` to Secret Manager.
5. `vecten github push` commits and pushes, because CI deploys what GitHub holds.
6. `vecten deploy --env prod` dispatches the apply workflow from the laptop and follows it; the
   workflow runs Terraform, hands each VM its key and ships every host.

`vecten user add-admin <firstname> --env prod` reaches the warehouse over the database
instance's public endpoint, which exists only while `database_public_cidrs` in the tfvars lists
the addresses you connect from. `vecten destroy --env prod` takes a final snapshot of each
database first, the only guard against deletion Managed Databases offer. The rendered project's
`docs/runbook.md` covers what bootstrap creates, the VM's key and its rotation, shipping by
pull, admin access, restore and destroy.

## Cost

`vecten estimate --cloud scaleway --json` prints the approximate monthly hosting cost for a
size and profile, from list prices gathered on the date it names. On 2026-10-16, both components
on Postgres:

| Size | Standard profile | Minimal profile |
|---|---|---|
| small | $107 | $84 |
| medium | $236 | $153 |

The `minimal` profile puts both components on one VM. Your own bill depends on region, traffic
and what the connectors load; the command is the estimate the project ships with, not a quote.

## Operating it

[Operating a running platform](operations.md) covers shipping, issues, rollback and key rotation;
the [admin panel](admin-panel.md) covers usage analytics; the rendered project's `docs/runbook.md`
covers restore and destroy for this cloud.

Back to the [docs index](README.md).
