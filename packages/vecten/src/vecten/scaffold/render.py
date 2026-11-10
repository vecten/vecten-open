from __future__ import annotations

import shutil
import tempfile
from importlib import resources
from pathlib import Path

from copier import run_copy

from vecten.knowhow.render import vendor_skills
from vecten.scaffold.baseline import write_baseline
from vecten.scaffold.params import ScaffoldParams

OWNER_FILES = frozenset({"osi/metrics.json", "evals/semantic.yaml"})

DEPLOY_WORKFLOWS = ("terraform-plan.yml", "ship.yml", "terraform-apply.yml", "deploy-platform.yml")


def template_root() -> Path:
    return Path(str(resources.files("vecten.scaffold")))


def DEPLOY_LAYER_PATHS(cloud: str, warehouse: str, components: list[str]) -> list[str]:
    paths = ["deploy/envs/prod.tfvars"]
    modules = ("main", "variables", "backend", "outputs")
    paths += [f"deploy/terraform/{cloud}/{n}.tf" for n in modules]
    if warehouse == "snowflake" and "platform" in components:
        paths += [f"deploy/terraform/snowflake/{n}.tf" for n in ("main", "variables", "backend")]
    paths += [f".github/workflows/{w}" for w in DEPLOY_WORKFLOWS]
    return paths


def render_project(target: Path, params: ScaffoldParams) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vecten-render-") as tmp:
        pristine = Path(tmp) / "project"
        run_copy(
            str(template_root()),
            str(pristine),
            data=params.copier_data(),
            defaults=True,
            quiet=True,
            unsafe=True,
            vcs_ref=None,
            overwrite=True,
        )
        vendor_skills(pristine)
        files = {
            p.relative_to(pristine).as_posix(): p.read_text()
            for p in pristine.rglob("*")
            if p.is_file()
        }
        for name in files:
            dest = target / name
            if name in OWNER_FILES and dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pristine / name, dest)
        write_baseline(target, params.vecten_version, files)
    return sorted(p for p in target.rglob("*") if p.is_file())
