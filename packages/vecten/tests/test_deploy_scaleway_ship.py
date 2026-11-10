"""The Scaleway control plane after bootstrap: Terraform's settings, the outputs, VM keys in
instance metadata, the public database endpoint, the pull-model ship and final snapshots."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from vecten.deploy.control import ControlPlane, get_control_plane
from vecten.deploy.names import rds_identifier, snapshot_name
from vecten.deploy.scaleway import ScalewayProvider
from vecten.deploy.scaleway import rdb as rdb_mod
from vecten.deploy.scaleway import ship as ship_mod
from vecten.deploy.scaleway import snapshots as snapshots_mod
from vecten.deploy.tfvars import env_tfvars_path
from vecten_shared.config import load_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import scope_for
from vecten_shared.provider.environment import EnvironmentOutputs
from vecten_shared.provider.scaleway import scaleway_secret_path

from .deploy_fixtures import COMMIT, render_aws, set_facts, write_outputs
from .scaleway_fakes import (
    APPS,
    BOOTED,
    CERTIFICATE_PEM,
    DEPLOY_ACCESS,
    DEPLOY_SECRET,
    DNS_PROJECT,
    HOST_KEYS,
    MASTER_PASSWORD,
    MASTER_USER,
    RESOURCES,
    SERVERS,
    START,
    STATE_PROJECT,
    FakeTime,
    FakeWatcher,
    actions_key,
    install,
    prepare_environment,
    scaleway_outputs,
    state_bucket,
)

OLD = "fedcba9876543210fedcba9876543210fedcba98"
AIRFLOW_URL = "https://airflow.data.acme.com/api/v2/monitor/health"
MCP_URL = "https://mcp.data.acme.com/health"


class Polls:
    def __init__(self):
        self.failing: set[str] = set()
        self.calls: list[tuple[str, int]] = []

    def __call__(self, url, timeout, interval=5, **kwargs):
        self.calls.append((url, timeout))
        return url not in self.failing


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A bootstrapped, deployed standard environment inside Actions with the deploy key."""
    root = render_aws(tmp_path / "proj", cloud="scaleway")
    monkeypatch.chdir(root)
    org, storage, clock = install(monkeypatch, tmp_path)
    actions_key(monkeypatch)
    outputs = prepare_environment(root, org, storage, scaleway_outputs(), deployed_commit=COMMIT)
    write_outputs(root, outputs)
    watcher = FakeWatcher(org, storage, outputs)
    time = FakeTime()
    monkeypatch.setattr(ship_mod, "sleep", time.sleep)
    monkeypatch.setattr(ship_mod, "clock", time.clock)
    monkeypatch.setattr(snapshots_mod, "sleep", time.sleep)
    polls = Polls()
    monkeypatch.setattr(ship_mod, "poll", polls)
    return SimpleNamespace(
        root=root,
        org=org,
        storage=storage,
        clock=clock,
        outputs=outputs,
        bucket=outputs["bucket"],
        watcher=watcher,
        time=time,
        polls=polls,
        plane=ScalewayProvider(root, "prod"),
    )


def _ship(env, commit: str = COMMIT) -> list[str]:
    outputs = EnvironmentOutputs.model_validate(env.outputs)
    return env.plane.ship_hosts(env.root, load_config(env.root), "prod", outputs, commit)


def _shipped_before(env, host: str, commit: str = OLD) -> None:
    pointer = {"commit": commit, "requested_at": "2026-10-01T00:00:00.000000Z"}
    env.storage.put(env.bucket, f"bundles/{host}/pointer.json", json.dumps(pointer).encode())
    env.storage.put(env.bucket, f"bundles/{host}/{commit}.tar.gz", b"previous bundle")
    env.watcher.running[host] = commit


def _puts(env, suffix: str) -> list[str]:
    return [c[2]["Key"] for c in env.storage.named("put_object") if c[2]["Key"].endswith(suffix)]


# --- the control plane --------------------------------------------------------------------


def test_scaleway_gets_its_control_plane_for_every_command(tmp_path):
    plane = get_control_plane("scaleway", tmp_path, "prod")
    assert isinstance(plane, ScalewayProvider)
    assert isinstance(plane, ControlPlane)


def test_the_protocol_asks_no_region():
    assert "region" not in ControlPlane.__dict__


def test_terraform_gets_the_state_bucket_and_the_key_for_backend_and_provider(env):
    plane = env.plane
    (root,) = plane.terraform_roots("prod")
    assert root.name == "scaleway"
    assert root.key == "prod/scaleway.tfstate"
    assert plane.backend_config("prod", root) == {
        "bucket": state_bucket(),
        "key": "prod/scaleway.tfstate",
    }
    # The S3 backend acts in the state project, which the deploy key does not prefer.
    assert plane.terraform_env() == {
        "AWS_ACCESS_KEY_ID": f"{DEPLOY_ACCESS}@{STATE_PROJECT}",
        "AWS_SECRET_ACCESS_KEY": DEPLOY_SECRET,
        "SCW_ACCESS_KEY": DEPLOY_ACCESS,
        "SCW_SECRET_KEY": DEPLOY_SECRET,
    }
    extra = dict(root.extra_vars)
    assert extra["project_id"] == RESOURCES
    assert extra["dns_project_id"] == DNS_PROJECT
    assert json.loads(extra["application_ids"]) == {
        "deploy": APPS["app_deploy"],
        "plan": APPS["app_plan"],
        "platform": APPS["app_platform"],
        "mcp": APPS["app_mcp"],
    }


@pytest.mark.parametrize(
    ("key", "kind"), [("operator_user", "user_id"), ("operator_application", "application_id")]
)
def test_the_cloud_root_gets_the_recorded_operator_only_when_bootstrap_recorded_one(env, key, kind):
    (root,) = ScalewayProvider(env.root, "prod").terraform_roots("prod", cloud_only=True)
    assert "operator_principal" not in dict(root.extra_vars)
    operator = "0bb0bb0b-0000-4000-8000-000000000002"
    ids = {**load_config(env.root).environments["prod"].cloud_ids, key: operator}
    set_facts(env.root, cloud_ids=ids)
    (root,) = ScalewayProvider(env.root, "prod").terraform_roots("prod", cloud_only=True)
    assert dict(root.extra_vars)["operator_principal"] == f"{kind}:{operator}"


