## ADDED Requirements

### Requirement: The platform host reaches dbt package sources

The host that runs the dbt DAG SHALL be able to open outbound HTTPS connections to dbt hub (`hub.getdbt.com`) and GitHub (`github.com` and `codeload.github.com`), and its image SHALL carry `git`, because the DAG's `deps` task installs the project's own packages from `dbt/project-packages.yml` on every run, into a scratch directory a ship empties. Both clouds' modules already allow every outbound connection from the hosts; a change that narrows egress SHALL keep these destinations.

#### Scenario: Hub and git packages on the host

- **WHEN** `dbt/project-packages.yml` lists a pinned hub package and a pinned public git package and the dbt DAG runs on a deployed host
- **THEN** its `deps` task installs both, and `build` uses them
