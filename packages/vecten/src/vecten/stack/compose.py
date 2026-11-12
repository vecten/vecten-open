"""The Compose stack, generated from vecten.toml; never a file in the project."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

import yaml

from vecten.stack.images import BUILD_DIR
from vecten_platform.dbt.artifacts import ARTIFACT_DIR_VAR, DEFAULT_ARTIFACT_DIR
from vecten_shared.config import VectenConfig, stack_project_name
from vecten_shared.declarations import component_for
from vecten_shared.extension_record import RECORD_FILE, Extension, read_record
from vecten_shared.names import SECRET_NAME, SECRET_RULE
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.environment import EnvironmentOutputs
from vecten_shared.provider.local import local_pg_published_port
from vecten_shared.sql import local_init_script_path, setup_sql_path
from vecten_shared.versions import VERSIONS

STACK_DIR = Path(".vecten") / "stack"
# The laptop's dbt results live in the project, where `vecten issues` reads them; Compose runs
# from the stack directory, so the mount source is relative to it.
LOCAL_ARTIFACTS = Path(".vecten") / "artifacts"
LOCAL_ARTIFACTS_SOURCE = "../artifacts"
LOCAL_HTTPS_PORT = 8443
# Not 8080: that is Airflow's own default host port, so another local Airflow likely holds it.
LOCAL_HTTP_PORT = 8480
# Plain HTTP on loopback: the Airflow MCP server signs in to Airflow's own auth manager here,
# which the self-signed certificate in front of 8443 would block.
LOCAL_AIRFLOW_API_PORT = 8482
# On a VM the secret files live on tmpfs, rebuilt by the agent on every ship and at boot.
VM_RUN_DIR = "/run/vecten"
VM_ENVIRONMENT_FILE = "/opt/vecten-env/environment.json"
# The release holds the outputs under .vecten/environments, one level above the stack.
VM_ENVIRONMENT_MOUNT = f"../environments/${{VECTEN_ENV}}.json:{VM_ENVIRONMENT_FILE}:ro"
PASSWORDS_FILE_TARGET = "/opt/airflow/simple_auth_manager_passwords.json.generated"
# Project directories mounted into Airflow, relative to the stack directory, with the
# stand-in under the stack directory that is mounted when the project lacks one.
PROJECT_MOUNTS = (
    ("../../orchestration/dags", "dags", "/opt/airflow/dags/project"),
    ("../../dbt", "dbt", "/opt/airflow/dbt"),
    ("../../connectors", "connectors", "/opt/airflow/connectors"),
)


# The key under which NGINX and Compose look up each component's public name.
HOSTNAME_KEYS = {"platform": "airflow", "mcp": "mcp"}


def local_hostnames() -> dict[str, str]:
    return {
        "airflow": f"airflow.localhost:{LOCAL_HTTPS_PORT}",
        "mcp": f"mcp.localhost:{LOCAL_HTTPS_PORT}",
    }


def vm_hostnames(outputs: EnvironmentOutputs) -> dict[str, str]:
    """Each component's public name as the environment's Terraform outputs give it.

    The domain is set per environment in its tfvars, and Route 53 and certbot serve these
    names; vecten.toml's domain holds for `prod` only.
    """
    names: dict[str, str] = {}
    for key, host in outputs.hosts.items():
        if len(host.components) != len(host.hostnames):
            raise Failure(
                ExitCode.INTERNAL,
                f"host {key} in the outputs pairs components {host.components} with "
                f"hostnames {host.hostnames}",
            )
        for component, name in zip(host.components, host.hostnames, strict=True):
            if component not in HOSTNAME_KEYS:
                raise Failure(ExitCode.INTERNAL, f"host {key} serves unknown {component!r}")
            names[HOSTNAME_KEYS[component]] = name
    return names


def _hosts(mode: str, hosts: dict[str, str] | None) -> dict[str, str]:
    if mode == "local":
        return local_hostnames()
    if hosts is None:
        raise Failure(ExitCode.INTERNAL, "a vm stack takes its hostnames from the outputs")
    return hosts


def compose_project_name(project_root: Path, config: VectenConfig) -> str:
    return stack_project_name(project_root, config.project.name)


def _project_mounts(stack_dir: Path) -> list[str]:
    mounts = []
    for source, empty, target in PROJECT_MOUNTS:
        if not (stack_dir / source).is_dir():
            source = f"./empty/{empty}"
        mounts.append(f"{source}:{target}")
    return mounts


def _declared(config: VectenConfig, record: dict[str, Extension]) -> set[str]:
    names = set(config.secrets)
    for entry in record.values():
        names.update(entry.secrets)
    return names


def platform_secrets(config: VectenConfig, record: dict[str, Extension]) -> list[str]:
    """The names delivered to Airflow: connectors', and declared names outside `MCP_`."""
    names = {s for c in config.connectors for s in c.secrets}
    names.update(n for n in _declared(config, record) if component_for(n) == "platform")
    return sorted(names)


