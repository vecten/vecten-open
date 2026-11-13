## MODIFIED Requirements

### Requirement: Platform issues resolve on green, gaps on the swap

A platform issue SHALL be considered resolved in an environment when its source runs green there at or after the commit that carries the fix. Under a promotion order a fix reaches a later environment only once that commit is promoted to it, so the issue stays listed there until then. A gap SHALL resolve as the feedback-loop spec states.

#### Scenario: Fix shipped and green

- **WHEN** a connector fix merges, ships, and the next scheduled run of that DAG succeeds
- **THEN** the issue no longer appears in `issues`

#### Scenario: Fix not yet promoted

- **WHEN** `[deploy] promotion = ["dev", "prod"]` and a connector fix has merged, shipped to `dev` and run green there, and has not been promoted
- **THEN** `issues --env dev` no longer lists the issue and `issues --env prod` still does
