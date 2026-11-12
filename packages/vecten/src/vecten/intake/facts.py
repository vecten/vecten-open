"""Post-deploy answers that `vecten answer` records in vecten.toml, not in vecten-answers.yml."""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args
from urllib.parse import urlsplit

from vecten_shared.config import McpAuth, load_config, promotion_problem, save_config
from vecten_shared.names import check_env_name
from vecten_shared.output import ExitCode, Failure

MCP_KEYS = ("mcp.auth", "mcp.oidc_issuer", "mcp.oidc_client_id", "mcp.admins")
ALERT_PREFIX = "alert_webhook."
ALERT_CHOICES = ("yes", "no")
OIDC_REGISTERED_PREFIX = "oidc_registered."
PROMOTION_KEY = "deploy.promotion"
NO_PROMOTION = "none"


def is_mcp_key(key: str) -> bool:
    return key in MCP_KEYS


def is_alert_key(key: str) -> bool:
    return key.startswith(ALERT_PREFIX) and len(key) > len(ALERT_PREFIX)


def _usage(message: str) -> Failure:
    return Failure(ExitCode.USAGE, message)


def _parse_mcp_value(key: str, raw: str, auth_names: tuple[str, ...] = ()) -> Any:
    value = raw.strip()
    if key == "mcp.auth":
        choices = (*get_args(McpAuth), *auth_names)
        if value.lower() not in choices:
            raise _usage(f"{key} must be one of: {', '.join(choices)}")
        return value.lower()
    if key == "mcp.oidc_issuer":
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise _usage(f"{key} must be an https:// URL; got {value!r}")
        return value
    if key == "mcp.oidc_client_id":
        if not value:
            raise _usage(f"{key} must not be empty")
        return value
    admins = [a.strip() for a in raw.split(",") if a.strip()]
    if not admins:
        raise _usage(f"{key} must name at least one email address")
    bad = [a for a in admins if "@" not in a]
    if bad:
        raise _usage(f"{key} must be a comma-separated list of email addresses; got {bad[0]!r}")
    return admins


def set_mcp_fact(project_root: Path, key: str, raw: str) -> Any:
    if not is_mcp_key(key):
        raise _usage(f"unknown mcp answer key {key!r}; known keys: {', '.join(MCP_KEYS)}")
    config = load_config(project_root)
    from vecten_shared.extensions import selected_entry_points

    auth_names = (
        tuple(ep.name for ep in selected_entry_points(config, "vecten.mcp.auth"))
        if key == "mcp.auth"
        else ()
    )
    value = _parse_mcp_value(key, raw, auth_names)
    setattr(config.mcp, key.removeprefix("mcp."), value)
    save_config(project_root, config)
    return value


def is_oidc_registered_key(key: str) -> bool:
    return key.startswith(OIDC_REGISTERED_PREFIX) and len(key) > len(OIDC_REGISTERED_PREFIX)


def set_oidc_registered_fact(project_root: Path, env: str, raw: str) -> bool:
    """Record that the human registered the MCP's OIDC callback; only a yes is an answer,
    since sign-in cannot work without the registration."""
    check_env_name(env)
    if raw.strip().lower() != "yes":
        raise _usage(f"{OIDC_REGISTERED_PREFIX}{env} takes yes once the callback URL is registered")
    config = load_config(project_root)
    config.environments[env].oidc_registered = True
    save_config(project_root, config)
    return True


def set_alert_fact(project_root: Path, env: str, raw: str) -> bool:
    check_env_name(env)
    answer = raw.strip().lower()
    if answer not in ALERT_CHOICES:
        raise _usage(f"{ALERT_PREFIX}{env} must be one of: {', '.join(ALERT_CHOICES)}")
    config = load_config(project_root)
    config.environments[env].alert_webhook_asked = True
    save_config(project_root, config)
    return answer == "yes"


def set_wanted(root: Path, sources: list[str]) -> list[str]:
    from vecten.intake.answers import SOURCES_NEED_PLATFORM

    config = load_config(root)
    if not config.has("platform"):
        raise _usage(SOURCES_NEED_PLATFORM)
    declared = {c.name for c in config.connectors}
    config.wanted = [s for s in sources if s not in declared]
    save_config(root, config)
    return config.wanted


def is_promotion_key(key: str) -> bool:
    return key == PROMOTION_KEY


def set_promotion(project_root: Path, raw: str) -> list[str] | None:
    """Record the promotion order, or remove it on `none`; `save_config` refuses an order
    under workflows that predate it."""
    from vecten.deploy.tfvars import env_tfvars_path

    config = load_config(project_root)
    if config.deploy is None:
        raise Failure(
            ExitCode.REFUSED,
            f"{PROMOTION_KEY} orders the deployed environments; this project has no deploy "
            "layer; run `vecten init --deploy` first",
        )
    order: list[str] | None = None
    if raw.strip().lower() != NO_PROMOTION:
        order = [env.strip() for env in raw.split(",") if env.strip()]
        if not order:
            raise _usage(f"{PROMOTION_KEY} must list environments, as in dev,prod, or be none")
        problem = promotion_problem(order)
        if problem:
            raise _usage(f"{PROMOTION_KEY} {problem}")
        for env in order:
            if not env_tfvars_path(project_root, env).is_file():
                raise _usage(f"{PROMOTION_KEY}: {env} has no deploy/envs/{env}.tfvars")
    config.deploy.promotion = order
    save_config(project_root, config)
    return order
