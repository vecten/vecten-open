## MODIFIED Requirements

### Requirement: Base images are published, the project layer is built on the VM

Vecten SHALL publish the Airflow and MCP base images to a public registry at each release, pinned in the versions table, each tag holding linux/amd64 and linux/arm64. `ship` SHALL build the thin project layer on the VM on top of the base image. For each image the layer SHALL first install, as root, the operating-system packages `[dependencies.<component>] system` declares, through the base image's own package manager, and SHALL then install, as the image's own user, the Python packages that component's lock pins: `requirements-airflow.lock` for the Airflow image and `requirements-mcp.lock` for the MCP image. A project with no lock files and no `[dependencies]` Python requirement SHALL install its connectors' dependencies unlocked, as before; one that declares a `[dependencies]` Python requirement without lock files SHALL be refused, naming `vecten lock`. The build SHALL refuse a lock whose recorded inputs differ from what `vecten.toml`, `requirements.txt` and `extensions.lock.json` now give, naming `vecten lock`. A lock without a recorded inputs line is checked by the earlier name-by-name rule. No per-project registry SHALL be required.

The MCP base image SHALL hold every third-party distribution at the version the repository's lockfile pins, the versions CI tests against, and its build SHALL fail rather than resolve anew when the lockfile does not match the workspace's package metadata. The Airflow base image SHALL resolve the platform packages against the distributions its pinned upstream image installs, since the lockfile holds no Airflow. Neither image's build SHALL leave behind an installer that the image would not hold without it; the MCP image SHALL keep pip, which the project layer installs with. The project layer SHALL leave no package-manager index behind.

#### Scenario: Extension added

- **WHEN** a project adds an extension and ships
- **THEN** the VM builds a project layer holding it on top of the pinned base image and the profile starts with it

#### Scenario: Different packages per image

- **WHEN** a project's `[dependencies.platform] python` lists `polars` and its `[dependencies.mcp] python` lists `rapidfuzz`, and it ships after `vecten lock`
- **THEN** the Airflow image's layer holds `polars` and not `rapidfuzz`, and the MCP image's holds `rapidfuzz` and not `polars`

#### Scenario: Operating-system package

- **WHEN** `[dependencies.platform] system = ["libgomp1"]` and the project ships
- **THEN** the Airflow image's layer holds `libgomp1`, installed before the Python packages, no apt list files remain, and the Airflow services run as the image's own user

#### Scenario: Invalid system package name

- **WHEN** `[dependencies.platform] system` holds `"libgomp1; curl evil.example"`
- **THEN** loading `vecten.toml` fails with the configuration error code naming the entry, and no Dockerfile is written

#### Scenario: Stale lock

- **WHEN** a person adds a package to `[dependencies.mcp] python` and runs `vecten up` without `vecten lock`
- **THEN** `up` exits non-zero naming `requirements-mcp.lock` and `vecten lock`, and nothing is built

#### Scenario: Laptop on Apple Silicon

- **WHEN** a person runs `vecten up` from the published package on an arm64 Mac, with no checkout and no locally built base images
- **THEN** the project layer builds on the arm64 image of the pinned base image and the stack starts

#### Scenario: Published tag missing a platform

- **WHEN** a release's published `vecten-airflow` or `vecten-mcp` tag lacks linux/amd64 or linux/arm64
- **THEN** the release check exits non-zero, naming the tag and the platform it lacks, and the release does not proceed

#### Scenario: MCP image holds the locked versions

- **WHEN** the MCP base image built from a commit is inspected
- **THEN** every installed distribution other than `vecten-mcp`, `vecten-shared` and pip is at the version that commit's lockfile pins

#### Scenario: Stale lockfile

- **WHEN** the MCP base image is built from a checkout whose lockfile no longer matches a member's declared dependencies
- **THEN** the build fails, naming the lockfile, and no image is tagged

#### Scenario: No installer left behind

- **WHEN** either base image is inspected
- **THEN** the MCP image holds no uv, and the only uv in the Airflow image is the one its upstream image ships
