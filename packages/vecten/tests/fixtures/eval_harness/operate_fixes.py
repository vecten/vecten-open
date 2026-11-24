"""Checks on the Git objects the fake GitHub merged for an operate case."""

import ast
import json
import sqlite3
import subprocess
from contextlib import closing

import yaml
from sqlglot import parse_one
from sqlglot.errors import ParseError


def _file(bare, commit, path):
    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"], cwd=bare, capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else ""


def reads_deal_name(source):
    """Trace a yielded deal name through simple assignments without executing agent code."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    def path(node, assignments, seen=frozenset()):
        if isinstance(node, ast.Name):
            if node.id in assignments and node.id not in seen:
                return path(assignments[node.id], assignments, seen | {node.id})
            return (node.id,)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            return (*path(node.value, assignments, seen), node.slice.value)
        return ()

    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef) or function.name != "deals":
            continue
        assignments = {
            target.id: item.value
            for item in ast.walk(function)
            if isinstance(item, ast.Assign)
            for target in item.targets
            if isinstance(target, ast.Name)
        }
        inputs = {
            loop.target.id
            for loop in ast.walk(function)
            if isinstance(loop, ast.For) and isinstance(loop.target, ast.Name)
        }
        outputs = []
        for item in ast.walk(function):
            if not isinstance(item, ast.Yield):
                continue
            row = item.value
            if isinstance(row, ast.Name):
                row = assignments.get(row.id)
            outputs.append(
                isinstance(row, ast.Dict)
                and any(
                    isinstance(key, ast.Constant)
                    and key.value in ("dealname", "deal_name")
                    and path(value, assignments)
                    in {(name, "properties", "deal_name") for name in inputs}
                    for key, value in zip(row.keys, row.values, strict=True)
                )
            )
        if outputs and all(outputs):
            return True
    return False


def tests_closed_deals(schema):
    """Evaluate a not_null test's scope over one NULL amount for each known stage."""
    try:
        models = yaml.safe_load(schema)["models"]
        mart = next(m for m in models if m["name"] == "sales_pipeline")
        column = next(c for c in mart["columns"] if c["name"] == "amount")
        tests = column.get("data_tests", column.get("tests", []))
        selected = set()
        for test in tests:
            if test == "not_null" or (isinstance(test, dict) and test.get("not_null", {}) is None):
                selected.update(("open", "won", "lost"))
                continue
            if not isinstance(test, dict) or not isinstance(test.get("not_null"), dict):
                continue
            config = test["not_null"].get("config", test["not_null"])
            if (
                config.get("enabled", True) is not True
                or config.get("severity", "error").lower() != "error"
            ):
                continue
            where = config.get("where")
            if where is None:
                selected.update(("open", "won", "lost"))
                continue
            if not isinstance(where, str):
                return False
            with closing(sqlite3.connect(":memory:")) as db:
                db.execute(
                    "create table deals(stage text, amount numeric, is_won int, close_date text)"
                )
                db.executemany(
                    "insert into deals values(?,NULL,?,?)",
                    [("open", 0, None), ("won", 1, "2026-10-19"), ("lost", 0, "2026-10-20")],
                )

                def allow(action, table, column, database, trigger):
                    if action == sqlite3.SQLITE_SELECT or (
                        action == sqlite3.SQLITE_READ and table == "deals"
                    ):
                        return sqlite3.SQLITE_OK
                    return sqlite3.SQLITE_DENY

                db.set_authorizer(allow)
                db.set_progress_handler(lambda: 1, 10000)
                selected.update(
                    row[0] for row in db.execute(f"select stage from deals where ({where})")
                )
        return selected == {"won", "lost"}
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        StopIteration,
        yaml.YAMLError,
        sqlite3.Error,
    ):
        return False


def subtracts_refunds(text):
    try:
        metrics = [m for doc in json.loads(text)["semantic_model"] for m in doc.get("metrics", [])]
        metric = next(m for m in metrics if m["name"] == "total_revenue")
        expressions = metric["expression"]["dialects"]
        actual = next(e["expression"] for e in expressions if e["dialect"] == "ANSI_SQL")
        return parse_one(actual) == parse_one(
            "SUM(sales_pipeline.amount) - SUM(sales_pipeline.refunded_amount)"
        )
    except ValueError, TypeError, KeyError, StopIteration, ParseError:
        return False


def verifies(store, repo, pull, issue):
    checks = {
        "connector": ("connectors/hubspot/connector.py", reads_deal_name),
        "model": ("dbt/models/marts/schema.yml", tests_closed_deals),
        "semantic": ("osi/metrics.json", subtracts_refunds),
    }
    if issue["resolution"] not in checks:
        return False
    path, check = checks[issue["resolution"]]
    bare = store.bare(repo)
    commit = pull["merge_commit_sha"]
    current = _file(bare, commit, path)
    return bool(current) and current != _file(bare, commit + "^1", path) and check(current)
