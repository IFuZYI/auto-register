from dataclasses import dataclass

from core.base_platform import RegisterConfig
from modules.automation import (
    AutomationHooks,
    RegistrationPipeline,
    RegistrationRequest,
)
from modules.config import RegistrationContextBuilder
from modules.execution import BrowserExecutorFactory
from modules.mail import MailboxFactory
from modules.platforms import ICloudAliasWorkflow, PlatformRegistrationService
from modules.proxy import ProxyPoolAdapter
from modules.verification import (
    VerificationChallenge,
    VerificationHandler,
)


class _Mailbox:
    pass


def test_mailbox_factory_creates_provider_with_merged_options():
    received = {}

    def build(*, extra, proxy):
        received["extra"] = extra
        received["proxy"] = proxy
        return _Mailbox()

    factory = MailboxFactory({"demo": build})

    mailbox = factory.create("demo", {"api_key": "task-key"}, "http://proxy:8080")

    assert isinstance(mailbox, _Mailbox)
    assert received == {
        "extra": {"api_key": "task-key"},
        "proxy": "http://proxy:8080",
    }


def test_registration_context_builder_merges_global_and_task_settings_without_mutating_inputs():
    global_settings = {"mail_provider": "luckmail", "shared": "global"}
    task_settings = {"shared": "task", "per_task": "value", "empty": ""}

    context = RegistrationContextBuilder(global_settings).build(
        RegisterConfig(proxy="http://proxy:8080", extra=task_settings)
    )

    assert context.settings == {
        "mail_provider": "luckmail",
        "shared": "task",
        "per_task": "value",
    }
    assert context.mail_provider == "luckmail"
    assert context.proxy == "http://proxy:8080"
    assert global_settings == {"mail_provider": "luckmail", "shared": "global"}
    assert task_settings == {"shared": "task", "per_task": "value", "empty": ""}


def test_browser_executor_factory_selects_protocol_and_browser_modes(monkeypatch):
    protocol_calls = []
    browser_calls = []

    class Protocol:
        def __init__(self, *, proxy):
            protocol_calls.append(proxy)

    class Browser:
        def __init__(self, *, proxy, headless):
            browser_calls.append((proxy, headless))

    factory = BrowserExecutorFactory(protocol_cls=Protocol, playwright_cls=Browser)

    factory.create("protocol", proxy="http://protocol")
    factory.create("headless", proxy="http://headless")
    factory.create("headed", proxy="http://headed")

    assert protocol_calls == ["http://protocol"]
    assert browser_calls == [("http://headless", True), ("http://headed", False)]


def test_icloud_alias_workflow_resolves_owner_checks_quota_and_generates_alias():
    events = []

    @dataclass
    class Owner:
        id: int
        email: str

    class Service:
        def resolve_account(self, email):
            events.append(("resolve", email))
            return Owner(id=7, email="owner@example.com")

        def alias_quota(self, account_id):
            events.append(("quota", account_id))
            return {"remaining": 4, "limit": 5}

        def generate_alias(self, account_id, *, label, note, proxy):
            events.append(("generate", account_id, label, note, proxy))
            return {"id": 11, "address": "alias@icloud.com"}

    result = ICloudAliasWorkflow(Service()).generate(
        owner_email="owner@example.com",
        label="registration",
        note="test",
        proxy="http://proxy:8080",
    )

    assert result.owner.id == 7
    assert result.quota == {"remaining": 4, "limit": 5}
    assert result.alias == {"id": 11, "address": "alias@icloud.com"}
    assert events == [
        ("resolve", "owner@example.com"),
        ("quota", 7),
        ("generate", 7, "registration", "test", "http://proxy:8080"),
    ]


def test_base_platform_delegates_executor_creation_to_reusable_factory(monkeypatch):
    from core.base_platform import BasePlatform

    received = {}

    class Platform(BasePlatform):
        def register(self, email=None, password=None):
            raise NotImplementedError

        def check_valid(self, account):
            return False

    class Factory:
        def create(self, executor_type, proxy):
            received["executor_type"] = executor_type
            received["proxy"] = proxy
            return "executor"

    # 工厂实现在 core（core/base_platform 不能反向依赖 modules）；
    # modules.execution 只是转出，所以按实现处打桩。
    monkeypatch.setattr("core.executors.factory.BrowserExecutorFactory", lambda: Factory())

    executor = Platform(RegisterConfig(executor_type="headless", proxy="http://proxy"))._make_executor()

    assert executor == "executor"
    assert received == {"executor_type": "headless", "proxy": "http://proxy"}


