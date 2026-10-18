import os

import pytest
import pytest_asyncio
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.workspaces import Workspace
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite
from pydantic_ai_harness.shell import Shell

from pydantic_ai_lizard import LizardSandboxBackend

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("LIZARD_LIVE_TESTS") != "1", reason="Set LIZARD_LIVE_TESTS=1"),
]


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


class TestLizardConformance(WorkspaceBackendSuite):
    @pytest_asyncio.fixture(scope="module", loop_scope="module")
    async def instances(self):
        created = []
        yield created
        for backend in created:
            await backend.destroy()

    @pytest.fixture(scope="module")
    def fresh_backend(self, instances):
        def create():
            backend = LizardSandboxBackend(lifetime_ms=3_600_000)
            instances.append(backend)
            return backend

        return create

    @pytest_asyncio.fixture(scope="module", loop_scope="module")
    async def backend(self, fresh_backend):
        return fresh_backend()

    @pytest.fixture
    def attach_backend(self):
        return lambda ref: LizardSandboxBackend(ref=ref)

    @pytest.fixture
    def destroy_environment(self):
        return lambda backend: backend.destroy()


async def test_agent_uses_workspace_and_can_reconnect():
    backend = LizardSandboxBackend(lifetime_ms=300_000)
    calls = 0

    def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert any(tool.name == "run_command" for tool in info.function_tools)
            return ModelResponse(
                [ToolCallPart("run_command", {"command": "printf 42 > agent.txt; cat agent.txt"})]
            )
        assert "42" in str(messages[-1])
        return ModelResponse([TextPart("Saved 42 to agent.txt")])

    try:
        agent = Agent(FunctionModel(model), capabilities=[Shell(tools=["run_command"])])
        result = await agent.run("Check the workspace", workspace=backend)
        assert result.output == "Saved 42 to agent.txt" and calls == 2
        workspace = Workspace(backend)
        assert await workspace.read_text("agent.txt") == "42"
        attached = LizardSandboxBackend(ref=backend.ref)
        assert (await attached.run(["cat", "agent.txt"])).stdout == "42"
    finally:
        await backend.destroy()
