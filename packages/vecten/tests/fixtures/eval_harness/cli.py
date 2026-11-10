"""The harness's entry point.

    uv run python packages/vecten/tests/fixtures/eval_harness up <case> [--dir DIR] [--port N]
        [--without-skill SKILL]
    uv run python packages/vecten/tests/fixtures/eval_harness env <case> [--dir DIR]
        [--without-skill SKILL]
    uv run python packages/vecten/tests/fixtures/eval_harness provision-warehouse --project DIR

`up` writes the case's `env.sh`, prints what the human does, and serves the case's hostnames
until interrupted, then removes its containers. The state the grader reads (the gh stand-in's
`calls.jsonl`, the mocked cloud) stays under `<dir>/<case>/`. With `--run <id>` the base is
`<runs>/<id>/` and holds the run's helpers (see `eval_harness.runs`).

The person's commands for runs: `asks [--follow]`, `reply <id> <n> <text>`, `brief <id>`,
`bundle <id> <outputs>` and `teardown <id>`; for grading, `grades`, `prepare-benchmark`,
`finish-benchmark` and `report` (see `eval_harness.grading`).
"""

from __future__ import annotations

import argparse
import json
import os
import platform as host_platform
import shlex
import shutil
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from eval_harness import ca as ca_mod
from eval_harness import gh_standin, issues_replay
from eval_harness import runs as runs_mod
from eval_harness.cases import CASES, LOCAL, SIGN_IN_PASSWORD, Case
from eval_harness.runs import DEFAULT_RUNS, DEMO_API_DOC, Run, write_helpers

DEFAULT_BASE = Path.home() / ".vecten-eval"
HOSTS_TAG = "# vecten-eval-harness"
HARNESS = Path(__file__).resolve().parent
SKILLS = HARNESS.parents[2] / "src/vecten/knowhow/skills"
OPERATE_GRADER = SKILLS / "operate/evals/grader"
MODEL_RAW_SCHEMA = SKILLS / "model/evals/files/raw-schema-demo.json"


@dataclass(frozen=True)
class Paths:
    base: Path
    case: str

    @property
    def dir(self) -> Path:
        return self.base / self.case

    @property
    def ca(self) -> Path:
        # Shared by every case, so trusting it once covers them all.
        return self.base / "ca"

    @property
    def github(self) -> Path:
        return self.dir / "github"

    @property
    def cloud(self) -> Path:
        return self.dir / "cloud"

    @property
    def env_file(self) -> Path:
        return self.dir / "env.sh"

    @property
    def bundle(self) -> Path:
        return self.dir / "ca-bundle.pem"


def environment(case: Case, paths: Paths, base: dict[str, str]) -> tuple[list[str], dict]:
    """(directories to put first on the PATH, other variables) for the case's shells."""
    authority = ca_mod.Authority.create(paths.ca)
    bundle = str(authority.bundle(paths.bundle))
    first: list[str] = []
    values: dict[str, str] = {
        "SSL_CERT_FILE": bundle,
        "REQUESTS_CA_BUNDLE": bundle,
        "CURL_CA_BUNDLE": bundle,
        "NODE_EXTRA_CA_CERTS": str(authority.cert_path),
        # `vecten` caches MCP sessions here; a cache shared between runs carries one run's
        # sign-in into the next.
        "XDG_CONFIG_HOME": str(paths.dir / "config"),
    }
    if case.github:
        found = gh_standin.install(paths.github, python=sys.executable, environ=base)
        first.append(found.pop("PATH").split(os.pathsep)[0])
        values.update(found)
    if case.mock_cloud:
        paths.cloud.mkdir(parents=True, exist_ok=True)
        found = gh_standin.mock_cloud_environ(paths.cloud, {})
        values.update(found)
    if case.issues:
        found = issues_replay.install(
            paths.dir / "vecten-bin",
            fixture=OPERATE_GRADER / f"{case.issues}.json",
            gh_dir=paths.github,
            python=sys.executable,
            environ=base,
        )
        first.insert(0, found.pop("PATH").split(os.pathsep)[0])
        values.update(found)
    return first, values


