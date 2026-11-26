"""Resolve public OAuth client metadata using one checked DNS answer."""

import asyncio
import ipaddress
import json
import re
import socket
import time
from collections import OrderedDict
from urllib.parse import unquote, urlsplit

import httpx

MAX_BYTES = 16 * 1024
MAX_AGE = 300


def redirect_allowed(uri):
    if not isinstance(uri, str) or not uri.isascii() or re.search(r"[\x00-\x20\x7f\\]", uri):
        return False
    try:
        url = urlsplit(uri)
        if url.port == 0:
            return False
    except ValueError:
        return False
    return bool(
        url.hostname
        and url.username is None
        and url.password is None
        and "#" not in uri
        and (
            url.scheme == "https"
            or url.scheme == "http"
            and url.hostname in {"127.0.0.1", "::1", "localhost"}
        )
    )


def metadata_redirect_matches(requested, registered):
    if not redirect_allowed(requested):
        return False
    if requested in registered:
        return True
    # RFC 8252 allows the native app to choose its listener port at runtime.
    loopback = re.compile(r"(http://(?:127\.0\.0\.1|\[::1\]))(?::[0-9]+)?([/?].*|)")
    match = loopback.fullmatch(requested)
    if match:
        for uri in registered:
            candidate = loopback.fullmatch(uri)
            if candidate and candidate.groups() == match.groups():
                return True
    return False


def _metadata_url(value):
    parts = urlsplit(value)
    if (
        len(value) > 2048
        or not redirect_allowed(value)
        or parts.scheme != "https"
        or not parts.path
        or parts.query
        or "%" in parts.netloc
        or any(piece in (".", "..") for piece in unquote(parts.path).split("/"))
    ):
        raise ValueError("invalid client metadata URL")
    return httpx.URL(value)


def _public(address):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if (
            ip.ipv4_mapped
            or ip.sixtofour
            or ip.teredo
            or ip in ipaddress.ip_network("64:ff9b::/96")
        ):
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _validate(body, client_id):
    doc = json.loads(body)
    if not isinstance(doc, dict) or doc.get("client_id") != client_id:
        raise ValueError("invalid client metadata")
    name = doc.get("client_name")
    redirects = doc.get("redirect_uris")
    grants = doc.get("grant_types", ["authorization_code"])
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 200
        or not name.strip()
        or not isinstance(redirects, list)
        or not 1 <= len(redirects) <= 10
        or not all(redirect_allowed(r) for r in redirects)
        or doc.get("token_endpoint_auth_method", "none") != "none"
        or "client_secret" in doc
        or "client_secret_expires_at" in doc
        or not isinstance(grants, list)
        or "authorization_code" not in grants
        or doc.get("response_types", ["code"]) != ["code"]
    ):
        raise ValueError("invalid client metadata")
    # A published document lists every grant its client uses with any server; only the two
    # this server issues to metadata clients carry over.
    offered = [g for g in ("authorization_code", "refresh_token") if g in grants]
    return {"client_name": name, "redirect_uris": redirects, "grant_types": offered}


def _lifetime(headers):
    directives = [part.strip().lower() for part in headers.get("cache-control", "").split(",")]
    if any(d.split("=")[0] in {"no-store", "no-cache", "private"} for d in directives):
        return 0
    ttl = MAX_AGE
    for d in directives:
        if d.startswith("max-age="):
            value = d.split("=", 1)[1].strip('"')
            ttl = min(ttl, int(value)) if value.isdecimal() and len(value) < 10 else 0
    age = headers.get("age", "0")
    return max(0, ttl - int(age)) if age.isdecimal() and len(age) < 10 else 0


class ClientMetadata:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.cache = OrderedDict()
        self.resolving = set()
        self.inflight = 0

    async def resolve(self, url):
        # Keep an outstanding lookup counted even if its caller times out. System resolver
        # threads can outlive a cancelled asyncio task.
        if len(self.resolving) >= 8:
            raise ValueError("client metadata resolver busy")
        lookup = asyncio.create_task(
            asyncio.get_running_loop().getaddrinfo(
                url.host, url.port or 443, type=socket.SOCK_STREAM
            )
        )
        self.resolving.add(lookup)

        def finished(task):
            self.resolving.discard(task)
            if not task.cancelled():
                task.exception()

        lookup.add_done_callback(finished)
        rows = await asyncio.shield(lookup)
        addresses = [row[4][0] for row in rows]
        if not addresses or not all(_public(address) for address in addresses):
            raise ValueError("invalid client metadata host")
        return addresses[0]

    async def fetch(self, client_id):
        url = _metadata_url(client_id)
        address = await self.resolve(url)
        # The numeric destination pins the checked address. Host, SNI and certificate
        # verification still use the name from the client's HTTPS URL.
        headers = {
            "Host": url.netloc.decode("ascii"),
            "Accept-Encoding": "identity",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=3) as client:
            async with client.stream(
                "GET",
                url.copy_with(host=address),
                headers=headers,
                extensions={"sni_hostname": url.host},
            ) as response:
                content_type = (
                    response.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                if (
                    response.status_code != 200
                    or not re.fullmatch(r"application/(?:json|[\w.-]+\+json)", content_type)
                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                ):
                    raise ValueError("invalid client metadata response")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > MAX_BYTES:
                        raise ValueError("client metadata exceeds size limit")
                    body.extend(chunk)
                return _validate(body, client_id), _lifetime(response.headers)

    async def get(self, client_id, limit, address):
        cached = self.cache.get(client_id)
        if cached and cached[0] > self.clock():
            self.cache.move_to_end(client_id)
            return cached[1]
        limit.hit(address)
        if self.inflight >= 8:
            raise ValueError("client metadata resolver busy")
        self.inflight += 1
        try:
            async with asyncio.timeout(4):
                doc, ttl = await self.fetch(client_id)
        except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError, RecursionError) as exc:
            raise ValueError("invalid_client") from exc
        finally:
            self.inflight -= 1
        if ttl:
            self.cache[client_id] = (self.clock() + ttl, doc)
            self.cache.move_to_end(client_id)
            if len(self.cache) > 256:
                self.cache.popitem(last=False)
        return doc