def mcp_secrets(config: VectenConfig, record: dict[str, Extension]) -> list[str]:
    return sorted(n for n in _declared(config, record) if component_for(n) == "mcp")


def project_record(stack_dir: Path) -> dict[str, Extension]:
    return read_record(stack_dir.parent.parent) or {}


def _record_mount(stack_dir: Path, target_dir: str) -> list[str]:
    # Compose creates a directory for a bind source that does not exist, so only a project that
    # has a record mounts it.
    if not (stack_dir.parent.parent / RECORD_FILE).is_file():
        return []
    return [f"../../{RECORD_FILE}:{target_dir}/{RECORD_FILE}:ro"]


def _add_secrets(environment: dict, names: list[str], kind: str) -> None:
    for secret in names:
        if secret in environment:
            continue
        # The name lands inside `${...}`, which Compose would misread with any other character.
        if not SECRET_NAME.fullmatch(secret):
            raise Failure(ExitCode.CONFIG, f"{kind} secret {secret!r} must be {SECRET_RULE}")
        if secret.startswith("AIRFLOW__"):
            raise Failure(
                ExitCode.CONFIG,
                f"{kind} secret {secret} collides with a stack variable; rename the secret",
            )
        # `${NAME:-}` lets the stack start before a secret is set; the run then names it.
        environment[secret] = f"${{{secret}:-}}"


def _build(mode: str, stack_dir: Path, dockerfile: str) -> dict:
    # A bundle is rendered on another machine than the VM it runs on, so its context is
    # relative to the stack directory Compose runs from.
    context = f"./{BUILD_DIR}" if mode == "vm" else str(stack_dir / BUILD_DIR)
    return {"context": context, "dockerfile": dockerfile}


# A Scaleway VM has no instance identity, so the agent hands its containers the VM's key. The
# values exist only in the environment the agent gives Compose; the file holds references.
SCALEWAY_RUNTIME_VARS = (
    "SCW_ACCESS_KEY",
    "SCW_SECRET_KEY",
    "SCW_DEFAULT_REGION",
    "SCW_DEFAULT_PROJECT_ID",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ENDPOINT_URL_S3",
)


def _cloud(config: VectenConfig) -> str | None:
    return config.deploy.cloud if config.deploy else None


def _vm_runtime_env(cloud: str | None) -> dict:
    env = {
        "AWS_REGION": "${AWS_REGION}",
        "AWS_DEFAULT_REGION": "${AWS_REGION}",
        "VECTEN_ENVIRONMENT_FILE": VM_ENVIRONMENT_FILE,
        "VECTEN_BUNDLE_COMMIT": "${VECTEN_BUNDLE_COMMIT}",
        "VECTEN_ARTIFACT_URL": "${VECTEN_ARTIFACT_URL}",
    }
    if cloud == "scaleway":
        env |= {var: f"${{{var}}}" for var in SCALEWAY_RUNTIME_VARS}
    return env


def _artifacts_source(mode: str) -> str:
    return LOCAL_ARTIFACTS_SOURCE if mode == "local" else "airflow-artifacts"


