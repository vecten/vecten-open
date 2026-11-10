"""Reads a dlt source package without importing it: which function, which secrets."""

from __future__ import annotations

import ast
from pathlib import Path

from vecten_shared.output import ExitCode, Failure


def _is_dlt_source_decorator(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, ast.Attribute) and ast.unparse(target) == "dlt.source"


def _source_functions(init_py: Path) -> list[ast.FunctionDef]:
    tree = ast.parse(init_py.read_text(encoding="utf-8"), filename=str(init_py))
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(_is_dlt_source_decorator(d) for d in node.decorator_list)
    ]


def _source_function(init_py: Path, source_name: str) -> ast.FunctionDef:
    functions = _source_functions(init_py)
    if not functions:
        raise Failure(ExitCode.TOOL_FAILED, f"{init_py} defines no @dlt.source function")
    return next((f for f in functions if f.name == source_name), functions[0])


def source_function_name(init_py: Path, source_name: str) -> str:
    return _source_function(init_py, source_name).name


def secret_params(init_py: Path, source_name: str) -> list[str]:
    fn = _source_function(init_py, source_name)
    args = fn.args
    positional = args.posonlyargs + args.args
    defaults = dict(
        zip(
            [a.arg for a in positional[len(positional) - len(args.defaults) :]],
            args.defaults,
            strict=True,
        )
    )
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if default is not None:
            defaults[arg.arg] = default
    return [name for name, d in defaults.items() if ast.unparse(d) == "dlt.secrets.value"]
