# Sandbox integrations

Lizard (lizard.build) provides cloud sandboxes for agent code. This repository connects agent frameworks to Lizard Sandboxes through the Lizard SDK.

| Package | Framework | Use |
| --- | --- | --- |
| [lizard-adk](packages/lizard-adk) | ADK 2.12+ | Shell, Python, and file tools; one sandbox per invocation |
| [smolagents-lizard](packages/smolagents-lizard) | smolagents 1.26 | Persistent Python executor for `CodeAgent` |
| [inspect-lizard](packages/inspect-lizard) | Inspect AI 0.3.280+ | One sandbox per evaluation sample |
| [@lizard-build/mastra](packages/mastra) | Mastra 1.75+ | Workspace command execution and file writes |
| [@lizard-build/opencode](packages/opencode) | OpenCode 2.0.30 / 1.19.2 | Per-session shell and text file tools |
| [pydantic-ai-lizard](packages/pydantic-ai-lizard) | Pydantic AI 2.57+ | Workspace commands, binary files, and reconnects |
| [@lizard-build/tanstack-ai](packages/tanstack-ai) | TanStack AI Sandbox 0.5.23 | Sandbox provider with commands, files, background processes, and HTTP ports |
| [Lizard Sandboxes for Dify](packages/dify) | Dify Plugin SDK 0.10.2 | Five tools to create, run commands, write, read, and destroy |

Lizard maintains these external packages. The Python packages are available on PyPI: [lizard-adk](https://pypi.org/project/lizard-adk/), [smolagents-lizard](https://pypi.org/project/smolagents-lizard/), and [inspect-lizard](https://pypi.org/project/inspect-lizard/). The [OpenCode package](https://www.npmjs.com/package/@lizard-build/opencode) is available on npm. The first npm release of `@lizard-build/mastra` is pending; use its [source install steps](packages/mastra/README.md). Framework maintainers review catalog entries separately.

## Setup

Create a project and an API key at [lizard.build](https://lizard.build/). Set `LIZARD_API_KEY` and `LIZARD_PROJECT_ID` in the host environment. Keep the API key out of source control and agent prompts. Each adapter creates its own sandbox with a fixed lifetime and a cleanup hook. Cloud tests use billed resources.

All adapters use the `interpreter` template. They do not accept arbitrary Docker images or Compose files. See each package for its cleanup rules and limits.

The [Pydantic AI package](https://pypi.org/project/pydantic-ai-lizard/) is available on PyPI. The first release of `@lizard-build/tanstack-ai` awaits npm publication; build `packages/tanstack-ai` and install its `npm pack` archive to use its source. Dify Marketplace approval is separate from packaging and testing.

## Test

Python 3.11 or later:

```sh
cd packages/lizard-adk  # or smolagents-lizard, inspect-lizard, pydantic-ai-lizard
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
mypy src
pytest tests -m 'not live' --disable-socket --allow-unix-socket
python -m build
```

Node 22.13 or later:

```sh
cd packages/mastra  # or opencode, tanstack-ai
npm ci
npm run build
npm test
```

To run cloud tests, set both credentials above and `LIZARD_LIVE_TESTS=1`. For Python, run `pytest tests -m live`; for Mastra, run `npm test -- tests/live.test.ts`. For OpenCode, run `npm run test:live`. Each test cleans up its own sandbox. A fixed lifetime bounds resources left by a lost connection.

The Inspect tests also run the portable checks shipped with Inspect AI. Three permission checks have strict expected failures: the interpreter template and the SDK file API run as root, so root can read or write files after `chmod -r` or `chmod -w`. This is distinct from executing a file without an execute bit, which raises `PermissionError`.

Pydantic AI runs its upstream `WorkspaceBackendSuite`. The root-permission test skips when the guest runs as root. TanStack AI runs seven upstream journal conformance tests plus provider checks with `npm run test:live`.

For Dify, use Python 3.12, install `packages/dify/requirements.txt` and pytest, then run `python -c 'import dify_plugin; import pytest; raise SystemExit(pytest.main(["tests", "-m", "not live"]))'` from that package directory. Import Dify first because its runtime patches Python networking with gevent. To run the cloud workflow, change the marker to `live` and set the credentials above.

## Release

Each Python package has its own workflow. PyPI requires a distinct publisher identity to create each new project; see [warehouse#16920](https://github.com/pypi/warehouse/issues/16920).

| PyPI project name | Workflow filename |
| --- | --- |
| `lizard-adk` | [publish-python.yml](.github/workflows/publish-python.yml) |
| `smolagents-lizard` | [publish-smolagents.yml](.github/workflows/publish-smolagents.yml) |
| `inspect-lizard` | [publish-inspect.yml](.github/workflows/publish-inspect.yml) |
| `pydantic-ai-lizard` | [publish-pydantic-ai.yml](.github/workflows/publish-pydantic-ai.yml) |

Configure Trusted Publishers with GitHub owner `lizard-build`, repository `integrations`, the workflow filename above, and no environment name. Manage each publisher from its PyPI project's Publishing page. For a new release, update the version in the package's `pyproject.toml`, then run its workflow from `main` with that version. The workflow checks the version, runs tests, builds the files, and publishes through PyPI Trusted Publishing.

The TypeScript packages use the npm scope `@lizard-build`. Build, test, and inspect `npm pack --dry-run` before publishing from an account with access to that scope.

Package Dify with `dify plugin package packages/dify` using the official Dify Plugin CLI. Submit the `.difypkg` to `langgenius/dify-plugins` with the required security disclosures and test results.

## Maintenance

Report bugs in [Issues](https://github.com/lizard-build/integrations/issues). Run the package checks before opening a PR. Changes to the shared Python command worker must update its TypeScript copy with `python scripts/sync-worker.py`.

Code uses the Apache 2.0 license. Lizard Sandboxes is a hosted service with its own [pricing and terms](https://lizard.build/).