def _airflow_common(
    config: VectenConfig,
    mode: str,
    stack_dir: Path,
    project: str,
    hosts: dict[str, str],
    dags_paused_at_creation: bool,
) -> dict:
    db = (
        "postgresql+psycopg2://airflow:${AIRFLOW_DB_PASSWORD}@postgres/airflow"
        if mode == "local"
        else "${AIRFLOW_DATABASE_URL}"
    )
    common = {
        "build": _build(mode, stack_dir, "airflow.Dockerfile"),
        "image": f"{project}-airflow:{VERSIONS.vecten}",
        "profiles": ["platform"],
        "environment": {
            "AIRFLOW__CORE__EXECUTOR": "LocalExecutor",
            "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
            # The laptop's DAGs always start paused, where a schedule would sync from the
            # human's machine. A deployed environment has no step that unpauses a DAG, so its
            # DAGs start paused only when its tfvars opt in, leaving each to a person to unpause.
            "AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION": str(
                mode == "local" or dags_paused_at_creation
            ),
            "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": db,
            "AIRFLOW__CORE__FERNET_KEY": "${AIRFLOW_FERNET_KEY}",
            "AIRFLOW__API__SECRET_KEY": "${AIRFLOW_API_SECRET_KEY}",
            "AIRFLOW__API_AUTH__JWT_SECRET": "${AIRFLOW_JWT_SECRET}",
            "AIRFLOW__API__BASE_URL": f"https://{hosts['airflow']}",
            "AIRFLOW__CORE__EXECUTION_API_SERVER_URL": "http://airflow-api-server:8080/execution/",
            "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS": "${AIRFLOW_ADMIN_USER}:admin",
            "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE": PASSWORDS_FILE_TARGET,
            "VECTEN_PROVIDER": mode if mode == "local" else "${VECTEN_PROVIDER}",
            "VECTEN_ENV": "local" if mode == "local" else "${VECTEN_ENV}",
            "VECTEN_COMPONENT": "platform",
            "FORWARDED_ALLOW_IPS": "*",
        },
        "volumes": [
            *_project_mounts(stack_dir),
            "../../vecten.toml:/opt/airflow/vecten.toml:ro",
            *_record_mount(stack_dir, "/opt/airflow"),
            "./extension_dags.py:/opt/airflow/dags/vecten_extensions.py:ro",
            # dbt reads the semantic model from the osi directory beside the dbt project.
            "../../osi:/opt/airflow/osi:ro",
            "airflow-logs:/opt/airflow/logs",
            f"{_artifacts_source(mode)}:{DEFAULT_ARTIFACT_DIR}",
        ],
        "restart": "unless-stopped",
    }
    runtime_env = {
        "LOADER_PASSWORD": "${LOADER_PASSWORD}",
        "ALERT_WEBHOOK_URL": "${ALERT_WEBHOOK_URL:-}",
        "VECTEN_PROJECT_ROOT": "/opt/airflow",
        "VECTEN_INSIDE_COMPOSE": "1",
        # The DAG's dlt state is restored from the warehouse, so its working directory is scratch.
        "VECTEN_DLT_DIR": "/tmp/vecten-dlt",
        "DLT_DATA_DIR": "/tmp/vecten-dlt/data",
        "RUNTIME__DLTHUB_TELEMETRY": "false",
        ARTIFACT_DIR_VAR: DEFAULT_ARTIFACT_DIR,
    }
    common["environment"] |= runtime_env
    if mode == "vm":
        cloud = _cloud(config)
        common["environment"] |= _vm_runtime_env(cloud) | {
            "VECTEN_BUNDLE_LINEAGE": "/opt/airflow/build-lineage.json",
            "AIRFLOW__CORE__PARALLELISM": "${AIRFLOW_PARALLELISM}",
            "AIRFLOW__LOGGING__REMOTE_LOGGING": "True",
            "AIRFLOW__LOGGING__REMOTE_BASE_LOG_FOLDER": "${AIRFLOW_REMOTE_BASE_LOG_FOLDER}",
            # On AWS no connection has this id, so the Amazon provider falls back to the
            # instance role; on Scaleway the agent defines it with the key and S3 endpoint.
            "AIRFLOW__LOGGING__REMOTE_LOG_CONN_ID": "aws_default",
        }
        if cloud == "scaleway":
            common["environment"]["AIRFLOW_CONN_AWS_DEFAULT"] = "${AIRFLOW_CONN_AWS_DEFAULT}"
        common["volumes"] += [
            "../ship/build-lineage.json:/opt/airflow/build-lineage.json:ro",
            f"{VM_RUN_DIR}/airflow_passwords.json:{PASSWORDS_FILE_TARGET}",
            VM_ENVIRONMENT_MOUNT,
        ]
    else:
        common["volumes"].append(f"./airflow_passwords.json:{PASSWORDS_FILE_TARGET}")
    if mode == "local":
        # The passwords file is 0600 and owned by the host user, so Airflow must run as that
        # uid; the official image supports an arbitrary uid only with gid 0.
        common["user"] = f"{os.getuid()}:0"
    # Checked against every variable the stack sets, the VM's included: the agent hands secrets
    # to Compose after the stack's own values, so a colliding name would replace them.
    secrets = platform_secrets(config, project_record(stack_dir))
    for secret in secrets:
        if secret in common["environment"] or secret in SCALEWAY_RUNTIME_VARS:
            raise Failure(
                ExitCode.CONFIG,
                f"declared secret {secret} collides with a stack variable; rename the secret",
            )
    _add_secrets(common["environment"], secrets, "declared")
    return common


