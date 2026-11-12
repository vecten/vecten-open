"""An in-memory Scaleway organization behind the control plane's REST client and S3 factory."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import itertools
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from vecten.deploy.scaleway import bootstrap as bootstrap_mod
from vecten.deploy.scaleway import clients
from vecten.deploy.scaleway import rdb as rdb_mod
from vecten.deploy.scaleway.clients import ApiError

ORGANIZATION = "0aa0aa0a-0000-4000-8000-000000000001"
OPERATOR_ACCESS = "SCWOPERATOR000000001"
OPERATOR_SECRET = "0eadbeef-0000-4000-8000-0000000000ff"
OPERATOR_USER = "0bb0bb0b-0000-4000-8000-000000000002"
START = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
CERTIFICATE_PEM = b"-----BEGIN CERTIFICATE-----\nMIIBfakeRdbCa\n-----END CERTIFICATE-----\n"
# The project holding the rendered project's DNS zone, `acme.com`: the organization's default
# project, which bootstrap neither creates nor lists.
DNS_PROJECT = "99999999-0000-4000-8000-000000000009"
SUBZONE_PROJECT = "88888888-0000-4000-8000-000000000008"


def _ts(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class Clock:
    def __init__(self, moment: datetime = START):
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta: Any) -> None:
        self.moment += timedelta(**delta)


class FakeScaleway:
    """Projects, IAM and Secret Manager, as the REST paths `clients.client` reaches them."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self._ids = itertools.count(16)
        self.projects: dict[str, dict] = {}
        self.applications: dict[str, dict] = {}
        self.policies: dict[str, dict] = {}
        self.rules: dict[str, list[dict]] = {}
        self.keys: dict[str, dict] = {
            OPERATOR_ACCESS: {
                "access_key": OPERATOR_ACCESS,
                "user_id": OPERATOR_USER,
                "application_id": None,
                "expires_at": None,
                "created_at": _ts(START - timedelta(days=400)),
            }
        }
        self.secrets: dict[str, dict] = {}
        self.versions: dict[str, list[str]] = {}
        # Domains and DNS zones of the organization, as ListDNSZones answers them.
        self.dns_zones: list[dict] = [
            {"domain": "acme.com", "subdomain": "", "project_id": DNS_PROJECT},
            {"domain": "acme.com", "subdomain": "eu", "project_id": SUBZONE_PROJECT},
        ]
        self.calls: list[tuple[str, str, str, Any, Any]] = []
        # Secret keys every CreateAPIKey answered, to search outputs for leaks.
        self.issued_secrets: list[str] = []
        # (api, method, path prefix) -> (status, type) the next matching call fails with.
        self.failures: dict[tuple[str, str, str], tuple[int, str]] = {}
        self.unauthorized = False
        # Instances: server id -> {"id", "creation_date", "user_data": {key: value}}.
        self.servers: dict[str, dict] = {}
        # Every user data value ever set, to search outputs for leaks.
        self.user_data_written: list[str] = []
        # Callables hearing (server id, key, value) on each user data PATCH.
        self.on_user_data: list[Callable[[str, str, str], None]] = []
        # RDB: instance id -> {"id", "name", "project_id", "region", "endpoints", "users"}.
        self.rdb_instances: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}
        # How many reads an instance answers `configuring` after one of its endpoints goes.
        self.configure_reads = 1
        # The clock and sleep the RDB readiness wait uses; `install` patches them in.
        self.rdb_time = FakeTime()
        # How many reads a new snapshot answers `creating` before `snapshot_final_status`.
        self.snapshot_reads_creating = 1
        self.snapshot_final_status = "ready"
        # Scaleway answers a snapshot created without `expires_at` with one 365 days out, the
        # longest it keeps a snapshot.
        self.snapshot_default_expires_at = "2027-10-18T15:30:00Z"
        self.snapshot_requests: list[dict] = []
        # A shared, ordered record of REST and Object Storage calls; `install` shares it.
        self.log: list[tuple[str, ...]] = []

    def _id(self) -> str:
        n = next(self._ids)
        return f"{n:08x}-0000-4000-8000-{n:012x}"

    def client(self, api: str, region: str | None = None) -> FakeApi:
        return FakeApi(self, api, region)

    def writes(self) -> list[tuple[str, str, str]]:
        return [(a, m, p) for a, m, p, _, _ in self.calls if m != "GET"]

    def app_named(self, name: str) -> dict | None:
        return next((a for a in self.applications.values() if a["name"] == name), None)

    def keys_of(self, application_id: str) -> list[dict]:
        return [k for k in self.keys.values() if k["application_id"] == application_id]

    def policy_of(self, application_id: str) -> dict:
        (policy,) = [p for p in self.policies.values() if p["application_id"] == application_id]
        return policy

    def rules_of_app(self, name: str) -> list[dict]:
        app = self.app_named(name)
        assert app is not None, name
        return self.rules[self.policy_of(app["id"])["id"]]

    def secret_value(self, path: str, name: str) -> str | None:
        for sid, secret in self.secrets.items():
            if secret["path"] == path and secret["name"] == name:
                versions = self.versions[sid]
                return base64.b64decode(versions[-1]).decode() if versions else None
        return None

    def add_secret(self, project_id: str, path: str, name: str, value: str | None) -> str:
        sid = self._id()
        self.secrets[sid] = {
            "id": sid,
            "name": name,
            "path": path,
            "project_id": project_id,
            "region": "fr-par",
            "protected": False,
        }
        self.versions[sid] = [] if value is None else [base64.b64encode(value.encode()).decode()]
        return sid

    def add_server(
        self,
        server_id: str,
        created: datetime,
        name: str = "",
        project: str = "",
        tags: tuple[str, ...] = (),
    ) -> dict:
        server = {
            "id": server_id,
            "name": name,
            "project": project,
            "tags": list(tags),
            "creation_date": _ts(created),
            "user_data": {},
        }
        self.servers[server_id] = server
        return server

    def add_rdb(self, name: str, project_id: str, *, public: bool = False) -> dict:
        endpoints = [
            {
                "id": self._id(),
                "ip": "172.16.0.4",
                "port": 5432,
                "private_network": {"private_network_id": "pn"},
                "load_balancer": None,
            }
        ]
        if public:
            endpoints.append(
                {
                    "id": self._id(),
                    "ip": "51.15.0.9",
                    "port": 20123,
                    "private_network": None,
                    "load_balancer": {},
                }
            )
        instance = {
            "id": self._id(),
            "name": name,
            "project_id": project_id,
            "region": "fr-par",
            "status": "ready",
            "endpoints": endpoints,
            # The users and the password each one signs in with.
            "users": {MASTER_USER: MASTER_PASSWORD},
            # Reads left before a `configuring` instance answers `ready`.
            "pending_reads": 0,
        }
        self.rdb_instances[instance["id"]] = instance
        return instance

    def rdb_named(self, name: str) -> dict | None:
        return next((i for i in self.rdb_instances.values() if i["name"] == name), None)

    def secret_versions(self, path: str, name: str) -> int:
        for sid, secret in self.secrets.items():
            if secret["path"] == path and secret["name"] == name:
                return len(self.versions[sid])
        return 0


