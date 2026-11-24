"""Checks on the Git objects the fake GitHub merged for an operate case."""

import ast
import re
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from sqlglot import Dialect, exp, parse_one
from sqlglot.errors import SqlglotError

from vecten_shared.osi.evals import EVALS_PATH, load_cases, run_case
from vecten_shared.osi.issues import ModelError
from vecten_shared.osi.semantic import SemanticModel


def _file(bare, commit, path):
    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"], cwd=bare, capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else ""


def _field_value(node, env):
    """Evaluate only data access on the synthetic record, without importing the connector."""
    match node:
        case ast.Constant(value=value):
            return value
        case ast.Name(id=name):
            return env[name]
        case ast.Subscript(value=value, slice=key):
            return _field_value(value, env)[_field_value(key, env)]
        case ast.Dict(keys=keys, values=values):
            return {
                _field_value(k, env): _field_value(v, env)
                for k, v in zip(keys, values, strict=True)
            }
        case ast.Call(func=ast.Attribute(value=obj, attr="get"), args=args, keywords=[]):
            return dict.get(_field_value(obj, env), *(_field_value(arg, env) for arg in args))
        case ast.Compare(left=left, ops=[op], comparators=[right]):
            value, choices = _field_value(left, env), _field_value(right, env)
            if isinstance(op, ast.In):
                return value in choices
            if isinstance(op, ast.NotIn):
                return value not in choices
        case ast.IfExp(test=test, body=body, orelse=other):
            return _field_value(body if _field_value(test, env) else other, env)
        case ast.BoolOp(op=ast.Or(), values=values):
            for expr in values:
                value = _field_value(expr, env)
                if value:
                    break
            return value
    raise ValueError("unsupported record expression")


def _landed_rows(nodes, env):
    for node in nodes:
        match node:
            case ast.Assign(targets=[ast.Name(id=name)], value=value):
                env[name] = _field_value(value, env)
            case ast.If(test=test, body=body, orelse=other):
                yield from _landed_rows(body if _field_value(test, env) else other, env)
            case ast.Expr(value=ast.Yield(value=value)):
                yield _field_value(value, env)
            case ast.Raise():
                raise KeyError("missing field")
            case _:
                raise ValueError("unsupported connector statement")


def reads_deal_name(source):
    try:
        tree = ast.parse(source)
        constants = {
            node.targets[0].id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
        }
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef) or function.name != "deals":
                continue
            for loop in ast.walk(function):
                if not isinstance(loop, ast.For) or not isinstance(loop.target, ast.Name):
                    continue
                marker = object()

                def rows(properties, loop=loop):
                    row = {"id": "1", "properties": properties}
                    return list(_landed_rows(loop.body, {**constants, loop.target.id: row}))

                landed = rows({"deal_name": marker})
                if len(landed) != 1 or landed[0].get("dealname") is not marker:
                    continue
                try:
                    missing = rows({})
                except KeyError:
                    return True
                return all(row.get("dealname") is not None for row in missing)
    except AttributeError, KeyError, TypeError, SyntaxError, ValueError:
        pass
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

                # SQLGlot accepts some SQLite comparison spellings even with read="postgres".
                if any(
                    token.text == "==" and token.token_type.name == "EQ"
                    for token in Dialect.get_or_raise("postgres").tokenize(where)
                ):
                    return False
                predicate = parse_one(where, read="postgres")
                if any(isinstance(op.expression, exp.Literal) for op in predicate.find_all(exp.Is)):
                    return False
                predicate_sql = predicate.sql(dialect="sqlite")
                selected.update(
                    row[0] for row in _read(db, f"select stage from deals where ({predicate_sql})")
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
        SqlglotError,
    ):
        return False


def _read(db, sql):
    def allow(action, _object, name, _schema, _trigger):
        if action in {sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT} or (
            action == sqlite3.SQLITE_FUNCTION and name != "load_extension"
        ):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    db.set_authorizer(allow)
    db.set_progress_handler(lambda: 1, 10000)
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
    return db.execute(sql)


def _semantic_fix(bare, commit, gap):
    mart = _file(bare, commit, "dbt/models/marts/sales_pipeline.sql")
    rendered = re.sub(r"\{\{\s*ref\(\s*['\"](deal|company)['\"]\s*\)\s*}}", r"\1", mart)
    try:
        select = parse_one(rendered, read="postgres")
        if not isinstance(select, exp.Query):
            return False
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript("""
                CREATE TABLE deal(deal_id, company_id, stage, amount, refunded_amount,
                                  created_at, close_date, is_won);
                CREATE TABLE company(company_id, name, country);
                INSERT INTO company VALUES (1,'Demo','DE');
                INSERT INTO deal VALUES (1,1,'won',100,17,NULL,NULL,1),
                                        (2,1,'won',40,NULL,NULL,NULL,1);
            """)
            # A projected literal or a refund field present only in core cannot satisfy both rows.
            mart_sql = select.sql(dialect="sqlite")
            observed = list(
                _read(
                    db,
                    f"SELECT amount, refunded_amount FROM ({mart_sql}) ORDER BY amount",
                )
            )
            if observed != [(40, None), (100, 17)]:
                return False
            with TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "osi").mkdir()
                names = (
                    subprocess.check_output(
                        ["git", "ls-tree", "-rz", "--name-only", commit, "--", "osi"], cwd=bare
                    )
                    .decode()
                    .split("\0")
                )
                for name in filter(None, names):
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(_file(bare, commit, name))
                model = SemanticModel.load(root / "osi", kind="postgres")
                field = model.fields("sales_pipeline")["refunded_amount"]
                source = parse_one(field["expression_sql"], read="postgres")
                if not isinstance(source, exp.Column) or source.name != "refunded_amount":
                    return False
                metric = model.metrics["total_revenue"]["expression_sql"]
                amount = parse_one(metric, read="postgres").sql(dialect="sqlite")
                actual = _read(
                    db, f"SELECT {amount} FROM ({mart_sql}) AS sales_pipeline"
                ).fetchall()
                if actual != [(123,)]:
                    return False
                path = root / EVALS_PATH
                path.parent.mkdir(parents=True)
                path.write_text(_file(bare, commit, EVALS_PATH.as_posix()))
                return any(
                    case.gap == gap
                    and case.expect["outcome"] == "compiled"
                    and "total_revenue" in case.request.get("metrics", [])
                    and run_case(model, case).ok
                    for case in load_cases(path)
                )
    except KeyError, ValueError, TypeError, ModelError, sqlite3.Error, SqlglotError:
        return False


def verifies(store, repo, pull, issue):
    bare = store.bare(repo)
    commit = pull["merge_commit_sha"]
    if issue["resolution"] == "semantic":
        changed = subprocess.run(
            [
                "git",
                "diff",
                "--quiet",
                commit + "^1",
                commit,
                "--",
                "osi",
                EVALS_PATH.as_posix(),
                "dbt/models/marts/sales_pipeline.sql",
            ],
            cwd=bare,
            capture_output=True,
        )
        return changed.returncode == 1 and _semantic_fix(bare, commit, issue["evidence"]["gap_id"])
    checks = {
        "connector": ("connectors/hubspot/connector.py", reads_deal_name),
        "model": ("dbt/models/marts/schema.yml", tests_closed_deals),
    }
    if issue["resolution"] not in checks:
        return False
    path, check = checks[issue["resolution"]]
    current = _file(bare, commit, path)
    return bool(current) and current != _file(bare, commit + "^1", path) and check(current)
