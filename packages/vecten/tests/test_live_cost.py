"""Tests for scripts/live/cost.py.

The cloud listing fixtures are written from the aws and scw documentation, not captured from
a real account: fixtures from documentation.
"""

import functools
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/live/cost.py"
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


@functools.cache
def mod():
    spec = importlib.util.spec_from_file_location("cost", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def wrapped(monkeypatch):
    """Every test runs as the wrapper runs it, but those that remove the marker."""
    monkeypatch.setenv("VECTEN_LIVE", "1")


def entry(project="acme", env="prod", combination="aws-postgres", destroyed_at=None, **extra):
    return {
        "combination": combination,
        "project": project,
        "env": env,
        "repository": "example-sandbox/acme",
        "created_at": "2026-11-19T10:00:00+00:00",
        "destroyed_at": destroyed_at,
        **extra,
    }


def write_created(tmp_path, *entries):
    path = tmp_path / "created.json"
    path.write_text(json.dumps({"environments": list(entries)}))
    return path


def found(kind, name, project, env=None):
    return mod().Found(kind, name, project, env)


def listers(**by_key):
    """Every real listing replaced by an empty one, then `by_key` ("aws:secret") on top."""
    m = mod()
    table = {key: (lambda: []) for key in m.real_listers(lambda name: "")}
    for key, lister in by_key.items():
        cloud, kind = key.split(":")
        assert (cloud, kind) in table
        table[(cloud, kind)] = lister
    return table


def sweep(entries, table):
    m = mod()
    return m.sweep([m.Environment.parse(e) for e in entries], table)


def test_estimate_prorates_the_monthly_price():
    assert mod().estimate(["aws-postgres"], 73, lambda c, p, w: 120.0) == pytest.approx(12.0)


def test_profiles_follow_the_cloud():
    calls = []

    def monthly(cloud, profile, warehouse):
        calls.append((cloud, profile, warehouse))
        return 0.0

    mod().estimate(["aws-snowflake", "scaleway-postgres", "azure-snowflake"], 1, monthly)
    assert calls == [
        ("aws", "standard", "snowflake"),
        ("scaleway", "minimal", "postgres"),
        ("azure", "minimal", "snowflake"),
    ]


def test_over_the_ceiling_exits_3_and_says_ask(capsys):
    code = mod().main(
        ["estimate", "--combinations", "aws-postgres", "--hours", "100"],
        monthly=lambda c, p, w: 730.0,
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "over the $60 ceiling: ask the maintainer" in captured.err
    assert "$100.00" in captured.out


def test_exactly_the_ceiling_passes(capsys):
    code = mod().main(
        ["estimate", "--combinations", "aws-postgres,scaleway-postgres", "--hours", "73"],
        monthly=lambda c, p, w: 300.0,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines() == [
        "aws-postgres         $30.00",
        "scaleway-postgres    $30.00",
        "total                $60.00",
    ]


def test_an_unknown_combination_is_refused(capsys):
    code = mod().main(
        ["estimate", "--combinations", "aws-mysql", "--hours", "1"], monthly=lambda *a: 0.0
    )
    assert code == 2
    assert "aws-mysql" in capsys.readouterr().err


def test_estimate_json_prints_one_document(capsys):
    code = mod().main(
        ["estimate", "--combinations", "aws-postgres", "--hours", "73", "--json"],
        monthly=lambda c, p, w: 730.0,
    )
    document = json.loads(capsys.readouterr().out)
    assert code == 3
    assert document == {
        "combinations": {"aws-postgres": 73.0},
        "total_usd": 73.0,
        "ceiling_usd": 60,
        "over_ceiling": True,
    }


def test_monthly_reads_the_asked_profiles_figure(monkeypatch):
    m = mod()
    seen = []
    body = {"clouds": {"scaleway": {"monthly_usd": 84, "standard_monthly_usd": 107}}}

    def run(cmd, **kwargs):
        seen.append(cmd)
        return Run(stdout=json.dumps(body))

    monkeypatch.setattr(m.subprocess, "run", run)
    assert m.vecten_monthly("scaleway", "minimal", "postgres") == 84.0
    assert seen[0][-7:] == [
        "--cloud",
        "scaleway",
        "--profile",
        "minimal",
        "--warehouse",
        "postgres",
        "--json",
    ]


def test_a_failed_estimate_is_refused_not_zero(capsys, monkeypatch):
    m = mod()
    monkeypatch.setattr(m, "ESTIMATE", ["false"])
    code = m.main(["estimate", "--combinations", "aws-postgres", "--hours", "1"])
    assert code == 2
    assert "estimate" in capsys.readouterr().err


def test_a_resource_of_a_live_listed_environment_is_not_a_leftover():
    table = listers(**{"aws:secret": lambda: [found("secret", "vecten/acme/prod/x", "acme")]})
    result = sweep([entry()], table)
    assert result.leftovers == [] and result.failed == []


def test_a_resource_of_a_destroyed_environment_is_a_leftover():
    table = listers(**{"aws:secret": lambda: [found("secret", "vecten/acme/prod/x", "acme")]})
    result = sweep([entry(destroyed_at="2026-11-19T11:00:00+00:00")], table)
    assert [(r.cloud, r.kind, r.name, r.project) for r in result.leftovers] == [
        ("aws", "secret", "vecten/acme/prod/x", "acme")
    ]


def test_a_resource_of_an_unlisted_project_is_a_leftover():
    table = listers(**{"scaleway:instance": lambda: [found("instance", "i-1", "stray")]})
    result = sweep([entry()], table)
    assert [(r.cloud, r.name, r.project) for r in result.leftovers] == [
        ("scaleway", "i-1", "stray")
    ]


def test_an_absent_destroyed_at_counts_as_live():
    bare = entry()
    del bare["destroyed_at"]
    table = listers(**{"aws:secret": lambda: [found("secret", "s", "acme")]})
    assert sweep([bare], table).leftovers == []


def test_a_failed_listing_is_reported_and_never_reads_as_clean(tmp_path, capsys):
    def broken():
        raise RuntimeError("scw exited 1: permission denied\nsecond line")

    table = listers(**{"scaleway:instance": broken})
    created = write_created(tmp_path, entry())
    code = mod().main(["sweep", "--created", str(created)], listers=table)
    captured = capsys.readouterr()
    assert code == 4
    assert "could not list scaleway instance: scw exited 1: permission denied" in captured.out
    assert "second line" not in captured.out
    assert "nothing left" not in captured.out


def test_a_failed_listing_still_exits_4_when_leftovers_were_found(tmp_path, capsys):
    def broken():
        raise RuntimeError("boom")

    table = listers(
        **{
            "scaleway:instance": broken,
            "aws:secret": lambda: [found("secret", "vecten/gone/x/y", "gone")],
        }
    )
    created = write_created(tmp_path, entry())
    code = mod().main(["sweep", "--created", str(created)], listers=table)
    out = capsys.readouterr().out
    assert code == 4
    assert "could not list scaleway instance: boom" in out
    assert "aws secret vecten/gone/x/y" in out


def test_a_clean_sweep_exits_0_and_says_so(tmp_path, capsys):
    created = write_created(tmp_path, entry())
    code = mod().main(["sweep", "--created", str(created)], listers=listers())
    assert code == 0
    assert "nothing left" in capsys.readouterr().out


def test_leftovers_exit_1_and_name_cloud_kind_name_and_project(tmp_path, capsys):
    table = listers(**{"aws:role": lambda: [found("role", "vecten-gone-x-deploy", "gone")]})
    created = write_created(tmp_path, entry())
    code = mod().main(["sweep", "--created", str(created)], listers=table)
    assert code == 1
    assert "aws role vecten-gone-x-deploy (project gone)" in capsys.readouterr().out


def test_sweep_json_prints_one_document(tmp_path, capsys):
    table = listers(**{"aws:role": lambda: [found("role", "r", "gone")]})
    created = write_created(tmp_path, entry())
    code = mod().main(["sweep", "--created", str(created), "--json"], listers=table)
    document = json.loads(capsys.readouterr().out)
    assert code == 1
    assert document["leftovers"] == [
        {"cloud": "aws", "kind": "role", "name": "r", "project": "gone", "env": None}
    ]
    assert document["failed"] == []


def test_an_untagged_state_bucket_of_a_destroyed_project_is_a_leftover():
    m = mod()
    envs = [m.Environment.parse(entry(destroyed_at="2026-11-19T11:00:00+00:00"))]
    owner = m.owner_of(envs)
    name = "vecten-acme-prod-state-111111111111"
    table = listers(**{"aws:state-bucket": lambda: [found("state-bucket", name, *owner(name))]})
    result = m.sweep(envs, table)
    assert [(r.kind, r.name, r.project, r.env) for r in result.leftovers] == [
        ("state-bucket", name, "acme", "prod")
    ]


def test_a_name_no_environment_claims_is_unknown_and_a_leftover():
    m = mod()
    owner = m.owner_of([m.Environment.parse(entry())])
    assert owner("vecten-other-prod-deploy") == ("unknown", None)
    assert owner("vecten-acme-prod-deploy") == ("acme", "prod")
    assert owner("vecten-acme-prod") == ("acme", "prod")
    assert owner("vecten-acme-prod-state-111111111111") == ("acme", "prod")
    assert owner("vecten-acme-production-deploy") == ("unknown", None)


def test_a_name_that_only_starts_with_a_prefix_is_not_claimed():
    m = mod()
    owner = m.owner_of([m.Environment.parse(entry())])
    assert owner("vecten-acme-prod-x-plan") == ("unknown", None)


def test_the_longest_matching_prefix_owns_the_name():
    m = mod()
    owner = m.owner_of(
        [m.Environment.parse(entry("acme", "prod")), m.Environment.parse(entry("acme-prod", "x"))]
    )
    assert owner("vecten-acme-prod-x-plan") == ("acme-prod", "x")


def test_spent_counts_a_running_environment_up_to_now(tmp_path, capsys):
    created = write_created(
        tmp_path,
        entry(created_at="2026-11-19T00:00:00+00:00"),
        entry(
            env="old",
            created_at="2026-11-18T00:00:00+00:00",
            destroyed_at="2026-11-18T10:00:00+00:00",
        ),
    )
    code = mod().main(["spent", "--created", str(created)], monthly=lambda c, p, w: 730.0, now=NOW)
    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        "aws-postgres         $22.00",
        "total                $22.00",
    ]


def test_spent_over_the_ceiling_exits_3_and_says_ask(tmp_path, capsys):
    created = write_created(tmp_path, entry(created_at="2026-11-19T00:00:00+00:00"))
    code = mod().main(["spent", "--created", str(created)], monthly=lambda *a: 7300.0, now=NOW)
    captured = capsys.readouterr()
    assert code == 3
    assert "over the $60 ceiling: ask the maintainer" in captured.err
    assert captured.out.splitlines()[-1] == "total                $120.00"


def test_spent_at_the_ceiling_exits_0(tmp_path, capsys):
    created = write_created(tmp_path, entry(created_at="2026-11-19T00:00:00+00:00"))
    code = mod().main(["spent", "--created", str(created)], monthly=lambda *a: 3650.0, now=NOW)
    assert code == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("marker", [None, "0", ""])
def test_sweep_refuses_outside_the_wrapper(tmp_path, capsys, monkeypatch, marker):
    if marker is None:
        monkeypatch.delenv("VECTEN_LIVE")
    else:
        monkeypatch.setenv("VECTEN_LIVE", marker)
    listed = []
    table = listers(**{"aws:role": lambda: listed.append("role") or []})
    created = write_created(tmp_path, entry())
    code = mod().main(["sweep", "--created", str(created)], listers=table)
    captured = capsys.readouterr()
    assert code == 3
    assert listed == []
    assert captured.out == ""
    assert len(captured.err.strip().splitlines()) == 1
    assert "scripts/live/live cost sweep" in captured.err


def test_estimate_and_spent_need_no_wrapper(tmp_path, monkeypatch):
    monkeypatch.delenv("VECTEN_LIVE")
    m = mod()
    estimate = ["estimate", "--combinations", "aws-postgres", "--hours", "1"]
    assert m.main(estimate, monthly=lambda *a: 1.0) == 0
    created = write_created(tmp_path, entry())
    assert m.main(["spent", "--created", str(created)], monthly=lambda *a: 1.0, now=NOW) == 0


def test_spent_refuses_a_time_without_a_timezone(tmp_path, capsys):
    created = write_created(tmp_path, entry(created_at="2026-11-19T00:00:00"))
    code = mod().main(["spent", "--created", str(created)], monthly=lambda *a: 1.0, now=NOW)
    assert code == 2
    assert "timezone" in capsys.readouterr().err


def test_spent_json_prints_one_document(tmp_path, capsys):
    created = write_created(tmp_path, entry(created_at="2026-11-19T00:00:00+00:00"))
    code = mod().main(
        ["spent", "--created", str(created), "--json"], monthly=lambda *a: 730.0, now=NOW
    )
    document = json.loads(capsys.readouterr().out)
    assert code == 0
    assert document["total_usd"] == 12.0
    assert document["combinations"] == {"aws-postgres": 12.0}


def test_a_missing_created_file_is_refused(tmp_path, capsys):
    code = mod().main(["sweep", "--created", str(tmp_path / "none.json")], listers=listers())
    assert code == 2
    assert "created" in capsys.readouterr().err


class Run:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def test_a_cloud_command_that_exits_non_zero_is_a_failed_listing(monkeypatch):
    m = mod()
    monkeypatch.setattr(
        m.subprocess, "run", lambda cmd, **kw: Run(returncode=255, stderr="boom\nAccessDenied\n")
    )
    with pytest.raises(m.ListingError, match="AccessDenied"):
        m.run_json(["aws", "x"])


def test_unparseable_cloud_output_is_a_failed_listing(monkeypatch):
    m = mod()
    monkeypatch.setattr(m.subprocess, "run", lambda cmd, **kw: Run(stdout="not json"))
    with pytest.raises(m.ListingError, match="not JSON"):
        m.run_json(["aws", "x"])


def test_a_missing_cloud_tool_is_a_failed_listing(monkeypatch):
    m = mod()

    def run(cmd, **kw):
        raise FileNotFoundError(2, "No such file", "aws")

    monkeypatch.setattr(m.subprocess, "run", run)
    with pytest.raises(m.ListingError, match="aws"):
        m.run_json(["aws", "x"])


class Fail:
    def __init__(self, message):
        self.message = message


def real(monkeypatch, outputs):
    m = mod()
    owner = m.owner_of([m.Environment.parse(entry())])

    def run(cmd, **kw):
        value = outputs[tuple(cmd)]
        if isinstance(value, Fail):
            return Run(returncode=1, stderr=value.message)
        return Run(stdout=json.dumps(value))

    monkeypatch.setattr(m.subprocess, "run", run)
    return m.real_listers(owner)


def scw(template, project_id):
    return tuple(part.format(project_id=project_id) for part in template)


def tagging(m, region):
    return tuple([*m.AWS_TAGGED, "--region", region])


def regions(*names):
    return {"Regions": [{"RegionName": n} for n in names]}


def arn_item(arn, **tags):
    return {"ResourceARN": arn, "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}


def test_aws_tagged_resources_are_read_in_every_region_with_their_environment(monkeypatch):
    m = mod()
    one = "arn:aws:ec2:eu-west-1:111111111111:instance/i-1"
    two = "arn:aws:ec2:us-east-1:111111111111:instance/i-2"
    outputs = {
        tuple(m.AWS_REGIONS): regions("eu-west-1", "us-east-1"),
        tagging(m, "eu-west-1"): {
            "ResourceTagMappingList": [
                arn_item(one, **{"vecten:project": "acme", "vecten:env": "prod"})
            ]
        },
        tagging(m, "us-east-1"): {
            "ResourceTagMappingList": [arn_item(two, **{"vecten:project": "acme"})]
        },
    }
    table = real(monkeypatch, outputs)
    assert table[("aws", "tagged")]() == [
        found("tagged", one, "acme", "prod"),
        found("tagged", two, "acme"),
    ]


def test_a_failing_region_is_a_failed_listing_that_names_it_and_keeps_the_rest(monkeypatch):
    m = mod()
    arn = "arn:aws:ec2:eu-west-1:111111111111:instance/i-1"
    outputs = {
        tuple(m.AWS_REGIONS): regions("eu-west-1", "us-east-1"),
        tagging(m, "eu-west-1"): {
            "ResourceTagMappingList": [arn_item(arn, **{"vecten:project": "gone"})]
        },
        tagging(m, "us-east-1"): Fail("AccessDenied"),
    }
    table = {("aws", "tagged"): real(monkeypatch, outputs)[("aws", "tagged")]}
    result = sweep([entry()], table)
    assert result.failed == [
        "could not list aws tagged: region us-east-1: aws exited 1: AccessDenied"
    ]
    assert [r.name for r in result.leftovers] == [arn]


def test_failing_to_list_the_regions_is_a_failed_listing(monkeypatch):
    m = mod()
    table = real(monkeypatch, {tuple(m.AWS_REGIONS): Fail("no credentials")})
    result = sweep([entry()], {("aws", "tagged"): table[("aws", "tagged")]})
    assert result.failed == ["could not list aws tagged: aws exited 1: no credentials"]


def test_aws_secrets_roles_policies_and_state_buckets_are_listed_by_name(monkeypatch):
    m = mod()
    outputs = {
        tuple(m.AWS_SECRETS): {"SecretList": [{"Name": "vecten/acme/prod/platform/KEY"}]},
        tuple(m.AWS_ROLES): {
            "Roles": [
                {"RoleName": "vecten-acme-prod-deploy"},
                {"RoleName": "vecten-acme-prod-plan"},
                {"RoleName": "vecten-acme-prod-platform"},
                {"RoleName": "AWSServiceRoleForRDS"},
            ]
        },
        tuple(m.AWS_POLICIES): {
            "Policies": [{"PolicyName": "vecten-acme-prod-boundary"}, {"PolicyName": "other"}]
        },
        tuple(m.AWS_BUCKETS): {
            "Buckets": [
                {"Name": "vecten-acme-prod-state-111111111111"},
                {"Name": "vecten-acme-prod-artifacts-111111111111"},
                {"Name": "unrelated"},
            ]
        },
    }
    table = real(monkeypatch, outputs)
    assert table[("aws", "secret")]() == [
        found("secret", "vecten/acme/prod/platform/KEY", "acme", "prod")
    ]
    assert table[("aws", "role")]() == [
        found("role", "vecten-acme-prod-deploy", "acme", "prod"),
        found("role", "vecten-acme-prod-plan", "acme", "prod"),
    ]
    assert table[("aws", "boundary-policy")]() == [
        found("boundary-policy", "vecten-acme-prod-boundary", "acme", "prod")
    ]
    assert table[("aws", "state-bucket")]() == [
        found("state-bucket", "vecten-acme-prod-state-111111111111", "acme", "prod")
    ]


def scw_outputs(m):
    return {
        tuple(m.SCW_PROJECTS): [
            {"id": "p1", "name": "vecten-acme-prod", "description": "vecten:resources:acme:prod"},
            {"id": "p2", "name": "vecten-acme-prod-state", "description": "vecten:state:acme:prod"},
            {"id": "p3", "name": "default", "description": ""},
        ],
        scw(m.SCW_INSTANCES, "p1"): [
            {"id": "i1", "name": "web", "tags": ["vecten:project=acme", "vecten:env=prod"]},
            {"id": "i2", "name": "other", "tags": []},
        ],
        scw(m.SCW_INSTANCES, "p2"): [],
        scw(m.SCW_DATABASES, "p1"): [{"id": "d1", "name": "db", "tags": ["vecten:project=gone"]}],
        scw(m.SCW_DATABASES, "p2"): [],
    }


def test_scaleway_projects_keys_applications_and_tagged_resources_are_listed(monkeypatch):
    m = mod()
    outputs = scw_outputs(m) | {
        tuple(m.SCW_APPLICATIONS): [
            {"id": "a1", "name": "vecten-acme-prod-deploy"},
            {"id": "a2", "name": "someone-else"},
        ],
        tuple(m.SCW_KEYS): [
            {"access_key": "SCWAAAA", "application_id": "a1"},
            {"access_key": "SCWBBBB", "application_id": "a2"},
            {"access_key": "SCWCCCC", "user_id": "u1"},
        ],
    }
    table = real(monkeypatch, outputs)
    assert table[("scaleway", "project")]() == [
        found("project", "vecten-acme-prod (p1)", "acme", "prod"),
        found("project", "vecten-acme-prod-state (p2)", "acme", "prod"),
    ]
    assert table[("scaleway", "application")]() == [
        found("application", "vecten-acme-prod-deploy (a1)", "acme", "prod")
    ]
    assert table[("scaleway", "api-key")]() == [found("api-key", "SCWAAAA", "acme", "prod")]
    assert table[("scaleway", "instance")]() == [found("instance", "web (i1)", "acme", "prod")]
    assert table[("scaleway", "database")]() == [found("database", "db (d1)", "gone")]


def test_a_failing_scaleway_project_is_a_failed_listing_that_names_it(monkeypatch):
    m = mod()
    outputs = scw_outputs(m) | {scw(m.SCW_INSTANCES, "p2"): Fail("quota")}
    table = real(monkeypatch, outputs)
    result = sweep([entry()], {("scaleway", "instance"): table[("scaleway", "instance")]})
    assert result.failed == [
        "could not list scaleway instance: project vecten-acme-prod-state (p2): scw exited 1: quota"
    ]
    assert result.leftovers == []


def test_when_the_projects_cannot_be_listed_instances_and_databases_are_failed_too(monkeypatch):
    m = mod()
    table = real(monkeypatch, {tuple(m.SCW_PROJECTS): Fail("denied")})
    result = sweep(
        [entry()],
        {
            ("scaleway", "instance"): table[("scaleway", "instance")],
            ("scaleway", "database"): table[("scaleway", "database")],
            ("scaleway", "project"): table[("scaleway", "project")],
        },
    )
    assert len(result.failed) == 3
    assert result.failed[0].startswith(
        "could not list scaleway instance: cannot scope to the vecten projects"
    )
    assert result.failed[1].startswith("could not list scaleway database: cannot scope")
    assert result.failed[2] == "could not list scaleway project: scw exited 1: denied"


def test_a_cloud_command_that_hangs_is_a_failed_listing(monkeypatch):
    m = mod()
    monkeypatch.setattr(m, "TIMEOUT_SECONDS", 0.2)
    with pytest.raises(m.ListingError, match="did not finish"):
        m.run_json([sys.executable, "-c", "import time; time.sleep(30)"])


def test_a_hung_estimate_is_refused(monkeypatch, capsys):
    m = mod()
    monkeypatch.setattr(m, "TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(m, "ESTIMATE", [sys.executable, "-c", "import time; time.sleep(30)", "x"])
    code = m.main(["estimate", "--combinations", "aws-postgres", "--hours", "1"])
    assert code == 2
    assert "did not finish" in capsys.readouterr().err


@pytest.mark.parametrize("hours", ["nan", "inf", "-1", "0"])
def test_hours_that_are_not_a_positive_number_are_refused(hours, capsys):
    code = mod().main(
        ["estimate", "--combinations", "aws-postgres", "--hours", hours], monthly=lambda *a: 1.0
    )
    assert code == 2
    assert "$" not in capsys.readouterr().out


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -5.0])
def test_a_monthly_price_that_is_not_a_usable_number_is_refused(price, capsys):
    code = mod().main(
        ["estimate", "--combinations", "aws-postgres", "--hours", "1"], monthly=lambda *a: price
    )
    assert code == 2
    assert capsys.readouterr().out == ""


def test_a_nan_in_the_estimate_json_is_refused(monkeypatch, capsys):
    m = mod()
    out = '{"clouds": {"aws": {"monthly_usd": NaN}}}'
    monkeypatch.setattr(m.subprocess, "run", lambda cmd, **kw: Run(stdout=out))
    code = m.main(["estimate", "--combinations", "aws-postgres", "--hours", "1"])
    assert code == 2


def test_a_destroyed_environment_is_a_leftover_while_a_sibling_of_its_project_is_live():
    table = listers(
        **{
            "aws:tagged": lambda: [
                found("tagged", "arn-live", "acme", "prod"),
                found("tagged", "arn-dead", "acme", "old"),
                found("tagged", "arn-noenv", "acme"),
            ]
        }
    )
    result = sweep(
        [entry(env="prod"), entry(env="old", destroyed_at="2026-11-19T11:00:00+00:00")], table
    )
    assert [(r.name, r.env) for r in result.leftovers] == [("arn-dead", "old")]


def test_a_leftover_line_names_the_environment_when_known(tmp_path, capsys):
    table = listers(**{"aws:role": lambda: [found("role", "r", "acme", "old")]})
    created = write_created(tmp_path, entry(env="prod"))
    code = mod().main(["sweep", "--created", str(created)], listers=table)
    assert code == 1
    assert "aws role r (project acme, env old)" in capsys.readouterr().out


SUBSCRIPTION = "/subscriptions/00000000-0000-0000-0000-000000000000"
ACME = {"vecten:project": "acme", "vecten:env": "prod"}
GONE = {"vecten:project": "gone", "vecten:env": "prod"}


def azure_outputs(m):
    """`az group list`, `az resource list` and `az keyvault list-deleted` as documented."""
    return {
        tuple(m.AZ_GROUPS): [
            {"name": "vecten-acme-prod", "location": "polandcentral", "tags": ACME},
            {"name": "NetworkWatcherRG", "location": "polandcentral", "tags": None},
        ],
        tuple(m.AZ_RESOURCES): [
            {
                "id": f"{SUBSCRIPTION}/resourceGroups/VECTEN-ACME-PROD/providers/x/y/web",
                "name": "web",
                "resourceGroup": "VECTEN-ACME-PROD",
                "tags": ACME,
            },
            {
                "id": f"{SUBSCRIPTION}/resourceGroups/vecten-livecheck-dns/providers/x/y/stray",
                "name": "stray",
                "resourceGroup": "vecten-livecheck-dns",
                "tags": GONE,
            },
            {
                "id": f"{SUBSCRIPTION}/resourceGroups/vecten-livecheck-dns/providers/x/y/zone",
                "name": "zone",
                "resourceGroup": "vecten-livecheck-dns",
                "tags": {},
            },
        ],
        tuple(m.AZ_DELETED_VAULTS): [
            {
                "name": "vabcdef0123456-mcp",
                "properties": {"location": "polandcentral", "tags": GONE},
            },
            {"name": "someone-elses", "properties": {"location": "polandcentral", "tags": {}}},
        ],
    }


def test_azure_groups_resources_and_deleted_vaults_are_found_by_tag(monkeypatch):
    m = mod()
    table = real(monkeypatch, azure_outputs(m))
    assert table[("azure", "resource-group")]() == [
        found("resource-group", "vecten-acme-prod", "acme", "prod")
    ]
    assert table[("azure", "resource")]() == [
        found(
            "resource",
            f"{SUBSCRIPTION}/resourceGroups/vecten-livecheck-dns/providers/x/y/stray",
            "gone",
            "prod",
        )
    ]
    assert table[("azure", "deleted-vault")]() == [
        found("deleted-vault", "vabcdef0123456-mcp", "gone", "prod")
    ]


def test_a_failing_azure_listing_is_a_failed_listing(monkeypatch):
    m = mod()
    table = real(monkeypatch, {tuple(m.AZ_GROUPS): Fail("AuthorizationFailed")})
    result = sweep([entry()], {("azure", "resource-group"): table[("azure", "resource-group")]})
    assert result.failed == [
        "could not list azure resource-group: az exited 1: AuthorizationFailed"
    ]


def purging(monkeypatch, outputs, failing=()):
    """Answer listings from `outputs`; record each purge, failing those of the named vaults."""
    m = mod()
    purged = []

    def run(cmd, **kw):
        if cmd[: len(m.AZ_PURGE)] == m.AZ_PURGE:
            purged.append(cmd)
            name = cmd[cmd.index("--name") + 1]
            return Run(returncode=1, stderr="Conflict") if name in failing else Run()
        return Run(stdout=json.dumps(outputs[tuple(cmd)]))

    monkeypatch.setattr(m.subprocess, "run", run)
    return purged


def vaults(*entries):
    return [
        {"name": name, "properties": {"location": "polandcentral", "tags": tags}}
        for name, tags in entries
    ]


DESTROYED = "2026-11-19T11:00:00+00:00"
STRAY = {"vecten:project": "stray", "vecten:env": "prod"}


def test_purge_requests_only_the_deleted_vaults_of_the_runs_destroyed_environments(
    tmp_path, monkeypatch, capsys
):
    m = mod()
    listed = vaults(("vlive-mcp", ACME), ("vgone-mcp", GONE), ("vstray-mcp", STRAY))
    purged = purging(monkeypatch, {tuple(m.AZ_DELETED_VAULTS): listed})
    table = listers(
        **{"azure:deleted-vault": m.real_listers(lambda n: "")[("azure", "deleted-vault")]}
    )
    created = write_created(
        tmp_path,
        entry(combination="azure-postgres"),
        entry(project="gone", combination="azure-postgres", destroyed_at=DESTROYED),
    )
    code = m.main(["sweep", "--created", str(created), "--purge-deleted-vaults"], listers=table)
    out = capsys.readouterr().out
    assert purged == [[*m.AZ_PURGE, "--name", "vgone-mcp", "--location", "polandcentral"]]
    assert "purge requested: vgone-mcp" in out
    assert "leftover: azure deleted-vault vstray-mcp (project stray, env prod)" in out
    assert code == 1


def test_without_the_flag_nothing_is_purged(tmp_path, monkeypatch):
    m = mod()
    purged = purging(monkeypatch, {tuple(m.AZ_DELETED_VAULTS): vaults(("vgone-mcp", GONE))})
    table = listers(
        **{"azure:deleted-vault": m.real_listers(lambda n: "")[("azure", "deleted-vault")]}
    )
    created = write_created(tmp_path, entry())
    assert m.main(["sweep", "--created", str(created)], listers=table) == 1
    assert purged == []


def test_a_failed_purge_exits_4_and_names_the_vault(tmp_path, monkeypatch, capsys):
    m = mod()
    listed = vaults(("vgone-mcp", GONE), ("vgone-setup", GONE))
    purging(monkeypatch, {tuple(m.AZ_DELETED_VAULTS): listed}, failing={"vgone-mcp"})
    table = listers(
        **{"azure:deleted-vault": m.real_listers(lambda n: "")[("azure", "deleted-vault")]}
    )
    created = write_created(tmp_path, entry(project="gone", destroyed_at=DESTROYED))
    code = m.main(
        ["sweep", "--created", str(created), "--purge-deleted-vaults", "--json"], listers=table
    )
    document = json.loads(capsys.readouterr().out)
    assert code == 4
    assert document["purge_requested"] == ["vgone-setup"]
    assert document["failed"] == [
        "could not purge azure deleted-vault vgone-mcp: az exited 1: Conflict"
    ]
