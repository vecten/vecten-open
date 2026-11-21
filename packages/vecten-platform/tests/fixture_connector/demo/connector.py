"""Test-only connector: two healthy objects and one that always fails."""

import dlt
from requests.exceptions import HTTPError

from vecten_platform.sync import TerminalConnectorError, secret


@dlt.resource(name="good_a", write_disposition="append")
def good_a(limit=None, cursor=dlt.sources.incremental("id", initial_value=0)):  # noqa: B008
    yield from [{"id": 1, "name": "a1"}, {"id": 2, "name": "a2"}][:limit]


@dlt.resource(name="bad")
def bad(credits=None):
    if credits is not None:
        try:
            raise HTTPError("402 Payment Required" if credits == 0 else "503 Service Unavailable")
        except HTTPError as exc:
            if credits == 0:
                raise TerminalConnectorError("metered source is out of credits") from exc
            raise
    raise RuntimeError("upstream returned 500")
    yield


@dlt.resource(name="good_b", write_disposition="append")
def good_b():
    yield from [{"id": 10, "amount": 1.5}]


@dlt.source(name="demo")
def demo_source(token: str, limit: int | None = None, credits: int | None = None):
    return [good_a(limit=limit), bad(credits=credits), good_b]


def source(limit: int | None = None, credits: int | None = None):
    return demo_source(token=secret("DEMO_TOKEN"), limit=limit, credits=credits)
