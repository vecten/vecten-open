"""`vecten dbt --env <env>`: dbt against a deployed environment's Snowflake warehouse.

dbt runs as the environment's LOADER, the identity the dbt DAG builds with, because the
Snowflake module creates no identity that reads raw, staging and marts without write grants.
On prod the run is held to commands that build nothing: an allow-list of dbt commands, and a
refusal of anything in the project that makes an allowed command write.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from vecten.deploy.snowflake import LOADER_PRIVATE_KEY
from vecten.secrets import cloud_store
from vecten_platform.dbt.deps import write_packages_yml
from vecten_platform.dbt.profile import profile_env
from vecten_platform.dbt.runner import base_env, output_tail, run_dbt
from vecten_shared.config import VectenConfig
from vecten_shared.names import check_env_name
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import ConnectionSpec, snowflake_names

ENV_FLAG = "--env"
PROD_ENV = "prod"
# The Snowflake module names the loader's service user and its role the same.
LOADER = "LOADER"
# dbt replaces the value of every variable with this prefix in all it logs and prints.
SCRUBBED_VAR = "DBT_ENV_SECRET_VECTEN_WAREHOUSE_KEY"

# Read against dbt 1.12, which the project's `require-dbt-version` pins: none of these
# materialises a node. What else makes `test` or `source freshness` write is refused below.
PROD_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("clean",),
    ("compile",),
    ("debug",),
    ("deps",),
    ("docs", "generate"),
    ("docs", "serve"),
    ("list",),
    ("parse",),
    ("show",),
    ("source", "freshness"),
    ("test",),
)
# dbt's run task, which `test` and `source freshness` extend, executes every on-run-start and
# on-run-end hook; compile, show and docs generate never do.
HOOKED_COMMANDS = frozenset({("test",), ("source", "freshness")})
TEST = ("test",)
# A unit test creates a temporary table in the tested model's schema; it needs no prod data.
UNIT_TESTS_OFF = {"DBT_EXCLUDE_RESOURCE_TYPES": "unit_test"}
_STORED_AS = frozenset({"table", "view"})


def split_env(args: list[str]) -> tuple[str | None, list[str]]:
    """The environment a leading `--env <env>` names, and the arguments that are dbt's."""
    env: str | None = None
    rest = list(args)
    if rest and rest[0] == ENV_FLAG:
        if len(rest) < 2:
            raise Failure(ExitCode.USAGE, "--env needs an environment name, e.g. --env prod")
        env, rest = rest[1], rest[2:]
    elif rest and rest[0].startswith(f"{ENV_FLAG}="):
        env, rest = rest[0].split("=", 1)[1], rest[1:]
    if env == "":
        raise Failure(ExitCode.USAGE, "--env needs an environment name, e.g. --env prod")
    # dbt has no --env option, so one after dbt's arguments is a misplaced vecten flag.
    if any(a == ENV_FLAG or a.startswith(f"{ENV_FLAG}=") for a in _before_separator(rest)):
        raise Failure(
            ExitCode.USAGE,
            "--env is vecten's, not dbt's: put it first, as in `vecten dbt --env prod test`",
        )
    if env is not None:
        check_env_name(env)
    return env, rest


def _before_separator(args: list[str]) -> list[str]:
    return args[: args.index("--")] if "--" in args else args


def dbt_command(args: list[str]) -> tuple[str, ...]:
    """The dbt command `args` invoke, resolved by dbt's own parser; empty when there is none."""
    import click
    from dbt.cli.main import cli

    names: list[str] = []
    command: click.Command | None = cli
    rest = list(args)
    while isinstance(command, click.Group):
        ctx = click.Context(command, info_name="dbt", resilient_parsing=True)
        try:
            # The base class parses the group's own options and leaves the subcommand in place.
            remaining = click.Command.parse_args(command, ctx, list(rest))
            if not remaining:
                break
            _, command, rest = command.resolve_command(ctx, remaining)
        except click.ClickException:
            return ()
        if command is None:
            return ()
        names.append(command.name or "")
    if isinstance(command, click.Group):
        names.append("")
    return tuple(names) if all(names) else ()


def _flag_given(args: list[str], flag: str) -> bool:
    return any(a == flag or a.startswith(f"{flag}=") for a in _before_separator(args))


def _allowed_list() -> str:
    return ", ".join(" ".join(c) for c in PROD_COMMANDS)


def check_prod_command(args: list[str]) -> tuple[str, ...]:
    command = dbt_command(args)
    if command not in PROD_COMMANDS:
        named = f"`dbt {' '.join(command)}`" if command else "these arguments name no dbt command"
        raise Failure(
            ExitCode.REFUSED,
            f"on {PROD_ENV}, `vecten dbt --env` runs only commands that build nothing: "
            f"{_allowed_list()}; {named} is not one of them. The dbt_build DAG builds "
            f"{PROD_ENV}; run other commands against another environment.",
        )
    if command == TEST and _flag_given(args, "--store-failures"):
        raise Failure(
            ExitCode.REFUSED,
            f"on {PROD_ENV}, `dbt test` must build nothing, and --store-failures writes a table "
            "for each failing test; leave it out",
        )
    if command == TEST and _flag_given(args, "--exclude-resource-type"):
        raise Failure(
            ExitCode.REFUSED,
            f"on {PROD_ENV}, `dbt test` leaves out unit tests, because each unit test creates "
            "a temporary table, and --exclude-resource-type would bring them back; choose "
            "tests with --select or --exclude, and run unit tests with `vecten dbt-build-local`",
        )
    return command


