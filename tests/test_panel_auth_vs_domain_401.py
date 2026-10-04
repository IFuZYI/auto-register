"""区分"面板登录态失效"和"业务接口拒绝"两种 401。

前端的 apiFetch 见到 401 会清掉 token 并跳登录页。如果业务接口也用 401 表达
"上游不认你提交的凭据"，用户在 iCloud 主号验证弹窗里点一下"验证并保存"就会被
踢出整个面板——线上真实出现过这个问题。

约定：只有面板自己的鉴权失败才带 X-Panel-Auth-Required 头，业务错误一律不用 401。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import PANEL_AUTH_HEADERS

PANEL_AUTH_HEADER = "X-Panel-Auth-Required"


@pytest.fixture
def app_with_password(monkeypatch):
    """装上真正的鉴权中间件，并设一个面板密码。"""
    import main as main_module
    from core.config_store import config_store

    monkeypatch.setitem(PANEL_AUTH_HEADERS, "X-Panel-Auth-Required", "1")
    original = config_store.get("auth_password_hash", "")
    config_store.set("auth_password_hash", "deadbeef")

    app = FastAPI()
    app.middleware("http")(main_module.auth_middleware)

    @app.get("/api/probe")
    def _probe():
        return {"ok": True}

    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        config_store.set("auth_password_hash", original)


def test_missing_token_is_marked_as_panel_auth(app_with_password):
    response = app_with_password.get("/api/probe")

    assert response.status_code == 401
    assert response.headers.get(PANEL_AUTH_HEADER) == "1"


def test_bad_token_is_marked_as_panel_auth(app_with_password):
    response = app_with_password.get(
        "/api/probe", headers={"Authorization": "Bearer not-a-real-token"}
    )

    assert response.status_code == 401
    assert response.headers.get(PANEL_AUTH_HEADER) == "1"


def test_icloud_never_uses_401_for_domain_errors():
    """业务错误码不能映射到 401，否则前端会把用户踢回登录页。"""
    from api.icloud import _ERROR_STATUS

    offenders = sorted(code for code, status in _ERROR_STATUS.items() if status == 401)

    assert offenders == [], (
        f"这些 iCloud 业务错误码映射到了 401：{offenders}。"
        "401 是面板登录态语义，业务失败请改用 400/403/409 等。"
    )


def test_apple_credential_rejection_is_not_a_panel_logout():
    """Apple 说密码错，应该是 400，而不是把人踢出面板的 401。"""
    from api.icloud import _ERROR_STATUS

    assert _ERROR_STATUS["invalid_credentials"] == 400
    assert _ERROR_STATUS["session_expired"] != 401
