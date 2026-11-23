## ADDED Requirements

### Requirement: Verify a shared-account binding's role hierarchy

In a shared Snowflake account the server SHALL verify a candidate's current and future grants
through its role hierarchy before binding or confirming it. It SHALL refuse a binding that reaches
a project environment role or a sibling environment's database, or whose grants it cannot verify.

#### Scenario: A role inherits the account-wide PUBLIC baseline

- **WHEN** every protected grant in the listing comes exclusively through the PUBLIC role
- **THEN** the binding check accepts the role
- **AND** the same grant reached through an explicit candidate membership is checked

#### Scenario: An inherited role can read another environment

- **WHEN** a candidate role for a dev group inherits a grant on the project's production database
- **THEN** binding is refused and the candidate is not recorded

#### Scenario: Grants change after confirmation

- **WHEN** a recorded binding acquires access to another environment
- **THEN** reconciliation identifies the group, bound role and reason in access health
- **AND** a successful check after the administrator corrects the grant clears the finding

#### Scenario: Visibility is incomplete

- **WHEN** the procedure owner lacks the account privilege to inspect all grants
- **THEN** binding and confirmation are refused and reconciliation reports affected bindings
