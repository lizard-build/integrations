# OpenCode tools for Lizard Sandboxes

Lizard (lizard.build) gives each OpenCode session its own cloud sandbox. This plugin adds shell and text file tools through the Lizard SDK. It supports OpenCode 2.0.30 and OpenCode 1.19.2 through separate entry points.

## Install

Use Node 22.13 or later and choose the entry for your OpenCode version.

### OpenCode 2

Add this to your project's `opencode.json`:

```json
{
  "plugins": [
    {
      "package": "@lizard-build/opencode",
      "options": { "lifetimeMs": 900000 }
    }
  ]
}
```

OpenCode installs the package and supplies `@opencode/plugin` 2.0.30 and Effect 4 at runtime. See the [config example](examples/opencode-v2.json).

### OpenCode 1.19

In the target project:

```sh
mkdir -p .opencode/plugins
npm install --prefix .opencode @lizard-build/opencode @opencode-ai/plugin@1.19.2
```

Save this as `.opencode/plugins/lizard.ts`:

```ts
export { default } from '@lizard-build/opencode/v1';
```

OpenCode loads this local plugin. The root entry targets OpenCode 2; version 1 needs `/v1`.

### Source install

```sh
git clone https://github.com/lizard-build/integrations.git
cd integrations/packages/opencode
npm ci
npm run build
npm run typecheck
npm test
npm pack
```

For OpenCode 2, use the absolute package directory as the `package` value, for example `/path/to/integrations/packages/opencode`. The local `server.js` entry loads the build. For version 1, install the resulting `lizard-build-opencode-0.1.0.tgz` in `.opencode` in place of the npm package above. The tarball also supports the standalone [session example](examples/session.mjs).

## Credentials and first run

Create a project and API key at [lizard.build](https://lizard.build/). Set `LIZARD_API_KEY` and `LIZARD_PROJECT_ID` in the environment that starts OpenCode. An optional `LIZARD_API_URL` selects another API endpoint. Keep credentials out of prompts, config files, and source control. CLI login alone does not set these SDK variables.

Start OpenCode, then ask:

```text
Use the lizard_sandbox tools. Write hello.py in my sandbox with print("hello"),
run it with python3, read the file back, then close the sandbox.
```

Each tool checks OpenCode's `lizard_sandbox` permission with the operation (`create`, `exec`, `read`, `write`, or `close`) as its resource. Your OpenCode permission rules decide whether to allow, deny, or prompt. An operation denied by those rules makes no SDK call. Automatic cleanup on session deletion or plugin disposal removes only sandboxes created by this plugin instance.

You can also run [examples/session.mjs](examples/session.mjs) after installing the tarball in its parent project. It needs the same environment variables and closes its sandbox in `finally`.

## Tools

| Tool | Input | Result |
| --- | --- | --- |
| `lizard_sandbox_create` | None | Sandbox ID, working directory, lifetime limit |
| `lizard_sandbox_exec` | `command`, optional `cwd` and `timeoutMs` | Exit code, stdout, stderr, truncation flag |
| `lizard_sandbox_read` | `path` | UTF-8 file content |
| `lizard_sandbox_write` | `path`, `content` | Remote path and bytes written |
| `lizard_sandbox_close` | None | Whether a sandbox was closed |

Tools create a sandbox on first use and reuse it for that session. Calls within a session run in order. Separate sessions use separate sandboxes. Tools do not take a sandbox ID from the model.

## Limits and cleanup

- Built-in OpenCode tools still use the local machine. Use the `lizard_sandbox_*` tools for remote work. The plugin does not copy your repository or host environment into a sandbox.
- Sandboxes use the `interpreter` template, size `small`, and `/workspace` as the default directory. Paths refer only to the remote filesystem. This template runs commands as root inside the sandbox.
- The lifetime is 15 minutes from creation by default, with no renewal. The plugin option `lifetimeMs` accepts 60,000–3,600,000 milliseconds. Expiry loses files; the next call reports it without replaying the operation. A later call can create a fresh sandbox.
- Commands default to a 60-second timeout and accept up to 120 seconds. A cancelled call, timed-out call, or failed command request destroys the session's sandbox to stop remote work. A nonzero command exit code returns normally and preserves it. Destruction loses all files in that sandbox.
- Reads and writes accept up to 64 KiB of UTF-8 text. Each command output stream is capped at 64 KiB, with `truncated: true` when needed. Use shell commands for directory creation, binary files, or partial reads.
- Session deletion and plugin disposal trigger cleanup. If the host crashes, the server-side lifetime limits resource use. If deletion fails, the handle stays available for another `lizard_sandbox_close` attempt. Cancellation during creation waits for the API response, then deletes the sandbox.
- Sandboxes use billed cloud resources. This package does not choose a subscription or change billing settings.

## Tests

```sh
npm ci
npm run build
npm run typecheck
npm test
npm pack --dry-run
```

Local tests cover session isolation, permissions, both plugin APIs, UTF-8 limits, timeout, cancellation, expiry, and cleanup retries. Set the two credential variables and `LIZARD_LIVE_TESTS=1`, then run `npm run test:live` for real cloud tests. These tests create two short-lived sandboxes and clean them up.

Lizard maintains this external plugin. Report bugs in the [integration repository](https://github.com/lizard-build/integrations/issues). Apache 2.0 license.
