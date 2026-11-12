"""`vecten app connect`: an analytics app's principal through the admin API, and its client
credentials stored where the app's host reads them. The secret never reaches the output."""

from __future__ import annotations

from pathlib import Path

from vecten.secrets import cloud_store, record_cloud_write
from vecten_shared.apps import bound_apps, credential_names
from vecten_shared.config import VectenConfig, load_config
from vecten_shared.extension_record import read_record
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.local import LocalProvider

LAPTOP_NOTE = (
    "The laptop app gets them at the next `vecten up`, but no container on the laptop "
    "reaches the MCP, so the app reads data once deployed."
)


def split_key(key: str) -> tuple[str, str]:
    extension, dot, app = key.partition(".")
    if not dot or not extension or not app:
        raise Failure(ExitCode.USAGE, f"name the app as <extension>.<app>, not {key!r}")
    return extension, app


def app_secret_names(root: Path, config: VectenConfig, extension: str, app: str) -> tuple[str, str]:
    """The names `app`'s credentials are stored under, in the scope of the host it runs on;
    refuses an app vecten.toml does not bind and one that runs on no host."""
    key = f"{extension}.{app}"
    if app not in config.apps.get(extension, {}):
        raise Failure(
            ExitCode.REFUSED,
            f"{key} is not bound; bind it in [apps.{extension}.{app}] of vecten.toml",
        )
    record = read_record(root) or {}
    entry = record.get(extension)
    declared = entry.apps.get(app) if entry else None
    if declared is not None and declared.service is None:
        raise Failure(
            ExitCode.REFUSED,
            f"{key} is a {declared.kind} app; only a container app reads through the MCP",
        )
    bound = next(
        b for b in bound_apps(config, record, set()) if b.extension == extension and b.app == app
    )
    return credential_names(app, bound.host)


def connect_app(root: Path, key: str, env: str, *, adopt: bool = False) -> dict:
    from vecten.mcp_client import AdminClient, endpoint

    extension, app = split_key(key)
    config = load_config(root)
    names = app_secret_names(root, config, extension, app)
    # Every refusal comes before the admin call: a call that succeeds has already replaced
    # the app's previous secret.
    endpoint(root, env)
    store = None if env == "local" else cloud_store(root, env)
    data = AdminClient(root, env).call("apps/connect", {"name": key, "adopt": adopt})
    credentials = data["credentials"]
    values = (credentials["client_id"], credentials["client_secret"])
    try:
        for name, value in zip(names, values, strict=True):
            if store is None:
                LocalProvider(root, inside_compose=False).put_secret(name, value)
            else:
                store.call(lambda name=name, value=value: store.provider.put_secret(name, value))
                record_cloud_write(root, env, name, from_local=False)
    except (Failure, OSError) as exc:
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"storing {names[1]} failed and the app's previous secret no longer works; run "
            f"`vecten app connect {key} --env {env}` again",
        ) from exc
    where = ".env" if store is None else f"the {env} secrets store"
    message = (
        f"Connected {key} as {data['principal']['warehouse_name']} in "
        f"{', '.join(data['groups'])}. Stored {names[0]} and {names[1]} in {where}; "
        + (LAPTOP_NOTE if store is None else "the next ship delivers them to the app.")
    )
    return {
        "app": key,
        "env": env,
        "principal": data["principal"]["warehouse_name"],
        "created": data["created"],
        "groups": data["groups"],
        "secrets": list(names),
        "message": message,
    }
