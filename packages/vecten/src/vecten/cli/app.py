from __future__ import annotations

import importlib
import pkgutil

import typer

from vecten.cli import commands as commands_pkg

app = typer.Typer(add_completion=False, no_args_is_help=True, rich_markup_mode=None)


@app.callback()
def _root() -> None:
    """Vecten Open: one CLI over a data platform bootstrap and a data MCP."""


def _register_all() -> None:
    for module_info in pkgutil.iter_modules(commands_pkg.__path__):
        module = importlib.import_module(f"{commands_pkg.__name__}.{module_info.name}")
        module.register(app)


_register_all()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
