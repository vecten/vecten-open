"""Where the dbt DAG keeps what outlives a run: each run's results and the commit last built."""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vecten_shared.records import write_text_atomic

BUILT_COMMIT_KEY = "dbt/built_commit"
ARTIFACT_DIR_VAR = "VECTEN_ARTIFACT_DIR"
ARTIFACT_URL_VAR = "VECTEN_ARTIFACT_URL"
DEFAULT_ARTIFACT_DIR = "/opt/airflow/artifacts"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


class ArtifactSink(Protocol):
    def put_text(self, key: str, text: str) -> None: ...

    def put_file(self, key: str, path: Path) -> None: ...

    def get_text(self, key: str) -> str | None: ...


@dataclass(frozen=True)
class DirectorySink:
    root: Path

    def put_text(self, key: str, text: str) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, text)

    def put_file(self, key: str, path: Path) -> None:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)

    def get_text(self, key: str) -> str | None:
        path = self.root / key
        return path.read_text(encoding="utf-8") if path.is_file() else None


@dataclass(frozen=True)
class S3Sink:
    """Keys under `prefix` in the environment's bucket; the instance role grants the access."""

    bucket: str
    prefix: str

    def _client(self):
        import boto3

        return boto3.client("s3")

    def put_text(self, key: str, text: str) -> None:
        self._client().put_object(
            Bucket=self.bucket, Key=self.prefix + key, Body=text.encode("utf-8")
        )

    def put_file(self, key: str, path: Path) -> None:
        self._client().upload_file(str(path), self.bucket, self.prefix + key)

    def get_text(self, key: str) -> str | None:
        from botocore.exceptions import ClientError

        try:
            response = self._client().get_object(Bucket=self.bucket, Key=self.prefix + key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        return response["Body"].read().decode("utf-8")


def artifact_root(environ: Mapping[str, str]) -> Path:
    return Path(environ.get(ARTIFACT_DIR_VAR) or DEFAULT_ARTIFACT_DIR)


def sink_from_environ(environ: Mapping[str, str]) -> ArtifactSink:
    url = environ.get(ARTIFACT_URL_VAR) or ""
    if url.startswith("s3://"):
        bucket, _, prefix = url[len("s3://") :].partition("/")
        prefix = prefix.strip("/")
        return S3Sink(bucket, f"{prefix}/" if prefix else "")
    return DirectorySink(artifact_root(environ))


def _run_segment(run_id: str) -> str:
    safe = _UNSAFE.sub("_", run_id)
    # The segment is a directory name under dbt/runs, so `.` or `..` would leave that directory.
    if not safe.strip("."):
        safe = "_" * max(len(safe), 1)
    return safe


def run_keys(run_id: str) -> dict[str, str]:
    run = f"dbt/runs/{_run_segment(run_id)}"
    return {
        "build": f"{run}/build/run_results.json",
        "convention": f"{run}/convention/run_results.json",
        "sources": f"{run}/freshness/sources.json",
    }
