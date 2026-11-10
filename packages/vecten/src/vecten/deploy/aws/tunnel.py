"""SSM port forwarding from the laptop to a host inside the VPC, as an injectable runner
(`VECTEN_TUNNEL_RUNNER`)."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol

from vecten.cli.injection import load_runner
from vecten.deploy.terraform import redacted_tail
from vecten_shared.output import ExitCode, Failure

TUNNEL_RUNNER_VAR = "VECTEN_TUNNEL_RUNNER"
DOCUMENT = "AWS-StartPortForwardingSessionToRemoteHost"
PLUGIN = "session-manager-plugin"
PLUGIN_INSTALL = (
    "https://docs.aws.amazon.com/systems-manager/latest/userguide/"
    "session-manager-working-with-install-plugin.html"
)
AWS_CLI_INSTALL = "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
READY_TIMEOUT = 45
POLL_INTERVAL = 0.5
STOP_TIMEOUT = 5


class TunnelRunner(Protocol):
    def open(
        self, instance_id: str, remote_host: str, remote_port: int, local_port: int, region: str
    ) -> AbstractContextManager[None]: ...


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


class TunnelHangup(Exception):
    """The terminal closed while the tunnel was open."""


def _hangup(signum, frame) -> None:
    raise TunnelHangup("the terminal closed while the SSM tunnel was open")


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError, PermissionError:
        # macOS answers EPERM for a group whose leader has exited but is not reaped yet;
        # either way no member is left to signal.
        pass


def stop_group(process: subprocess.Popen) -> None:
    """SIGTERM the process's group, then SIGKILL it after `STOP_TIMEOUT` seconds, and reap.

    The process leads its own session, so its pid is the group id. The group is signalled
    even when the leader has exited, since the plugin it started may still be running.
    """
    running = process.poll() is None
    _signal_group(process.pid, signal.SIGTERM)
    if running:
        try:
            process.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass
    # The plugin can outlive `aws` after SIGTERM; the group is killed either way.
    _signal_group(process.pid, signal.SIGKILL)
    process.wait()


def _watch_hangup():
    """Install `_hangup` for SIGHUP and return the handler to restore; None when nothing was
    installed: off the main thread, where Python cannot install handlers, or when SIGHUP is
    already ignored (under `nohup`), where a hangup ends nothing.

    The session runs in its own process group, so closing the terminal no longer reaches
    it; the handler turns the hangup into an exception, so the tunnel's clean-up still runs.
    """
    if threading.current_thread() is not threading.main_thread():
        return None
    previous = signal.getsignal(signal.SIGHUP)
    if previous is signal.SIG_IGN:
        return None
    signal.signal(signal.SIGHUP, _hangup)
    return signal.SIG_DFL if previous is None else previous


class SessionManagerTunnel:
    """`aws ssm start-session` with the port-forwarding document, for as long as the context
    lasts; the session uses whatever AWS login boto3 would."""

    @contextmanager
    def open(
        self, instance_id: str, remote_host: str, remote_port: int, local_port: int, region: str
    ) -> Iterator[None]:
        if shutil.which("aws") is None:
            raise Failure(
                ExitCode.PREREQUISITE,
                f"the AWS CLI is not installed; the tunnel needs it: {AWS_CLI_INSTALL}",
            )
        if shutil.which(PLUGIN) is None:
            raise Failure(
                ExitCode.PREREQUISITE,
                f"`{PLUGIN}` is not installed; the tunnel needs it: {PLUGIN_INSTALL}",
            )
        args = [
            "aws",
            "ssm",
            "start-session",
            "--target",
            instance_id,
            "--document-name",
            DOCUMENT,
            "--parameters",
            f"host={remote_host},portNumber={remote_port},localPortNumber={local_port}",
            "--region",
            region,
        ]
        # stderr goes to a file: a pipe nobody reads while the session runs could fill and
        # stall the plugin.
        with tempfile.TemporaryFile() as errors:
            previous = None
            process = None
            try:
                # Installed before the session starts, so no hangup falls between the two.
                previous = _watch_hangup()
                # `aws` starts session-manager-plugin as a child that holds the local port;
                # signalling `aws` alone would leave it running, so the whole group is stopped.
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                    start_new_session=True,
                )
                self._wait_ready(process, local_port, errors)
                yield
            finally:
                if previous is not None:
                    # A second hangup must not interrupt the group kill.
                    signal.signal(signal.SIGHUP, signal.SIG_IGN)
                try:
                    if process is not None:
                        stop_group(process)
                finally:
                    if previous is not None:
                        signal.signal(signal.SIGHUP, previous)

    def _wait_ready(self, process, local_port: int, errors) -> None:
        deadline = time.monotonic() + READY_TIMEOUT
        while True:
            if port_open(local_port):
                return
            code = process.poll()
            if code is not None or time.monotonic() > deadline:
                errors.seek(0)
                tail = redacted_tail(errors.read().decode("utf-8", "replace"), 5)
                what = (
                    f"exited with code {code}"
                    if code is not None
                    else f"did not open local port {local_port} within {READY_TIMEOUT} s"
                )
                message = f"the SSM port-forwarding session {what}"
                raise Failure(
                    ExitCode.TOOL_FAILED, message + (f": {'; '.join(tail)}" if tail else "")
                )
            time.sleep(POLL_INTERVAL)


def get_tunnel() -> TunnelRunner:
    return load_runner(TUNNEL_RUNNER_VAR, SessionManagerTunnel)
