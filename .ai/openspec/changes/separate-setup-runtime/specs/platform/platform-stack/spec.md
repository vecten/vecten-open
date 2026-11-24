## MODIFIED Requirements

### Requirement: One runtime write, scoped per VM

MCP SHALL be the sole component that writes secrets during normal operation. The cloud SHALL allow a runtime identity to write only MCP_SIGNING_KEY and MCP_IMPERSONATOR_PASSWORD, and only on a host running MCP. On a minimal VM both components use that host identity and share its grants. The runtime identity SHALL read only the enabled components' secret scopes on its VM and write objects only under their bucket prefixes. Database setup and first creation of Airflow secrets SHALL use the setup identity from `shared/deploy`. Airflow task logs SHALL go under `s3://<bucket>/platform/logs` on both clouds, using the S3-compatible endpoint on Scaleway.

#### Scenario: DAG asks for an MCP secret

- **WHEN** a DAG on a dedicated platform VM asks for the MCP provisioner credential
- **THEN** the cloud refuses the read

#### Scenario: Task logs on a VM

- **WHEN** an Airflow task writes a remote log and a later process reads it
- **THEN** both succeed with runtime credentials, including after cloud credential renewal

#### Scenario: MCP maintains its credentials

- **WHEN** MCP initializes its signing key or rotates the impersonator password
- **THEN** the runtime identity can persist the required value and any rotation envelope
- **AND** a request to overwrite the provisioner credential using that identity is refused
