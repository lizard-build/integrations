# TanStack AI with Lizard Sandboxes

Lizard (lizard.build) runs agent code in cloud sandboxes. This package implements TanStack AI's `SandboxProvider` contract through the Lizard SDK.

## Install and run

Requires Node 22.13 or later and `@tanstack/ai-sandbox` 0.5.23 or later within the 0.5 series.

```sh
npm install @lizard-build/tanstack-ai @tanstack/ai-sandbox
```

Create a project and API key at [lizard.build](https://lizard.build/). Set `LIZARD_API_KEY` and `LIZARD_PROJECT_ID` on the host. Keep the API key out of prompts and source control.

```ts
import { defineSandbox } from '@tanstack/ai-sandbox'
import { lizardSandbox } from '@lizard-build/tanstack-ai'

const sandbox = defineSandbox({
  id: 'code-workspace',
  provider: lizardSandbox({ lifetimeMs: 600_000 }),
  lifecycle: { reuse: 'thread', snapshot: 'none' },
})

const context = { threadId: 'example-thread', runId: 'example-run' }
try {
  const handle = await sandbox.ensure(context)
  await handle.fs.write('example.py', 'print(6 * 7)')
  const result = await handle.process.exec('python3 example.py')
  console.log(result.stdout) // 42
} finally {
  await sandbox.destroy(context)
}
```

Pass the same definition to TanStack AI's `withSandbox` integration when running a harness. Use a shared instance store if more than one host needs to resume a thread. The provider's `resume({ id })` reconnects to a running sandbox without extending its lifetime. It returns `null` for a missing sandbox. Resume a paused sandbox through the Lizard SDK before reconnecting.

## Configuration

| Option | Default | Purpose |
| --- | --- | --- |
| `apiKey` | `LIZARD_API_KEY` | Host-side API credential. Never copied into the guest. |
| `projectId` | `LIZARD_PROJECT_ID` | Project that owns new sandboxes. |
| `size` | `small` | `small`, `medium`, or `large`. |
| `lifetimeMs` | `1800000` | Fixed sandbox lifetime in milliseconds. Expiration discards files. |
| `commandTimeoutMs` | `600000` | Maximum time for each command, including background processes. |
| `workdir` | `/workspace` | Absolute guest path that backs the virtual `/workspace` path. |

Each sandbox uses the `interpreter` template and runs commands as root. Relative paths start in the working directory. Absolute paths address the guest filesystem; a working directory is not an access-control boundary.

## Capabilities and limits

| Supported | Details |
| --- | --- |
| Files and directories | Binary reads and writes, listing, rename, remove, and `lstat`. Writes accept up to 4 MiB. |
| Commands | Foreground execution and background output streams; real exit codes; invalid UTF-8 becomes the replacement character. |
| Cancellation | Stops the remote process group on abort or `kill()`. Supports SIGTERM and SIGKILL; both force termination. |
| Environment | Explicit create-time values and `env.set()` updates. Updates remain available after reconnecting. |
| HTTP ports | `ports.connect()` returns an HTTPS URL and an access-token header. Send the returned headers with each request. |

The provider captures up to 10 MiB per command output stream. A command that exceeds the cap or its deadline fails and stops. Consume each background output stream once. Stdin is at EOF; interactive stdin is unsupported.

Native snapshots, forks, durable files after expiration, and guest network restrictions are unsupported. `network: 'deny'` fails before creating a sandbox. Guest code can reach the internet. Keep credentials for untrusted code out of guest environment values and files.

Always call `destroy()` when the sandbox is no longer needed. A fixed lifetime bounds resources left by a lost connection. Commands, files, and explicit guest environment values go to Lizard. See [pricing](https://lizard.build/pricing) and [privacy](https://lizard.build/privacy).

## Test

```sh
npm ci
npm run build
npm run typecheck
npm test
npm pack --dry-run
```

Set both Lizard credentials and `LIZARD_LIVE_TESTS=1`, then run `npm run test:live`. The suite covers files, reconnects, environment updates, cancellation, HTTP ports, and TanStack AI's seven journal conformance tests. Cloud tests use billed resources and delete the sandboxes they create. Without credentials, they report skips.

Lizard maintains this package. TanStack reviews catalog submissions separately. [Source and examples](https://github.com/lizard-build/integrations/tree/main/packages/tanstack-ai), [report a bug](https://github.com/lizard-build/integrations/issues).
