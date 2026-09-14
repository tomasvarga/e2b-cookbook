from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from unittest.mock import patch

from e2b import AuthenticationException, CommandExitException, TimeoutException

from main import (
    DETAIL_TAIL,
    RUN_METADATA,
    SANDBOX_TIMEOUT,
    SKILL_NAME,
    _failure_detail,
    main,
    run_demo,
)


@dataclass
class RecordedResult:
    exit_code: int = 0
    stdout: str = "ok\n"
    stderr: str = ""


class RecordingCommands:
    def __init__(
        self,
        *,
        fail_first_agent: bool = False,
        fail_skill_check: bool = False,
    ) -> None:
        self.commands: list[str] = []
        self.fail_first_agent = fail_first_agent
        self.fail_skill_check = fail_skill_check
        self.agent_runs = 0

    def run(self, command: str, **_: object) -> RecordedResult:
        self.commands.append(command)
        if command.startswith("hermes chat"):
            self.agent_runs += 1
            if self.fail_first_agent and self.agent_runs == 1:
                raise CommandExitException(
                    stdout="API call failed after 3 retries: provider failed",
                    stderr="\u26a0 Deprecated .env settings detected",
                    exit_code=1,
                    error=None,
                )
        if command.startswith("find "):
            if self.fail_skill_check:
                raise CommandExitException(
                    stderr="skills directory missing",
                    stdout="",
                    exit_code=1,
                    error=None,
                )
            return RecordedResult(
                stdout=f"/home/user/.hermes/skills/{SKILL_NAME}/SKILL.md\n"
            )
        return RecordedResult()


class RecordingFiles:
    def __init__(self) -> None:
        self.writes: dict[str, str] = {}

    def write(self, path: str, content: str) -> None:
        self.writes[path] = content


class RecordingSandbox:
    def __init__(
        self,
        *,
        fail_first_agent: bool = False,
        fail_skill_check: bool = False,
    ) -> None:
        self.commands = RecordingCommands(
            fail_first_agent=fail_first_agent,
            fail_skill_check=fail_skill_check,
        )
        self.files = RecordingFiles()
        self.killed = False

    def kill(self) -> None:
        self.killed = True


class RecordingFactory:
    def __init__(self, sandbox: RecordingSandbox) -> None:
        self.sandbox = sandbox
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, template: str, **options: object) -> RecordingSandbox:
        self.calls.append((template, options))
        return self.sandbox


