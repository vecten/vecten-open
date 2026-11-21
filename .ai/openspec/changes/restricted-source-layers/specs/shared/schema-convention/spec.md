## ADDED Requirements

### Requirement: Opt-in staging and snapshots for an area

An area with `restricted_source_layers = true` SHALL also own `staging_<area>` and `snapshots_<area>`. Project authors place models and snapshots explicitly using dbt schema configuration. Staging metadata and snapshot naming rules still apply. An area grant SHALL include those schemas only while the area opts in. Shared staging and snapshots remain independently grantable. Existing areas keep their two schemas.

#### Scenario: Public staging alongside private ledger history

- **WHEN** a group holds a grant on shared staging and another holds the opted-in finance area
- **THEN** the area group alone can read ledger rows placed in the finance staging and snapshots schemas
