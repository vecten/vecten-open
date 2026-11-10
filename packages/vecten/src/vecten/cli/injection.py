"""Runner injection: an environment variable naming `module:attr` replaces the default."""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from typing import Any


def load_runner(env_var: str, default_factory: Callable[[], Any]) -> Any:
    dotted = os.environ.get(env_var)
    if dotted:
        module, name = dotted.rsplit(":", 1)
        return getattr(importlib.import_module(module), name)()
    return default_factory()
