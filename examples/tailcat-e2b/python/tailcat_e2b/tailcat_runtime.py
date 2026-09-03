"""Helpers for running Tailcat inside E2B sandboxes and on this machine.

A Tailcat *server* (listener) prints an address; a Tailcat *client* connects to
it. Both sides can live in a sandbox or on the laptop. This module hides the
three things that are easy to get wrong: reading the address, waiting until the
server is actually reachable, and turning on direct paths.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

from e2b import CommandExitException, Sandbox

from .template import ENABLE_DIRECT_PATHS_CMD, TAILCAT_URL, TEMPLATE_ALIAS

# A Tailcat address looks like `tco2FwWCD...`: the prefix `tc` followed by base64url.
TAILCAT_ADDRESS_PATTERN = re.compile(r"\btc[A-Za-z0-9_-]{20,}")

INSTALL_TAILCAT_CMD = (
    f"curl -fsSL -o /tmp/tailcat.tgz {TAILCAT_URL} && mkdir -p /tmp/tc && tar xzf /tmp/tailcat.tgz -C /tmp/tc"
    " && sudo install -m 0755 $(find /tmp/tc -type f -name tailcat) /usr/local/bin/tailcat && rm -rf /tmp/tc /tmp/tailcat.tgz"
)

# Keep our prints in order with the output of the tailcat subprocesses.
sys.stdout.reconfigure(line_buffering=True)


@dataclass
class CommandResult:
    exit_code: int
    output: str
    """stdout and stderr, concatenated."""


CommandRunner = Callable[[str], CommandResult]
"""Runs a shell command locally or in a sandbox and captures its exit code and combined output."""


@dataclass
class TailcatServer:
    """A running Tailcat server and how to stop it."""

    address: str
    stop: Callable[[], None]


# ---------------------------------------------------------------------------
# Sandboxes
# ---------------------------------------------------------------------------


def create_sandbox(timeout_seconds: int = 600) -> Sandbox:
    """Create a sandbox with Tailcat installed and direct paths enabled."""
    started_at = time.time()
    sandbox = _create_sandbox_from_template_or_fallback(timeout_seconds)
    print(f"[{sandbox.sandbox_id}] sandbox ready in {time.time() - started_at:.1f}s")

    # Idempotent: the template's start command already does this, but sandboxes
    # made from other templates need it too. See README, "Quirks".
    sandbox.commands.run(ENABLE_DIRECT_PATHS_CMD)
    return sandbox


def _create_sandbox_from_template_or_fallback(timeout_seconds: int) -> Sandbox:
    """Use the `tailcat` template when this team has built it, else a default sandbox with Tailcat installed on the fly."""
    try:
        return Sandbox.create(TEMPLATE_ALIAS, timeout=timeout_seconds)
    except Exception as error:
        template_missing = "404" in str(error)
        if not template_missing:
            raise
    sandbox = Sandbox.create(timeout=timeout_seconds)
    sandbox.commands.run(INSTALL_TAILCAT_CMD, timeout=120)
    print(
        f"[{sandbox.sandbox_id}] template '{TEMPLATE_ALIAS}' not found, installed tailcat at runtime instead "
        "(run `poetry run build-template` once to skip this)"
    )
    return sandbox


def run_sandbox_command(sandbox: Sandbox, command: str, timeout_seconds: int = 60) -> CommandResult:
    """Run a command in the sandbox without raising on a non-zero exit."""
    try:
        result = sandbox.commands.run(command, timeout=timeout_seconds)
        return CommandResult(result.exit_code, result.stdout + result.stderr)
    except CommandExitException as error:
        return CommandResult(error.exit_code, error.stdout + error.stderr)


def create_sandbox_command_runner(sandbox: Sandbox, timeout_seconds: int = 120) -> CommandRunner:
    """Create a command runner bound to one sandbox."""

    def run_in_sandbox(command: str) -> CommandResult:
        return run_sandbox_command(sandbox, command, timeout_seconds)

    return run_in_sandbox


def local_command_runner(command: str) -> CommandResult:
    completed_process = subprocess.run(command, shell=True, capture_output=True, text=True)
    return CommandResult(completed_process.returncode, completed_process.stdout + completed_process.stderr)


def require_successful_command(result: CommandResult) -> None:
    """Raise with the command output when a command did not exit 0."""
    if result.exit_code != 0:
        raise RuntimeError(f"command failed (exit {result.exit_code}):\n{result.output}")


# ---------------------------------------------------------------------------
# Tailcat servers
# ---------------------------------------------------------------------------


def start_sandbox_tailcat_server(
    sandbox: Sandbox,
    tailcat_arguments: str,
    instance_name: str = "tailcat",
    wait_seconds: float = 20,
    stdout_file: str | None = None,
) -> TailcatServer:
    """Start `tailcat <tailcat_arguments>` in the background inside the sandbox and return its address.

    `tailcat_arguments` is passed to the shell unsplit. Tailcat writes the
    address to TAILCAT_ADDR_FILE, which is more reliable than scraping stderr
    from a background command. `stdout_file` redirects the server's stdout
    (bare `tailcat` copies its one connection there).
    """
    address_file = f"/tmp/{instance_name}.addr"
    log_file = f"/tmp/{instance_name}.log"
    server_process = sandbox.commands.run(
        f"rm -f {address_file}; TAILCAT_ADDR_FILE={address_file} tailcat {tailcat_arguments} > {stdout_file or log_file} 2> {log_file}",
        background=True,
    )

    def read_address_file() -> str:
        return run_sandbox_command(sandbox, f"cat {address_file} 2>/dev/null").output

    def read_server_log() -> str:
        return run_sandbox_command(sandbox, f"cat {log_file}").output

    def stop() -> None:
        server_process.kill()

    address = _wait_for_tailcat_address(
        read_address_file=read_address_file,
        wait_seconds=wait_seconds,
        read_server_log=read_server_log,
    )
    print(f"[{sandbox.sandbox_id}] tailcat {tailcat_arguments}  ->  {_abbreviate(address)}")
    return TailcatServer(address, stop=stop)


def start_local_tailcat_server(tailcat_arguments: str, wait_seconds: float = 20) -> TailcatServer:
    """Start `tailcat <tailcat_arguments>` on this machine and return its address. `tailcat_arguments` is split on whitespace."""
    require_local_tailcat()
    address_file = os.path.join(tempfile.gettempdir(), f"tailcat-{os.getpid()}-{int(time.time() * 1000)}.addr")
    server_process = subprocess.Popen(
        ["tailcat", *tailcat_arguments.split()],
        env={**os.environ, "TAILCAT_ADDR_FILE": address_file},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def read_address_file() -> str:
        if not os.path.exists(address_file):
            return ""
        with open(address_file) as file:
            return file.read()

    def stop() -> None:
        server_process.terminate()
        if os.path.exists(address_file):
            os.unlink(address_file)

    try:
        address = _wait_for_tailcat_address(read_address_file, wait_seconds)
    except Exception:
        server_process.terminate()
        raise
    print(f"[laptop] tailcat {tailcat_arguments}  ->  {_abbreviate(address)}")
    return TailcatServer(address, stop)


def _wait_for_tailcat_address(
    read_address_file: Callable[[], str],
    wait_seconds: float,
    read_server_log: Callable[[], str] | None = None,
) -> str:
    """Poll `read_address_file` until it contains a Tailcat address."""
    started_at = time.time()
    while time.time() - started_at < wait_seconds:
        if address_match := TAILCAT_ADDRESS_PATTERN.search(read_address_file()):
            return address_match.group(0)
        time.sleep(0.3)
    server_log = f"\n{read_server_log()}" if read_server_log else ""
    raise RuntimeError(f"tailcat did not print an address within {wait_seconds}s{server_log}")


# ---------------------------------------------------------------------------
# Tailcat clients
# ---------------------------------------------------------------------------


def wait_until_reachable(run_command: CommandRunner, address: str, timeout_seconds: float = 30) -> None:
    """Block until a Tailcat server answers a ping from the client side.

    A Tailcat server prints its address before it has finished connecting to
    the DERP relay. A client that connects in that window is told the relay
    does not know the peer, so retry instead of trusting the first attempt.
    """
    started_at = time.time()
    while time.time() - started_at < timeout_seconds:
        ping = run_command(f"tailcat ping --timeout 3s {address}")
        if ping.exit_code == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"tailcat server {_abbreviate(address)} not reachable after {timeout_seconds}s")


def describe_network_path(run_command: CommandRunner, address: str, timeout: str = "20s") -> str:
    """Ping until a direct path forms and describe the result: relay latency, direct latency, or neither."""
    ping = run_command(f"tailcat ping --until-direct --timeout {timeout} {address}")
    pong_lines = [line for line in ping.output.splitlines() if "pong in" in line]
    relay_latency = next((line.split("pong in ")[1] for line in pong_lines if "DERP" in line), None)
    direct_latency = next((line.split("pong in ")[1] for line in pong_lines if "DERP" not in line), None)
    if direct_latency:
        return f"direct path: {direct_latency}" + (f" (relay was {relay_latency})" if relay_latency else "")
    if relay_latency:
        return f"relay only: {relay_latency} (no direct path within {timeout})"
    return f"no pong: {ping.output.strip()[-200:]}"


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def require_local_tailcat() -> None:
    if not shutil.which("tailcat"):
        raise SystemExit(
            "tailcat is not installed on this machine. Install it with `brew install tailcat` "
            "or see https://github.com/tailscale/tailcat#install"
        )


def compute_local_md5(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def compute_sandbox_md5(sandbox: Sandbox, path: str) -> str:
    result = run_sandbox_command(sandbox, f"md5sum {path}")
    require_successful_command(result)
    return result.output.split()[0]


def format_megabits_per_second(size_bytes: int, seconds: float) -> str:
    return f"{size_bytes * 8 / seconds / 1e6:.0f} Mbit/s"


def seconds_since(started_at: float) -> float:
    return time.time() - started_at


def _abbreviate(address: str) -> str:
    return f"{address[:20]}…"
