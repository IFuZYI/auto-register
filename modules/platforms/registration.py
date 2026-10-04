"""Adapters that preserve platform-specific registration contracts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class PlatformRegistrationService:
    """Invoke a platform registration adapter through an injectable contract."""

    def __init__(
        self,
        *,
        adapter_builder: Callable[[dict[str, Any]], Any],
        context_builder: Callable[..., Any],
        password_generator: Callable[[], str],
    ):
        self._adapter_builder = adapter_builder
        self._context_builder = context_builder
        self._password_generator = password_generator

    def register_chatgpt(
        self,
        *,
        mailbox: Any,
        proxy: str | None,
        email: str | None,
        password: str | None,
        settings: dict[str, Any] | None,
        mailbox_kind: str,
        log_fn: Callable[[str], None],
    ) -> Any:
        """Preserve the established ChatGPT adapter inputs and account conversion."""
        resolved_password = str(password or "") or self._password_generator()
        adapter = self._adapter_builder(dict(settings or {}))
        context = self._context_builder(
            mailbox=mailbox,
            proxy_url=proxy,
            callback_logger=log_fn,
            email=email,
            password=resolved_password,
            extra_config=dict(settings or {}),
            mailbox_kind=mailbox_kind,
        )
        result = adapter.run(context)
        if result is None or not result.success:
            message = getattr(result, "error_message", "") if result is not None else "注册失败"
            error = RuntimeError(message)
            error.retryable = bool(getattr(result, "retryable", True))
            raise error
        return adapter.build_account(result, resolved_password)


def create_application_platform_registration_service(
    *,
    adapter_builder: Callable[[dict[str, Any]], Any] | None = None,
    context_builder: Callable[..., Any] | None = None,
    password_generator: Callable[[], str] | None = None,
) -> PlatformRegistrationService:
    """Assemble the ChatGPT compatibility service from current project adapters."""
    if adapter_builder is None or context_builder is None or password_generator is None:
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            ChatGPTRegistrationContext,
            build_chatgpt_registration_mode_adapter,
        )
        from platforms.chatgpt.registration_engine import generate_password

        adapter_builder = adapter_builder or build_chatgpt_registration_mode_adapter
        context_builder = context_builder or ChatGPTRegistrationContext
        password_generator = password_generator or generate_password

    return PlatformRegistrationService(
        adapter_builder=adapter_builder,
        context_builder=context_builder,
        password_generator=password_generator,
    )
