"""Per-environment tfvars: the subset of HCL `init` renders, checked against vecten.toml."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from vecten.deploy.sizing import TIERS
from vecten_shared.config import APEX, VectenConfig
from vecten_shared.names import check_env_name, env_name_problem
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import recorded_layout, shared_layout, snowflake_names

REQUIRED = (
    "environment",
    "project_name",
    "domain",
    "dns_zone",
    "region",
    "github_repository",
    "components",
    "profile",
    "size",
    "warehouse",
)
SECOND_WAREHOUSE = "a second warehouse is a second deployment"
# Read by `ship` when it renders a host's stack; Terraform has no use for it, and the cloud
# root declares it only so the shared tfvars file plans without an undeclared-variable warning.
DAGS_PAUSED_AT_CREATION = "dags_paused_at_creation"
_KEY = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")


def _no_constant(name: str) -> Any:
    raise ValueError(name)


_DECODER = json.JSONDecoder(parse_constant=_no_constant)
_TEMPLATE = ("${", "%{")


def has_snowflake_root(config: VectenConfig) -> bool:
    # `init` renders deploy/terraform/snowflake under exactly this condition: the platform
    # builds the warehouse, so an MCP over an existing Snowflake account has no root.
    return config.warehouse.kind == "snowflake" and "platform" in config.components


def env_tfvars_path(project_root: Path, env: str) -> Path:
    return project_root / "deploy" / "envs" / f"{env}.tfvars"


def root_var_file(env: str) -> str:
    """`deploy/envs/<env>.tfvars` as Terraform reaches it from a root in `deploy/terraform/`."""
    return f"../../envs/{env}.tfvars"


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in [k, *_strings(v)]]
    return []


def _value(text: str) -> Any:
    # `init` renders every value with Jinja's `tojson`, whose output HCL reads as the same
    # value. null, NaN and Infinity have no HCL literal, so they are refused here.
    value, end = _DECODER.raw_decode(text)
    rest = text[end:].strip()
    if value is None or (rest and not rest.startswith("#")):
        raise ValueError
    return value


def read_tfvars(path: Path) -> dict[str, Any]:
    """One `key = value` per line, the value JSON; blank lines and `#` comments are skipped."""
    values: dict[str, Any] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _KEY.fullmatch(line)
        try:
            if match is None:
                raise ValueError
            value = _value(match.group(2))
        except ValueError:
            raise Failure(
                ExitCode.CONFIG,
                f"{path}: line {number} is not `key = value` with a JSON value "
                "(a quoted string, a list, an object, a finite number or true/false)",
            ) from None
        key = match.group(1)
        # HCL expands `${...}` and `%{...}` inside quoted strings, so Terraform would not
        # read the value this parser returns.
        if any(t in s for s in _strings(value) for t in _TEMPLATE):
            raise Failure(
                ExitCode.CONFIG,
                f"{path}: line {number} holds a `${{` or `%{{` template sequence in {key}",
            )
        if key in values:
            raise Failure(ExitCode.CONFIG, f"{path}: line {number} sets {key} a second time")
        values[key] = value
    return values


ACCOUNT_HINT = (
    "record the account with `vecten answer snowflake_account <ORGANIZATION-ACCOUNT>`, then run "
    "`vecten init --deploy`, which writes it to both the tfvars and vecten.toml"
)


def _mismatch(rel: str, key: str, found: Any, expected: Any, where: str) -> Failure:
    return Failure(ExitCode.CONFIG, f"{rel} sets {key} = {found!r}; {where} says {expected!r}")


def no_tfvars(env: str) -> Failure:
    """The refusal for an environment whose `deploy/envs/<env>.tfvars` does not exist."""
    hint = (
        "run `vecten init --deploy` to render it"
        if env == "prod"
        else f'copy deploy/envs/prod.tfvars to it, set environment = "{env}" and set '
        "domain to the name this environment serves under"
    )
    return Failure(ExitCode.REFUSED, f"environment {env!r} has no deploy/envs/{env}.tfvars; {hint}")


def check_tfvars(project_root: Path, config: VectenConfig, env: str) -> dict[str, Any]:
    """Read `deploy/envs/<env>.tfvars` and refuse it unless it describes this project."""
    check_env_name(env)
    rel = f"deploy/envs/{env}.tfvars"
    path = env_tfvars_path(project_root, env)
    if not path.is_file():
        raise no_tfvars(env)
    values = read_tfvars(path)
    snowflake = config.warehouse.kind == "snowflake"
    required = REQUIRED + (("snowflake_account",) if snowflake else ())
    missing = [k for k in required if values.get(k) in (None, "")]
    if missing:
        hint = f"; {ACCOUNT_HINT}" if "snowflake_account" in missing else ""
        raise Failure(
            ExitCode.CONFIG,
            f"{rel} is missing {', '.join(missing)}{hint}",
            details={"missing": missing},
        )
    if values["environment"] != env:
        raise _mismatch(rel, "environment", values["environment"], env, "the file name")
    # Every environment serves under its own tfvars domain; `prod` is the one vecten.toml names.
    if env == "prod" and config.deploy is not None and values["domain"] != config.deploy.domain:
        raise _mismatch(
            rel, "domain", values["domain"], config.deploy.domain, "vecten.toml [deploy] domain"
        )
    # The Scaleway state backend's region is fixed when the deploy layer renders, so
    # resources in another region would split from their state.
    deploy = config.deploy
    if deploy is not None and deploy.cloud == "scaleway" and values["region"] != deploy.region:
        raise _mismatch(
            rel,
            "region",
            values["region"],
            deploy.region,
            "vecten.toml [deploy] region, which the rendered state backend uses,",
        )
    if values["warehouse"] != config.warehouse.kind:
        raise Failure(
            ExitCode.CONFIG,
            f"{rel} sets warehouse = {values['warehouse']!r}; vecten.toml says "
            f"{config.warehouse.kind!r}: {SECOND_WAREHOUSE}",
        )
    # Each environment names its own Snowflake account; `prod`'s is the one vecten.toml names.
    if snowflake and env == "prod":
        found, expected = values["snowflake_account"], config.warehouse.account
        if not expected:
            raise Failure(
                ExitCode.CONFIG,
                f"{rel} sets snowflake_account = {found!r} but vecten.toml sets no "
                f"[warehouse] account; {ACCOUNT_HINT}",
            )
        if found != expected:
            raise _mismatch(
                rel, "snowflake_account", found, expected, "vecten.toml [warehouse] account"
            )
    if has_snowflake_root(config):
        _check_account_layout(project_root, config, env, str(values["snowflake_account"]))
    if not snowflake and "snowflake_account" in values:
        raise Failure(
            ExitCode.CONFIG,
            f"{rel} sets snowflake_account on a Postgres project: {SECOND_WAREHOUSE}",
        )
    components = values["components"]
    if not isinstance(components, list) or sorted(map(str, components)) != sorted(
        config.components
    ):
        raise _mismatch(rel, "components", components, config.components, "vecten.toml")
    if values["profile"] != config.profile:
        raise _mismatch(rel, "profile", values["profile"], config.profile, "vecten.toml")
    if values["project_name"] != config.project.name:
        raise _mismatch(
            rel, "project_name", values["project_name"], config.project.name, "vecten.toml"
        )
    if values["size"] not in TIERS:
        raise Failure(
            ExitCode.CONFIG,
            f"{rel} sets size = {values['size']!r}; size must be one of: {', '.join(TIERS)}",
        )
    paused = values.get(DAGS_PAUSED_AT_CREATION, False)
    if not isinstance(paused, bool):
        raise Failure(
            ExitCode.CONFIG,
            f"{rel} sets {DAGS_PAUSED_AT_CREATION} = {paused!r}; it must be true or false",
        )
    _check_service_hostnames(rel, config, values)
    return values


def environment_accounts(project_root: Path) -> dict[str, str]:
    """The Snowflake account each environment's `deploy/envs/<env>.tfvars` names."""
    accounts: dict[str, str] = {}
    envs = project_root / "deploy" / "envs"
    for path in sorted(envs.glob("*.tfvars")) if envs.is_dir() else []:
        if env_name_problem(path.stem):
            continue
        account = read_tfvars(path).get("snowflake_account")
        if isinstance(account, str) and account:
            accounts[path.stem] = account
    return accounts


