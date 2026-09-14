import { Anthropic } from '@anthropic-ai/sdk'
import { Sandbox, CommandExitError } from '@veris-ai/e2b'

import * as dotenv from 'dotenv'

dotenv.config()

const MODEL_NAME = 'claude-sonnet-5'
const MAX_TURNS = 10

const SYSTEM_PROMPT = `
## your job & context
you are an operations agent working inside a linux sandbox. you complete tasks by running shell commands with the \`run_command\` tool.
- the sandbox has curl, python3 and the usual unix tools.
- every command you run has the Stripe secret key in the STRIPE_SECRET_KEY environment variable. use it — never invent credentials.
- talk to the real Stripe API at https://api.stripe.com with curl, e.g.:
  curl -sS https://api.stripe.com/v1/customers -u "$STRIPE_SECRET_KEY:"
- commands are non-interactive. check the JSON a call returns before moving on.
- when the task is done, reply with a short summary instead of another tool call.
`

const tools: Array<Anthropic.Tool> = [
    {
        name: 'run_command',
        description: 'Run a shell command in the sandbox and return its exit code, stdout and stderr.',
        input_schema: {
            type: 'object',
            properties: {
                command: {
                    type: 'string',
                    description: 'The shell command to run.'
                }
            },
            required: ['command']
        }
    }
]

const client = new Anthropic()

async function runCommand(sandbox: Sandbox, command: string, envs: Record<string, string>): Promise<string> {
    console.log(`\n$ ${command}`)
    try {
        // `user: 'user'` matters: in proxy mode, root could bypass the interception rules.
        const result = await sandbox.commands.run(command, { user: 'user', envs, timeoutMs: 60_000 })
        console.log(result.stdout || result.stderr)
        return JSON.stringify({ exitCode: 0, stdout: result.stdout, stderr: result.stderr })
    } catch (error) {
        // A non-zero exit is feedback for the model, not a crash of the example.
        if (error instanceof CommandExitError) {
            console.log(`[exit ${error.exitCode}]`, error.stderr || error.stdout)
            return JSON.stringify({ exitCode: error.exitCode, stdout: error.stdout, stderr: error.stderr })
        }
        throw error
    }
}

async function chatWithClaude(sandbox: Sandbox, envs: Record<string, string>, userMessage: string): Promise<string> {
    console.log(`\n${'='.repeat(50)}\nUser Message: ${userMessage}\n${'='.repeat(50)}`)

    const messages: Anthropic.MessageParam[] = [{ role: 'user', content: userMessage }]

    for (let turn = 0; turn < MAX_TURNS; turn++) {
        const message = await client.messages.create({
            model: MODEL_NAME,
            system: SYSTEM_PROMPT,
            max_tokens: 4096,
            messages,
            tools,
        })
        messages.push({ role: 'assistant', content: message.content })

        const toolUses = message.content.filter(
            (block): block is Anthropic.ToolUseBlock => block.type === 'tool_use'
        )
        if (message.stop_reason !== 'tool_use' || toolUses.length === 0) {
            return message.content
                .filter((block): block is Anthropic.TextBlock => block.type === 'text')
                .map(block => block.text)
                .join('\n')
        }

        const toolResults: Anthropic.ToolResultBlockParam[] = []
        for (const toolUse of toolUses) {
            const output = toolUse.name === 'run_command'
                ? await runCommand(sandbox, (toolUse.input as { command: string }).command, envs)
                : `unknown tool: ${toolUse.name}`
            toolResults.push({ type: 'tool_result', tool_use_id: toolUse.id, content: output })
        }
        messages.push({ role: 'user', content: toolResults })
    }

    throw new Error(`Agent did not finish within ${MAX_TURNS} turns.`)
}

async function run() {
    // A drop-in subclass of E2B's Sandbox: create() also provisions a Veris "twin"
    // of your vendor stack and points the sandbox's egress at it. Code inside the
    // sandbox keeps its production hostnames — api.stripe.com — but the calls are
    // answered by a stateful, contract-accurate mock.
    const sandbox = await Sandbox.create({ timeoutMs: 10 * 60_000 })
    console.log(`sandbox ${sandbox.sandboxId} · veris ${sandbox.verisSandboxId} · mode ${sandbox.verisMode}`)

    try {
        // Read the credentials the mock publishes — never invent them. An invented
        // key is refused exactly as the real vendor would refuse it.
        const services = await sandbox.veris.services()
        const stripe = services.find(service => service.name === 'stripe')
        if (!stripe) {
            const names = services.map(service => service.name).join(', ') || 'none'
            throw new Error(`This Veris environment has no 'stripe' service (found: ${names}). Use an environment that includes Stripe.`)
        }
        const config = await fetch(`${stripe.control_url}/veris/data?entity_type=config`).then(res => res.json())
        const stripeKey: string = config.rows[0].api_keys[0]
        console.log(`stripe mock published key ${stripeKey.slice(0, 12)}…`)

        const report = await chatWithClaude(
            sandbox,
            { STRIPE_SECRET_KEY: stripeKey },
            'Create a Stripe customer named "Ada Lovelace" with email ada@example.com, then fetch that customer back by id to confirm it was persisted, and finish with a one-line summary that includes the customer id.'
        )
        console.log(`\n${'='.repeat(50)}\nAgent Report:\n${report}\n${'='.repeat(50)}`)

        // The receipt: what the mock actually saw. An agent that fabricated the
        // API responses and one that really made the calls produce identical
        // transcripts — they produce different receipts.
        const receipt = await sandbox.veris.receipt()
        console.log(`\nReceipt (mode=${receipt.mode}, integrity=${receipt.integrity}):`)
        for (const [name, entry] of Object.entries(receipt.services)) {
            if (!entry) continue
            console.log(`  ${name}: ${entry.requests} request(s)`)
            for (const request of entry.entries) {
                console.log(`    ${request.method} ${request.path} -> ${request.status}`)
            }
        }

        // Throws unless Stripe really saw a matching request.
        await sandbox.veris.assertTouched('stripe', { method: 'POST', path: '/v1/customers' })
        console.log('\nPASS — the receipt proves the agent really called Stripe.')
    } catch (error) {
        console.error('An error occurred:', error)
        process.exitCode = 1
    } finally {
        // kill() also deletes the Veris twin.
        await sandbox.kill()
    }
}

run()
