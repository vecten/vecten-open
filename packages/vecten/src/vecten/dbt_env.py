"""`vecten dbt --env <env>`: dbt against a deployed environment's Snowflake warehouse.

dbt runs as the environment's LOADER, the identity the dbt DAG builds with, because the
Snowflake module creates no identity that reads raw, staging and marts without write grants.
On prod the run is held to commands that build nothing: an allow-list of dbt commands, and a
refusal of anything in the project that makes an allowed command write.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from vecten.deploy.snowflake import LOADER_PRIVATE_KEY
from vecten.secrets import cloud_store
from vecten_platform.dbt.deps import CONVENTION_PACKAGE, write_packages_yml
from vecten_platform.dbt.profile import profile_env
from vecten_platform.dbt.runner import AREAS_VAR, base_env, output_tail, run_dbt
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
# The prod check parses the project, profile and target that vecten sets; the run must use them.
CHOSEN_BY_VECTEN = {
    "profile": "--profile",
    "profiles_dir": "--profiles-dir",
    "project_dir": "--project-dir",
    "target": "--target (-t)",
}


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


def read_args(args: list[str]) -> tuple[tuple[str, ...], frozenset[str]]:
    """The dbt command `args` invoke and the options they give, read with dbt's own parser.

    Options are named as dbt's parameters are, so every spelling of one counts. The command is
    empty when the arguments name none or dbt cannot read them.
    """
    import click
    from dbt.cli.main import cli

    names: list[str] = []
    given: set[str] = set()
    command: click.Command = cli
    rest = list(args)
    while True:
        ctx = click.Context(command, info_name="dbt")
        try:
            # The parser records what the arguments say; the environment is not consulted.
            opts, rest, _ = command.make_parser(ctx).parse_args(args=list(rest))
            given.update(opts)
            if not isinstance(command, click.Group) or not rest:
                break
            _, found, rest = command.resolve_command(ctx, rest)
        except click.ClickException:
            return (), frozenset()
        if found is None:
            return (), frozenset()
        command = found
        names.append(command.name or "")
    if isinstance(command, click.Group):
        names.append("")
    return (tuple(names) if all(names) else ()), frozenset(given)


def _takes_vars(command: tuple[str, ...]) -> bool:
    from dbt.cli.main import cli

    if not command:
        return False
    found = cli
    for name in command:
        found = found.commands.get(name) if hasattr(found, "commands") else None
        if found is None:
            return False
    return any(param.name == "vars" for param in found.params)


def with_area_vars(args: list[str], areas) -> list[str]:
    """`args` passing the declared areas as the convention's dbt var, when the command takes vars.

    A `--vars` the person gave keeps its own variables and gains the areas, unless it names
    them itself or is not a mapping, in which case it is passed on as given.
    """
    names = sorted(areas)
    if not names or not _takes_vars(dbt_command(args)):
        return list(args)
    import yaml

    head = _before_separator(args)
    tail = args[len(head) :]
    for i, arg in enumerate(head):
        given = head[i + 1] if arg == "--vars" and i + 1 < len(head) else None
        if arg.startswith("--vars="):
            given = arg.split("=", 1)[1]
        if given is None:
            continue
        try:
            values = yaml.safe_load(given)
        except yaml.YAMLError:
            return list(args)
        if not isinstance(values, dict) or AREAS_VAR in values:
            return list(args)
        merged = json.dumps({**values, AREAS_VAR: names})
        if arg == "--vars":
            return [*head[: i + 1], merged, *head[i + 2 :], *tail]
        return [*head[:i], f"--vars={merged}", *head[i + 1 :], *tail]
    return [*head, "--vars", json.dumps({AREAS_VAR: names}), *tail]


def dbt_command(args: list[str]) -> tuple[str, ...]:
    """The dbt command `args` invoke, resolved by dbt's own parser; empty when there is none."""
    return read_args(args)[0]


def _allowed_list() -> str:
    return ", ".join(" ".join(c) for c in PROD_COMMANDS)


def check_prod_command(args: list[str]) -> tuple[str, ...]:
    command, given = read_args(args)
    if command not in PROD_COMMANDS:
        named = f"`dbt {' '.join(command)}`" if command else "these arguments name no dbt command"
        raise Failure(
            ExitCode.REFUSED,
            f"on {PROD_ENV}, `vecten dbt --env` runs only commands that build nothing: "
            f"{_allowed_list()}; {named} is not one of them. The dbt_build DAG builds "
            f"{PROD_ENV}; run other commands against another environment.",
        )
    chosen = sorted(given & CHOSEN_BY_VECTEN.keys())
    if chosen:
        raise Failure(
            ExitCode.REFUSED,
            f"on {PROD_ENV}, vecten chooses the dbt project, profile and target, and checks "
            "that one before the run; leave out "
            + ", ".join(CHOSEN_BY_VECTEN[name] for name in chosen),
        )
    if command == TEST and "store_failures" in given:
        raise Failure(
            ExitCode.REFUSED,
            f"on {PROD_ENV}, `dbt test` must build nothing, and --store-failures writes a table "
            "for each failing test; leave it out",
        )
    if command == TEST and "exclude_resource_types" in given:
        raise Failure(
            ExitCode.REFUSED,
            f"on {PROD_ENV}, `dbt test` leaves out unit tests, because each unit test creates "
            "a temporary table, and --exclude-resource-types would bring them back; choose "
            "tests with --select or --exclude, and run unit tests with `vecten dbt-build-local`",
        )
    return command


def writing_nodes(manifest: dict, command: tuple[str, ...]) -> list[str]:
    """What in the parsed project makes `command` write on prod, one line per finding."""
    nodes = list((manifest.get("nodes") or {}).values())
    found: list[str] = []
    if command in HOOKED_COMMANDS:
        # The convention package's hook reads the parsed graph and sends the warehouse nothing.
        hooks = sorted(
            n["name"]
            for n in nodes
            if n.get("resource_type") == "operation" and n.get("package_name") != CONVENTION_PACKAGE
        )
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


def without_dbt_settings(environ: Mapping[str, str]) -> dict[str, str]:
    """`environ` without any variable that sets a dbt option.

    dbt reads each option from `DBT_ENGINE_<NAME>` before `DBT_<NAME>` and copies the first over
    the second, so a shell's variable of either spelling would override what vecten sets.
    """
    from dbt.env_vars import KNOWN_ENGINE_ENV_VARS

    known = {v.name for v in KNOWN_ENGINE_ENV_VARS} | {
        v.old_name for v in KNOWN_ENGINE_ENV_VARS if v.old_name
    }
    return {k: v for k, v in environ.items() if k not in known and not k.startswith("DBT_ENGINE_")}


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
    inherited = without_dbt_settings(os.environ) if env == PROD_ENV else None
    if env == PROD_ENV:
        # The check parses without the credential, so a refused run never reads it.
        check_env = base_env(dbt_dir, profile_env(spec, ""), inherited)
        check_prod_project(dbt_dir, command, args, check_env)
    try:
        secret = store.call(lambda: store.provider.get_secret(LOADER_PRIVATE_KEY))
    except Failure as failure:
        raise Failure(
            failure.code,
            f"{failure.message}; `vecten deploy --env {env}` generates the loader's key pair",
        ) from None
    process_env = base_env(dbt_dir, {**profile_env(spec, secret), SCRUBBED_VAR: secret}, inherited)
    if command == TEST:
        process_env.update(UNIT_TESTS_OFF)
    return process_env
