"""Adapters over a recording cursor, loaded by path from the tests that use them: the
`tests` package name is shared with the other packages' tests, so a relative import resolves
to whichever was collected first."""

from dataclasses import replace

from vecten_mcp.warehouse.postgres import PostgresAdapter
from vecten_mcp.warehouse.snowflake import SnowflakeAdapter
from vecten_shared.config import VectenConfig
from vecten_shared.provider import snowflake_names


class Cursor:
    description = [("privilege",), ("granted_on",), ("name",)]

    def __init__(self, answer):
        self.answer, self.statements = answer, []

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self

    def fetchall(self):
        if "FUTURE" in self.statements[-1][0]:
            return []
        return self.answer

    def fetchone(self):
        return self.answer[0] if self.answer else None

    def close(self):
        pass


def names(*, shared_account=False):
    """Project `acme`'s names: the fixed layout over database D and warehouse W, or its
    shared-account dev environment's."""
    config = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "acme", "scaffold_version": "0.1.1"},
            "warehouse": {"kind": "snowflake", "account": "a"},
        }
    )
    if shared_account:
        return snowflake_names(config, "dev", shared_account=True)
    return replace(snowflake_names(config, "prod"), database="D", warehouse="W")


def snowflake(answer, *, shared_account=False):
    cursor = Cursor(answer)
    adapter = SnowflakeAdapter(
        lambda: cursor, None, None, names=names(shared_account=shared_account)
    )
    return adapter, cursor


def postgres(answer):
    cursor = Cursor(answer)
    return PostgresAdapter(lambda: cursor, None), cursor
