"""Shipping to Scaleway hosts by pull, and handing each host its key.

Scaleway has no run-command API, so CI never reaches into a VM. It uploads
`bundles/<host>/<commit>.tar.gz` to the artifacts bucket and rewrites
`bundles/<host>/pointer.json`; the host's `vecten-watch.timer` reads the pointer, ships the
bundle with the release's own agent and writes `<first component>/ship-status/<commit>.json`
(`{ok, line, requested_at, running}`, `running` naming the commit the host runs once the agent
is done, after any rollback of its own). A status counts only when its `requested_at` is the one
CI wrote, since an earlier ship of the same commit left a status of its own.

The pointer names the host's last good release between ships, because the next ship rolls back
to whatever it names: a rejected commit is replaced by the one the host reports running, and a
rejected first ship leaves no pointer at all.

A host's key reaches it once, through its instance metadata: `deploy` sets the user data entry
`vecten-key`, waits for the watcher's `<first component>/key-ack/<sha256(access key)>.json`,
then deletes the entry, because the provider reads every user data entry into Terraform state
on refresh. Instances API paths, from the Scaleway Go SDK
(`scaleway-sdk-go/api/instance/v1/server_utils.go`), which the provider uses:

- `GET /instance/v1/zones/{zone}/servers/{server_id}` (its `creation_date`);
- `GET /instance/v1/zones/{zone}/servers/{server_id}/user_data`, which lists the entries'
  keys and not their values (`ListServerUserData`);
- `PATCH /instance/v1/zones/{zone}/servers/{server_id}/user_data/{key}`, the value as a
  `text/plain` body;
- `DELETE /instance/v1/zones/{zone}/servers/{server_id}/user_data/{key}`.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from vecten import git_extensions
from vecten.deploy import shipping
from vecten.deploy.bundle import (
    build_host_bundle,
    prepare_bundle_metadata,
)
from vecten.deploy.names import name_prefix
from vecten.deploy.scaleway import clients
from vecten.deploy.scaleway.bootstrap import (
    HOST_KEY_SCOPE,
    ack_key,
    find_secret,
    host_key_name,
    read_latest,
)
from vecten.deploy.scaleway.clients import ApiError, s3_errors, s3_failure, s3_missing
from vecten.deploy.shipping import HEALTH, bundle_key, component_url, ship_order
from vecten_shared.config import VectenConfig
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.environment import EnvironmentOutputs
from vecten_shared.provider.scaleway import scaleway_secret_path

if TYPE_CHECKING:
    from vecten.deploy.scaleway import ScalewayProvider

FIRST_SHIP_WINDOW = 900
SHIP_WINDOW = 600
STATUS_POLL_SECONDS = 10
KEY_ACK_WINDOW = 900
KEY_POLL_SECONDS = 10
KEY_ENTRY = "vecten-key"
SERVER_PAGE = 100
REPORTED_LIMIT = 300
REDACTED = "***"
# The clause the agent appends to its line once it has put the previous release back.
AGENT_ROLLED_BACK = "; rolled back to "
_COMMIT = re.compile(r"[0-9a-f]{40}")
_UNPRINTABLE = re.compile(r"[^\x20-\x7e]")

sleep = time.sleep
clock = time.monotonic
poll = shipping.poll


def now() -> datetime:
    return datetime.now(UTC)


def zone_of(region: str) -> str:
    return f"{region}-1"


def pointer_key(host: str) -> str:
    return f"bundles/{host}/pointer.json"


def status_key(component: str, commit: str) -> str:
    return f"{component}/ship-status/{commit}.json"


# --- objects ------------------------------------------------------------------------------


def _read_json(s3: Any, bucket: str, key: str) -> Any:
    """The object's JSON, None when it is absent or holds no JSON."""
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except s3_errors() as exc:
        if s3_missing(exc):
            return None
        raise s3_failure(f"GetObject of {key}", exc) from None
    try:
        return json.loads(body)
    except ValueError, UnicodeDecodeError:
        return None


def read_pointer(s3: Any, bucket: str, host: str) -> dict[str, Any] | None:
    doc = _read_json(s3, bucket, pointer_key(host))
    if not isinstance(doc, dict) or not _COMMIT.fullmatch(str(doc.get("commit", ""))):
        return None
    return doc


