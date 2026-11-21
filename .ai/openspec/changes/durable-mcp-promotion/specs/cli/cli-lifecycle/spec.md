## ADDED Requirements

### Requirement: Confirm shipment before MCP onboarding

For a deployed later environment, a new identity confirmation or changed MCP onboarding answer SHALL survive a checkout change as a pending promotion. Before offering commands that need the running MCP, next SHALL require a successful ship to that environment with the desired settings. A failed or skipped ship SHALL leave the promotion pending.

#### Scenario: Resume after pushing from another checkout

- **WHEN** a fresh checkout contains the MCP settings and prod identity confirmation pushed by an operator
- **THEN** next offers promotion until prod has shipped those settings, then resumes onboarding
