"""RemotePythonExecutor for the released smolagents 1.26 interface."""

from __future__ import annotations

import base64
import os
from io import BytesIO
from typing import Any

from lizard import CodeSandbox
from smolagents.local_python_executor import CodeOutput
from smolagents.monitoring import AgentLogger
from smolagents.remote_executors import RemotePythonExecutor
from smolagents.utils import AgentError


class LizardExecutor(RemotePythonExecutor):
    """Execute Python in a persistent kernel inside Lizard Sandboxes.

    Pass the instance as ``CodeAgent(executor=executor, ...)``. The package
    targets smolagents 1.26; it does not require the pending executor registry.
    Pickle is disabled so sandbox output cannot deserialize code on the host.
    """

    def __init__(
        self,
        additional_imports: list[str] | None = None,
        logger: AgentLogger | None = None,
        *,
        project_id: str | None = None,
        api_key: str | None = None,
        api_url: str | None = None,
        size: str = "small",
        lifetime_ms: int = 900_000,
        command_timeout_ms: int = 60_000,
    ) -> None:
        project = project_id or os.environ.get("LIZARD_PROJECT_ID")
        if not project:
            raise ValueError("Set LIZARD_PROJECT_ID or pass project_id.")
        if not 0 < command_timeout_ms <= 120_000 or lifetime_ms <= 0:
            raise ValueError("Use a positive lifetime and a command timeout of 1–120000 ms.")
        super().__init__(additional_imports or [], logger or AgentLogger(), allow_pickle=False)
        self._timeout = command_timeout_ms
        self.sandbox: CodeSandbox | None = None
        self.sandbox = CodeSandbox.create(
            project_id=project,
            api_key=api_key,
            api_url=api_url,
            size=size,
            timeout_ms=lifetime_ms,
            metadata={"integration": "smolagents"},
        )
        try:
            self.installed_packages = self.install_packages(self.additional_imports)
        except BaseException:
            self.cleanup()
            raise

    def install_packages(self, additional_imports: list[str]) -> list[str]:
        """Install packages into the running kernel and refresh its import paths."""
        if additional_imports:
            code = (
                "import importlib, os, site, subprocess, sys\n"
                f"subprocess.run([sys.executable, '-m', 'pip', 'install', '--', "
                f"*{additional_imports!r}], check=True)\n"
                "user_site = site.getusersitepackages()\n"
                "if os.path.isdir(user_site) and user_site not in sys.path:\n"
                "    system_sites = set(site.getsitepackages())\n"
                "    positions = [i for i, p in enumerate(sys.path) if p in system_sites]\n"
                "    sys.path.insert(min(positions) if positions else len(sys.path), user_site)\n"
                "importlib.invalidate_caches()\n"
            )
            self.logger.log(self.run_code_raise_errors(code).logs)
        return additional_imports

    def run_code_raise_errors(self, code: str) -> CodeOutput:
        """Return the result or raise AgentError for a remote Python error."""
        if self.sandbox is None:
            raise RuntimeError("The Lizard executor is closed.")
        result = self.sandbox.run_code(code, timeout_ms=self._timeout)
        logs = result.stdout + result.stderr
        if result.error:
            if result.error.name == self.FINAL_ANSWER_EXCEPTION:
                # smolagents 1.26 exposes this only on the base class; the package
                # pins that release line until the public replacement ships.
                answer = self._deserialize_final_answer(result.error.message, allow_pickle=False)
                return CodeOutput(output=answer, logs=logs, is_final_answer=True)
            raise AgentError(
                f"{logs}\n{result.error.name}: {result.error.message}\n{result.error.traceback}",
                self.logger,
            )
        output: Any = None
        for item in reversed(result.results):
            if item.mime in {"image/png", "image/jpeg"}:
                from PIL import Image

                with Image.open(BytesIO(base64.b64decode(item.data))) as img:
                    output = img.copy()
                break
            if item.mime == "text/plain":
                output = item.data
                break
        return CodeOutput(output=output, logs=logs, is_final_answer=False)

    def cleanup(self) -> None:
        """Release the sandbox. A failed cleanup can be retried."""
        if self.sandbox is not None:
            self.sandbox.kill()
            self.sandbox = None

    def __enter__(self) -> LizardExecutor:
        return self

    def __exit__(self, *args: Any) -> None:
        self.cleanup()
