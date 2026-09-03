/**
 * Transfer a large file in both directions and verify every copy.
 *
 * Upload:   the sandbox runs `tailcat recv` (a write-only drop box), the laptop runs `tailcat cp`.
 * Download: the sandbox runs `tailcat serve files` (read-only), the laptop runs `tailcat ls` and `tailcat cp`.
 * Bytes flow over an encrypted WireGuard tunnel, relayed through DERP until a
 * direct UDP path forms, and never touch the E2B API.
 */
import "dotenv/config";
import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Sandbox } from "e2b";
import {
  computeLocalMd5,
  computeSandboxMd5,
  createSandbox,
  describeNetworkPath,
  formatMegabitsPerSecond,
  localCommandRunner,
  requireLocalTailcat,
  secondsSince,
  startSandboxTailcatServer,
  waitUntilReachable,
} from "./tailcatRuntime";

const FILE_SIZE_MIB = Number(process.env.DEMO_FILE_SIZE_MIB ?? 50);
const FILE_SIZE_BYTES = FILE_SIZE_MIB * 1024 * 1024;

function runLocalTailcat(...tailcatArguments: string[]): void {
  execFileSync("tailcat", tailcatArguments, { stdio: "inherit" });
}

async function uploadToSandbox(
  sandbox: Sandbox,
  localFile: string,
  localInputMd5: string,
): Promise<void> {
  await sandbox.commands.run("mkdir -p /home/user/inbox");
  const uploadReceiver = await startSandboxTailcatServer(
    sandbox,
    "recv /home/user/inbox",
    "recv",
  );
  try {
    await waitUntilReachable(localCommandRunner, uploadReceiver.address);
    console.log(
      "[laptop] " +
        (await describeNetworkPath(localCommandRunner, uploadReceiver.address)),
    );

    const startedAt = Date.now();
    runLocalTailcat("cp", localFile, `${uploadReceiver.address}:`);
    const elapsedSeconds = secondsSince(startedAt);

    const checksumMatches =
      (await computeSandboxMd5(sandbox, "/home/user/inbox/input.bin")) ===
      localInputMd5;
    if (!checksumMatches) {
      throw new Error(
        "the uploaded file checksum does not match the local file",
      );
    }
    console.log(
      `[laptop -> sandbox] ${FILE_SIZE_MIB} MiB in ${elapsedSeconds.toFixed(
        1,
      )}s ` +
        `(${formatMegabitsPerSecond(FILE_SIZE_BYTES, elapsedSeconds)}), md5 ok`,
    );
  } finally {
    await uploadReceiver.stop();
  }
}

async function downloadFromSandbox(
  sandbox: Sandbox,
  workingDirectory: string,
  localInputMd5: string,
): Promise<void> {
  const prepareResultsCommand = [
    "mkdir -p /home/user/results",
    "cd /home/user/results",
    `head -c ${FILE_SIZE_BYTES} /dev/urandom > output.bin`,
    "input_md5=$(md5sum /home/user/inbox/input.bin | cut -d' ' -f1)",
    "output_md5=$(md5sum output.bin | cut -d' ' -f1)",
    `printf '{"inputBytes":${FILE_SIZE_BYTES},"inputMd5":"%s","outputBytes":${FILE_SIZE_BYTES},` +
      `"outputMd5":"%s"}\\n' "$input_md5" "$output_md5" > transfer-report.json`,
  ].join(" && ");
  await sandbox.commands.run(prepareResultsCommand);
  const downloadServer = await startSandboxTailcatServer(
    sandbox,
    "serve --files=/home/user/results:ro files",
    "files",
  );
  try {
    await waitUntilReachable(localCommandRunner, downloadServer.address);

    console.log("[laptop] tailcat ls -l <sandbox>:");
    runLocalTailcat("ls", "-l", downloadServer.address);

    const startedAt = Date.now();
    runLocalTailcat(
      "cp",
      `${downloadServer.address}:output.bin`,
      workingDirectory,
    );
    const elapsedSeconds = secondsSince(startedAt);

    const localOutputMd5 = computeLocalMd5(
      join(workingDirectory, "output.bin"),
    );
    const sandboxOutputMd5 = await computeSandboxMd5(
      sandbox,
      "/home/user/results/output.bin",
    );
    const checksumMatches = localOutputMd5 === sandboxOutputMd5;
    if (!checksumMatches) {
      throw new Error(
        "the downloaded file checksum does not match the sandbox file",
      );
    }
    console.log(
      `[sandbox -> laptop] ${FILE_SIZE_MIB} MiB in ${elapsedSeconds.toFixed(
        1,
      )}s ` +
        `(${formatMegabitsPerSecond(FILE_SIZE_BYTES, elapsedSeconds)}), md5 ok`,
    );

    runLocalTailcat(
      "cp",
      `${downloadServer.address}:transfer-report.json`,
      workingDirectory,
    );
    const reportPath = join(workingDirectory, "transfer-report.json");
    const transferReport = readFileSync(reportPath, "utf8").trim();
    const expectedReport = JSON.stringify({
      inputBytes: FILE_SIZE_BYTES,
      inputMd5: localInputMd5,
      outputBytes: FILE_SIZE_BYTES,
      outputMd5: localOutputMd5,
    });
    if (transferReport !== expectedReport) {
      throw new Error("the transfer report does not match the copied files");
    }
    console.log(`\nTransfer report saved to ${reportPath}:\n${transferReport}`);
  } finally {
    await downloadServer.stop();
  }
}

requireLocalTailcat();
const sandbox = await createSandbox();
try {
  const workingDirectory = mkdtempSync(
    join(tmpdir(), "tailcat-laptop-sandbox-file-transfer-"),
  );
  const localFile = join(workingDirectory, "input.bin");
  writeFileSync(localFile, randomBytes(FILE_SIZE_BYTES));
  if (statSync(localFile).size !== FILE_SIZE_BYTES) {
    throw new Error("test file was not written completely");
  }
  const localInputMd5 = computeLocalMd5(localFile);

  await uploadToSandbox(sandbox, localFile, localInputMd5);
  await downloadFromSandbox(sandbox, workingDirectory, localInputMd5);
} finally {
  await sandbox.kill();
}
