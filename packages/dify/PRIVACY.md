# Privacy

Lizard (lizard.build) maintains this Dify plugin. Contact team@lizard.build for plugin questions.

The plugin uses the API key and project ID stored in Dify's provider settings. It sends the key over HTTPS to `lizard.build` to authenticate requests. It does not pass the key into a sandbox, return it in tool output, or write it to plugin logs.

Commands, file paths, file contents, sandbox IDs, and command results pass between Dify and Lizard. Dify may retain workflow inputs, outputs, and logs according to the workspace's settings. Lizard processes this data to run the sandbox service under its [privacy policy](https://lizard.build/privacy) and terms.

The plugin has no separate database, analytics, advertising, or tracking. Sandboxes contain files and processes until they are destroyed or expire. The plugin's lifetime is at most thirty minutes. Destroying a sandbox discards its guest files; this does not delete Dify's workflow history or service operational records.

Code and commands in a sandbox can reach the internet and send data to destinations chosen by the workflow. The plugin does not restrict that traffic. Only run code you intend to execute, and avoid putting sensitive data or credentials in commands and files unless needed.

The plugin returns command output and file text to Dify. Review these outputs before sharing them. Errors from the API use fixed messages to avoid exposing credentials or raw response bodies.
