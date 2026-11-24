## ADDED Requirements

### Requirement: Deployed Airflow can use the project's OpenID Connect provider

When `[airflow] auth = "oidc"`, a deployed Airflow SHALL use the configured MCP issuer and administrator email list. It SHALL issue an administrator session only after verifying an ID token, its nonce and a verified email on the list. New projects and existing projects without this setting SHALL retain password login. The laptop SHALL retain password login in either mode.

#### Scenario: Administrator signs in with the configured provider

- **WHEN** a user completes authorization code with PKCE through the configured issuer with a verified administrator email
- **THEN** Airflow issues its normal secure UI cookie for that user

#### Scenario: The identity does not establish admission

- **WHEN** a verified email is absent or outside the administrator list, or the callback fails token or state validation
- **THEN** the user receives no Airflow session

#### Scenario: Automation reads Airflow in OIDC mode

- **WHEN** an API client supplies valid credentials at the Airflow token endpoint
- **THEN** it receives at most viewer access, even when the password previously belonged to the Airflow administrator
