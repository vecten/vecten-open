"""Check extension declarations in a project's own CI, printing a refusal as the CLI does."""

import sys
from pathlib import Path

from vecten.extensions import check_declarations
from vecten_shared.output import Failure, render


def main(root: Path) -> int:
    try:
        check_declarations(root, project_ci=True)
    except Failure as failure:
        text, code = render(failure, as_json=False)
        print(text, file=sys.stderr)
        return code
    return 0


if __name__ == "__main__":
    sys.exit(main(Path.cwd()))
