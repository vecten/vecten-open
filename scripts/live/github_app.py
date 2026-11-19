#!/usr/bin/env python3
"""Mint short-lived GitHub App installation tokens. Standard library and openssl only."""

from __future__ import annotations

import argparse
import base64
import calendar
import json
import os
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

API = "https://api.github.com"
# A token this close to expiry could lapse before the caller's request lands.
REFRESH_MARGIN = 300


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, key_path: Path, now: int) -> str:
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iat": now - 60, "exp": now + 540, "iss": app_id}
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    signed = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
        input=signing_input,
        capture_output=True,
    )
    if signed.returncode != 0:
        raise SystemExit(f"openssl could not sign with the key {key_path.name}")
    return f"{header}.{payload}.{_b64(signed.stdout)}"


def _post(url: str, jwt: str) -> dict:
    request = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def parse_time(stamp: str) -> int:
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


def _cached(path: Path, now: int) -> str | None:
    try:
        entry = json.loads(path.read_text())
        if parse_time(entry["expires_at"]) - now > REFRESH_MARGIN:
            return entry["token"]
    except OSError, ValueError, KeyError, TypeError:
        return None
    return None


def _store(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(json.dumps(entry))
    path.chmod(0o600)


def installation_token(
    config: Path,
    cache_dir: Path,
    now: int,
    fetch: Callable[[str, str], dict] = _post,
) -> str:
    cache = cache_dir / f"{config.stem}.json"
    token = _cached(cache, now)
    if token is not None:
        return token
    settings = tomllib.loads(config.read_text())
    installation = str(settings["installation_id"])
    jwt = app_jwt(str(settings["app_id"]), config.parent / settings["key"], now)
    url = f"{API}/app/installations/{installation}/access_tokens"
    try:
        granted = fetch(url, jwt)
    except urllib.error.HTTPError as refused:
        refused.close()
        raise SystemExit(
            f"GitHub refused a token for installation {installation}: HTTP {refused.code}"
        ) from None
    except urllib.error.URLError as unreachable:
        raise SystemExit(
            f"GitHub was unreachable for installation {installation}: {unreachable.reason}"
        ) from None
    entry = {"token": granted["token"], "expires_at": granted["expires_at"]}
    _store(cache, entry)
    return entry["token"]


def credential_home() -> Path:
    override = os.environ.get("VECTEN_LIVE_HOME")
    return Path(override) if override else Path.home() / ".config" / "vecten-livecheck"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    token = commands.add_parser("token", help="print an installation token")
    token.add_argument("config", type=Path)
    args = parser.parse_args()
    print(installation_token(args.config, credential_home() / "cache", int(time.time()), _post))


if __name__ == "__main__":
    main()
