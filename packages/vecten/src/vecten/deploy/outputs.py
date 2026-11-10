"""An environment's Terraform outputs: the seven names every cloud module exposes, and the file.

`deploy` writes them to `outputs_path` in the project and to `outputs_key` in the state store;
the runtime provider and `ship` read them back through `EnvironmentOutputs`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.environment import EnvironmentOutputs, project_environment_file
from vecten_shared.records import write_text_atomic

OUTPUT_NAMES: tuple[str, ...] = (
    "environment",
    "region",
    "hosts",
    "setup_host",
    "bucket",
    "secret_prefix",
    "postgres",
)


def outputs_path(project_root: Path, env: str) -> Path:
    return project_environment_file(project_root, env)


def outputs_key(env: str) -> str:
    return f"{env}/outputs.json"


def write_outputs(project_root: Path, env: str, raw: dict[str, Any]) -> EnvironmentOutputs:
    """Validate `terraform output -json` and write its values to the environment's file."""
    missing = [n for n in OUTPUT_NAMES if not isinstance(raw.get(n), dict) or "value" not in raw[n]]
    if missing:
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"terraform output has no {', '.join(missing)}; the module must expose "
            f"{', '.join(OUTPUT_NAMES)}",
            details={"missing": missing},
        )
    values = {n: raw[n]["value"] for n in OUTPUT_NAMES}
    try:
        outputs = EnvironmentOutputs.model_validate(values)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise Failure(ExitCode.TOOL_FAILED, f"terraform output {loc}: {first['msg']}") from None
    if outputs.environment != env:
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"terraform output environment is {outputs.environment!r}, not {env!r}",
        )
    save_outputs(project_root, env, outputs)
    return outputs


def parse_stored_outputs(body: bytes | str, where: str, env: str) -> EnvironmentOutputs:
    """The outputs object read back from a state store at `where`, refused when malformed or
    holding another environment's outputs."""
    try:
        outputs = EnvironmentOutputs.model_validate_json(body)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "(top level)"
        raise Failure(ExitCode.TOOL_FAILED, f"{where}: {loc}: {first['msg']}") from None
    if outputs.environment != env:
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"{where} holds the outputs of environment {outputs.environment!r}, not {env!r}",
        )
    return outputs


def outputs_text(outputs: EnvironmentOutputs) -> str:
    """The one serialisation of the outputs, in the project and in the state bucket alike."""
    return json.dumps(outputs.model_dump(), indent=2, sort_keys=True) + "\n"


def save_outputs(project_root: Path, env: str, outputs: EnvironmentOutputs) -> Path:
    path = outputs_path(project_root, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, outputs_text(outputs))
    return path
