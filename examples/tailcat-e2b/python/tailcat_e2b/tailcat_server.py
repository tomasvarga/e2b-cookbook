"""Start Tailcat listeners and wait until their addresses are reachable."""

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

from e2b import Sandbox

from .e2b_sandbox import CommandRunner, run_sandbox_command

TAILCAT_ADDRESS_PATTERN = re.compile(r"\btc[A-Za-z0-9_-]{20,}")


@dataclass
class TailcatServer:
    address: str
    stop: Callable[[], None]


def start_sandbox_tailcat_server(
    sandbox: Sandbox,
    tailcat_arguments: str,
    instance_name: str = "tailcat",
    wait_seconds: float = 20,
    stdout_file: str | None = None,
) -> TailcatServer:
    """Start a Tailcat listener inside a sandbox and read its generated address."""
    address_file = f"/tmp/{instance_name}.addr"
    log_file = f"/tmp/{instance_name}.log"
    server_process = sandbox.commands.run(
        f"rm -f {address_file}; TAILCAT_ADDR_FILE={address_file} tailcat {tailcat_arguments} "
        f"> {stdout_file or log_file} 2> {log_file}",
        background=True,
    )

    def read_address_file() -> str:
        return run_sandbox_command(sandbox, f"cat {address_file} 2>/dev/null").output

    def read_server_log() -> str:
        return run_sandbox_command(sandbox, f"cat {log_file}").output

    try:
        address = _wait_for_tailcat_address(read_address_file, wait_seconds, read_server_log)
    except Exception:
        server_process.kill()
        raise
    print(f"[{sandbox.sandbox_id}] tailcat {tailcat_arguments}  ->  {_abbreviate(address)}")
    return TailcatServer(address, stop=server_process.kill)


def start_local_tailcat_server(tailcat_arguments: str, wait_seconds: float = 20) -> TailcatServer:
    """Start a Tailcat listener on the laptop and read its generated address."""
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

    try:
        address = _wait_for_tailcat_address(read_address_file, wait_seconds)
    except Exception:
        server_process.terminate()
        raise

    print(f"[laptop] tailcat {tailcat_arguments}  ->  {_abbreviate(address)}")

    def stop() -> None:
        server_process.terminate()
        if os.path.exists(address_file):
            os.unlink(address_file)

    return TailcatServer(address, stop)


def wait_until_reachable(run_command: CommandRunner, address: str, timeout_seconds: float = 60) -> str:
    """Retry Tailcat ping until the listener has joined DERP, then report its path."""
    started_at = time.time()
    while time.time() - started_at < timeout_seconds:
        ping = run_command(f"tailcat ping --timeout 3s {address}")
        if ping.exit_code == 0:
            pong_line = next((line for line in ping.output.splitlines() if "pong in" in line), None)
            if pong_line and "DERP" in pong_line:
                return f"DERP relay: {pong_line.split('pong in ', maxsplit=1)[-1]}"
            return f"Tailcat connection: {pong_line.strip() if pong_line else 'ready'}"
        time.sleep(1)
    raise RuntimeError(f"tailcat server {_abbreviate(address)} not reachable after {timeout_seconds}s")


def require_local_tailcat() -> None:
    if not shutil.which("tailcat"):
        raise RuntimeError(
            "tailcat is not installed; run `brew install tailcat` or see "
            "https://github.com/tailscale/tailcat#install"
        )


def _wait_for_tailcat_address(
    read_address_file: Callable[[], str],
    wait_seconds: float,
    read_server_log: Callable[[], str] | None = None,
) -> str:
    started_at = time.time()
    while time.time() - started_at < wait_seconds:
        if address_match := TAILCAT_ADDRESS_PATTERN.search(read_address_file()):
            return address_match.group(0)
        time.sleep(0.3)
    server_log = f"\n{read_server_log()}" if read_server_log else ""
    raise RuntimeError(f"tailcat did not print an address within {wait_seconds}s{server_log}")


def _abbreviate(address: str) -> str:
    return f"{address[:20]}…"
