# @lizard-build/mastra

Lizard (lizard.build) provides a `WorkspaceSandbox` for Mastra. It runs commands and writes files in Lizard Sandboxes through the Lizard SDK.

## Install from source

Requires Node 22.13+, Mastra 1.75+, a Lizard project, and a project API key. The first npm release is pending.

```sh
git clone https://github.com/lizard-build/integrations.git
cd integrations/packages/mastra
npm ci
npm run build
npm pack
# In your application, install the resulting lizard-build-mastra-0.1.0.tgz.
```

Set `LIZARD_API_KEY` and `LIZARD_PROJECT_ID`, then use the [complete workspace example](examples/workspace.ts):

```typescript
import { Workspace } from '@mastra/core/workspace';
import { LizardSandbox } from '@lizard-build/mastra';

const sandbox = new LizardSandbox();
const workspace = new Workspace({ sandbox });
try {
  await workspace.init();
  const result = await sandbox.executeCommand('python3', ['-c', 'print(6 * 7)']);
  console.log(result.stdout);
} finally {
  await workspace.destroy();
  await sandbox.destroy();
}
```

## Behavior

- Commands support args, cwd, environment variables, millisecond timeouts, and `AbortSignal`.
- With a nonempty args array, the provider executes argv directly. Without args, it runs the command string through `/bin/sh -c`.
- Output uses separate stdout and stderr files. `maxRetainedBytes` caps each returned tail and reports dropped bytes. The default is 10 MiB per stream.
- `writeFiles()` accepts text or bytes and optional Unix file modes.
- `stop()` pauses the sandbox; `start()` resumes it; `destroy()` deletes it. A failed deletion can be retried.
- Concurrent starts share one sandbox. The default lifetime is 30 minutes.

Options include `projectId`, `apiKey`, `apiUrl`, `size`, `lifetimeMs`, `workingDirectory`, `env`, and `id`. The default working directory is a private directory under `/tmp` in the sandbox.

This adapter provides command execution and direct file writes. It does not implement a Mastra filesystem provider, filesystem mounts, background process management, live output callbacks, public port URLs, or checkpoints. `supportsCheckpoints` is false, and `snapshot()` has no effect. It uses the interpreter template and does not build arbitrary Docker images.

Always destroy the workspace and sandbox in a `finally` block. A cancelled file batch can leave earlier files written. Output retention does not cap disk use while a command runs.

## Checks

```sh
npm ci
npm run build
npm test
# With Lizard credentials:
LIZARD_LIVE_TESTS=1 npm test -- tests/live.test.ts
```

The tests cover the Mastra Workspace contract, argv, files, output limits, timeouts, cancellation, lifecycle calls, and cleanup retries. The cloud test also checks pause and resume.
