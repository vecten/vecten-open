# The MCP

For anyone who will ask questions of the data, or let an agent do it: what the Model Context Protocol server over the warehouse is for, what its tools do, how a client connects, and what keeps it safe. The six pages linked below hold the detail.

A warehouse full of consolidated data is only useful once people can ask it questions. The MCP
is the layer that lets a person, or an agent working for them, do that from the tools they
already use: Claude Desktop, Claude Code, or any client that speaks the protocol. Every answer
is attributable to one person, because every query runs as that person's own warehouse
identity; every answer is reproducible, because every call leaves an audit record with a receipt
that says how it was answered; and every wrong answer becomes a fix, because a reported gap in
the semantic model turns into a pull request.

## How it fits

```
person or agent  ──MCP client──▶  the MCP  ──as that person's warehouse user──▶  the warehouse
                                     │
                                     ├─ semantic model (osi/): what the data means
                                     ├─ audit: every call, with a receipt
                                     └─ gaps: what the model could not answer
```

The server runs deployed, at `https://mcp.<domain>`, and on the laptop at
`https://mcp.localhost:8443` (except over Snowflake, whose login trusts the deployed server only).
It reads the semantic model from the project's `osi/` directory and keeps its own state (people,
groups, grants, audit, gaps) in a private Postgres database, `mcp_state`, never in the warehouse.

## Tools

| Group | Tools | What they do |
|---|---|---|
| Discovery | `describe`, `search` | What datasets, columns and metrics exist and what they mean, from the semantic model |
| Answers | `query_metrics`, `sql-exec`, `verify_receipt` | Compile a metric question into SQL and run it; run one read-only statement; check a receipt against the audit |
| Feedback | `report_feedback`, `list_gaps`, `read_gap`, `set_gap_status` | Report an answer as wrong; see, read and resolve the gaps that reports create |
| Administration | `list_users`, `list_groups`, `list_grants`, `usage`, `usage_view`, `model_health`, `connected_clients`, `search_audit`, `read_audit_row`, `read_settings` | Read-only views for people with the admin flag: who has access, what is asked, whether the shipped model is current, what the audit holds |

Everyone signed in can use the discovery, answer and feedback tools; warehouse grants still
decide what data a query returns. The admin tools are visible only to people with the admin
flag, and no tool changes identities, grants or settings; that is the CLI's and the admin
panel's job. The [tool reference](mcp-tools.md) has every argument and result;
[querying metrics](query-metrics.md) explains how a metric question becomes SQL.

## Connecting a client

`vecten mcp connection --env prod` prints the deployed URL and how to add it:

- Claude Code: `claude mcp add --transport http vecten https://mcp.<domain>/mcp`
- Claude Desktop: Settings, Connectors, add a custom connector with the URL

The first call opens the server's own sign-in page (a password, or your identity provider over
OpenID Connect); the client holds the session from then on. A person is created by an admin with
`vecten user add`, put in groups with `vecten group add-member`, and given data with
`vecten grant`; [identity and access](mcp-identity.md) covers all of it, including service
principals for automations.

## Identity, audit and the feedback loop

- **Identity.** One warehouse user per person: a Postgres role, or a Snowflake user, that the
  server creates through a tightly scoped `provisioner` and never holds a password for. On
  Postgres a session runs through an `impersonator` role granted membership in the person's
  role for the session only. The server holds no admin warehouse account and refuses to start
  when the provisioner's grants differ from the contract. [MCP identity and access](mcp-identity.md).
- **Audit.** Every call is recorded with the principal, the question, the tool, the SQL hash and
  the tables and columns it touched, and answered with a receipt id that `verify_receipt`
  checks. [MCP audit records](mcp-audit.md).
- **Feedback.** `report_feedback` on a wrong answer creates a gap; the `semantic` skill reads it,
  fixes the definition in `osi/`, adds a regression case and opens a pull request; merging and
  shipping closes the gap. [Correcting a business definition](feedback-loop.md).
- **Usage.** The admin panel shows what people ask, which definitions are missing, and who is
  active. [Admin panel and usage analytics](admin-panel.md).

## Using it safely

`sql-exec` runs exactly one read-only statement, as the caller's warehouse user, inside a
read-only transaction on Postgres, with a row cap and a statement timeout; it cannot write, and
it cannot read what the person's grants do not allow. Questions, tool arguments and the SQL an
agent writes are untrusted data to the server and to the skills: nothing in them is executed as
an instruction. Keep the admin flag to the few people who administer the server, give agents
the groups their people have and nothing more, and read [SECURITY.md](../SECURITY.md) for how
to report a problem.

The server process holds no admin warehouse account, but its VM does more than serve: it also
runs the database setup, so its cloud identity can read the database master credentials. Code
that runs inside the MCP container can administer the databases.
[Connections, credentials, and compromise](mcp-identity.md#connections-credentials-and-compromise)
says what that means and what else v0.1 does not protect against.

Back to the [docs index](README.md).
