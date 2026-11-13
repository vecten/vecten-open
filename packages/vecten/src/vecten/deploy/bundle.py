"""One content bundle per host: the project's content at a commit and the stack for that host.

The VM agent extracts a bundle to `/opt/vecten/releases/<commit>/` and runs from it, so the
layout here is the agent's contract: `.vecten/ship/host.json` (every key in
`vm.apply.HOST_KEYS`), `.vecten/ship/apply.py`, `.vecten/ship/warehouse_setup.sql`,
`.vecten/environments/<env>.json` and `.vecten/stack/` rendered in vm mode.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from vecten import git_extensions
from vecten.deploy.feedback import feedback_manifest
from vecten.deploy.names import name_prefix
from vecten.deploy.outputs import outputs_text
from vecten.deploy.sizing import tier
from vecten.deploy.tfvars import DAGS_PAUSED_AT_CREATION, check_tfvars
from vecten.deploy.vm.apply import HOST_KEYS, PLUGIN_DIRS
from vecten.github.gh import ToolRunner, get_git
from vecten.stack.compose import (
    PROJECT_MOUNTS,
    STACK_DIR,
    generate_compose,
    host_services,
    platform_secrets,
    served_services,
    vm_hostnames,
    write_extension_dag_loader,
)
from vecten.stack.images import write_project_layer
from vecten.stack.nginx import service_site, write_nginx
from vecten.stack.up import PUBLISHED_REGISTRY
from vecten_shared.config import VectenConfig
from vecten_shared.declarations import check_settings
from vecten_shared.extension_record import Extension, read_record
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.environment import EnvironmentOutputs
from vecten_shared.records import write_text_atomic
from vecten_shared.services import Bound, check_bindings
from vecten_shared.sql import setup_sql_path
from vecten_shared.versions import VERSIONS

CONTENT: tuple[str, ...] = (
    "vecten.toml",
    "requirements.txt",
    "requirements.lock",
    "requirements-airflow.lock",
    "requirements-mcp.lock",
    "extensions.lock.json",
    "extensions",
    "orchestration/dags",
    "orchestration/docker-compose.override.yml",
    "connectors",
    "dbt",
    "osi",
    "evals",
)
SHIP_DIR = Path(".vecten") / "ship"
DBT_DIR = "dbt"
ENVIRONMENTS_DIR = Path(".vecten") / "environments"
SKIPPED_NAMES = frozenset({".git", ".venv", "__pycache__", ".terraform", ".DS_Store"})
SKIPPED_SUFFIXES = (".pyc",)
# dbt writes these on every run and vecten regenerates the package files before each build,
# so they belong to the machine that ran dbt, not to the commit.
SKIPPED_PATHS = frozenset(
    {"dbt/target", "dbt/logs", "dbt/dbt_packages", "dbt/packages.yml", "dbt/package-lock.yml"}
)
# Every member carries this owner, and a directory this mtime, so a bundle's bytes depend on
# its content only.
MEMBER_MTIME = 0
# A file's mtime, from 2001-09-09 on, follows its content (see `content_mtime`).
CONTENT_MTIME_BASE = 1_000_000_000
CONTENT_MTIME_SPAN = 2**29
DEFAULT_PLATFORM_SIZE = "small"
# The commit names the release directory on the VM and appears in its shell commands.
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def check_commit(commit: str) -> str:
    if not _COMMIT.fullmatch(commit):
        raise Failure(ExitCode.INTERNAL, f"{commit!r} is not a full git commit id")
    return commit


def head_commit(git: ToolRunner, project_root: Path) -> str:
    result = git.run(["rev-parse", "HEAD"], project_root)
    commit = result.stdout.strip()
    if result.returncode != 0 or not _COMMIT.fullmatch(commit):
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"git rev-parse HEAD named no commit in {project_root}; ship from a git checkout",
        )
    return commit


def bundle_name(host: str, commit: str) -> str:
    return f"{host}-{commit}.tar.gz"


def build_lineage(project_root: Path, commit: str) -> dict:
    check_commit(commit)
    try:
        result = get_git().run(["rev-list", commit], project_root)
    except OSError:
        return {"commit": commit, "ancestors": []}
    commits = result.stdout.splitlines() if result.returncode == 0 else []
    if not commits or commits[0] != commit or not all(_COMMIT.fullmatch(c) for c in commits):
        commits = []
    # A shallow checkout proves only the ancestors present in it; omitted ancestry
    # leaves the model waiting for an exact build instead of guessing commit order.
    return {"commit": commit, "ancestors": sorted(set(commits) - {commit})}


def compose_project(config: VectenConfig, env: str) -> str:
    """The Compose project on a VM; one per environment, independent of any checkout path."""
    return name_prefix(config.project.name, env)


# A package's local build output is rebuilt on the host from its source.
SKIPPED_BUILD_OUTPUT = frozenset({"dist", "build"})


def _extension_build_output(rel: str, name: str) -> bool:
    """A build tool's output at an extension's root, or setuptools' egg-info beside its code.

    Deeper folders of these names are package data, such as a portal's built static files.
    """
    parts = rel.split("/")
    if parts[0] != "extensions" or len(parts) < 3:
        return False
    at_root = len(parts) == 3
    under_src = len(parts) == 4 and parts[2] == "src"
    return (at_root and name in SKIPPED_BUILD_OUTPUT) or (
        (at_root or under_src) and name.endswith(".egg-info")
    )


def _skipped(rel: str, name: str) -> bool:
    return (
        name in SKIPPED_NAMES
        or name == ".env"
        or name.startswith(".env.")
        or name.endswith(SKIPPED_SUFFIXES)
        or rel in SKIPPED_PATHS
        or _extension_build_output(rel, name)
    )


def _refuse_link(rel: str) -> Failure:
    # A link could point anywhere on the machine that builds the bundle or on the VM.
    return Failure(ExitCode.CONFIG, f"{rel} is a symbolic link; a shipped project holds files only")


def _copy_tree(project_root: Path, stage: Path, top: str) -> None:
    source = project_root / top
    for directory, dirs, files in os.walk(source):
        here = Path(directory)
        rel_dir = here.relative_to(project_root).as_posix()
        (stage / rel_dir).mkdir(parents=True, exist_ok=True)
        kept = []
        for name in sorted(dirs):
            rel = f"{rel_dir}/{name}"
            if (here / name).is_symlink():
                raise _refuse_link(rel)
            if not _skipped(rel, name):
                kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            rel = f"{rel_dir}/{name}"
            if (here / name).is_symlink():
                raise _refuse_link(rel)
            if not _skipped(rel, name):
                shutil.copyfile(here / name, stage / rel)


def _copy_content(project_root: Path, stage: Path) -> None:
    for entry in CONTENT:
        source = project_root / entry
        if source.is_symlink():
            raise _refuse_link(entry)
        if source.is_file():
            (stage / entry).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, stage / entry)
        elif source.is_dir():
            _copy_tree(project_root, stage, entry)


def _write_git_sources(stage: Path, sources: Mapping[str, Path]) -> None:
    """Place each fetched git source at `extensions/<name>/`, beside the in-repo extensions."""
    for name, directory in sorted(sources.items()):
        target = stage / "extensions" / name
        if target.exists():
            raise Failure(
                ExitCode.REFUSED,
                f"extensions/{name} is a folder of the project, but {name} comes from git; "
                "remove the folder, or add it as an in-repo extension with "
                f"`vecten extension add ./extensions/{name}`",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(directory, target)


def _served(
    config: VectenConfig, record: dict[str, Extension], outputs: EnvironmentOutputs, host: str
) -> list[tuple[Bound, str]]:
    return served_services(
        config,
        record,
        list(outputs.hosts[host].components),
        vm_hostnames(outputs),
        outputs.hosts[host].service_hostnames,
    )


def _render_stack(
    stage: Path,
    config: VectenConfig,
    outputs: EnvironmentOutputs,
    components: list[str],
    project: str,
    tfvars: dict[str, Any],
    served: list[tuple[Bound, str]],
) -> None:
    stack_dir = stage / STACK_DIR
    stack_dir.mkdir(parents=True, exist_ok=True)
    for source, empty, _ in PROJECT_MOUNTS:
        if not (stack_dir / source).is_dir():
            (stack_dir / "empty" / empty).mkdir(parents=True, exist_ok=True)
    write_extension_dag_loader(stack_dir)
    hosts = vm_hostnames(outputs)
    doc = generate_compose(
        config,
        "vm",
        components,
        stack_dir,
        project=project,
        hosts=hosts,
        dags_paused_at_creation=tfvars.get(DAGS_PAUSED_AT_CREATION, False),
        snowflake_account=tfvars.get("snowflake_account"),
    )
    write_text_atomic(stack_dir / "docker-compose.yml", yaml.safe_dump(doc, sort_keys=False))
    write_nginx(
        stack_dir,
        components,
        hosts,
        "vm",
        admin_panel=config.mcp.admin_panel,
        admin_allow=config.mcp.admin_allow,
        airflow_allow=config.airflow.allow,
        services=[service_site(bound, name) for bound, name in served],
    )
    write_project_layer(stack_dir, config, PUBLISHED_REGISTRY)


def _host_tier(host: str, cloud: str, tfvars: dict[str, Any]) -> dict[str, Any]:
    # The platform VM runs untrusted DAG code and stays small unless tfvars says otherwise;
    # every other host follows the environment's size.
    size = (
        tfvars.get("platform_size", DEFAULT_PLATFORM_SIZE) if host == "platform" else tfvars["size"]
    )
    return tier(cloud, size)


def _project_id(config: VectenConfig, env: str, cloud: str) -> str:
    """The Scaleway resources project the VM's Secret Manager calls name; empty on AWS."""
    if cloud != "scaleway":
        return ""
    facts = config.environments.get(env)
    project_id = facts.cloud_ids.get("project") if facts else None
    if not project_id:
        raise Failure(
            ExitCode.CONFIG,
            f'vecten.toml: [environments.{env}] cloud_ids["project"] is not set; '
            f"run `vecten bootstrap --env {env}`",
        )
    return project_id


