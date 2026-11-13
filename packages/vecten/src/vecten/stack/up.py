"""`up` and `down`: the local stack from vecten.toml, nothing in the project."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from vecten.cli.injection import load_runner
from vecten.stack import SNOWFLAKE_MCP_NOTE, local_components
from vecten.stack.certs import ensure_self_signed
from vecten.stack.compose import (
    LOCAL_AIRFLOW_API_PORT,
    LOCAL_HTTPS_PORT,
    STACK_DIR,
    compose_project_name,
    local_hostnames,
    service_host_name,
    services_to_run,
    write_compose,
)
from vecten.stack.docker import DockerRunner, SubprocessDocker
from vecten.stack.env import ensure_local_env
from vecten.stack.images import SOURCES_DIR, base_image, write_project_layer
from vecten.stack.nginx import AIRFLOW_HEALTH_PATH, HOST_KEYS, service_site, write_nginx
from vecten_shared.config import load_config
from vecten_shared.declarations import check_settings
from vecten_shared.extension_record import read_record as read_extension_record
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import read_record, remove_record, write_record, write_text_atomic
from vecten_shared.services import check_bindings

PUBLISHED_REGISTRY = "ghcr.io/vecten"
NO_LOCAL_IMAGES_HINT = (
    "no local base images found; build them with `vecten dev build-images` "
    "from a vecten-open checkout"
)
# Each component's own verdict through NGINX, as the cloud ship polls it.
FRONT_DOOR_CHECKS = {"airflow": (AIRFLOW_HEALTH_PATH, {200}), "mcp": ("/health", {200})}


def get_runner() -> DockerRunner:
    return load_runner("VECTEN_DOCKER_RUNNER", SubprocessDocker)


@dataclass
class UpResult:
    profiles: list[str]
    urls: dict[str, str]
    stack_dir: Path
    # The loopback Airflow API and its user for the Airflow MCP server; empty without platform.
    airflow_api: dict[str, str]
    left_out: list[str]
    # Each started service's health, or why Compose could not start it.
    services: dict[str, str]
    # What the started services reserve together on this machine.
    limits: dict[str, float]
    # Each bound route of a started service, by `<extension>.<service>.<route>`, at its URL.
    routes: dict[str, str] = field(default_factory=dict)


@dataclass
class DownResult:
    stopped: bool
    volumes_removed: bool = False


def _compose_files(project_root: Path) -> list[Path]:
    listing = project_root / STACK_DIR / "compose.project"
    if not listing.is_file():
        return []
    return [Path(line) for line in listing.read_text().splitlines() if line]


def _env(env: dict[str, str], key: str) -> str:
    if not env.get(key):
        raise Failure(ExitCode.INTERNAL, f"{key} missing from .env after ensure_local_env")
    return env[key]


def wait_for_front_doors(urls: dict[str, str], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(verify=False, timeout=5) as client:
        for component, url in urls.items():
            path, expected = FRONT_DOOR_CHECKS[component]
            host = url.removeprefix("https://")
            # NGINX refuses a handshake without a known SNI name, and an IP URL sends none.
            sni = {"sni_hostname": host.split(":", 1)[0]}
            status: int | None = None
            while True:
                try:
                    status = client.get(
                        f"https://127.0.0.1:{LOCAL_HTTPS_PORT}{path}",
                        headers={"Host": host},
                        extensions=sni,
                    ).status_code
                except httpx.HTTPError:
                    status = None
                if status in expected:
                    break
                if time.monotonic() > deadline:
                    raise Failure(
                        ExitCode.TOOL_FAILED,
                        f"{component} did not answer at {url} (last status {status})",
                    )
                time.sleep(2)


def _stack_services(
    runner: DockerRunner, project_root: Path, profiles: list[str], left: set[str]
) -> list[str]:
    """Every service the active profiles start, the override file's included, but `left`."""
    files = _compose_files(project_root)
    result = runner.compose(project_root, files, profiles, ["config", "--services"])
    if result.returncode != 0:
        raise Failure(
            ExitCode.TOOL_FAILED, f"docker compose config failed: {result.stderr[-2000:]}"
        )
    return [name for name in result.stdout.split() if name not in left]