def write_env(
    case: Case, paths: Paths, base: dict[str, str], extra: dict[str, str] | None = None
) -> Path:
    first, values = environment(case, paths, base)
    values.update(extra or {})
    lines = [f"# Source in every shell of the {paths.case} evaluation: . {paths.env_file}"]
    if first:
        lines.append(f'export PATH={shlex.quote(os.pathsep.join(first))}":$PATH"')
    for name, value in values.items():
        if name == "PYTHONPATH":
            lines.append(f'export PYTHONPATH={shlex.quote(value)}"${{PYTHONPATH:+:$PYTHONPATH}}"')
        else:
            lines.append(f"export {name}={shlex.quote(value)}")
    paths.dir.mkdir(parents=True, exist_ok=True)
    paths.env_file.write_text("\n".join(lines) + "\n")
    return paths.env_file


def unresolved(names: list[str]) -> list[str]:
    missing = []
    for name in names:
        try:
            if socket.gethostbyname(name) != "127.0.0.1":
                missing.append(name)
        except OSError:
            missing.append(name)
    return missing


def print_instructions(case: Case, paths: Paths, port: int = 443) -> None:
    say = print
    say(f"\n== vecten eval harness: {paths.case} ({case.skill} eval {case.eval_id}) ==\n")
    say("1. In the terminal that starts Claude Code, and in the one where you type secrets:")
    say(f"     . {paths.env_file}")
    names = case.hostnames()
    if names:
        sed = "sed -i ''" if host_platform.system() == "Darwin" else "sed -i"
        say("2. Once, as root (the stubs answer on 127.0.0.1):")
        say(f'     echo "127.0.0.1 {" ".join(names)} {HOSTS_TAG}" | sudo tee -a /etc/hosts')
        say(f"   Remove it afterwards: sudo {sed} '/{HOSTS_TAG}$/d' /etc/hosts")
        missing = unresolved(names)
        say(
            f"   Not resolving to 127.0.0.1 yet: {', '.join(missing)}"
            if missing
            else "   All names resolve to 127.0.0.1."
        )
        if port != 443:
            say(f"   Serving on port {port}: the evaluation's URLs carry no port, so use 443.")
    if case.mcp:
        cert = paths.ca / "ca.pem"
        say("3. Optional, so the browser trusts the sign-in page without a warning (root):")
        if host_platform.system() == "Darwin":
            say(
                f"     sudo security add-trusted-cert -d -r trustRoot "
                f"-k /Library/Keychains/System.keychain {cert}"
            )
            say(
                f'   Remove: sudo security delete-certificate -c "{ca_mod.NAME}" '
                "/Library/Keychains/System.keychain"
            )
        else:
            say(
                f"     sudo cp {cert} /usr/local/share/ca-certificates/vecten-eval.crt && "
                "sudo update-ca-certificates"
            )
        if case.harness_passwords:
            say(
                f"   Every person signs in to https://mcp.{case.domain} with the password "
                f"{SIGN_IN_PASSWORD}"
            )
    if case.secrets:
        say("Secrets, typed at the hidden prompt, never pasted into the chat:")
        for secret in case.secrets:
            flag = "" if secret.where == LOCAL else f" --env {secret.where}"
            say(
                f"   vecten secret set {secret.name}{flag}   value: {secret.value}   "
                f"({secret.when})"
            )
    if case.warehouse:
        say("When the agent shows the provisioner script (run from this checkout):")
        say(
            f"   uv run python {HARNESS} provision-warehouse --dir {paths.base} "
            "--project <the run's project folder>"
        )
    for note in case.notes:
        say(f"Note: {note}")
    if case.github:
        say(
            f"The grader's GitHub evidence: {paths.github / 'calls.jsonl'} and "
            f"{paths.github / 'runs'}"
        )
    if case.mock_cloud:
        say(f"The mocked cloud account: {paths.cloud}")
    say("")


