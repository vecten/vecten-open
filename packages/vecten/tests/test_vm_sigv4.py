"""The agent's stdlib SigV4 signer, against AWS's published S3 examples and botocore."""

import datetime
import hashlib
import importlib.util
from pathlib import Path
from unittest import mock

import pytest
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

import vecten.deploy.vm

APPLY = Path(vecten.deploy.vm.__file__).parent / "apply.py"

# AWS's published S3 header-signing examples use these credentials and this clock, so the
# expected signatures below hold only with them.
ACCESS = "AKIAIOSFODNN7EXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
NOW = datetime.datetime(2013, 5, 24, tzinfo=datetime.UTC)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def load_agent():
    spec = importlib.util.spec_from_file_location("vecten_vm_apply_sigv4", APPLY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def agent():
    return load_agent()


def test_get_object_example(agent):
    headers = agent.sigv4_headers(
        "GET",
        "https://examplebucket.s3.amazonaws.com/test.txt",
        "us-east-1",
        ACCESS,
        SECRET,
        EMPTY_SHA256,
        NOW,
        headers={"Range": "bytes=0-9"},
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request,"
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date,"
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    assert headers["x-amz-date"] == "20130524T000000Z"
    assert headers["x-amz-content-sha256"] == EMPTY_SHA256


def test_put_object_example(agent):
    body = b"Welcome to Amazon S3."
    payload = hashlib.sha256(body).hexdigest()
    assert payload == "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"
    headers = agent.sigv4_headers(
        "PUT",
        "https://examplebucket.s3.amazonaws.com/test%24file.text",
        "us-east-1",
        ACCESS,
        SECRET,
        payload,
        NOW,
        headers={
            "Date": "Fri, 24 May 2013 00:00:00 GMT",
            "x-amz-storage-class": "REDUCED_REDUNDANCY",
        },
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request,"
        "SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class,"
        "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    )


def botocore_signature(method, url, region, body):
    request = AWSRequest(method=method, url=url, data=body)
    with mock.patch("botocore.auth.get_current_datetime", return_value=NOW.replace(tzinfo=None)):
        S3SigV4Auth(Credentials(ACCESS, SECRET), "s3", region).add_auth(request)
    return request.headers["Authorization"]


@pytest.mark.parametrize(
    ("method", "key", "body"),
    [
        ("GET", "bundles/main/pointer.json", b""),
        ("PUT", "platform/ship-status/" + "a" * 40 + ".json", b'{"ok": true}'),
        ("GET", "mcp/key-ack/with space+and~tilde.json", b""),
    ],
)
def test_path_style_scaleway_requests_sign_as_botocore_does(agent, method, key, body):
    url = agent.s3_object_url("fr-par", "vecten-acme-prod-artifacts-0123abcd", key)
    assert url.startswith("https://s3.fr-par.scw.cloud/vecten-acme-prod-artifacts-0123abcd/")
    ours = agent.sigv4_headers(
        method, url, "fr-par", ACCESS, SECRET, hashlib.sha256(body).hexdigest(), NOW
    )
    ours_signature = ours["Authorization"].rsplit("Signature=", 1)[1]
    theirs_signature = botocore_signature(method, url, "fr-par", body).rsplit("Signature=", 1)[1]
    assert ours_signature == theirs_signature


def test_a_query_string_is_signed_sorted_and_encoded(agent):
    url = "https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J"
    ours = agent.sigv4_headers("GET", url, "us-east-1", ACCESS, SECRET, EMPTY_SHA256, NOW)
    # The published "GET Bucket (List Objects)" example.
    assert ours["Authorization"].endswith(
        "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"
    )
