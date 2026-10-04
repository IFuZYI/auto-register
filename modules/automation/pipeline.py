"""Platform-neutral registration workflow orchestration.

The pipeline owns reusable setup concerns—executor creation, mailbox allocation,
OTP baselining, and verification delegation. A platform-specific callback owns
its protocol state machine and returns its native result.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from modules.verification import VerificationChallenge


@dataclass(frozen=True)
class AutomationEvent:
    """An observable lifecycle event emitted by an automation attempt."""

    name: str
    request: RegistrationRequest
    email: str = ""
    error: Exception | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AutomationHooks:
    """Optional framework hooks for logs, metrics, persistence, or tracing."""

    on_event: Callable[[AutomationEvent], None] | None = None


@dataclass(frozen=True)
class RegistrationRequest:
    """Provider-neutral inputs required to start one registration attempt."""

    mail_provider: str
    executor_type: str = "protocol"
    proxy: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    email: str = ""


@dataclass(frozen=True)
class RegistrationResult:
    """The platform callback result plus the identity used by the attempt."""

    email: str
    value: Any


@dataclass
class RegistrationFlowContext:
    """Capabilities a platform callback may use during one registration attempt."""

    request: RegistrationRequest
    mailbox: Any
    mailbox_account: Any
    executor: Any
    otp_before_ids: set[Any]
    verification_handler: Any | None = None

    @property
    def email(self) -> str:
        return str(self.request.email or getattr(self.mailbox_account, "email", "") or "").strip()

    def wait_for_email_code(self, **kwargs: Any) -> str:
        options = dict(kwargs)
        options.setdefault("before_ids", set(self.otp_before_ids))
        return self.mailbox.wait_for_code(self.mailbox_account, **options)

    def solve_verification(self, challenge: VerificationChallenge) -> str:
        if self.verification_handler is None:
            raise RuntimeError("注册流程未配置验证处理器")
        return self.verification_handler.solve(challenge)


class RegistrationPipeline:
    """Prepare reusable registration capabilities and invoke a platform callback."""

    def __init__(
        self,
        *,
        mailbox_factory: Callable[[str, dict[str, Any], str | None], Any],
        browser_factory: Any,
        verification_handler: Any | None = None,
        proxy_provider: Any | None = None,
        hooks: AutomationHooks | None = None,
    ):
        self._mailbox_factory = mailbox_factory
        self._browser_factory = browser_factory
        self._verification_handler = verification_handler
        self._proxy_provider = proxy_provider
        self._hooks = hooks or AutomationHooks()

    @classmethod
    def from_application_defaults(
        cls,
        *,
        verification_handler: Any | None = None,
        proxy_provider: Any | None = None,
        hooks: AutomationHooks | None = None,
    ) -> RegistrationPipeline:
        """Build a pipeline using this application's existing provider factories."""
        from modules.execution import BrowserExecutorFactory
        from modules.mail import create_mailbox
        from modules.proxy import create_application_proxy_pool_adapter

        return cls(
            mailbox_factory=lambda provider, settings, proxy: create_mailbox(
                provider,
                extra=settings,
                proxy=proxy,
            ),
            browser_factory=BrowserExecutorFactory(),
            verification_handler=verification_handler,
            proxy_provider=(
                proxy_provider
                if proxy_provider is not None
                else create_application_proxy_pool_adapter()
            ),
            hooks=hooks,
        )

    def run(
        self,
        request: RegistrationRequest,
        platform_flow: Callable[[RegistrationFlowContext], Any],
    ) -> RegistrationResult:
        executor = None
        email = ""
        request, proxy_from_pool = self._resolve_proxy(request)
        self._emit("attempt.started", request)
        try:
            executor = self._browser_factory.create(request.executor_type, request.proxy)
            mailbox = self._mailbox_factory(
                request.mail_provider,
                dict(request.settings),
                request.proxy,
            )
            mailbox_account = mailbox.get_email()
            before_ids = self._current_mail_ids(mailbox, mailbox_account)
            context = RegistrationFlowContext(
                request=request,
                mailbox=mailbox,
                mailbox_account=mailbox_account,
                executor=executor,
                otp_before_ids=before_ids,
                verification_handler=self._verification_handler,
            )
            value = platform_flow(context)
            email = context.email
            result = RegistrationResult(email=email, value=value)
            self._report_proxy_success(proxy_from_pool)
            self._emit("attempt.succeeded", request, email=email)
            return result
        except Exception as exc:
            self._report_proxy_failure(proxy_from_pool)
            self._emit("attempt.failed", request, email=email, error=exc)
            raise
        finally:
            self._close_executor(executor)
            self._emit("attempt.finished", request, email=email)

    def _resolve_proxy(
        self, request: RegistrationRequest
    ) -> tuple[RegistrationRequest, str | None]:
        """Fill an unset proxy from the pool; explicit request proxies win."""
        if request.proxy or self._proxy_provider is None:
            return request, None
        acquired = self._proxy_provider.acquire(request.settings.get("proxy_region", ""))
        if not acquired:
            return request, None
        return replace(request, proxy=acquired), acquired

    def _report_proxy_success(self, proxy: str | None) -> None:
        if proxy and self._proxy_provider is not None:
            self._proxy_provider.report_success(proxy)

    def _report_proxy_failure(self, proxy: str | None) -> None:
        if proxy and self._proxy_provider is not None:
            self._proxy_provider.report_failure(proxy)

    def solve_verification(self, challenge: VerificationChallenge) -> str:
        if self._verification_handler is None:
            raise RuntimeError("注册流程未配置验证处理器")
        return self._verification_handler.solve(challenge)

    def _emit(
        self,
        name: str,
        request: RegistrationRequest,
        *,
        email: str = "",
        error: Exception | None = None,
    ) -> None:
        if self._hooks.on_event is None:
            return
        self._hooks.on_event(AutomationEvent(name=name, request=request, email=email, error=error))

    @staticmethod
    def _close_executor(executor: Any) -> None:
        close = getattr(executor, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _current_mail_ids(mailbox: Any, account: Any) -> set[Any]:
        get_current_ids = getattr(mailbox, "get_current_ids", None)
        if not callable(get_current_ids):
            return set()
        try:
            return set(get_current_ids(account) or [])
        except (AttributeError, TypeError, ValueError):
            return set()
