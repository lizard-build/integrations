"""Run with LIZARD_API_KEY, LIZARD_PROJECT_ID, and OPENAI_API_KEY on the host."""

import asyncio

from pydantic_ai import Agent
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.shell import Shell

from pydantic_ai_lizard import LizardSandboxBackend


async def main():
    workspace = LizardSandboxBackend(lifetime_ms=600_000)
    agent = Agent("openai:gpt-5", capabilities=[Shell(), FileSystem()])
    try:
        result = await agent.run(
            "Use Python to compute the first ten Fibonacci numbers and save them to fib.txt.",
            workspace=workspace,
        )
        print(result.output)
        print(workspace.ref)
    finally:
        await workspace.destroy()


if __name__ == "__main__":
    asyncio.run(main())
