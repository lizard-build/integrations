from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from dify_plugin.entities.tool import ToolRuntime

import runtime
from runtime import TEXT_LIMIT, LizardTools
from tools.run_command import RunCommandTool


@pytest.fixture
def sdk(monkeypatch):
    sandbox = MagicMock()
    sandbox.sandbox_id = "sandbox-123"
    sandbox.process.exec_.return_value = SimpleNamespace(
        stdout="42", stderr="err", exit_code=7, truncated=False
    )
    sandbox.kill.return_value = True
    sandbox.get_info.return_value = SimpleNamespace(metadata={"integration": "dify"})
    factory = MagicMock()
    factory.create.return_value = sandbox
    factory.connect.return_value = sandbox
    factory.list.return_value = [SimpleNamespace(sandbox_id="sandbox-123", metadata=None)]
    monkeypatch.setattr(runtime, "Sandbox", factory)
    return factory, sandbox


@pytest.fixture
def client():
    return LizardTools({"api_key": "host-secret", "project_id": "project-123"})


def test_validation_creates_no_resources(client, sdk):
    client.validate()
    sdk[0].list.assert_called_once_with(
        project_id="project-123", api_key="host-secret", api_url="https://lizard.build"
    )
    sdk[0].create.assert_not_called()


@pytest.mark.parametrize("metadata", [{}, {"integration": "inspect"}, None])
def test_cannot_control_unrelated_sandbox(client, sdk, metadata):
    sdk[1].get_info.return_value.metadata = metadata
    with pytest.raises(ValueError, match="not found"):
        client.destroy_sandbox({"sandbox_id": "sandbox-123"})
    sdk[1].kill.assert_not_called()


def test_cannot_control_another_project(client, sdk):
    sdk[0].list.return_value = []
    with pytest.raises(ValueError, match="not found"):
        client.destroy_sandbox({"sandbox_id": "sandbox-123"})
    sdk[0].connect.assert_not_called()


def test_create_has_fixed_lifetime_and_cleans_failed_setup(client, sdk):
    with pytest.raises(RuntimeError):
        client.create_sandbox({})
    sdk[1].kill.assert_called_once()
    assert sdk[0].create.call_args.kwargs["timeout_ms"] == 600000
    assert "envs" not in sdk[0].create.call_args.kwargs


@pytest.mark.parametrize("value", [0, 1801, True, "600", 1.5, float("nan")])
def test_invalid_lifetimes_do_not_create(client, sdk, value):
    with pytest.raises(ValueError):
        client.create_sandbox({"lifetime_seconds": value})
    sdk[0].create.assert_not_called()


def test_command_and_returned_nonzero_exit(client, sdk):
    result = client.run_command({"sandbox_id": "sandbox-123", "command": "printf 42; exit 7"})
    assert result["exit_code"] == 7
    assert result["stdout"] == "42"
    assert "host-secret" not in str(sdk[1].process.exec_.call_args)


@pytest.mark.parametrize("already_exited", [False, True])
def test_output_limit_stops_process(client, sdk, already_exited):
    def execute(*args, **kwargs):
        kwargs["on_pid"](123)
        kwargs["on_stdout"]("x" * (TEXT_LIMIT + 1))

    sdk[1].process.exec_.side_effect = execute
    if already_exited:
        sdk[1].process.kill.side_effect = RuntimeError("no such process")
    result = client.run_command({"sandbox_id": "sandbox-123", "command": "large output"})
    assert result["truncated"] is True and result["exit_code"] is None
    assert len(result["stdout"]) == TEXT_LIMIT
    sdk[1].process.kill.assert_called_once_with(123, "SIGKILL")


def test_timeout_stops_process(client, sdk):
    def execute(*args, **kwargs):
        kwargs["on_pid"](123)
        raise TimeoutError("timeout")

    sdk[1].process.exec_.side_effect = execute
    with pytest.raises(TimeoutError):
        client.run_command({"sandbox_id": "sandbox-123", "command": "sleep 99"})
    sdk[1].process.kill.assert_called_once_with(123, "SIGKILL")


def test_write_empty_file_and_literal_path(client, sdk):
    result = client.write_file(
        {"sandbox_id": "sandbox-123", "path": "$(touch bad).txt", "content": ""}
    )
    assert result["bytes_written"] == 0
    sdk[1].fs.write.assert_called_once_with("/workspace/$(touch bad).txt", "")
    sdk[1].process.exec_.assert_not_called()


def test_tool_uses_dify_runtime_and_hides_raw_errors(sdk):
    tool = RunCommandTool(
        ToolRuntime(
            credentials={"api_key": "host-secret", "project_id": "project-123"},
            user_id="user",
            session_id="session",
        ),
        MagicMock(),
    )
    messages = list(tool.invoke({"sandbox_id": "sandbox-123", "command": "printf 42"}))
    assert messages and messages[0].message.json_object["stdout"] == "42"
    sdk[0].list.side_effect = RuntimeError("header host-secret")
    with pytest.raises(Exception) as error:
        list(tool.invoke({"sandbox_id": "sandbox-123", "command": "true"}))
    assert "host-secret" not in str(error.value)


def test_credentials_are_not_model_inputs():
    with open("provider/lizard.yaml") as f:
        provider = yaml.safe_load(f)
    assert provider["credentials_for_provider"]["api_key"]["type"] == "secret-input"
    for filename in provider["tools"]:
        with open(filename) as f:
            tool = yaml.safe_load(f)
        assert not {"api_key", "project_id"} & {p["name"] for p in tool["parameters"]}
