## MODIFIED Requirements

### Requirement: The VM agent isolates services and hands them their secrets

The VM agent SHALL enforce the cloud metadata drop rule for every generated service
bridge before services start on ship and before Docker starts at boot. It SHALL continue to accept
earlier host records that specify one exact bridge name or have no service network. An agent that cannot install the
rule SHALL fail the isolation step. The host record's `service_secrets` list SHALL retain
its empty default. The agent SHALL read the permitted secrets from their declared scopes,
pass them to Compose through the environment and mask them in ship output. The agent SHALL
use the Python 3.9 standard library and its Terraform copy SHALL be byte-identical.

#### Scenario: New and previous bridges during an upgrade

- **WHEN** a bundle specifies the service bridge prefix
- **THEN** the guard installs a rule covering new service bridges and the previous shared bridge
- **AND** the default stack bridge stays outside the rule
