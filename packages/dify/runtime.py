"""The plugin's API calls. Credentials stay in Dify's provider settings."""

from __future__ import annotations

import json
import math
import posixpath
from typing import Any

from lizard import Sandbox

API_URL = "https://lizard.build"
TEXT_LIMIT = 64 * 1024


def text(params: dict[str, Any], name: str, *, maximum: int = TEXT_LIMIT) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"{name} must be a nonempty string without NUL.")
    if len(value.encode()) > maximum:
        raise ValueError(f"{name} is too large.")
    return value


def integer(params: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be an integer from {low} to {high}.")
    if not math.isfinite(value) or int(value) != value or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}.")
    return int(value)


class LizardTools:
    def __init__(self, credentials: dict[str, Any]):
        self.api_key = text(credentials, "api_key", maximum=4096)
        self.project_id = text(credentials, "project_id", maximum=256)

    def validate(self) -> None:
        Sandbox.list(project_id=self.project_id, api_key=self.api_key, api_url=API_URL)

    def _sandbox(self, params: dict[str, Any]) -> Sandbox:
        sandbox_id = text(params, "sandbox_id", maximum=256)
        # Limit control to this plugin's sandboxes in the configured project.
        items = Sandbox.list(project_id=self.project_id, api_key=self.api_key, api_url=API_URL)
        if not any(item.sandbox_id == sandbox_id for item in items):
            raise ValueError("Sandbox not found among this plugin's sandboxes in the project.")
        sandbox = Sandbox.connect(sandbox_id, api_key=self.api_key, api_url=API_URL)
        # Project lists omit metadata; the single-sandbox response includes it.
        if (sandbox.get_info().metadata or {}).get("integration") != "dify":
            raise ValueError("Sandbox not found among this plugin's sandboxes in the project.")
        return sandbox

    def create_sandbox(self, params: dict[str, Any]) -> dict[str, Any]:
        lifetime = integer(params, "lifetime_seconds", 600, 60, 1800)
        size = params.get("size", "small")
        if size not in {"small", "medium", "large"}:
            raise ValueError("size must be small, medium, or large.")
        sandbox = Sandbox.create(
            "interpreter",
            project_id=self.project_id,
            api_key=self.api_key,
            api_url=API_URL,
            size=size,
            timeout_ms=lifetime * 1000,
            metadata={"integration": "dify"},
        )
        try:
            result = sandbox.process.exec_(["mkdir", "-p", "/workspace"], timeout_ms=30_000)
            if result.exit_code:
                raise RuntimeError("Could not prepare the working directory.")
        except BaseException:
            sandbox.kill()
            raise
        return {"sandbox_id": sandbox.sandbox_id, "lifetime_seconds": lifetime, "size": size}

    def run_command(self, params: dict[str, Any]) -> dict[str, Any]:
        command = text(params, "command")
        timeout = integer(params, "timeout_seconds", 30, 1, 120)
        sandbox = self._sandbox(params)
        pid = None
        captured = {"stdout": bytearray(), "stderr": bytearray()}

        class OutputLimit(Exception):
            pass

        def remember(value: int):
            nonlocal pid
            pid = value

        def capture(name: str, line: str):
            data = line.encode() + b"\n"
            remaining = TEXT_LIMIT - len(captured[name])
            captured[name].extend(data[:remaining])
            if len(data) > remaining:
                raise OutputLimit()

        try:
            result = sandbox.process.exec_(
                ["/bin/sh", "-c", command],
                workdir="/workspace",
                timeout_ms=timeout * 1000,
                on_pid=remember,
                on_stdout=lambda line: capture("stdout", line),
                on_stderr=lambda line: capture("stderr", line),
            )
        except OutputLimit:
            if pid is not None:
                try:
                    sandbox.process.kill(pid, "SIGKILL")
                except Exception:
                    pass  # A short command can exit before its final output arrives.
            return {
                "sandbox_id": sandbox.sandbox_id,
                "exit_code": None,
                "stdout": captured["stdout"].decode(errors="replace"),
                "stderr": captured["stderr"].decode(errors="replace"),
                "error": "Output limit reached; command termination requested.",
                "truncated": True,
            }
        except BaseException:
            if pid is not None:
                try:
                    sandbox.process.kill(pid, "SIGKILL")
                except Exception:
                    pass
            raise
        stdout, stderr = result.stdout.encode(), result.stderr.encode()
        return {
            "sandbox_id": sandbox.sandbox_id,
            "exit_code": result.exit_code,
            "stdout": stdout[:TEXT_LIMIT].decode(errors="replace"),
            "stderr": stderr[:TEXT_LIMIT].decode(errors="replace"),
            "truncated": result.truncated or len(stdout) > TEXT_LIMIT or len(stderr) > TEXT_LIMIT,
        }

    def write_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path = text(params, "path", maximum=4096)
        content = params.get("content")
        if not isinstance(content, str) or len(content.encode()) > TEXT_LIMIT:
            raise ValueError("content must be UTF-8 text of at most 64 KiB.")
        target = posixpath.join("/workspace", path)
        sandbox = self._sandbox(params)
        sandbox.fs.write(target, content)
        return {
            "sandbox_id": sandbox.sandbox_id,
            "path": target,
            "bytes_written": len(content.encode()),
        }

    def read_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path = posixpath.join("/workspace", text(params, "path", maximum=4096))
        sandbox = self._sandbox(params)
        # Reject FIFOs/devices and bound the read before data reaches Dify.
        script = (
            "import os,sys,stat,json; "
            "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NONBLOCK); "
            "assert stat.S_ISREG(os.fstat(fd).st_mode),'Expected a regular file'; "
            f"data=os.read(fd,{TEXT_LIMIT + 1}); os.close(fd); "
            f"print(json.dumps(dict(content=data[:{TEXT_LIMIT}].decode('utf-8',errors='replace'),"
            f"truncated=len(data)>{TEXT_LIMIT})))"
        )
        result = sandbox.process.exec_(["python3", "-c", script, path], timeout_ms=30_000)
        if result.exit_code or result.truncated:
            raise ValueError("Could not read the UTF-8 file. Check the path and file type.")
        return {"sandbox_id": sandbox.sandbox_id, "path": path, **json.loads(result.stdout)}

    def destroy_sandbox(self, params: dict[str, Any]) -> dict[str, Any]:
        sandbox = self._sandbox(params)
        return {"sandbox_id": sandbox.sandbox_id, "destroyed": sandbox.kill()}
