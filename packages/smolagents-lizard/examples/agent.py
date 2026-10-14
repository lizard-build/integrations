"""Run with HF_TOKEN, LIZARD_API_KEY, and LIZARD_PROJECT_ID set."""

from smolagents import CodeAgent, InferenceClientModel

from smolagents_lizard import LizardExecutor

with LizardExecutor() as executor:
    agent = CodeAgent(tools=[], model=InferenceClientModel(), executor=executor)
    print(agent.run("Use Python to sum the integers from 1 to 100."))
