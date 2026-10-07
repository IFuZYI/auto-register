"""代理与鉴权 API 的集成/单元测试。

为什么专门测这两块：

- ``/api/proxies`` 是注册链路的基础设施（任务全靠它取出口）。批量删除的边界
  （空列表 / 超 1000 条 / 部分 ID 不存在 / 重复 ID）此前没有任何断言 —— 这些
  分支一旦出错，界面看到的是「点了删除没反应」或整页 500，而不是一条错误信息。
- ``api.auth`` 是纯 stdlib 实现的 JWT + TOTP（没有 PyJWT / pyotp 兜底），签名
  校验、过期判断、漂移窗口全得自己钉住。TOTP 用 RFC 6238 的**已知向量**验证，
  防止「自算自验」式的假绿：自己生成码再用自己的实现去验，两边一起写错也测不出来。

测试手法（沿用仓库约定）：

- TestClient 必须进上下文（``with TestClient(app)``）—— FastAPI 的 lifespan 只在
  那里执行，平台插件/中间件才是注册好的状态；
- 鉴权相关的全局状态（``auth_password_hash`` / ``auth_totp_secret`` /
  ``api.auth._pending_2fa``）在 fixture 里快照-恢复：中间件读的是 config_store
  单例，不恢复的话同进程的其它测试会被带成 401；
- TOTP 的时间用 ``mock.patch.object(api.auth, "time", ...)`` 冻结 ——
  ``verify_totp`` 直接调 ``time.time()``，不冻时间就测不了 ±1 窗口，只能碰运气。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """真实 ASGI 栈的测试客户端（进上下文跑 lifespan）。"""
    import main as main_mod

    with TestClient(main_mod.app) as test_client:
        yield test_client


@pytest.fixture
def auth_state():
    """快照并恢复鉴权相关的全局配置。

    这些用例会写 ``auth_password_hash`` / ``auth_totp_secret`` —— 它们存在
    config_store 单例里，而 ``main.auth_middleware`` 每个请求都现读。用例结束
    不恢复的话，同一 pytest 进程里后面所有 ``/api/*`` 请求都会 401（实测踩过
    这类跨用例污染）。
    """
    import api.auth as auth_mod
    from core.config_store import config_store

    original_hash = config_store.get("auth_password_hash", "")
    original_totp = config_store.get("auth_totp_secret", "")
    config_store.set("auth_password_hash", "")
    config_store.set("auth_totp_secret", "")
    auth_mod._pending_2fa.clear()
    try:
        yield config_store
    finally:
        config_store.set("auth_password_hash", original_hash)
        config_store.set("auth_totp_secret", original_totp)
        auth_mod._pending_2fa.clear()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _add_proxy(client, url: str, region: str = "") -> dict:
    response = client.post("/api/proxies", json={"url": url, "region": region})
    assert response.status_code == 200, response.text
    return response.json()


def _delete_proxies_with(client, marker: str) -> None:
    """删掉本用例建的代理（URL 里带唯一 marker），不碰其它用例留下的行。"""
    for row in client.get("/api/proxies").json():
        if marker in row["url"]:
            client.delete(f"/api/proxies/{row['id']}")


# ────────────────────────────── 代理 CRUD ──────────────────────────────


def test_proxy_list_is_json_array(client):
    response = client.get("/api/proxies")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_add_proxy_and_duplicate_rejected(client):
    marker = uuid.uuid4().hex
    url = f"dup-{marker}.example:8000"
    created = _add_proxy(client, url, region="dup-region")
    try:
        assert created["url"] == url
        assert created["region"] == "dup-region"
        assert created["is_active"] is True

        again = client.post("/api/proxies", json={"url": url})
        assert again.status_code == 400, again.text
        assert "已存在" in again.json()["detail"]
    finally:
        _delete_proxies_with(client, marker)


def test_bulk_add_skips_blanks_and_dedupes_existing(client):
    marker = uuid.uuid4().hex
    keep = f"bulk-keep-{marker}.example:8000"
    existing = f"bulk-existing-{marker}.example:8000"
    pre = _add_proxy(client, existing)
    try:
        response = client.post(
            "/api/proxies/bulk",
            json={
                "proxies": ["", "   ", keep, existing, keep],
                "region": "bulk-region",
            },
        )
        assert response.status_code == 200, response.text
        # 空行跳过、已存在的不重加、批内重复只算一次 —— 只加进去 keep 一条
        assert response.json()["added"] == 1

        rows = {row["url"]: row for row in client.get("/api/proxies").json()}
        assert keep in rows
        assert rows[keep]["region"] == "bulk-region"
        # 已存在的行不被 bulk 的 region 覆盖
        assert rows[existing]["id"] == pre["id"]
        assert rows[existing]["region"] != "bulk-region"

        # 整批都重复：added == 0，而不是报错
        response = client.post("/api/proxies/bulk", json={"proxies": [keep, existing]})
        assert response.status_code == 200
        assert response.json()["added"] == 0
    finally:
        _delete_proxies_with(client, marker)


def test_delete_proxy_404_then_success(client):
    response = client.delete("/api/proxies/999999999")
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]

    marker = uuid.uuid4().hex
    created = _add_proxy(client, f"del-{marker}.example:8000")
    response = client.delete(f"/api/proxies/{created['id']}")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    # 再删一次：已经没了，必须是 404 而不是静默成功
    assert client.delete(f"/api/proxies/{created['id']}").status_code == 404


def test_batch_delete_rejects_empty_ids(client):
    response = client.post("/api/proxies/batch-delete", json={"ids": []})
    assert response.status_code == 400
    assert "不能为空" in response.json()["detail"]


def test_batch_delete_rejects_oversized_request(client):
    response = client.post(
        "/api/proxies/batch-delete", json={"ids": list(range(1, 1002))}
    )
    assert response.status_code == 400
    assert "1000" in response.json()["detail"]


def test_batch_delete_reports_not_found_and_dedupes(client):
    marker = uuid.uuid4().hex
    first = _add_proxy(client, f"bd1-{marker}.example:8000")
    second = _add_proxy(client, f"bd2-{marker}.example:8000")
    response = client.post(
        "/api/proxies/batch-delete",
        json={"ids": [first["id"], second["id"], 999999999, first["id"]]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] == 2
    assert body["not_found"] == [999999999]
    # 重复 ID 去重后按唯一 ID 计数，不是按提交条数
    assert body["total_requested"] == 3
    assert client.delete(f"/api/proxies/{first['id']}").status_code == 404


def test_toggle_proxy_404_and_flips_both_ways(client):
    assert client.patch("/api/proxies/999999999/toggle").status_code == 404

    marker = uuid.uuid4().hex
    created = _add_proxy(client, f"tg-{marker}.example:8000")
    try:
        assert created["is_active"] is True
        response = client.patch(f"/api/proxies/{created['id']}/toggle")
        assert response.status_code == 200
        assert response.json()["is_active"] is False
        response = client.patch(f"/api/proxies/{created['id']}/toggle")
        assert response.json()["is_active"] is True
    finally:
        _delete_proxies_with(client, marker)


def test_check_endpoint_starts_background_task(client):
    """``/api/proxies/check`` 只负责把探活排进后台，本身不做网络调用。"""
    with mock.patch("api.proxies.proxy_pool.check_all") as check_mock:
        response = client.post("/api/proxies/check")
        assert response.status_code == 200
        assert "检测任务已启动" in response.json()["message"]
        # 后台任务在 TestClient 里随请求同步执行完
        assert check_mock.call_count == 1


# ────────────────────────── JWT / 密码 / TOTP 纯函数 ──────────────────────────


def test_b64url_roundtrip_including_padding_lengths():
    from api.auth import _b64url_decode, _b64url_encode

    for raw in (b"", b"a", b"ab", b"abc", b"\x00\xff" * 7, b"1234567890"):
        encoded = _b64url_encode(raw)
        assert "=" not in encoded, "JWT 段不能带 padding"
        assert _b64url_decode(encoded) == raw


def test_create_and_verify_token_roundtrip():
    from api.auth import create_token, verify_token

    token = create_token()
    assert token.count(".") == 2
    data = verify_token(token)
    assert data["sub"] == "admin"
    assert data["exp"] > time.time()
    assert data["iat"] <= time.time() + 1


def test_verify_token_rejects_malformed_token():
    from api.auth import verify_token

    with pytest.raises(HTTPException) as excinfo:
        verify_token("not-a-jwt")
    assert excinfo.value.status_code == 401
    assert (excinfo.value.headers or {}).get("X-Panel-Auth-Required") == "1"


def test_verify_token_rejects_tampered_payload():
    from api.auth import _b64url_encode, create_token, verify_token

    token = create_token()
    header, _payload, sig = token.split(".")
    forged = _b64url_encode(
        json.dumps({"sub": "admin", "exp": int(time.time()) + 99999}).encode()
    )
    with pytest.raises(HTTPException) as excinfo:
        verify_token(f"{header}.{forged}.{sig}")
    assert excinfo.value.status_code == 401
    assert "签名" in excinfo.value.detail


def test_verify_token_rejects_expired_token():
    from api.auth import create_token, verify_token

    token = create_token(expire_seconds=-10)
    with pytest.raises(HTTPException) as excinfo:
        verify_token(token)
    assert excinfo.value.status_code == 401
    assert "过期" in excinfo.value.detail


def test_hash_pw_is_stable_sha256():
    from api.auth import _hash_pw

    assert _hash_pw("secret123") == _hash_pw("secret123")
    assert _hash_pw("secret123") == hashlib.sha256("secret123".encode("utf-8")).hexdigest()
    assert _hash_pw("secret123") != _hash_pw("secret124")


def test_generate_totp_secret_is_base32_and_random():
    from api.auth import generate_totp_secret

    first = generate_totp_secret()
    assert len(first) == 32
    # 能解出来才算合法 base32（长度不是 8 的倍数会抛错）
    assert len(base64.b32decode(first)) == 20
    assert first != generate_totp_secret()


def test_totp_at_matches_rfc6238_sha1_vectors():
    """RFC 6238 附录 B 的 SHA1 测试向量（secret = ASCII "12345678901234567890"）。

    用已知向量而不是自算自验：实现里 offset 取字节、掩码、取模任何一处写错，
    自验都测不出来，只有对照标准答案才会红。
    """
    from api.auth import _totp_at

    secret = base64.b32encode(b"12345678901234567890").decode()
    assert _totp_at(secret, 0) == "755224"
    assert _totp_at(secret, 1) == "287082"
    assert _totp_at(secret, 2) == "359152"
    assert _totp_at(secret, 3) == "969429"


def test_verify_totp_accepts_drift_window(monkeypatch):
    """±1 个 30 秒窗口内的码都要认（客户端时钟漂移是常态）。"""
    import api.auth as auth_mod

    frozen = 1_700_000_000.0
    monkeypatch.setattr(auth_mod, "time", SimpleNamespace(time=lambda: frozen))
    secret = auth_mod.generate_totp_secret()
    counter = int(frozen) // 30

    for delta in (-1, 0, 1):
        code = auth_mod._totp_at(secret, counter + delta)
        assert auth_mod.verify_totp(secret, code), f"delta={delta} 应当通过"

    for delta in (-2, 2):
        code = auth_mod._totp_at(secret, counter + delta)
        assert not auth_mod.verify_totp(secret, code), f"delta={delta} 不应通过"


# ────────────────────────────── 鉴权端点 ──────────────────────────────


def test_auth_status_shape(client, auth_state):
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    assert response.json() == {"has_password": False, "has_totp": False}

    auth_state.set("auth_password_hash", "deadbeef")
    auth_state.set("auth_totp_secret", "DEADBEEFDEADBEEFDEADBEEFDEADBEEF")
    body = client.get("/api/auth/status").json()
    assert body == {"has_password": True, "has_totp": True}


def test_setup_rejects_short_password(client, auth_state):
    response = client.post("/api/auth/setup", json={"password": "12345"})
    assert response.status_code == 400
    assert "6" in response.json()["detail"]


def test_setup_requires_token_when_password_already_set(client, auth_state):
    from api.auth import _hash_pw, create_token

    auth_state.set("auth_password_hash", _hash_pw("oldsecret"))
    response = client.post("/api/auth/setup", json={"password": "newsecret"})
    assert response.status_code == 401

    response = client.post(
        "/api/auth/setup",
        json={"password": "newsecret"},
        headers=_bearer(create_token()),
    )
    assert response.status_code == 200, response.text
    assert auth_state.get("auth_password_hash", "") == _hash_pw("newsecret")


def test_login_without_password_returns_403(client, auth_state):
    response = client.post("/api/auth/login", json={"password": "whatever"})
    assert response.status_code == 403
    assert response.json()["detail"] == "no_password_set"


def test_setup_login_flow_enforces_middleware(client, auth_state):
    """设密码 → 业务接口需要 Bearer；登录发 token；错误密码 401。"""
    from api.auth import verify_token

    response = client.post("/api/auth/setup", json={"password": "secret123"})
    assert response.status_code == 200, response.text
    setup_token = response.json()["access_token"]
    assert verify_token(setup_token)["sub"] == "admin"

    # 密码已设 → /api/* 无 token 一律 401（带面板鉴权头）
    denied = client.get("/api/proxies")
    assert denied.status_code == 401
    assert denied.headers.get("X-Panel-Auth-Required") == "1"
    assert client.get("/api/proxies", headers=_bearer(setup_token)).status_code == 200

    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    response = client.post("/api/auth/login", json={"password": "secret123"})
    assert response.status_code == 200
    body = response.json()
    assert body["requires_2fa"] is False
    assert verify_token(body["access_token"])["sub"] == "admin"


def test_two_factor_login_flow(client, auth_state):
    """启用 TOTP 后：登录只发临时令牌，验证码通过才换 access_token。"""
    import api.auth as auth_mod
    from api.auth import _hash_pw, _totp_at, verify_token

    secret = auth_mod.generate_totp_secret()
    auth_state.set("auth_password_hash", _hash_pw("secret123"))
    auth_state.set("auth_totp_secret", secret)

    frozen = 1_700_000_000.0
    with mock.patch.object(auth_mod, "time", SimpleNamespace(time=lambda: frozen)):
        response = client.post("/api/auth/login", json={"password": "secret123"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["requires_2fa"] is True
        assert "access_token" not in body
        temp_token = body["temp_token"]

        # 构造一个确定不在 ±1 窗口内的错误码，避免撞上正确码造成偶发假绿
        counter = int(frozen) // 30
        valid = {_totp_at(secret, counter + delta) for delta in (-1, 0, 1)}
        wrong = next(f"{i:06d}" for i in range(1_000_000) if f"{i:06d}" not in valid)

        response = client.post(
            "/api/auth/verify-totp", json={"temp_token": temp_token, "code": wrong}
        )
        assert response.status_code == 400
        assert temp_token in auth_mod._pending_2fa, "验证码错误不应消耗临时令牌"

        code = _totp_at(secret, counter)
        response = client.post(
            "/api/auth/verify-totp", json={"temp_token": temp_token, "code": code}
        )
        assert response.status_code == 200, response.text
        assert verify_token(response.json()["access_token"])["sub"] == "admin"
        assert temp_token not in auth_mod._pending_2fa, "临时令牌用过即焚"

        # 同一个临时令牌不能换第二次 token
        response = client.post(
            "/api/auth/verify-totp", json={"temp_token": temp_token, "code": code}
        )
        assert response.status_code == 401


def test_verify_totp_rejects_expired_or_unknown_temp_token(client, auth_state):
    import api.auth as auth_mod

    auth_state.set("auth_totp_secret", auth_mod.generate_totp_secret())
    auth_mod._pending_2fa["expired-temp"] = time.time() - 10

    response = client.post(
        "/api/auth/verify-totp", json={"temp_token": "expired-temp", "code": "123456"}
    )
    assert response.status_code == 401
    assert "过期" in response.json()["detail"]

    response = client.post(
        "/api/auth/verify-totp", json={"temp_token": "never-issued", "code": "123456"}
    )
    assert response.status_code == 401


def test_change_password_requires_auth_and_validates(client, auth_state):
    from api.auth import _hash_pw, create_token

    auth_state.set("auth_password_hash", _hash_pw("secret123"))
    headers = _bearer(create_token())

    # 无 token：路由依赖 require_auth 直接 401
    response = client.post(
        "/api/auth/change-password",
        json={"current_password": "secret123", "new_password": "brandnew1"},
    )
    assert response.status_code == 401

    # 当前密码错误 → 400
    response = client.post(
        "/api/auth/change-password",
        json={"current_password": "nope", "new_password": "brandnew1"},
        headers=headers,
    )
    assert response.status_code == 400

    # 新密码过短 → 400
    response = client.post(
        "/api/auth/change-password",
        json={"current_password": "secret123", "new_password": "123"},
        headers=headers,
    )
    assert response.status_code == 400

    # 正常修改 → 落库的哈希更新，新密码能登录
    response = client.post(
        "/api/auth/change-password",
        json={"current_password": "secret123", "new_password": "brandnew1"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert auth_state.get("auth_password_hash", "") == _hash_pw("brandnew1")
    assert (
        client.post("/api/auth/login", json={"password": "brandnew1"}).status_code == 200
    )


def test_disable_auth_clears_credentials(client, auth_state):
    from api.auth import _hash_pw, create_token

    auth_state.set("auth_password_hash", _hash_pw("secret123"))
    auth_state.set("auth_totp_secret", "DEADBEEFDEADBEEFDEADBEEFDEADBEEF")

    # 已设密码时先认证才能关
    assert client.post("/api/auth/disable").status_code == 401

    response = client.post("/api/auth/disable", headers=_bearer(create_token()))
    assert response.status_code == 200
    assert auth_state.get("auth_password_hash", "") == ""
    assert auth_state.get("auth_totp_secret", "") == ""
    # 关掉之后中间件放行，业务接口不再要求 token
    assert client.get("/api/proxies").status_code == 200


def test_2fa_setup_enable_disable_flow(client, auth_state):
    import api.auth as auth_mod
    from api.auth import _hash_pw, _totp_at, create_token

    auth_state.set("auth_password_hash", _hash_pw("secret123"))
    headers = _bearer(create_token())

    assert client.get("/api/auth/2fa/setup").status_code == 401
    response = client.get("/api/auth/2fa/setup", headers=headers)
    assert response.status_code == 200, response.text
    secret = response.json()["secret"]
    assert "otpauth://totp/" in response.json()["uri"]

    frozen = 1_700_000_000.0
    with mock.patch.object(auth_mod, "time", SimpleNamespace(time=lambda: frozen)):
        counter = int(frozen) // 30
        valid = {_totp_at(secret, counter + delta) for delta in (-1, 0, 1)}
        wrong = next(f"{i:06d}" for i in range(1_000_000) if f"{i:06d}" not in valid)

        # 密钥太短 → 400
        response = client.post(
            "/api/auth/2fa/enable", json={"secret": "short", "code": "123456"}, headers=headers
        )
        assert response.status_code == 400

        # 验证码错误 → 400，密钥不落库
        response = client.post(
            "/api/auth/2fa/enable", json={"secret": secret, "code": wrong}, headers=headers
        )
        assert response.status_code == 400
        assert auth_state.get("auth_totp_secret", "") == ""

        # 正确验证码 → 启用
        response = client.post(
            "/api/auth/2fa/enable",
            json={"secret": secret, "code": _totp_at(secret, counter)},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert auth_state.get("auth_totp_secret", "") == secret

    response = client.post("/api/auth/2fa/disable", headers=headers)
    assert response.status_code == 200
    assert auth_state.get("auth_totp_secret", "") == ""
