"""Two E2B sandboxes exchange data directly, without going through the orchestrator.

The producer sandbox generates a dataset and serves a read-write directory.
The consumer sandbox pulls the dataset over Tailcat, analyzes it, and pushes
a JSON report back into the producer's directory. The orchestrator only passes
the producer's address to the consumer. Once NAT traversal completes, the two
sandboxes talk peer to peer inside E2B's network (about 1 to 2 ms), otherwise
via the DERP relay.
"""

import time

from dotenv import load_dotenv

from .tailcat_runtime import (
    compute_sandbox_md5,
    create_sandbox,
    create_sandbox_command_runner,
    describe_network_path,
    format_megabits_per_second,
    require_successful_command,
    run_sandbox_command,
    seconds_since,
    start_sandbox_tailcat_server,
    wait_until_reachable,
)

FILE_SIZE_MIB = 100
FILE_SIZE_BYTES = FILE_SIZE_MIB * 1024 * 1024


def main() -> None:
    load_dotenv()
    producer_sandbox = create_sandbox()
    consumer_sandbox = create_sandbox()
    try:
        # Producer: generate a dataset and share its directory read-write.
        producer_sandbox.commands.run(
            f"mkdir -p /home/user/share && head -c {FILE_SIZE_BYTES} /dev/urandom > /home/user/share/dataset.bin",
        )
        shared_directory_server = start_sandbox_tailcat_server(
            producer_sandbox, "serve --files=/home/user/share:rw files", "share"
        )
        try:

            # Consumer: connect using only the address string.
            run_in_consumer = create_sandbox_command_runner(consumer_sandbox, timeout_seconds=300)
            wait_until_reachable(run_in_consumer, shared_directory_server.address)
            print("[consumer] " + describe_network_path(run_in_consumer, shared_directory_server.address))

            directory_listing = run_in_consumer(f"tailcat ls -l {shared_directory_server.address}")
            require_successful_command(directory_listing)
            print("[consumer] tailcat ls -l <producer>:\n" + directory_listing.output.strip())

            started_at = time.time()
            require_successful_command(
                run_in_consumer(f"tailcat cp {shared_directory_server.address}:dataset.bin /home/user/dataset.bin")
            )
            elapsed_seconds = seconds_since(started_at)
            checksum_matches = compute_sandbox_md5(consumer_sandbox, "/home/user/dataset.bin") == compute_sandbox_md5(
                producer_sandbox, "/home/user/share/dataset.bin"
            )
            if not checksum_matches:
                raise RuntimeError("the dataset checksum differs between the producer and consumer")
            print(
                f"[producer -> consumer] {FILE_SIZE_MIB} MiB in {elapsed_seconds:.1f}s "
                f"({format_megabits_per_second(FILE_SIZE_BYTES, elapsed_seconds)}), md5 ok"
            )

            # Consumer: analyze the dataset, then push a useful result back to the producer.
            analysis = run_in_consumer(
                "checksum=$(sha256sum /home/user/dataset.bin | cut -d' ' -f1); "
                f"printf '{{\"bytesProcessed\":{FILE_SIZE_BYTES},\"status\":\"complete\","
                "\"generatedBy\":\"worker-sandbox\",\"sha256\":\"%s\"}\\n' "
                '"$checksum" > /home/user/analysis-report.json'
            )
            require_successful_command(analysis)
            started_at = time.time()
            require_successful_command(
                run_in_consumer(f"tailcat cp /home/user/analysis-report.json {shared_directory_server.address}:")
            )
            elapsed_seconds = seconds_since(started_at)
            processed_result = run_sandbox_command(
                producer_sandbox, "python3 -m json.tool /home/user/share/analysis-report.json"
            )
            require_successful_command(processed_result)
            print(f"[consumer -> producer] analysis-report.json returned in {elapsed_seconds:.1f}s")
            print("\nFinal report:\n" + processed_result.output.strip())

            # Streaming variant: bare `tailcat` accepts one connection and copies it to
            # stdout, so the producer receives whatever the consumer pipes in.
            stream_receiver = start_sandbox_tailcat_server(
                producer_sandbox, "", "stream", stdout_file="/home/user/share/stream.log"
            )
            try:
                wait_until_reachable(run_in_consumer, stream_receiver.address)
                require_successful_command(
                    run_in_consumer(f"seq 1 5 | sed 's/^/log line /' | tailcat {stream_receiver.address}")
                )
                time.sleep(1)
                received_stream = run_sandbox_command(producer_sandbox, "cat /home/user/share/stream.log")
                require_successful_command(received_stream)
                print("[consumer -> producer stream] producer received:\n" + received_stream.output.strip())
            finally:
                stream_receiver.stop()
        finally:
            shared_directory_server.stop()
    finally:
        producer_sandbox.kill()
        consumer_sandbox.kill()


if __name__ == "__main__":
    main()
