"""Intake answers recorded by `vecten answer`, read by `next`, consumed by `init`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from vecten.connectors.access import ACCESS_PREFIX, is_access_key
from vecten.intake.facts import (
    ALERT_PREFIX,
    MCP_KEYS,
    OIDC_REGISTERED_PREFIX,
    PROMOTION_KEY,
    is_alert_key,
    is_mcp_key,
    is_oidc_registered_key,
    is_promotion_key,
)
from vecten_shared.config import deploy_value_problem, parse_warehouse_locator
from vecten_shared.names import CONNECTOR_RULE, connector_name_problem
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic

ANSWERS_FILE = "vecten-answers.yml"
EMPTY_SOURCES = ("none", "start empty")
DECLINED = "none"
DECLINABLE = ("domain", "github_repository")
SHARED_ACCOUNT_KEY = "snowflake_shared_account"
SOURCES_NEED_PLATFORM = (
    "sources belong to the platform component; an existing warehouse deploys mcp only"
)


@dataclass(frozen=True)
class AnswerKey:
    name: str
    kind: Literal["choice", "text", "int", "list", "url"]
    question: str
    scope: Literal["local", "deploy"]
    choices: tuple[str, ...] | None = None


ANSWER_KEYS: dict[str, AnswerKey] = {
    k.name: k
    for k in (
        AnswerKey(
            "has_warehouse",
            "choice",
            "Do you already have a data warehouse?",
            "local",
            ("yes", "no"),
        ),
        AnswerKey(
            "sources",
            "list",
            "Do you have data sources to connect, such as a CRM, existing databases or financial "
            "spreadsheets, or do you want to start empty?",
            "local",
        ),
        AnswerKey(
            "warehouse",
            "choice",
            "Should the warehouse be Postgres, run for you, "
            "or a Snowflake account you already have?",
            "local",
            ("postgres", "snowflake"),
        ),
        AnswerKey(
            "warehouse_url", "url", "What is the locator of the existing warehouse?", "local"
        ),
        AnswerKey("users", "int", "How many people and agents will use the warehouse?", "local"),
        AnswerKey("name", "text", "What should the project be called?", "local"),
        AnswerKey(
            "cloud",
            "choice",
            "Do you have a cloud account? Which provider?",
            "deploy",
            ("aws", "scaleway", DECLINED),
        ),
        AnswerKey("region", "text", "Which region should the platform run in?", "deploy"),
        AnswerKey("domain", "text", "Do you have a registered domain?", "deploy"),
        AnswerKey("dns_zone", "text", "Which DNS zone is delegated to the cloud?", "deploy"),
        AnswerKey(
            "github_repository", "text", "Do you have a GitHub account? Which repository?", "deploy"
        ),
        AnswerKey(
            "snowflake_account",
            "text",
            "Which Snowflake account should the platform build in? Give its "
            "ORGANIZATION-ACCOUNT identifier, for example ACME-PROD.",
            "deploy",
        ),
        AnswerKey(
            SHARED_ACCOUNT_KEY,
            "choice",
            "Does this Snowflake account hold anything besides this one environment, such as "
            "another environment of this project or another team's data?",
            "deploy",
            ("yes", "no"),
        ),
    )
}


def source_names(items: list[Any]) -> list[str]:
    names = [str(item).strip().lower() for item in items]
    markers = [n for n in names if n in EMPTY_SOURCES]
    if markers and len(names) > 1:
        raise Failure(ExitCode.USAGE, f"sources cannot combine {markers[0]!r} with named sources")
    if markers:
        return []
    for entry, name in zip(items, names, strict=True):
        if connector_name_problem(name):
            raise Failure(
                ExitCode.USAGE,
                f"sources must be connector names ({CONNECTOR_RULE}), as in "
                f"`vecten answer sources hubspot,billing_db`; {str(entry).strip()!r} is not one",
            )
    return list(dict.fromkeys(names))


def validate_answer(key: str, raw: str) -> Any:
    if is_access_key(key):
        raise Failure(
            ExitCode.USAGE,
            f"{key} is recorded on the connector declaration in vecten.toml, "
            f"not in {ANSWERS_FILE}; run `vecten answer {key} <groups>` inside the project",
        )
    if is_mcp_key(key) or is_alert_key(key) or is_oidc_registered_key(key) or is_promotion_key(key):
        raise Failure(
            ExitCode.USAGE,
            f"{key} is recorded in vecten.toml, not in {ANSWERS_FILE}; "
            "run `vecten answer` inside the project",
        )
    spec = ANSWER_KEYS.get(key)
    if spec is None:
        extra = (
            f"{ACCESS_PREFIX}<connector>",
            *MCP_KEYS,
            f"{ALERT_PREFIX}<env>",
            f"{OIDC_REGISTERED_PREFIX}<env>",
            PROMOTION_KEY,
        )
        known = ", ".join((*ANSWER_KEYS, *extra))
        raise Failure(ExitCode.USAGE, f"unknown answer key {key!r}; known keys: {known}")
    if spec.kind == "choice":
        value = raw.strip().lower()
        if value not in spec.choices:
            raise Failure(ExitCode.USAGE, f"{key} must be one of: {', '.join(spec.choices)}")
        return value
    if spec.kind == "int":
        digits = raw.strip()
        if not (digits.isascii() and digits.isdigit()):
            raise Failure(ExitCode.USAGE, f"{key} must be a positive integer")
        value = int(digits)
        if value <= 0:
            raise Failure(ExitCode.USAGE, f"{key} must be a positive integer")
        return value
    if spec.kind == "list":
        items = [s.strip() for s in raw.split(",") if s.strip()]
        if not items:
            raise Failure(
                ExitCode.USAGE, f"{key} must name at least one source, or be 'none' to start empty"
            )
        return source_names(items)
    if spec.kind == "url":
        locator = raw.strip()
        parse_warehouse_locator(locator)
        return locator
    value = raw.strip()
    if not value:
        raise Failure(ExitCode.USAGE, f"{key} must not be empty")
    if key in DECLINABLE and value.lower() == DECLINED:
        return DECLINED
    problem = deploy_value_problem(key, value)
    if problem:
        raise Failure(ExitCode.USAGE, problem)
    return value


def read_answers_file(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Failure(ExitCode.USAGE, f"answers file {path} not found") from None
    except yaml.YAMLError as exc:
        raise Failure(ExitCode.CONFIG, f"{path}: {exc}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise Failure(ExitCode.CONFIG, f"{path}: expected a mapping of answer keys")
    return data


def read_answers(directory: Path) -> dict[str, Any]:
    path = directory / ANSWERS_FILE
    if not path.is_file():
        return {}
    return read_answers_file(path)


def write_answer(directory: Path, key: str, raw: str) -> dict[str, Any]:
    value = validate_answer(key, raw)
    answers = read_answers(directory)
    answers[key] = value
    write_text_atomic(
        directory / ANSWERS_FILE, yaml.safe_dump(answers, sort_keys=True, allow_unicode=True)
    )
    return answers
