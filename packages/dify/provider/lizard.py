from typing import Any

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

from runtime import LizardTools


class LizardProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        try:
            LizardTools(credentials).validate()
        except Exception:
            raise ToolProviderCredentialValidationError(
                "Could not access the Lizard project. Check the API key and project ID."
            ) from None