def test_registration_pipeline_coordinates_mailbox_browser_and_verification():
    events = []

    class Mailbox:
        def get_email(self):
            events.append("mailbox.get_email")
            return type("MailboxAccount", (), {"email": "person@example.com"})()

        def get_current_ids(self, account):
            events.append(("mailbox.get_current_ids", account.email))
            return {"old-mail"}

        def wait_for_code(self, account, **kwargs):
            events.append(("mailbox.wait_for_code", account.email, kwargs))
            return "123456"

    class BrowserFactory:
        def create(self, executor_type, proxy):
            events.append(("browser.create", executor_type, proxy))
            return "browser-session"

    class Verifier:
        def solve(self, challenge):
            events.append(("verification.solve", challenge.kind, challenge.site_key))
            return "captcha-token"

    def platform_flow(context):
        events.append(("platform", context.email, context.executor, context.otp_before_ids))
        assert context.solve_verification(
            VerificationChallenge(kind="turnstile", page_url="https://example.com", site_key="site-key")
        ) == "captcha-token"
        assert context.wait_for_email_code(timeout=45) == "123456"
        return {"account_id": "account-1"}

    result = RegistrationPipeline(
        mailbox_factory=lambda provider, extra, proxy: Mailbox(),
        browser_factory=BrowserFactory(),
        verification_handler=Verifier(),
    ).run(
        RegistrationRequest(
            mail_provider="demo",
            executor_type="headless",
            proxy="http://proxy:8080",
            settings={"mail_provider": "demo"},
        ),
        platform_flow,
    )

    assert result.email == "person@example.com"
    assert result.value == {"account_id": "account-1"}
    assert events == [
        ("browser.create", "headless", "http://proxy:8080"),
        "mailbox.get_email",
        ("mailbox.get_current_ids", "person@example.com"),
        ("platform", "person@example.com", "browser-session", {"old-mail"}),
        ("verification.solve", "turnstile", "site-key"),
        ("mailbox.wait_for_code", "person@example.com", {"timeout": 45, "before_ids": {"old-mail"}}),
    ]


def test_registration_pipeline_rejects_unsupported_verification_challenge():
    class Verifier:
        def solve(self, challenge):
            raise ValueError(f"unsupported: {challenge.kind}")

    pipeline = RegistrationPipeline(
        mailbox_factory=lambda provider, extra, proxy: _Mailbox(),
        browser_factory=object(),
        verification_handler=Verifier(),
    )

    try:
        pipeline.solve_verification(VerificationChallenge(kind="image"))
    except ValueError as exc:
        assert str(exc) == "unsupported: image"
    else:
        raise AssertionError("expected verification failure")


def test_verification_handler_routes_turnstile_and_image_challenges_to_captcha_provider():
    calls = []

    class Captcha:
        def solve_turnstile(self, page_url, site_key):
            calls.append(("turnstile", page_url, site_key))
            return "turnstile-token"

        def solve_image(self, image_b64):
            calls.append(("image", image_b64))
            return "image-answer"

    handler = VerificationHandler(Captcha())

    assert handler.solve(
        VerificationChallenge(kind="turnstile", page_url="https://example.com", site_key="key")
    ) == "turnstile-token"
    assert handler.solve(VerificationChallenge(kind="image", image_b64="encoded-image")) == "image-answer"
    assert calls == [
        ("turnstile", "https://example.com", "key"),
        ("image", "encoded-image"),
    ]


def test_registration_pipeline_emits_lifecycle_events_and_closes_executor():
    events = []

    class Executor:
        def close(self):
            events.append("executor.close")

    class BrowserFactory:
        def create(self, executor_type, proxy):
            events.append("executor.create")
            return Executor()

    class Mailbox:
        def get_email(self):
            events.append("mailbox.get_email")
            return type("MailboxAccount", (), {"email": "person@example.com"})()

    hooks = AutomationHooks(on_event=lambda event: events.append(event.name))
    pipeline = RegistrationPipeline(
        mailbox_factory=lambda provider, extra, proxy: Mailbox(),
        browser_factory=BrowserFactory(),
        hooks=hooks,
    )

    result = pipeline.run(
        RegistrationRequest(mail_provider="demo"),
        lambda context: {"email": context.email},
    )

    assert result.value == {"email": "person@example.com"}
    assert events == [
        "attempt.started",
        "executor.create",
        "mailbox.get_email",
        "attempt.succeeded",
        "executor.close",
        "attempt.finished",
    ]


def test_registration_pipeline_closes_executor_and_reports_failure():
    events = []

    class Executor:
        def close(self):
            events.append("executor.close")

    class Mailbox:
        def get_email(self):
            return type("MailboxAccount", (), {"email": "person@example.com"})()

    pipeline = RegistrationPipeline(
        mailbox_factory=lambda provider, extra, proxy: Mailbox(),
        browser_factory=type("Factory", (), {"create": lambda self, *_: Executor()})(),
        hooks=AutomationHooks(on_event=lambda event: events.append(event.name)),
    )

    try:
        pipeline.run(RegistrationRequest(mail_provider="demo"), lambda context: (_ for _ in ()).throw(RuntimeError("failed")))
    except RuntimeError as exc:
        assert str(exc) == "failed"
    else:
        raise AssertionError("expected platform flow failure")

    assert events == ["attempt.started", "attempt.failed", "executor.close", "attempt.finished"]


