"""Build evidence shared by the model loader and dbt without runtime-package imports."""

import hashlib
import json
from pathlib import Path

from vecten_shared.osi.documents import document_sources

BUILT_COMMIT = "dbt/built_commit"
BUILD_FINISHED = "dbt/build_finished_at"
BUILT_RELATIONS = "dbt/built_relations.json"
# Every record read here; each cloud's MCP host policy grants exactly these keys.
DBT_RECORDS = (BUILT_COMMIT, BUILD_FINISHED, BUILT_RELATIONS)


def content_revision(directory):
    digest = hashlib.sha256()
    for name, path in document_sources(Path(directory)):
        for value in (name.encode(), path.read_bytes()):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return "local:" + digest.hexdigest()


def build_covers(marker, commit):
    if not isinstance(marker, str):
        return False
    if marker.strip() == commit:
        return True
    try:
        data = json.loads(marker)
    except ValueError:
        return False
    return isinstance(data, dict) and (
        data.get("commit") == commit
        or (isinstance(data.get("ancestors"), list) and commit in data["ancestors"])
    )


def read_built_commit(environ):
    return _read_dbt_record(environ, BUILT_COMMIT)


def read_build_finished(environ):
    """When the dbt DAG's last build finished, or None before the first."""
    return _read_dbt_record(environ, BUILD_FINISHED)


def read_built_relations(environ):
    """The relations the last full green build made, or None before the first, or once a
    later build finished: that build, narrowed or red, may have made relations the record
    lacks. The DAG writes the record before the finish time they share."""
    text = _read_dbt_record(environ, BUILT_RELATIONS)
    if text is None:
        return None
    record = json.loads(text)
    finished = read_build_finished(environ)
    if finished is None or finished.strip() != record.get("finished_at"):
        return None
    return record["relations"]


def _read_dbt_record(environ, name):
    url = environ.get("VECTEN_ARTIFACT_URL", "")
    if not url:
        path = Path(environ.get("VECTEN_ARTIFACT_DIR", "/opt/airflow/artifacts"))
        marker = path / name
        return marker.read_text() if marker.is_file() else None
    if not url.startswith("s3://"):
        raise ValueError("unsupported artifact location")
    import boto3
    from botocore.exceptions import ClientError

    bucket, _, prefix = url[5:].partition("/")
    key = "/".join(part for part in (prefix.strip("/"), name) if part)
    try:
        response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return None
        raise
    with response["Body"] as body:
        return body.read().decode("utf-8")
