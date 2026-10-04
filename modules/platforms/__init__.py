"""Platform-specific workflows and compatibility services."""

from .icloud import ICloudAliasResult, ICloudAliasWorkflow
from .registration import (
    PlatformRegistrationService,
    create_application_platform_registration_service,
)

__all__ = [
    "ICloudAliasResult",
    "ICloudAliasWorkflow",
    "PlatformRegistrationService",
    "create_application_platform_registration_service",
]
