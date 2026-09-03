"""Transfer a large file in both directions and verify every copy.

Upload:   the sandbox runs `tailcat recv` (a write-only drop box), the laptop runs `tailcat cp`.
Download: the sandbox runs `tailcat serve files` (read-only), the laptop runs `tailcat ls` and `tailcat cp`.
Bytes flow over WireGuard through a DERP relay and never touch the E2B API.
"""

import json
import os
import subprocess
import tempfile
import time

from dotenv import load_dotenv
from e2b import Sandbox

from .tailcat_runtime import (
    compute_local_md5,
    compute_sandbox_md5,
    create_sandbox,
    describe_connection,
    format_megabits_per_second,
    local_command_runner,
    require_local_tailcat,
    seconds_since,
    start_sandbox_tailcat_server,
    wait_until_reachable,
)

FILE_SIZE_MIB = int(os.environ.get("DEMO_FILE_SIZE_MIB", "50"))
FILE_SIZE_BYTES = FILE_SIZE_MIB * 1024 * 1024


def run_local_tailcat(*tailcat_arguments: str) -> None:
    subprocess.run(["tailcat", *tailcat_arguments], check=True)


def upload_to_sandbox(sandbox: Sandbox, local_file: str, local_input_md5: str) -> None:
    sandbox.commands.run("mkdir -p /home/user/inbox")
    upload_receiver = start_sandbox_tailcat_server(sandbox, "recv /home/user/inbox", "recv")
    try:
        wait_until_reachable(local_command_runner, upload_receiver.address)
        print("[laptop] " + describe_connection(local_command_runner, upload_receiver.address))

        started_at = time.time()
        run_local_tailcat("cp", local_file, f"{upload_receiver.address}:")
        elapsed_seconds = seconds_since(started_at)

        checksum_matches = compute_sandbox_md5(sandbox, "/home/user/inbox/input.bin") == local_input_md5
        if not checksum_matches:
            raise RuntimeError("the uploaded file checksum does not match the local file")
        print(
            f"[laptop -> sandbox] {FILE_SIZE_MIB} MiB in {elapsed_seconds:.1f}s "
            f"({format_megabits_per_second(FILE_SIZE_BYTES, elapsed_seconds)}), md5 ok"
        )
    finally:
        upload_receiver.stop()


def download_from_sandbox(sandbox: Sandbox, working_directory: str, local_input_md5: str) -> None:
    prepare_results_command = " && ".join(
        [
            "mkdir -p /home/user/results",
            "cd /home/user/results",
            f"head -c {FILE_SIZE_BYTES} /dev/urandom > output.bin",
            "input_md5=$(md5sum /home/user/inbox/input.bin | cut -d' ' -f1)",
            "output_md5=$(md5sum output.bin | cut -d' ' -f1)",
            f"printf '{{\"inputBytes\":{FILE_SIZE_BYTES},\"inputMd5\":\"%s\","
            f"\"outputBytes\":{FILE_SIZE_BYTES},\"outputMd5\":\"%s\"}}\\n' "
            '"$input_md5" "$output_md5" > transfer-report.json',
        ]
    )
    sandbox.commands.run(prepare_results_command)
    download_server = start_sandbox_tailcat_server(sandbox, "serve --files=/home/user/results:ro files", "files")
    try:
        wait_until_reachable(local_command_runner, download_server.address)

        print("[laptop] tailcat ls -l <sandbox>:")
        run_local_tailcat("ls", "-l", download_server.address)

        started_at = time.time()
        run_local_tailcat("cp", f"{download_server.address}:output.bin", working_directory)
        elapsed_seconds = seconds_since(started_at)

        local_output_md5 = compute_local_md5(os.path.join(working_directory, "output.bin"))
        sandbox_output_md5 = compute_sandbox_md5(sandbox, "/home/user/results/output.bin")
        checksum_matches = local_output_md5 == sandbox_output_md5
        if not checksum_matches:
            raise RuntimeError("the downloaded file checksum does not match the sandbox file")
        print(
            f"[sandbox -> laptop] {FILE_SIZE_MIB} MiB in {elapsed_seconds:.1f}s "
            f"({format_megabits_per_second(FILE_SIZE_BYTES, elapsed_seconds)}), md5 ok"
        )

        run_local_tailcat("cp", f"{download_server.address}:transfer-report.json", working_directory)
        report_path = os.path.join(working_directory, "transfer-report.json")
        with open(report_path) as report_file:
            transfer_report = json.load(report_file)
        expected_report = {
            "inputBytes": FILE_SIZE_BYTES,
            "inputMd5": local_input_md5,
            "outputBytes": FILE_SIZE_BYTES,
            "outputMd5": local_output_md5,
        }
        if transfer_report != expected_report:
            raise RuntimeError("the transfer report does not match the copied files")
        print(f"\nTransfer report saved to {report_path}:\n" + json.dumps(transfer_report, indent=2))
    finally:
        download_server.stop()


def main() -> None:
    load_dotenv()
    require_local_tailcat()
    sandbox = create_sandbox()
    try:
        working_directory = tempfile.mkdtemp(prefix="tailcat-laptop-sandbox-file-transfer-")
        local_file = os.path.join(working_directory, "input.bin")
        with open(local_file, "wb") as input_file:
            input_file.write(os.urandom(FILE_SIZE_BYTES))
        local_input_md5 = compute_local_md5(local_file)

        upload_to_sandbox(sandbox, local_file, local_input_md5)
        download_from_sandbox(sandbox, working_directory, local_input_md5)
    finally:
        sandbox.kill()


if __name__ == "__main__":
    main()
