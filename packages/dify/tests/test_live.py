import os

import pytest

from runtime import LizardTools

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("LIZARD_LIVE_TESTS") != "1", reason="Set LIZARD_LIVE_TESTS=1"),
]


def test_full_workflow():
    client = LizardTools(
        {"api_key": os.environ["LIZARD_API_KEY"], "project_id": os.environ["LIZARD_PROJECT_ID"]}
    )
    client.validate()
    created = client.create_sandbox({"lifetime_seconds": 300})
    params = {"sandbox_id": created["sandbox_id"]}
    try:
        client.write_file({**params, "path": "example.py", "content": "print(6 * 7)"})
        run = client.run_command({**params, "command": "python3 example.py"})
        assert run["exit_code"] == 0 and run["stdout"].strip() == "42"
        assert client.read_file({**params, "path": "example.py"})["content"] == "print(6 * 7)"
        nonzero = client.run_command({**params, "command": "printf err >&2; exit 7"})
        assert nonzero["exit_code"] == 7 and nonzero["stderr"] == "err"
        capped = client.run_command({**params, "command": "python3 -c \"print('x'*70000)\""})
        assert capped["truncated"] and capped["exit_code"] is None
    finally:
        assert client.destroy_sandbox(params)["destroyed"]