def _files(envs: list[str]) -> str:
    return ", ".join(f"deploy/envs/{env}.tfvars" for env in envs)


def _check_account_layout(project_root: Path, config: VectenConfig, env: str, account: str) -> None:
    """Refuse an account whose names would meet another environment's: fixed names hold an
    account alone, and a shared account holds no environment with fixed names."""
    rel = f"deploy/envs/{env}.tfvars"
    shared = shared_layout(config, env)
    if shared:
        # Refuses an environment name or base the suffixed names cannot carry.
        snowflake_names(config, env)
    # Snowflake account identifiers are case-insensitive.
    others = sorted(
        other
        for other, named in environment_accounts(project_root).items()
        if other != env and named.upper() == account.upper()
    )
    if not shared and recorded_layout(config, env) is None and others:
        raise Failure(
            ExitCode.CONFIG,
            f"{rel} names Snowflake account {account!r}, which {_files(others)} also "
            f"names; with fixed Snowflake names an account holds one environment of the "
            f"project, so give {env} an account of its own, or set [warehouse] "
            "shared_account = true before its first deploy",
        )
    # A deployed fixed environment holds its account already; a fixed environment beside it
    # is refused at its own first deploy, above.
    beside = [other for other in others if shared_layout(config, other) != shared]
    if beside:
        layout = "fixed" if shared else "shared-account"
        raise Failure(
            ExitCode.CONFIG,
            f"{rel} names Snowflake account {account!r}, which {_files(beside)} also names "
            f"for an environment with {layout} Snowflake names; a fixed environment holds "
            f"its account alone, so give {'this environment' if shared else 'that one'} "
            "another account",
        )


def _check_service_hostnames(rel: str, config: VectenConfig, values: dict[str, Any]) -> None:
    # `deploy` derives the variable from vecten.toml, so a tfvars entry would be a second
    # source that one environment alone follows.
    if "service_hostnames" in values:
        raise Failure(
            ExitCode.CONFIG,
            f"{rel} sets service_hostnames; `vecten deploy` passes it from the hostnames "
            "vecten.toml binds, so remove the line",
        )
    # Each environment serves under its own tfvars domain, which may be the zone itself.
    if values["domain"] != values["dns_zone"]:
        return
    for extension, bindings in sorted(config.services.items()):
        for name, binding in sorted(bindings.items()):
            if binding.hostname == APEX:
                raise Failure(
                    ExitCode.CONFIG,
                    f"vecten.toml: services.{extension}.{name}.hostname: @ is "
                    f"{values['domain']}, the DNS zone itself in {rel}, whose apex records "
                    "the zone keeps; choose a label",
                )