def _service_states(
    runner: DockerRunner, project_root: Path, profiles: list[str], names: dict[str, str]
) -> dict[str, str]:
    """Each service's health check verdict, or its container state while it has none."""
    files = _compose_files(project_root)
    result = runner.compose(project_root, files, profiles, ["ps", "--format", "json", *names])
    if result.returncode != 0:
        return {key: "unknown: docker compose ps failed" for key in names.values()}
    rows = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            parsed = json.loads(line)
            rows += parsed if isinstance(parsed, list) else [parsed]
    found = {row.get("Service"): row.get("Health") or row.get("State") for row in rows}
    return {key: found.get(name) or "not running" for name, key in names.items()}


def _registry(runner: DockerRunner, components: list[str]) -> str | None:
    explicit = os.environ.get("VECTEN_IMAGE_REGISTRY")
    if explicit:
        return explicit
    probe = base_image("airflow" if "platform" in components else "mcp", None)
    return None if runner.image_exists(probe) else PUBLISHED_REGISTRY


def up(project_root: Path, runner: DockerRunner, wait_seconds: int = 240) -> UpResult:
    if not runner.available():
        raise Failure(
            ExitCode.PREREQUISITE,
            "Docker is not reachable; install Docker and start the daemon, "
            "then run `vecten up` again",
        )
    config = load_config(project_root)
    record = read_extension_record(project_root) or {}
    check_settings(config, record, "local")
    check_bindings(config, record)
    from vecten import extensions

    extensions.refuse_stale_services(project_root, config, record)
    components = local_components(config)
    if not components:
        raise Failure(
            ExitCode.REFUSED, f"{SNOWFLAKE_MCP_NOTE}, so there is no local stack to start"
        )
    env = ensure_local_env(project_root, config)
    stack_dir = project_root / STACK_DIR
    write_compose(project_root, config)
    running = services_to_run(config, "local", components, stack_dir)
    names = {bound.compose_name: bound.key for bound in running}
    hosts = local_hostnames()
    routed = [
        (bound, service_host_name("local", bound.binding.hostname, hosts))
        for bound in running
        if bound.binding.hostname is not None
    ]
    sites = [service_site(bound, name) for bound, name in routed]
    write_nginx(
        stack_dir,
        components,
        hosts,
        "local",
        admin_panel=config.mcp.admin_panel,
        admin_allow=config.mcp.admin_allow,
        services=sites,
    )
    served = {HOST_KEYS[c]: hosts[HOST_KEYS[c]] for c in components}
    ensure_self_signed(stack_dir, [*served.values(), *(site.host for site in sites)])
    if "platform" in components:
        passwords = {_env(env, "AIRFLOW_ADMIN_USER"): _env(env, "AIRFLOW_ADMIN_PASSWORD")}
        write_text_atomic(stack_dir / "airflow_passwords.json", json.dumps(passwords), mode=0o600)
    registry = _registry(runner, components)
    from vecten import git_extensions

    # Kept under the stack directory between runs, re-fetched once its hash stops matching.
    git_extensions.write_sources(project_root, config, stack_dir / SOURCES_DIR, keep=True)
    write_project_layer(stack_dir, config, registry)
    profiles = [*components, "local"]
    # A restarting container resets its health check, so an unbounded --wait can block forever.
    args = ["up", "-d", "--build", "--wait", "--wait-timeout", str(wait_seconds)]
    if running:
        # `--wait` waits on every service it starts, so an extension's service starts after
        # the stack and never holds `up` back.
        args += _stack_services(runner, project_root, profiles, set(names))
    result = runner.compose(project_root, _compose_files(project_root), profiles, args)
    if result.returncode != 0:
        message = f"docker compose up failed: {result.stderr[-2000:]}"
        if registry == PUBLISHED_REGISTRY and not os.environ.get("VECTEN_IMAGE_REGISTRY"):
            message += f"; {NO_LOCAL_IMAGES_HINT}"
        raise Failure(ExitCode.TOOL_FAILED, message)
    states: dict[str, str] = {}
    if running:
        started = runner.compose(
            project_root, _compose_files(project_root), profiles, ["up", "-d", "--no-build", *names]
        )
        if started.returncode != 0:
            reason = started.stderr.strip()[-300:] or "docker compose up failed"
            states = {key: f"not started: {reason}" for key in names.values()}
        else:
            states = _service_states(runner, project_root, profiles, names)
    limits = {
        "memory_mb": sum(b.memory_mb for b in running),
        "cpus": round(sum(b.cpus for b in running), 2),
    }
    urls = {key: f"https://{host}" for key, host in served.items()}
    wait_for_front_doors(urls, wait_seconds)
    routes = {
        f"{bound.key}.{name}": f"https://{host}{route.path}"
        for bound, host in routed
        for name, route in bound.routes.items()
    }
    airflow_api = (
        {
            "airflow_api": f"http://127.0.0.1:{LOCAL_AIRFLOW_API_PORT}",
            "airflow_user": _env(env, "AIRFLOW_ADMIN_USER"),
        }
        if "platform" in components
        else {}
    )
    write_record(
        project_root,
        "up",
        {"profiles": profiles, "urls": urls, "stack_dir": str(stack_dir), **airflow_api},
    )
    left_out = [c for c in config.components if c not in components]
    return UpResult(
        profiles=profiles,
        urls=urls,
        stack_dir=stack_dir,
        airflow_api=airflow_api,
        left_out=left_out,
        services=states,
        limits=limits,
        routes=routes,
    )


