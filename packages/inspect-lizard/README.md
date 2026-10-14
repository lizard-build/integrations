# inspect-lizard

Lizard (lizard.build) supplies an Inspect AI `SandboxEnvironment` backed by Lizard Sandboxes. Each evaluation sample gets its own sandbox; sample cleanup deletes it.

## Install and use

Requires Python 3.11+, Inspect AI 0.3.280+, a Lizard project, and a project API key. The first PyPI release is pending. Install from source:

```sh
pip install 'inspect-lizard @ git+https://github.com/lizard-build/integrations.git#subdirectory=packages/inspect-lizard'
```

Set `LIZARD_API_KEY` and `LIZARD_PROJECT_ID`. The package registers the name `lizard` through Inspect's extension entry point.

```python
from inspect_ai import Task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate, use_tools
from inspect_ai.tool import bash
from inspect_lizard import LizardSandboxConfig

my_task = Task(
    dataset=[Sample(input="Use Python in bash to calculate 6 * 7.", target="42")],
    solver=[use_tools(bash()), generate()],
    scorer=includes(),
    sandbox=("lizard", LizardSandboxConfig()),
)
```

Run the [task example](examples/eval.py) with `inspect eval examples/eval.py --model <provider/model>` and that model provider's credentials. You can also use `--sandbox lizard` with tasks that do not require Docker-specific setup.

## Contract and limits

The adapter supports argv commands, stdin bytes or text, cwd, environment variables, Linux users, timeouts, UTF-8 text, and binary files. It keeps stdout and stderr tails within Inspect's limits. Large reads and writes use chunks to stay within the SDK transport limits. It does not retry commands after a timeout.

Configuration fields are `project_id`, `template="interpreter"`, `size`, and `lifetime_ms`. Defaults are small size and a 30-minute lifetime. The provider suggests two concurrent samples; set Inspect's sample limit to suit your account and budget.

The template and SDK file operations run as root. `exec(user=...)` can select a Linux user already in the sandbox. Changing a file's mode does not remove root's access. Three upstream checks for those chmod cases have strict expected failures. Execute permission errors still raise `PermissionError`.

This adapter does not build Dockerfiles, run Compose services, or expose Docker-compatible images. Files last for the sample's sandbox lifetime. Cleanup retries failed sample deletions at task shutdown. `inspect sandbox cleanup lizard <sandbox-id>` only deletes a sandbox with this integration's metadata.

Output retention caps the data returned to the host. A command can still write files inside its sandbox, including temporary output files, up to the sandbox's disk limit.

## Checks

```sh
pip install -e '.[dev]'
pytest tests -m 'not live' --disable-socket --allow-unix-socket
# Runs Inspect's portable sandbox checks against the cloud:
LIZARD_LIVE_TESTS=1 pytest tests -m live
```

Unit tests exercise the command worker and cover AnyIO cancellation during creation and command startup, timeout cleanup, large output, file errors, large file reads, and cleanup retries.
