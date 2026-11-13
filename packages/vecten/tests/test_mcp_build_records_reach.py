"""The MCP host reads every dbt record the model loader and the reconcile trigger read."""

from pathlib import Path

import pytest

from vecten_shared.osi.revision import DBT_RECORDS

REPO = Path(__file__).resolve().parents[3]
# The platform component's prefix in the artifacts bucket, under which the dbt DAG writes.
PLATFORM_PREFIX = "platform/"

SOURCES = [
    "terraform/aws/iam.tf",
    "terraform/scaleway/bucket.tf",
    "terraform/aws/tests/scope.tftest.hcl",
    "terraform/scaleway/tests/scope.tftest.hcl",
]


def test_the_records_are_the_built_commit_and_the_finish_time():
    assert set(DBT_RECORDS) == {"dbt/built_commit", "dbt/build_finished_at"}


@pytest.mark.parametrize("path", SOURCES)
def test_each_clouds_mcp_policy_and_its_test_name_every_record(path):
    text = (REPO / path).read_text()
    for record in DBT_RECORDS:
        assert f"{PLATFORM_PREFIX}{record}" in text, (path, record)
