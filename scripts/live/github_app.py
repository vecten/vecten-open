#!/usr/bin/env python3
"""Mint short-lived GitHub App installation tokens. Standard library and openssl only."""

from __future__ import annotations

import argparse
import base64
import calendar
import json
import os
import stat
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

API = "https://api.github.com"
# A deploy or ship follows its workflow with one token for many minutes, so every token handed
# out has 55 of its 60 minutes left.
REFRESH_MARGIN = 3300
# The signer sees the App's private key outside any sandbox, so it is the system's own binary,
# which no command of the user can replace, wherever the system has one.
OPENSSL = "/usr/bin/openssl" if Path("/usr/bin/openssl").is_file() else "openssl"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, key_path: Path, now: int) -> str:
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iat": now - 60, "exp": now + 540, "iss": app_id}
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    signed = subprocess.run(
        [OPENSSL, "dgst", "-sha256", "-sign", str(key_path)],
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


# This command runs outside the OS sandbox, so it neither reads nor writes through a link: one
# planted in the cache would point its reads and writes at any file of the user's.
def _cache_directory(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    if not stat.S_ISDIR(os.lstat(directory).st_mode):
        raise SystemExit(f"the token cache {directory} is not a directory")


def _cached(path: Path, now: int) -> str | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        with os.fdopen(descriptor) as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            entry = json.loads(handle.read())
        if parse_time(entry["expires_at"]) - now > REFRESH_MARGIN:
            return entry["token"]
    except OSError, ValueError, KeyError, TypeError:
        return None
    return None


def _store(path: Path, entry: dict) -> None:
    # A fresh file renamed over the entry replaces a link there instead of writing through it.
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(entry))
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def installation_token(
    config: Path,
    cache_dir: Path,
    now: int,
    fetch: Callable[[str, str], dict] = _post,
) -> str:
    _cache_directory(cache_dir)
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
