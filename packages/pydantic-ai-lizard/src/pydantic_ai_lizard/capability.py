"""Workspace selection for Pydantic AI agents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai.workspaces import WorkspaceRef

from .backend import LizardSandboxBackend


@dataclass(kw_only=True)
class LizardSandbox(AbstractCapability[AgentDepsT]):
    """Supply Lizard Sandboxes to workspace tools such as Shell and FileSystem."""

    project_id: str | None = None
    size: Literal["small", "medium", "large"] = "small"
    lifetime_ms: int = 3_600_000
    working_dir: str = "/workspace"
    env: Mapping[str, str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.defer_loading:
            raise UserError("LizardSandbox cannot defer loading: it selects the run's workspace.")
        self._backend(None)  # Validate configuration without making an API call.

    def _backend(self, ref: WorkspaceRef | None) -> LizardSandboxBackend:
        return LizardSandboxBackend(
            ref=ref,
            project_id=self.project_id,
            size=self.size,
            lifetime_ms=self.lifetime_ms,
            working_dir=self.working_dir,
            env=self.env,
        )

    def backend(self, ref: WorkspaceRef) -> LizardSandboxBackend:
        """Attach lazily to an existing workspace."""
        return self._backend(ref)

    async def destroy(self, ref: WorkspaceRef) -> None:
        """Destroy the named sandbox without creating a replacement."""
        await self.backend(ref).destroy()

    def get_workspace(
        self, ctx: RunContext[AgentDepsT], *, ref: WorkspaceRef | None
    ) -> LizardSandboxBackend | None:
        if ref is not None and ref.provider != "lizard":
            return None
        return self._backend(ref)
