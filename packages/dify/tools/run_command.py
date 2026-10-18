from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from runtime import LizardTools


class RunCommandTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        try:
            result = LizardTools(self.runtime.credentials).run_command(tool_parameters)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
        except Exception:
            raise RuntimeError(
                "Lizard could not complete the operation. "
                "Check the sandbox state, account limits, and credentials."
            ) from None
        yield self.create_json_message(result)
