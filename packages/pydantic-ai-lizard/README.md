# Pydantic AI with Lizard Sandboxes

Lizard (lizard.build) runs agent commands and files in cloud sandboxes. This package supplies a Pydantic AI workspace through the Lizard SDK. It works with workspace tools, including Pydantic AI Harness's `Shell`, `FileSystem`, and `Coder`.

## Install

Requires Python 3.11 or later and Pydantic AI 2.57 or later.

```sh
pip install pydantic-ai-lizard pydantic-ai-harness 'pydantic-ai-slim[openai]'
```

Create a project and API key at [lizard.build](https://lizard.build/). Set `LIZARD_API_KEY` and `LIZARD_PROJECT_ID` on the host. Set `OPENAI_API_KEY` for the example below. Keep credentials out of prompts and source control.

## Run an agent

```python
import asyncio

from pydantic_ai import Agent
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.shell import Shell
from pydantic_ai_lizard import LizardSandboxBackend


async def main():
    workspace = LizardSandboxBackend(lifetime_ms=600_000)
    agent = Agent("openai:gpt-5", capabilities=[Shell(), FileSystem()])
    try:
        result = await agent.run(
            "Use Python to compute the first ten Fibonacci numbers and save them to fib.txt.",
            workspace=workspace,
        )
        print(result.output)
        print(workspace.ref)
    finally:
        await workspace.destroy()


asyncio.run(main())
```

No API call happens in the constructor. The first operation creates one sandbox, even when tools start at the same time. Commands run as root in the `interpreter` template, with `/workspace` as the default working directory. `env=` contains only values you pass; the adapter never copies host environment variables into the sandbox.

## Reconnect and retain state

Save `backend.ref` after first use. Pass it to `LizardSandboxBackend(ref=ref)` to reconnect from a later run. An unknown or expired reference raises `WorkspaceUnavailableError`; it never creates a replacement. A run ending does not delete its workspace. Call `await backend.destroy()` when you no longer need it. The default lifetime is one hour; expiration discards the sandbox and its files.

You can also add `LizardSandbox()` to an agent's `capabilities`. It supplies the workspace, while `Shell`, `FileSystem`, or your own tools use it. `LizardSandbox.backend(ref)` builds a backend for a saved reference, and `await LizardSandbox().destroy(ref)` releases it. Deferred loading is unsupported because Pydantic AI selects the workspace before loading deferred tools.

## Behavior and limits

- Uses asyncio. The supported sizes are `small`, `medium`, and `large`.
- Implements `WorkspaceBackend`, `SupportsCommands`, `SupportsFilesystem`, and `SupportsRealpath`. Binary file transfers use the Lizard SDK; guest helpers preserve path errors, symlink behavior, and file metadata.
- Pass an argv list for literal arguments, or a string with `shell=True` for shell syntax.
- A nonzero exit returns `CommandResult`. Missing programs exit 127. Invalid UTF-8 becomes the replacement character.
- A timeout raises `WorkspaceTimeoutError` with partial output. Cancellation stops the foreground process group on a best-effort basis. Detached background jobs may continue until cleanup or expiration.
- Captures up to 10 MiB per output stream. Larger output raises `WorkspaceOutputLimitError`; it never reports truncated output as a successful result. Output uses files inside the sandbox while the command runs.
- Does not configure network isolation, install custom images, resume paused sandboxes, or renew a sandbox's lifetime on reconnect. Commands can reach the network. A paused sandbox must be resumed through the Lizard SDK before attaching.
- Commands can access the whole guest filesystem. The sandbox is the host isolation boundary; a working directory is not an access-control boundary.
- The API key stays on the host. Commands, files, and explicitly supplied environment values go to Lizard. See [privacy](https://lizard.build/privacy) and [pricing](https://lizard.build/pricing).

## Test

```sh
pip install -e '.[dev]'
ruff check .
ruff format --check .
mypy src
pytest tests -m 'not live' --disable-socket --allow-unix-socket
python -m build
twine check dist/*
```

Set `LIZARD_API_KEY`, `LIZARD_PROJECT_ID`, and `LIZARD_LIVE_TESTS=1`, then run `pytest tests -m live` to run Pydantic AI's `WorkspaceBackendSuite` against temporary cloud sandboxes. Cloud tests use billed resources and clean up sandboxes they create. The fixed lifetime bounds resources left by a lost connection.

Source: [lizard-build/integrations](https://github.com/lizard-build/integrations/tree/main/packages/pydantic-ai-lizard). Report bugs in the repository's [issue tracker](https://github.com/lizard-build/integrations/issues). Lizard maintains this package; Pydantic AI reviews catalog submissions separately.
