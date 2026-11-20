## ADDED Requirements

### Requirement: Parse project datasets in the selected environment's database

When the Snowflake target database differs from the project's generated database,
`vecten dbt --env` SHALL give both its project preflight and dbt command a temporary
copy of the project's Ossie documents with matching three-part sources qualified for
that target. Other sources and all metrics SHALL be preserved.

#### Scenario: A shared-account environment parses a mart

- **WHEN** the committed source is SHOP.marts.customers and the environment uses SHOP_DEV
- **THEN** dbt parses SHOP_DEV.marts.customers and the committed document keeps its original bytes
