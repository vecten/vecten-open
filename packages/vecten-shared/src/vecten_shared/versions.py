"""The versions table: one place for every external pin the product carries."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib import resources


def minor_constraint(version: str) -> str:
    major, minor = version.split(".")[:2]
    return f">={major}.{minor},<{major}.{int(minor) + 1}"


@dataclass(frozen=True)
class Versions:
    vecten: str
    python: str
    airflow_image: str
    postgres_major: str
    nginx_image: str
    log_cleaner_image: str
    mcp_base_image: str
    certbot_image: str
    dbt_core: str
    dbt_postgres: str
    dbt_snowflake: str
    fakesnow: str
    dlt: str
    mcp_sdk: str
    sqlglot: str
    airflow_mcp_server: str
    dbt_mcp: str
    boto3: str
    psycopg: str
    terraform: str
    terraform_module_ref: str
    terraform_aws_provider: str
    terraform_snowflake_provider: str
    terraform_scaleway_provider: str
    terraform_random_provider: str
    docker_compose_plugin: str
    docker_buildx_plugin: str
    ruff: str
    github_runner: str
    github_actions: dict[str, str]
    ci_playwright_image: str
    vecten_convention: str
    ossie_spec: str
    schema_convention: int

    @property
    def airflow_version(self) -> str:
        return self.airflow_image.rsplit(":", 1)[1].split("-")[0]


def _load() -> Versions:
    raw = tomllib.loads(
        resources.files("vecten_shared").joinpath("versions.toml").read_text(encoding="utf-8")
    )
    return Versions(
        vecten=raw["vecten"],
        python=raw["python"],
        airflow_image=raw["images"]["airflow"],
        postgres_major=raw["images"]["postgres_major"],
        nginx_image=raw["images"]["nginx"],
        log_cleaner_image=raw["images"]["log_cleaner"],
        mcp_base_image=raw["images"]["mcp_base"],
        certbot_image=raw["images"]["certbot"],
        dbt_core=raw["python_packages"]["dbt_core"],
        dbt_postgres=raw["python_packages"]["dbt_postgres"],
        dbt_snowflake=raw["python_packages"]["dbt_snowflake"],
        fakesnow=raw["python_packages"]["fakesnow"],
        dlt=raw["python_packages"]["dlt"],
        mcp_sdk=raw["python_packages"]["mcp_sdk"],
        sqlglot=raw["python_packages"]["sqlglot"],
        airflow_mcp_server=raw["python_packages"]["airflow_mcp_server"],
        dbt_mcp=raw["python_packages"]["dbt_mcp"],
        boto3=raw["python_packages"]["boto3"],
        psycopg=raw["python_packages"]["psycopg"],
        terraform=raw["terraform"]["engine"],
        terraform_module_ref=raw["terraform"]["module_ref"],
        terraform_aws_provider=raw["terraform"]["aws_provider"],
        terraform_snowflake_provider=raw["terraform"]["snowflake_provider"],
        terraform_scaleway_provider=raw["terraform"]["scaleway_provider"],
        terraform_random_provider=raw["terraform"]["random_provider"],
        docker_compose_plugin=raw["tools"]["docker_compose_plugin"],
        docker_buildx_plugin=raw["tools"]["docker_buildx_plugin"],
        ruff=raw["tools"]["ruff"],
        github_runner=raw["github_actions"]["runner"],
        github_actions={k: v for k, v in raw["github_actions"].items() if k != "runner"},
        ci_playwright_image=raw["ci"]["playwright_image"],
        vecten_convention=raw["dbt_packages"]["vecten_convention"],
        ossie_spec=raw["specs"]["ossie"],
        schema_convention=raw["specs"]["schema_convention"],
    )


VERSIONS = _load()

_PYPI_TO_FIELD = {
    "dbt-core": "dbt_core",
    "dbt-postgres": "dbt_postgres",
    "dbt-snowflake": "dbt_snowflake",
    "fakesnow": "fakesnow",
    "dlt": "dlt",
    "mcp": "mcp_sdk",
    "sqlglot": "sqlglot",
    "boto3": "boto3",
    "psycopg": "psycopg",
}


def normalize_name(distribution: str) -> str:
    """PEP 503 form, so `dbt_core`, `DBT-Core` and `dbt.core` all resolve to `dbt-core`."""
    return re.sub(r"[-_.]+", "-", distribution).lower()


def pins_for(distribution: str) -> str | None:
    field = _PYPI_TO_FIELD.get(normalize_name(distribution))
    return minor_constraint(getattr(VERSIONS, field)) if field else None