def test_proxy_pool_adapter_normalizes_and_reports_outcomes():
    events = []

    class Pool:
        def get_next(self, region=""):
            events.append(("get_next", region))
            return "socks5://user:pass@host:1080"

        def report_success(self, url):
            events.append(("success", url))

        def report_fail(self, url):
            events.append(("fail", url))

    adapter = ProxyPoolAdapter(Pool())

    acquired = adapter.acquire(region="sg")
    adapter.report_success(acquired)
    adapter.report_failure(acquired)

    assert acquired == "socks5h://user:pass@host:1080"
    assert events == [
        ("get_next", "sg"),
        ("success", "socks5h://user:pass@host:1080"),
        ("fail", "socks5h://user:pass@host:1080"),
    ]


def test_proxy_pool_adapter_returns_none_when_pool_is_empty():
    class EmptyPool:
        def get_next(self, region=""):
            return None

    adapter = ProxyPoolAdapter(EmptyPool())

    assert adapter.acquire() is None
    # Reporting on an empty acquisition must be a safe no-op.
    adapter.report_success(None)
    adapter.report_failure(None)


def test_registration_pipeline_uses_proxy_provider_and_reports_success():
    events = []

    class ProxyProvider:
        def acquire(self, region=""):
            events.append(("acquire", region))
            return "http://pool-proxy:8080"

        def report_success(self, proxy):
            events.append(("proxy_success", proxy))

        def report_failure(self, proxy):
            events.append(("proxy_failure", proxy))

    class BrowserFactory:
        def create(self, executor_type, proxy):
            events.append(("browser.create", proxy))
            return "executor"

    class Mailbox:
        def get_email(self):
            return type("MailboxAccount", (), {"email": "person@example.com"})()

    pipeline = RegistrationPipeline(
        mailbox_factory=lambda provider, extra, proxy: Mailbox(),
        browser_factory=BrowserFactory(),
        proxy_provider=ProxyProvider(),
    )

    result = pipeline.run(
        RegistrationRequest(mail_provider="demo"),
        lambda context: {"proxy": context.request.proxy},
    )

    assert result.value == {"proxy": "http://pool-proxy:8080"}
    assert events == [
        ("acquire", ""),
        ("browser.create", "http://pool-proxy:8080"),
        ("proxy_success", "http://pool-proxy:8080"),
    ]


