"""Whether Snowflake's own grammar refuses what the app SQL check refuses after LATERAL.

Runs only with VECTEN_SF_CI_ACCOUNT and VECTEN_SF_CI_PRIVATE_KEY set (the snowflake-live
workflow, started by hand before a release), as the service user VECTEN_CI. It creates nothing.
"""

import os

import pytest

from vecten_mcp.app_sql import relation, sql_problem

ACCOUNT = os.environ.get("VECTEN_SF_CI_ACCOUNT")
PRIVATE_KEY = os.environ.get("VECTEN_SF_CI_PRIVATE_KEY")
pytestmark = [
    pytest.mark.snowflake_live,
    pytest.mark.filterwarnings("ignore:X509.get_subject is deprecated:DeprecationWarning"),
    pytest.mark.skipif(
        not (ACCOUNT and PRIVATE_KEY), reason="needs VECTEN_SF_CI_ACCOUNT and _PRIVATE_KEY"
    ),
]
# Two tables every Snowflake account can read.
DECLARED = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.NATION"
UNDECLARED = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER"
STATEMENT = f"SELECT * FROM {DECLARED}, LATERAL TABLE('{UNDECLARED}') LIMIT 1"


def _connect():
    import snowflake.connector

    from vecten_shared.keypair import load_private_key

    return snowflake.connector.connect(
        account=ACCOUNT,
        login_timeout=30,
        user="VECTEN_CI",
        role="VECTEN_CI_ADMIN",
        warehouse="VECTEN_CI_WH",
        private_key=load_private_key(PRIVATE_KEY, "VECTEN_SF_CI_PRIVATE_KEY"),
    )


def test_the_check_refuses_a_table_literal_after_lateral():
    relations = frozenset({relation(DECLARED, "snowflake")})
    assert sql_problem(STATEMENT, kind="snowflake", relations=relations)


def test_snowflake_itself_refuses_a_table_literal_after_lateral():
    """If this fails, Snowflake reads a relation named in a string after LATERAL, and the
    check above is the only thing between an app and that relation."""
    import snowflake.connector.errors

    with _connect() as conn, conn.cursor() as cursor:
        with pytest.raises(snowflake.connector.errors.ProgrammingError):
            cursor.execute(STATEMENT)
