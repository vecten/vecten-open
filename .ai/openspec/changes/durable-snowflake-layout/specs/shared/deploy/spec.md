## MODIFIED Requirements

### Requirement: Snowflake layout per environment

Before applying any root, deploy SHALL check the saved Snowflake plan against the names already in its environment's Terraform state. A layout or shared-base change that would rename existing objects SHALL be refused. A successful deployment may record names in the checkout. A dispatch alone SHALL leave the local layout unset.

#### Scenario: Workflows launched from GitHub

- **WHEN** the first workflow created shared-account objects and another checkout proposes fixed names
- **THEN** CI refuses the saved plan before applying any root, including when the checkout has no layout record

#### Scenario: First workflow refused before apply

- **WHEN** a first workflow failed before creating a Snowflake object
- **THEN** the operator can correct the layout and run deploy again