def _page(items: list[dict], key: str, params: dict | None) -> dict:
    params = params or {}
    size = int(params.get("page_size", 50))
    page = int(params.get("page", 1))
    chunk = items[(page - 1) * size : page * size]
    return {key: copy.deepcopy(chunk), "total_count": len(items)}


class FakeApi:
    def __init__(self, org: FakeScaleway, api: str, region: str | None):
        self.org = org
        self.api = api
        self.region = region

    def request(
        self,
        method: str,
        path: str,
        json: Any = None,
        params: Any = None,
        data: str | None = None,
    ) -> dict:
        org = self.org
        body = data if data is not None else copy.deepcopy(json)
        org.calls.append((self.api, method, path, body, copy.deepcopy(params)))
        org.log.append(("rest", self.api, method, path))
        self._data = data
        if org.unauthorized:
            raise ApiError(f"Scaleway {self.api} {method} {path}", 401, "denied_authentication")
        for (api, verb, prefix), (status, kind) in list(org.failures.items()):
            if api == self.api and verb == method and path.startswith(prefix):
                del org.failures[(api, verb, prefix)]
                raise ApiError(f"Scaleway {self.api} {method} {path}", status, kind)
        handler = getattr(self, f"_{self.api.replace('-', '_')}")
        return handler(method, path, json or {}, params or {})

    def _missing(self, method: str, path: str) -> ApiError:
        return ApiError(f"Scaleway {self.api} {method} {path}", 404, "not_found")

    def _domain(self, method, path, body, params):
        if (method, path) != ("GET", "/dns-zones"):
            raise self._missing(method, path)
        if params.get("organization_id") != ORGANIZATION:
            return _page([], "dns_zones", params)
        # The filter matches by the zone's full name; a subzone answers only when named.
        wanted = set(params.get("dns_zones") or [])

        def name(zone: dict) -> str:
            return f"{zone['subdomain']}.{zone['domain']}" if zone["subdomain"] else zone["domain"]

        found = [z for z in self.org.dns_zones if not wanted or name(z) in wanted]
        return _page(found, "dns_zones", params)

    def _account(self, method, path, body, params):
        org = self.org
        if (method, path) == ("GET", "/projects"):
            found = [
                p
                for p in org.projects.values()
                if p["organization_id"] == params.get("organization_id")
                and (not params.get("name") or params["name"] in p["name"])
            ]
            return _page(found, "projects", params)
        if (method, path) == ("POST", "/projects"):
            project = {
                "id": org._id(),
                "name": body["name"],
                "organization_id": body["organization_id"],
                "description": body.get("description", ""),
            }
            org.projects[project["id"]] = project
            return dict(project)
        raise self._missing(method, path)

    def _iam(self, method, path, body, params):
        org = self.org
        parts = path.strip("/").split("/")
        if parts[0] == "applications":
            if method == "GET" and len(parts) == 1:
                found = [
                    a
                    for a in org.applications.values()
                    if a["organization_id"] == params.get("organization_id")
                    and (not params.get("name") or params["name"] in a["name"])
                ]
                return _page(found, "applications", params)
            if method == "POST":
                app = {
                    "id": org._id(),
                    "name": body["name"],
                    "organization_id": body["organization_id"],
                    "description": body.get("description", ""),
                }
                org.applications[app["id"]] = app
                return dict(app)
            if method == "DELETE" and parts[1] in org.applications:
                app_id = parts[1]
                del org.applications[app_id]
                for access in [k["access_key"] for k in org.keys_of(app_id)]:
                    del org.keys[access]
                return {}
        if parts[0] == "policies":
            if method == "GET" and len(parts) == 1:
                ids = params.get("application_ids") or []
                found = [
                    p
                    for p in org.policies.values()
                    if (not ids or p["application_id"] in ids)
                    and (not params.get("policy_name") or params["policy_name"] in p["name"])
                ]
                return _page(found, "policies", params)
            if method == "POST":
                policy = {
                    "id": org._id(),
                    "name": body["name"],
                    "organization_id": body["organization_id"],
                    "application_id": body.get("application_id"),
                }
                org.policies[policy["id"]] = policy
                org.rules[policy["id"]] = [
                    {"id": org._id(), **copy.deepcopy(r)} for r in body.get("rules", [])
                ]
                return dict(policy)
            if method == "DELETE" and parts[1] in org.policies:
                del org.policies[parts[1]]
                del org.rules[parts[1]]
                return {}
        if parts[0] == "rules":
            if method == "GET":
                return _page(org.rules[params["policy_id"]], "rules", params)
            if method == "PUT":
                org.rules[body["policy_id"]] = [
                    {"id": org._id(), **copy.deepcopy(r)} for r in body["rules"]
                ]
                return {"rules": copy.deepcopy(org.rules[body["policy_id"]])}
        if parts[0] == "api-keys":
            if method == "GET" and len(parts) == 2:
                if parts[1] not in org.keys:
                    raise self._missing(method, path)
                return dict(org.keys[parts[1]])
            if method == "GET":
                # The live API refuses a key listing without the organization.
                if params.get("organization_id") != ORGANIZATION:
                    raise ApiError(f"Scaleway {self.api} {method} {path}", 400, "invalid_arguments")
                found = [
                    k
                    for k in org.keys.values()
                    if params.get("bearer_type") == "application"
                    and k["application_id"] == params.get("bearer_id")
                ]
                return _page(found, "api_keys", params)
            if method == "POST":
                n = next(org._ids)
                access = f"SCW{n:017d}"
                secret = f"{n:08x}-5ec7-4000-8000-{n:012x}"
                key = {
                    "access_key": access,
                    "application_id": body["application_id"],
                    "user_id": None,
                    "default_project_id": body.get("default_project_id"),
                    "description": body.get("description", ""),
                    "expires_at": body.get("expires_at"),
                    "created_at": _ts(org.clock()),
                }
                org.keys[access] = key
                org.issued_secrets.append(secret)
                return {**key, "secret_key": secret}
            if method == "DELETE" and parts[1] in org.keys:
                del org.keys[parts[1]]
                return {}
        raise self._missing(method, path)

    def _secret_manager(self, method, path, body, params):
        org = self.org
        parts = path.strip("/").split("/")
        if parts == ["secrets"] and method == "GET":
            found = [
                s
                for s in org.secrets.values()
                if s["project_id"] == params.get("project_id")
                and (not params.get("path") or s["path"] == params["path"])
                and (not params.get("name") or params["name"] in s["name"])
            ]
            return _page(found, "secrets", params)
        if parts == ["secrets"] and method == "POST":
            secret = {
                "id": org._id(),
                "name": body["name"],
                "path": body["path"],
                "project_id": body["project_id"],
                "region": self.region,
                "protected": body["protected"],
            }
            org.secrets[secret["id"]] = secret
            org.versions[secret["id"]] = []
            return dict(secret)
        if len(parts) >= 2 and parts[1] not in org.secrets:
            raise self._missing(method, path)
        if len(parts) == 2 and method == "DELETE":
            del org.secrets[parts[1]]
            del org.versions[parts[1]]
            return {}
        if parts[2:] == ["versions"] and method == "POST":
            org.versions[parts[1]].append(body["data"])
            return {"revision": len(org.versions[parts[1]])}
        if parts[2:] == ["versions", "latest_enabled", "access"] and method == "GET":
            versions = org.versions[parts[1]]
            if not versions:
                raise self._missing(method, path)
            return {"data": versions[-1], "revision": len(versions)}
        raise self._missing(method, path)

    def _instance(self, method, path, body, params):
        org = self.org
        parts = path.strip("/").split("/")
        if parts == ["servers"] and method == "GET":
            wanted = [t for t in str(params.get("tags", "")).split(",") if t]
            found = [
                {k: v for k, v in s.items() if k != "user_data"}
                for s in org.servers.values()
                if (not params.get("project") or s["project"] == params["project"])
                and all(t in s["tags"] for t in wanted)
            ]
            size, page = int(params.get("per_page", 50)), int(params.get("page", 1))
            chunk = found[(page - 1) * size : page * size]
            return {"servers": copy.deepcopy(chunk), "total_count": len(found)}
        if parts[0] != "servers" or len(parts) < 2 or parts[1] not in org.servers:
            raise self._missing(method, path)
        server = org.servers[parts[1]]
        if len(parts) == 2 and method == "GET":
            return {"server": {k: v for k, v in server.items() if k != "user_data"}}
        if parts[2:] == ["user_data"] and method == "GET":
            return {"user_data": sorted(server["user_data"])}
        if parts[2:3] == ["user_data"] and len(parts) == 4:
            key = parts[3]
            if method == "PATCH":
                assert self._data is not None, "user data goes as a text body"
                server["user_data"][key] = self._data
                org.user_data_written.append(self._data)
                for hook in org.on_user_data:
                    hook(parts[1], key, self._data)
                return {}
            if method == "DELETE":
                if key not in server["user_data"]:
                    raise self._missing(method, path)
                del server["user_data"][key]
                return {}
        raise self._missing(method, path)

    def _rdb(self, method, path, body, params):
        org = self.org
        parts = path.strip("/").split("/")
        if parts == ["instances"] and method == "GET":
            found = [
                i
                for i in org.rdb_instances.values()
                if (not params.get("project_id") or i["project_id"] == params["project_id"])
                and (not params.get("name") or params["name"] in i["name"])
            ]
            return _page(found, "instances", params)
        if parts[0] == "instances" and len(parts) >= 2:
            if parts[1] not in org.rdb_instances:
                raise self._missing(method, path)
            instance = org.rdb_instances[parts[1]]
            if len(parts) == 2 and method == "GET":
                answer = {k: v for k, v in instance.items() if k != "pending_reads"}
                if instance["pending_reads"] > 0:
                    instance["pending_reads"] -= 1
                    if instance["pending_reads"] == 0:
                        instance["status"] = "ready"
                return copy.deepcopy(answer)
            if parts[2:3] == ["users"] and len(parts) == 4 and method == "PATCH":
                if parts[3] not in instance["users"]:
                    raise self._missing(method, path)
                if instance["status"] != "ready":
                    raise ApiError(f"Scaleway rdb {method} {path}", 409, "transient_state")
                instance["users"][parts[3]] = body["password"]
                return {"name": parts[3], "is_admin": True}
            if parts[2:] == ["certificate"] and method == "GET":
                return {
                    "name": "ssl.crt",
                    "content_type": "application/x-pem-file",
                    "content": base64.b64encode(CERTIFICATE_PEM).decode(),
                }
            if parts[2:] == ["snapshots"] and method == "POST":
                org.snapshot_requests.append(dict(body))
                snapshot = {
                    "id": org._id(),
                    "name": body["name"],
                    "instance_id": instance["id"],
                    "status": "creating",
                    "expires_at": body.get("expires_at") or org.snapshot_default_expires_at,
                    "reads": 0,
                }
                org.snapshots[snapshot["id"]] = snapshot
                return {k: v for k, v in snapshot.items() if k != "reads"}
        if parts[0] == "snapshots" and len(parts) == 2 and method == "GET":
            snapshot = org.snapshots.get(parts[1])
            if snapshot is None:
                raise self._missing(method, path)
            snapshot["reads"] += 1
            if snapshot["reads"] > org.snapshot_reads_creating:
                snapshot["status"] = org.snapshot_final_status
            return {k: v for k, v in snapshot.items() if k != "reads"}
        if parts[0] == "endpoints" and len(parts) == 2 and method == "DELETE":
            for instance in org.rdb_instances.values():
                kept = [e for e in instance["endpoints"] if e["id"] != parts[1]]
                if len(kept) != len(instance["endpoints"]):
                    instance["endpoints"] = kept
                    if org.configure_reads:
                        instance["status"] = "configuring"
                        instance["pending_reads"] = org.configure_reads
                    return {}
            raise self._missing(method, path)
        raise self._missing(method, path)


