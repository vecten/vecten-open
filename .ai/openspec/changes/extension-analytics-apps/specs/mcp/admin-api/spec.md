## ADDED Requirements

### Requirement: Apps are readable on the admin API and as an admin tool

The admin API SHALL offer a read operation returning the Analytics screen's list, and the same operation SHALL be an admin tool. Neither SHALL change an app's binding, which lives in `vecten.toml`.

#### Scenario: Agent asks which dashboards exist

- **WHEN** an admin asks their agent which analytics apps are installed and who can open them
- **THEN** the agent calls the admin tool and answers with each app's title, URL and admitted groups
