import typer

from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import Result
from vecten_shared.records import remove_record, write_record


def login(
    env: str = typer.Option("local", "--env"),
    complete: bool = typer.Option(False, "--complete"),
    browser: bool = typer.Option(False, "--browser"),
    json_output: bool = JsonFlag,
):
    def run(ctx):
        from vecten.mcp_client import (
            TokenCache,
            device_complete,
            device_start,
            endpoint,
            http_client,
            login_record,
        )

        root = ctx.require_project()
        issuer, cache = endpoint(root, env), TokenCache()
        with http_client(root, env) as http:
            if browser:
                from vecten.mcp_login import loopback_login

                data = loopback_login(issuer, http, cache)
            elif complete:
                data = device_complete(issuer, http, cache)
            else:
                data = device_start(issuer, http, cache)
                if not ctx.json:
                    typer.echo(f"Open {data['verification_uri']} and enter {data['user_code']}.")
                    data = device_complete(issuer, http, cache)
                else:
                    data["next"] = f"vecten login --env {env} --complete --json"
        if data.get("authenticated"):
            # `next` compares this id with the cached session's, so a session another
            # project left under the same issuer does not count.
            client_id = cache.load(issuer)["client_id"]
            write_record(root, login_record(env), {"client_id": client_id})
        return Result(
            data=data,
            message="Signed in."
            if data.get("authenticated")
            else "Complete browser authorization, then run login --complete.",
        )

    run_command(json_output, run)


def logout(env: str = typer.Option("local", "--env"), json_output: bool = JsonFlag):
    def run(ctx):
        from vecten.mcp_client import (
            TokenCache,
            endpoint,
            http_client,
            login_record,
            safe_request,
        )

        root = ctx.require_project()
        issuer, cache = endpoint(root, env), TokenCache()
        row = cache.load(issuer)
        if row and row.get("access_token"):
            with http_client(root, env) as http:
                safe_request(
                    http,
                    "POST",
                    issuer + "/revoke",
                    data={"token": row["access_token"], "client_id": row["client_id"]},
                )
        cache.remove(issuer)
        remove_record(root, login_record(env))
        return Result(data={"logged_out": True}, message="Signed out.")

    run_command(json_output, run)


def register(app):
    app.command("login")(login)
    app.command("logout")(logout)