def host_document(
    config: VectenConfig,
    env: str,
    outputs: EnvironmentOutputs,
    host: str,
    commit: str,
    tfvars: dict[str, Any],
    record: dict[str, Extension] | None = None,
) -> dict[str, Any]:
    if config.deploy is None:
        raise Failure(ExitCode.REFUSED, "no deploy answers; run `vecten init --deploy` first")
    cloud = config.deploy.cloud
    sizing = _host_tier(host, cloud, tfvars)
    components = list(outputs.hosts[host].components)
    service_secrets, service_network = host_services(config, record or {}, components)
    served = _served(config, record or {}, outputs, host)
    doc = {
        "host": host,
        "cloud": cloud,
        "components": components,
        "env": env,
        "project": config.project.name,
        "region": outputs.region,
        "project_id": _project_id(config, env, cloud),
        "bucket": outputs.bucket,
        "commit": commit,
        "setup_host": outputs.setup_host == host,
        # The agent issues and renews a certificate for every name listed here.
        "hostnames": [*outputs.hosts[host].hostnames, *sorted(name for _, name in served)],
        "secret_prefix": outputs.secret_prefix,
        "compose_project": compose_project(config, env),
        # Every platform name the project and its extensions declare, not connectors' alone.
        "connector_secrets": platform_secrets(config, record or {}),
        "private_package_index": config.packages.private_index,
        "compose_plugin_version": VERSIONS.docker_compose_plugin,
        "buildx_plugin_version": VERSIONS.docker_buildx_plugin,
        "certbot_image": VERSIONS.certbot_image,
        "postgres_image": f"postgres:{VERSIONS.postgres_major}",
        "airflow_parallelism": sizing["airflow_parallelism"],
        "mcp_pool_cap": sizing["mcp_pool_cap"],
        "plugin_dir": PLUGIN_DIRS[cloud],
        "service_secrets": service_secrets,
        "service_network": service_network,
    }
    if set(doc) != set(HOST_KEYS):
        raise Failure(ExitCode.INTERNAL, "host.json keys differ from the agent's HOST_KEYS")
    return doc