def test_the_roots_refuse_an_environment_bootstrap_recorded_no_ids_for(env):
    set_facts(env.root, cloud_ids={})
    with pytest.raises(Failure) as caught:
        ScalewayProvider(env.root, "prod").terraform_roots("prod")
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "vecten bootstrap --env prod" in caught.value.message


def test_outputs_go_to_the_state_bucket_in_the_state_project(env):
    raw = {k: {"value": v, "type": "x", "sensitive": False} for k, v in env.outputs.items()}
    env.storage.buckets[state_bucket()]["objects"].clear()
    env.plane.store_outputs(raw)
    (put,) = env.storage.named("put_object")
    assert put[1] == f"{DEPLOY_ACCESS}@{STATE_PROJECT}"
    assert put[2]["Key"] == "prod/outputs.json"
    assert env.storage.json(state_bucket(), "prod/outputs.json") == env.outputs
    fresh = ScalewayProvider(env.root, "prod")
    assert fresh.stored_outputs().model_dump() == env.outputs
    assert fresh.outputs_location() == f"s3://{state_bucket()}/prod/outputs.json"


def test_no_stored_outputs_reads_as_none(env):
    env.storage.buckets[state_bucket()]["objects"].clear()
    assert env.plane.stored_outputs() is None


def test_forgetting_the_outputs_never_raises(env):
    del env.storage.buckets[state_bucket()]
    left = env.plane.forget_outputs()
    assert len(left) == 1 and state_bucket() in left[0]
    set_facts(env.root, bootstrapped=False, state_bucket=None)
    left = ScalewayProvider(env.root, "prod").forget_outputs()
    assert left and "bootstrap" in left[0]


def test_aws_forgetting_the_outputs_never_raises_either(tmp_path):
    root = render_aws(tmp_path / "aws")
    left = get_control_plane("aws", root, "prod").forget_outputs()
    assert left and "bootstrap" in left[0]


# --- secrets from the laptop ----------------------------------------------------------------


def test_secret_set_creates_a_missing_secret_at_its_scope_path(env):
    env.plane.put_secret("HUBSPOT_API_KEY", "hub-value")
    path = scaleway_secret_path("acme", "prod", scope_for("HUBSPOT_API_KEY"))
    assert env.org.secret_value(path, "HUBSPOT_API_KEY") == "hub-value"
    (secret,) = [s for s in env.org.secrets.values() if s["name"] == "HUBSPOT_API_KEY"]
    assert secret["project_id"] == RESOURCES
    env.plane.put_secret("HUBSPOT_API_KEY", "hub-value-2")
    assert env.org.secret_value(path, "HUBSPOT_API_KEY") == "hub-value-2"
    assert len([s for s in env.org.secrets.values() if s["name"] == "HUBSPOT_API_KEY"]) == 1
    assert env.plane.get_secret("HUBSPOT_API_KEY") == "hub-value-2"
    assert "HUBSPOT_API_KEY" in env.plane.list_secrets()


def test_the_master_credential_comes_from_the_admin_secret(env):
    outputs = EnvironmentOutputs.model_validate(env.outputs)
    assert env.plane.master_credential(outputs.postgres["warehouse"]) == (
        MASTER_USER,
        MASTER_PASSWORD,
    )


# --- VM keys --------------------------------------------------------------------------------


def _user_data_calls(env) -> list[tuple[str, str]]:
    return [(m, p) for a, m, p, _, _ in env.org.calls if a == "instance" and "/user_data/" in p]


def _user_data_calls_any(env) -> list[tuple[str, str]]:
    return [(m, p) for a, m, p, _, _ in env.org.calls if a == "instance" and "/user_data" in p]


def test_keys_are_placed_acknowledged_then_removed(env):
    placed = ship_mod.place_vm_keys(env.plane, EnvironmentOutputs.model_validate(env.outputs))
    assert placed == ["mcp", "platform"]
    patches = [(a, p, body) for a, m, p, body, _ in env.org.calls if m == "PATCH"]
    assert patches == [
        ("instance", f"/servers/{SERVERS['mcp']}/user_data/vecten-key", ":".join(HOST_KEYS["mcp"])),
        (
            "instance",
            f"/servers/{SERVERS['platform']}/user_data/vecten-key",
            ":".join(HOST_KEYS["platform"]),
        ),
    ]
    assert all(not s["user_data"] for s in env.org.servers.values())
    deletes = [p for m, p in _user_data_calls(env) if m == "DELETE"]
    assert sorted(deletes) == sorted(
        f"/servers/{SERVERS[h]}/user_data/vecten-key" for h in ("mcp", "platform")
    )
    # Each key leaves the metadata only after its acknowledgement exists.
    log = env.org.log
    for host in ("mcp", "platform"):
        digest = hashlib.sha256(HOST_KEYS[host][0].encode()).hexdigest()
        ack = next(
            i for i, e in enumerate(log) if e[:2] == ("s3", "head_object") and digest in e[3]
        )
        delete = next(
            i
            for i, e in enumerate(log)
            if e[2] == "DELETE" and SERVERS[host] in e[3] and "user_data" in e[3]
        )
        assert ack < delete


def test_a_key_never_acknowledged_exits_5_naming_the_host_and_is_still_removed(env):
    env.watcher.no_ack.add("platform")
    with pytest.raises(Failure) as caught:
        ship_mod.place_vm_keys(env.plane, EnvironmentOutputs.model_validate(env.outputs))
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert "platform" in caught.value.message and "900" in caught.value.message
    assert "mcp" not in caught.value.message.split(";")[0]
    assert all(not s["user_data"] for s in env.org.servers.values())
    assert env.time.now >= 900
    assert HOST_KEYS["platform"][1] not in caught.value.message


def test_a_key_this_server_already_acknowledged_is_not_placed_again(env):
    digest = hashlib.sha256(HOST_KEYS["platform"][0].encode()).hexdigest()
    env.storage.put(env.bucket, f"platform/key-ack/{digest}.json", b"{}", modified=START)
    placed = ship_mod.place_vm_keys(env.plane, EnvironmentOutputs.model_validate(env.outputs))
    assert placed == ["mcp"]
    assert not [p for m, p in _user_data_calls(env) if m == "PATCH" and SERVERS["platform"] in p]


def test_a_replaced_server_gets_its_key_again_despite_an_older_acknowledgement(env):
    digest = hashlib.sha256(HOST_KEYS["platform"][0].encode()).hexdigest()
    older = BOOTED - timedelta(days=3)
    env.storage.put(env.bucket, f"platform/key-ack/{digest}.json", b"{}", modified=older)
    placed = ship_mod.place_vm_keys(env.plane, EnvironmentOutputs.model_validate(env.outputs))
    assert "platform" in placed