def down(project_root: Path, runner: DockerRunner, volumes: bool = False) -> DownResult:
    if not runner.available():
        if volumes:
            raise Failure(
                ExitCode.PREREQUISITE,
                "Docker is not reachable, so the stack's volumes cannot be removed; "
                "start the daemon, then run `vecten down --volumes` again",
            )
        return DownResult(stopped=False)
    config = load_config(project_root)
    files = _compose_files(project_root)
    # Compose stops only the given profiles' services. A failed `up` after a components change
    # leaves containers under both the recorded and the configured profiles, so stop the union.
    started = read_record(project_root, "up") or read_record(project_root, "down")
    recorded = (started or {}).get("profiles") or []
    profiles = sorted(set(recorded) | {*config.components, "local"})
    if not files or not all(f.is_file() for f in files):
        if not volumes:
            return DownResult(stopped=False)
        _remove_labelled_volumes(runner, compose_project_name(project_root, config))
        _record_down(project_root, profiles, volumes)
        return DownResult(stopped=False, volumes_removed=True)
    ps = runner.compose(project_root, files, profiles, ["ps", "-q"])
    if ps.returncode != 0:
        raise Failure(ExitCode.TOOL_FAILED, f"docker compose ps failed: {ps.stderr[-2000:]}")
    running = bool(ps.stdout.strip())
    # Volumes outlive the containers, so removing them is still due when nothing is running.
    if not running and not volumes:
        return DownResult(stopped=False)
    result = runner.compose(project_root, files, profiles, ["down", "-v"] if volumes else ["down"])
    if result.returncode != 0:
        raise Failure(ExitCode.TOOL_FAILED, f"docker compose down failed: {result.stderr[-2000:]}")
    _record_down(project_root, profiles, volumes)
    return DownResult(stopped=running, volumes_removed=volumes)


def _record_down(project_root: Path, profiles: list[str], volumes: bool) -> None:
    write_record(project_root, "down", {"profiles": profiles, "volumes_removed": volumes})
    remove_record(project_root, "up")


def _remove_labelled_volumes(runner: DockerRunner, project: str) -> None:
    result = runner.remove_volumes(runner.volumes_by_label(project))
    if result.returncode != 0:
        raise Failure(ExitCode.TOOL_FAILED, f"docker volume rm failed: {result.stderr[-2000:]}")
