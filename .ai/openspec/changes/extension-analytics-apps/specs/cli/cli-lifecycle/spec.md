## ADDED Requirements

### Requirement: app connect

`vecten app connect <extension>.<app> --env <env>` SHALL, with the person's cached admin token, create the app principal `svc_app_<app>` through the admin API when it does not exist, set its groups to the app's bound groups, mark it as the principal of that app, issue it client credentials, and store the client id and secret in the environment's secrets store under the app's credential names, as the `shared/extension-points` spec defines them, so the secret's name puts it in the scope of the app's host. Run again, it SHALL rotate the secret and update the groups. It SHALL never print the secret. For `--env local` it SHALL write both to `.env`. It SHALL refuse an unbound app, a `snowflake` app, and a missing admin token naming `vecten login`. `next` SHALL offer the command for each environment where a bound container app has no principal.

#### Scenario: First connection

- **WHEN** an admin runs `vecten app connect acme-sales.overview --env prod` for an app bound to `["sales"]`
- **THEN** `svc_app_overview` exists in `sales`, `APP_OVERVIEW_CLIENT_ID` and `APP_OVERVIEW_CLIENT_SECRET` are in prod's `platform` scope, and the output names both secrets without their values

#### Scenario: Rotation

- **WHEN** the command runs again for the same app and environment
- **THEN** the previous client secret stops working and the new one is stored, and the next ship delivers it to the app

#### Scenario: Not signed in

- **WHEN** the command runs with no cached token
- **THEN** it exits non-zero naming `vecten login`, and nothing is created or stored