def test_a_missing_host_key_names_bootstrap_before_any_key_is_placed(env):
    (sid,) = [s["id"] for s in env.org.secrets.values() if s["name"] == "HOST_KEY_PLATFORM"]
    del env.org.secrets[sid]
    with pytest.raises(Failure) as caught:
        ship_mod.place_vm_keys(env.plane, EnvironmentOutputs.model_validate(env.outputs))
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "HOST_KEY_PLATFORM" in caught.value.message
    assert "vecten bootstrap --env prod" in caught.value.message
    assert not [p for m, p in _user_data_calls(env) if m == "PATCH"]


def test_an_unusable_creation_date_refuses_naming_the_server_before_any_key_is_placed(env):
    env.org.servers[SERVERS["platform"]]["creation_date"] = "yesterday"
    with pytest.raises(Failure) as caught:
        ship_mod.place_vm_keys(env.plane, EnvironmentOutputs.model_validate(env.outputs))
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert SERVERS["platform"] in caught.value.message
    assert "nothing was placed" in caught.value.message
    assert not [p for m, p in _user_data_calls(env) if m == "PATCH"]
    assert env.time.now == 0


def test_a_creation_date_without_a_time_zone_refuses_too(env):
    env.org.servers[SERVERS["mcp"]]["creation_date"] = "2026-10-18T11:00:00"
    with pytest.raises(Failure) as caught:
        ship_mod.place_vm_keys(env.plane, EnvironmentOutputs.model_validate(env.outputs))
    assert SERVERS["mcp"] in caught.value.message


# --- keys a cancelled deploy left ------------------------------------------------------------


def test_leftover_keys_are_deleted_and_a_missing_entry_counts_as_clean(env):
    env.org.servers[SERVERS["mcp"]]["user_data"]["vecten-key"] = ":".join(HOST_KEYS["mcp"])
    env.plane.before_cloud_apply()
    deleted = sorted(p for m, p in _user_data_calls(env) if m == "DELETE")
    assert deleted == sorted(
        f"/servers/{SERVERS[h]}/user_data/vecten-key" for h in ("mcp", "platform")
    )
    assert all(not s["user_data"] for s in env.org.servers.values())


def test_only_this_environments_servers_are_swept(env):
    env.org.add_server(
        "dddddddd-0000-4000-8000-00000000000d",
        BOOTED,
        name="vecten-acme-staging-platform",
        project=RESOURCES,
        tags=("vecten", "vecten:env=staging"),
    )
    env.org.add_server(
        "eeeeeeee-0000-4000-8000-00000000000e",
        BOOTED,
        name="someone-elses-box",
        project=RESOURCES,
        tags=("vecten:env=prod",),
    )
    asked = ship_mod.clear_leftover_keys(env.plane)
    assert asked == ["vecten-acme-prod-mcp", "vecten-acme-prod-platform"]
    deleted = [p for m, p in _user_data_calls(env) if m == "DELETE"]
    assert not any("dddddddd" in p or "eeeeeeee" in p for p in deleted)


def test_a_first_deploy_finds_no_server_and_deletes_nothing(env):
    env.org.servers.clear()
    assert ship_mod.clear_leftover_keys(env.plane) == []
    assert not [c for c in env.org.calls if c[1] == "DELETE"]


def test_a_leftover_key_that_cannot_be_deleted_stops_the_deploy(env):
    env.org.failures[("instance", "DELETE", f"/servers/{SERVERS['mcp']}")] = (
        403,
        "permissions_denied",
    )
    with pytest.raises(Failure) as caught:
        ship_mod.clear_leftover_keys(env.plane)
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert "vecten-acme-prod-mcp" in caught.value.message
    assert "HTTP 403 permissions_denied" in caught.value.message


def test_a_leftover_key_refuses_a_refreshing_plan_naming_the_host_and_deletes_nothing(env):
    # An interrupted handoff left the entry; a full `--plan-only` must not refresh it in.
    env.org.servers[SERVERS["mcp"]]["user_data"]["vecten-key"] = ":".join(HOST_KEYS["mcp"])
    with pytest.raises(Failure) as caught:
        env.plane.check_refresh_safe()
    failure = caught.value
    assert failure.code == ExitCode.REFUSED
    assert "vecten-acme-prod-mcp" in failure.message
    assert "vecten-acme-prod-platform" not in failure.message
    assert "vecten deploy --env prod" in failure.message
    assert failure.details == {"hosts": ["vecten-acme-prod-mcp"]}
    assert HOST_KEYS["mcp"][1] not in failure.message
    assert env.org.writes() == []
    assert env.org.servers[SERVERS["mcp"]]["user_data"]
    listed = [p for m, p in _user_data_calls_any(env) if m == "GET"]
    assert sorted(listed) == sorted(f"/servers/{SERVERS[h]}/user_data" for h in ("mcp", "platform"))


def test_clean_servers_pass_the_refresh_check(env):
    assert env.plane.check_refresh_safe() is None
    assert env.org.writes() == []


def test_a_first_deploy_has_no_server_and_passes_the_refresh_check(env):
    env.org.servers.clear()
    assert env.plane.check_refresh_safe() is None
    assert env.org.writes() == []


def test_a_server_that_vanished_during_the_check_counts_as_clean(env):
    env.org.failures[("instance", "GET", f"/servers/{SERVERS['platform']}/user_data")] = (
        404,
        "not_found",
    )
    assert env.plane.check_refresh_safe() is None


def test_a_refused_user_data_listing_fails_the_check_with_no_writes(env):
    env.org.failures[("instance", "GET", f"/servers/{SERVERS['mcp']}/user_data")] = (
        403,
        "permissions_denied",
    )
    with pytest.raises(Failure) as caught:
        env.plane.check_refresh_safe()
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert "HTTP 403 permissions_denied" in caught.value.message
    assert env.org.writes() == []


def test_aws_has_nothing_to_check_before_a_refresh(tmp_path):
    plane = get_control_plane("aws", render_aws(tmp_path / "aws"), "prod")
    assert isinstance(plane, ControlPlane)
    assert plane.check_refresh_safe() is None


def test_aws_has_nothing_to_clear_before_its_apply(tmp_path):
    assert (
        get_control_plane("aws", render_aws(tmp_path / "aws"), "prod").before_cloud_apply() is None
    )


