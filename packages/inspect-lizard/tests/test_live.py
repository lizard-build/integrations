import asyncio
import os

import pytest
import pytest_asyncio
from inspect_ai.util._sandbox import self_check

from inspect_lizard import LizardSandboxConfig, LizardSandboxEnvironment

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("LIZARD_LIVE_TESTS") != "1", reason="Set LIZARD_LIVE_TESTS=1"),
]

# Collect the checks shipped with the supported Inspect release.
for _name, _test in vars(self_check).items():
    if _name.startswith("test_") and callable(_test):
        globals()[_name] = _test


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_environment():
    environments = await LizardSandboxEnvironment.sample_init(
        "conformance", LizardSandboxConfig(), {}
    )
    try:
        yield environments["default"]
    finally:
        await LizardSandboxEnvironment.sample_cleanup("conformance", None, environments, False)


@pytest.fixture
async def sandbox_env(live_environment, request):
    if request.node.originalname in {
        "test_read_file_not_allowed",
        "test_write_text_file_without_permissions",
        "test_write_binary_file_without_permissions",
    }:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True, reason="The interpreter template and SDK file API run as root."
            )
        )
    return live_environment


async def test_cancellation_and_large_output_live(live_environment):
    env = live_environment
    output = await env.exec(["python3", "-c", "print('x' * 700000)"])
    assert len(output.stdout) == 700001
    task = asyncio.create_task(env.exec(["sh", "-c", "sleep 5; touch cancelled-marker"]))
    await asyncio.sleep(1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(5)
    result = await env.exec(["test", "!", "-e", "cancelled-marker"])
    assert result.success


async def test_inspect_eval_uses_registered_provider(tmp_path):
    from inspect_ai import Task, eval_async
    from inspect_ai.dataset import Sample
    from inspect_ai.scorer import includes
    from inspect_ai.solver import solver
    from inspect_ai.util import sandbox

    @solver
    def compute():
        async def solve(state, generate):
            result = await sandbox().exec(["python3", "-c", "print(6 * 7)"])
            assert result.success
            state.output.completion = result.stdout.strip()
            return state

        return solve

    task = Task(
        dataset=[Sample(input="Compute six times seven", target="42")],
        solver=compute(),
        scorer=includes(),
        sandbox=("lizard", LizardSandboxConfig(lifetime_ms=300000)),
    )
    logs = await eval_async(task, model="mockllm/model", log_dir=str(tmp_path))
    assert logs[0].status == "success"
    assert logs[0].samples[0].output.completion == "42"
