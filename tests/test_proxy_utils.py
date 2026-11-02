from core.proxy_utils import (
    build_playwright_proxy_config,
    build_requests_proxy_config,
    normalize_proxy_url,
    redact_proxy_url,
)


def test_redact_proxy_url_hides_url_credentials():
    redacted = redact_proxy_url("http://user-name:secret-pass@proxy.example:2000")

    assert redacted == "http://***:***@proxy.example:2000"
    assert "user-name" not in redacted
    assert "secret-pass" not in redacted


def test_redact_proxy_url_hides_legacy_credentials():
    redacted = redact_proxy_url("proxy.example:2000:user-name:secret-pass")

    assert redacted == "proxy.example:2000:***:***"


def test_normalize_proxy_url_converts_legacy_credentials():
    normalized = normalize_proxy_url("proxy.example:2000:user-name:secret-pass")

    assert normalized == "http://user-name:secret-pass@proxy.example:2000"


def test_normalize_proxy_url_escapes_legacy_credentials():
    normalized = normalize_proxy_url("proxy.example:2000:user name:secret/pass")

    assert normalized == "http://user%20name:secret%2Fpass@proxy.example:2000"


def test_normalize_proxy_url_converts_socks5_to_remote_dns():
    normalized = normalize_proxy_url("socks5://user:secret@proxy.example:2000")

    assert normalized == "socks5h://user:secret@proxy.example:2000"


def test_build_requests_proxy_config_normalizes_legacy_credentials():
    config = build_requests_proxy_config("proxy.example:2000:user-name:secret-pass")

    assert config == {
        "http": "http://user-name:secret-pass@proxy.example:2000",
        "https": "http://user-name:secret-pass@proxy.example:2000",
    }


def test_build_playwright_proxy_config_normalizes_legacy_credentials():
    config = build_playwright_proxy_config("proxy.example:2000:user-name:secret-pass")

    assert config == {
        "server": "http://proxy.example:2000",
        "username": "user-name",
        "password": "secret-pass",
    }


def test_build_playwright_proxy_config_downgrades_authenticated_socks5():
    """带认证的 socks5 必须降级成 http。

    Chromium 不支持带认证的 SOCKS5（实测 `Browser does not support socks5
    proxy authentication` / `net::ERR_NO_SUPPORTED_PROXIES`），而多数住宅代理商
    同一端口也开 HTTP CONNECT。旧断言期望保留 `socks5://`，那正是线上
    `所有 Turnstile 方案均失败 → net::ERR_NO_SUPPORTED_PROXIES` 的根因。
    """
    config = build_playwright_proxy_config("socks5://user:secret@proxy.example:2000")

    assert config == {
        "server": "http://proxy.example:2000",
        "username": "user",
        "password": "secret",
    }


def test_grok_nav_failure_message_is_a_proxy_error():
    """Grok 导航失败必须被认成「出口问题」。

    2026-11-02 实测：某一轮两轮注册都栽在同一个出口的
    `打开注册页失败（打开超时）—— 出口不通或太慢`，但旧特征词表里没有这句，
    于是那个烂出口一直没被摘掉，下一轮还会被挑到。
    """
    from core.proxy_utils import looks_like_proxy_error

    assert looks_like_proxy_error(
        "打开注册页失败（打开超时）—— 出口不通或太慢，换一个出口 IP 重试，地址仍然可用"
    )


def test_playwright_network_errors_are_proxy_errors():
    from core.proxy_utils import looks_like_proxy_error

    for text in (
        "Page.goto: net::ERR_CONNECTION_CLOSED at https://accounts.x.ai/sign-up",
        "net::ERR_TUNNEL_CONNECTION_FAILED",
        "net::ERR_CONNECTION_TIMED_OUT",
    ):
        assert looks_like_proxy_error(text), text


def test_otp_wait_timeout_is_not_a_proxy_error():
    """反向护栏：等码超时不能命中 —— 误判会把好出口摘掉。"""
    from core.proxy_utils import looks_like_proxy_error

    for text in ("等验证码超时（240s）", "OTP timeout", "code wait timed out"):
        assert not looks_like_proxy_error(text), text
