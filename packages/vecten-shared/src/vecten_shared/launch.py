"""Start an analytics app's web framework on a file inside an installed package.

`python -m vecten_shared.launch streamlit <package>/<path>.py` runs as the command of the service
a `container` app generates. It uses the standard library only and imports neither the
framework nor the app's package, so a broken app fails in its own process and every check here
runs before any of the app's code.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

RUNTIMES = ("streamlit",)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PORT = re.compile(r"[1-9][0-9]{0,4}")


class LaunchError(Exception):
    pass


def resolve(entry: str) -> Path:
    """The file `entry` names, `<package>/<path>`, which must lie inside that package."""
    package, _, rest = entry.partition("/")
    parts = rest.split("/")
    if (
        not _IDENTIFIER.fullmatch(package)
        or not rest
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise LaunchError(
            f"entry {entry!r} must name a file inside an installed package, as <package>/<path>"
        )
    # A top-level spec is found without running the package's __init__.
    spec = importlib.util.find_spec(package)
    locations = (spec.submodule_search_locations or []) if spec else []
    if not locations:
        raise LaunchError(f"entry {entry!r}: no installed package {package}")
    for location in locations:
        root = Path(location).resolve()
        path = (root / rest).resolve()
        if not path.is_relative_to(root):
            raise LaunchError(f"entry {entry!r} must stay inside the package {package}")
        if path.is_file():
            return path
    raise LaunchError(f"entry {entry!r}: no file {rest} in the package {package}")


def streamlit_argv(path: Path, port: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(path),
        "--server.port",
        port,
        "--server.address",
        "0.0.0.0",
        "--server.headless",
        "true",
        # Streamlit's browser page reports usage to Streamlit unless this is off.
        "--browser.gatherUsageStats",
        "false",
        "--server.enableXsrfProtection",
        "true",
        # The app owns its hostname, so it is served from the root.
        "--server.baseUrlPath",
        "",
    ]


def main(args: list[str] | None = None) -> int:
    args = sys.argv[1:] if args is None else args
    # `python -m` puts the working directory first on sys.path, where a directory named like
    # the package would be found in place of the installed one.
    if sys.path and Path(sys.path[0] or ".").resolve() == Path.cwd().resolve():
        del sys.path[0]
    try:
        if len(args) != 2:
            raise LaunchError("usage: python -m vecten_shared.launch streamlit <package>/<path>")
        runtime, entry = args
        if runtime not in RUNTIMES:
            raise LaunchError(f"runtime {runtime!r} must be one of {', '.join(RUNTIMES)}")
        port = os.environ.get("PORT", "")
        if not _PORT.fullmatch(port) or int(port) > 65535:
            raise LaunchError(f"PORT must be a port number; got {port!r}")
        argv = streamlit_argv(resolve(entry), port)
    except LaunchError as error:
        print(f"vecten_shared.launch: {error}", file=sys.stderr)
        return 2
    os.execv(sys.executable, argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
