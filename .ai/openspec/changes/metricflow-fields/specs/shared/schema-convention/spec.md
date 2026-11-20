## ADDED Requirements

### Requirement: Generated fields and dbt metric compatibility

Generated semantic field names and primary-key references SHALL use lowercase. The ANSI SQL
expression SHALL preserve the declared physical column. A platform project's MCP validation
SHALL report dbt's field-name, primary-entity and cross-model naming conflicts when metrics
are present. Composite keys remain valid Ossie keys. Adding metrics to such a dbt project
requires a single tested key column per mart. Matching names alone SHALL NOT imply a join.

#### Scenario: First metric on a composite-key mart

- **WHEN** a platform project adds a metric and a mart has multiple primary entities
- **THEN** validation identifies the mart and explains how to model a single key