def write_pointer(s3: Any, bucket: str, host: str, commit: str, *, force: bool) -> str:
    """Point `host` at `commit` and return the pointer's `requested_at`.

    `force` makes the watcher ship a commit it already runs, recreating its containers.
    """
    requested = now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _put_pointer(s3, bucket, host, {"commit": commit, "requested_at": requested, "force": force})
    return requested


def _put_pointer(s3: Any, bucket: str, host: str, doc: dict[str, Any]) -> None:
    try:
        s3.put_object(
            Bucket=bucket,
            Key=pointer_key(host),
            Body=json.dumps(doc).encode("utf-8"),
            ContentType="application/json",
        )
    except s3_errors() as exc:
        raise s3_failure(f"PutObject of {pointer_key(host)}", exc) from None


def delete_pointer(s3: Any, bucket: str, host: str) -> None:
    try:
        s3.delete_object(Bucket=bucket, Key=pointer_key(host))
    except s3_errors() as exc:
        raise s3_failure(f"DeleteObject of {pointer_key(host)}", exc) from None


def restore_pointer(
    s3: Any, bucket: str, host: str, previous: dict[str, Any] | None, *, new_server: bool
) -> None:
    """Point `host` back at the commit a ship replaced, so the pointer names the last good
    release.

    The pointer gets a new request, because the watcher records a request as handled only once
    its status is written: after a lost status it still holds the replaced pointer as handled.
    With a new one it ships nothing when it runs that commit and ships it back otherwise. A
    server that may be new gets the replaced pointer unchanged, which its watcher skips as
    meant for the server it replaced.
    """
    if previous is None:
        delete_pointer(s3, bucket, host)
    elif new_server:
        _put_pointer(s3, bucket, host, previous)
    else:
        write_pointer(s3, bucket, host, str(previous["commit"]), force=False)


def last_good(status: dict[str, Any], rejected: str, previous: dict[str, Any] | None) -> str | None:
    """The release to point a host back at once `rejected` failed on it: the commit its watcher
    reports running, else the one the pointer named before; never `rejected` itself."""
    running = str(status.get("running") or "")
    if _COMMIT.fullmatch(running) and running != rejected:
        return running
    if previous is not None and previous["commit"] != rejected:
        return str(previous["commit"])
    return None


def upload(s3: Any, bucket: str, host: str, commit: str, bundle: Path) -> None:
    key = bundle_key(host, commit)
    try:
        with bundle.open("rb") as handle:
            s3.put_object(Bucket=bucket, Key=key, Body=handle)
    except s3_errors() as exc:
        raise s3_failure(f"PutObject of {key}", exc) from None


def wait_status(
    s3: Any, bucket: str, component: str, commit: str, requested: str, window: int
) -> dict[str, Any] | None:
    """The status the watcher wrote for the pointer `requested`; None after `window` s."""
    deadline = clock() + window
    while True:
        doc = _read_json(s3, bucket, status_key(component, commit))
        if (
            isinstance(doc, dict)
            and doc.get("requested_at") == requested
            and isinstance(doc.get("ok"), bool)
        ):
            return doc
        if clock() >= deadline:
            return None
        sleep(STATUS_POLL_SECONDS)


def reported_line(status: dict[str, Any], known: set[str]) -> str:
    """The watcher's line, printable, without any value read in this run, at most 300
    characters: the VM's output is not trusted."""
    line = _UNPRINTABLE.sub(" ", str(status.get("line") or "")).strip()
    for value in sorted((v for v in known if v), key=len, reverse=True):
        line = line.replace(value, REDACTED)
    return line[:REPORTED_LIMIT]


# --- ship -----------------------------------------------------------------------------------


def ship_hosts(
    plane: ScalewayProvider,
    project_root: Path,
    config: VectenConfig,
    env: str,
    outputs: EnvironmentOutputs,
    commit: str,
    keyed: frozenset[str] = frozenset(),
) -> list[str]:
    """Build and ship one bundle per host, setup host first; stops at the first failure.

    A host in `keyed` was handed its key in this run, so its server may be new and still
    booting: it gets the first-ship window whatever its pointer says.
    """
    metadata = prepare_bundle_metadata(
        project_root,
        config,
        commit,
        with_mcp=any("mcp" in host.components for host in outputs.hosts.values()),
    )
    s3 = plane.storage("project")
    shipped: list[str] = []
    plane.orphaned_volumes = {}
    with tempfile.TemporaryDirectory(prefix="vecten-bundles-") as tmp:
        # Every source is fetched and checked before any host changes.
        sources = git_extensions.write_sources(project_root, config, Path(tmp) / "sources")
        for host in ship_order(outputs):
            bundle = build_host_bundle(
                project_root,
                config,
                env,
                outputs,
                host,
                commit,
                Path(tmp),
                metadata=metadata,
                sources=sources,
            )
            plane.orphaned_volumes[host] = ship_host(
                plane, s3, outputs, host, bundle, commit, first=host in keyed
            )
            shipped.append(host)
    return shipped


