from unittest.mock import MagicMock, patch

import pytest
from lizard.code_interpreter.types import Execution, ExecutionError, ResultItem
from smolagents.utils import AgentError

from smolagents_lizard import LizardExecutor


@pytest.fixture
def native():
    sandbox = MagicMock()
    sandbox.run_code.return_value = Execution()
    with patch("smolagents_lizard.executor.CodeSandbox.create", return_value=sandbox):
        yield sandbox


def test_config_requires_project(monkeypatch):
    monkeypatch.delenv("LIZARD_PROJECT_ID", raising=False)
    with pytest.raises(ValueError, match="LIZARD_PROJECT_ID"):
        LizardExecutor()


def test_stdout_and_result(native):
    with LizardExecutor(project_id="project") as executor:
        native.run_code.return_value = Execution(stdout="hello\n", results=[ResultItem(data="42")])
        result = executor("40 + 2")
        assert result.output == "42"
        assert result.logs == "hello\n"
        assert not result.is_final_answer
    native.kill.assert_called_once()


def test_final_answer_safe_types(native):
    with LizardExecutor(project_id="project") as executor:
        native.run_code.return_value = Execution(
            error=ExecutionError("FinalAnswerException", 'safe:{"answer": 42}', "")
        )
        result = executor("final_answer({'answer': 42})")
        assert result.output == {"answer": 42}
        assert result.is_final_answer


def test_pickle_rejected(native):
    with LizardExecutor(project_id="project") as executor:
        native.run_code.return_value = Execution(
            error=ExecutionError("FinalAnswerException", "pickle:malicious", "")
        )
        with pytest.raises(Exception, match="Pickle data rejected"):
            executor("final_answer(1)")


def test_python_error(native):
    with LizardExecutor(project_id="project") as executor:
        native.run_code.return_value = Execution(
            stdout="before\n", error=ExecutionError("ValueError", "bad", "trace")
        )
        with pytest.raises(AgentError, match="ValueError"):
            executor("raise ValueError('bad')")


def test_install_arguments_are_not_shell_interpolated(native):
    with LizardExecutor(project_id="project") as executor:
        executor.install_packages(["emoji", "quote'package"])
        code = native.run_code.call_args.args[0]
        assert "subprocess.run([sys.executable" in code
        assert "check=True" in code
        assert "importlib.invalidate_caches()" in code
        compile(code, "install", "exec")


def test_constructor_failure_cleans_up(native):
    native.run_code.side_effect = RuntimeError("install failed")
    with pytest.raises(RuntimeError, match="install failed"):
        LizardExecutor(["emoji"], project_id="project")
    native.kill.assert_called_once()


def test_cleanup_is_retryable_and_idempotent(native):
    executor = LizardExecutor(project_id="project")
    native.kill.side_effect = [RuntimeError("network"), None]
    with pytest.raises(RuntimeError):
        executor.cleanup()
    executor.cleanup()
    executor.cleanup()
    assert native.kill.call_count == 2
    with pytest.raises(RuntimeError, match="closed"):
        executor("42")
