## Why

Issue: [#39](https://github.com/vecten/vecten-open/issues/39).

The product has no dashboards, by design, and no place for a project or an extension to add one.
Teams that already have analytics apps, such as Streamlit apps that run inside Snowflake or a
hosted Streamlit app, have nowhere to register them, deploy them with the project, or grant
access to them through the groups that govern the data. They deploy the apps with a separate
workflow and grant access with hand-written SQL. The `shared/extension-points` spec already uses
`vecten.dashboards` as its example of a group this release does not know.

This change builds on `extension-services` (#38) and lands after it: a self-hosted app is a
service with a signed-in route, and needs nothing from #38 that a portal does not.

## What Changes

- **A new entry point group, `vecten.apps`.** Each entry declares one analytics app: a title and
  description, its runtime `kind`, its source, and the Ossie datasets it reads. `vecten lock`
  records it in `extensions.lock.json`. No BI tool is built in.
- **Two runtime kinds.**
  - `container`: a self-hosted web app run as a #38 service from the project image, with one
    `login` route. A `streamlit` runtime shorthand turns a file in the extension package into the
    right command through a small launcher in `vecten_shared`, with Streamlit's usage statistics
    off. Any other web framework gives its own command and port.
  - `snowflake`: Streamlit in Snowflake. The declaration, its access model and its listing are
    part of this change; publishing it needs warehouse objects and grants that #40 introduces, so
    until then `ship` reports such an app as not published, naming #40.
- **The project binds each app** in `[apps.<extension>.<app>]` of `vecten.toml`: who may open it
  (`groups`), and for a container app its hostname and whether it starts on the laptop.
  `extension add` writes the binding from the declaration's suggestions.
- **Who may open an app follows the MCP's groups.** For a container app the #38 gate admits only
  the bound groups. For a Snowflake app the MCP grants `USAGE` on the Streamlit object to the
  bound groups' roles as reconciled desired state.
- **What data an app reads follows the MCP's groups too.** An app reads through the MCP as its
  own service principal, `svc_app_<app>`, which belongs to the app's groups and which the MCP
  restricts to the datasets the app declares. The gate admits a person only through a bound
  group that may itself read every declared dataset; a bound group that may not is left out and
  reported as an access-health failure. The viewing person is recorded in the audit
  beside the app's principal. A Snowflake app runs as an app role holding exactly its declared
  datasets.
- **`vecten app connect <extension>.<app> --env <env>`** creates or rotates the app's service
  principal and stores its client credentials as the secrets the app reads.
- **Listing.** The admin panel gains an "Analytics" screen listing every bound app with its
  extension, version, kind, URL, groups, datasets and state; the admin API and an admin tool
  return the same list. Signed-in people get `/apps` on the MCP host listing the apps they may
  open. `docs/analytics-apps.md` describes the extension point.

## Capabilities

### Modified Capabilities

- `shared/extension-points`: the `vecten.apps` group, its kinds and the project's binding.
- `mcp/access-control`: who may open an app, app principals restricted to declared datasets, the
  consistency check between viewers and datasets, Snowflake app grants.
- `mcp/admin-panel`: the Analytics screen and the `/apps` page.
- `mcp/admin-api`: the apps read operation.
- `cli/cli-lifecycle`: `app connect`, and `extension add` with apps.

## Impact

- Code: `vecten_shared.extensions` (the group), `vecten_shared.config` (`[apps]`), a launcher
  module in `vecten_shared`; `vecten.stack.compose` and `vecten.stack.nginx` through #38's
  service rendering; `vecten_mcp` (app registry from the bundle, principal scoping in `sql-exec`,
  `query_metrics`, `describe` and `search`, the consistency check, audit field, panel screen,
  `/apps`, admin API); a new `cli/commands/app.py`; `vecten.next`.
- Not changed: the VM agent, Terraform, the base images.
- Docs: new `docs/analytics-apps.md`, `docs/extensions.md`, `docs/admin-panel.md`,
  `docs/GLOSSARY.md` ("Analytics app", "App principal").
- Depends on: `extension-services` (#38); #49/#50 for declared secrets and the record; the
  in-repo extensions change for templates kept in the project; #51 for restricted marts, which the
  consistency check reads; #40 before the `snowflake` kind publishes anything.