def ship_host(
    plane: ScalewayProvider,
    s3: Any,
    outputs: EnvironmentOutputs,
    host: str,
    bundle: Path,
    commit: str,
    *,
    first: bool = False,
) -> list[str]:
    """Upload `bundle`, point `host` at it, wait for its watcher, poll its components and
    point it back at the previous commit when a poll fails; the orphaned service volumes
    the watcher reported.

    A failed agent run is not rolled back from here: the agent restores the previous release
    itself when it fails after its swap, and its line says so. Only the pointer moves back, to
    the last good release, and CI waits for that only when the host does not run it already.
    A ship that times out, or a first ship whose poll fails, leaves the pointer as it found it.
    """
    target = outputs.hosts[host]
    component = target.components[0]
    bucket = outputs.bucket
    previous = read_pointer(s3, bucket, host)
    window = SHIP_WINDOW if previous and not first else FIRST_SHIP_WINDOW
    upload(s3, bucket, host, commit, bundle)
    # Without a pointer the host's release is unknown: a first ship whose poll failed left
    # `commit` running, and only `force` makes the watcher ship it again.
    force = previous is None or previous["commit"] == commit
    requested = write_pointer(s3, bucket, host, commit, force=force)
    status = wait_status(s3, bucket, component, commit, requested, window)
    if status is None:
        restore_pointer(s3, bucket, host, previous, new_server=first)
        if window == FIRST_SHIP_WINDOW:
            # A server with no release skips a pointer stamped before its key acknowledgement,
            # and CI's clock stamps the one while the VM's clock stamps the other.
            causes = (
                "the VM may still be booting, its clock may run ahead of this machine's, or it "
                "may not hold its key yet"
            )
        else:
            causes = "the VM may still be booting or may not hold its key yet"
        if previous is None:
            pointer = "the pointer is removed"
        else:
            pointer = (
                f"the pointer is back on {previous['commit']}, which a watcher still shipping "
                f"{commit} ships after it"
            )
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"host {host}: the watcher did not pick up bundle {commit} within {window} s; "
            f"{causes}, which `vecten deploy --env {plane.env}` places; {pointer}",
            details={"host": host, "commit": commit},
        )
    if not status["ok"]:
        line = reported_line(status, plane.known_values())
        logged = status.get("log")
        log = shipping.log_excerpt(logged if isinstance(logged, list) else [], plane.known_values())
        message = f"shipping to host {host} failed"
        if line:
            message += f": {line}"
        rolled_back = AGENT_ROLLED_BACK in line
        good = last_good(status, commit, previous)
        if good is None:
            delete_pointer(s3, bucket, host)
        elif good == status.get("running"):
            write_pointer(s3, bucket, host, good, force=False)
        else:
            # The host does not run `good`, so this pointer makes its watcher ship it.
            rolled_back, outcome = _point_back(plane, s3, bucket, host, component, good, False)
            message += f"; {outcome}"
        raise Failure(
            ExitCode.TOOL_FAILED,
            message + shipping.container_log_message(log),
            details={"host": host, "rolled_back": rolled_back, "container_log": log},
        )
    for name in target.components:
        url = component_url(outputs, name)
        if poll(url, window, healthy=HEALTH[name].healthy):
            continue
        message = f"{name} did not answer healthy at {url} within {window} s; "
        rolled_back = False
        if previous is None:
            delete_pointer(s3, bucket, host)
            message += f"host {host} has no earlier release to roll back to"
        else:
            back = str(previous["commit"])
            # The rollback ships a release the watcher may already run: the one that failed
            # its poll, when this ship re-shipped it.
            rolled_back, outcome = _point_back(
                plane, s3, bucket, host, component, back, back == commit
            )
            message += outcome
        raise Failure(
            ExitCode.TOOL_FAILED,
            message,
            details={"component": name, "host": host, "url": url, "rolled_back": rolled_back},
        )

    volumes = status.get("volumes")
    return shipping.volume_names(volumes) if isinstance(volumes, list) else []


