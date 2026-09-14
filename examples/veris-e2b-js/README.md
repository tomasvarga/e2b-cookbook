# Running Simulated Services in a Sandbox with Veris in JS/TS

This is an example of building and testing a feature that depends on a vendor API (here: Stripe) without standing that service up in production or staging. The code runs exactly as it would in production — same hostname (`api.stripe.com`), same credentials flow, completely unmodified — inside an E2B sandbox where a stateful [Veris](https://veris.ai) simulation of the vendor answers the calls. The customer the agent creates is really there when it reads it back; there's just no real Stripe behind it.

At the end, the example reads the **receipt** — what the simulation actually saw — and asserts the agent really made the calls: an agent that fabricated its API responses and one that really made them produce identical transcripts, but different receipts.

## Techstack

- [Veris SDK for E2B](https://www.npmjs.com/package/@veris-ai/e2b) (`@veris-ai/e2b`) — a drop-in subclass of E2B's `Sandbox` whose `create()` also provisions a Veris twin of your vendor stack
- [Anthropic AI SDK](https://www.npmjs.com/package/@anthropic-ai/sdk) for using Claude as an LLM
- JavaScript/TypeScript

## How it works

1. `Sandbox.create()` starts a normal E2B sandbox, provisions a Veris "twin" of your vendor stack, and points the sandbox's egress at it.
2. The example reads the Stripe API key the mock publishes (credentials are read from the mock, never invented — an invented key is refused exactly as the real vendor would refuse it) and injects it as `STRIPE_SECRET_KEY` into every command.
3. Claude gets one tool, `run_command`, and a task standing in for your feature's real job: create a Stripe customer and confirm it was persisted. It runs `curl` against `https://api.stripe.com` inside the sandbox; the mock answers, statefully — the customer it creates is there when it reads it back.
4. The example prints the receipt and calls `assertTouched('stripe', { method: 'POST', path: '/v1/customers' })`, which throws unless the mock really saw that request.
5. `kill()` tears down the sandbox and deletes the twin with it.

## Setup

### 1. Set up API keys

- Copy `.env.template` to `.env`
  - Get the [E2B API KEY](https://e2b.dev/docs/getting-started/api-key)
  - Get the [ANTHROPIC API KEY](https://console.anthropic.com/settings/keys)
  - Get the `VERIS_API_KEY` and a `VERIS_ENVIRONMENT_ID` from your [Veris dashboard](https://studio.veris.ai) — the environment decides which vendor mocks your sandbox gets, and it must include **Stripe** for this example

### 2. Install packages

```
npm i
```

### 3. Run the example

```
npm run start
```

You should see the agent's `curl` commands, its final summary with the created customer id, and a receipt like:

```
Receipt (mode=gateway, integrity=verified):
  stripe: 3 request(s)
    GET /v1/customers/cus_… -> 200
    POST /v1/customers -> 200
    GET /veris/data -> 200

PASS — the receipt proves the agent really called Stripe.
```

(The `GET /veris/data` entry is the example itself reading the key the mock publishes — the mock's control plane lives on the same service.)

If you encounter any problems, please let us know at the [E2B Discord](https://discord.com/invite/U7KEcGErtQ) or at [docs.veris.ai](https://docs.veris.ai).

### 4. Visit the docs

Check the [E2B documentation](https://e2b.dev/docs) and the [`@veris-ai/e2b` API reference](https://github.com/veris-ai/veris-e2b/blob/main/e2b/docs/reference.md) to learn more — interception modes, egress policy, webhooks, and the rest of the receipt API.
