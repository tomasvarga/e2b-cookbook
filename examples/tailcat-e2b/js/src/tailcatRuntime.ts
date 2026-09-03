/**
 * Helpers for running Tailcat inside E2B sandboxes and on this machine.
 *
 * A Tailcat *server* (listener) prints an address; a Tailcat *client* connects
 * to it. Both sides can live in a sandbox or on the laptop. This module hides
 * the two things that are easy to get wrong: reading the address and waiting
 * until the server is actually reachable through the DERP relay.
 */
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { CommandExitError, Sandbox } from "e2b";
import { TAILCAT_URL, TEMPLATE_ALIAS } from "../template/template";

/** A Tailcat address looks like `tco2FwWCD...`: the prefix `tc` followed by base64url. */
const TAILCAT_ADDRESS_PATTERN = /\btc[A-Za-z0-9_-]{20,}/;

const INSTALL_TAILCAT_CMD =
  `curl -fsSL -o /tmp/tailcat.tgz ${TAILCAT_URL} && mkdir -p /tmp/tc && tar xzf /tmp/tailcat.tgz -C /tmp/tc` +
  " && sudo install -m 0755 $(find /tmp/tc -type f -name tailcat) /usr/local/bin/tailcat && rm -rf /tmp/tc /tmp/tailcat.tgz";

export interface CommandResult {
  exitCode: number;
  /** stdout and stderr, concatenated. */
  output: string;
}

/** Runs a shell command locally or in a sandbox and captures its exit code and combined output. */
export type CommandRunner = (command: string) => Promise<CommandResult>;

/** A running Tailcat server and how to stop it. */
export interface TailcatServer {
  address: string;
  stop(): Promise<void> | void;
}

// ---------------------------------------------------------------------------
// Sandboxes
// ---------------------------------------------------------------------------

/** Create a sandbox with Tailcat installed. */
export async function createSandbox(timeoutMs = 600_000): Promise<Sandbox> {
  const startedAt = Date.now();
  const sandbox = await createSandboxFromTemplateOrFallback(timeoutMs);
  const elapsedSeconds = (Date.now() - startedAt) / 1000;
  console.log(
    `[${sandbox.sandboxId}] sandbox ready in ${elapsedSeconds.toFixed(1)}s`,
  );

  return sandbox;
}

/** Use the `tailcat` template when this team has built it, else a default sandbox with Tailcat installed on the fly. */
async function createSandboxFromTemplateOrFallback(
  timeoutMs: number,
): Promise<Sandbox> {
  try {
    return await Sandbox.create(TEMPLATE_ALIAS, { timeoutMs });
  } catch (error) {
    const templateMissing = String(error).includes("404");
    if (!templateMissing) {
      throw error;
    }
  }
  const sandbox = await Sandbox.create({ timeoutMs });
  await sandbox.commands.run(INSTALL_TAILCAT_CMD, { timeoutMs: 120_000 });
  console.log(
    `[${sandbox.sandboxId}] template '${TEMPLATE_ALIAS}' not found, installed tailcat at runtime instead ` +
      "(run `npm run e2b:build` once to skip this)",
  );
  return sandbox;
}

/** Run a command in the sandbox without throwing on a non-zero exit. */
export async function runSandboxCommand(
  sandbox: Sandbox,
  command: string,
  timeoutMs = 60_000,
): Promise<CommandResult> {
  try {
    const result = await sandbox.commands.run(command, { timeoutMs });
    return { exitCode: result.exitCode, output: result.stdout + result.stderr };
  } catch (error) {
    if (error instanceof CommandExitError) {
      return { exitCode: error.exitCode, output: error.stdout + error.stderr };
    }
    throw error;
  }
}

/** Create a command runner bound to one sandbox. */
export function createSandboxCommandRunner(
  sandbox: Sandbox,
  timeoutMs = 120_000,
): CommandRunner {
  return async function runInSandbox(command: string): Promise<CommandResult> {
    return runSandboxCommand(sandbox, command, timeoutMs);
  };
}

export function localCommandRunner(command: string): Promise<CommandResult> {
  const completedProcess = spawnSync(command, {
    shell: true,
    encoding: "utf8",
  });
  return Promise.resolve({
    exitCode: completedProcess.status ?? 1,
    output: (completedProcess.stdout ?? "") + (completedProcess.stderr ?? ""),
  });
}

/** Throw with the command output when a command did not exit 0. */
export function requireSuccessfulCommand(result: CommandResult): void {
  if (result.exitCode !== 0) {
    throw new Error(
      `command failed (exit ${result.exitCode}):\n${result.output}`,
    );
  }
}

// ---------------------------------------------------------------------------
// Tailcat servers
// ---------------------------------------------------------------------------

/**
 * Start `tailcat <tailcatArguments>` in the background inside the sandbox and return its address.
 *
 * `tailcatArguments` is passed to the shell unsplit. Tailcat writes the address
 * to TAILCAT_ADDR_FILE, which is more reliable than scraping stderr from a
 * background command. `stdoutFile` redirects the server's stdout (bare
 * `tailcat` copies its one connection there).
 */
