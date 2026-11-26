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

## MODIFIED Requirements

### Requirement: Airflow's own login guards the Airflow host

NGINX SHALL pass every path on the Airflow host to Airflow without HTTP authentication of its own, and Airflow's auth manager SHALL require its login for the UI and the API; with password authentication its admin password SHALL come from the secrets provider, not from a file in the project. Airflow's health path `/api/v2/monitor/health` answers without a login, so the ship health poll reads Airflow's own verdict; it answers component states only. The MCP host SHALL NOT add HTTP authentication either, because the MCP server authenticates with its own OAuth flow. NGINX basic auth MUST NOT sit in front of Airflow: Airflow 3's UI sends its token in the `Authorization` header, which basic auth reads as missing credentials, so a browser would be challenged again on every call.

#### Scenario: No credentials

- **WHEN** `https://airflow.<domain>/api/v2/dags` is requested without a token
- **THEN** Airflow answers 401, and `https://airflow.<domain>/` serves Airflow's sign-in

#### Scenario: Health path without credentials

- **WHEN** `https://airflow.<domain>/api/v2/monitor/health` is requested without credentials
- **THEN** Airflow's health answer is served

#### Scenario: Browser sign-in

- **WHEN** Airflow uses password authentication (the default, or `[airflow] auth = "password"`) and a person opens `https://airflow.<domain>/` and signs in as `admin` with `AIRFLOW_ADMIN_PASSWORD`
- **THEN** the UI loads and stays signed in, with no further browser credential prompt

#### Scenario: Credential source

- **WHEN** Airflow uses password authentication and the Airflow admin password is rotated in the secrets provider
- **THEN** the next stack start uses the new password with no project file changed

#### Scenario: MCP host without basic auth

- **WHEN** `https://mcp.<domain>/` is requested without credentials
- **THEN** the MCP server's own login page is served, not a 401 from NGINX
