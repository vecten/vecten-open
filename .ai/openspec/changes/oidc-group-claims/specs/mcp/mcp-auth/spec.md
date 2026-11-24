## MODIFIED Requirements

### Requirement: Identity backends are plugins
The server SHALL resolve a person's identity through an identity backend selected at deploy time from the `vecten.mcp.auth` entry point group. The open edition SHALL ship a `password` backend and an `oidc` backend against a single provider. A backend SHALL return the person's issuer, subject, email and display name. With an explicit OIDC group mapping, a verified complete array claim SHALL synchronize the configured local human memberships at sign-in. Group membership SHALL live in `mcp_state`. Administrators manage unmapped groups and service accounts locally. The server SHALL reject manual changes to mapped human memberships and report their sources in access health.

#### Scenario: OIDC login against one provider
- **WHEN** the deployment is configured with the `oidc` backend pointing at a Google Workspace or Entra ID tenant and a person signs in
- **THEN** the person is authenticated through that provider
- **AND** the session carries the person's identity from the provider and the groups recorded in `mcp_state`

#### Scenario: Backend not installed
- **WHEN** the deployment names an identity backend that no installed plugin provides
- **THEN** the server refuses to start and reports the missing backend by name

#### Scenario: Provider removes a membership

- **WHEN** a provisioned person signs in with a valid claim which no longer contains the mapped value
- **THEN** the warehouse role and state membership are revoked while locally managed groups remain
