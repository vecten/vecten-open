## ADDED Requirements

### Requirement: Airflow Variable lookups are opt-in

Vecten syncs SHALL disable dlt's Airflow Variable provider by default and SHALL honor an explicit
`PROVIDERS__ENABLE_AIRFLOW_SECRETS` setting.

#### Scenario: Start a sync with default configuration

- **WHEN** a Vecten sync starts without an explicit setting
- **THEN** dlt skips Airflow Variables when resolving configuration and secrets

#### Scenario: Use configuration stored in Airflow Variables

- **WHEN** the project sets `PROVIDERS__ENABLE_AIRFLOW_SECRETS=true`
- **THEN** dlt can consult the Airflow Variable provider
