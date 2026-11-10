"""`python packages/vecten/tests/fixtures/eval_harness ...`: see `eval_harness.cli`."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent
# Run as a directory, Python puts this directory on the path, where the package's modules
# would import under bare names; the package and the mocked cloud live one level up.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]
for entry in (FIXTURES / "mock_cloud", FIXTURES):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from eval_harness.cli import main  # noqa: E402

sys.exit(main())