def _write_ship_files(stage: Path, env: str, outputs: EnvironmentOutputs, host_doc: dict) -> None:
    (stage / ENVIRONMENTS_DIR).mkdir(parents=True, exist_ok=True)
    write_text_atomic(stage / ENVIRONMENTS_DIR / f"{env}.json", outputs_text(outputs))
    ship_dir = stage / SHIP_DIR
    ship_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(ship_dir / "host.json", json.dumps(host_doc, indent=2, sort_keys=True) + "\n")
    agent = resources.files("vecten.deploy.vm").joinpath("apply.py").read_text(encoding="utf-8")
    write_text_atomic(ship_dir / "apply.py", agent)
    shutil.copyfile(setup_sql_path(), ship_dir / "warehouse_setup.sql")


def content_mtime(data: bytes) -> int:
    """The mtime a member with these bytes carries: equal content, equal time.

    BuildKit reuses a build-context file it already holds whose size and mtime match, even one
    from another release's directory; a time drawn from the content's sha256 makes two contents
    of one size share it with a chance of 1 in 2**29."""
    digest = int.from_bytes(hashlib.sha256(data).digest()[:8], "big")
    return CONTENT_MTIME_BASE + digest % CONTENT_MTIME_SPAN


def _normalised(
    info: tarfile.TarInfo, sources: frozenset[str] = frozenset(), data: bytes | None = None
) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = MEMBER_MTIME if data is None else content_mtime(data)
    # Airflow runs in group 0 and dbt writes into its own project directory.
    writable = info.name == DBT_DIR or info.name.startswith(f"{DBT_DIR}/")
    # A git source's executable bits are part of the source_sha256 the host's build checks.
    # Nothing else needs one: the agent is started as `python3 <path>`.
    parts = info.name.split("/")
    executable = (
        len(parts) > 2
        and parts[0] == "extensions"
        and parts[1] in sources
        and bool(info.mode & 0o100)
    )
    if info.isdir():
        info.mode = 0o775 if writable else 0o755
    else:
        info.mode = 0o755 if executable else 0o664 if writable else 0o644
    return info


