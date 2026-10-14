import os
from types import SimpleNamespace

import pytest
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from lizard_adk import LizardPlugin

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("LIZARD_LIVE_TESTS") != "1", reason="Set LIZARD_LIVE_TESTS=1"),
]


async def test_tools_live():
    plugin = LizardPlugin()
    context = SimpleNamespace(invocation_id="live-tools")
    try:
        assert (await plugin.lizard_execute_command("printf hello", context))["stdout"] == "hello"
        await plugin.lizard_write_file("/tmp/adk.txt", "a\r\nb", context)
        assert (await plugin.lizard_read_file("/tmp/adk.txt", context))["content"] == "a\r\nb"
        await plugin.lizard_execute_code("answer = 21", context)
        result = await plugin.lizard_execute_code("print(answer * 2)", context)
        assert result["stdout"] == "42\n"
        error = await plugin.lizard_execute_code("raise ValueError('expected')", context)
        assert error["error"]["name"] == "ValueError"
        await plugin.after_run_callback(invocation_context=context)
        assert not plugin._sandboxes
    finally:
        await plugin.close()


class ScriptedModel(BaseLlm):
    model: str = "scripted-test"
    calls: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        part = (
            types.Part.from_function_call(name="lizard_execute_code", args={"code": "print(6 * 7)"})
            if self.calls == 1
            else types.Part.from_text(text="done")
        )
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


async def test_real_runner_calls_tool_and_cleans_up():
    plugin = LizardPlugin()
    model = ScriptedModel()
    agent = Agent(name="sandbox_agent", model=model, tools=plugin.get_tools())
    runner = InMemoryRunner(
        app=App(name="lizard_test", root_agent=agent, plugins=[plugin]), plugin_close_timeout=120
    )
    try:
        session = await runner.session_service.create_session(
            app_name="lizard_test", user_id="test"
        )
        events = [
            event
            async for event in runner.run_async(
                user_id="test",
                session_id=session.id,
                new_message=types.Content(
                    role="user", parts=[types.Part.from_text(text="Run Python")]
                ),
            )
        ]
        responses = [
            part.function_response
            for event in events
            if event.content
            for part in event.content.parts or []
            if part.function_response
        ]
        assert any(response.response.get("stdout") == "42\n" for response in responses)
        assert not plugin._sandboxes
    finally:
        await runner.close()
