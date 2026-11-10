import json
import subprocess

from vecten_shared.output import ExitCode
from vecten_shared.versions import VERSIONS


def checkout(root):
    (root / "images" / "airflow").mkdir(parents=True)
    (root / "images" / "airflow" / "Dockerfile").write_text("FROM scratch\n")


def test_print_uses_the_versions_table(invoke, tmp_path):
    checkout(tmp_path)
    result = invoke("dev", "build-images", "--print", "--json")
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    uv, airflow, mcp = doc["commands"]
    assert uv[:2] == ["uv", "build"]
    assert f"AIRFLOW_IMAGE={VERSIONS.airflow_image}" in airflow
    assert f"AIRFLOW_VERSION={VERSIONS.airflow_version}" in airflow
    assert f"vecten-airflow:{VERSIONS.vecten}" in airflow
    assert f"BASE_IMAGE={VERSIONS.mcp_base_image}" in mcp
    assert doc["images"] == [f"vecten-airflow:{VERSIONS.vecten}", f"vecten-mcp:{VERSIONS.vecten}"]


def test_refuses_outside_a_checkout(invoke):
    result = invoke("dev", "build-images", "--print", "--json")
    assert result.exit_code == ExitCode.USAGE


def test_stale_wheels_are_removed_before_the_build(invoke, tmp_path, monkeypatch):
    checkout(tmp_path)
    stale = tmp_path / "dist" / "vecten_shared-0.0.9-py3-none-any.whl"
    stale.parent.mkdir()
    stale.write_text("old")
    seen = []

    def fake_run(cmd, **kw):
        seen.append(stale.exists())
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = invoke("dev", "build-images", "--json")
    assert result.exit_code == 0
    assert seen == [False, False, False]


def test_missing_tool_is_tool_failed(invoke, tmp_path, monkeypatch):
    checkout(tmp_path)

    def fake_run(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = invoke("dev", "build-images", "--json")
    assert result.exit_code == ExitCode.TOOL_FAILED
    assert json.loads(result.stdout)["error"] == "uv not found on PATH"


def test_build_context_admits_the_wheels_and_the_lock_inputs():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    assert (root / ".dockerignore").read_text().split() == [
        "*",
        "!dist/*.whl",
        "!uv.lock",
        "!pyproject.toml",
        "!packages/*/pyproject.toml",
    ]


AIRFLOW_REF = f"ghcr.io/vecten/vecten-airflow:{VERSIONS.vecten}"
MCP_REF = f"ghcr.io/vecten/vecten-mcp:{VERSIONS.vecten}"


def descriptor(*archs, digest="sha256:index"):
    return {
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "digest": digest,
        "manifests": [{"platform": {"os": "linux", "architecture": a}} for a in archs],
    }


def fake_inspect(monkeypatch, descriptors, calls=None):
    """`subprocess.run` answering imagetools inspects from `descriptors`, by ref."""

    def fake_run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        if cmd[:4] != ["docker", "buildx", "imagetools", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        answer = descriptors[cmd[4]]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, str):
            return subprocess.CompletedProcess(cmd, 1, "", answer)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(answer), "")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_check_images_passes_with_both_platforms(invoke, monkeypatch):
    calls = []
    fake_inspect(
        monkeypatch,
        {
            AIRFLOW_REF: descriptor("amd64", "arm64", digest="sha256:a"),
            MCP_REF: descriptor("amd64", "arm64", digest="sha256:m"),
        },
        calls,
    )
    result = invoke("dev", "check-images", "--json")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["images"] == {AIRFLOW_REF: "sha256:a", MCP_REF: "sha256:m"}
    assert calls == [
        ["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest}}"]
        for ref in (AIRFLOW_REF, MCP_REF)
    ]


def test_check_images_names_the_missing_platform(invoke, monkeypatch):
    fake_inspect(
        monkeypatch, {AIRFLOW_REF: descriptor("amd64"), MCP_REF: descriptor("amd64", "arm64")}
    )
    result = invoke("dev", "check-images", "--json")
    assert result.exit_code == ExitCode.REFUSED
    doc = json.loads(result.stdout)
    assert "vecten-airflow" in doc["error"] and "linux/arm64" in doc["error"]
    assert doc["missing"] == {AIRFLOW_REF: ["linux/arm64"]}


def test_check_images_reports_every_tag_lacking_a_platform(invoke, monkeypatch):
    fake_inspect(monkeypatch, {AIRFLOW_REF: descriptor("amd64"), MCP_REF: descriptor("arm64")})
    result = invoke("dev", "check-images", "--json")
    assert result.exit_code == ExitCode.REFUSED
    assert json.loads(result.stdout)["missing"] == {
        AIRFLOW_REF: ["linux/arm64"],
        MCP_REF: ["linux/amd64"],
    }


