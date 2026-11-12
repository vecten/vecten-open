## ADDED Requirements

### Requirement: Sources name the project's database
A dataset `source` whose database part is the project's database name, `[warehouse] database` or the project name, SHALL be read on Snowflake as the environment's database whenever the server checks the source, compiles SQL against it or grants on it, so one commit of `osi/` serves every environment. A source naming any other database SHALL be read as written. `describe` SHALL show the source with the environment's database.

#### Scenario: Same model in two environments of one account
- **WHEN** `osi/datasets.json` names the source `ACME.MARTS.ORDERS` and is shipped to `dev` and `prod` of a project whose environments share an account
- **THEN** `dev`'s server checks, queries and grants `ACME_DEV.MARTS.ORDERS`, and `prod`'s `ACME_PROD.MARTS.ORDERS`

#### Scenario: Fixed names
- **WHEN** the same model is shipped to an environment with fixed names
- **THEN** the server reads `ACME.MARTS.ORDERS` as written
