"""A one-time set-password link for a deployed MCP admin, carried by the secrets store.

The MCP reads MCP_ADMIN_LINK when the link is used, so the link works whenever the operator
issues it rather than only in the minutes after the server started.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from vecten.mcp_url import mcp_url
from vecten.password_links import admin_link_issued
from vecten_shared.config import load_config, save_config
from vecten_shared.names import check_env_name
from vecten_shared.output import ExitCode, Failure

ADMIN_LINK_SECRET = "MCP_ADMIN_LINK"
# The MCP's PasswordBackend refuses a link this many seconds after `issued_at`.
EXPIRES_IN = 900


def issue_admin_link(project_root: Path, env: str, email: str | None) -> dict:
    if env == "local":
        raise Failure(
            ExitCode.REFUSED,
            "the local MCP reads secrets only when it starts; run "
            "`vecten secret set MCP_ADMIN_SETUP_TOKEN`, then `vecten down` and `vecten up`",
        )
    check_env_name(env)
    config = load_config(project_root)
    if not config.has("mcp"):
        raise Failure(ExitCode.REFUSED, "the project does not include the mcp component")
    facts = config.environments.get(env)
    if config.deploy is None or facts is None or not facts.deployed_commit:
        raise Failure(
            ExitCode.REFUSED,
            f"{env} has not been deployed yet; run `vecten deploy --env {env}` first",
        )
    if config.mcp.auth != "password":
        raise Failure(
            ExitCode.REFUSED,
            f"the MCP signs people in with {config.mcp.auth}, not password; "
            "admin-link issues password links only",
        )
    admins = {admin.lower() for admin in config.mcp.admins}
    if not admins:
        raise Failure(
            ExitCode.REFUSED, "[mcp] admins in vecten.toml is empty; add the first admin's email"
        )
    chosen = (email or config.mcp.admins[0]).strip().lower()
    if chosen not in admins:
        raise Failure(
            ExitCode.REFUSED,
            f"{chosen} is not in [mcp] admins in vecten.toml; one of: {', '.join(sorted(admins))}",
        )
    from vecten.password_links import read_links
    from vecten.secrets import set_secret

    # A corrupt record or an unreadable tfvars fails here, before the one-time token exists,
    # so none is lost unprinted.
    read_links(project_root, env)
    url = mcp_url(project_root, config, env) + "/set-password"
    token = secrets.token_urlsafe(32)
    value = json.dumps({"token": token, "email": chosen, "issued_at": time.time()})
    set_secret(project_root, ADMIN_LINK_SECRET, value, env, from_local=False)
    config = load_config(project_root)
    config.environments[env].admin_link_issued = True
    save_config(project_root, config)
    admin_link_issued(project_root, env, chosen)
    return {
        "email": chosen,
        "env": env,
        # The token travels beside the URL, never in it, so no access log on the way holds it.
        "url": url,
        "setup_token": token,
        "expires_in": EXPIRES_IN,
    }
