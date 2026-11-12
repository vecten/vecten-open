## MODIFIED Requirements

### Requirement: Content ships on merge without Terraform

A rendered ship workflow SHALL run on every push to main once the environment is deployed. It SHALL run `vecten ship --env <env>`, which builds one content bundle, the DAGs, the dbt project, `osi/`, `evals/` and the in-repo extensions under `extensions/` at that commit, ships it to every component host through the cloud's `ship` mechanism, and runs no Terraform and creates nothing billable. The bundle SHALL record its commit, and every host SHALL keep the previous bundle to roll back to when the health poll fails.

#### Scenario: Merged metric fix reaches both hosts

- **WHEN** a pull request editing `osi/metrics.json` and a dbt mart merges to main
- **THEN** the ship workflow places the bundle at that commit on the platform host and the MCP host without anyone dispatching `deploy`

#### Scenario: Merged extension change reaches both hosts

- **WHEN** a pull request editing a module of `extensions/acme-portal` merges to main
- **THEN** the ship workflow places a bundle holding `extensions/acme-portal` at that commit on every host, and each host's images run it

#### Scenario: Ship before deploy

- **WHEN** a push to main happens before `deploy --env prod` has ever run
- **THEN** the ship workflow exits without shipping and says the environment is not deployed

#### Scenario: Health poll fails after ship

- **WHEN** a shipped bundle leaves a component's URL unanswered or not healthy (Airflow: HTTP 200 with a healthy metadatabase; the MCP: HTTP 200 from `/health`)
- **THEN** the host restores the previous bundle and the workflow fails naming the component

#### Scenario: Agent fails after the swap

- **WHEN** the VM agent fails after it has pointed `current` at the new bundle
- **THEN** the agent itself restores the previous bundle and NGINX, and the workflow fails naming the step and the commit it rolled back to

#### Scenario: A container fails at startup

- **WHEN** the MCP container exits at startup and restarts in a loop, so `compose up` fails
- **THEN** `vecten ship` and the workflow print, after the failure, the last log lines of each failing container with its service name, every secret value the agent injected masked and the rest redacted, without anyone opening a session on the VM

#### Scenario: Compose fails before a container runs

- **WHEN** `docker compose up` fails on a host before any container is failing, as when an image cannot be pulled or built
- **THEN** `vecten ship` and the workflow print, after the failure, the last lines of compose's own output, masked and redacted the same way

## ADDED Requirements

### Requirement: In-repo extensions are built on the host from the bundle

When a project has in-repo extensions, the generated Compose file SHALL hand the bundle's `extensions/` directory to each project-layer build as an additional build context, and the generated Dockerfile SHALL install them from that context without copying them into a layer. The VM agent, `host.json` and the base images SHALL be unchanged by in-repo extensions, and no host SHALL need a credential to install them. A project without in-repo extensions SHALL get the same Compose file and Dockerfiles as before this change.

#### Scenario: arm64 host

- **WHEN** a project with an in-repo extension ships from an amd64 runner to an arm64 VM
- **THEN** the VM builds and installs the extension from the bundled source, and the profile starts

#### Scenario: Index-only projects unchanged

- **WHEN** a project with no `extensions/` directory ships
- **THEN** its bundle, Compose file and Dockerfiles are the same as before this change
