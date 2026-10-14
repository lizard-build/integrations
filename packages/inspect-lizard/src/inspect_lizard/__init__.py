"""Inspect AI sandbox provider for Lizard Sandboxes."""

from inspect_ai.util import sandboxenv

from .environment import LizardSandboxConfig, LizardSandboxEnvironment

sandboxenv("lizard")(LizardSandboxEnvironment)

__all__ = ["LizardSandboxConfig", "LizardSandboxEnvironment"]
