"""`init --deploy` is a flag of `init`; this module holds its body and registers no command."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import tomlkit
import typer
import yaml

from vecten.cli.context import CliContext
from vecten.deploy.tfvars import has_snowflake_root
from vecten.intake.answers import DECLINED, SHARED_ACCOUNT_KEY, read_answers, read_answers_file
from vecten.scaffold.baseline import BASELINE, checked_path, read_baseline, write_baseline
from vecten.scaffold.merge import merge_snapshots
from vecten.scaffold.params import ALLOWED, DEFAULT_REGION, DEPLOY_FIELDS, ScaffoldParams
from vecten.scaffold.render import DEPLOY_LAYER_PATHS, render_project
from vecten_shared.config import DeploySection, VectenConfig, load_config, save_config
from vecten_shared.output import ExitCode, Failure, Result
from vecten_shared.provider import recorded_layout
from vecten_shared.records import write_text_atomic
from vecten_shared.versions import VERSIONS

DEPLOY_ANSWERS = ("cloud", "domain", "dns_zone", "github_repository")
# The Snowflake account lands in the tfvars beside the deploy answers, so it is taken here too.
DEPLOY_FLAGS = (*DEPLOY_FIELDS, "snowflake_account")
COPIER_ANSWERS = ".copier-answers.yml"
RUNBOOK = "docs/runbook.md"


def register(app: typer.Typer) -> None:
    pass


def _read_copier_answers(project_root: Path) -> dict[str, Any]:
    path = project_root / COPIER_ANSWERS
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise Failure(ExitCode.REFUSED, f"{path} not found; run `vecten init` first") from None
    except yaml.YAMLError as exc:
        raise Failure(ExitCode.CONFIG, f"{path}: {exc}") from None
    if not isinstance(data, dict):
        raise Failure(ExitCode.CONFIG, f"{path}: expected a mapping of answers")
    return data


def _flag_name(key: str) -> str:
    return "--source" if key == "sources" else "--" + key.replace("_", "-")


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _merged_runbook(
    work: Path, project_root: Path, staged: Path, baseline: dict | None
) -> str | None:
    """The runbook with its cloud sections, or None when the human's edits make that a hand merge.

    The runbook is the human's to edit, so the sections arrive by the merge `update` uses, and a
    runbook without a pristine copy to merge from is left as it is.
    """
    new = (staged / RUNBOOK).read_text(encoding="utf-8")
    current = _read(project_root / RUNBOOK)
    old = baseline["files"].get(RUNBOOK) if baseline is not None else None
    if current is None or current == old:
        return new
    if old is None:
        return None
    merge_dir = work / "runbook-merge"
    merge_dir.mkdir()
    merged, conflicts = merge_snapshots(
        merge_dir, {RUNBOOK: old}, {RUNBOOK: new}, {RUNBOOK: current}
    )
    return None if conflicts else merged[RUNBOOK]


def _sync(src: Path, dst: Path) -> bool:
    text = src.read_text(encoding="utf-8")
    if dst.is_file() and dst.read_text(encoding="utf-8") == text:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(dst, text)
    return True


def _shared_account(config: VectenConfig, recorded: dict[str, Any]) -> bool | None:
    """The recorded answer to whether the Snowflake account is shared, while no environment
    has deployed: a deployed environment keeps the layout it recorded, so a later answer
    changes nothing."""
    answer = recorded.get(SHARED_ACCOUNT_KEY)
    if answer not in ("yes", "no") or not has_snowflake_root(config):
        return None
    if any(recorded_layout(config, env) is not None for env in config.environments):
        return None
    return answer == "yes"


def run_init_deploy(
    ctx: CliContext, project_root: Path, flags: dict[str, Any], answers_file: str | None
) -> Result:
    if not (project_root / "vecten.toml").is_file():
        raise Failure(
            ExitCode.REFUSED, f"{project_root} is not a vecten project; run `vecten init` first"
        )
    local = [_flag_name(k) for k, v in flags.items() if k not in DEPLOY_FLAGS and v is not None]
    if local:
        raise Failure(
            ExitCode.USAGE,
            f"{', '.join(local)} belong to `vecten init` without --deploy; under --deploy, "
            "size, profile and components come from vecten.toml",
        )
    config = load_config(project_root)
    base = _read_copier_answers(project_root)
    baseline = read_baseline(project_root)
    if base.get("vecten_version") != VERSIONS.vecten:
        raise Failure(ExitCode.REFUSED, "run vecten update before init --deploy with a newer CLI")
    recorded = (
        read_answers_file(ctx.cwd / answers_file) if answers_file else read_answers(project_root)
    )
    # A declined deploy question is recorded as `none`, which leaves the answer still open.
    recorded = {k: v for k, v in recorded.items() if not (k in DEPLOY_FIELDS and v == DECLINED)}
    cloud = flags.get("cloud") or recorded.get("cloud") or base.get("cloud") or None
    if cloud is not None and cloud not in ALLOWED["cloud"]:
        raise Failure(
            ExitCode.USAGE, f"cloud must be one of: {', '.join(ALLOWED['cloud'])}; got {cloud!r}"
        )
    # Only a rendered layer writes [deploy], so before one exists the cloud is still open.
    if config.deploy is not None and cloud is not None and config.deploy.cloud != cloud:
        raise Failure(
            ExitCode.REFUSED,
            f"changing cloud from {config.deploy.cloud} to {cloud} is not supported "
            "by init --deploy",
        )
    # The rendered answers fill gaps on a rerun, but their region belongs to their own cloud.
    fallback = base if base.get("cloud") in (None, "", cloud) else {**base, "region": None}
    deploy = {
        k: flags.get(k) or recorded.get(k) or fallback.get(k) or None
        for k in (*DEPLOY_ANSWERS, "region")
    }
    missing = [k for k in DEPLOY_ANSWERS if not deploy[k]]
    if missing:
        raise Failure(
            ExitCode.USAGE,
            f"missing deploy answers: {', '.join(missing)}",
            details={"missing": missing},
        )
    deploy["region"] = deploy["region"] or DEFAULT_REGION.get(deploy["cloud"])
    answers = {k: v for k, v in base.items() if k in ScaffoldParams.model_fields}
    answers["name"] = answers.pop("project_name", None)
    answers["has_warehouse"] = "yes" if answers.get("has_warehouse") else "no"
    account = {
        "snowflake_account": flags.get("snowflake_account")
        or recorded.get("snowflake_account")
        or base.get("snowflake_account")
        or None
    }
    # vecten.toml is the editable configuration, so its sizing wins over the rendered answers.
    params = ScaffoldParams.from_answers(
        {**answers, **deploy, **account},
        {"size": config.size, "profile": config.profile, "components": config.components},
    )
    section = DeploySection(**deploy)
    changed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "staged"
        render_project(staged, params)
        deploy_paths = DEPLOY_LAYER_PATHS(params.cloud, params.warehouse, params.components)
        for rel in [*deploy_paths, RUNBOOK, COPIER_ANSWERS, BASELINE, "vecten.toml"]:
            checked_path(project_root, rel)
        runbook = _merged_runbook(Path(tmp).resolve(), project_root, staged, baseline)
        pristine = None
        if baseline is not None:
            pristine = baseline["files"].copy()
            for rel in [*deploy_paths, *([RUNBOOK] if runbook is not None else [])]:
                pristine[rel] = (staged / rel).read_text()
            pristine[COPIER_ANSWERS] = (staged / COPIER_ANSWERS).read_text()
            config_base = tomlkit.parse(pristine["vecten.toml"])
            staged_config = tomlkit.parse((staged / "vecten.toml").read_text())
            for table in ("deploy", "warehouse"):
                config_base[table] = staged_config[table]
            pristine["vecten.toml"] = tomlkit.dumps(config_base)
        for rel in deploy_paths:
            if _sync(staged / rel, project_root / rel):
                changed.append(rel)
        if runbook is not None and runbook != _read(project_root / RUNBOOK):
            write_text_atomic(project_root / RUNBOOK, runbook)
            changed.append(RUNBOOK)
        # Parsed equality, so a difference in YAML layout alone never rewrites the answers file.
        staged_answers = (staged / COPIER_ANSWERS).read_text(encoding="utf-8")
        if yaml.safe_load(staged_answers) != base:
            write_text_atomic(project_root / COPIER_ANSWERS, staged_answers)
            changed.append(COPIER_ANSWERS)
    account_changed = (
        params.snowflake_account is not None
        and config.warehouse.account != params.snowflake_account
    )
    shared = _shared_account(config, recorded)
    shared_changed = shared is not None and config.warehouse.shared_account != shared
    if config.deploy != section or account_changed or shared_changed:
        config.deploy = section
        # The tfvars just rendered name this account, and deploy refuses unless both agree.
        if account_changed:
            config.warehouse.account = params.snowflake_account
        if shared_changed:
            config.warehouse.shared_account = shared
        save_config(project_root, config)
        changed.append("vecten.toml")
    if pristine is not None and pristine != baseline["files"]:
        write_baseline(project_root, baseline["version"], pristine)
        changed.append(BASELINE)
    hand_merge = [] if runbook is not None else [RUNBOOK]
    message = f"deploy layer rendered for {params.cloud}; changed {len(changed)} files"
    if hand_merge:
        message += (
            f"; {RUNBOOK} kept as edited, so its {params.cloud} sections are not in it: "
            "`vecten update` lists the conflict to merge by hand"
        )
    return Result(
        data={
            "project_dir": str(project_root),
            "cloud": params.cloud,
            "changed": sorted(changed),
            "hand_merge": hand_merge,
        },
        message=message,
    )
