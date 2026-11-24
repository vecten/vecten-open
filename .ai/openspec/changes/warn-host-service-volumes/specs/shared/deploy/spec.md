## ADDED Requirements

### Requirement: Warn before removing a host with bound service volumes

Deploy SHALL identify bound services with named Docker volumes on each host the saved cloud plan deletes or replaces. It SHALL name the host and the volumes in the plan report and before applying, including when a profile change removes the old host. Creates and in-place updates SHALL produce no volume-loss warning.

#### Scenario: Replace a shared host

- **WHEN** the saved plan replaces the minimal profile's main host and services on either component declare volumes
- **THEN** the operator sees those volumes in the plan text and deploy warnings before Terraform applies the plan

#### Scenario: Replace only one component host

- **WHEN** the plan replaces the platform host and updates the MCP host in place
- **THEN** the warning lists only the platform host's bound service volumes
