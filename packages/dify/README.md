# Lizard Sandboxes for Dify

Lizard (lizard.build) runs commands and files in cloud sandboxes. This plugin connects Dify workflows and agents to Lizard Sandboxes through the Lizard SDK.

## Setup

1. Upload the `.difypkg` package under **Plugins → Install from local package file**. Marketplace publication requires a separate review.
2. Create a project and API key at [lizard.build](https://lizard.build/).
3. Open the plugin's authorization settings. Enter the API key and the project's ID. Validation lists the project's sandboxes; it does not create a resource.
4. Add the tools below to a workflow or agent. Keep credentials in the provider settings, not in tool parameters or prompts.

The plugin is free to install. Lizard usage requires a Lizard account: Pro costs $19/month and includes $19 of usage. New accounts have a seven-day trial with $5 of usage and a required card. See [current pricing](https://lizard.build/pricing).

## Example workflow

Connect these tool nodes in order:

1. **Create sandbox**: size `small`, lifetime `600` seconds. Save `json.sandbox_id`.
2. **Write file**: pass that ID, path `example.py`, and content `print(6 * 7)`.
3. **Run command**: pass the same ID and command `python3 example.py`. The JSON output contains `stdout`, `stderr`, `exit_code`, and `truncated`.
4. **Read file**: pass the same ID and path `example.py` to retrieve its text.
5. **Destroy sandbox**: pass the same ID when the workflow is done.

Always route errors to cleanup when possible. If a workflow stops before cleanup, the fixed lifetime expires the sandbox and discards its files. The default is ten minutes; the maximum is thirty minutes.

## Tools and limits

| Tool | Behavior |
| --- | --- |
| Create sandbox | Creates an `interpreter` sandbox in the configured project. Sizes: small, medium, large. Lifetime: 60–1800 seconds. |
| Run command | Runs a shell command in `/workspace`, as root. Deadline: 1–120 seconds, default 30. Captures up to 64 KiB per output stream and requests termination when output exceeds the cap. |
| Write file | Writes up to 64 KiB of UTF-8 text and creates parent directories. |
| Read file | Reads the first 64 KiB of a regular file. Returns `truncated: true` if more data exists. Invalid UTF-8 becomes the replacement character. |
| Destroy sandbox | Stops the sandbox and discards its files. |

Only sandboxes created by this plugin in the configured project are accepted. File paths refer to the guest: relative paths start in `/workspace`, and absolute paths address the guest filesystem. Commands can access the whole guest and the internet. The working directory is not an access-control boundary. Do not send secrets to untrusted code.

The SDK's command stream normalizes line breaks; use the file tools when exact text matters. Command output that reaches the cap returns `truncated: true`, an error, and `exit_code: null`. Failed API calls return a fixed error message without the provider's raw response or credentials.

Command termination is best effort. Detached child processes can remain until you destroy the sandbox or its lifetime expires.

## Data and connections

The plugin sends credentials only to `https://lizard.build`. Commands, file contents, and results pass between Dify and Lizard. Code running in the sandbox can contact other services; the plugin does not restrict guest egress. See [PRIVACY.md](PRIVACY.md) for data handling.

## Source and support

[Source repository](https://github.com/lizard-build/integrations/tree/main/packages/dify). [Report a bug](https://github.com/lizard-build/integrations/issues) or contact team@lizard.build.

Lizard maintains this plugin. Dify reviews Marketplace submissions separately. The package targets Python 3.12 and Dify Plugin SDK 0.10.2.
