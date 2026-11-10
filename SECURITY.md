# Security policy

Vecten Open holds cloud credentials, warehouse identities and an OAuth server, so we want to
hear about problems privately and first.

## Reporting a vulnerability

Report it through GitHub's private vulnerability reporting on this repository: open the
**Security** tab and choose **Report a vulnerability**. That creates a private advisory only
the maintainers can see. Please do not open a public issue or discussion for a security
problem. We acknowledge a report within five working days and keep you informed until it is
fixed and disclosed.

What counts: anything that lets a person read data outside their own warehouse identity or
their groups' grants, bypass or alter the audit, forge a receipt, reach the control plane or a
VM with the project's credentials, or make the MCP run a write. A deployment made insecure by
a default we ship counts too.

## Known limitations

These are known and planned to change; a report that only restates one of them is not a new
vulnerability, but a way to exploit one beyond what is written here is.

- **The MCP VM can reach the database master credentials.** It is also the database setup
  host, and the MCP container can use its cloud identity, so code running in that container
  can administer the databases. Under the `minimal` profile, DAG code shares that VM.
- **Postgres connections do not verify the server.** Deployed connections use TLS but rely on
  the private network for the database's identity.
- **The AWS deploy role carries `PowerUserAccess`.** Use an AWS account dedicated to the
  project, as [Deploying to AWS](docs/deploy-aws.md) says.

[MCP identity and access](docs/mcp-identity.md#connections-credentials-and-compromise) gives
the details.

## Supported versions

Only the latest 0.x release receives fixes. Pin the minor and upgrade with `vecten update`.

## Scope notes

The MCP treats questions, tool arguments and SQL as untrusted data; a prompt that makes an
agent do something unwise within that agent's own grants is a client-side concern, not a
server vulnerability, unless the server let the agent exceed those grants. Third-party
components (dlt, Apache Airflow, dbt, Postgres, Snowflake) have their own policies; report
their problems to them and tell us if the default configuration we ship makes one worse.
