import asyncio
import errno
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from inspect_ai.util import OutputLimitExceededError, SandboxEnvironmentLimits
from lizard import NotFoundError

from inspect_lizard import LizardSandboxConfig, LizardSandboxEnvironment


class LocalFiles:
    def write(self, path, contents):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(contents.encode() if isinstance(contents, str) else contents)

    def read_bytes(self, path):
        try:
            return Path(path).read_bytes()
        except FileNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc

    def read(self, path):
        return self.read_bytes(path).decode()


class LocalProcess:
    def exec_(self, cmd, **kwargs):
        if cmd[0] == "/usr/local/bin/python3":
            cmd = [sys.executable, *cmd[1:]]
        r = subprocess.run(cmd, capture_output=True)
        return SimpleNamespace(
            stdout=r.stdout.decode(), stderr=r.stderr.decode(), exit_code=r.returncode
        )

    def kill(self, pid):
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass


@pytest.fixture
def environment(tmp_path):
    native = SimpleNamespace(
        fs=LocalFiles(), process=LocalProcess(), sandbox_id="unit", kill=MagicMock()
    )
    env = LizardSandboxEnvironment(native)
    env._root = str(tmp_path / "remote")
    env._cwd = env._root + "/workspace"
    env._helper = env._root + "/worker.py"
    return env