# --- the public database endpoint ------------------------------------------------------------


def _public(env) -> dict[str, int]:
    return {
        i["name"]: len([e for e in i["endpoints"] if e["load_balancer"] is not None])
        for i in env.org.rdb_instances.values()
    }


def test_public_endpoints_are_deleted_when_no_cidrs_are_set(env):
    for instance in env.org.rdb_instances.values():
        instance["endpoints"].append(
            {"id": "lb-" + instance["id"], "ip": "51.15.0.9", "port": 20123, "load_balancer": {}}
        )
    env.plane.after_cloud_apply(EnvironmentOutputs.model_validate(env.outputs))
    assert set(_public(env).values()) == {0}
    deleted = [p for a, m, p, _, _ in env.org.calls if a == "rdb" and m == "DELETE"]
    assert len(deleted) == 2 and all(p.startswith("/endpoints/lb-") for p in deleted)
    # Each deletion left its instance configuring; the password waited for it to be ready.
    for instance in env.org.rdb_instances.values():
        assert instance["status"] == "ready"
        assert instance["users"] == {MASTER_USER: MASTER_PASSWORD}
    assert env.org.rdb_time.sleeps == [rdb_mod.READY_POLL_SECONDS] * 2


def test_public_endpoints_stay_when_cidrs_are_set(env):
    path = env_tfvars_path(env.root, "prod")
    path.write_text(path.read_text() + 'database_public_cidrs = ["203.0.113.7/32"]\n')
    for instance in env.org.rdb_instances.values():
        instance["endpoints"].append(
            {"id": "lb-" + instance["id"], "ip": "51.15.0.9", "port": 20123, "load_balancer": {}}
        )
    env.plane.after_cloud_apply(EnvironmentOutputs.model_validate(env.outputs))
    assert set(_public(env).values()) == {1}


# --- the admin password ------------------------------------------------------------------------

PASSWORD_A = "instance-kept-password-a"


def _password_patches(env) -> list[tuple[str, object]]:
    return [(p, body) for a, m, p, body, _ in env.org.calls if a == "rdb" and m == "PATCH"]


def _admin_secret_id(env, key: str) -> str:
    name = f"SETUP_{key.upper()}_ADMIN_PASSWORD"
    (sid,) = [i for i, s in env.org.secrets.items() if s["name"] == name]
    return sid


def test_deploy_sets_each_instance_to_the_admin_password_its_secret_holds(env):
    # A partially failed apply: the instances kept password A, the secrets hold B.
    for instance in env.org.rdb_instances.values():
        instance["users"][MASTER_USER] = PASSWORD_A
    env.plane.after_cloud_apply(EnvironmentOutputs.model_validate(env.outputs))
    for instance in env.org.rdb_instances.values():
        assert instance["users"] == {MASTER_USER: MASTER_PASSWORD}
    expected = {
        f"/instances/{env.org.rdb_named(rds_identifier('acme', 'prod', key))['id']}"
        f"/users/{MASTER_USER}"
        for key in ("state", "warehouse")
    }
    patches = _password_patches(env)
    assert {p for p, _ in patches} == expected
    assert all(body == {"password": MASTER_PASSWORD} for _, body in patches)
    assert MASTER_PASSWORD in env.plane.known_values()


def _instance_calls(env, instance_id: str) -> list[tuple[str, str]]:
    prefix = f"/instances/{instance_id}"
    return [
        (m, p.removeprefix(prefix) or "/")
        for a, m, p, _, _ in env.org.calls
        if a == "rdb" and p.startswith(prefix)
    ]


def test_the_admin_password_waits_until_a_configuring_instance_is_ready(env):
    instance = env.org.rdb_named(rds_identifier("acme", "prod", "warehouse"))
    instance["status"], instance["pending_reads"] = "configuring", 1
    instance["users"][MASTER_USER] = PASSWORD_A
    assert env.plane.reset_admin_passwords(EnvironmentOutputs.model_validate(env.outputs)) is None
    assert instance["users"] == {MASTER_USER: MASTER_PASSWORD}
    # Configuring, then ready, then the PATCH, then a last read before the setup agent connects.
    assert _instance_calls(env, instance["id"]) == [
        ("GET", "/"),
        ("GET", "/"),
        ("PATCH", f"/users/{MASTER_USER}"),
        ("GET", "/"),
    ]
    assert env.org.rdb_time.sleeps == [rdb_mod.READY_POLL_SECONDS]


def test_an_instance_that_never_becomes_ready_times_out_naming_it_and_its_status(env):
    name = rds_identifier("acme", "prod", "state")
    instance = env.org.rdb_named(name)
    instance["status"], instance["pending_reads"] = "configuring", 10**6
    with pytest.raises(Failure) as caught:
        env.plane.after_cloud_apply(EnvironmentOutputs.model_validate(env.outputs))
    failure = caught.value
    assert failure.code == ExitCode.TOOL_FAILED
    assert name in failure.message and "configuring" in failure.message
    assert f"{rdb_mod.READY_WINDOW} s" in failure.message
    assert MASTER_PASSWORD not in failure.message
    assert env.org.rdb_time.now >= rdb_mod.READY_WINDOW
    assert _password_patches(env) == []
    assert not [p for m, p in _user_data_calls(env) if m == "PATCH"]


def test_an_instance_in_error_stops_the_wait_at_once(env):
    name = rds_identifier("acme", "prod", "state")
    env.org.rdb_named(name)["status"] = "error"
    with pytest.raises(Failure) as caught:
        env.plane.after_cloud_apply(EnvironmentOutputs.model_validate(env.outputs))
    assert name in caught.value.message and "error" in caught.value.message
    assert env.org.rdb_time.sleeps == []
    assert _password_patches(env) == []


def test_the_admin_password_is_set_before_any_key_is_placed(env):
    env.plane.after_cloud_apply(EnvironmentOutputs.model_validate(env.outputs))
    log = env.org.log
    passwords = [i for i, e in enumerate(log) if e[1:3] == ("rdb", "PATCH")]
    keys = [i for i, e in enumerate(log) if e[1:3] == ("instance", "PATCH")]
    assert passwords and keys and max(passwords) < min(keys)