class FakeObjectStorage:
    """Buckets as boto3 sees them; every client records the access key it signs with."""

    def __init__(self, clock: Clock | None = None):
        self.clock = clock or Clock()
        self.buckets: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        # Callables hearing (bucket, key, body) after each PutObject.
        self.on_put: list[Callable[[str, str, bytes], None]] = []
        self.log: list[tuple[str, ...]] = []
        # Buckets whose policy names someone else: HeadBucket and every setting answer 403
        # until a PutBucketPolicy succeeds, and those in `policy_denied` refuse that too.
        self.forbidden_heads: set[str] = set()
        self.policy_denied: set[str] = set()

    def factory(self, region: str, access_key: str, secret_key: str, project_id: str):
        return _FakeS3(self, f"{access_key}@{project_id}")

    def named(self, operation: str) -> list[tuple[str, str, dict]]:
        return [c for c in self.calls if c[0] == operation]

    def create(self, bucket: str) -> None:
        self.buckets.setdefault(bucket, {"objects": {}, "owner": "test"})

    def put(self, bucket: str, key: str, body: bytes, modified: datetime | None = None) -> None:
        """Write an object as a VM would, outside any recorded client call."""
        store = self.buckets[bucket]
        store["objects"][key] = body
        store.setdefault("modified", {})[key] = modified or self.clock()

    def body(self, bucket: str, key: str) -> bytes | None:
        return self.buckets.get(bucket, {}).get("objects", {}).get(key)

    def json(self, bucket: str, key: str) -> Any:
        raw = self.body(bucket, key)
        return None if raw is None else json.loads(raw)


