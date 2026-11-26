## MODIFIED Requirements

### Requirement: Identity backends are plugins
The server SHALL resolve a person's identity through an identity backend selected at deploy time from the `vecten.mcp.auth` entry point group. The open edition SHALL ship a `password` backend and an `oidc` backend against a single provider. A backend MUST return the person's identity only: issuer, subject, email and display name. Group membership SHALL live in `mcp_state` and is managed by admins; an Enterprise directory-sync extension MAY write it.

#### Scenario: OIDC login against one provider
- **WHEN** the deployment is configured with the `oidc` backend pointing at a Google Workspace or Entra ID tenant and a person signs in
- **THEN** the person is authenticated through that provider
- **AND** the session carries the person's identity from the provider and the groups recorded in `mcp_state`

#### Scenario: Backend not installed
- **WHEN** the deployment names an identity backend that no installed plugin provides
- **THEN** the server refuses to start and reports the missing backend by name

#### Scenario: Extension receives a verified sign-in

- **WHEN** a configured sign-in hook receives the verified identity and its token claims
- **THEN** it may write human memberships only for its own declared groups through the access service. Manual changes to those human memberships are refused, access health identifies the owner, and deleting a group still removes its members.


#### Scenario: A person awaits provisioning

- **WHEN** an invited person or first administrator signs in before their warehouse account is provisioned
- **THEN** the server skips membership hooks and continues sign-in

#### Scenario: A hook cannot complete the sign-in check

- **WHEN** a hook for a provisioned administrator raises an exception
- **THEN** the server discards all membership changes requested during that sign-in, records a separate `login` event with outcome `hook_failed`, and allows the administrator to sign in with their existing memberships
- **AND** if the provisioned person is not an administrator, the server instead refuses sign-in with HTTP 503 and records a denied login
- **AND** the server logs only the exception type for the hook error and does not disclose its text to the person
