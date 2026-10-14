"""One sandbox per ADK invocation, released when the invocation ends."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any, TypeVar

import anyio
from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools import FunctionTool, ToolContext
from lizard import CodeSandbox

T = TypeVar("T")


async def _blocking(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Finish an SDK call before allowing cleanup to race with it."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            await task
        raise


class LizardPlugin(BasePlugin):
    """Give an ADK invocation shell, code, and file tools in its own sandbox.

    Register both ``plugin.get_tools()`` on the agent and ``plugins=[plugin]``
    on the ADK App. Close the runner in a ``finally`` block. No sandbox or key
    is stored in ADK session state or returned in a tool response.
    """

    def __init__(
        self,
        *,
        project_id: str | None = None,
        api_key: str | None = None,
        api_url: str | None = None,
        size: str = "small",
        lifetime_ms: int = 900_000,
        command_timeout_ms: int = 60_000,
    ) -> None:
        super().__init__(name="lizard_sandboxes")
        self._project = project_id or os.environ.get("LIZARD_PROJECT_ID")
        if not self._project:
            raise ValueError("Set LIZARD_PROJECT_ID or pass project_id.")
        if size not in {"small", "medium", "large"}:
            raise ValueError("size must be small, medium, or large.")
        if lifetime_ms <= 0 or not 0 < command_timeout_ms <= 120_000:
            raise ValueError("Use a positive lifetime and a command timeout of 1–120000 ms.")
        self._options = dict(
            project_id=self._project,
            api_key=api_key,
            api_url=api_url,
            size=size,
            timeout_ms=lifetime_ms,
            metadata={"integration": "adk"},
        )
        self._command_timeout_ms = command_timeout_ms
        self._sandboxes: dict[str, CodeSandbox] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def get_tools(self) -> list[FunctionTool]:
        """Return the four tools to pass to an ADK Agent."""
        return [
            FunctionTool(self.lizard_execute_command),
            FunctionTool(self.lizard_execute_code),
            FunctionTool(self.lizard_read_file),
            FunctionTool(self.lizard_write_file),
        ]

    async def _sandbox(self, invocation_id: str) -> CodeSandbox:
        async with self._lock:
            if self._closed:
                raise RuntimeError("The Lizard plugin is closed.")
            if invocation_id not in self._sandboxes:
                task = asyncio.create_task(asyncio.to_thread(CodeSandbox.create, **self._options))
                try:
                    sandbox = await asyncio.shield(task)
                except asyncio.CancelledError:
                    with anyio.CancelScope(shield=True):
                        sandbox = await task
                        self._sandboxes[invocation_id] = sandbox
                        await _blocking(sandbox.kill)
                        del self._sandboxes[invocation_id]
                    raise
                self._sandboxes[invocation_id] = sandbox
            return self._sandboxes[invocation_id]

    async def lizard_execute_command(
        self, command: str, tool_context: ToolContext, cwd: str = "/home/user"
    ) -> dict[str, Any]:
        """Run a shell command in this invocation's sandbox.

        Args:
            command: Shell command to run in the sandbox.
            cwd: Absolute working directory inside the sandbox.
        """
        sandbox = await self._sandbox(tool_context.invocation_id)
        result = await _blocking(
            sandbox.process.exec_, command, workdir=cwd, timeout_ms=self._command_timeout_ms
        )
        return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code}

    async def lizard_execute_code(self, code: str, tool_context: ToolContext) -> dict[str, Any]:
        """Run Python code; variables persist within this invocation.

        Args:
            code: Python source code to run in the sandbox's Jupyter kernel.
        """
        sandbox = await self._sandbox(tool_context.invocation_id)
        result = await _blocking(sandbox.run_code, code, timeout_ms=self._command_timeout_ms)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "results": [{"mime": item.mime, "data": item.data} for item in result.results],
            "error": (
                {"name": result.error.name, "message": result.error.message}
                if result.error
                else None
            ),
        }

    async def lizard_read_file(self, path: str, tool_context: ToolContext) -> dict[str, Any]:
        """Read a UTF-8 file of at most 1 MiB from the sandbox.

        Args:
            path: Absolute file path inside the sandbox.
        """
        sandbox = await self._sandbox(tool_context.invocation_id)
        data = await _blocking(sandbox.fs.read_bytes, path)
        if len(data) > 1_048_576:
            raise ValueError("File exceeds 1 MiB; use a command to read a smaller part.")
        return {"content": data.decode("utf-8")}

    async def lizard_write_file(
        self, path: str, content: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """Write a UTF-8 file, creating its parent directories if needed.

        Args:
            path: Absolute file path inside the sandbox.
            content: Text to write. Replaces an existing file.
        """
        if len(content.encode("utf-8")) > 1_048_576:
            raise ValueError("Content exceeds the 1 MiB tool limit.")
        sandbox = await self._sandbox(tool_context.invocation_id)
        await _blocking(sandbox.fs.write, path, content)
        return {"path": path, "written": True}

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        """Release this invocation's sandbox after the runner finishes it."""
        async with self._lock:
            key = invocation_context.invocation_id
            if key in self._sandboxes:
                await _blocking(self._sandboxes[key].kill)
                del self._sandboxes[key]

    async def close(self) -> None:
        """Release remaining sandboxes, including those from failed invocations."""
        async with self._lock:
            self._closed = True
            failures = []
            for key, sandbox in list(self._sandboxes.items()):
                try:
                    await _blocking(sandbox.kill)
                    del self._sandboxes[key]
                except Exception as exc:
                    failures.append(exc)
            if failures:
                raise ExceptionGroup("Sandbox cleanup failed; close() can be retried", failures)
