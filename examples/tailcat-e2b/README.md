# Tailcat with E2B sandboxes

[Tailcat](https://github.com/tailscale/tailcat) is netcat over Tailscale's data plane: WireGuard encryption, NAT traversal and DERP relays, with no control plane, accounts or admin rights. A listener prints an address string, a client uses that string to connect. That fits E2B sandboxes well: a sandbox only needs outbound connectivity, no root, and no kernel network changes.

This example ships a sandbox template with tailcat preinstalled and three demos, in [Python](./python) and [TypeScript](./js):

| Script | What it shows | Needs tailcat on your machine |
| --- | --- | --- |
| `laptop-sandbox-file-transfer` | Transfer a large file in both directions and produce a computed verification report | yes |
| `sandbox-to-sandbox` | A producer sends a dataset to a worker, which returns a JSON analysis report and streams logs | no |
| `sandbox-tests-local-dev-server` | A sandbox tests a development API on your laptop, locked to the sandbox's key | yes |

Measured on 2026-09-02 with tailcat 0.4.0: sandbox to sandbox pings went from 39 ms over the relay to 1.6 ms once the direct path formed, and a 100 MB copy took 1.3 s. Laptop to sandbox went from about 215 ms via the relay to about 170 ms direct.

![The TypeScript sandbox-to-sandbox demo transferring a dataset, returning an analysis report, and streaming logs](./demo/sandbox-to-sandbox.gif)

![The three Tailcat networking flows: laptop-to-sandbox files, sandbox-to-sandbox processing, and testing a laptop-local service](./demo/tailcat-demo-flows.svg)

## Setup

Both versions read `E2B_API_KEY` from a `.env` file (copy `.env.template`). `laptop-sandbox-file-transfer` and `sandbox-tests-local-dev-server` also need tailcat on your machine: `brew install tailcat`, or see the [install options](https://github.com/tailscale/tailcat#install).

Build the `tailcat` template once per team, from either language (about one minute):

```bash
cd js && npm install && npm run e2b:build
# or
cd python && poetry install && poetry run build-template
```

If you skip this, the demos create a default sandbox and install tailcat at runtime, which costs a couple of seconds per sandbox.

## Run

```bash
# TypeScript
cd js
npm run laptop-sandbox-file-transfer   # laptop <-> sandbox files
npm run sandbox-to-sandbox              # sandbox <-> sandbox   (also `npm start`)
npm run sandbox-tests-local-dev-server # sandbox tests a laptop-local development API

# Python
cd python
poetry run laptop-sandbox-file-transfer
poetry run sandbox-to-sandbox   # also `poetry run start`
poetry run sandbox-tests-local-dev-server
```

`DEMO_FILE_SIZE_MIB` changes the file size in `laptop-sandbox-file-transfer` (default 50). Laptop transfers are limited by your upstream bandwidth, so do not read the laptop numbers as a tailcat benchmark.

## What happens in each demo

The runnable scripts stay under 140 lines in both languages. They read like orchestration code; the roughly 280-line `tailcatRuntime` / `tailcat_runtime` module owns process management, retries and E2B-specific setup.

### Laptop and sandbox exchange files

```text
create a temporary 50 MiB file on the laptop
create a sandbox and start its write-only receiver
copy input.bin from laptop to sandbox
compare the local and sandbox MD5 hashes; stop on a mismatch
have the sandbox create output.bin and a report containing real sizes and hashes
start a read-only file server in the sandbox
list and download the results to the laptop
verify output.bin and the complete report; always stop the listener and sandbox
```

This is the most familiar file-transfer example. It showcases visible end products on the laptop: `output.bin` and `transfer-report.json`.

### Two sandboxes exchange a dataset and a result

```text
create a producer sandbox and a consumer sandbox
have the producer create and serve dataset.bin
give only the producer's Tailcat address to the consumer
copy 100 MiB directly and verify its MD5 at both ends
have the consumer calculate a SHA-256 analysis report
copy analysis-report.json back to the producer
stream five log lines through a separate one-shot connection
always stop both listeners and both sandboxes
```

This is the strongest networking showcase: no laptop Tailcat installation is needed, and the output makes both directions of communication obvious.

### A sandbox tests a laptop-local development server

```text
start an HTTP server bound only to the laptop's loopback interface
create a sandbox and generate a Tailcat client key inside it
serve the local HTTP port, allowing only that sandbox key
call /health and /api/project through Tailcat's SOCKS proxy
map the same connection to a sandbox-local TCP port and call /api/check
try a different identity and prove that it is rejected
always stop Tailcat, the sandbox and the local HTTP server
```

This is the most practical agent-development example: code running remotely can test a service that has not been deployed or exposed publicly.

## Named callbacks

- `handleLocalHttpRequest` / `LocalRequestHandler.do_GET` handles the three HTTP routes used by the local-service demo.
- `readAddressFile` and `readServerLog` are passed to `waitForTailcatAddress`; the helper calls them while waiting for a listener to become ready.
- Every started listener returns a named `stop` callback. Each demo calls it from `finally`, so a failed checksum or request does not leave a process running.
- The Python SDK's background stderr callback was unreliable during the spike, so neither implementation depends on it. Both poll Tailcat's documented `TAILCAT_ADDR_FILE` instead.

## How it fits together

```
js/                                   python/
├── template/template.ts              ├── tailcat_e2b/template.py          the sandbox template
├── template/build.ts                 ├── tailcat_e2b/build_template.py    Template.build(alias="tailcat")
├── src/tailcatRuntime.ts              ├── tailcat_e2b/tailcat_runtime.py   shared helper
├── src/laptopSandboxFileTransfer.ts  ├── tailcat_e2b/laptop_sandbox_file_transfer.py
├── src/sandboxToSandbox.ts           ├── tailcat_e2b/sandbox_to_sandbox.py
└── src/sandboxTestsLocalDevServer.ts └── tailcat_e2b/sandbox_tests_local_dev_server.py
```

- The **template** installs tailcat from the GitHub release, plus `openssh-client` (tailcat drives the system scp and ssh), `socat` and `iproute2`, and runs the dummy IP command described below at sandbox start.
- The **helper** starts a listener in a sandbox or on the laptop and returns its address, waits until the listener answers, and reports whether the path is relayed or direct. Everything that is a quirk lives here.
- Each **script** is a short program that wires these together. The two languages are line-for-line ports of each other.

## Quirks you need to know about

Three things came out of the spike. The first one is specific to E2B, the other two are tailcat behaviour you would hit anywhere.

### 1. E2B specific: the sandbox needs a dummy IPv4 or no direct path ever forms

An E2B sandbox's only IPv4 address is link-local:

```
inet 169.254.0.21/30 scope global eth0
default via 169.254.0.22 dev eth0
```

Tailscale's network monitor defines `HaveV4` as "some non-localhost, **non-link-local** IPv4 address on an interface that's up" ([net/netmon/state.go](https://github.com/tailscale/tailscale/blob/main/net/netmon/state.go), `HaveV4` doc comment; link-local addresses are sorted into a separate `linklocal4` bucket a few lines above). netcheck then only plans IPv4 STUN probes when `ifState.HaveV4` is true ([net/netcheck/netcheck.go](https://github.com/tailscale/tailscale/blob/main/net/netcheck/netcheck.go), `makeProbePlan`). With `HaveV4 == false` no STUN packet is ever sent, the report says `UDP is blocked` in the same second, and the sandbox advertises `169.254.0.21` as its only endpoint. The peer tries to dial that, fails, and traffic stays on the relay.

Observed with `tailcat ping --verbose` inside a stock sandbox:

```
link state: interfaces.State{defaultRoute=eth0 ifs={eth0:[llu4 llu6]} v4=false v6=false}
netcheck: netcheck: UDP is blocked, trying HTTPS
netcheck: [v1] report: udp=false icmpv4=false ...
magicsock: endpoints changed: 169.254.0.21:32793 (local)
```

UDP egress itself is fine. A raw STUN request from the sandbox to Google's and Cloudflare's servers gets an answer, and the mapped port equals the source port across destinations, which is the easy NAT type for hole punching.

Adding any routable address to the interface flips `HaveV4`:

```bash
sudo ip addr add 10.200.0.1/32 dev eth0
```

Traffic still leaves through the sandbox's real address. The same command afterwards:

```
netcheck: [v1] report: udp=true ... v4a=34.19.7.32:52826 derp=303
magicsock: endpoints changed: 34.19.7.32:52826 (stun), 10.200.0.1:52826 (local)
pong in 1.64ms via 34.19.7.32:40289
```

The template runs this in its start command and `createSandbox` / `create_sandbox` repeats it for the fallback sandbox created when that template is unavailable. There is no tailcat or Tailscale flag that skips the check (`TS_DEBUG_NETCHECK` only adds logging). Cleaner fixes would be E2B giving sandboxes a routable private address, or Tailscale counting a link-local-only host that has a default route as `HaveV4`. Both are worth raising, the workaround is fine for a demo.

### 2. tailcat prints its address before it is reachable

The address appears on stderr as soon as the key exists, but the listener needs another one to three seconds to connect to the DERP relay. A client that connects in that window logs `derp-303 does not know about peer` and its 10 s deadline expires. `waitUntilReachable` / `wait_until_reachable` pings with a 3 s timeout until the listener answers. The same applies to `tailcat cp` and `tailcat ssh`, which do an internal ping first.

### 3. Reading the address from the SDK

Background commands in the Python SDK did not deliver stderr callbacks reliably in this setup, and polling a file is the same code in both languages. tailcat offers two clean alternatives: `TAILCAT_ADDR_FILE=/path` writes the address to a file, and `--json` prints `{"listenAddr": ...}` on stdout. The helper uses the file.

## What tailcat gives you here

- **Encrypted transfers that bypass the E2B API.** Big files or directories move over WireGuard straight between the endpoints, with `cp`, `ls` and `recv` semantics and no public URL.
- **Sandbox to sandbox networking.** E2B does not route between sandboxes. Two sandboxes that hold each other's tailcat address can talk directly inside the same network.
- **Test a local development server from a sandbox.** With `tailcat serve --allow=<sandbox key> <port>` on the laptop, an agent or test runner in the sandbox can exercise your local app before it is deployed, and only that sandbox can connect.
- **Shell access.** `tailcat serve no-auth-ssh` in the sandbox and `tailcat ssh <addr>` from anywhere, no key setup.

## Limits

- The public DERP relays at `tailcat.dev` are free, rate limited and have no SLA. They only carry traffic until a direct path forms, or all of it when one cannot.
- tailcat has no API, CLI or wire-format stability guarantees yet.
- `no-auth-ssh` is what it says. Pair it with `--allow` or keep it to throwaway sandboxes.
- If a sandbox has a network allowlist, it needs the DERP hosts from https://tailcat.dev/derpmap.json on TCP 443 and UDP 3478.
