## ADDED Requirements

### Requirement: next asks whether the Snowflake account is shared

In a project with the `platform` component and a Snowflake warehouse, `next` SHALL report, right after the Snowflake account question and before `init --deploy`, an `ask` item: "Does this Snowflake account hold anything besides this one environment, such as another environment of this project or another team's data?", allowing `yes` and `no` and feeding `answer snowflake_shared_account`. `init --deploy` SHALL record the answer as `[warehouse] shared_account` in `vecten.toml`. `next` SHALL NOT ask it once any environment is deployed.

#### Scenario: Snowflake deploy questions

- **WHEN** a Snowflake project with the `platform` component has answered cloud, domain, GitHub and the Snowflake account
- **THEN** the next item is the shared-account question with kind `ask`, `yes` and `no` allowed and `snowflake_shared_account` as its answer key
- **AND** once it is answered, the next item is `init --deploy`

#### Scenario: Postgres project

- **WHEN** a Postgres project has answered every deploy question
- **THEN** no shared-account question is reported
