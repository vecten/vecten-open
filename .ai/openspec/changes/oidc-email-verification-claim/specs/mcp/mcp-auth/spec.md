## ADDED Requirements

### Requirement: Select the MCP identity provider's email verification claim

The built-in OIDC backend SHALL require the configured top-level ID token claim to equal
the JSON boolean `true`. The `[mcp] oidc_email_verified_claim` setting SHALL default to
`email_verified`. The server SHALL continue to validate the ID token and bind the user
by issuer and subject.

#### Scenario: A single-tenant provider uses another claim

- **WHEN** a valid ID token carries an email and `xms_edov: true` as its only email verification claim
- **THEN** MCP sign-in accepts it when the configured claim is `xms_edov` and refuses it with the default setting

#### Scenario: The selected claim is missing or has the wrong value

- **WHEN** the selected claim is absent, false, or has a non-boolean value
- **THEN** the callback refuses the sign-in, even when another claim is true