def _point_back(
    plane: ScalewayProvider,
    s3: Any,
    bucket: str,
    host: str,
    component: str,
    back: str,
    force: bool,
) -> tuple[bool, str]:
    """Point `host` at `back`, wait for its watcher, and say whether it is back on it.

    A rollback that fails leaves the pointer on `back`: no release is known to work there.
    """
    requested = write_pointer(s3, bucket, host, back, force=force)
    undone = wait_status(s3, bucket, component, back, requested, SHIP_WINDOW)
    if undone is None:
        return False, (
            f"the watcher on host {host} did not pick up the rollback to {back} "
            f"within {SHIP_WINDOW} s"
        )
    if undone["ok"]:
        return True, f"host {host} is back on {back}"
    line = reported_line(undone, plane.known_values())
    return False, f"the rollback on host {host} failed too: {line}"


# --- keys -----------------------------------------------------------------------------------


def _created(instances: Any, host: str, server_id: str) -> datetime:
    """When the server was created; refuses when Scaleway names no usable time, since the
    acknowledgement check needs it."""
    doc = instances.request("GET", f"/servers/{quote(server_id, safe='')}")
    stamp = (doc.get("server") or {}).get("creation_date")
    try:
        created = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        created = None
    if created is None or created.tzinfo is None:
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"Scaleway gave server {server_id} (host {host}) no creation date with a time zone, "
            "so it is unknown whether that server holds its key; nothing was placed",
            details={"host": host, "server": server_id},
        )
    return created


def _acked_since(s3: Any, bucket: str, key: str, created: datetime) -> bool:
    """Whether the acknowledgement `key` exists and was written after the server booted.

    An older one came from a server this one replaced, which held the same key.
    """
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except s3_errors() as exc:
        if s3_missing(exc):
            return False
        raise s3_failure("HeadObject of a key acknowledgement", exc) from None
    written = head.get("LastModified")
    return isinstance(written, datetime) and written >= created


def _await_acks(s3: Any, bucket: str, waiting: dict[str, tuple[str, datetime]]) -> list[str]:
    """The hosts among `waiting` that wrote no acknowledgement within the window."""
    deadline = clock() + KEY_ACK_WINDOW
    left = dict(waiting)
    while True:
        for host, (key, created) in list(left.items()):
            if _acked_since(s3, bucket, key, created):
                del left[host]
        if not left or clock() >= deadline:
            return sorted(left)
        sleep(KEY_POLL_SECONDS)


def _remove_entries(instances: Any, servers: dict[str, str]) -> list[str]:
    """Delete `vecten-key` from each host's user data; returns the hosts where it may stay."""
    stuck: list[str] = []
    for host, server_id in servers.items():
        try:
            instances.request(
                "DELETE", f"/servers/{quote(server_id, safe='')}/user_data/{KEY_ENTRY}"
            )
        except ApiError as error:
            if error.status != 404:
                stuck.append(f"{host} ({error.message.rsplit(': ', 1)[-1]})")
        except Failure as failure:
            stuck.append(f"{host} ({failure.message})")
    return stuck


def _stuck_failure(stuck: list[str]) -> Failure:
    return Failure(
        ExitCode.TOOL_FAILED,
        f"the {KEY_ENTRY} user data entry may remain on {', '.join(stuck)}; delete it "
        "before the next Terraform run, which would copy it into state",
        details={"hosts": [s.split(" ", 1)[0] for s in stuck]},
    )


def environment_servers(plane: ScalewayProvider, instances: Any) -> dict[str, str]:
    """The environment's servers by name: those in its resources project that carry its
    `vecten:env=<env>` tag and the `<name prefix>-` the module names them with.

    `GET /instance/v1/zones/{zone}/servers?project=&tags=&page=&per_page=` lists them
    (Scaleway Go SDK `ListServers`); `tags` matches servers carrying every tag named.
    """
    prefix = f"{name_prefix(plane.config().project.name, plane.env)}-"
    tag = f"vecten:env={plane.env}"
    params: dict[str, Any] = {"project": plane.cloud_id("project"), "tags": tag}
    found: dict[str, str] = {}
    page = 1
    while True:
        doc = instances.request(
            "GET", "/servers", params={**params, "page": page, "per_page": SERVER_PAGE}
        )
        batch = [s for s in doc.get("servers") or [] if isinstance(s, dict)]
        for server in batch:
            name, sid = str(server.get("name") or ""), server.get("id")
            if sid and name.startswith(prefix) and tag in (server.get("tags") or []):
                found[name] = str(sid)
        total = doc.get("total_count")
        seen = (page - 1) * SERVER_PAGE + len(batch)
        if len(batch) < SERVER_PAGE or (isinstance(total, int) and seen >= total):
            return found
        page += 1


