"""The scaffold's parameters: the intake answers and nothing else."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, ValidationInfo, field_validator

from vecten.intake.answers import DECLINED, SOURCES_NEED_PLATFORM
from vecten.intake.sizing import size_for_users
from vecten_shared.config import (
    Cloud,
    Component,
    Profile,
    Size,
    WarehouseKind,
    WarehouseSection,
    check_deploy_value,
    parse_warehouse_locator,
)
from vecten_shared.output import ExitCode, Failure
from vecten_shared.versions import VERSIONS

DEFAULT_REGION: dict[str, str] = {"aws": "eu-central-1", "scaleway": "fr-par"}
ALLOWED = {
    "cloud": ("aws", "scaleway"),
    "warehouse": ("postgres", "snowflake"),
    "components": ("platform", "mcp"),
    "profile": ("standard", "minimal"),
    "size": ("small", "medium", "large"),
}
DEPLOY_FIELDS = ("cloud", "region", "domain", "dns_zone", "github_repository")
PROJECT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}")
HAS_WAREHOUSE_YES = ("yes", "true", "1")
HAS_WAREHOUSE_NO = ("no", "false", "0")
HAS_WAREHOUSE_ANSWERS = ("yes", "no", "true", "false", "1", "0")


def _has_warehouse(merged: dict[str, Any]) -> bool:
    if "has_warehouse" not in merged:
        return "warehouse_url" in merged
    answer = str(merged.pop("has_warehouse")).lower()
    if answer not in HAS_WAREHOUSE_ANSWERS:
        allowed = ", ".join(HAS_WAREHOUSE_ANSWERS)
        raise Failure(ExitCode.USAGE, f"has_warehouse must be one of: {allowed}")
    if answer in HAS_WAREHOUSE_NO and "warehouse_url" in merged:
        raise Failure(
            ExitCode.USAGE,
            "warehouse_url describes an existing warehouse, which needs has_warehouse yes",
        )
    return answer in HAS_WAREHOUSE_YES


class ScaffoldParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_name: str
    users: int
    sources: list[str] = []
    warehouse: WarehouseKind = "postgres"
    size: Size = "small"
    components: list[Component] = ["platform", "mcp"]
    profile: Profile = "standard"
    has_warehouse: bool = False
    warehouse_url: str | None = None
    cloud: Cloud | None = None
    region: str | None = None
    domain: str | None = None
    dns_zone: str | None = None
    github_repository: str | None = None
    snowflake_account: str | None = None

    @field_validator("region", "domain", "dns_zone", "github_repository", "snowflake_account")
    @classmethod
    def _deploy_value(cls, value: str | None, info: ValidationInfo) -> str | None:
        return check_deploy_value(info.field_name, value)

    @property
    def render_deploy(self) -> bool:
        return all(getattr(self, f) for f in DEPLOY_FIELDS)

    @property
    def vecten_version(self) -> str:
        return VERSIONS.vecten

    @property
    def airflow_image(self) -> str:
        return VERSIONS.airflow_image

    @property
    def postgres_major(self) -> str:
        return VERSIONS.postgres_major

    @property
    def terraform_module_ref(self) -> str:
        return VERSIONS.terraform_module_ref

    @property
    def convention_version(self) -> int:
        return VERSIONS.schema_convention

    @property
    def ossie_spec(self) -> str:
        return VERSIONS.ossie_spec

    @property
    def warehouse_locator(self) -> WarehouseSection:
        if self.warehouse_url:
            return parse_warehouse_locator(self.warehouse_url)
        return WarehouseSection(kind=self.warehouse, existing=False)

    def copier_data(self) -> dict[str, Any]:
        loc = self.warehouse_locator
        # Copier rejects None for str and int questions; "" and 0 are their unset defaults.
        data = {
            **self.model_dump(),
            "render_deploy": self.render_deploy,
            "vecten_version": self.vecten_version,
            "airflow_image": self.airflow_image,
            "postgres_major": self.postgres_major,
            "terraform_module_ref": self.terraform_module_ref,
            "terraform_engine": VERSIONS.terraform,
            "terraform_snowflake_provider": VERSIONS.terraform_snowflake_provider,
            "terraform_scaleway_provider": VERSIONS.terraform_scaleway_provider,
            "github_runner": VERSIONS.github_runner,
            "ruff_version": VERSIONS.ruff,
            **{f"action_{name}": uses for name, uses in VERSIONS.github_actions.items()},
            "convention_version": self.convention_version,
            "ossie_spec": self.ossie_spec,
            "warehouse_host": loc.host,
            "warehouse_port": loc.port or 0,
            "warehouse_database": loc.database,
            "warehouse_account": loc.account or self.snowflake_account,
            "warehouse_admin_user": loc.admin_user,
        }
        return {k: "" if v is None else v for k, v in data.items()}

    @classmethod
    def from_answers(cls, answers: dict[str, Any], overrides: dict[str, Any]) -> ScaffoldParams:
        # Copier answers files carry "" for every unset text question, and a declined deploy
        # question is recorded as `none`; both leave the question unanswered.
        merged = {
            k: v
            for k, v in {**answers, **overrides}.items()
            if v is not None and v != "" and not (k in DEPLOY_FIELDS and v == DECLINED)
        }
        has_warehouse = _has_warehouse(merged)
        for field, allowed in ALLOWED.items():
            value = merged.get(field)
            values = value if isinstance(value, list) else ([value] if value is not None else [])
            bad = [v for v in values if v not in allowed]
            if bad:
                raise Failure(
                    ExitCode.USAGE,
                    f"{field} must be one of: {', '.join(allowed)}; got {bad[0]!r}",
                )
        missing = [f for f in ("name", "users") if f not in merged]
        if has_warehouse and "warehouse_url" not in merged:
            missing.append("warehouse_url")
        if not has_warehouse and "sources" not in merged:
            missing.append("sources or warehouse_url")
        if missing:
            raise Failure(
                ExitCode.USAGE,
                f"missing intake answers: {', '.join(missing)}",
                details={"missing": missing},
            )
        if not PROJECT_NAME.fullmatch(str(merged["name"])):
            raise Failure(
                ExitCode.USAGE,
                "name must be lowercase letters, digits, hyphens or underscores, start with a "
                "letter and be at most 63 characters",
            )
        if has_warehouse:
            if merged.get("sources"):
                raise Failure(ExitCode.USAGE, SOURCES_NEED_PLATFORM)
            merged["components"] = ["mcp"]
            merged["warehouse"] = parse_warehouse_locator(merged["warehouse_url"]).kind
        if merged.get("snowflake_account") and (
            has_warehouse or merged.get("warehouse") != "snowflake"
        ):
            raise Failure(
                ExitCode.USAGE,
                "snowflake_account names the account the platform builds its Snowflake "
                "warehouse in; an existing warehouse's locator names its own account",
            )
        try:
            users = int(merged["users"])
        except TypeError, ValueError:
            users = 0
        if users <= 0:
            raise Failure(ExitCode.USAGE, "users must be a positive integer")
        data = {
            "project_name": merged["name"],
            "users": users,
            "sources": merged.get("sources", []) or [],
            "warehouse": merged.get("warehouse", "postgres"),
            "size": merged.get("size") or size_for_users(users),
            "components": merged.get("components", ["platform", "mcp"]),
            "profile": merged.get("profile", "standard"),
            "has_warehouse": has_warehouse,
            "warehouse_url": merged.get("warehouse_url"),
            "cloud": merged.get("cloud"),
            "region": merged.get("region") or DEFAULT_REGION.get(merged.get("cloud", "")),
            "domain": merged.get("domain"),
            "dns_zone": merged.get("dns_zone"),
            "github_repository": merged.get("github_repository"),
            "snowflake_account": merged.get("snowflake_account"),
        }
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(map(str, first["loc"]))
            raise Failure(ExitCode.USAGE, f"{loc}: {first['msg']}") from None
