"""The rules for names vecten.toml carries into shell lines, file paths and schema names."""

from __future__ import annotations

import re

from vecten_shared.output import ExitCode, Failure

# A connector name becomes a schema, a Python package and a file name.
CONNECTOR_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
CONNECTOR_RULE = (
    "lowercase letters, digits and underscores, starting with a letter, at most 63 characters"
)
# A group name becomes a warehouse role and part of a DAG file name.
GROUP_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
GROUP_RULE = (
    "lowercase letters, digits and underscores, starting with a letter, at most 63 characters"
)
# A secret name lands inside Compose's `${...}` and every provider's key space.
SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SECRET_RULE = (
    "letters, digits and underscores, not starting with a digit (^[A-Za-z_][A-Za-z0-9_]*$)"
)
# An environment name becomes part of record file names, shell lines and cloud resource names,
# whose slug collapses hyphen runs and trims end hyphens; single inner hyphens keep it unique.
ENV_NAME = re.compile(r"(?=.{1,32}\Z)[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
ENV_RULE = (
    "lowercase letters, digits and hyphens, starting with a letter, with no hyphen at the end "
    "or next to another, at most 32 characters"
)
RESERVED_ENVS = frozenset({"local"})


def connector_name_problem(value: str) -> str | None:
    if CONNECTOR_NAME.fullmatch(value):
        return None
    return f"connector name must be {CONNECTOR_RULE}; got {value!r}"


def group_name_problem(value: str) -> str | None:
    if GROUP_NAME.fullmatch(value):
        return None
    return f"group names must be {GROUP_RULE}; got {value!r}"


def secret_name_problem(value: str) -> str | None:
    if SECRET_NAME.fullmatch(value):
        return None
    return f"secret name must be {SECRET_RULE}; got {value!r}"


def env_name_problem(value: str) -> str | None:
    if value in RESERVED_ENVS:
        return f"environment name {value!r} is reserved for the laptop; name a cloud environment"
    if ENV_NAME.fullmatch(value):
        return None
    return f"environment name must be {ENV_RULE}; got {value!r}"


def _check(problem: str | None) -> None:
    if problem:
        raise Failure(ExitCode.USAGE, problem)


def check_connector_name(value: str) -> None:
    _check(connector_name_problem(value))


def check_group_name(value: str) -> None:
    _check(group_name_problem(value))


def check_secret_name(value: str) -> None:
    _check(secret_name_problem(value))


def check_env_name(value: str) -> None:
    _check(env_name_problem(value))
