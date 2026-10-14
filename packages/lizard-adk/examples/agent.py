"""Run with GOOGLE_API_KEY, LIZARD_API_KEY, and LIZARD_PROJECT_ID set."""

import asyncio

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.genai import types

from lizard_adk import LizardPlugin


async def main() -> None:
    plugin = LizardPlugin()
    root_agent = Agent(
        model="gemini-flash-latest",
        name="sandbox_agent",
        instruction="Use the sandbox tools to run Python and check your answer.",
        tools=plugin.get_tools(),
    )
    runner = InMemoryRunner(
        app=App(name="sandbox_example", root_agent=root_agent, plugins=[plugin]),
        plugin_close_timeout=120,
    )
    try:
        session = await runner.session_service.create_session(
            app_name="sandbox_example", user_id="example"
        )
        async for event in runner.run_async(
            user_id="example",
            session_id=session.id,
            new_message=types.Content(
                role="user",
                parts=[types.Part.from_text(text="Use Python to sum the integers from 1 to 100.")],
            ),
        ):
            if event.content:
                for part in event.content.parts or []:
                    if part.text:
                        print(part.text)
    finally:
        await runner.close()


if __name__ == "__main__":
    asyncio.run(main())
