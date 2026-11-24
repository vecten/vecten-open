## ADDED Requirements

### Requirement: Independent service gate

A project with a bound login route SHALL run the service-host gate in a separate container using the MCP image. The container SHALL have the state database credential and a dedicated network, receive no warehouse or provider secrets, and run on a bridge guarded against metadata access on a VM. Session verification SHALL continue while the MCP process restarts.

#### Scenario: The MCP process stops

- **WHEN** a service receives a valid gate session while the MCP process is down
- **THEN** the gate checks the current state and forwards the same authorized viewer identity

#### Scenario: A new browser signs in

- **WHEN** the browser starts a service login
- **THEN** the gate hands it to the issuer and redeems the resulting one-time code at the service hostname
