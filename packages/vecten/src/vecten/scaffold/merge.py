"""Copier merges static snapshots in temporary repositories, never executing project hooks."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml
from copier import run_update

from vecten_shared.output import ExitCode, Failure

MERGE_ANSWERS = ".vecten-merge-answers.yml"


def git(root: Path, *args: str) -> None:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Vecten update",
            "-c",
            "user.email=update@vecten.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise Failure(ExitCode.TOOL_FAILED, f"update: git {args[0]} failed: {result.stderr}")


def put(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def merge_snapshots(
    work: Path, old: dict[str, str], new: dict[str, str], current: dict[str, str]
) -> tuple[dict[str, str], list[str]]:
    if shutil.which("git") is None:
        raise Failure(ExitCode.PREREQUISITE, "git is required for vecten update")
    source, staged = work / "source", work / "merged"
    source.mkdir()
    (source / "copier.yml").write_text(
        "_subdirectory: files\n_templates_suffix: .vecten-never-render\n"
    )
    put(source / "files", {**old, ".vecten-merge-keep": ""})
    git(source, "init")
    git(source, "add", "--all")
    git(source, "commit", "-m", "pristine old release")
    git(source, "tag", "v1")
    staged.mkdir()
    put(staged, current)
    (staged / MERGE_ANSWERS).write_text(yaml.safe_dump({"_src_path": str(source), "_commit": "v1"}))
    git(staged, "init")
    git(staged, "config", "core.hooksPath", "/dev/null")
    git(staged, "config", "commit.gpgsign", "false")
    git(staged, "add", "--all", "--force")
    git(staged, "commit", "-m", "project edits")
    shutil.rmtree(source / "files")
    put(source / "files", {**new, ".vecten-merge-keep": ""})
    git(source, "add", "--all")
    git(source, "commit", "--allow-empty", "-m", "new release")
    git(source, "tag", "v2")
    run_update(
        staged,
        vcs_ref="v2",
        answers_file=MERGE_ANSWERS,
        defaults=True,
        quiet=True,
        overwrite=True,
        skip_tasks=True,
    )
    merged = {
        name: (staged / name).read_text()
        for name in old.keys() | new.keys()
        if (staged / name).is_file()
    }
    conflicts = sorted(
        name
        for name, text in merged.items()
        if "<<<<<<< before updating" in text or (staged / (name + ".rej")).exists()
    )
    # Deleted files that changed upstream require a decision, even if Git retains one side.
    conflicts += [
        name for name in old if name not in current and name in new and old[name] != new[name]
    ]
    conflicts += [
        name for name in old if name not in new and name in current and old[name] != current[name]
    ]
    conflicts += [
        name for name in new if name not in old and name in current and new[name] != current[name]
    ]
    return merged, sorted(set(conflicts))