def test_registration_pipeline_reports_proxy_failure_on_error():
    events = []

    class ProxyProvider:
        def acquire(self, region=""):
            return "http://pool-proxy:8080"

        def report_success(self, proxy):
            events.append(("proxy_success", proxy))

        def report_failure(self, proxy):
            events.append(("proxy_failure", proxy))

    class Mailbox:
        def get_email(self):
            return type("MailboxAccount", (), {"email": "person@example.com"})()

    pipeline = RegistrationPipeline(
        mailbox_factory=lambda provider, extra, proxy: Mailbox(),
        browser_factory=type("Factory", (), {"create": lambda self, *_: None})(),
        proxy_provider=ProxyProvider(),
    )

    try:
        pipeline.run(
            RegistrationRequest(mail_provider="demo"),
            lambda context: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected failure")

    assert events == [("proxy_failure", "http://pool-proxy:8080")]


def test_explicit_request_proxy_bypasses_proxy_provider():
    events = []

    class ProxyProvider:
        def acquire(self, region=""):
            events.append("acquire")
            return "http://pool-proxy:8080"

        def report_success(self, proxy):
            events.append(("proxy_success", proxy))

        def report_failure(self, proxy):
            events.append(("proxy_failure", proxy))

    class Mailbox:
        def get_email(self):
            return type("MailboxAccount", (), {"email": "person@example.com"})()

    pipeline = RegistrationPipeline(
        mailbox_factory=lambda provider, extra, proxy: Mailbox(),
        browser_factory=type("Factory", (), {"create": lambda self, *_: None})(),
        proxy_provider=ProxyProvider(),
    )

    result = pipeline.run(
        RegistrationRequest(mail_provider="demo", proxy="http://fixed:9000"),
        lambda context: {"proxy": context.request.proxy},
    )

    assert result.value == {"proxy": "http://fixed:9000"}
    assert events == []


def test_platform_registration_service_preserves_chatgpt_adapter_contract():
    calls = []

    class Adapter:
        def run(self, context):
            calls.append(("run", context.mailbox, context.proxy_url, context.email, context.password))
            return type("Result", (), {"success": True, "retryable": True})()

        def build_account(self, result, fallback_password):
            calls.append(("build_account", result, fallback_password))
            return "account"

    service = PlatformRegistrationService(
        adapter_builder=lambda settings: Adapter(),
        context_builder=lambda **kwargs: type("Context", (), kwargs)(),
        password_generator=lambda: "generated-password",
    )

    account = service.register_chatgpt(
        mailbox="mailbox",
        proxy="http://proxy:8080",
        email="fixed@example.com",
        password="",
        settings={"mode": "access_token_only"},
        mailbox_kind="demo",
        log_fn=lambda message: None,
    )

    assert account == "account"
    assert calls[0] == (
        "run",
        "mailbox",
        "http://proxy:8080",
        "fixed@example.com",
        "generated-password",
    )
    assert calls[1][0] == "build_account"
    assert calls[1][2] == "generated-password"


def test_mailbox_facade_reexports_every_public_name():
    """兼容门面必须重导出渠道包里的每个公开类（防止未来拆分/改名静默丢渠道）。

    `core.base_mailbox` 是历史导入路径，全仓几十处引用它；渠道实现搬到
    `core/mailboxes/channels/` 后，一旦有人漏改门面，旧路径会 ImportError。

    期望值不取自门面自身的 `__all__` —— 那样把名字从 `__all__` 删掉就检查
    不出来了（变异测试已验证）。这里直接扫描渠道模块里的公开类作为独立事实源。
    """
    import importlib
    import inspect
    import pkgutil

    import core.base_mailbox as facade
    import core.mailboxes.channels as channels_pkg

    # 渠道包里的每个公开类（不含下划线开头），必须能从门面取到。
    # 渠道既可能是单文件，也可能是拆包后的目录（如 outlook/），所以递归子包。
    expected = set()

    def _collect(module) -> None:
        for name, obj in vars(module).items():
            if name.startswith("_"):
                continue
            if inspect.isclass(obj) and obj.__module__ == module.__name__:
                expected.add(name)

    for mod_info in pkgutil.iter_modules(channels_pkg.__path__):
        mod = importlib.import_module(f"{channels_pkg.__name__}.{mod_info.name}")
        _collect(mod)
        if mod_info.ispkg:
            for sub_info in pkgutil.iter_modules(mod.__path__):
                sub = importlib.import_module(f"{mod.__name__}.{sub_info.name}")
                _collect(sub)

    assert expected, "渠道包扫描结果为空，测试本身失效了"
    missing = sorted(n for n in expected if not hasattr(facade, n))
    assert missing == [], f"门面缺少这些渠道类: {missing}"

    # 反向：__all__ 里声明的名字必须全部可解析
    unresolved = [name for name in facade.__all__ if not hasattr(facade, name)]
    assert unresolved == [], f"__all__ 声明但取不到: {unresolved}"

    # 再反向：渠道类必须都列进 __all__（否则 `import *` 会静默漏掉它们）
    not_exported = sorted(n for n in expected if n not in facade.__all__)
    assert not_exported == [], f"渠道类未列入门面 __all__: {not_exported}"


def test_every_channel_module_imports_and_registers():
    """每个渠道文件都必须能被导入，且导入后注册表里查得到对应 provider。

    渠道靠「文件末尾 @register_mailbox_provider」自注册，靠 `__init__.py`
    的导入副作用触发。少一次 import 的故障现场是「渠道文件存在但查不到」，
    报错还发生在注册任务的深处 —— 这条测试把它提前到导入期。
    """
    from core.mailboxes import available_providers
    from core.mailboxes.channels import __all__ as builtin_channel_modules

    registered = set(available_providers())
    # 内置渠道（core/mailboxes/channels/）—— 清单取自该包的 `__all__`，
    # 那是渠道的权威定义（在这里再抄一份的话，删/加渠道就要改两处）
    builtin = set(builtin_channel_modules)
    # 依赖 services/ 的渠道（modules/mail/，由注册表延迟加载触发）
    service_backed = {"icloud_local"}

    missing_builtin = sorted(builtin - registered)
    missing_service = sorted(service_backed - registered)
    assert missing_builtin == [], f"内置渠道未注册: {missing_builtin}"
    assert missing_service == [], f"services 侧渠道未注册: {missing_service}"


def test_unknown_mailbox_provider_raises_instead_of_falling_back():
    """未知 provider 必须抛 ValueError，不能静默返回某个默认渠道。

    旧工厂以 `else: # laoudo` 收尾：拼错渠道名会静默拿到 LaoudoMailbox，
    故障现场是「一直收不到验证码」，极难排查。这条测试守住新语义。
    """
    import pytest

    from core.base_mailbox import create_mailbox

    with pytest.raises(ValueError, match="未知邮箱提供商"):
        create_mailbox("no-such-provider-xyz")
