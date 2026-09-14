"""Teach Hermes an incident playbook, then reuse it in a fresh session."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from e2b import AuthenticationException, CommandExitException, SandboxException

EXAMPLE_DIRECTORY = Path(__file__).resolve().parent
SANDBOX_WORKSPACE = "/home/user/incident-workspace"
SKILL_NAME = "incident-triage"
KEY_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
AGENT_TIMEOUT = 600
# Allow two sequential agent turns plus workspace setup and file transfers.
SANDBOX_TIMEOUT = 1500
RUN_METADATA = {"example": "hermes-incident-playbook-python"}
# Hermes still writes tool diffs to stdout under -Q, so bound what a failure reports.
DETAIL_TAIL = 600


class CommandResult(Protocol):
    exit_code: int
    stdout: str
    stderr: str


class SandboxLike(Protocol):
    commands: Any
    files: Any

    def kill(self) -> None: ...


SandboxFactory = Callable[..., SandboxLike]


def _settings(environment: Mapping[str, str]) -> tuple[str, str, dict[str, str]]:
    if not environment.get("E2B_API_KEY"):
        raise RuntimeError(
            "Missing E2B_API_KEY. Copy .env.example to .env and load it."
        )

    provider = environment.get("HERMES_PROVIDER", "openrouter")
    model = environment.get(
        "HERMES_MODEL",
        "anthropic/claude-sonnet-4.6",
    )
    key_name = environment.get(
        "HERMES_PROVIDER_KEY_ENV",
        "OPENROUTER_API_KEY",
    )
    if not KEY_NAME_PATTERN.fullmatch(key_name):
        raise RuntimeError("HERMES_PROVIDER_KEY_ENV must be an environment name.")

    key = environment.get(key_name)
    if not key:
        raise RuntimeError(f"Missing {key_name} for the Hermes model provider.")

    return provider, model, {key_name: key}


def _hermes_command(
    *,
    provider: str,
    model: str,
    prompt: str,
    skill: str | None = None,
) -> str:
    arguments = [
        "hermes",
        "chat",
        "-Q",
        "--yolo",
        "--provider",
        provider,
        "--model",
        model,
        "--toolsets",
        "terminal,file,skills,memory",
    ]
    if skill:
        arguments.extend(["--skills", skill])
    arguments.extend(["-q", prompt])
    return shlex.join(arguments)


def _tail(stream: str, limit: int = DETAIL_TAIL) -> str:
    text = (stream or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def _failure_detail(error: CommandExitException) -> str:
    # Hermes writes its real failure to stdout and its warnings to stderr, and
    # the template always emits a startup warning, so reporting stderr alone
    # would surface that warning and hide the cause. Even under -Q, Hermes
    # still prints tool diffs, so keep the tail of each stream: the failure is
    # the last thing written, not the first.
    parts = [tail for tail in (_tail(error.stdout), _tail(error.stderr)) if tail]
    return " | ".join(parts) or str(error)


def _run_agent(sandbox: SandboxLike, command: str) -> str:
    try:
        result: CommandResult = sandbox.commands.run(
            command,
            timeout=AGENT_TIMEOUT,
        )
    except CommandExitException as error:
        raise RuntimeError(f"Hermes failed: {_failure_detail(error)}") from error
    return result.stdout


def run_demo(
    *,
    environ: Mapping[str, str] | None = None,
    sandbox_factory: SandboxFactory | None = None,
    example_directory: Path = EXAMPLE_DIRECTORY,
) -> None:
    """Run two independent Hermes sessions in one persistent E2B sandbox."""
    environment = os.environ if environ is None else environ
    provider, model, provider_env = _settings(environment)
    template = environment.get("HERMES_TEMPLATE", "hermes")

    if sandbox_factory is None:
        from e2b import Sandbox

        sandbox_factory = Sandbox.create

    sandbox = sandbox_factory(
        template,
        envs=provider_env,
        metadata=RUN_METADATA,
        timeout=SANDBOX_TIMEOUT,
    )
    try:
        try:
            sandbox.commands.run(
                f"mkdir -p {SANDBOX_WORKSPACE}/incidents "
                f"{SANDBOX_WORKSPACE}/reports"
            )
        except CommandExitException as error:
            raise RuntimeError(
                f"Could not prepare workspace: {_failure_detail(error)}"
            ) from error

        sandbox.files.write(
            f"{SANDBOX_WORKSPACE}/runbook.md",
            (example_directory / "runbook.md").read_text(encoding="utf-8"),
        )
        first_incident = example_directory / "incidents" / "checkout-latency.json"
        sandbox.files.write(
            f"{SANDBOX_WORKSPACE}/incidents/{first_incident.name}",
            first_incident.read_text(encoding="utf-8"),
        )

        first_prompt = f"""
You are the on-call investigator. Read {SANDBOX_WORKSPACE}/runbook.md and
{SANDBOX_WORKSPACE}/incidents/checkout-latency.json. Use terminal and file tools
to investigate the evidence. Write a concise report to
{SANDBOX_WORKSPACE}/reports/checkout-latency.md. Then use skill_manage to create
a reusable skill named {SKILL_NAME} from the successful procedure, including
verification and rollback decision rules. Save one compact memory noting where
this service's incident inputs and reports live. Do not modify the input files.
""".strip()

        print("\n1/2 Investigating the first incident and learning a playbook")
        first_response = _run_agent(
            sandbox,
            _hermes_command(
                provider=provider,
                model=model,
                prompt=first_prompt,
            ),
        )
        print(first_response.strip())

        try:
            skill_check = sandbox.commands.run(
                "find \"$HOME/.hermes/skills\" -type f "
                f"-path '*/{SKILL_NAME}/SKILL.md' -print -quit"
            )
        except CommandExitException as error:
            raise RuntimeError(
                f"Hermes did not create the expected {SKILL_NAME} skill."
            ) from error
        if not skill_check.stdout.strip():
            raise RuntimeError(
                f"Hermes did not create the expected {SKILL_NAME} skill."
            )

        next_incident = example_directory / "incidents" / "checkout-errors.json"
        sandbox.files.write(
            f"{SANDBOX_WORKSPACE}/incidents/{next_incident.name}",
            next_incident.read_text(encoding="utf-8"),
        )

        second_prompt = f"""
Start a fresh investigation using the preloaded {SKILL_NAME} skill and your
persistent memory. Analyze
{SANDBOX_WORKSPACE}/incidents/checkout-errors.json and write the evidence,
conclusion, and next action to
{SANDBOX_WORKSPACE}/reports/checkout-errors.md. Do not modify the input file.
""".strip()

        print("\n2/2 Applying the learned playbook to a new incident")
        second_response = _run_agent(
            sandbox,
            _hermes_command(
                provider=provider,
                model=model,
                prompt=second_prompt,
                skill=SKILL_NAME,
            ),
        )
        print(second_response.strip())
        print(f"\nReports remain in {SANDBOX_WORKSPACE}/reports until cleanup.")
    finally:
        sandbox.kill()


def main() -> None:
    try:
        run_demo()
    except (RuntimeError, SandboxException, AuthenticationException) as error:
        # Every failure the SDK can raise here is an environment or provider
        # problem, not a bug worth a traceback: a bad key, an unreachable
        # template, a killed sandbox, or a turn that outran its timeout.
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
