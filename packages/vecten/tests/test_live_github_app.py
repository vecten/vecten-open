import base64
import importlib.util
import io
import json
import os
import stat
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]
URL = "https://api.github.com/app/installations/987654/access_tokens"
EXPIRES_AT = "2027-01-15T09:00:00Z"
EXPIRES = 1_800_003_600


def mod():
    spec = importlib.util.spec_from_file_location("github_app", REPO / "scripts/live/github_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def b64d(segment):
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def make_rsa_key(directory):
    private = directory / "app.pem"
    public = directory / "app.pub"
    subprocess.run(
        ["openssl", "genrsa", "-out", str(private), "2048"], check=True, capture_output=True
    )
    subprocess.run(
        ["openssl", "rsa", "-in", str(private), "-pubout", "-out", str(public)],
        check=True,
        capture_output=True,
    )
    return SimpleNamespace(private=private, public=public)


def openssl_verifies(public, message, signature):
    sig = public.parent / "sig.bin"
    msg = public.parent / "msg.bin"
    sig.write_bytes(signature)
    msg.write_bytes(message)
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(sig), str(msg)],
        capture_output=True,
    )
    return result.returncode == 0


def config(tmp_path, name="sandbox"):
    key = make_rsa_key(tmp_path)
    directory = tmp_path / "github"
    directory.mkdir()
    (directory / "app.pem").write_bytes(key.private.read_bytes())
    path = directory / f"{name}.toml"
    path.write_text('app_id = "12345"\ninstallation_id = "987654"\nkey = "app.pem"\n')
    return path


def test_jwt_verifies_against_the_public_key(tmp_path):
    key = make_rsa_key(tmp_path)
    token = mod().app_jwt("12345", key.private, now=1_800_000_000)
    header, payload, signature = token.split(".")
    assert json.loads(b64d(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(b64d(payload)) == {
        "iat": 1_799_999_940,
        "exp": 1_800_000_540,
        "iss": "12345",
    }
    assert openssl_verifies(key.public, f"{header}.{payload}".encode(), b64d(signature))


def test_every_token_handed_out_has_fifty_five_minutes_left(tmp_path):
    # A deploy or ship follows its workflow with the token it started with, for many minutes.
    cfg = config(tmp_path)
    cache = tmp_path / "cache"
    calls = []

    def fetch(url, jwt):
        calls.append(url)
        return {"token": f"ghs_{len(calls)}", "expires_at": EXPIRES_AT}

    m = mod()
    expires = m.parse_time(EXPIRES_AT)
    assert m.installation_token(cfg, cache, expires - 3600, fetch) == "ghs_1"
    assert m.installation_token(cfg, cache, expires - 3301, fetch) == "ghs_1"
    assert calls == [URL]
    assert m.installation_token(cfg, cache, expires - 3299, fetch) == "ghs_2"
    assert len(calls) == 2


def test_the_cache_file_is_owner_only(tmp_path):
    cfg = config(tmp_path)
    cache = tmp_path / "cache"
    mod().installation_token(
        cfg, cache, 1_800_000_000, lambda url, jwt: {"token": "t", "expires_at": EXPIRES_AT}
    )
    assert stat.S_IMODE((cache / "sandbox.json").stat().st_mode) == 0o600


def test_a_refused_request_names_the_installation_and_no_key_material(tmp_path):
    cfg = config(tmp_path)
    seen = []

    def fetch(url, jwt):
        seen.append(jwt)
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO())

    with pytest.raises(SystemExit) as refused:
        mod().installation_token(cfg, tmp_path / "cache", 1_800_000_000, fetch)
    message = str(refused.value)
    assert "987654" in message
    assert seen[0] not in message
    for line in (cfg.parent / "app.pem").read_text().splitlines():
        assert line not in message


def test_the_cli_prints_only_the_token(tmp_path, monkeypatch, capsys):
    cfg = config(tmp_path)
    m = mod()
    monkeypatch.setenv("VECTEN_LIVE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        m, "_post", lambda url, jwt: {"token": "ghs_cli", "expires_at": "2099-01-01T00:00:00Z"}
    )
    monkeypatch.setattr("sys.argv", ["github_app.py", "token", str(cfg)])
    m.main()
    assert capsys.readouterr().out == "ghs_cli\n"
    assert (tmp_path / "home/cache/sandbox.json").exists()


def test_the_signer_is_the_systems_own_openssl_where_there_is_one():
    expected = "/usr/bin/openssl" if Path("/usr/bin/openssl").is_file() else "openssl"
    assert mod().OPENSSL == expected


def test_the_jwt_is_signed_by_the_openssl_the_constant_names(tmp_path, monkeypatch):
    record = tmp_path / "signer-argv"
    signer = tmp_path / "openssl"
    signer.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\nprintf signature\n')
    signer.chmod(0o755)
    module = mod()
    monkeypatch.setattr(module, "OPENSSL", str(signer))
    token = module.app_jwt("12345", tmp_path / "app.pem", now=1_800_000_000)
    assert record.read_text().splitlines() == [
        "dgst",
        "-sha256",
        "-sign",
        str(tmp_path / "app.pem"),
    ]
    assert b64d(token.split(".")[2]) == b"signature"


def test_a_link_planted_in_the_cache_is_neither_read_nor_written_through(tmp_path):
    cfg = config(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    target = tmp_path / "script.py"
    planted = json.dumps({"token": "ghs_planted", "expires_at": EXPIRES_AT})
    target.write_text(planted)
    (cache / "sandbox.json").symlink_to(target)
    fresh = {"token": "ghs_fresh", "expires_at": EXPIRES_AT}
    token = mod().installation_token(cfg, cache, 1_800_000_000, lambda url, jwt: fresh)
    assert token == "ghs_fresh"
    assert target.read_text() == planted
    entry = cache / "sandbox.json"
    assert stat.S_ISREG(os.lstat(entry).st_mode)
    assert stat.S_IMODE(os.lstat(entry).st_mode) == 0o600
    assert json.loads(entry.read_text()) == fresh
    assert sorted(path.name for path in cache.iterdir()) == ["sandbox.json"]


def test_a_cache_directory_that_is_a_link_is_refused(tmp_path):
    cfg = config(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "cache").symlink_to(elsewhere)
    fetch = {"token": "ghs_fresh", "expires_at": EXPIRES_AT}
    with pytest.raises(SystemExit, match="not a directory"):
        mod().installation_token(cfg, tmp_path / "cache", 1_800_000_000, lambda url, jwt: fetch)
    assert list(elsewhere.iterdir()) == []


def test_a_new_cache_directory_is_owner_only(tmp_path):
    cfg = config(tmp_path)
    cache = tmp_path / "home/cache"
    fresh = {"token": "ghs_fresh", "expires_at": EXPIRES_AT}
    mod().installation_token(cfg, cache, 1_800_000_000, lambda url, jwt: fresh)
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