def privileged_port_hint(port: int, system: str) -> str:
    if system == "Darwin":
        # macOS has no setting that lets an unprivileged process bind below 1024.
        return (
            f"binding 127.0.0.1:{port} needs root on macOS; serve with `up <case> --port 9443` "
            f"and redirect {port} to it: echo 'rdr pass on lo0 inet proto tcp from any to "
            f"127.0.0.1 port {port} -> 127.0.0.1 port 9443' | sudo pfctl -ef - (undo with "
            "`sudo pfctl -f /etc/pf.conf && sudo pfctl -d`)"
        )
    return (
        f"binding 127.0.0.1:{port} needs privileges here; allow it once with "
        f"`sudo sysctl -w net.ipv4.ip_unprivileged_port_start={port}`"
    )


def check_port(port: int) -> str | None:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except PermissionError:
            return privileged_port_hint(port, host_platform.system())
        except OSError as exc:
            return f"127.0.0.1:{port} is taken ({exc.strerror}); stop what listens there"
    return None


def prepare(case: Case, paths: Paths, without_skill: str | None = None):
    """Build the case's starting project, if it has one, and write its env.sh; what was
    built, or None. `without_skill` builds the baseline project, which never had that skill."""
    from eval_harness import projects

    built = projects.build(case, paths.dir, without_skill)
    write_env(case, paths, dict(os.environ), built.environ if built else None)
    if built is not None:
        print(f"Start the agent in {built.root}")
    return built


def start_warehouse(case: Case, paths: Paths, authority, built, **where):
    """The case's existing warehouse, holding the built project's table when it names one,
    provisioned for that project as its administrator did before the case begins. `where`
    overrides the container's port and name."""
    from eval_harness.cases import SEMANTIC_PROVISIONER
    from eval_harness.warehouse import GUIDE_SEED, ExistingWarehouse
    from vecten_shared.versions import VERSIONS

    secret = next(s for s in case.secrets if s.name == "WAREHOUSE_ADMIN_PASSWORD")
    seed = built.warehouse if built is not None and built.warehouse else GUIDE_SEED
    warehouse = ExistingWarehouse(paths.dir, password=secret.value, seed=seed, **where)
    cert, key = authority.issue([warehouse_host(), "localhost"], paths.dir / "warehouse-tls")
    print(f"starting the warehouse at {warehouse_host()}:{warehouse.port}")
    warehouse.start(cert, key, f"postgres:{VERSIONS.postgres_major}")
    if seed is not GUIDE_SEED:
        environ = dict(os.environ)
        first, extra = environment(case, paths, environ)
        environ.update(extra)
        environ["PATH"] = os.pathsep.join([*first, environ.get("PATH", "")])
        warehouse.provision(built.root, SEMANTIC_PROVISIONER, environ)
        store_cloud_secrets(built.root, paths, SEMANTIC_PROVISIONER)
        print("the warehouse is provisioned and its credentials are in the mocked cloud")
    return warehouse


def store_cloud_secrets(root: Path, paths: Paths, values: dict[str, str]) -> None:
    if str(gh_standin.MOCK_CLOUD_DIR) not in sys.path:
        sys.path.insert(0, str(gh_standin.MOCK_CLOUD_DIR))
    os.environ.update(gh_standin.mock_cloud_environ(paths.cloud, {}))
    from vecten_mock_cloud import MockCloud

    plane = MockCloud(root, "prod")
    for name, value in values.items():
        plane.put_secret(name, value)


def lay_out(run: Run, case: Case) -> None:
    write_helpers(run, case)
    print(f"Run {run.id}: the agent runs commands with {run.base / 'run'} '<command>'")
    print(f"  and reaches the person with {run.base / 'human'} '<message>'")
    print(f"  its project folder is {run.project}")
    print(f"  append {run.prompt_file} to the run's skill-creator prompt")


