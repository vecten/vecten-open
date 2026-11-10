"""What a failure says: the full cause for the server's log, a short reason for the client.

Warehouse drivers put the warehouse's own message in their errors, which carries no secret,
but a connection error can carry a DSN; every text passes the redaction `vecten issues`
applies to evidence.
"""

from mcp.server.mcpserver.exceptions import ToolError

from vecten_shared.issues import cap, redact

LOG_LIMIT = 1000
REASON_LIMIT = 300
_DRIVERS = ("psycopg", "snowflake")
# Snowflake numbers its connector's and its login service's errors from 250000 up.
SNOWFLAKE_CLIENT_ERRORS = 250000
# Snowflake's refusal of an External OAuth token the server signed for the caller.
SNOWFLAKE_TOKEN_REJECTED = 390303


class ModelNotReady(ToolError):
    def __init__(self):
        super().__init__(
            "the semantic model is not ready, so this tool cannot answer; sql_exec still "
            "queries the warehouse. An administrator finds the reason in vecten issues"
        )


class NoWarehouseAccess(ToolError):
    """The caller's own account state, so telling them reveals nothing about the server."""

    def __init__(self):
        super().__init__(
            "your account has no warehouse access yet: it is not provisioned or it is disabled. "
            "An administrator runs vecten user add, or enables you again"
        )


def _chain(exc: BaseException) -> list[BaseException]:
    """`exc` and what it was raised from, outermost first."""
    chain = [exc]
    while exc.__cause__ is not None and all(exc.__cause__ is not s for s in chain):
        exc = exc.__cause__
        chain.append(exc)
    return chain


def _innermost(exc: BaseException) -> BaseException:
    return _chain(exc)[-1]


def _module(exc: BaseException) -> str:
    return type(exc).__module__.split(".")[0]


def _driver(exc: BaseException) -> bool:
    return _module(exc) in _DRIVERS


def _connection_failure(exc: BaseException, code: str) -> bool:
    """A failure to reach or sign in to the warehouse, whose text names hosts and roles the
    caller has no part in: SQLSTATE classes 08 and 28, Snowflake's client and login errors."""
    if _module(exc) == "snowflake":
        return int(code) >= SNOWFLAKE_CLIENT_ERRORS
    return code[:2] in {"08", "28"}


def _code_and_message(exc: BaseException) -> tuple[str | None, str]:
    """The warehouse's error code and message: psycopg's SQLSTATE, Snowflake's error number,
    whose own `str` repeats the code before the message."""
    if not _driver(exc):
        return None, str(exc)
    if type(exc).__module__.startswith("snowflake"):
        errno = getattr(exc, "errno", None)
        return (str(errno) if errno else None), str(getattr(exc, "raw_msg", None) or exc)
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    return getattr(exc, "sqlstate", None), primary or str(exc)


def _clean(text: str, limit: int) -> str:
    return cap(" ".join(redact(text).split()), limit)


def describe_error(exc: BaseException) -> str:
    """`<Class>[ <code>]: <message>` of the innermost cause, redacted and capped, for logs."""
    cause = _innermost(exc)
    code, message = _code_and_message(cause)
    label = type(cause).__name__ + (f" {code}" if code else "")
    return f"{label}: {_clean(message, LOG_LIMIT)}"


def warehouse_reason(exc: BaseException) -> str | None:
    """The reason a client is told when the warehouse refused the request; None for any
    other failure, whose text may describe the server rather than the caller's request."""
    chain = _chain(exc)
    cause = chain[-1]
    # SQLAlchemy wraps only the state database's driver errors; the warehouse is reached
    # through the drivers directly.
    if not _driver(cause) or any(_module(e) == "sqlalchemy" for e in chain):
        return None
    code, message = _code_and_message(cause)
    if code is None or _connection_failure(cause, code):
        return None
    return f"the warehouse refused the request ({code}): {_clean(message, REASON_LIMIT)}"


def sign_in_reason(exc: BaseException) -> str | None:
    """What a client is told when the warehouse refused the token the server signed for it;
    the warehouse's text, which names the host, stays in the log."""
    cause = _innermost(exc)
    if _module(cause) != "snowflake" or getattr(cause, "errno", None) != SNOWFLAKE_TOKEN_REJECTED:
        return None
    return (
        "the warehouse did not accept the sign-in token this server made for you, so nothing "
        "ran; an administrator finds the cause in vecten issues"
    )
