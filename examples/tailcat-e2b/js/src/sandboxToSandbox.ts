/**
 * Two E2B sandboxes exchange encrypted data through Tailcat's DERP relay.
 *
 * The producer sandbox generates a dataset and serves a read-write directory.
 * The consumer sandbox pulls the dataset over Tailcat, analyzes it, and pushes
 * a JSON report back into the producer's directory. The orchestrator only passes
 * the producer's address to the consumer; file bytes do not pass through it.
 */
import "dotenv/config";
import { setTimeout as delay } from "node:timers/promises";
import {
  computeSandboxMd5,
  createSandbox,
  createSandboxCommandRunner,
  describeConnection,
  formatMegabitsPerSecond,
  requireSuccessfulCommand,
  runSandboxCommand,
  secondsSince,
  startSandboxTailcatServer,
  waitUntilReachable,
} from "./tailcatRuntime";

const FILE_SIZE_MIB = Number(process.env.DEMO_FILE_SIZE_MIB ?? 10);
const FILE_SIZE_BYTES = FILE_SIZE_MIB * 1024 * 1024;

const producerSandbox = await createSandbox();
const consumerSandbox = await createSandbox();
try {
  // Producer: generate a dataset and share its directory read-write.
  await producerSandbox.commands.run(
    `mkdir -p /home/user/share && head -c ${FILE_SIZE_BYTES} /dev/urandom > /home/user/share/dataset.bin`,
  );
  const sharedDirectoryServer = await startSandboxTailcatServer(
    producerSandbox,
    "serve --files=/home/user/share:rw files",
    "share",
  );
  try {
    // Consumer: connect using only the address string.
    const runInConsumer = createSandboxCommandRunner(consumerSandbox, 300_000);
    await waitUntilReachable(runInConsumer, sharedDirectoryServer.address);
    console.log(
      "[consumer] " +
        (await describeConnection(
          runInConsumer,
          sharedDirectoryServer.address,
        )),
    );

    const directoryListing = await runInConsumer(
      `tailcat ls -l ${sharedDirectoryServer.address}`,
    );
    requireSuccessfulCommand(directoryListing);
    console.log(
      "[consumer] tailcat ls -l <producer>:\n" + directoryListing.output.trim(),
    );

    let startedAt = Date.now();
    requireSuccessfulCommand(
      await runInConsumer(
        `tailcat cp ${sharedDirectoryServer.address}:dataset.bin /home/user/dataset.bin`,
      ),
    );
    let elapsedSeconds = secondsSince(startedAt);
    const checksumMatches =
      (await computeSandboxMd5(consumerSandbox, "/home/user/dataset.bin")) ===
      (await computeSandboxMd5(
        producerSandbox,
        "/home/user/share/dataset.bin",
      ));
    if (!checksumMatches) {
      throw new Error(
        "the dataset checksum differs between the producer and consumer",
      );
    }
    console.log(
      `[producer -> consumer] ${FILE_SIZE_MIB} MiB in ${elapsedSeconds.toFixed(
        1,
      )}s ` +
        `(${formatMegabitsPerSecond(FILE_SIZE_BYTES, elapsedSeconds)}), md5 ok`,
    );

    // Consumer: analyze the dataset, then push a useful result back to the producer.
    const analysis = await runInConsumer(
      `checksum=$(sha256sum /home/user/dataset.bin | cut -d' ' -f1); ` +
        `printf '{"bytesProcessed":${FILE_SIZE_BYTES},"status":"complete","generatedBy":"worker-sandbox","sha256":"%s"}\n' ` +
        `"$checksum" > /home/user/analysis-report.json`,
    );
    requireSuccessfulCommand(analysis);
    startedAt = Date.now();
    requireSuccessfulCommand(
      await runInConsumer(
        `tailcat cp /home/user/analysis-report.json ${sharedDirectoryServer.address}:`,
      ),
    );
    elapsedSeconds = secondsSince(startedAt);
    const processedResult = await runSandboxCommand(
      producerSandbox,
      "python3 -m json.tool /home/user/share/analysis-report.json",
    );
    requireSuccessfulCommand(processedResult);
    console.log(
      `[consumer -> producer] analysis-report.json returned in ${elapsedSeconds.toFixed(
        1,
      )}s`,
    );
    console.log("\nFinal report:\n" + processedResult.output.trim());

    // Streaming variant: bare `tailcat` accepts one connection and copies it to
    // stdout, so the producer receives whatever the consumer pipes in.
    const streamReceiver = await startSandboxTailcatServer(
      producerSandbox,
      "",
      "stream",
      {
        stdoutFile: "/home/user/share/stream.log",
      },
    );
    try {
      await waitUntilReachable(runInConsumer, streamReceiver.address);
      requireSuccessfulCommand(
        await runInConsumer(
          `seq 1 5 | sed 's/^/log line /' | tailcat ${streamReceiver.address}`,
        ),
      );
      await delay(1000);
      const receivedStream = await runSandboxCommand(
        producerSandbox,
        "cat /home/user/share/stream.log",
      );
      requireSuccessfulCommand(receivedStream);
      console.log(
        "[consumer -> producer stream] producer received:\n" +
          receivedStream.output.trim(),
      );
    } finally {
      await streamReceiver.stop();
    }
  } finally {
    await sharedDirectoryServer.stop();
  }
} finally {
  await producerSandbox.kill();
  await consumerSandbox.kill();
}
