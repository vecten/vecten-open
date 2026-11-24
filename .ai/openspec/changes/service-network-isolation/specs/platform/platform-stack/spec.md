## MODIFIED Requirements

### Requirement: Services are isolated from the stack

Each running extension service and container app SHALL join its own Docker network.
Only NGINX SHALL also join that network. The service SHALL publish no host port. Its
environment SHALL contain only its declared secrets within the scopes its host may read and
SHALL exclude cloud credentials and Airflow connections. The shared read API SHALL read
its secrets from its environment. On a VM, the agent SHALL block access from service bridges
to the cloud metadata address.

#### Scenario: Two services on one host

- **WHEN** the project renders a laptop or VM stack with two running services
- **THEN** NGINX joins the default network and both service networks
- **AND** each service joins only its own network

## ADDED Requirements

### Requirement: Clear incoming viewer attribution on service hostnames

NGINX SHALL clear `X-Vecten-On-Behalf-Of` when forwarding a service hostname, including
login, public, gate and health paths.

#### Scenario: An app reports its viewer

- **WHEN** the gate admits a signed-in viewer
- **THEN** the app receives the gate's identity and can set viewer attribution on its own
  authenticated request to the MCP endpoint
