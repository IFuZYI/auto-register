"""Reusable registration building blocks.

This package contains provider-agnostic seams that new registration projects can
reuse without importing HTTP APIs, database models, or a specific platform.
"""

from .automation import (
    AutomationEvent,
    AutomationHooks,
    RegistrationFlowContext,
    RegistrationPipeline,
    RegistrationRequest,
    RegistrationResult,
)
from .config import RegistrationContext, RegistrationContextBuilder
from .execution import BrowserExecutorFactory
from .mail import MailboxFactory, create_mailbox
from .platforms import (
    ICloudAliasWorkflow,
    PlatformRegistrationService,
    create_application_platform_registration_service,
)
from .proxy import ProxyPoolAdapter, create_application_proxy_pool_adapter
from .verification import (
    VerificationChallenge,
    VerificationHandler,
)

__all__ = [
    "AutomationEvent",
    "AutomationHooks",
    "BrowserExecutorFactory",
    "ICloudAliasWorkflow",
    "MailboxFactory",
    "PlatformRegistrationService",
    "ProxyPoolAdapter",
    "RegistrationContext",
    "RegistrationContextBuilder",
    "RegistrationFlowContext",
    "RegistrationPipeline",
    "RegistrationRequest",
    "RegistrationResult",
    "VerificationChallenge",
    "VerificationHandler",
    "create_application_platform_registration_service",
    "create_application_proxy_pool_adapter",
    "create_mailbox",
]
