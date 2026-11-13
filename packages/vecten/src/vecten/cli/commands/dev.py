from __future__ import annotations

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten.knowhow.render import GUIDE_DOCUMENT, render_guide_document, render_plugin
from vecten.stack.images import REQUIRED_PLATFORMS, base_image, missing_platforms
from vecten.stack.up import PUBLISHED_REGISTRY
from vecten_shared.output import ExitCode, Failure, Result
from vecten_shared.versions import VERSIONS

dev = typer.Typer(help="Developer commands for the vecten repository itself.", hidden=True)


@dev.command("build-images", help="Build the two base images from a vecten-open checkout.")
def build_images(
    repo: str = typer.Option(".", "--repo"),
    print_only: bool = typer.Option(False, "--print"),
    push: str | None = typer.Option(
        None,
        "--push",
        metavar="REGISTRY",
        help="Build linux/amd64 and linux/arm64 with buildx, push to REGISTRY, then check.",
    ),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        root = (ctx.cwd / repo).resolve()
        if not (root / "images" / "airflow" / "Dockerfile").is_file():
            raise Failure(
                ExitCode.USAGE,
                f"{root} is not a vecten-open checkout (images/airflow/Dockerfile missing)",
            )
        images = [base_image("airflow", push), base_image("mcp", push)]
        # One buildx run per image puts both platforms in one index, so a tag never exists
        # with one platform only.
        build = (
            ["docker", "buildx", "build", "--platform", ",".join(REQUIRED_PLATFORMS)]
            if push
            else ["docker", "build"]
        )
        context = ["--push", str(root)] if push else [str(root)]
        commands = [
            ["uv", "build", "--all-packages", "--wheel", "--out-dir", str(root / "dist")],
            [
                *build,
                "-f",
                str(root / "images" / "airflow" / "Dockerfile"),
                "--build-arg",
                f"AIRFLOW_IMAGE={VERSIONS.airflow_image}",
                "--build-arg",
                f"AIRFLOW_VERSION={VERSIONS.airflow_version}",
                "--build-arg",
                f"VECTEN_VERSION={VERSIONS.vecten}",
                "-t",
                images[0],
                *context,
            ],
            [
                *build,
                "-f",
                str(root / "images" / "mcp" / "Dockerfile"),
                "--build-arg",
                f"BASE_IMAGE={VERSIONS.mcp_base_image}",
                "--build-arg",
                f"VECTEN_VERSION={VERSIONS.vecten}",
                "-t",
                images[1],
                *context,
            ],
        ]
        if not print_only:
            # The Dockerfiles glob dist/ by package name; a wheel of another version would collide.
            shutil.rmtree(root / "dist", ignore_errors=True)
            wheels, *builds = commands
            run_tool(wheels, root)
            if push:
                # A release stops at the first failed build, so that failure pushes no later image.
                for cmd in builds:
                    run_tool(cmd, root)
            else:
                # The builds only read the checkout and cache apart, so they run side by side.
                with ThreadPoolExecutor(len(builds)) as pool:
                    for done in [pool.submit(run_tool, cmd, root) for cmd in builds]:
                        done.result()
        data = {"commands": commands, "images": images}
        message = "\n".join(" ".join(c) for c in commands)
        if push and not print_only:
            data["digests"] = check_published(images)
            message += "".join(f"\n{ref} {digest}" for ref, digest in data["digests"].items())
        return Result(data=data, message=message)

    run_command(json_output, run)


def run_tool(cmd: list[str], root: Path) -> None:
    try:
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    except FileNotFoundError:
        raise Failure(ExitCode.TOOL_FAILED, f"{cmd[0]} not found on PATH") from None
    if r.returncode != 0:
        raise Failure(ExitCode.TOOL_FAILED, f"{cmd[0]} failed: {r.stderr[-2000:]}")


def check_published(refs: list[str]) -> dict[str, str]:
    """Each ref's index digest; refuses when any ref lacks a required platform."""
    digests, missing, single = {}, {}, []
    for ref in refs:
        cmd = ["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest}}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            raise Failure(ExitCode.TOOL_FAILED, "docker not found on PATH") from None
        if r.returncode != 0:
            raise Failure(ExitCode.TOOL_FAILED, f"inspecting {ref} failed: {r.stderr[-2000:]}")
        descriptor = json.loads(r.stdout)
        try:
            lacking = missing_platforms(descriptor)
        except ValueError:
            single.append(ref)
            lacking = list(REQUIRED_PLATFORMS)
        if lacking:
            missing[ref] = lacking
        digests[ref] = descriptor["digest"]
    if missing:
        lines = [
            f"{ref} is a single-platform image"
            if ref in single
            else f"{ref} lacks {', '.join(lacking)}"
            for ref, lacking in missing.items()
        ]
        raise Failure(
            ExitCode.REFUSED,
            f"{'; '.join(lines)}; every tag must hold {' and '.join(REQUIRED_PLATFORMS)}",
            details={"missing": missing},
        )
    return digests


@dev.command(
    "check-images",
    help="Fail unless both published base images hold linux/amd64 and linux/arm64.",
)
def check_images(
    registry: str = typer.Option(PUBLISHED_REGISTRY, "--registry"),
    version: str = typer.Option(VERSIONS.vecten, "--version"),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        refs = [base_image(kind, registry, version) for kind in ("airflow", "mcp")]
        digests = check_published(refs)
        return Result(
            data={"images": digests},
            message="\n".join(f"{ref} {digest}" for ref, digest in digests.items()),
        )

    run_command(json_output, run)


@dev.command(
    "render-plugin",
    help="Write plugins/vecten, the marketplace file and docs/agent-guide.md from the skills.",
)
def render_plugin_command(
    repo: str = typer.Option(".", "--repo"),
    check: bool = typer.Option(False, "--check", help="Write nothing; exit 6 if out of sync."),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        root = (ctx.cwd / repo).resolve()
        readme = root / "README.md"
        if not readme.is_file():
            raise Failure(
                ExitCode.USAGE, f"{root} is not a vecten-open checkout (README.md missing)"
            )
        changed = render_plugin(root, check=check)
        if render_guide_document(root, check=check):
            changed.append(root / GUIDE_DOCUMENT)
        names = [p.relative_to(root).as_posix() for p in changed]
        if check and names:
            raise Failure(
                ExitCode.CONFIG,
                f"out of sync with the package skills: {', '.join(names)}; "
                "run `vecten dev render-plugin`",
                details={"changed": names},
            )
        message = "\n".join(f"updated {n}" for n in names) or "plugin and guide document in sync"
        return Result(data={"changed": names}, message=message)

    run_command(json_output, run)


@dev.command(
    "bump-version",
    help="Move the four distributions to one new version across the twelve files that name it.",
)
def bump_version(
    version: str = typer.Argument(..., help="The new PEP 440 version, higher than the current."),
    repo: str = typer.Option(".", "--repo"),
    check: bool = typer.Option(False, "--check", help="List the files to change; write none."),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        from vecten.version_bump import apply, plan

        root = (ctx.cwd / repo).resolve()
        old, edits = plan(root, version)
        if not check:
            apply(root, edits)
        files = [edit.rel for edit in edits]
        verb = "would change" if check else "changed"
        message = f"{old} -> {version}: {verb} {len(files)} files\n" + "\n".join(files)
        if not check:
            message += "\nthen run `uv sync --locked --all-packages` to confirm the lock"
        return Result(
            data={"from": old, "to": version, "files": files, "written": not check},
            message=message,
        )

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.add_typer(dev, name="dev", hidden=True)
