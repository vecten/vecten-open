import boto3
import pytest
from moto import mock_aws

from vecten_platform.dbt.artifacts import (
    BUILT_COMMIT_KEY,
    DirectorySink,
    S3Sink,
    run_keys,
    sink_from_environ,
)

BUCKET = "vecten-acme-prod-artifacts"


@pytest.fixture
def bucket(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-central-1")
        s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-central-1"}
        )
        yield s3


def test_s3_sink_round_trips(bucket, tmp_path):
    sink = S3Sink(BUCKET, "platform/")
    assert sink.get_text(BUILT_COMMIT_KEY) is None
    sink.put_text(BUILT_COMMIT_KEY, "abc123\n")
    assert sink.get_text(BUILT_COMMIT_KEY) == "abc123\n"
    src = tmp_path / "r.json"
    src.write_text('{"ok": true}')
    key = run_keys("manual__1")["build"]
    sink.put_file(key, src)
    body = bucket.get_object(Bucket=BUCKET, Key=f"platform/{key}")["Body"].read()
    assert body == b'{"ok": true}'


def test_built_commit_lands_at_the_key_the_mcp_role_reads(bucket):
    sink = sink_from_environ({"VECTEN_ARTIFACT_URL": f"s3://{BUCKET}/platform/"})
    sink.put_text(BUILT_COMMIT_KEY, "c1\n")
    body = bucket.get_object(Bucket=BUCKET, Key="platform/dbt/built_commit")["Body"].read()
    assert body == b"c1\n"


def test_sink_from_environ_picks_s3_for_an_s3_url(tmp_path):
    sink = sink_from_environ(
        {"VECTEN_ARTIFACT_URL": "s3://b/platform/", "VECTEN_ARTIFACT_DIR": str(tmp_path)}
    )
    assert isinstance(sink, S3Sink)
    assert (sink.bucket, sink.prefix) == ("b", "platform/")
    assert sink_from_environ({"VECTEN_ARTIFACT_URL": "s3://b/platform"}).prefix == "platform/"
    assert sink_from_environ({"VECTEN_ARTIFACT_URL": "s3://b"}).prefix == ""
    assert isinstance(sink_from_environ({"VECTEN_ARTIFACT_URL": ""}), DirectorySink)


def test_s3_sink_read_failure_other_than_missing_raises(bucket):
    sink = S3Sink("no-such-bucket-here", "platform/")
    with pytest.raises(Exception) as exc:
        sink.get_text(BUILT_COMMIT_KEY)
    assert "NoSuchBucket" in repr(exc.value) or "NoSuchBucket" in str(exc.value)


def test_directory_sink_round_trips(tmp_path):
    sink = DirectorySink(tmp_path)
    assert sink.get_text(BUILT_COMMIT_KEY) is None
    sink.put_text(BUILT_COMMIT_KEY, "abc123\n")
    assert sink.get_text(BUILT_COMMIT_KEY) == "abc123\n"
    src = tmp_path / "r.json"
    src.write_text("{}")
    sink.put_file(run_keys("manual__1")["build"], src)
    assert (tmp_path / "dbt" / "runs" / "manual__1" / "build" / "run_results.json").read_text() == (
        "{}"
    )


def test_sink_from_environ(tmp_path):
    assert sink_from_environ({"VECTEN_ARTIFACT_DIR": str(tmp_path)}).root == tmp_path
    assert str(sink_from_environ({}).root) == "/opt/airflow/artifacts"


def test_run_keys_sanitise_run_id():
    keys = run_keys("scheduled__2026-10-17T00:00:00+00:00")
    assert "/" not in keys["build"].split("dbt/runs/")[1].split("/build")[0]
    assert keys["sources"].endswith("/sources.json")


def test_run_keys_name_one_directory_per_run_and_step():
    keys = run_keys("scheduled__2026-10-17T00:00:00+00:00")
    run = "dbt/runs/scheduled__2026-10-17T00_00_00_00_00"
    assert keys == {
        "build": f"{run}/build/run_results.json",
        "convention": f"{run}/convention/run_results.json",
        "sources": f"{run}/freshness/sources.json",
    }


def test_run_keys_never_climb_out_of_the_runs_directory():
    for run_id in ("..", ".", "../../etc"):
        segment = run_keys(run_id)["build"].split("/")[2]
        assert segment not in ("", ".", ".."), run_id
        assert "/" not in segment
