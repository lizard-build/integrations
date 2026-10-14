import os

import pytest
from smolagents import ChatMessage, CodeAgent, Model
from smolagents.default_tools import FinalAnswerTool

from smolagents_lizard import LizardExecutor

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("LIZARD_LIVE_TESTS") != "1", reason="Set LIZARD_LIVE_TESTS=1"),
]


def test_persistent_kernel_install_and_safe_final_answer():
    with LizardExecutor() as executor:
        executor("x = 21")
        assert executor("print(x * 2)").logs == "42\n"
        executor.install_packages(["emoji==2.15.0"])
        assert "🐍" in executor("import emoji; print(emoji.emojize(':snake:'))").logs
        executor.send_variables({"items": [20, 22]})
        executor.send_tools({"final_answer": FinalAnswerTool()})
        result = executor("final_answer({'sum': sum(items)})")
        assert result.is_final_answer and result.output == {"sum": 42}


class ScriptedModel(Model):
    def generate(self, messages, **kwargs):
        return ChatMessage(role="assistant", content="<code>final_answer(6 * 7)</code>")


def test_code_agent_uses_remote_executor():
    with LizardExecutor() as executor:
        agent = CodeAgent(tools=[], model=ScriptedModel(), executor=executor, max_steps=2)
        assert agent.run("Return six times seven") == 42
