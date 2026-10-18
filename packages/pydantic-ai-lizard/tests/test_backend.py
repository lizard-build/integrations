import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from lizard import AuthenticationError, LimitExceededError, NotFoundError
from pydantic_ai.workspaces import (
    SupportsCommands,
    WorkspaceBackend,
    WorkspaceError,
    WorkspaceRef,
    WorkspaceUnavailableError,
)

from pydantic_ai_lizard import LizardSandbox, LizardSandboxBackend
from pydantic_ai_lizard import backend as module


@pytest.fixture
def sdk(monkeypatch):
    sandbox = MagicMock()
    sandbox.sandbox_id = "sandbox-123"
    sandbox.get_info.return_value = SimpleNamespace(status="running")
    sandbox.process.exec_.return_value = SimpleNamespace(
        stdout="/workspace\n", stderr="", exit_code=0, truncated=False
    )
    factory = MagicMock()
    factory.create.return_value = sandbox
    factory.connect.return_value = sandbox
    factory.return_value = sandbox
    monkeypatch.setattr(module, "Sandbox", factory)
    monkeypatch.setenv("LIZARD_PROJECT_ID", "test-project")
    monkeypatch.delenv("LIZARD_API_URL", raising=False)
    return factory, sandbox


async def test_lazy_concurrent_creation_and_stable_ref(sdk):
    factory, sandbox = sdk
    backend = LizardSandboxBackend()
    assert isinstance(backend, WorkspaceBackend)
    assert isinstance(backend, SupportsCommands)
    assert backend.ref is None
    factory.create.assert_not_called()
    assert await asyncio.gather(*(backend.working_dir() for _ in range(10))) == ["/workspace"] * 10
    factory.create.assert_called_once()
    assert backend.ref == WorkspaceRef(provider="lizard", id="sandbox-123")
    sandbox.kill.assert_not_called()
    await backend.destroy()
    await backend.destroy()
    sandbox.kill.assert_called_once()


async def test_failed_prepare_preserves_ref_and_can_retry(sdk):
    factory, sandbox = sdk
    sandbox.process.exec_.return_value.exit_code = 1
    backend = LizardSandboxBackend()
    with pytest.raises(WorkspaceError):
        await backend.working_dir()
    assert backend.ref.id == "sandbox-123"
    sandbox.process.exec_.return_value.exit_code = 0
    await backend.working_dir()
    factory.create.assert_called_once()
    await backend.destroy()
    sandbox.kill.assert_called_once()


async def test_cancellation_during_creation_retains_identity(sdk):
    factory, sandbox = sdk
    started, release = threading.Event(), threading.Event()

    def create(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return sandbox

    factory.create.side_effect = create
    backend = LizardSandboxBackend()
    task = asyncio.create_task(backend.working_dir())
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.ref == WorkspaceRef(provider="lizard", id="sandbox-123")
    await backend.working_dir()
    factory.create.assert_called_once()
    await backend.destroy()


async def test_unknown_ref_does_not_create_replacement(sdk):
    factory, _ = sdk
    factory.connect.side_effect = NotFoundError("gone")
    ref = WorkspaceRef(provider="lizard", id="missing")
    backend = LizardSandboxBackend(ref=ref)
    with pytest.raises(WorkspaceUnavailableError):
        await backend.working_dir()
    factory.create.assert_not_called()
    assert backend.ref == ref


async def test_auth_errors_hide_provider_text(sdk):
    factory, _ = sdk
    factory.create.side_effect = AuthenticationError("secret-token-rejected")
    with pytest.raises(WorkspaceUnavailableError, match="rejected the API key") as error:
        await LizardSandboxBackend().working_dir()
    assert "secret-token" not in str(error.value)


async def test_transient_create_error_can_retry(sdk):
    factory, sandbox = sdk
    factory.create.side_effect = [LimitExceededError("retry later"), sandbox]
    backend = LizardSandboxBackend()
    with pytest.raises(LimitExceededError):
        await backend.working_dir()
    assert backend.ref is None
    await backend.working_dir()
    assert backend.ref.id == sandbox.sandbox_id


@pytest.mark.parametrize(
    "command,shell,error",
    [
        ("true", False, TypeError),
        (["true"], True, TypeError),
        (b"true", False, TypeError),
        ([], False, ValueError),
        ([1], False, TypeError),
        (["x\0y"], False, ValueError),
    ],
)
async def test_invalid_commands_do_not_create(sdk, command, shell, error):
    with pytest.raises(error):
        await LizardSandboxBackend().run(command, shell=shell)
    sdk[0].create.assert_not_called()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), "10"])
async def test_invalid_timeout_does_not_create(sdk, timeout):
    with pytest.raises(ValueError):
        await LizardSandboxBackend().run(["true"], timeout=timeout)
    sdk[0].create.assert_not_called()


@pytest.mark.parametrize(
    "config",
    [
        {"working_dir": "relative"},
        {"working_dir": "/a\0b"},
        {"size": "xl"},
        {"lifetime_ms": 0},
        {"lifetime_ms": True},
        {"ref": WorkspaceRef(provider="other", id="123")},
    ],
)
def test_invalid_configuration(config):
    with pytest.raises(ValueError):
        LizardSandboxBackend(**config)


async def test_https_required_before_sending_key(sdk, monkeypatch):
    monkeypatch.setenv("LIZARD_API_URL", "http://example.com")
    with pytest.raises(ValueError, match="HTTPS"):
        await LizardSandboxBackend().working_dir()
    sdk[0].create.assert_not_called()


def test_capability_selects_only_its_own_refs():
    cap = LizardSandbox()
    assert cap.get_workspace(None, ref=WorkspaceRef(provider="e2b", id="x")) is None
    ref = WorkspaceRef(provider="lizard", id="x")
    assert cap.get_workspace(None, ref=ref).ref == ref
    with pytest.raises(Exception, match="cannot defer"):
        LizardSandbox(defer_loading=True)
