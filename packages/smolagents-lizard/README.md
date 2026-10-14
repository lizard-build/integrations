# smolagents-lizard

Lizard (lizard.build) provides a remote Python executor for smolagents. The agent and model calls stay on the host; generated Python runs in a persistent kernel in Lizard Sandboxes.

## Install and use

Requires Python 3.11+, smolagents 1.26, a Lizard project, and a project API key. The first PyPI release is pending. Install from source:

```sh
pip install 'smolagents-lizard @ git+https://github.com/lizard-build/integrations.git#subdirectory=packages/smolagents-lizard'
```

Set `LIZARD_API_KEY` and `LIZARD_PROJECT_ID`. The example also needs `HF_TOKEN` for its model provider.

```python
from smolagents import CodeAgent, InferenceClientModel
from smolagents_lizard import LizardExecutor

with LizardExecutor() as executor:
    agent = CodeAgent(tools=[], model=InferenceClientModel(), executor=executor)
    print(agent.run("Use Python to sum the integers from 1 to 100."))
```

Pass the executor instance through `executor=`. This package works with the released 1.26 API and does not use a pending executor registry. It pins smolagents below 1.27 because 1.26 exposes final-answer decoding through a private base-class method.

## Behavior

- Python variables persist until cleanup.
- `additional_imports=["numpy"]` installs packages in the remote kernel.
- The base executor sends tools and state through the smolagents protocol.
- Final answers support the base executor's safe formats. Pickle decoding stays disabled.
- Python failures raise `AgentError` with remote logs and traceback.
- Use the context manager or call `cleanup()`. A failed deletion retains its handle for retry.

Options include `project_id`, `api_key`, `api_url`, `size`, `lifetime_ms`, and `command_timeout_ms`. Defaults are a small interpreter sandbox, a 15-minute lifetime, and a 60-second code timeout; the maximum code timeout is 120 seconds.

Remote Python is not restricted by the local executor's import allowlist. Managed agents and host-only tools do not move to the sandbox. Only pass tools whose source and dependencies work in a remote Python process. The package does not offer arbitrary Docker images or return host Python objects through pickle.

## Checks

```sh
pip install -e '.[dev]'
pytest tests -m 'not live' --disable-socket --allow-unix-socket
# With Lizard credentials:
LIZARD_LIVE_TESTS=1 pytest tests -m live
```

Cloud tests cover persistent variables, package installation, state and tools, a safe final answer, and `CodeAgent` with a scripted model.
