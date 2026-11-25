## ADDED Requirements

### Requirement: Airflow uses the project's OIDC email verification claim

When deployed Airflow uses OIDC, its callback SHALL require the JSON boolean `true`
in the claim named by `[mcp] oidc_email_verified_claim` before matching the email against
`[mcp] admins`.

#### Scenario: A configured administrator signs in with Entra

- **WHEN** a valid ID token contains the listed administrator's email and `xms_edov: true` and the project selects `xms_edov`
- **THEN** Airflow grants an administrator session

#### Scenario: Another verification claim is present

- **WHEN** the selected claim is missing or is not the boolean `true`
- **THEN** Airflow refuses sign-in even if the other supported claim is true