class HermesIncidentPlaybookTests(unittest.TestCase):
    def _example(self, root: Path) -> Path:
        example = root / "example"
        (example / "incidents").mkdir(parents=True)
        (example / "runbook.md").write_text("triage steps\n", encoding="utf-8")
        (example / "incidents" / "checkout-latency.json").write_text(
            '{"incident_id":"INC-1"}\n', encoding="utf-8"
        )
        (example / "incidents" / "checkout-errors.json").write_text(
            '{"incident_id":"INC-2"}\n', encoding="utf-8"
        )
        return example

    def test_learns_and_reuses_the_incident_skill(self) -> None:
        sandbox = RecordingSandbox()
        factory = RecordingFactory(sandbox)
        environment = {
            "E2B_API_KEY": "e2b_test",
            "OPENROUTER_API_KEY": "or_test",
            "HERMES_PROVIDER": "openrouter",
            "HERMES_MODEL": "test/model",
            "HERMES_TEMPLATE": "hermes:smoke",
        }

        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(StringIO()):
                run_demo(
                    environ=environment,
                    sandbox_factory=factory,
                    example_directory=self._example(Path(directory)),
                )

        self.assertEqual(factory.calls[0][0], "hermes:smoke")
        self.assertEqual(
            factory.calls[0][1]["envs"],
            {"OPENROUTER_API_KEY": "or_test"},
        )
        self.assertEqual(factory.calls[0][1]["metadata"], RUN_METADATA)
        self.assertEqual(factory.calls[0][1]["timeout"], SANDBOX_TIMEOUT)
        agent_commands = [
            command
            for command in sandbox.commands.commands
            if command.startswith("hermes chat")
        ]
        self.assertEqual(len(agent_commands), 2)
        self.assertIn("--yolo", agent_commands[0])
        self.assertIn("--model test/model", agent_commands[0])
        self.assertNotIn("--skills", agent_commands[0])
        self.assertIn(f"--skills {SKILL_NAME}", agent_commands[1])
        self.assertEqual(len(sandbox.files.writes), 3)
        self.assertTrue(sandbox.killed)

    def test_kills_the_sandbox_when_hermes_fails(self) -> None:
        sandbox = RecordingSandbox(fail_first_agent=True)
        factory = RecordingFactory(sandbox)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                with redirect_stdout(StringIO()):
                    run_demo(
                        environ={
                            "E2B_API_KEY": "e2b_test",
                            "OPENROUTER_API_KEY": "or_test",
                        },
                        sandbox_factory=factory,
                        example_directory=self._example(Path(directory)),
                    )

        self.assertTrue(sandbox.killed)

    def test_kills_the_sandbox_when_the_skill_check_fails(self) -> None:
        sandbox = RecordingSandbox(fail_skill_check=True)
        factory = RecordingFactory(sandbox)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "did not create the expected"):
                with redirect_stdout(StringIO()):
                    run_demo(
                        environ={
                            "E2B_API_KEY": "e2b_test",
                            "OPENROUTER_API_KEY": "or_test",
                        },
                        sandbox_factory=factory,
                        example_directory=self._example(Path(directory)),
                    )

        self.assertTrue(sandbox.killed)

    def test_surfaces_the_real_cause_not_just_the_startup_warning(self) -> None:
        # The hermes template always writes a deprecation warning to stderr, so
        # reporting stderr alone would hide the real failure on stdout.
        sandbox = RecordingSandbox(fail_first_agent=True)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError) as caught:
                with redirect_stdout(StringIO()):
                    run_demo(
                        environ={
                            "E2B_API_KEY": "e2b_test",
                            "OPENROUTER_API_KEY": "or_test",
                        },
                        sandbox_factory=RecordingFactory(sandbox),
                        example_directory=self._example(Path(directory)),
                    )

        message = str(caught.exception)
        self.assertIn("API call failed after 3 retries", message)
        self.assertIn("Deprecated .env settings", message)
        self.assertTrue(sandbox.killed)

    def test_bounds_a_noisy_failure_detail(self) -> None:
        # Under -Q the real template still prints tool diffs to stdout, so a
        # failed turn produced a ~4.5k-character message. The cause is the last
        # thing written, so the tail is what has to survive truncation.
        noise = "+ review diff line\n" * 400
        error = CommandExitException(
            stdout=noise + "API call failed after 3 retries: Gemini HTTP 503",
            stderr="",
            exit_code=1,
            error=None,
        )
        detail = _failure_detail(error)
        self.assertIn("Gemini HTTP 503", detail)
        self.assertLessEqual(len(detail), DETAIL_TAIL + 3)
        self.assertTrue(detail.startswith("..."))

    def test_reports_sdk_failures_without_a_traceback(self) -> None:
        # A bad key, an unreachable template, a killed sandbox, or a turn that
        # outran its timeout must all exit with a message, not a traceback.
        for error in (
            TimeoutException("sandbox timed out"),
            AuthenticationException("invalid api key"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch("main.run_demo", side_effect=error):
                    with self.assertRaises(SystemExit) as caught:
                        main()
                self.assertEqual(str(caught.exception), str(error))

    def test_requires_e2b_and_provider_credentials(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Missing E2B_API_KEY"):
            run_demo(environ={}, sandbox_factory=RecordingFactory(RecordingSandbox()))

        with self.assertRaisesRegex(RuntimeError, "Missing OPENROUTER_API_KEY"):
            run_demo(
                environ={"E2B_API_KEY": "e2b_test"},
                sandbox_factory=RecordingFactory(RecordingSandbox()),
            )