def clear_leftover_keys(plane: ScalewayProvider) -> list[str]:
    """Delete `vecten-key` from every server of the environment, a missing entry counting
    as clean; returns the servers asked. A first deploy finds no server and asks none."""
    instances = clients.client("instance", zone_of(plane.region()))
    servers = environment_servers(plane, instances)
    stuck = _remove_entries(instances, servers)
    if stuck:
        raise _stuck_failure(stuck)
    return sorted(servers)


def servers_holding_keys(plane: ScalewayProvider) -> list[str]:
    """The names of the environment's servers whose user data lists `vecten-key`; reads the
    entries' keys only and changes nothing. A server gone by the time it is asked counts as
    clean, and a first deploy finds no server."""
    instances = clients.client("instance", zone_of(plane.region()))
    holding: list[str] = []
    for name, server_id in sorted(environment_servers(plane, instances).items()):
        try:
            doc = instances.request("GET", f"/servers/{quote(server_id, safe='')}/user_data")
        except ApiError as error:
            if error.status == 404:
                continue
            raise
        if KEY_ENTRY in (doc.get("user_data") or []):
            holding.append(name)
    return holding


def place_vm_keys(plane: ScalewayProvider, outputs: EnvironmentOutputs) -> list[str]:
    """Give each host whose server has not acknowledged its current key that key, through
    the `vecten-key` user data entry, and take the entry away again; returns the hosts given
    one, setup host first.

    Every host's key is read before the first is placed, so a missing one refuses with
    nothing placed. Every host's entry is deleted at the end, whatever happened, so none is
    left for a later refresh to copy into Terraform state.
    """
    env = plane.env
    project_id = plane.cloud_id("project")
    secrets = clients.client("secret-manager", outputs.region)
    instances = clients.client("instance", zone_of(outputs.region))
    s3 = plane.storage("project")
    path = scaleway_secret_path(plane.config().project.name, env, HOST_KEY_SCOPE)
    order = ship_order(outputs)
    keys: dict[str, str] = {}
    for host in order:
        name = host_key_name(host)
        sid = find_secret(secrets, project_id, path, name)
        value = read_latest(secrets, sid) if sid else None
        if not value or ":" not in value:
            raise Failure(
                ExitCode.PREREQUISITE,
                f"host {host} has no key at {path}/{name}; run `vecten bootstrap --env {env}`",
            )
        plane.remember(value)
        keys[host] = value
    servers = {host: outputs.hosts[host].instance_id for host in order}
    waiting: dict[str, tuple[str, datetime]] = {}
    placed: list[str] = []
    missing: list[str] = []
    failed: BaseException | None = None
    try:
        for host in order:
            created = _created(instances, host, servers[host])
            access = keys[host].split(":", 1)[0]
            ack = ack_key(outputs.hosts[host].components[0], access)
            if not _acked_since(s3, outputs.bucket, ack, created):
                waiting[host] = (ack, created)
        for host in waiting:
            instances.request(
                "PATCH",
                f"/servers/{quote(servers[host], safe='')}/user_data/{KEY_ENTRY}",
                data=keys[host],
            )
            placed.append(host)
        if waiting:
            missing = _await_acks(s3, outputs.bucket, waiting)
    except BaseException as exc:
        failed = exc
    stuck = _remove_entries(instances, servers)
    if stuck:
        raise _stuck_failure(stuck) from failed
    if failed is not None:
        raise failed
    if missing:
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"host {', '.join(missing)} did not acknowledge its Scaleway key within "
            f"{KEY_ACK_WINDOW} s; the key was taken back out of its metadata. Check that the VM "
            f"runs and its vecten-watch.timer is active, then run `vecten deploy --env {env}` "
            "again",
            details={"hosts": missing},
        )
    return placed
