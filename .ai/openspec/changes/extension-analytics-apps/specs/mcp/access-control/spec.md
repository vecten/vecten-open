## ADDED Requirements

### Requirement: An app reads data as its own principal, through the MCP

Each bound app SHALL read data only through the MCP as its app principal `svc_app_<app>`, a service principal that belongs to the app's bound groups and that the server marks as the principal of that app. The server SHALL restrict an app principal to the app's declared datasets as recorded in the shipped `extensions.lock.json`: `sql-exec` SHALL refuse, before the warehouse, a statement referencing any relation that is not the source of a declared dataset; `query_metrics` SHALL refuse a metric whose home dataset is not declared; `describe` and `search` SHALL show declared datasets only. The warehouse SHALL still enforce the principal's group roles underneath. A declared dataset missing from the loaded model SHALL be listed in model health.

#### Scenario: Declared dataset

- **WHEN** `svc_app_overview`, declared to read `orders`, runs `sql-exec` with `SELECT region, sum(amount) FROM marts.orders GROUP BY 1`
- **THEN** the query runs as `svc_app_overview`'s warehouse user and is audited under that principal

#### Scenario: Undeclared relation

- **WHEN** the same principal runs `sql-exec` against `marts.payroll`
- **THEN** the tool refuses naming the app and the relation, and no query reaches the warehouse

#### Scenario: Restricted mart through an app

- **WHEN** an app declares a dataset in the restricted area `finance` and its principal belongs to `finance`
- **THEN** its queries on that dataset succeed, and the warehouse refuses them once `finance`'s area grant is revoked

### Requirement: An app admits only groups that may read all of its data

The server SHALL compute, for each bound app, its admitted groups: the bound groups that may read every declared dataset through a dataset grant, a schema grant or a restricted-area grant. The gate SHALL admit a person to the app only through an admitted group. Each bound group that is not admitted SHALL be listed in access health with the app and the first dataset it cannot read. The computation SHALL run at every grant reconciliation, every model swap and every ship that changes the app's binding or declaration.

#### Scenario: Default group bound to a finance dashboard

- **WHEN** an app reading `marts_finance.revenue` is bound to `["finance", "default"]` and only `finance` holds the `finance` area
- **THEN** members of `finance` open the app, a person only in `default` is answered 403, and access health names `default`, the app and `marts_finance.revenue`

#### Scenario: Grant added later

- **WHEN** an admin then grants the `finance` area to `default`
- **THEN** after the next reconciliation a person only in `default` opens the app and the access-health entry is gone

### Requirement: The viewer is audited, never trusted

The server SHALL accept an `X-Vecten-On-Behalf-Of` header carrying an email only on requests authenticated as an app principal, SHALL record it in the audit row beside the principal, and SHALL NOT use it for any authorization decision. On a request from any other principal the header SHALL be ignored and not recorded.

#### Scenario: Dashboard query audited

- **WHEN** Ada views the sales app and it queries the MCP with `X-Vecten-On-Behalf-Of: ada@acme.com`
- **THEN** the audit row names `svc_app_overview` as the principal and `ada@acme.com` as the viewer

#### Scenario: Header from a person's client

- **WHEN** a person's MCP client sends `X-Vecten-On-Behalf-Of: ada@acme.com`
- **THEN** the call runs and is audited as that person only

### Requirement: Snowflake app grants are reconciled desired state

For each bound `snowflake` app that has been published, the server SHALL hold as desired state `USAGE` on the app's Streamlit object for each admitted group's role and `SELECT` on exactly the declared datasets' sources for the app's role `app_<app>`, SHALL reconcile both with the other grants, and SHALL revoke from any role it manages a grant on the app that the state does not hold. The app role SHALL hold no group's role and no other read grant.

#### Scenario: Group unbound

- **WHEN** a ship removes `sales` from a published Snowflake app's `groups`
- **THEN** the next reconciliation revokes `USAGE` on the app from the `sales` role
