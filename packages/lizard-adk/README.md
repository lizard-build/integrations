# lizard-adk

Lizard (lizard.build) connects ADK agents to Lizard Sandboxes. The plugin supplies four tools and releases each sandbox when its ADK invocation ends.

## Install

Requires Python 3.11+, ADK 2.12+, a Lizard project, and a project API key.

```sh
pip install lizard-adk
```

Set `LIZARD_API_KEY` and `LIZARD_PROJECT_ID`. The [agent example](examples/agent.py) also needs `GOOGLE_API_KEY` for Gemini.

## Use

```python
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from lizard_adk import LizardPlugin

plugin = LizardPlugin()
root_agent = Agent(
    name="sandbox_agent",
    model="gemini-flash-latest",
    tools=plugin.get_tools(),
)
runner = InMemoryRunner(
    app=App(name="sandbox_example", root_agent=root_agent, plugins=[plugin]),
    plugin_close_timeout=120,
)
# Run the agent, then always await runner.close() in a finally block.
```

Register the plugin in both places: `get_tools()` exposes the tools, and `App.plugins` runs cleanup. See [the complete example](examples/agent.py) for session creation, a prompt, and cleanup.

| Tool | Result |
| --- | --- |
| `lizard_execute_command` | Shell stdout, stderr, and exit code |
| `lizard_execute_code` | Python output, display results, and a structured error |
| `lizard_read_file` | UTF-8 text, up to 1 MiB |
| `lizard_write_file` | Write UTF-8 text, up to 1 MiB |

Python variables and files persist across calls within one invocation. A later user message starts a new invocation and gets a fresh sandbox. The host keeps the Lizard API key; it does not put it in session state or tool results.

## Options and limits

Pass `project_id`, `api_key`, `api_url`, `size`, `lifetime_ms`, or `command_timeout_ms` to `LizardPlugin`. Defaults are the small interpreter template, a 15-minute lifetime, and a 60-second command timeout. The maximum command timeout is 120 seconds. The command tool defaults to `/home/user`.

Shell output follows the Lizard SDK response limits. Use files for larger results. This plugin does not persist a sandbox between invocations or support custom Docker images. Always close the runner after errors or cancelled runs. If cleanup fails, retry `plugin.close()`; it retains the sandbox handle until deletion succeeds.

## Checks

Unit tests cover schemas, invocation isolation, files, errors, cancellation, and cleanup retries. Cloud tests exercise each tool and a real ADK runner with a scripted model, so they do not call a paid model API.

```sh
pip install -e '.[dev]'
pytest tests -m 'not live' --disable-socket --allow-unix-socket
# With Lizard credentials:
LIZARD_LIVE_TESTS=1 pytest tests -m live
```
