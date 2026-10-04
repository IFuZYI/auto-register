"""Registration attempt lifecycle orchestration."""

from .pipeline import (
    AutomationEvent,
    AutomationHooks,
    RegistrationFlowContext,
    RegistrationPipeline,
    RegistrationRequest,
    RegistrationResult,
)

__all__ = [
    "AutomationEvent",
    "AutomationHooks",
    "RegistrationFlowContext",
    "RegistrationPipeline",
    "RegistrationRequest",
    "RegistrationResult",
]
