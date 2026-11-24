## ADDED Requirements

### Requirement: Declare Airflow resources

The stack SHALL support project and extension declarations of pools, variable defaults and read-only API accounts. The stack SHALL apply pool sizes and create missing variables on local up and deployed ship. Each viewer account SHALL reference a platform-scoped password secret declared by its owner. Removing the account SHALL prevent new logins. Tokens already issued stay valid until they expire. Updating pool slots SHALL preserve operator settings for the pool description and deferred tasks.

#### Scenario: Existing environment starts with updated declarations

- **WHEN** an operator changes a variable and ships a release increasing the slots of a declared pool
- **THEN** the pool has the declared size and the variable retains the operator's value
- **AND** a declared API account can read the pool and cannot change it

#### Scenario: A declared viewer password is missing

- **WHEN** a local up or ship finds no password for a declared viewer
- **THEN** it SHALL refuse before starting the new stack and name the account and secret
- **AND** a failed ship SHALL keep the previous release serving
- **WHEN** the VM agent starts the current release outside a ship without that password
- **THEN** it SHALL log the account and secret names, omit the account, and start the stack
