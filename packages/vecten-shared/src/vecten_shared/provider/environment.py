"""An environment's Terraform outputs, read from the JSON file `deploy` writes."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from vecten_shared.output import ExitCode, Failure

ENVIRONMENT_FILE_VAR = "VECTEN_ENVIRONMENT_FILE"


class HostOutput(BaseModel):
    instance_id: str
    public_ip: str
    components: list[str]
    hostnames: list[str]
    # The service hostnames whose records point at this host. Left out of the dump when
    # empty, so the outputs of a project without services keep their bytes in the bundle.
    service_hostnames: list[str] = Field(default_factory=list, exclude_if=lambda names: not names)


class PostgresOutput(BaseModel):
    host: str
    port: int
    databases: list[str]
    master_secret: str


class EnvironmentOutputs(BaseModel):
    environment: str
    region: str
    hosts: dict[str, HostOutput]
    setup_host: str
    bucket: str
    secret_prefix: str
    postgres: dict[str, PostgresOutput]


def project_environment_file(project_root: Path, env: str) -> Path:
    """Where `deploy` writes the outputs inside the project, whatever the override says."""
    return project_root / ".vecten" / "environments" / f"{env}.json"


def environment_file(project_root: Path, env: str) -> Path:
    override = os.environ.get(ENVIRONMENT_FILE_VAR)
    if override:
        return Path(override)
    return project_environment_file(project_root, env)


def load_outputs(path: Path, env: str) -> EnvironmentOutputs:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"environment {env!r} has no outputs file at {path}; "
            f"run `vecten deploy --env {env}` first",
        ) from None
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise Failure(ExitCode.CONFIG, f"{path}: {exc}") from None
    try:
        return EnvironmentOutputs.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "(top level)"
        raise Failure(ExitCode.CONFIG, f"{path}: {loc}: {first['msg']}") from None
