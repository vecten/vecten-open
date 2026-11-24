"""Airflow metadata and read-only API accounts declared by projects and extensions."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from vecten_shared.output import ExitCode, Failure


class AirflowResources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pools: dict[str, Annotated[StrictInt, Field(gt=0, le=2147483647)]] = Field(default_factory=dict)
    variables: dict[str, str] = Field(default_factory=dict)
    api_accounts: dict[str, str] = Field(default_factory=dict)

    @field_validator("pools", "variables", "api_accounts")
    @classmethod
    def check_names(cls, mapping, info):
        for name in mapping:
            if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}", name):
                raise ValueError(f"invalid Airflow {info.field_name} name {name!r}")
        if info.field_name == "pools" and "default_pool" in mapping:
            raise ValueError("default_pool belongs to Airflow")
        if info.field_name == "api_accounts" and any(n.lower() == "admin" for n in mapping):
            raise ValueError("admin is the built-in Airflow account")
        return mapping


def effective_resources(config, record) -> AirflowResources:
    from vecten_shared.declarations import component_for, secret_problem

    result = AirflowResources()
    sources = [("project", config.airflow, config.secrets)]
    sources += [(name, entry.airflow, entry.secrets) for name, entry in sorted(record.items())]
    for owner, resources, names in sources:
        if resources is None:
            continue
        for account, secret in resources.api_accounts.items():
            if secret not in names or secret_problem(secret) or component_for(secret) != "platform":
                raise Failure(
                    ExitCode.CONFIG,
                    f"{owner}: account {account} needs its own declared platform secret",
                )
            if any(secret in other_names for other, _, other_names in sources if other != owner):
                raise Failure(
                    ExitCode.CONFIG,
                    f"{owner}: account secret {secret} is shared with another declaration",
                )
        for field in AirflowResources.model_fields:
            values = getattr(resources, field)
            current = getattr(result, field)
            if duplicate := current.keys() & values.keys():
                raise Failure(
                    ExitCode.CONFIG,
                    f"{owner}: duplicate Airflow {field}: {', '.join(sorted(duplicate))}",
                )
            current.update(values)
    return result
