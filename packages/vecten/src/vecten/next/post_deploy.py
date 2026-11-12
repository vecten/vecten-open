"""The items `next` reports after a deploy, from vecten.toml and the local git state."""

from __future__ import annotations

import time
from pathlib import Path

from vecten.deploy.snowflake import identity_pending
from vecten.intake.facts import OIDC_REGISTERED_PREFIX
from vecten.mcp_url import mcp_url
from vecten.next.declared import declared_secret_items
from vecten.next.deploy import PUSH
from vecten.next.gitstate import GitState
from vecten.next.ingestion import access_apply_items
from vecten.next.items import Item
from vecten.password_links import owed
from vecten.secrets import recorded_secret_names
from vecten_shared.config import EnvironmentFacts, VectenConfig
from vecten_shared.output import Failure
from vecten_shared.records import read_record

OIDC = Item(
    "ask",
    "Which OIDC issuer and client id should sign people in?",
    "Which OIDC issuer URL and client id should the MCP use to sign people in? "
    '(Set [mcp] auth = "password" with `vecten answer mcp.auth password` to opt out.)',
    answer_keys=("mcp.oidc_issuer", "mcp.oidc_client_id"),
)
ADMIN = Item(
    "ask",
    "Who is the first admin?",
    "Which email address should be the first admin of the MCP?",
    answer_keys=("mcp.admins",),
)


def _register_callback(root: Path, config: VectenConfig, env: str) -> Item:
    return Item(
        "ask",
        f"Register {mcp_url(root, config, env)}/oidc/callback with your identity "
        "provider. If it gave you a client secret, store it with "
        f"`vecten secret set MCP_OIDC_CLIENT_SECRET --env {env}`.",
        f"vecten answer {OIDC_REGISTERED_PREFIX}{env} yes",
        answer_keys=(f"{OIDC_REGISTERED_PREFIX}{env}",),
        allowed=("yes",),
    )


def _signed_in(root: Path, config: VectenConfig, env: str) -> bool:
    """Whether this machine holds a session the admin calls can use, saved by this project's
    login; a cache or a record that cannot be read counts as no session."""
    # Deferred so that `next` loads httpx only when it has a session to look for.
    from vecten.mcp_client import TokenCache, login_record, session_usable

    try:
        session = TokenCache().load(mcp_url(root, config, env))
        record = read_record(root, login_record(env))
    except Failure:
        return False
    # A client id the record does not name is a session another project left under the
    # same issuer.
    return (
        session_usable(session, time.time())
        and isinstance(record, dict)
        and session.get("client_id") == record.get("client_id")
    )


def _sign_in_items(
    root: Path, config: VectenConfig, env: str, facts: EnvironmentFacts
) -> list[Item]:
    """What stands between the first admin and the admin calls that follow: the IdP
    registration or the password link, both needed once, then a session on this machine."""
    mcp = config.mcp
    if mcp.auth == "oidc":
        if not (mcp.oidc_issuer and mcp.oidc_client_id):
            return []
        if not facts.oidc_registered:
            return [] if facts.first_user_added else [_register_callback(root, config, env)]
    elif mcp.auth == "password":
        if not facts.admin_link_issued and not facts.first_user_added:
            title = "Give the first admin a password link"
            return [Item("command", title, f"vecten mcp admin-link --env {env}")]
    if _signed_in(root, config, env):
        return []
    return [Item("command", "Sign in to the MCP as an admin", f"vecten login --env {env}")]