@pytest.mark.parametrize("missing", ["secret", "version"])
def test_a_missing_admin_secret_refuses_with_no_password_sent(env, missing):
    sid = _admin_secret_id(env, "warehouse")
    if missing == "secret":
        del env.org.secrets[sid]
    else:
        env.org.versions[sid] = []
    with pytest.raises(Failure) as caught:
        env.plane.after_cloud_apply(EnvironmentOutputs.model_validate(env.outputs))
    assert "/vecten/acme/prod/setup/SETUP_WAREHOUSE_ADMIN_PASSWORD" in caught.value.message
    assert "vecten deploy --env prod" in caught.value.message
    assert _password_patches(env) == []
    assert not [p for m, p in _user_data_calls(env) if m == "PATCH"]


def test_an_admin_secret_that_is_not_username_password_refuses(env):
    sid = _admin_secret_id(env, "state")
    env.org.versions[sid] = [base64.b64encode(MASTER_PASSWORD.encode()).decode()]
    with pytest.raises(Failure) as caught:
        env.plane.after_cloud_apply(EnvironmentOutputs.model_validate(env.outputs))
    assert "SETUP_STATE_ADMIN_PASSWORD" in caught.value.message
    assert MASTER_PASSWORD not in caught.value.message
    assert _password_patches(env) == []


def test_a_refused_password_change_names_the_instance_and_never_the_password(env):
    env.org.failures[("rdb", "PATCH", "/instances/")] = (409, "precondition_failed")
    with pytest.raises(Failure) as caught:
        env.plane.after_cloud_apply(EnvironmentOutputs.model_validate(env.outputs))
    failure = caught.value
    assert failure.code == ExitCode.TOOL_FAILED
    assert rds_identifier("acme", "prod", "state") in failure.message
    assert "HTTP 409 precondition_failed" in failure.message
    assert MASTER_PASSWORD not in failure.message
    assert MASTER_PASSWORD not in json.dumps(failure.details, default=str)
    assert not [p for m, p in _user_data_calls(env) if m == "PATCH"]


def test_an_instance_the_api_does_not_list_refuses_naming_it(env):
    name = rds_identifier("acme", "prod", "warehouse")
    del env.org.rdb_instances[env.org.rdb_named(name)["id"]]
    with pytest.raises(Failure) as caught:
        env.plane.after_cloud_apply(EnvironmentOutputs.model_validate(env.outputs))
    assert name in caught.value.message
    assert MASTER_PASSWORD not in caught.value.message


# --- the pull-model ship ----------------------------------------------------------------------