def _write_tarball(stage: Path, target: Path, sources: frozenset[str] = frozenset()) -> None:
    entries = sorted((p.relative_to(stage).as_posix(), p) for p in stage.rglob("*"))
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with (
            os.fdopen(fd, "wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
        ):
            for name, path in entries:
                info = tar.gettarinfo(str(path), arcname=name)
                if info.isfile():
                    data = path.read_bytes()
                    tar.addfile(_normalised(info, sources, data), io.BytesIO(data))
                else:
                    tar.addfile(_normalised(info, sources))
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def prepare_bundle_metadata(project_root, config, commit, *, with_mcp=True):
    """Collect external verification before any host in an environment is changed."""
    lineage = build_lineage(project_root, commit)
    feedback = (
        feedback_manifest(project_root, config, commit, lineage)
        if with_mcp
        else {**lineage, "resolutions": [], "unreviewed": []}
    )
    return lineage, feedback


def build_host_bundle(
    project_root: Path,
    config: VectenConfig,
    env: str,
    outputs: EnvironmentOutputs,
    host: str,
    commit: str,
    out_dir: Path,
    *,
    metadata: tuple | None = None,
    sources: Mapping[str, Path] | None = None,
) -> Path:
    """Write `<out_dir>/<host>-<commit>.tar.gz`; equal inputs give byte-identical tarballs.

    `sources` holds the git sources `git_extensions.write_sources` wrote, by name; without it
    they are fetched here."""
    check_commit(commit)
    if host not in outputs.hosts:
        raise Failure(
            ExitCode.INTERNAL,
            f"host {host!r} is not in the outputs of {env!r}; hosts: {', '.join(outputs.hosts)}",
        )
    tfvars = check_tfvars(project_root, config, env)
    record = read_record(project_root) or {}
    check_settings(config, record, env)
    check_bindings(config, record)
    host_doc = host_document(config, env, outputs, host, commit, tfvars, record)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / bundle_name(host, commit)
    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".bundle-") as tmp:
        stage = Path(tmp) / "stage"
        stage.mkdir()
        if sources is None:
            sources = git_extensions.write_sources(project_root, config, Path(tmp) / "sources")
        _copy_content(project_root, stage)
        _write_git_sources(stage, sources)
        _render_stack(
            stage,
            config,
            outputs,
            host_doc["components"],
            host_doc["compose_project"],
            tfvars,
            _served(config, record, outputs, host),
        )
        _write_ship_files(stage, env, outputs, host_doc)
        lineage, feedback = metadata or prepare_bundle_metadata(
            project_root, config, commit, with_mcp="mcp" in host_doc["components"]
        )
        write_text_atomic(
            stage / SHIP_DIR / "build-lineage.json",
            json.dumps(lineage, sort_keys=True) + "\n",
        )
        write_text_atomic(
            stage / SHIP_DIR / "feedback.json",
            json.dumps(feedback, sort_keys=True) + "\n",
        )
        _write_tarball(stage, target, frozenset(sources))
    return target