def _waiting_hostname_items(root: Path, config: VectenConfig, env: str) -> list[Item]:
    """A bound service hostname gets its DNS record from the next deploy; without the
    environment's outputs on this machine there is nothing to compare with."""
    # Deferred so that `next` loads the deploy modules only for a project with a hostname.
    from vecten.deploy.shipping import unserved_hostnames
    from vecten_shared.extension_record import read_record as read_extension_record
    from vecten_shared.provider.environment import environment_file, load_outputs

    # A bound container app is served at its own name when it binds no hostname.
    if not config.apps and not any(
        b.hostname for bindings in config.services.values() for b in bindings.values()
    ):
        return []
    path = environment_file(root, env)
    if not path.is_file():
        return []
    try:
        outputs = load_outputs(path, env)
        names = unserved_hostnames(config, read_extension_record(root) or {}, outputs)
    except Failure:
        return []
    if not names:
        return []
    noun = "record" if len(names) == 1 else "records"
    return [
        Item(
            "command", f"Create the DNS {noun} for {', '.join(names)}", f"vecten deploy --env {env}"
        )
    ]


def app_connect_items(root: Path, config: VectenConfig, env: str) -> list[Item]:
    """`vecten app connect` for each bound container app whose credentials `env` has not
    received from this checkout; a binding that does not render yet is reported elsewhere."""
    if not config.apps:
        return []
    # Deferred so that `next` loads the app helpers only for a project that binds an app.
    from vecten.app_connect import app_secret_names

    recorded = recorded_secret_names(root, env)
    items = []
    for extension, bindings in sorted(config.apps.items()):
        for app in sorted(bindings):
            try:
                names = app_secret_names(root, config, extension, app)
            except Failure:
                continue
            if not set(names) <= recorded:
                key = f"{extension}.{app}"
                items.append(
                    Item(
                        "command",
                        f"Connect {key} to the MCP in {env}",
                        f"vecten app connect {key} --env {env}",
                    )
                )
    return items


def post_deploy_items(root: Path, config: VectenConfig, env: str, git: GitState) -> list[Item]:
    facts = config.environments.get(env) or EnvironmentFacts()
    if not facts.deployed_commit:
        return []
    # An answer recorded after the deploy changes vecten.toml, which reaches the MCP when
    # the push ships it, so it is pushed before anything else is asked.
    items: list[Item] = [PUSH] if git.dirty or git.ahead else []
    # An extension added after the deploy can declare secrets the environment does not hold.
    items += declared_secret_items(root, config, env)
    items += _waiting_hostname_items(root, config, env)
    if not config.has("mcp"):
        return items
    # Nothing below works before the MCP runs, and it cannot start without its identity.
    if identity_pending(config, env):
        title = "Set up the MCP's Snowflake identity"
        return [*items, Item("command", title, f"vecten mcp provisioner-script --env {env}")]
    mcp = config.mcp
    if mcp.auth == "oidc" and not (mcp.oidc_issuer and mcp.oidc_client_id):
        items.append(OIDC)
    if not mcp.admins:
        items.append(ADMIN)
    admin_work: list[Item] = []
    if not facts.first_user_added:
        # `vecten user add` records the answer, so the ask names no answer key.
        title = "Who should have access first? Give an email address and a name."
        command = f'vecten user add --email <email> --name "<name>" --env {env}'
        admin_work.append(Item("ask", title, command))
    if mcp.auth == "password":
        admin_work += [
            Item(
                "command",
                f"Give {link['email']} a password link",
                f"vecten user password-link {link['id']} --env {env}",
            )
            for link in owed(root, env)
        ]
    admin_work += access_apply_items(root, config, env)
    admin_work += app_connect_items(root, config, env)
    if admin_work and mcp.admins:
        items += _sign_in_items(root, config, env, facts)
    items += admin_work
    if not facts.connection_shown:
        title = "Show the MCP URL and how to connect a client"
        items.append(Item("command", title, f"vecten mcp connection --env {env}"))
    # Only the platform's syncs post to the webhook.
    if config.has("platform") and not facts.alert_webhook_asked:
        title = (
            "Should sync failures be posted to a webhook? "
            f"On yes, run `vecten secret set ALERT_WEBHOOK_URL --env {env}` first."
        )
        items.append(
            Item(
                "ask",
                title,
                f"vecten answer alert_webhook.{env} <yes|no>",
                answer_keys=(f"alert_webhook.{env}",),
                allowed=("yes", "no"),
            )
        )
    return items