export async function startSandboxTailcatServer(
  sandbox: Sandbox,
  tailcatArguments: string,
  instanceName = "tailcat",
  {
    waitMs = 20_000,
    stdoutFile,
  }: { waitMs?: number; stdoutFile?: string } = {},
): Promise<TailcatServer> {
  const addressFile = `/tmp/${instanceName}.addr`;
  const logFile = `/tmp/${instanceName}.log`;
  const serverProcess = await sandbox.commands.run(
    `rm -f ${addressFile}; TAILCAT_ADDR_FILE=${addressFile} tailcat ${tailcatArguments} > ${
      stdoutFile ?? logFile
    } 2> ${logFile}`,
    { background: true },
  );

  async function readAddressFile(): Promise<string> {
    return (await runSandboxCommand(sandbox, `cat ${addressFile} 2>/dev/null`))
      .output;
  }

  async function readServerLog(): Promise<string> {
    return (await runSandboxCommand(sandbox, `cat ${logFile}`)).output;
  }

  async function stop(): Promise<void> {
    await serverProcess.kill();
  }

  const address = await waitForTailcatAddress(
    readAddressFile,
    waitMs,
    readServerLog,
  );
  console.log(
    `[${sandbox.sandboxId}] tailcat ${tailcatArguments}  ->  ${abbreviate(
      address,
    )}`,
  );
  return { address, stop };
}

/** Start `tailcat <tailcatArguments>` on this machine and return its address. `tailcatArguments` is split on whitespace. */
export async function startLocalTailcatServer(
  tailcatArguments: string,
  waitMs = 20_000,
): Promise<TailcatServer> {
  requireLocalTailcat();
  const addressFile = join(
    tmpdir(),
    `tailcat-${process.pid}-${Date.now()}.addr`,
  );
  const serverProcess: ChildProcess = spawn(
    "tailcat",
    tailcatArguments.split(/\s+/).filter(Boolean),
    {
      env: { ...process.env, TAILCAT_ADDR_FILE: addressFile },
      stdio: "ignore",
    },
  );

  function readAddressFile(): Promise<string> {
    const contents = existsSync(addressFile)
      ? readFileSync(addressFile, "utf8")
      : "";
    return Promise.resolve(contents);
  }

  let address: string;
  try {
    address = await waitForTailcatAddress(readAddressFile, waitMs);
  } catch (error) {
    serverProcess.kill();
    throw error;
  }
  console.log(
    `[laptop] tailcat ${tailcatArguments}  ->  ${abbreviate(address)}`,
  );

  function stop(): void {
    serverProcess.kill();
    if (existsSync(addressFile)) {
      unlinkSync(addressFile);
    }
  }

  return {
    address,
    stop,
  };
}

/** Poll `readAddressFile` until it contains a Tailcat address. */
async function waitForTailcatAddress(
  readAddressFile: () => Promise<string>,
  waitMs: number,
  readServerLog?: () => Promise<string>,
): Promise<string> {
  const startedAt = Date.now();
  while (Date.now() - startedAt < waitMs) {
    const addressMatch = TAILCAT_ADDRESS_PATTERN.exec(await readAddressFile());
    if (addressMatch) {
      return addressMatch[0];
    }
    await delay(300);
  }
  const serverLog = readServerLog ? `\n${await readServerLog()}` : "";
  throw new Error(
    `tailcat did not print an address within ${waitMs / 1000}s${serverLog}`,
  );
}

// ---------------------------------------------------------------------------
// Tailcat clients
// ---------------------------------------------------------------------------

/**
 * Block until a Tailcat server answers a ping from the client side.
 *
 * A Tailcat server prints its address before it has finished connecting to the
 * DERP relay. A client that connects in that window is told the relay does not
 * know the peer, so retry instead of trusting the first attempt.
 */
export async function waitUntilReachable(
  runCommand: CommandRunner,
  address: string,
  timeoutMs = 30_000,
): Promise<void> {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    const ping = await runCommand(`tailcat ping --timeout 3s ${address}`);
    if (ping.exitCode === 0) {
      return;
    }
    await delay(1000);
  }
  throw new Error(
    `tailcat server ${abbreviate(address)} not reachable after ${
      timeoutMs / 1000
    }s`,
  );
}

/** Ping once and describe the Tailcat connection. */
export async function describeConnection(
  runCommand: CommandRunner,
  address: string,
  timeout = "5s",
): Promise<string> {
  const ping = await runCommand(`tailcat ping --timeout ${timeout} ${address}`);
  const pongLine = ping.output
    .split("\n")
    .find((line) => line.includes("pong in"));
  if (!pongLine) {
    return `no pong: ${ping.output.trim().slice(-200)}`;
  }
  const latency = pongLine.split("pong in ")[1] ?? pongLine.trim();
  return pongLine.includes("DERP")
    ? `DERP relay: ${latency}`
    : `Tailcat connection: ${latency}`;
}

// ---------------------------------------------------------------------------
// Small utilities
// ---------------------------------------------------------------------------

export function requireLocalTailcat(): void {
  if (spawnSync("tailcat", ["version"], { stdio: "ignore" }).status !== 0) {
    console.error(
      "tailcat is not installed on this machine. Install it with `brew install tailcat` or see https://github.com/tailscale/tailcat#install",
    );
    process.exit(1);
  }
}

export function computeLocalMd5(path: string): string {
  return createHash("md5").update(readFileSync(path)).digest("hex");
}

export async function computeSandboxMd5(
  sandbox: Sandbox,
  path: string,
): Promise<string> {
  const result = await runSandboxCommand(sandbox, `md5sum ${path}`);
  requireSuccessfulCommand(result);
  return result.output.trim().split(/\s+/)[0] ?? "";
}

export function formatMegabitsPerSecond(
  bytes: number,
  seconds: number,
): string {
  return `${Math.round((bytes * 8) / seconds / 1e6)} Mbit/s`;
}

export function secondsSince(startedAt: number): number {
  return (Date.now() - startedAt) / 1000;
}

function abbreviate(address: string): string {
  return `${address.slice(0, 20)}…`;
}
