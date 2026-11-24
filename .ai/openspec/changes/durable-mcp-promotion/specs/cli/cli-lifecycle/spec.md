## ADDED Requirements

### Requirement: Confirm shipment before MCP onboarding

For a deployed later environment, a new identity confirmation or changed MCP onboarding answer SHALL survive a checkout change as a pending promotion. Before offering commands that need the running MCP, next SHALL require a successful ship to that environment with the desired settings. A failed or skipped ship SHALL leave the promotion pending.

#### Scenario: Resume after pushing from another checkout

- **WHEN** a fresh checkout contains the MCP settings and prod identity confirmation pushed by an operator
- **THEN** next offers `vecten promote --status` to check the receipt, then offers promotion if the settings are pending

#### Scenario: Previously verified shipment offline

- **WHEN** the operator has verified a matching ship and GitHub becomes unavailable or the ship ages out of the recent history
- **THEN** next uses the verified settings in the checkout and continues normal onboarding without contacting GitHub
- **AND** a changed answer or verified rollback gates commands that require the running MCP again

#### Scenario: Established environment from an earlier release

- **WHEN** an environment already has its first MCP user and has no pending settings or identity change
- **THEN** next continues ordinary post-deploy guidance even without a ship in recent history