def test_check_images_registry_and_version_options(invoke, monkeypatch):
    calls = []
    refs = ["localhost:5000/vecten-airflow:9.9.9", "localhost:5000/vecten-mcp:9.9.9"]
    fake_inspect(monkeypatch, {ref: descriptor("amd64", "arm64") for ref in refs}, calls)
    result = invoke(
        "dev", "check-images", "--registry", "localhost:5000", "--version", "9.9.9", "--json"
    )
    assert result.exit_code == 0, result.stdout
    assert [c[4] for c in calls] == refs


def test_check_images_single_platform_image_is_refused(invoke, monkeypatch):
    single = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:s"}
    fake_inspect(monkeypatch, {AIRFLOW_REF: single, MCP_REF: descriptor("amd64", "arm64")})
    result = invoke("dev", "check-images", "--json")
    assert result.exit_code == ExitCode.REFUSED
    doc = json.loads(result.stdout)
    assert "single-platform" in doc["error"]
    assert doc["missing"] == {AIRFLOW_REF: ["linux/amd64", "linux/arm64"]}


def test_check_images_unknown_tag_is_tool_failed(invoke, monkeypatch):
    fake_inspect(monkeypatch, {AIRFLOW_REF: "ERROR: ghcr.io/vecten/vecten-airflow: not found"})
    result = invoke("dev", "check-images", "--json")
    assert result.exit_code == ExitCode.TOOL_FAILED
    assert "not found" in json.loads(result.stdout)["error"]


def test_check_images_without_docker_is_tool_failed(invoke, monkeypatch):
    fake_inspect(monkeypatch, {AIRFLOW_REF: FileNotFoundError("docker")})
    result = invoke("dev", "check-images", "--json")
    assert result.exit_code == ExitCode.TOOL_FAILED
    assert json.loads(result.stdout)["error"] == "docker not found on PATH"


def test_push_builds_both_platforms(invoke, tmp_path, monkeypatch):
    checkout(tmp_path)
    calls = []
    fake_inspect(monkeypatch, {}, calls)
    result = invoke("dev", "build-images", "--print", "--push", "ghcr.io/vecten", "--json")
    assert result.exit_code == 0, result.stdout
    doc = json.loads(result.stdout)
    local = json.loads(invoke("dev", "build-images", "--print", "--json").stdout)["commands"]
    uv, airflow, mcp = doc["commands"]
    assert uv == local[0]
    for cmd, ref, built in ((airflow, AIRFLOW_REF, local[1]), (mcp, MCP_REF, local[2])):
        assert cmd[:5] == ["docker", "buildx", "build", "--platform", "linux/amd64,linux/arm64"]
        assert cmd[-2:] == ["--push", str(tmp_path)]
        assert cmd[cmd.index("-t") + 1] == ref
        args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--build-arg"]
        assert args == [built[i + 1] for i, a in enumerate(built) if a == "--build-arg"]
        assert f"VECTEN_VERSION={VERSIONS.vecten}" in args
        assert cmd[cmd.index("-f") + 1] == built[built.index("-f") + 1]
    assert doc["images"] == [AIRFLOW_REF, MCP_REF]
    assert calls == []


def test_push_checks_what_it_pushed(invoke, tmp_path, monkeypatch):
    checkout(tmp_path)
    calls = []
    fake_inspect(
        monkeypatch,
        {
            AIRFLOW_REF: descriptor("amd64", "arm64", digest="sha256:a"),
            MCP_REF: descriptor("amd64", "arm64", digest="sha256:m"),
        },
        calls,
    )
    result = invoke("dev", "build-images", "--push", "ghcr.io/vecten", "--json")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["digests"] == {AIRFLOW_REF: "sha256:a", MCP_REF: "sha256:m"}
    kinds = [c[1] if c[0] == "docker" else c[0] for c in calls]
    assert kinds == ["uv", "buildx", "buildx", "buildx", "buildx"]
    assert [c[2] for c in calls[1:]] == ["build", "build", "imagetools", "imagetools"]


def test_push_fails_when_a_platform_is_missing_after_the_push(invoke, tmp_path, monkeypatch):
    checkout(tmp_path)
    fake_inspect(
        monkeypatch, {AIRFLOW_REF: descriptor("amd64", "arm64"), MCP_REF: descriptor("amd64")}
    )
    result = invoke("dev", "build-images", "--push", "ghcr.io/vecten", "--json")
    assert result.exit_code == ExitCode.REFUSED
    assert json.loads(result.stdout)["missing"] == {MCP_REF: ["linux/arm64"]}


def test_images_carry_oci_labels():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    for image in ("airflow", "mcp"):
        text = (root / "images" / image / "Dockerfile").read_text()
        assert "ARG VECTEN_VERSION" in text, image
        for label in (
            "org.opencontainers.image.source=https://github.com/vecten/vecten-open",
            "org.opencontainers.image.licenses=Apache-2.0",
            "org.opencontainers.image.description=",
            "org.opencontainers.image.version=${VECTEN_VERSION}",
        ):
            assert label in text, (image, label)
