"""Inspect's sandbox contract backed by Lizard SDK."""

from __future__ import annotations

import asyncio
import base64
import errno
import json
import os
import posixpath
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, Literal, TypeVar, overload

import anyio
from inspect_ai.util import (
    ExecResult,
    OutputLimitExceededError,
    SandboxEnvironment,
    SandboxEnvironmentLimits,
    SandboxUserUnsupportedError,
)
from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_state
from lizard import NotFoundError, Sandbox
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


async def _blocking(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            await task
        raise


class LizardSandboxConfig(BaseModel):
    """Configuration for one isolated sandbox per evaluation sample."""

    model_config = ConfigDict(extra="forbid")
    project_id: str | None = None
    template: Literal["interpreter"] = "interpreter"
    size: Literal["small", "medium", "large"] = "small"
    lifetime_ms: int = Field(default=1_800_000, gt=0)


@dataclass
class _CleanupState:
    active: dict[str, LizardSandboxEnvironment] = field(default_factory=dict)


class LizardSandboxEnvironment(SandboxEnvironment):
    """Run sample commands and file operations in Lizard Sandboxes.

    This provider uses the interpreter template. It does not build Dockerfiles
    or Compose services. It never retries commands after a timeout.
    """

    _active: dict[str, LizardSandboxEnvironment] = {}

    def __init__(self, sandbox: Sandbox, lifetime_ms: int = 1_800_000) -> None:
        super().__init__()
        self.sandbox = sandbox
        self._registry_ref = self._registry()
        self._root = "/tmp/inspect-lizard-" + uuid.uuid4().hex
        self._cwd = self._root + "/workspace"
        self._helper = self._root + "/worker.py"
        self._lock = asyncio.Lock()
        self._ready = False
        self._closed = False
        self._deadline = time.monotonic() + lifetime_ms / 1000

    @classmethod
    def _registry(cls) -> dict[str, LizardSandboxEnvironment]:
        scope = sandbox_lifecycle_state()
        return scope.get(_CleanupState).active if scope is not None else cls._active

    async def _prepare(self) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("The sandbox is closed.")
            if not self._ready:
                source = files("inspect_lizard").joinpath("_worker.py").read_text()
                await _blocking(self.sandbox.fs.write, self._helper, source)
                result = await _blocking(
                    self.sandbox.process.exec_,
                    ["/bin/mkdir", "-p", self._cwd],
                )
                if result.exit_code != 0:
                    raise RuntimeError(result.stderr)
                self._ready = True

    def _path(self, path: str) -> str:
        return posixpath.normpath(posixpath.join(self._cwd, path))

    async def _control(self, action: str, value: str) -> dict[str, Any]:
        await self._prepare()
        result = await _blocking(
            self.sandbox.process.exec_,
            ["/usr/local/bin/python3", self._helper, action, value],
            timeout_ms=30_000,
        )
        if result.exit_code != 0:
            raise RuntimeError("Sandbox worker failed: " + result.stderr)
        return json.loads(result.stdout)

    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        """Execute argv without shell interpolation and retain each output's tail."""
        if not cmd or any(not isinstance(arg, str) for arg in cmd):
            raise ValueError("cmd must contain at least one string argument.")
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive.")
        await self._prepare()
        job = self._root + "/job-" + uuid.uuid4().hex
        data = input.encode() if isinstance(input, str) else input or b""
        request = dict(
            cmd=cmd,
            input=base64.b64encode(data).decode(),
            cwd=self._path(cwd or "."),
            env=env or {},
            user=user,
            timeout=timeout,
            output_limit=SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE,
        )
        await self._upload(job + "/request.json", json.dumps(request))
        pid = None
        completed = False
        deadline = (
            min(self._deadline, time.monotonic() + timeout + 10) if timeout else self._deadline
        )
        try:
            launch = asyncio.create_task(self._control("start", job))
            try:
                pid = (await asyncio.shield(launch))["pid"]
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    pid = (await launch)["pid"]
                raise
            while True:
                try:
                    raw = await _blocking(self.sandbox.fs.read, job + "/result.json")
                    result = json.loads(raw)
                    completed = True
                    break
                except NotFoundError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("No command result arrived before the deadline.")
                    await asyncio.sleep(0.1)
            if result.get("error") == "timeout":
                raise TimeoutError("Sandbox command exceeded its timeout.")
            if result.get("error") == "user":
                raise SandboxUserUnsupportedError(result["message"])
            if result.get("error") == "permission":
                raise PermissionError(errno.EACCES, result["message"], cmd[0])
            if result.get("error"):
                raise RuntimeError(result.get("message", result["error"]))
            limit = SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE
            stdout = await self._download(job + "/stdout", result["stdout_size"], limit)
            stderr = await self._download(job + "/stderr", result["stderr_size"], limit)
            return ExecResult(
                success=result["returncode"] == 0,
                returncode=result["returncode"],
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        except BaseException:
            if pid is not None and not completed:
                with anyio.CancelScope(shield=True):
                    try:
                        await self._control("stop", str(pid))
                        for _ in range(50):
                            try:
                                await _blocking(self.sandbox.fs.read, job + "/result.json")
                                break
                            except NotFoundError:
                                await asyncio.sleep(0.1)
                    except Exception:
                        # Preserve the execution error; sample cleanup kills the VM.
                        pass
            raise
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await _blocking(self.sandbox.process.exec_, ["/bin/rm", "-rf", job])
                except Exception:
                    pass  # Sample cleanup removes remaining job files.

    async def _stat(self, path: str, *, missing_ok: bool = False) -> dict[str, Any]:
        result = await self._control("stat", path)
        code = result.get("errno")
        if code == errno.ENOENT and missing_ok:
            return {}
        if code:
            raise OSError(code, result["message"], path)
        if result["directory"]:
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), path)
        return result

    async def write_file(self, file: str, contents: str | bytes) -> None:
        """Write bytes or UTF-8 text and create missing parent directories."""
        path = self._path(file)
        await self._stat(path, missing_ok=True)
        await self._upload(path, contents)

    @overload
    async def read_file(self, file: str, text: Literal[True] = True) -> str: ...

    @overload
    async def read_file(self, file: str, text: Literal[False]) -> bytes: ...

    async def read_file(self, file: str, text: bool = True) -> str | bytes:
        """Read a file without newline conversion; reject files above Inspect's limit."""
        path = self._path(file)
        stat = await self._stat(path)
        limit = SandboxEnvironmentLimits.MAX_READ_FILE_SIZE
        if stat["size"] > limit:
            raise OutputLimitExceededError(SandboxEnvironmentLimits.MAX_READ_FILE_SIZE_STR, None)
        data = await self._download(path, stat["size"], limit)
        if len(data) > limit:
            raise OutputLimitExceededError(SandboxEnvironmentLimits.MAX_READ_FILE_SIZE_STR, None)
        return data.decode("utf-8") if text else data

    async def _upload(self, path: str, contents: str | bytes) -> None:
        data = contents.encode("utf-8") if isinstance(contents, str) else contents
        chunk_size = 4 * 1024 * 1024
        if len(data) <= chunk_size:
            await _blocking(self.sandbox.fs.write, path, data)
            return
        temporary = self._root + "/upload-" + uuid.uuid4().hex
        try:
            for offset in range(0, len(data), chunk_size):
                await _blocking(
                    self.sandbox.fs.write, temporary, data[offset : offset + chunk_size]
                )
                request = json.dumps(dict(source=temporary, target=path, offset=offset))
                await self._control("write_chunk", request)
        finally:
            with anyio.CancelScope(shield=True):
                await _blocking(self.sandbox.process.exec_, ["/bin/rm", "-f", temporary])

    async def _download(self, path: str, size: int, limit: int) -> bytes:
        if size <= 8 * 1024 * 1024:
            data = await _blocking(self.sandbox.fs.read_bytes, path)
        else:
            # The file API caps one download at 15 MiB. Keep each transfer below it.
            chunk = self._root + "/read-" + uuid.uuid4().hex
            parts = []
            offset = 0
            try:
                while offset <= limit:
                    request = json.dumps(
                        dict(path=path, offset=offset, length=8 * 1024 * 1024, target=chunk)
                    )
                    await self._control("chunk", request)
                    part = await _blocking(self.sandbox.fs.read_bytes, chunk)
                    parts.append(part)
                    offset += len(part)
                    if len(part) < 8 * 1024 * 1024:
                        break
                data = b"".join(parts)
            finally:
                with anyio.CancelScope(shield=True):
                    await _blocking(self.sandbox.process.exec_, ["/bin/rm", "-f", chunk])
        return data

    @classmethod
    def config_deserialize(cls, config: dict[str, Any]) -> LizardSandboxConfig:
        return LizardSandboxConfig(**config)

    @classmethod
    def default_concurrency(cls) -> int:
        return 2

    @classmethod
    def is_docker_compatible(cls) -> bool:
        return False

    @classmethod
    async def sample_init(cls, task_name, config, metadata):
        if config is None:
            config = LizardSandboxConfig()
        if not isinstance(config, LizardSandboxConfig):
            raise ValueError("Use LizardSandboxConfig; Docker and Compose files are unsupported.")
        project = config.project_id or os.environ.get("LIZARD_PROJECT_ID")
        if not project:
            raise ValueError("Set LIZARD_PROJECT_ID or config.project_id.")
        task = asyncio.create_task(
            _blocking(
                Sandbox.create,
                config.template,
                project_id=project,
                size=config.size,
                timeout_ms=config.lifetime_ms,
                metadata={"integration": "inspect", "task": task_name},
            )
        )
        try:
            sandbox = await asyncio.shield(task)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                sandbox = await task
                instance = cls(sandbox, config.lifetime_ms)
                instance._registry_ref[sandbox.sandbox_id] = instance
                try:
                    await instance.close()
                except Exception:
                    pass  # Keep the handle for task_cleanup to retry.
            raise
        instance = cls(sandbox, config.lifetime_ms)
        instance._registry_ref[sandbox.sandbox_id] = instance
        return {"default": instance}

    async def close(self) -> None:
        with anyio.CancelScope(shield=True):
            if not self._closed:
                await _blocking(self.sandbox.kill)
                self._closed = True
                self._registry_ref.pop(self.sandbox.sandbox_id, None)

    @classmethod
    async def sample_cleanup(cls, task_name, config, environments, interrupted):
        errors = []
        for instance in environments.values():
            if isinstance(instance, cls):
                try:
                    await instance.close()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise ExceptionGroup("Sandbox cleanup failed", errors)

    @classmethod
    async def task_cleanup(cls, task_name, config, cleanup):
        if cleanup:
            remaining = dict(cls._registry())
            await cls.sample_cleanup(task_name, config, remaining, False)
        else:
            cls._registry().clear()  # Explicitly retained resources belong to the caller.

    @classmethod
    async def cli_cleanup(cls, id):
        if not id:
            raise ValueError("Pass the sandbox ID to clean up; bulk deletion is not supported.")
        sandbox = await _blocking(Sandbox.connect, id)
        info = await _blocking(sandbox.get_info)
        if not info.metadata or info.metadata.get("integration") != "inspect":
            raise ValueError("Refusing to delete a sandbox not created by this integration.")
        await _blocking(sandbox.kill)
