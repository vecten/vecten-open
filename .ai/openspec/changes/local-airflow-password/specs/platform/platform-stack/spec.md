## ADDED Requirements

### Requirement: Local Airflow password lives in memory

The laptop SHALL persist the Airflow admin password only in the project's `.env`. Airflow SHALL receive its password file on memory-backed storage. Starting an existing project SHALL remove its old generated host password file.

#### Scenario: Rotate a local password

- **WHEN** a person changes the admin password in `.env` and restarts the local stack with `down` and `up`
- **THEN** the UI and loopback API accept the new password and refuse the old password
