## ADDED Requirements

### Requirement: Validate project Terraform roots in CI

The generated CI workflow SHALL check formatting, initialize without a backend and validate each project Terraform root before bootstrap, using the Terraform version selected for deployment.

#### Scenario: Project adds a Snowflake grant with a bad reference

- **WHEN** a pull request adds a reference to an undeclared resource in a Snowflake root
- **THEN** CI fails Terraform validation without cloud credentials

#### Scenario: Multiple roots need repairs

- **WHEN** one root has formatting changes and another has a validation error
- **THEN** one CI run names both roots, shows the formatting diff, and fails after checking all roots

#### Scenario: Laptop-only project

- **WHEN** the checkout contains no project Terraform roots
- **THEN** CI skips Terraform setup and validation
