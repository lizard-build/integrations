"""Run: inspect eval examples/eval.py --model <provider/model>."""

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate, use_tools
from inspect_ai.tool import bash

from inspect_lizard import LizardSandboxConfig


@task
def calculate():
    return Task(
        dataset=[Sample(input="Use Python in bash to calculate 6 * 7.", target="42")],
        solver=[use_tools(bash()), generate()],
        scorer=includes(),
        sandbox=("lizard", LizardSandboxConfig()),
    )
