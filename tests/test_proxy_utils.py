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
