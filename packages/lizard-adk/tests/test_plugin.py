import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from google.adk.agents import Agent
from lizard.code_interpreter.types import Execution, ExecutionError, ResultItem

from lizard_adk import LizardPlugin


@pytest.fixture
def native():
    sandbox = MagicMock()
    sandbox.process.exec_.return_value = SimpleNamespace(stdout="out", stderr="err", exit_code=3)
    sandbox.run_code.return_value = Execution(stdout="42\n", results=[ResultItem(data="42")])
    sandbox.fs.read_bytes.return_value = b"a\r\nb\x00"
    with patch("lizard_adk.plugin.CodeSandbox.create", return_value=sandbox) as create:
        yield sandbox, create


def ctx(value="run-1"):
    return SimpleNamespace(invocation_id=value)


def test_config_requires_project(monkeypatch):
    monkeypatch.delenv("LIZARD_PROJECT_ID", raising=False)
    with pytest.raises(ValueError, match="LIZARD_PROJECT_ID"):
        LizardPlugin()


@pytest.mark.parametrize(
    "options", [{"size": "huge"}, {"lifetime_ms": 0}, {"command_timeout_ms": 0}]
)
def test_invalid_config(options):
    with pytest.raises(ValueError):
        LizardPlugin(project_id="project", **options)


def test_adk_tool_schemas_hide_context():
    plugin = LizardPlugin(project_id="project")
    tools = plugin.get_tools()
    agent = Agent(name="test_agent", model="gemini-flash-latest", tools=tools)
    assert len(agent.tools) == 4
    for tool in tools:
        declaration = tool._get_declaration()
        schema = declaration.parameters_json_schema or declaration.parameters.model_dump()
        assert "tool_context" not in schema["properties"]
        assert schema["properties"]


async def test_tools_share_invocation_and_forward_results(native):
    sandbox, create = native
    plugin = LizardPlugin(project_id="project")
    result = await plugin.lizard_execute_command("false", ctx(), cwd="/tmp")
    assert result == {"stdout": "out", "stderr": "err", "exit_code": 3}
    sandbox.process.exec_.assert_called_once_with("false", workdir="/tmp", timeout_ms=60_000)
    result = await plugin.lizard_execute_code("21 * 2", ctx())
    assert result["results"] == [{"mime": "text/plain", "data": "42"}]
    assert create.call_count == 1
    await plugin.after_run_callback(invocation_context=ctx())
    sandbox.kill.assert_called_once()
    await plugin.close()
    assert sandbox.kill.call_count == 1


async def test_parallel_tools_create_once(native):
    _, create = native
    plugin = LizardPlugin(project_id="project")
    await asyncio.gather(*(plugin.lizard_execute_code("42", ctx()) for _ in range(4)))
    assert create.call_count == 1
    await plugin.close()


async def test_distinct_invocations_do_not_share_sandbox(native):
    _, create = native
    plugin = LizardPlugin(project_id="project")
    await plugin.lizard_execute_code("42", ctx("a"))
    await plugin.lizard_execute_code("42", ctx("b"))
    assert create.call_count == 2
    await plugin.close()


async def test_python_error_is_structured(native):
    native[0].run_code.return_value = Execution(error=ExecutionError("ValueError", "bad", "trace"))
    plugin = LizardPlugin(project_id="project")
    result = await plugin.lizard_execute_code("raise ValueError('bad')", ctx())
    assert result["error"] == {"name": "ValueError", "message": "bad"}
    await plugin.close()


async def test_files_preserve_text_and_reject_large_output(native):
    sandbox, _ = native
    plugin = LizardPlugin(project_id="project")
    assert (await plugin.lizard_read_file("/tmp/a", ctx()))["content"] == "a\r\nb\x00"
    await plugin.lizard_write_file("/tmp/a", "new", ctx())
    sandbox.fs.write.assert_called_with("/tmp/a", "new")
    sandbox.fs.read_bytes.return_value = b"x" * 1_048_577
    with pytest.raises(ValueError, match="1 MiB"):
        await plugin.lizard_read_file("/tmp/a", ctx())
    with pytest.raises(ValueError, match="1 MiB"):
        await plugin.lizard_write_file("/tmp/a", "x" * 1_048_577, ctx())
    await plugin.close()


async def test_failed_cleanup_can_retry(native):
    sandbox, _ = native
    plugin = LizardPlugin(project_id="project")
    await plugin.lizard_execute_code("42", ctx())
    sandbox.kill.side_effect = [RuntimeError("network"), None]
    with pytest.raises(ExceptionGroup):
        await plugin.close()
    await plugin.close()
    assert sandbox.kill.call_count == 2
    with pytest.raises(RuntimeError, match="closed"):
        await plugin.lizard_execute_code("42", ctx())


async def test_cancelled_creation_releases_resource(native):
    sandbox, create = native
    started, release = threading.Event(), threading.Event()

    def slow_create(**kwargs):
        started.set()
        release.wait(5)
        return sandbox

    create.side_effect = slow_create
    plugin = LizardPlugin(project_id="project")
    task = asyncio.create_task(plugin.lizard_execute_code("42", ctx()))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    sandbox.kill.assert_called_once()
    await plugin.close()


async def test_cancelled_sdk_call_preserves_cancellation_after_error():
    import threading

    from lizard_adk.plugin import _blocking

    started = threading.Event()
    release = threading.Event()

    def delayed_error():
        started.set()
        release.wait(3)
        raise RuntimeError("transport failed")

    task = asyncio.create_task(_blocking(delayed_error))
    await asyncio.to_thread(started.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