def up(
    case_name: str,
    base: Path,
    port: int,
    without_skill: str | None = None,
    run: Run | None = None,
) -> int:
    from eval_harness import demo_api
    from eval_harness.front import Front

    case = CASES[case_name]
    paths = Paths(base, case_name)
    built = prepare(case, paths, without_skill)
    if run is not None:
        lay_out(run, case)
    print_instructions(case, paths, port)
    if not case.serves:
        print(
            "This case has nothing to keep running: the stand-ins work in every shell that "
            "sourced env.sh."
        )
        return 0
    problem = check_port(port)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 3
    authority = ca_mod.Authority.create(paths.ca)
    cert, key = authority.issue([*case.hostnames(), "localhost"], paths.dir / "tls")
    front = Front()
    if case.demo_api:
        front.mount("api.demo.test", demo_api.app(demo_api.DemoApi()))
    warehouse = service = joiner = None
    try:
        if case.warehouse:
            from eval_harness.stack_network import Joiner

            warehouse = start_warehouse(case, paths, authority, built)
            joiner = Joiner(warehouse.container, warehouse_host())
            joiner.start()
        if case.mcp:
            from eval_harness.mcp_service import DeployedMcp

            sys.path.insert(0, str(gh_standin.MOCK_CLOUD_DIR))
            os.environ.update(gh_standin.mock_cloud_environ(paths.cloud, {}))
            service = DeployedMcp(
                front,
                cloud_dir=paths.cloud,
                work=paths.dir / "mcp",
                host=f"mcp.{case.domain}",
                password=SIGN_IN_PASSWORD,
                gaps=built.gaps if built is not None else None,
                seed_passwords=case.harness_passwords,
            )
        server = front.server("127.0.0.1", port, cert, key)
        if service is not None:
            service.start()
        names = ", ".join(f"https://{n}" for n in case.hostnames() if n != warehouse_host())

        def announce():
            while not server.started and not server.should_exit:
                time.sleep(0.1)
            if server.started:
                print(
                    f"serving {names} on 127.0.0.1:{port}; Ctrl-C stops and cleans up", flush=True
                )

        threading.Thread(target=announce, daemon=True).start()
        try:
            server.run()
        except KeyboardInterrupt:
            # uvicorn shuts down gracefully on Ctrl-C, then raises the signal again.
            pass
        print("stopped; removing the containers", flush=True)
    finally:
        if service is not None:
            service.stop()
        if joiner is not None:
            joiner.stop()
        if warehouse is not None:
            warehouse.stop()
    return 0


