"""The demo CRM API that `connector/evals/files/demo-api.md` documents, with fixed data.

Bearer tokens, cursor pagination (`limit` default 50, maximum 200), `updated_since` on every
list endpoint and a per-token rate limit, exactly as the document describes; nothing else.
The first companies and deals carry the values of the test suite's fixture project rows.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

BASE_PATH = "/v1"
READ_TOKEN = "demo-eval-read-token-5c1f0b7e9a"
NO_SCOPE_TOKEN = "demo-eval-noscope-token-41d2"
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
RATE_LIMIT, RATE_WINDOW = 100, 60.0
COMPANY_COUNT, DEAL_COUNT = 120, 300

COUNTRIES = ("DE", "GB", "FR", "PL", "US", "NL", "SE", "ES")
STAGES = ("open", "won", "lost")
SUFFIXES = ("GmbH", "Ltd", "SAS", "Sp. z o.o.", "Inc", "BV", "AB", "SL")
EPOCH = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

FIRST_COMPANIES = [
    (1, "Acme GmbH", "HTTPS://www.Acme.com/about", " Sales@Acme.com ", "DE"),
    (2, "Beta Ltd", "beta.co.uk", "hello@beta.co.uk", "GB"),
    (3, "Gamma SAS", "gamma.fr", "", "FR"),
]
FIRST_DEALS = [
    (1, 1, "Acme pilot", "won", 1200.5, "2026-02-20"),
    (2, 1, "Acme rollout", "open", 8000.0, None),
    (3, 2, "Beta trial", "lost", 300.0, "2026-02-28"),
    (4, 3, "Gamma renewal", "won", 4500.0, "2026-03-31"),
]


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def companies() -> list[dict]:
    rows = []
    for i in range(1, COMPANY_COUNT + 1):
        created = EPOCH + timedelta(days=i - 1)
        updated = created + timedelta(days=(i * 7) % 90, hours=i % 24)
        if i <= len(FIRST_COMPANIES):
            _, name, domain, email, country = FIRST_COMPANIES[i - 1]
        else:
            stem = f"company{i:03d}"
            name = f"Company {i:03d} {SUFFIXES[i % len(SUFFIXES)]}"
            domain = f"https://www.{stem}.example/contact" if i % 5 == 0 else f"{stem}.example"
            domain = domain.upper() if i % 11 == 0 else domain
            email = "" if i % 7 == 0 else f"info@{stem}.example"
            country = COUNTRIES[i % len(COUNTRIES)]
        rows.append(
            {
                "id": i,
                "name": name,
                "domain": domain,
                "email": email,
                "country": country,
                "created_at": _iso(created),
                "updated_at": _iso(updated),
            }
        )
    return rows


def deals() -> list[dict]:
    rows = []
    for j in range(1, DEAL_COUNT + 1):
        created = EPOCH + timedelta(days=20 + (j * 3) % 240, hours=10)
        if j <= len(FIRST_DEALS):
            _, company_id, title, stage, amount, close_date = FIRST_DEALS[j - 1]
        else:
            company_id = (j * 17) % COMPANY_COUNT + 1
            stage = STAGES[j % len(STAGES)]
            title = f"Company {company_id:03d} deal {j}"
            amount = round(500 + (j * 137) % 9500 + (j % 4) * 0.25, 2)
            closed = created + timedelta(days=10 + j % 50)
            close_date = None if stage == "open" else closed.date().isoformat()
        updated = created + timedelta(days=(j * 5) % 60, minutes=j % 60)
        rows.append(
            {
                "id": j,
                "company_id": company_id,
                "title": title,
                "stage": stage,
                "amount": amount,
                "created_at": _iso(created),
                "close_date": close_date,
                "is_won": stage == "won",
                "updated_at": _iso(updated),
            }
        )
    return rows


def encode_cursor(last_id: int) -> str:
    raw = json.dumps({"id": last_id}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    padded = cursor + "=" * (-len(cursor) % 4)
    value = json.loads(base64.urlsafe_b64decode(padded.encode()))
    if not isinstance(value, dict) or type(value.get("id")) is not int:
        raise ValueError(cursor)
    return value["id"]


def parse_since(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


Response = tuple[int, dict[str, str], bytes]


def _json(status: int, body: object, headers: dict[str, str] | None = None) -> Response:
    payload = json.dumps(body).encode()
    return status, {"content-type": "application/json", **(headers or {})}, payload


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> Response:
    return _json(status, {"error": message}, headers)


class DemoApi:
    def __init__(self, *, rate_limit: int = RATE_LIMIT, clock: Callable[[], float] = time.time):
        self.companies = companies()
        self.deals = deals()
        self.objects = {"companies": self.companies, "deals": self.deals}
        self.tokens = {READ_TOKEN: {"read"}, NO_SCOPE_TOKEN: set()}
        self.rate_limit, self.clock = rate_limit, clock
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def _throttled(self, token: str) -> int | None:
        """Seconds until the token's window reopens, or None while it has requests left."""
        now = self.clock()
        with self._lock:
            start, count = self._windows.get(token, (now, 0))
            if now - start >= RATE_WINDOW:
                start, count = now, 0
            if count >= self.rate_limit:
                return max(1, int(start + RATE_WINDOW - now + 0.999))
            self._windows[token] = (start, count + 1)
        return None

    def handle(self, method: str, path: str, query: str, headers: dict[str, str]) -> Response:
        name = path.removeprefix(BASE_PATH + "/").strip("/")
        if not path.startswith(BASE_PATH + "/") or name not in self.objects:
            return _error(404, f"no endpoint {path}")
        if method != "GET":
            return _error(405, f"{method} is not allowed on {path}")
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or token not in self.tokens:
            return _error(401, "missing or invalid bearer token")
        if "read" not in self.tokens[token]:
            return _error(403, "the token lacks the read scope")
        wait = self._throttled(token)
        if wait is not None:
            return _error(429, "rate limit exceeded", {"retry-after": str(wait)})
        params = {key: values[-1] for key, values in parse_qs(query).items()}
        try:
            limit = int(params.get("limit", DEFAULT_LIMIT))
        except ValueError:
            return _error(400, "limit must be an integer")
        if not 1 <= limit <= MAX_LIMIT:
            return _error(400, f"limit must be between 1 and {MAX_LIMIT}")
        rows = self.objects[name]
        if "updated_since" in params:
            try:
                since = parse_since(params["updated_since"])
            except ValueError:
                return _error(400, "updated_since must be an ISO 8601 timestamp")
            rows = [row for row in rows if parse_since(row["updated_at"]) >= since]
        if "cursor" in params:
            try:
                after = decode_cursor(params["cursor"])
            except ValueError, binascii.Error, UnicodeDecodeError:
                return _error(400, "invalid cursor")
            rows = [row for row in rows if row["id"] > after]
        page = rows[:limit]
        cursor = encode_cursor(page[-1]["id"]) if len(rows) > limit else None
        return _json(200, {"data": page, "next_cursor": cursor})


def app(api: DemoApi):
    """`api` as an ASGI application."""

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        status, response_headers, body = api.handle(
            scope["method"], scope["path"], scope["query_string"].decode("latin-1"), headers
        )
        # Closing after each response leaves no idle client connection for the harness's
        # shutdown to wait on or abandon.
        response_headers = {**response_headers, "connection": "close"}
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(k.encode(), v.encode()) for k, v in response_headers.items()],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return asgi