def test_ship_uploads_each_bundle_then_moves_its_pointer_setup_host_first(env):
    assert _ship(env) == ["mcp", "platform"]
    keys = [c[2]["Key"] for c in env.storage.named("put_object")]
    assert keys == [
        f"bundles/mcp/{COMMIT}.tar.gz",
        "bundles/mcp/pointer.json",
        f"bundles/platform/{COMMIT}.tar.gz",
        "bundles/platform/pointer.json",
    ]
    # No host has a pointer yet, so each ship is forced.
    for _host, doc in env.watcher.pointers:
        assert doc["commit"] == COMMIT and doc["force"] is True
        datetime.strptime(doc["requested_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
    assert env.polls.calls == [(MCP_URL, 900), (AIRFLOW_URL, 900)]
    signers = {c[1] for c in env.storage.named("put_object")}
    assert signers == {f"{DEPLOY_ACCESS}@{RESOURCES}"}


def test_a_status_from_an_earlier_request_does_not_count(env):
    env.watcher.silent.add("mcp")
    stale = {"ok": True, "line": "shipped", "requested_at": "2026-10-01T00:00:00.000000Z"}
    env.storage.put(env.bucket, f"mcp/ship-status/{COMMIT}.json", json.dumps(stale).encode())
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert "did not pick up" in caught.value.message


def test_ship_times_out_naming_the_host_when_no_status_appears(env):
    env.watcher.silent.add("mcp")
    with pytest.raises(Failure) as caught:
        _ship(env)
    failure = caught.value
    assert failure.code == ExitCode.TOOL_FAILED
    assert "host mcp" in failure.message
    assert "the watcher did not pick up" in failure.message
    assert "900 s" in failure.message
    assert failure.details["host"] == "mcp"
    assert 900 <= env.time.now < 900 + ship_mod.STATUS_POLL_SECONDS + 1
    assert set(env.time.sleeps) == {ship_mod.STATUS_POLL_SECONDS}
    # The first failure stops the ship: platform's bundle never leaves.
    assert not _puts(env, "platform/pointer.json")


def test_a_host_shipped_before_waits_600_seconds(env):
    _shipped_before(env, "mcp")
    env.watcher.silent.add("mcp")
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert "600 s" in caught.value.message
    assert 600 <= env.time.now < 600 + ship_mod.STATUS_POLL_SECONDS + 1


def test_an_agent_failure_reports_its_line_cut_to_300_characters(env):
    line = "vecten agent: compose up failed " + "x" * 400
    env.watcher.results[("mcp", COMMIT)] = (False, line)
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert line[:300] in caught.value.message
    assert line[:301] not in caught.value.message
    assert [h for h, _ in env.watcher.pointers] == ["mcp"]


def test_a_failed_status_prints_its_log(env):
    secret = env.plane.get_secret("SETUP_WAREHOUSE_ADMIN_PASSWORD")
    env.watcher.results[("mcp", COMMIT)] = (False, "vecten agent ship: compose up failed")
    env.watcher.logs[("mcp", COMMIT)] = [
        "mcp-server: ValueError: provisioner has unexpected grant OWNERSHIP on X",
        f"mcp-server: login with {secret}\x07",
    ]
    with pytest.raises(Failure) as caught:
        _ship(env)
    expected = [
        "mcp-server: ValueError: provisioner has unexpected grant OWNERSHIP on X",
        "mcp-server: login with [redacted]",
    ]
    assert caught.value.details["container_log"] == expected
    assert caught.value.message.endswith(
        "\nlast output of compose and the failing containers:\n  " + "\n  ".join(expected)
    )
    assert secret not in caught.value.message


@pytest.mark.parametrize("log", [None, "not a list", [1, 2]])
def test_a_status_without_a_log_list_reports_the_line(env, log):
    env.watcher.results[("mcp", COMMIT)] = (False, "vecten agent ship: compose up failed")
    if log is not None:
        env.watcher.logs[("mcp", COMMIT)] = log
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert caught.value.message.startswith(
        "shipping to host mcp failed: vecten agent ship: compose up failed"
    )
    assert "last output" not in caught.value.message
    assert caught.value.details["container_log"] == []


def test_a_failed_health_poll_rolls_back_to_the_previous_commit(env):
    _shipped_before(env, "mcp")
    _shipped_before(env, "platform")
    env.polls.failing.add(AIRFLOW_URL)
    with pytest.raises(Failure) as caught:
        _ship(env)
    failure = caught.value
    assert failure.code == ExitCode.TOOL_FAILED
    assert failure.message.startswith("platform did not answer healthy")
    assert f"host platform is back on {OLD}" in failure.message
    assert failure.details == {
        "component": "platform",
        "host": "platform",
        "url": AIRFLOW_URL,
        "rolled_back": True,
    }
    pointers = [(h, d["commit"], d["force"]) for h, d in env.watcher.pointers]
    assert pointers == [
        ("mcp", COMMIT, False),
        ("platform", COMMIT, False),
        ("platform", OLD, False),
    ]
    first, back = [d for h, d in env.watcher.pointers if h == "platform"]
    assert back["requested_at"] != first["requested_at"]
    # The watcher downloads the previous bundle again, so it stays in the bucket.
    assert env.storage.body(env.bucket, f"bundles/platform/{OLD}.tar.gz") == b"previous bundle"
    assert not env.storage.named("delete_object")


def test_shipping_the_running_commit_again_forces_and_so_does_its_rollback(env):
    _shipped_before(env, "mcp", COMMIT)
    _shipped_before(env, "platform", COMMIT)
    env.polls.failing.add(MCP_URL)
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert f"host mcp is back on {COMMIT}" in caught.value.message
    shipped, back = [d for h, d in env.watcher.pointers if h == "mcp"]
    assert shipped["commit"] == back["commit"] == COMMIT
    assert shipped["force"] is True and back["force"] is True
    assert back["requested_at"] != shipped["requested_at"]


def test_a_rollback_the_watcher_never_picks_up_is_named(env):
    _shipped_before(env, "mcp")
    env.polls.failing.add(MCP_URL)
    env.watcher.results[("mcp", OLD)] = None
    with pytest.raises(Failure) as caught:
        _ship(env)
    message = caught.value.message
    assert "the watcher on host mcp did not pick up the rollback" in message
    assert caught.value.details["rolled_back"] is False


def test_a_failed_rollback_reports_its_line(env):
    _shipped_before(env, "mcp")
    env.polls.failing.add(MCP_URL)
    env.watcher.results[("mcp", OLD)] = (False, "vecten agent: compose up failed")
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert "the rollback on host mcp failed too: vecten agent: compose up failed" in (
        caught.value.message
    )


def test_a_first_ship_whose_poll_fails_has_nothing_to_roll_back_to(env):
    env.polls.failing.add(MCP_URL)
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert "host mcp has no earlier release to roll back to" in caught.value.message
    assert [h for h, _ in env.watcher.pointers] == ["mcp"]


NEXT = "abcdef0123456789abcdef0123456789abcdef01"


def _pointer(env, host: str) -> dict | None:
    return env.storage.json(env.bucket, f"bundles/{host}/pointer.json")


def test_an_agent_failure_points_the_host_back_at_the_release_it_runs(env):
    _shipped_before(env, "mcp")
    env.watcher.results[("mcp", COMMIT)] = (False, f"vecten agent: boom; rolled back to {OLD}")
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert caught.value.details["rolled_back"] is True
    assert [(h, d["commit"]) for h, d in env.watcher.pointers] == [("mcp", COMMIT), ("mcp", OLD)]
    assert _pointer(env, "mcp")["commit"] == OLD
    assert _pointer(env, "mcp")["force"] is False


def test_a_poll_failure_after_a_rejected_ship_rolls_back_to_the_last_good_release(env):
    _shipped_before(env, "mcp")
    env.watcher.results[("mcp", COMMIT)] = (False, f"vecten agent: boom; rolled back to {OLD}")
    with pytest.raises(Failure):
        _ship(env)
    env.polls.failing.add(MCP_URL)
    with pytest.raises(Failure) as caught:
        _ship(env, NEXT)
    assert f"host mcp is back on {OLD}" in caught.value.message
    assert COMMIT not in caught.value.message
    assert _pointer(env, "mcp")["commit"] == OLD


def test_the_rollback_target_is_the_commit_the_watcher_reports_running(env):
    _shipped_before(env, "mcp")
    # The agent's own rollback landed on a release other than the one the pointer named.
    env.watcher.results[("mcp", COMMIT)] = (
        False,
        f"vecten agent: boom; rolled back to {NEXT}",
        NEXT,
    )
    with pytest.raises(Failure):
        _ship(env)
    assert _pointer(env, "mcp")["commit"] == NEXT


def test_an_agent_that_kept_the_rejected_release_points_back_at_the_previous_one(env):
    _shipped_before(env, "mcp")
    env.watcher.results[("mcp", COMMIT)] = (False, "vecten agent: rolling back failed", COMMIT)
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert _pointer(env, "mcp")["commit"] == OLD
    assert caught.value.message.endswith(f"; host mcp is back on {OLD}")
    assert caught.value.details["rolled_back"] is True


def test_a_rejected_first_ship_leaves_no_pointer_so_the_next_ship_is_a_first_one(env):
    env.watcher.results[("mcp", COMMIT)] = (False, "vecten agent: no previous release")
    with pytest.raises(Failure):
        _ship(env)
    assert _pointer(env, "mcp") is None
    env.polls.failing.add(MCP_URL)
    with pytest.raises(Failure) as caught:
        _ship(env, NEXT)
    assert "host mcp has no earlier release to roll back to" in caught.value.message
    assert env.polls.calls[-1] == (MCP_URL, 900)


def _pointed_at_by_a_replaced_server(env, host: str) -> None:
    """The pointer and bundle the replaced server shipped; the new server runs nothing yet."""
    _shipped_before(env, host)
    del env.watcher.running[host]


def test_a_ship_that_times_out_points_back_at_the_previous_commit_with_a_new_request(env):
    _shipped_before(env, "mcp")
    before = _pointer(env, "mcp")
    env.watcher.silent.add("mcp")
    with pytest.raises(Failure) as caught:
        _ship(env)
    after = _pointer(env, "mcp")
    assert after["commit"] == OLD and after["force"] is False
    # A watcher whose status for the new commit was lost still holds the old request as
    # handled; only a new one makes it reconcile.
    assert after["requested_at"] != before["requested_at"]
    assert caught.value.message.endswith(
        f"; the pointer is back on {OLD}, which a watcher still shipping {COMMIT} ships after it"
    )


def test_a_keyed_host_that_times_out_gets_the_previous_pointer_back_unchanged(env):
    _shipped_before(env, "mcp")
    before = _pointer(env, "mcp")
    env.watcher.silent.add("mcp")
    outputs = EnvironmentOutputs.model_validate(env.outputs)
    with pytest.raises(Failure):
        ship_mod.ship_hosts(
            env.plane, env.root, load_config(env.root), "prod", outputs, COMMIT, frozenset({"mcp"})
        )
    assert _pointer(env, "mcp") == before


def test_retrying_a_first_ship_whose_poll_failed_forces_it(env):
    env.polls.failing.add(MCP_URL)
    for _attempt in range(2):
        with pytest.raises(Failure):
            _ship(env)
    shipped = [d for h, d in env.watcher.pointers if h == "mcp"]
    assert [d["commit"] for d in shipped] == [COMMIT, COMMIT]
    assert shipped[1]["force"] is True


def test_a_first_ship_that_times_out_leaves_no_pointer(env):
    env.watcher.silent.add("mcp")
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert _pointer(env, "mcp") is None
    assert caught.value.message.endswith("; the pointer is removed")


def test_a_first_ship_whose_poll_fails_leaves_no_pointer(env):
    env.polls.failing.add(MCP_URL)
    with pytest.raises(Failure):
        _ship(env)
    assert _pointer(env, "mcp") is None


def test_only_a_first_ship_timeout_names_the_clock(env):
    env.watcher.silent.add("mcp")
    with pytest.raises(Failure) as first:
        _ship(env)
    assert "its clock may run ahead of this machine's" in first.value.message
    _shipped_before(env, "mcp")
    with pytest.raises(Failure) as later:
        _ship(env)
    assert "clock" not in later.value.message


def test_a_rejected_ship_on_a_replaced_server_waits_for_the_release_it_points_back_at(env):
    _pointed_at_by_a_replaced_server(env, "mcp")
    env.watcher.results[("mcp", COMMIT)] = (False, "vecten agent: no previous release")
    with pytest.raises(Failure) as caught:
        _ship(env)
    failure = caught.value
    assert failure.message.startswith("shipping to host mcp failed: vecten agent: no previous")
    assert failure.message.endswith(f"; host mcp is back on {OLD}")
    assert failure.details["rolled_back"] is True
    assert _pointer(env, "mcp")["commit"] == OLD


def test_a_replaced_server_that_fails_the_release_it_is_pointed_back_at_says_so(env):
    _pointed_at_by_a_replaced_server(env, "mcp")
    env.watcher.results[("mcp", COMMIT)] = (False, "vecten agent: no previous release")
    env.watcher.results[("mcp", OLD)] = (False, "vecten agent: compose up failed")
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert caught.value.message.endswith(
        "; the rollback on host mcp failed too: vecten agent: compose up failed"
    )
    assert caught.value.details["rolled_back"] is False


def test_a_replaced_server_that_never_picks_up_the_release_it_is_pointed_back_at_says_so(env):
    _pointed_at_by_a_replaced_server(env, "mcp")
    env.watcher.results[("mcp", COMMIT)] = (False, "vecten agent: no previous release")
    env.watcher.results[("mcp", OLD)] = None
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert f"the watcher on host mcp did not pick up the rollback to {OLD}" in (
        caught.value.message
    )
    assert caught.value.details["rolled_back"] is False


def test_a_reported_line_never_carries_a_secret_this_run_read(env):
    secret = env.plane.get_secret("SETUP_WAREHOUSE_ADMIN_PASSWORD")
    env.watcher.results[("mcp", COMMIT)] = (False, f"vecten agent: setup failed: {secret}")
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert MASTER_PASSWORD not in caught.value.message


# --- final snapshots -------------------------------------------------------------------------


NOW = datetime(2026, 10, 18, 15, 30, tzinfo=UTC)


def test_final_snapshots_are_taken_per_instance_and_waited_for(env):
    heard: list[str] = []
    absent: list[str] = []
    taken = env.plane.final_snapshots(
        EnvironmentOutputs.model_validate(env.outputs),
        NOW,
        on_created=heard.append,
        absent=absent,
        expires={},
    )
    expected = [snapshot_name("acme", "prod", k, NOW) for k in ("state", "warehouse")]
    assert taken == expected and heard == expected and absent == []
    assert {s["name"] for s in env.org.snapshots.values()} == set(expected)
    assert all(s["status"] == "ready" for s in env.org.snapshots.values())


def test_final_snapshots_report_when_scaleway_deletes_them(env):
    expires: dict[str, str] = {}
    taken = env.plane.final_snapshots(
        EnvironmentOutputs.model_validate(env.outputs),
        NOW,
        on_created=lambda s: None,
        absent=[],
        expires=expires,
    )
    assert expires == {name: env.org.snapshot_default_expires_at for name in taken}
    # Scaleway's default is already its 365-day maximum, so destroy asks for no other date.
    assert all("expires_at" not in body for body in env.org.snapshot_requests)


def test_an_instance_already_gone_is_reported_absent(env):
    gone = env.org.rdb_named(rds_identifier("acme", "prod", "warehouse"))
    del env.org.rdb_instances[gone["id"]]
    absent: list[str] = []
    taken = env.plane.final_snapshots(
        EnvironmentOutputs.model_validate(env.outputs),
        NOW,
        on_created=lambda s: None,
        absent=absent,
        expires={},
    )
    assert taken == [snapshot_name("acme", "prod", "state", NOW)]
    assert absent == [rds_identifier("acme", "prod", "warehouse")]


def test_a_snapshot_in_error_stops_before_anything_is_destroyed(env):
    env.org.snapshot_final_status = "error"
    with pytest.raises(Failure) as caught:
        env.plane.final_snapshots(
            EnvironmentOutputs.model_validate(env.outputs),
            NOW,
            on_created=lambda s: None,
            absent=[],
            expires={},
        )
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert "nothing was destroyed" in caught.value.message


def test_a_snapshot_that_never_becomes_ready_times_out(env):
    env.org.snapshot_reads_creating = 10_000
    with pytest.raises(Failure) as caught:
        env.plane.final_snapshots(
            EnvironmentOutputs.model_validate(env.outputs),
            NOW,
            on_created=lambda s: None,
            absent=[],
            expires={},
        )
    assert "nothing was destroyed" in caught.value.message


# --- add-admin's way in --------------------------------------------------------------------


def test_admin_connection_reaches_the_public_endpoint(env):
    path = env_tfvars_path(env.root, "prod")
    path.write_text(path.read_text() + 'database_public_cidrs = ["203.0.113.7/32"]\n')
    warehouse = env.org.rdb_named(rds_identifier("acme", "prod", "warehouse"))
    warehouse["endpoints"].append(
        {"id": "lb", "ip": "51.15.0.9", "port": 20123, "private_network": None, "load_balancer": {}}
    )
    outputs = EnvironmentOutputs.model_validate(env.outputs)
    env.plane.require_admin_access()
    with env.plane.admin_connection(outputs, outputs.postgres["warehouse"]) as reach:
        path = Path(reach.pop("sslrootcert"))
        assert reach == {"host": "51.15.0.9", "port": 20123, "sslmode": "verify-ca"}
        assert path.read_bytes() == CERTIFICATE_PEM
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.exists()
    fetched = [p for a, m, p, _, _ in env.org.calls if a == "rdb" and p.endswith("/certificate")]
    assert fetched == [f"/instances/{warehouse['id']}/certificate"]


def test_the_certificate_file_is_removed_when_the_connection_fails(env):
    warehouse = env.org.rdb_named(rds_identifier("acme", "prod", "warehouse"))
    warehouse["endpoints"].append(
        {"id": "lb", "ip": "51.15.0.9", "port": 20123, "private_network": None, "load_balancer": {}}
    )
    outputs = EnvironmentOutputs.model_validate(env.outputs)
    seen: list[Path] = []
    with pytest.raises(RuntimeError):
        with env.plane.admin_connection(outputs, outputs.postgres["warehouse"]) as reach:
            seen.append(Path(reach["sslrootcert"]))
            raise RuntimeError("the server went away")
    assert seen and not seen[0].exists()


def test_an_answer_without_a_certificate_connects_nowhere(env, monkeypatch):
    from . import scaleway_fakes

    warehouse = env.org.rdb_named(rds_identifier("acme", "prod", "warehouse"))
    warehouse["endpoints"].append(
        {"id": "lb", "ip": "51.15.0.9", "port": 20123, "private_network": None, "load_balancer": {}}
    )
    monkeypatch.setattr(scaleway_fakes, "CERTIFICATE_PEM", b"not a certificate")
    outputs = EnvironmentOutputs.model_validate(env.outputs)
    with pytest.raises(Failure) as caught:
        with env.plane.admin_connection(outputs, outputs.postgres["warehouse"]):
            pass
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert "certificate" in caught.value.message


def test_admin_access_is_refused_without_public_cidrs(env):
    with pytest.raises(Failure) as caught:
        env.plane.require_admin_access()
    assert caught.value.code == ExitCode.REFUSED
    assert "database_public_cidrs" in caught.value.message
    assert "deploy/envs/prod.tfvars" in caught.value.message


def test_an_instance_without_its_public_endpoint_yet_names_deploy(env):
    outputs = EnvironmentOutputs.model_validate(env.outputs)
    with pytest.raises(Failure) as caught:
        with env.plane.admin_connection(outputs, outputs.postgres["warehouse"]):
            pass
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "vecten deploy --env prod" in caught.value.message


def test_minimal_profile_reaches_the_state_instance(tmp_path, monkeypatch):
    root = render_aws(tmp_path / "proj", cloud="scaleway", profile="minimal")
    monkeypatch.chdir(root)
    org, storage, _ = install(monkeypatch, tmp_path)
    outputs = prepare_environment(root, org, storage, scaleway_outputs(minimal=True), public=True)
    plane = ScalewayProvider(root, "prod")
    parsed = EnvironmentOutputs.model_validate(outputs)
    with plane.admin_connection(parsed, parsed.postgres["state"]) as reach:
        assert reach["host"] == "51.15.0.9"


def test_the_secret_id_is_the_scaleway_resource_name(env):
    assert (
        env.plane.secret_id("ADMIN_JANE_PASSWORD") == "/vecten/acme/prod/admin/ADMIN_JANE_PASSWORD"
    )


def test_scaleway_admins_get_the_warehouse_roles_the_master_created(env):
    assert env.plane.admin_grants == ("warehouse_owner", "loader")


def test_a_host_keyed_in_this_deploy_gets_the_first_ship_window(env):
    _shipped_before(env, "mcp")
    _shipped_before(env, "platform")
    digest = hashlib.sha256(HOST_KEYS["platform"][0].encode()).hexdigest()
    env.storage.put(env.bucket, f"platform/key-ack/{digest}.json", b"{}", modified=START)
    env.plane.after_cloud_apply(EnvironmentOutputs.model_validate(env.outputs))
    env.watcher.silent.add("mcp")
    with pytest.raises(Failure) as caught:
        _ship(env)
    assert "900 s" in caught.value.message


def test_a_host_this_deploy_did_not_key_keeps_the_shorter_window(env):
    _shipped_before(env, "mcp")
    _shipped_before(env, "platform")
    for host in ("mcp", "platform"):
        digest = hashlib.sha256(HOST_KEYS[host][0].encode()).hexdigest()
        first = env.outputs["hosts"][host]["components"][0]
        env.storage.put(env.bucket, f"{first}/key-ack/{digest}.json", b"{}", modified=START)
    env.plane.after_cloud_apply(EnvironmentOutputs.model_validate(env.outputs))
    assert env.polls.calls == []
    _ship(env)
    assert env.polls.calls == [(MCP_URL, 600), (AIRFLOW_URL, 600)]


def test_feedback_is_collected_once_before_scaleway_ships_any_host(env, monkeypatch):
    calls = []

    def metadata(root, config, commit, lineage):
        calls.append(commit)
        assert not _puts(env, "pointer.json")
        return {"commit": commit, "ancestors": [], "resolutions": []}

    monkeypatch.setattr("vecten.deploy.bundle.feedback_manifest", metadata)
    assert _ship(env) == ["mcp", "platform"]
    assert calls == [COMMIT]


def test_read_object_reads_the_artifacts_bucket(env):
    env.storage.put(env.bucket, "platform/dbt/built_commit", b"abc\n")
    assert env.plane.read_object("platform/dbt/built_commit") == b"abc\n"
    assert env.plane.read_object("platform/dbt/runs/missing.json") is None