def writing_nodes(manifest: dict, command: tuple[str, ...]) -> list[str]:
    """What in the parsed project makes `command` write on prod, one line per finding."""
    nodes = list((manifest.get("nodes") or {}).values())
    found: list[str] = []
    if command in HOOKED_COMMANDS:
        hooks = sorted(n["name"] for n in nodes if n.get("resource_type") == "operation")
        if hooks:
            found.append(
                f"on-run-start and on-run-end hooks, which `dbt {' '.join(command)}` executes: "
                + ", ".join(hooks)
            )
    if command == TEST:
        storing = sorted(
            n["name"]
            for n in nodes
            if n.get("resource_type") == "test"
            and (
                (n.get("config") or {}).get("store_failures") is True
                or (n.get("config") or {}).get("store_failures_as") in _STORED_AS
            )
        )
        if storing:
            found.append(
                "tests that store their failures in the warehouse (store_failures or "
                "store_failures_as): " + ", ".join(storing)
            )
    return found


def _vars_args(args: list[str]) -> list[str]:
    """`--vars` as given, since a hook or a test's config may depend on a variable."""
    found: list[str] = []
    rest = _before_separator(args)
    for i, arg in enumerate(rest):
        if arg == "--vars" and i + 1 < len(rest):
            found += [arg, rest[i + 1]]
        elif arg.startswith("--vars="):
            found.append(arg)
    return found


def check_prod_project(
    dbt_dir: Path, command: tuple[str, ...], args: list[str], env: dict[str, str]
) -> None:
    """Parse the project the way the run will, and refuse what would make the run write."""
    if command not in HOOKED_COMMANDS:
        return
    with tempfile.TemporaryDirectory(prefix="vecten-dbt-check-") as target:
        parse_env = {**env, "DBT_TARGET_PATH": target}
        run = run_dbt(["parse", *_vars_args(args)], project_dir=dbt_dir, env=parse_env)
        manifest_path = Path(target) / "manifest.json"
        if run.returncode != 0 or not manifest_path.is_file():
            detail = output_tail(run) or f"see {dbt_dir / 'logs' / 'dbt.log'}"
            raise Failure(
                ExitCode.TOOL_FAILED,
                f"dbt parse, which checks the project before a {PROD_ENV} run, failed: {detail}",
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    found = writing_nodes(manifest, command)
    if found:
        raise Failure(
            ExitCode.REFUSED,
            f"on {PROD_ENV}, `dbt {' '.join(command)}` must build nothing, and this project has "
            + "; and ".join(found)
            + ". Run it against another environment, or remove them from the project.",
        )


def _loader_spec(config: VectenConfig, env: str) -> ConnectionSpec:
    if not config.warehouse.account:
        raise Failure(ExitCode.CONFIG, "vecten.toml: warehouse.account is required on Snowflake")
    names = snowflake_names(config, env)
    return ConnectionSpec(
        kind="snowflake",
        database=names.database,
        role=LOADER,
        auth="key_pair",
        account=config.warehouse.account,
        warehouse=names.warehouse,
    )


def _refuse_postgres(env: str) -> Failure:
    return Failure(
        ExitCode.REFUSED,
        f"environment {env} keeps its Postgres warehouse on the deployment's private network, "
        "which this machine cannot reach, so `vecten dbt --env` runs on Snowflake projects "
        f"only. The dbt_build DAG builds {env}, `vecten issues --env {env}` reports its "
        "failures, and `vecten dbt` without --env runs against the laptop stack.",
    )


def environment_dbt_env(
    root: Path, config: VectenConfig, env: str, args: list[str]
) -> dict[str, str]:
    """The process environment dbt runs with against `env`; nothing of it is written."""
    if config.warehouse.kind != "snowflake":
        raise _refuse_postgres(env)
    store = cloud_store(root, env)
    command = check_prod_command(args) if env == PROD_ENV else ()
    spec = _loader_spec(config, env)
    dbt_dir = root / "dbt"
    write_packages_yml(dbt_dir, config)
    if env == PROD_ENV:
        # The check parses without the credential, so a refused run never reads it.
        check_prod_project(dbt_dir, command, args, base_env(dbt_dir, profile_env(spec, "")))
    try:
        secret = store.call(lambda: store.provider.get_secret(LOADER_PRIVATE_KEY))
    except Failure as failure:
        raise Failure(
            failure.code,
            f"{failure.message}; `vecten deploy --env {env}` generates the loader's key pair",
        ) from None
    process_env = base_env(dbt_dir, {**profile_env(spec, secret), SCRUBBED_VAR: secret})
    if env == PROD_ENV:
        process_env.pop("DBT_STORE_FAILURES", None)
        if command == TEST:
            process_env.update(UNIT_TESTS_OFF)
    return process_env