def _postgres(config: VectenConfig) -> dict:
    with_mcp = config.has("mcp")
    environment = {
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "${POSTGRES_MASTER_PASSWORD}",
        "POSTGRES_DB": "postgres",
        "AIRFLOW_DB_PASSWORD": "${AIRFLOW_DB_PASSWORD}",
        "LOADER_PASSWORD": "${LOADER_PASSWORD}",
        "VECTEN_WITH_MCP": "true" if with_mcp else "false",
    }
    if with_mcp:
        environment |= {
            "MCP_STATE_DB_PASSWORD": "${MCP_STATE_DB_PASSWORD}",
            "PROVISIONER_PASSWORD": "${PROVISIONER_PASSWORD}",
            "IMPERSONATOR_PASSWORD": "${IMPERSONATOR_PASSWORD}",
        }
    return {
        "image": f"postgres:{VERSIONS.postgres_major}",
        "profiles": ["local"],
        "command": ["postgres", "-c", "log_line_prefix=%m [%p] %u@%d app=%a "],
        "environment": environment,
        "ports": [f"127.0.0.1:{local_pg_published_port()}:5432"],
        "volumes": [
            "postgres-data:/var/lib/postgresql/data",
            "./pg-init:/docker-entrypoint-initdb.d:ro",
        ],
        "healthcheck": {
            # During first-boot init the entrypoint's temporary server listens on the unix
            # socket only, so a TCP probe passes only once init has finished.
            "test": ["CMD", "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
            "interval": "5s",
            "timeout": "5s",
            "retries": 20,
        },
        "restart": "unless-stopped",
    }


def generate_compose(
    config: VectenConfig,
    mode: Literal["local", "vm"],
    host_components: list[str],
    stack_dir: Path,
    project: str | None = None,
    hosts: dict[str, str] | None = None,
    dags_paused_at_creation: bool = False,
) -> dict:
    """The Compose document; a vm stack passes `hosts` from `vm_hostnames(outputs)`.

    `dags_paused_at_creation` is the environment's tfvars setting; a local stack starts its
    DAGs paused whatever it says.
    """
    hosts = _hosts(mode, hosts)
    services: dict[str, dict] = {}
    volumes: dict[str, dict] = {}
    with_platform = "platform" in host_components
    with_mcp = "mcp" in host_components
    if project is None:
        # PROJECT_MOUNTS already fixes the stack directory two levels below the project root.
        project = compose_project_name(stack_dir.parent.parent, config)

    if mode == "local":
        services["postgres"] = _postgres(config)
        volumes["postgres-data"] = {}

    if with_platform:
        common = _airflow_common(config, mode, stack_dir, project, hosts, dags_paused_at_creation)
        depends = {"postgres": {"condition": "service_healthy"}} if mode == "local" else {}
        services["airflow-api-server"] = {
            **common,
            **({"ports": [f"127.0.0.1:{LOCAL_AIRFLOW_API_PORT}:8080"]} if mode == "local" else {}),
            "depends_on": depends,
            "command": [
                "bash",
                "-c",
                "airflow db migrate && exec airflow api-server --proxy-headers",
            ],
            "healthcheck": {
                "test": ["CMD", "curl", "--fail", "http://localhost:8080/api/v2/monitor/health"],
                "interval": "10s",
                "timeout": "10s",
                "retries": 30,
                "start_period": "60s",
            },
        }
        # Compose builds every service that declares a build, in parallel, and two builds
        # exporting one tag can collide; the others run the image the API server's build made.
        shared = {key: value for key, value in common.items() if key != "build"}
        for name, cmd in (
            ("airflow-scheduler", "scheduler"),
            ("airflow-dag-processor", "dag-processor"),
            ("airflow-triggerer", "triggerer"),
        ):
            services[name] = {
                **shared,
                "pull_policy": "never",
                "command": cmd,
                "depends_on": {"airflow-api-server": {"condition": "service_healthy"}},
            }
        services["log-cleaner"] = {
            "image": VERSIONS.log_cleaner_image,
            "profiles": ["platform"],
            "command": [
                "/bin/sh",
                "-c",
                "while true; do find /opt/airflow/logs -type f "
                '-mtime +"${AIRFLOW_LOG_RETENTION_DAYS:-14}" -delete; '
                "find /opt/airflow/logs -type d -empty -delete; sleep 86400; done",
            ],
            "volumes": ["airflow-logs:/opt/airflow/logs"],
            "restart": "unless-stopped",
        }
        volumes["airflow-logs"] = {}
        if mode == "vm":
            volumes["airflow-artifacts"] = {}

    if with_mcp:
        services["mcp-server"] = {
            "build": _build(mode, stack_dir, "mcp.Dockerfile"),
            "image": f"{project}-mcp:{VERSIONS.vecten}",
            "profiles": ["mcp"],
            "environment": {
                "VECTEN_MCP_PUBLIC_URL": f"https://{hosts['mcp']}",
                "VECTEN_PROVIDER": mode if mode == "local" else "${VECTEN_PROVIDER}",
                "VECTEN_ENV": "local" if mode == "local" else "${VECTEN_ENV}",
                "VECTEN_COMPONENT": "mcp",
                "VECTEN_PROJECT_ROOT": "/app",
                "MCP_STATE_DB_PASSWORD": "${MCP_STATE_DB_PASSWORD}",
            },
            "volumes": [
                "../../osi:/app/osi:ro",
                "../../vecten.toml:/app/vecten.toml:ro",
                *_record_mount(stack_dir, "/app"),
            ],
            "depends_on": (
                {"postgres": {"condition": "service_healthy"}} if mode == "local" else {}
            ),
            "healthcheck": {
                "test": ["CMD", "curl", "--fail", "http://localhost:8001/health"],
                "interval": "5s",
                "timeout": "5s",
                "retries": 30,
            },
            "restart": "unless-stopped",
        }
        if mode == "local":
            if config.has("platform"):
                services["mcp-server"]["volumes"].append(
                    f"{LOCAL_ARTIFACTS_SOURCE}:{DEFAULT_ARTIFACT_DIR}:ro"
                )
                services["mcp-server"]["environment"][ARTIFACT_DIR_VAR] = DEFAULT_ARTIFACT_DIR
            services["mcp-server"]["environment"] |= {
                "MCP_PROVISIONER_PASSWORD": "${MCP_PROVISIONER_PASSWORD:-${PROVISIONER_PASSWORD}}",
                "MCP_IMPERSONATOR_PASSWORD": (
                    "${MCP_IMPERSONATOR_PASSWORD:-${IMPERSONATOR_PASSWORD}}"
                ),
                "MCP_ADMIN_SETUP_TOKEN": "${MCP_ADMIN_SETUP_TOKEN:-}",
                "MCP_OIDC_CLIENT_SECRET": "${MCP_OIDC_CLIENT_SECRET:-}",
            }
            services["mcp-server"]["volumes"].append("mcp-secrets:/var/lib/vecten-mcp/secrets")
            volumes["mcp-secrets"] = {}
            # On a VM the MCP server reads its own scope through its provider instead.
            mcp_env = services["mcp-server"]["environment"]
            declared = mcp_secrets(config, project_record(stack_dir))
            for secret in declared:
                if secret in mcp_env:
                    raise Failure(
                        ExitCode.CONFIG,
                        f"declared secret {secret} collides with a stack variable; rename it",
                    )
            _add_secrets(mcp_env, declared, "declared")
        if mode == "vm":
            services["mcp-server"]["environment"] |= _vm_runtime_env(_cloud(config)) | {
                "VECTEN_MCP_POOL_CAP": "${VECTEN_MCP_POOL_CAP}",
                "VECTEN_FEEDBACK_MANIFEST": "/app/feedback.json",
            }
            services["mcp-server"]["volumes"].append(VM_ENVIRONMENT_MOUNT)
            services["mcp-server"]["volumes"].append("../ship/feedback.json:/app/feedback.json:ro")
            if config.mcp.admin_panel == "off":
                # NGINX refuses /admin publicly; a tunnel to the VM's loopback reaches the panel.
                services["mcp-server"]["ports"] = ["127.0.0.1:8001:8001"]

    if mode == "local":
        nginx_ports = [f"127.0.0.1:{LOCAL_HTTPS_PORT}:443", f"127.0.0.1:{LOCAL_HTTP_PORT}:80"]
        cert_volumes = ["./certs:/etc/nginx/certs:ro"]
    else:
        nginx_ports = ["443:443", "80:80"]
        cert_volumes = [
            "/etc/letsencrypt:/etc/letsencrypt:ro",
            "/var/www/certbot:/var/www/certbot:ro",
        ]
    nginx_depends = {}
    if with_platform:
        nginx_depends["airflow-api-server"] = {"condition": "service_healthy"}
    if with_mcp:
        nginx_depends["mcp-server"] = {"condition": "service_healthy"}
    services["nginx"] = {
        "image": VERSIONS.nginx_image,
        "profiles": ["platform", "mcp", "local"],
        "ports": nginx_ports,
        "volumes": [
            "./nginx/conf.d:/etc/nginx/conf.d:ro",
            *cert_volumes,
        ],
        "depends_on": nginx_depends,
        "restart": "unless-stopped",
    }
    document = {"name": project, "services": services, "volumes": volumes}
    if config.packages.private_index:
        secrets = {}
        for name, service in services.items():
            if "build" not in service:
                continue
            component = "mcp" if name == "mcp-server" else "platform"
            source = f"{component}_package_index"
            variable = (
                "MCP_PACKAGE_INDEX_URL"
                if component == "mcp" and mode == "vm"
                else "PACKAGE_INDEX_URL"
            )
            secrets[source] = {"environment": variable}
            service["build"]["secrets"] = [{"source": source, "target": "package_index"}]
        document["secrets"] = secrets
    return document


def _write_pg_init(stack_dir: Path) -> None:
    pg_init = stack_dir / "pg-init"
    # The postgres entrypoint runs every top-level *.sql itself, without the psql variables
    # the setup SQL needs, so the SQL sits one level down where only the script reaches it.
    (pg_init / "vecten").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(setup_sql_path(), pg_init / "vecten" / "warehouse_setup.sql")
    script = pg_init / "10-vecten.sh"
    shutil.copyfile(local_init_script_path(), script)
    script.chmod(0o755)


def write_extension_dag_loader(stack_dir: Path) -> None:
    (stack_dir / "extension_dags.py").write_text(
        '"""Airflow DAG discovery for installed Vecten extensions."""\n'
        "import os\nfrom pathlib import Path\n"
        "from vecten_shared.config import load_config\n"
        "from vecten_platform.extensions import load_dags\n"
        "globals().update(load_dags(load_config(Path(os.environ['VECTEN_PROJECT_ROOT']))))\n"
    )


def write_compose(project_root: Path, config: VectenConfig) -> Path:
    stack_dir = project_root / STACK_DIR
    stack_dir.mkdir(parents=True, exist_ok=True)
    if config.has("platform"):
        # Docker would create a missing bind-mount source owned by root.
        (project_root / LOCAL_ARTIFACTS).mkdir(parents=True, exist_ok=True)
    _write_pg_init(stack_dir)
    write_extension_dag_loader(stack_dir)
    for source, empty, _ in PROJECT_MOUNTS:
        if not (stack_dir / source).is_dir():
            (stack_dir / "empty" / empty).mkdir(parents=True, exist_ok=True)
    doc = generate_compose(config, "local", list(config.components), stack_dir)
    path = stack_dir / "docker-compose.yml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    override = project_root / "orchestration" / "docker-compose.override.yml"
    files = [str(path)] + ([str(override)] if override.is_file() else [])
    (stack_dir / "compose.project").write_text("\n".join(files) + "\n")
    return path