async def test_argv_stdin_cwd_env_and_newlines(environment):
    result = await environment.exec(
        [
            sys.executable,
            "-c",
            "import os,sys; print(os.getenv('VALUE')); print(os.getcwd()); "
            "sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
        input=b"stdin\r\n",
        env={"VALUE": "quote' $()"},
    )
    assert result.success
    assert "stdin\r\n" in result.stdout
    assert "quote' $()" in result.stdout
    assert environment._cwd in result.stdout


async def test_nonzero_and_missing_command(environment):
    result = await environment.exec(
        [sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(7)"]
    )
    assert result.returncode == 7
    assert result.stderr == "bad"
    assert not result.success
    result = await environment.exec(["/does/not/exist"])
    assert result.returncode == 127


async def test_timeout(environment):
    with pytest.raises(TimeoutError):
        await environment.exec([sys.executable, "-c", "import time; time.sleep(20)"], timeout=1)
    assert (await environment.exec([sys.executable, "-c", "print('alive')"])).stdout == "alive\n"


async def test_output_tail_larger_than_sdk_limit(environment, monkeypatch):
    monkeypatch.setattr(SandboxEnvironmentLimits, "MAX_EXEC_OUTPUT_SIZE", 600_000)
    result = await environment.exec(
        [sys.executable, "-c", "import sys; sys.stdout.write('x'*700000+'END')"]
    )
    assert len(result.stdout) == 600_000
    assert result.stdout.endswith("END")


async def test_file_roundtrip_and_errors(environment, monkeypatch):
    content = b"a\r\nb\x00\xff"
    await environment.write_file("nested/a.bin", content)
    assert await environment.read_file("nested/a.bin", text=False) == content
    with pytest.raises(UnicodeDecodeError):
        await environment.read_file("nested/a.bin")
    with pytest.raises(FileNotFoundError):
        await environment.read_file("missing")
    with pytest.raises(IsADirectoryError):
        await environment.read_file("nested")
    with pytest.raises(IsADirectoryError):
        await environment.write_file("nested", "no")
    monkeypatch.setattr(SandboxEnvironmentLimits, "MAX_READ_FILE_SIZE", 2)
    with pytest.raises(OutputLimitExceededError):
        await environment.read_file("nested/a.bin", text=False)


async def test_permission_error_mapping(environment):
    with patch.object(
        environment, "_control", return_value={"errno": errno.EACCES, "message": "denied"}
    ):
        with pytest.raises(PermissionError):
            await environment.read_file("secret")


async def test_cancellation_stops_command(environment, tmp_path):
    marker = tmp_path / "must-not-exist"
    task = asyncio.create_task(
        environment.exec(
            [
                sys.executable,
                "-c",
                "import time; from pathlib import Path; time.sleep(2); "
                f"Path({str(marker)!r}).touch()",
            ]
        )
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2)
    assert not marker.exists()


def test_rejects_docker_configuration():
    assert not LizardSandboxEnvironment.is_docker_compatible()
    with pytest.raises(ValueError):
        LizardSandboxConfig(image="python:latest")


async def test_lifecycle_keeps_failed_cleanup_for_retry():
    native = MagicMock(sandbox_id="test-resource")
    native.kill.side_effect = [RuntimeError("network"), None]
    with patch("inspect_lizard.environment.Sandbox.create", return_value=native):
        environments = await LizardSandboxEnvironment.sample_init(
            "task", LizardSandboxConfig(project_id="project"), {}
        )
    with pytest.raises(ExceptionGroup):
        await LizardSandboxEnvironment.sample_cleanup("task", None, environments, False)
    await LizardSandboxEnvironment.task_cleanup("shutdown", None, True)
    assert native.kill.call_count == 2
    assert "test-resource" not in LizardSandboxEnvironment._active


async def test_exec_permission_error(environment, tmp_path):
    target = tmp_path / "not-executable"
    target.write_text("nothing")
    target.chmod(0o644)
    with pytest.raises(PermissionError):
        await environment.exec([str(target)])


async def test_anyio_cancelled_create_cleans_up():
    import time

    import anyio

    native = MagicMock(sandbox_id="cancelled-create")

    def create(*args, **kwargs):
        time.sleep(0.15)
        return native

    with patch("inspect_lizard.environment.Sandbox.create", side_effect=create):
        with anyio.move_on_after(0.02) as scope:
            await LizardSandboxEnvironment.sample_init(
                "sample-task", LizardSandboxConfig(project_id="test"), {}
            )
    assert scope.cancel_called
    native.kill.assert_called_once()
    assert native.sandbox_id not in LizardSandboxEnvironment._active


async def test_anyio_cancelled_create_retains_failed_cleanup():
    import time

    import anyio

    native = MagicMock(sandbox_id="cancelled-create-retry")
    native.kill.side_effect = [RuntimeError("network"), None]

    def create(*args, **kwargs):
        time.sleep(0.15)
        return native

    with patch("inspect_lizard.environment.Sandbox.create", side_effect=create):
        with anyio.move_on_after(0.02):
            await LizardSandboxEnvironment.sample_init(
                "sample-task", LizardSandboxConfig(project_id="test"), {}
            )
    assert native.sandbox_id in LizardSandboxEnvironment._active
    await LizardSandboxEnvironment.task_cleanup("shutdown", None, True)
    assert native.kill.call_count == 2


async def test_cancel_during_start_keeps_pid(environment, tmp_path):
    import threading
    import time

    import anyio

    started = threading.Event()
    original = environment.sandbox.process.exec_
    marker = tmp_path / "startup-must-not-exist"

    def delayed(cmd, **kwargs):
        result = original(cmd, **kwargs)
        if len(cmd) > 2 and cmd[2] == "start":
            started.set()
            time.sleep(0.3)
        return result

    environment.sandbox.process.exec_ = delayed

    async def run():
        with anyio.CancelScope() as scope:

            async def cancel():
                await asyncio.to_thread(started.wait, 3)
                scope.cancel()

            cancel_task = asyncio.create_task(cancel())
            try:
                await environment.exec(
                    [
                        sys.executable,
                        "-c",
                        "import time; from pathlib import Path; time.sleep(1); "
                        f"Path({str(marker)!r}).touch()",
                    ]
                )
            finally:
                with anyio.CancelScope(shield=True):
                    await cancel_task

    await run()
    await asyncio.sleep(1)
    assert not marker.exists()


async def test_dead_worker_has_host_deadline(environment, monkeypatch):
    import inspect_lizard.environment as module

    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    environment._deadline = 1800
    stopped = False

    async def control(action, value):
        nonlocal stopped
        if action == "start":
            return {"pid": 2147483647}
        if action == "stop":
            stopped = True
        return {}

    def missing_result(path):
        if stopped:
            return "{}"
        clock.now = 12
        raise NotFoundError(path)

    environment._control = control
    environment.sandbox.fs.read = missing_result
    with pytest.raises(TimeoutError, match="deadline"):
        await environment.exec(["never-started"], timeout=1)
    assert stopped


async def test_large_file_uses_chunks(environment):
    content = b"\xc3" * (17 * 1024 * 1024)
    await environment.write_file("large.bin", content)
    original = environment.sandbox.fs.read_bytes

    def limited(path):
        if Path(path).exists():
            assert Path(path).stat().st_size <= 8 * 1024 * 1024
        return original(path)

    environment.sandbox.fs.read_bytes = limited
    assert await environment.read_file("large.bin", text=False) == content


async def test_lifecycle_scopes_do_not_clean_each_other():
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    retained = MagicMock(sandbox_id="retained")
    current = MagicMock(sandbox_id="current")
    with sandbox_lifecycle_scope():
        with patch("inspect_lizard.environment.Sandbox.create", return_value=retained):
            first = await LizardSandboxEnvironment.sample_init(
                "first", LizardSandboxConfig(project_id="test"), {}
            )
        with sandbox_lifecycle_scope():
            with patch("inspect_lizard.environment.Sandbox.create", return_value=current):
                await LizardSandboxEnvironment.sample_init(
                    "second", LizardSandboxConfig(project_id="test"), {}
                )
            await LizardSandboxEnvironment.task_cleanup("shutdown", None, True)
        current.kill.assert_called_once()
        retained.kill.assert_not_called()
        await LizardSandboxEnvironment.task_cleanup("shutdown", None, False)
    await LizardSandboxEnvironment.task_cleanup("shutdown", None, True)
    retained.kill.assert_not_called()
    await first["default"].close()


async def test_large_command_output_uses_chunks(environment, monkeypatch):
    monkeypatch.setattr(SandboxEnvironmentLimits, "MAX_EXEC_OUTPUT_SIZE", 17 * 1024 * 1024)
    original = environment.sandbox.fs.read_bytes

    def limited(path):
        if Path(path).exists():
            assert Path(path).stat().st_size <= 8 * 1024 * 1024
        return original(path)

    environment.sandbox.fs.read_bytes = limited
    result = await environment.exec([sys.executable, "-c", "print('x' * (16 * 1024 * 1024))"])
    assert len(result.stdout) == 16 * 1024 * 1024 + 1


def test_config_works_in_inspect_cache_key():
    from inspect_ai import Task
    from inspect_ai.dataset import Sample

    task = Task(dataset=[Sample(input="example")], sandbox=("lizard", LizardSandboxConfig()))
    cache = {task.sandbox: "resolved"}
    same = Task(dataset=[Sample(input="another")], sandbox=("lizard", LizardSandboxConfig()))
    assert cache[same.sandbox] == "resolved"


async def test_cancelled_poll_preserves_cancellation_after_not_found(environment):
    import threading

    started = threading.Event()
    release = threading.Event()
    stopped = False
    reads = 0

    async def control(action, value):
        nonlocal stopped
        if action == "start":
            return {"pid": 2147483647}
        if action == "stop":
            stopped = True
        return {}

    def delayed_read(path):
        nonlocal reads
        reads += 1
        if reads == 1:
            started.set()
            release.wait(3)
            raise NotFoundError(path)
        return '{"returncode": 0, "stdout_size": 0, "stderr_size": 0}'

    environment._control = control
    environment.sandbox.fs.read = delayed_read
    task = asyncio.create_task(environment.exec(["scripted-command"]))
    await asyncio.to_thread(started.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped
