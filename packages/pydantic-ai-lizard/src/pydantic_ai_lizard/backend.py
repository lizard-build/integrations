"""Lazy workspace acquisition and complete command results."""

from __future__ import annotations

import asyncio
import json
import math
import os
import posixpath
import uuid
from collections.abc import Callable, Mapping
from importlib.resources import files
from typing import Any, Literal, TypeVar
from urllib.parse import urlsplit

import anyio
from lizard import AuthenticationError, LizardError, NotFoundError, Sandbox
from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    WorkspaceCommand,
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

T = TypeVar("T")
OUTPUT_LIMIT = 10 * 1024 * 1024


async def _blocking(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Keep a started SDK call observable when its asyncio caller is cancelled."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            try:
                await task
            except Exception:
                pass
        raise


def _argv(command: WorkspaceCommand, shell: bool) -> list[str]:
    if isinstance(command, str):
        if not shell:
            raise TypeError("A string command requires shell=True.")
        result = ["/bin/sh", "-c", command]
    else:
        if shell or isinstance(command, bytes):
            raise TypeError("Pass an argv sequence with shell=False.")
        result = list(command)
        if not result:
            raise ValueError("The command must not be empty.")
    if any(not isinstance(arg, str) for arg in result):
        raise TypeError("Command arguments must be strings.")
    if any("\0" in arg for arg in result):
        raise ValueError("Command arguments must not contain NUL.")
    return result


class LizardSandboxBackend:
    """An asyncio workspace backed by Lizard Sandboxes.

    Implements WorkspaceBackend, SupportsCommands, SupportsFilesystem, and
    SupportsRealpath. No commands run on the host.
    The caller owns cleanup; an agent run never destroys its workspace.
    """

    def __init__(
        self,
        *,
        ref: WorkspaceRef | None = None,
        project_id: str | None = None,
        api_key: str | None = None,
        size: Literal["small", "medium", "large"] = "small",
        lifetime_ms: int = 3_600_000,
        working_dir: str = "/workspace",
        env: Mapping[str, str] | None = None,
    ) -> None:
        if ref is not None and ref.provider != "lizard":
            raise ValueError("Expected a workspace reference with provider='lizard'.")
        if size not in {"small", "medium", "large"}:
            raise ValueError("size must be small, medium, or large.")
        if type(lifetime_ms) is not int or lifetime_ms <= 0:
            raise ValueError("lifetime_ms must be a positive integer.")
        if not posixpath.isabs(working_dir) or "\0" in working_dir:
            raise ValueError("working_dir must be an absolute POSIX path without NUL.")
        self._ref = ref
        self._project_id = project_id
        self._api_key = api_key
        self._size = size
        self._lifetime_ms = lifetime_ms
        self._cwd = working_dir
        self._env = dict(env or {})
        self._sandbox: Sandbox | None = None
        self._lock = asyncio.Lock()
        self._ready = False
        self._closed = False
        self._root = "/tmp/pydantic-ai-lizard-" + uuid.uuid4().hex
        self._helper = self._root + "/worker.py"

    @property
    def ref(self) -> WorkspaceRef | None:
        return self._ref

    def _acquire(self) -> Sandbox:
        api_url = os.environ.get("LIZARD_API_URL", "https://lizard.build")
        if urlsplit(api_url).scheme != "https":
            raise ValueError("LIZARD_API_URL must use HTTPS.")
        if self._ref is None:
            project = self._project_id or os.environ.get("LIZARD_PROJECT_ID")
            if not project:
                raise ValueError("Set LIZARD_PROJECT_ID or pass project_id.")
            sandbox = Sandbox.create(
                "interpreter",
                project_id=project,
                api_key=self._api_key,
                size=self._size,
                timeout_ms=self._lifetime_ms,
                metadata={"integration": "pydantic-ai"},
            )
            # Record identity in the thread before cancellation can discard its result.
            self._ref = WorkspaceRef(provider="lizard", id=sandbox.sandbox_id)
        else:
            sandbox = Sandbox.connect(self._ref.id, api_key=self._api_key)
        self._sandbox = sandbox
        return sandbox

    async def _sdk(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        try:
            return await _blocking(fn, *args, **kwargs)
        except NotFoundError as exc:
            raise WorkspaceUnavailableError("The sandbox no longer exists.") from exc
        except AuthenticationError:
            raise WorkspaceUnavailableError("Lizard rejected the API key.") from None
        except LizardError:
            if self._sandbox is not None:
                await self._check_alive()
            raise

    async def _check_alive(self) -> None:
        assert self._sandbox is not None
        try:
            info = await _blocking(self._sandbox.get_info)
        except NotFoundError as exc:
            raise WorkspaceUnavailableError("The sandbox no longer exists.") from exc
        if info.status not in (None, "running", "ready"):
            raise WorkspaceUnavailableError(f"The sandbox is not running ({info.status}).")

    async def _prepare(self) -> Sandbox:
        async with self._lock:
            if self._closed:
                raise WorkspaceUnavailableError("The workspace has been destroyed.")
            if self._sandbox is None:
                await self._sdk(self._acquire)
            assert self._sandbox is not None
            if not self._ready:
                await self._check_alive()
                await self._sdk(
                    self._sandbox.fs.write,
                    self._helper,
                    files("pydantic_ai_lizard").joinpath("_worker.py").read_text(),
                )
                result = await self._sdk(
                    self._sandbox.process.exec_,
                    [
                        "python3",
                        "-c",
                        (
                            "import os,sys; os.makedirs(sys.argv[1],exist_ok=True); "
                            "print(os.path.realpath(sys.argv[1]))"
                        ),
                        self._cwd,
                    ],
                    timeout_ms=30_000,
                )
                if result.exit_code or result.truncated:
                    raise WorkspaceError("Could not prepare the workspace directory.")
                self._cwd = result.stdout.strip()
                self._ready = True
            return self._sandbox

    async def working_dir(self) -> str:
        await self._prepare()
        return self._cwd

    async def _file(self, action: str, path: str, **options: Any) -> dict[str, Any]:
        if not posixpath.isabs(path) or "\0" in path:
            raise ValueError("Expected an absolute POSIX path without NUL.")
        await self._prepare()
        result = await self._control("fs", json.dumps({"action": action, "path": path, **options}))
        assert result is not None
        if result.get("errno"):
            raise OSError(result["errno"], result["message"], path)
        return result

    async def _read(self, path: str) -> bytes:
        assert self._sandbox is not None
        try:
            return await _blocking(self._sandbox.fs.read_bytes, path)
        except NotFoundError:
            await self._check_alive()
            raise FileNotFoundError(path) from None

    async def read_bytes(self, path: str) -> bytes:
        size = (await self._file("read_check", path))["size"]
        chunk_size = 8 * 1024 * 1024
        if size <= chunk_size:
            return await self._read(path)
        chunk = self._root + "/read-" + uuid.uuid4().hex
        parts: list[bytes] = []
        try:
            for offset in range(0, size, chunk_size):
                await self._file("read_chunk", path, offset=offset, length=chunk_size, target=chunk)
                parts.append(await self._read(chunk))
            return b"".join(parts)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self._file("remove", chunk)
                except (OSError, WorkspaceError, LizardError):
                    pass  # Temporary files also expire with the sandbox.

    async def write_bytes(self, path: str, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes.")
        sandbox = await self._prepare()
        chunk = self._root + "/write-" + uuid.uuid4().hex
        chunk_size = 4 * 1024 * 1024
        try:
            for offset in range(0, max(1, len(data)), chunk_size):
                await self._sdk(sandbox.fs.write, chunk, data[offset : offset + chunk_size])
                await self._file("write", path, offset=offset, source=chunk)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self._file("remove", chunk)
                except (OSError, WorkspaceError, LizardError):
                    pass

    async def stat(self, path: str) -> FileEntry:
        return FileEntry(**await self._file("stat", path))

    async def list_dir(self, path: str) -> list[FileEntry]:
        return [FileEntry(**entry) for entry in (await self._file("list", path))["entries"]]

    async def make_dir(self, path: str) -> None:
        await self._file("mkdir", path)

    async def remove(self, path: str) -> None:
        cwd = await self.working_dir()
        normalized = posixpath.normpath(path)
        if normalized == "/" or normalized == cwd or cwd.startswith(normalized + "/"):
            raise ValueError("Cannot remove the working directory or its ancestors.")
        await self._file("remove", path)

    async def exists(self, path: str) -> bool:
        return (await self._file("exists", path))["exists"]

    async def realpath(self, path: str) -> str:
        return (await self._file("realpath", path))["path"]

    async def _control(self, action: str, value: str) -> dict[str, Any] | None:
        assert self._sandbox is not None
        result = await self._sdk(
            self._sandbox.process.exec_,
            ["python3", self._helper, action, value],
            timeout_ms=30_000,
        )
        if result.exit_code or result.truncated:
            raise WorkspaceError("The sandbox command worker failed.")
        return json.loads(result.stdout)

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        argv = _argv(command, shell)
        if timeout is not None and (
            not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0
        ):
            raise ValueError("timeout must be positive and finite, or None.")
        combined_env = {**self._env, **(env or {})}
        if any(
            not isinstance(k, str)
            or not isinstance(v, str)
            or not k
            or "=" in k
            or "\0" in k
            or "\0" in v
            for k, v in combined_env.items()
        ):
            raise ValueError("Environment keys and values must be valid strings.")
        sandbox = await self._prepare()
        job = self._root + "/job-" + uuid.uuid4().hex
        pid: int | None = None
        completed = False
        try:
            await self._sdk(
                sandbox.fs.write,
                job + "/request.json",
                json.dumps(
                    {
                        "argv": argv,
                        "cwd": self._cwd,
                        "env": combined_env,
                        "timeout": timeout,
                        "output_limit": OUTPUT_LIMIT,
                    }
                ),
            )

            async def start() -> None:
                nonlocal pid
                result = await self._control("start", job)
                assert result is not None
                pid = result["pid"]

            launch = asyncio.create_task(start())
            try:
                await asyncio.shield(launch)
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    await launch
                raise
            while (result := await self._control("poll", job)) is None:
                await asyncio.sleep(0.1)
            completed = True
            stdout = (await self._sdk(sandbox.fs.read_bytes, job + "/stdout")).decode(
                "utf-8", errors="replace"
            )
            stderr = (await self._sdk(sandbox.fs.read_bytes, job + "/stderr")).decode(
                "utf-8", errors="replace"
            )
            if result.get("error") == "timeout":
                raise WorkspaceTimeoutError("Command timed out.", stdout=stdout, stderr=stderr)
            if result.get("error") == "output_limit":
                raise WorkspaceOutputLimitError(
                    "Command output exceeded 10 MiB per stream.",
                    limit=OUTPUT_LIMIT,
                    stdout=stdout,
                    stderr=stderr,
                )
            if result.get("error"):
                raise WorkspaceError("The sandbox command worker could not complete the command.")
            return CommandResult(exit_code=result["exit_code"], stdout=stdout, stderr=stderr)
        finally:
            with anyio.CancelScope(shield=True):
                if pid is not None and not completed:
                    try:
                        await self._control("stop", str(pid))
                        for _ in range(30):
                            if await self._control("poll", job) is not None:
                                completed = True
                                break
                            await asyncio.sleep(0.1)
                    except Exception:
                        pass  # Preserve the command error; the fixed lifetime bounds leftovers.
                if completed or pid is None:
                    try:
                        await self._sdk(sandbox.process.exec_, ["rm", "-rf", "--", job])
                    except Exception:
                        pass

    async def destroy(self) -> None:
        """Explicitly destroy this workspace, including one whose preparation failed."""
        async with self._lock:
            if self._ref is not None and not self._closed:
                sandbox = self._sandbox or Sandbox(self._ref.id, api_key=self._api_key)
                await _blocking(sandbox.kill)
            self._closed = True
