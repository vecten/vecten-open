## ADDED Requirements

### Requirement: Declare Airflow resources

The stack SHALL support project and extension declarations of pools, variable defaults and read-only API accounts. The stack SHALL apply pool sizes and create missing variables on local up and deployed ship. Each viewer account SHALL reference a platform-scoped password secret declared by its owner. Removing the account SHALL remove the configured login.

#### Scenario: Existing environment starts with updated declarations

- **WHEN** an operator changes a variable and ships a release increasing the slots of a declared pool
- **THEN** the pool has the declared size and the variable retains the operator's value
- **AND** a declared API account can read the pool and cannot change it