def _client_error(code: str, status: int, operation: str):
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": "no"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


class _FakeS3:
    def __init__(self, store: FakeObjectStorage, signer: str):
        self.store = store
        self.signer = signer

    def _record(self, operation: str, **kwargs) -> None:
        self.store.calls.append((operation, self.signer, kwargs))
        self.store.log.append(("s3", operation, kwargs.get("Bucket", ""), kwargs.get("Key", "")))

    def head_bucket(self, Bucket):
        self._record("head_bucket", Bucket=Bucket)
        if Bucket in self.store.forbidden_heads:
            raise _client_error("403", 403, "HeadBucket")
        if Bucket not in self.store.buckets:
            raise _client_error("404", 404, "HeadBucket")
        return {}

    def create_bucket(self, Bucket, **kwargs):
        self._record("create_bucket", Bucket=Bucket, **kwargs)
        self.store.buckets[Bucket] = {"objects": {}, "owner": self.signer}
        return {}

    def put_bucket_policy(self, Bucket, Policy):
        self._record("put_bucket_policy", Bucket=Bucket, Policy=Policy)
        if Bucket in self.store.policy_denied:
            raise _client_error("AccessDenied", 403, "PutBucketPolicy")
        self.store.forbidden_heads.discard(Bucket)
        self.store.buckets[Bucket]["policy"] = Policy
        return {}

    def put_bucket_versioning(self, Bucket, VersioningConfiguration):
        self._record("put_bucket_versioning", Bucket=Bucket, Config=VersioningConfiguration)
        self.store.buckets[Bucket]["versioning"] = VersioningConfiguration
        return {}

    def put_bucket_lifecycle_configuration(self, Bucket, LifecycleConfiguration):
        self._record("put_bucket_lifecycle_configuration", Bucket=Bucket)
        self.store.buckets[Bucket]["lifecycle"] = LifecycleConfiguration
        return {}

    def head_object(self, Bucket, Key):
        self._record("head_object", Bucket=Bucket, Key=Key)
        bucket = self.store.buckets.get(Bucket)
        if bucket is None:
            raise _client_error("NoSuchBucket", 404, "HeadObject")
        if Key not in bucket["objects"]:
            raise _client_error("404", 404, "HeadObject")
        modified = bucket.get("modified", {}).get(Key, START - timedelta(days=1))
        return {"LastModified": modified, "ContentLength": len(bucket["objects"][Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        data = Body if isinstance(Body, bytes) else Body.read()
        self._record("put_object", Bucket=Bucket, Key=Key, ContentType=ContentType)
        bucket = self.store.buckets.get(Bucket)
        if bucket is None:
            raise _client_error("NoSuchBucket", 404, "PutObject")
        bucket["objects"][Key] = data
        bucket.setdefault("modified", {})[Key] = self.store.clock()
        for hook in self.store.on_put:
            hook(Bucket, Key, data)
        return {}

    def get_object(self, Bucket, Key):
        self._record("get_object", Bucket=Bucket, Key=Key)
        bucket = self.store.buckets.get(Bucket)
        if bucket is None:
            raise _client_error("NoSuchBucket", 404, "GetObject")
        if Key not in bucket["objects"]:
            raise _client_error("NoSuchKey", 404, "GetObject")
        return {"Body": io.BytesIO(bucket["objects"][Key])}

    def delete_object(self, Bucket, Key):
        self._record("delete_object", Bucket=Bucket, Key=Key)
        bucket = self.store.buckets.get(Bucket)
        if bucket is None:
            raise _client_error("NoSuchBucket", 404, "DeleteObject")
        bucket["objects"].pop(Key, None)
        return {}


def install(monkeypatch: pytest.MonkeyPatch, home) -> tuple[FakeScaleway, FakeObjectStorage, Clock]:
    """Patch the REST client, the S3 factory and the clock; the operator's key in the env."""
    clock = Clock()
    org = FakeScaleway(clock)
    storage = FakeObjectStorage(clock)
    storage.log = org.log
    monkeypatch.setattr(clients, "client", org.client)
    monkeypatch.setattr(clients, "object_storage", storage.factory)
    monkeypatch.setattr(bootstrap_mod, "now", clock)
    monkeypatch.setattr(rdb_mod, "sleep", org.rdb_time.sleep)
    monkeypatch.setattr(rdb_mod, "clock", org.rdb_time.clock)
    monkeypatch.setenv("SCW_ACCESS_KEY", OPERATOR_ACCESS)
    monkeypatch.setenv("SCW_SECRET_KEY", OPERATOR_SECRET)
    monkeypatch.setenv("SCW_DEFAULT_ORGANIZATION_ID", ORGANIZATION)
    monkeypatch.setenv("SCW_CONFIG_PATH", str(home / "no-scw-config.yaml"))
    return org, storage, clock


def expiry_of(org: FakeScaleway, access: str) -> datetime:
    return _parse(org.keys[access]["expires_at"])


# --- a bootstrapped, deployed Scaleway environment -------------------------------------------


REGION = "fr-par"
RESOURCES = "11111111-0000-4000-8000-000000000001"
STATE_PROJECT = "22222222-0000-4000-8000-000000000002"
APPS = {
    "app_deploy": "33333333-0000-4000-8000-000000000003",
    "app_plan": "44444444-0000-4000-8000-000000000004",
    "app_platform": "55555555-0000-4000-8000-000000000005",
    "app_mcp": "66666666-0000-4000-8000-000000000006",
    "app_main": "77777777-0000-4000-8000-000000000007",
}
SERVERS = {
    "platform": "aaaaaaaa-0000-4000-8000-00000000000a",
    "mcp": "bbbbbbbb-0000-4000-8000-00000000000b",
    "main": "cccccccc-0000-4000-8000-00000000000c",
}
DEPLOY_ACCESS = "SCWDEPLOY00000000001"
DEPLOY_SECRET = "0eadbeef-0000-4000-8000-0000000000dd"
HOST_KEYS = {
    "platform": ("SCWPLATFORM000000001", "0eadbeef-0000-4000-8000-00000000a1a1"),
    "mcp": ("SCWMCP00000000000001", "0eadbeef-0000-4000-8000-00000000b2b2"),
    "main": ("SCWMAIN0000000000001", "0eadbeef-0000-4000-8000-00000000c3c3"),
}
MASTER_USER = "vecten_admin"
MASTER_PASSWORD = "scaleway-master-password-0001"
BOOTED = START - timedelta(hours=1)


def artifacts_bucket(env: str = "prod") -> str:
    from vecten.deploy.names import scaleway_bucket_name

    return scaleway_bucket_name("acme", env, "artifacts", RESOURCES)


def state_bucket(env: str = "prod") -> str:
    from vecten.deploy.names import scaleway_bucket_name

    return scaleway_bucket_name("acme", env, "state", STATE_PROJECT)


def scaleway_outputs(
    env: str = "prod", domain: str = "data.acme.com", minimal: bool = False
) -> dict:
    if minimal:
        hosts = {
            "main": {
                "instance_id": SERVERS["main"],
                "public_ip": "51.158.0.12",
                "components": ["platform", "mcp"],
                "hostnames": [f"airflow.{domain}", f"mcp.{domain}"],
            }
        }
        postgres = {
            "state": {
                "host": "172.16.0.4",
                "port": 5432,
                "databases": ["airflow", "mcp_state", "warehouse"],
                "master_secret": "state-secret-id",
            }
        }
        setup = "main"
    else:
        hosts = {
            "platform": {
                "instance_id": SERVERS["platform"],
                "public_ip": "51.158.0.10",
                "components": ["platform"],
                "hostnames": [f"airflow.{domain}"],
            },
            "mcp": {
                "instance_id": SERVERS["mcp"],
                "public_ip": "51.158.0.11",
                "components": ["mcp"],
                "hostnames": [f"mcp.{domain}"],
            },
        }
        postgres = {
            "state": {
                "host": "172.16.0.4",
                "port": 5432,
                "databases": ["airflow", "mcp_state"],
                "master_secret": "state-secret-id",
            },
            "warehouse": {
                "host": "172.16.0.5",
                "port": 5432,
                "databases": ["warehouse"],
                "master_secret": "warehouse-secret-id",
            },
        }
        setup = "mcp"
    return {
        "environment": env,
        "region": REGION,
        "hosts": hosts,
        "setup_host": setup,
        "bucket": artifacts_bucket(env),
        "secret_prefix": f"/vecten/acme/{env}/",
        "postgres": postgres,
    }


def cloud_ids(hosts) -> dict[str, str]:
    ids = {"project": RESOURCES, "state_project": STATE_PROJECT, "dns_project": DNS_PROJECT}
    ids.update({k: v for k, v in APPS.items() if k in ("app_deploy", "app_plan")})
    ids.update({f"app_{h}": APPS[f"app_{h}"] for h in hosts})
    return ids


def prepare_environment(
    root,
    org: FakeScaleway,
    storage: FakeObjectStorage,
    outputs: dict,
    *,
    env: str = "prod",
    deployed_commit: str | None = None,
    stored: bool = True,
    public: bool = False,
) -> dict:
    """Facts, buckets, VM keys, servers, RDB instances and master secrets of a bootstrapped
    environment; returns `outputs` with the master secrets' real IDs."""
    from vecten.deploy.names import name_prefix, rds_identifier
    from vecten_shared.config import load_config, save_config

    config = load_config(root)
    facts = config.environments[env]
    facts.bootstrapped = True
    facts.state_bucket = state_bucket(env)
    facts.cloud_ids = cloud_ids(outputs["hosts"])
    facts.deployed_commit = deployed_commit
    save_config(root, config)
    storage.create(state_bucket(env))
    storage.create(outputs["bucket"])
    for host, target in outputs["hosts"].items():
        access, secret = HOST_KEYS[host]
        org.add_secret(
            RESOURCES,
            f"/vecten/acme/{env}/deploy",
            f"HOST_KEY_{host.upper()}",
            f"{access}:{secret}",
        )
        org.add_server(
            target["instance_id"],
            BOOTED,
            name=f"{name_prefix('acme', env)}-{host}",
            project=RESOURCES,
            tags=("vecten", "vecten:project=acme", f"vecten:env={env}", f"vecten:host={host}"),
        )
    credential = json.dumps({"username": MASTER_USER, "password": MASTER_PASSWORD})
    for key, instance in outputs["postgres"].items():
        instance["master_secret"] = org.add_secret(
            RESOURCES,
            f"/vecten/acme/{env}/setup",
            f"SETUP_{key.upper()}_ADMIN_PASSWORD",
            credential,
        )
        org.add_rdb(rds_identifier("acme", env, key), RESOURCES, public=public)
    if stored:
        storage.put(state_bucket(env), f"{env}/outputs.json", json.dumps(outputs).encode())
    return outputs


def actions_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deploy key in the job's environment, as CI holds it: no organization."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("SCW_ACCESS_KEY", DEPLOY_ACCESS)
    monkeypatch.setenv("SCW_SECRET_KEY", DEPLOY_SECRET)
    monkeypatch.delenv("SCW_DEFAULT_ORGANIZATION_ID", raising=False)


class FakeWatcher:
    """Every VM's watcher: a pointer gets its status, a placed key its acknowledgement.

    `results` maps (host, commit) to (ok, line) or (ok, line, running); hosts in `silent` never
    answer a pointer and hosts in `no_ack` never acknowledge a key. `running` holds the commit
    each host runs: a ship that succeeds moves it, a failed one leaves it where the agent's own
    rollback put it, unless the result names another. `logs` maps (host, commit) to the
    status's `log` value; without an entry the status has no `log`, as an older agent's.
    """

    def __init__(self, org: FakeScaleway, storage: FakeObjectStorage, outputs: dict):
        self.storage = storage
        self.outputs = outputs
        # None as a result: that pointer is never answered.
        self.results: dict[tuple[str, str], tuple[bool, str] | None] = {}
        self.logs: dict[tuple[str, str], object] = {}
        # (host, commit) to the status's `volumes`, the orphaned volumes the agent named.
        self.volumes: dict[tuple[str, str], object] = {}
        self.silent: set[str] = set()
        self.no_ack: set[str] = set()
        self.pointers: list[tuple[str, dict]] = []
        self.running: dict[str, str] = {}
        org.on_user_data.append(self._key)
        storage.on_put.append(self._pointer)

    def _first(self, host: str) -> str:
        return self.outputs["hosts"][host]["components"][0]

    def _pointer(self, bucket: str, key: str, body: bytes) -> None:
        if bucket != self.outputs["bucket"] or not key.endswith("/pointer.json"):
            return
        host = key.split("/")[1]
        doc = json.loads(body)
        self.pointers.append((host, doc))
        if host in self.silent:
            return
        commit = doc["commit"]
        found = self.results.get((host, commit), (True, f"shipped {commit}"))
        if found is None:
            return
        ok, line = found[0], found[1]
        if len(found) > 2:
            self.running[host] = found[2]
        elif ok:
            self.running[host] = commit
        status = {
            "ok": ok,
            "line": line,
            "requested_at": doc["requested_at"],
            "running": self.running.get(host),
        }
        if (host, commit) in self.logs:
            status["log"] = self.logs[(host, commit)]
        if (host, commit) in self.volumes:
            status["volumes"] = self.volumes[(host, commit)]
        status_key = f"{self._first(host)}/ship-status/{commit}.json"
        self.storage.put(bucket, status_key, json.dumps(status).encode())

    def _key(self, server_id: str, key: str, value: str) -> None:
        host = next(h for h, t in self.outputs["hosts"].items() if t["instance_id"] == server_id)
        if key != "vecten-key" or host in self.no_ack:
            return
        digest = hashlib.sha256(value.split(":", 1)[0].encode()).hexdigest()
        ack = f"{self._first(host)}/key-ack/{digest}.json"
        self.storage.put(self.outputs["bucket"], ack, json.dumps({"host": host}).encode())


class FakeTime:
    """A monotonic clock that only `sleep` moves."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
