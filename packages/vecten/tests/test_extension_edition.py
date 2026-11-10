import importlib.util
from pathlib import Path

import pytest


def checker():
    path = Path(__file__).resolve().parents[3] / "scripts/check_extension_wheels.py"
    spec = importlib.util.spec_from_file_location("wheel_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "source",
    [
        "import posthog",
        "from onelogin.saml2 import auth",
        "license_key = 'key'",
        "def check_licence(): pass",
    ],
)
def test_boundary_check_rejects_planted_gating_and_telemetry(source):
    with pytest.raises(ValueError):
        checker().check_source(source, "planted.py")


def test_boundary_check_accepts_public_hooks():
    checker().check_source("def identity_backend(name): return name", "hooks.py")