def run_vecten(args: list[str], cwd: Path, environ: dict[str, str], input=None) -> str:
    """One real `vecten` command in the case's environment; its stdout, or SystemExit."""
    import subprocess

    from eval_harness.issues_replay import real_vecten

    command = [*real_vecten(environ.get("PATH", "")), *args]
    result = subprocess.run(
        command, cwd=cwd, env=environ, input=input, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SystemExit(f"`vecten {' '.join(args)}` exited {result.returncode}:\n{result.stdout}")
    return result.stdout


def prepare_model(base: Path, run=run_vecten) -> int:
    """Build the model case's starting project with the steps the `connector` skill ends on,
    against the demo API `up model` serves, then compare the raw schema it landed with the
    committed fixture; 1 when they differ."""
    from eval_harness import projects
    from eval_harness.demo_api import READ_TOKEN

    case, paths = CASES["model"], Paths(base, "model")
    write_env(case, paths, dict(os.environ))
    environ = dict(os.environ)
    first, extra = environment(case, paths, environ)
    environ.update(extra)
    environ["PATH"] = os.pathsep.join([*first, environ.get("PATH", "")])
    root = paths.dir / "proj"
    init = ["init", "proj", "--name", "acme", "--users", "5", "--source", "demo"]
    run([*init, "--warehouse", "postgres", "--json"], paths.dir, environ)
    shutil.copy(DEMO_API_DOC, root / "docs" / "demo-api.md")
    run(["connector", "add", "demo", "--from", "rest", "--json"], root, environ)
    (root / "connectors" / "demo" / "connector.py").write_text(projects.DEMO_CONNECTOR)
    run(["secret", "set", "DEMO_API_KEY"], root, environ, input=READ_TOKEN)
    run(["up", "--json"], root, environ)
    run(["connector", "run", "demo", "--record", "--json"], root, environ)
    landed = json.loads(run(["raw", "schema", "demo", "--json"], root, environ))
    (paths.dir / "raw-schema-landed.json").write_text(json.dumps(landed, indent=2) + "\n")
    drift = projects.schema_drift(landed, json.loads(MODEL_RAW_SCHEMA.read_text()))
    print(f"Start the agent in {root}")
    if not drift:
        print("the raw schema the connector landed matches the committed fixture")
        return 0
    print(f"the raw schema the connector landed differs from {MODEL_RAW_SCHEMA}:")
    for line in drift:
        print(f"  {line}")
    print(f"  the landed schema is in {paths.dir / 'raw-schema-landed.json'}")
    return 1


def warehouse_host() -> str:
    from eval_harness.warehouse import HOST

    return HOST


def provision(base: Path, project: Path) -> int:
    from eval_harness.warehouse import ExistingWarehouse

    case = CASES["guide-2"]
    paths = Paths(base, "guide-2")
    values = {s.name: s.value for s in case.secrets}
    passwords = {n: values[n] for n in ("MCP_PROVISIONER_PASSWORD", "MCP_IMPERSONATOR_PASSWORD")}
    environ = dict(os.environ)
    first, extra = environment(case, paths, environ)
    environ.update(extra)
    environ["PATH"] = os.pathsep.join([*first, environ.get("PATH", "")])
    warehouse = ExistingWarehouse(paths.dir, password=values["WAREHOUSE_ADMIN_PASSWORD"])
    print(warehouse.provision(project.resolve(), passwords, environ))
    print('the provisioner script ran on the warehouse; reply "our administrator ran it"')
    return 0


def _asks(args) -> int:
    if args.follow:
        try:
            runs_mod.follow_asks(args.runs, lambda found: print(found.line(), flush=True))
        except KeyboardInterrupt:
            pass
        return 0
    for found in runs_mod.asks(args.runs):
        if found.reply is None:
            print(found.line())
    return 0


def _reply(args) -> int:
    runs_mod.reply(runs_mod.find_run(args.runs, args.run), args.number, args.text)
    return 0


def _teardown(args) -> int:
    run = runs_mod.find_run(args.runs, args.run)
    environ = dict(os.environ)
    first, extra = environment(CASES[run.case], Paths(run.base, run.case), environ)
    environ.update(extra)
    environ["PATH"] = os.pathsep.join([*first, environ.get("PATH", "")])
    return runs_mod.teardown(run, environ)


def _brief(args) -> int:
    from eval_harness.prompts import grader_brief

    run = runs_mod.find_run(args.runs, args.run)
    print(grader_brief(run, CASES[run.case]), end="")
    return 0


def _bundle(args) -> int:
    from eval_harness.grading import bundle

    args.outputs.mkdir(parents=True, exist_ok=True)
    count = bundle(runs_mod.find_run(args.runs, args.run), args.outputs)
    print(f"{args.outputs}: {count} commands")
    return 0


def _grading(args) -> int:
    from eval_harness import grading

    if args.command == "grades":
        lines = grading.grades(args.workspace)
    elif args.command == "prepare-benchmark":
        lines = grading.prepare_benchmark(args.workspace)
    elif args.command == "finish-benchmark":
        notes = json.loads(args.notes.read_text()) if args.notes else []
        grading.finish_benchmark(
            args.iteration,
            executor=args.executor,
            grader=args.grader,
            skill=args.skill,
            commit=args.commit,
            notes=notes,
        )
        lines = [f"{args.iteration / 'benchmark.json'} updated"]
    else:
        text = grading.report(
            args.iteration, executor=args.executor, grader=args.grader, commit=args.commit
        )
        args.out.write_text(text)
        lines = [f"{args.out}: {len(text)} chars"]
    print("\n".join(lines))
    return 0


PERSON_COMMANDS = {
    "asks": _asks,
    "reply": _reply,
    "brief": _brief,
    "bundle": _bundle,
    "teardown": _teardown,
    "grades": _grading,
    "prepare-benchmark": _grading,
    "finish-benchmark": _grading,
    "report": _grading,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval_harness", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("up", "env"):
        sub = commands.add_parser(name)
        sub.add_argument("case", choices=sorted(CASES))
        where = sub.add_mutually_exclusive_group()
        where.add_argument("--dir", type=Path, default=DEFAULT_BASE)
        where.add_argument(
            "--run", metavar="ID", help="lay out run ID under --runs, with its agent's helpers"
        )
        sub.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
        sub.add_argument(
            "--without-skill",
            metavar="SKILL",
            help="build the baseline project, which never vendored SKILL; use its own --dir",
        )
        if name == "up":
            sub.add_argument("--port", type=int, default=443)
    sub = commands.add_parser("provision-warehouse")
    sub.add_argument("--project", type=Path, required=True)
    sub.add_argument("--dir", type=Path, default=DEFAULT_BASE)
    sub = commands.add_parser("prepare-model")
    sub.add_argument("--dir", type=Path, default=DEFAULT_BASE)
    sub = commands.add_parser("asks", help="the messages agents put to the person")
    sub.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    sub.add_argument("--follow", action="store_true", help="keep printing new ones")
    sub = commands.add_parser("reply", help="the person's reply to one message")
    sub.add_argument("run")
    sub.add_argument("number")
    sub.add_argument("text")
    sub.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    sub = commands.add_parser("brief", help="the grader's brief for a run")
    sub.add_argument("run")
    sub.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    sub = commands.add_parser("bundle", help="a run's evidence for its grader")
    sub.add_argument("run")
    sub.add_argument("outputs", type=Path)
    sub.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    sub = commands.add_parser("grades", help="each graded run's score in a workspace")
    sub.add_argument("workspace", type=Path)
    sub = commands.add_parser("prepare-benchmark", help="runs into run-1/, summaries recomputed")
    sub.add_argument("workspace", type=Path)
    sub = commands.add_parser("finish-benchmark", help="the aggregator's placeholders filled")
    sub.add_argument("iteration", type=Path)
    sub.add_argument("--executor", required=True)
    sub.add_argument("--grader", required=True)
    sub.add_argument("--skill", type=Path, required=True)
    sub.add_argument("--commit", required=True)
    sub.add_argument("--notes", type=Path, help="a JSON list of the analyst's notes")
    sub = commands.add_parser("report", help="an iteration's results as GitHub markdown")
    sub.add_argument("iteration", type=Path)
    sub.add_argument("--executor", required=True)
    sub.add_argument("--grader", required=True)
    sub.add_argument("--commit", required=True)
    sub.add_argument("--out", type=Path, required=True)
    sub = commands.add_parser("teardown", help="`vecten down --volumes` in a run's project")
    sub.add_argument("run")
    sub.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    sub = commands.add_parser("clean")
    sub.add_argument("case", choices=sorted(CASES))
    sub.add_argument("--dir", type=Path, default=DEFAULT_BASE)
    return parser


def command_names() -> set[str]:
    parser = build_parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return set(commands.choices)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in PERSON_COMMANDS:
        return PERSON_COMMANDS[args.command](args)
    base = args.dir.expanduser().resolve()
    run = None
    if getattr(args, "run", None):
        run = Run((args.runs.expanduser() / args.run).resolve(), args.case)
        base = run.base
    if args.command == "up":
        return up(args.case, base, args.port, args.without_skill, run)
    if args.command == "env":
        case = CASES[args.case]
        paths = Paths(base, args.case)
        prepare(case, paths, args.without_skill)
        if run is not None:
            lay_out(run, case)
        print(f". {paths.env_file}")
        return 0
    if args.command == "prepare-model":
        return prepare_model(base)
    if args.command == "provision-warehouse":
        return provision(base, args.project)
    from eval_harness.warehouse import CONTAINER, _docker

    if CASES[args.case].warehouse:
        _docker("rm", "-f", "-v", CONTAINER, check=False)
    shutil.rmtree(Paths(base, args.case).dir, ignore_errors=True)
    print(f"removed {Paths(base, args.case).dir}")
    return 0
