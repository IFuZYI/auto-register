"""邮箱导入与贡献服务器 API 的集成/单元测试。

为什么专门测这两块：

- ``/api/mail-imports`` 的四个端点（快照/执行/删除/批量删除）都是「薄壳」：
  路由只做类型解析、异常映射（ValueError / RuntimeError → 400）。薄壳本身没人
  钉的话，改一个 except 顺序或漏一个分支，界面拿到的就是 500 —— 而真实场景里
  这些都是用户能触发的输入（贴错格式、删一个已经不存在的行）。
- ``api/contribution.py`` 的「候选端点回退」是它最核心的行为：贡献服务器有多个
  历史版本的路径（``/public/quota-stats`` 与 ``/public/quota/stats`` 等），客户端
  必须按顺序探测、命中即用、全部失败才报 502 并把每次尝试回报出来。这条逻辑
  只能在 mock 掉出网的前提下测，否则测试依赖外部服务器的可用性。

测试手法：

- 出网一律 mock（``api.contribution.requests.request``）—— 真实网络请求既慢又
  不稳定，且这里要验的是**回退顺序**本身；
- 邮箱导入只走 ``mailapi_url`` 行：OAuth 行会触发真实探活（微软 Graph），
  测试里不该碰网络；``mailapi_url`` 分支不探活（见 providers 的
  ``_evaluate_availability`` 短路）；
- 用例导入的邮箱行用唯一 marker 邮箱并在用例结束后清理 —— conftest 的库虽然
  是临时库，但同一个 pytest 会话里其它用例共享它。
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """真实 ASGI 栈的测试客户端（进上下文跑 lifespan）。"""
    import main as main_mod

    with TestClient(main_mod.app) as test_client:
        yield test_client


@pytest.fixture
def contrib_config(monkeypatch):
    """把贡献配置收进一个字典，隔离 config_store 与真实环境变量。

    ``_resolve_*`` 读的是 ``config_store.get``，而它会回落到 ``.env`` / 进程环境
    （本机可能真的配了贡献服务器）—— 不隔离的话「未配置时用默认地址」这类断言
    会看本机脸色。这里把单例的 ``get`` 换成字典查询，pytest 负责还原。
    """
    from core.config_store import config_store

    values: dict[str, str] = {}
    monkeypatch.setattr(config_store, "get", lambda key, default="": values.get(key, default))
    return values


# ─────────────────────────── 邮箱导入：providers / 快照 ───────────────────────────


def test_providers_lists_descriptors_once(client):
    response = client.get("/api/mail-imports/providers")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert isinstance(items, list) and items

    microsoft = [item for item in items if item["type"] == "microsoft"]
    # registry 内部有 "outlook" 别名指向同一个策略，descriptors() 必须去重
    assert len(microsoft) == 1, f"microsoft 描述符重复下发: {items}"
    descriptor = microsoft[0]
    assert descriptor["label"]
    assert descriptor["description"]
    assert "----" in descriptor["content_placeholder"], "要给出导入格式示例"


def test_snapshot_unknown_type_maps_value_error_to_400(client):
    response = client.get("/api/mail-imports/snapshot?type=unknown")
    assert response.status_code == 400, response.text
    assert "不支持的邮箱导入类型" in response.json()["detail"]


def test_snapshot_outlook_alias_resolves_to_microsoft(client):
    """``outlook`` 是 registry 的别名 —— 走别名也要能拿到 microsoft 的快照。"""
    response = client.get("/api/mail-imports/snapshot?type=outlook")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["type"] == "microsoft"
    assert body["label"]
    assert isinstance(body["items"], list)
    assert isinstance(body["truncated"], bool)


def test_snapshot_requires_type_param(client):
    response = client.get("/api/mail-imports/snapshot")
    assert response.status_code == 422


def test_snapshot_preview_limit_is_bounded(client):
    """preview_limit 在 schema 里被约束（1..500），越界要 400 而不是静默截断。"""
    assert (
        client.get("/api/mail-imports/snapshot?type=microsoft&preview_limit=0").status_code
        == 400
    )
    assert (
        client.get("/api/mail-imports/snapshot?type=microsoft&preview_limit=999").status_code
        == 400
    )


def test_snapshot_maps_strategy_value_error_to_400(client, monkeypatch):
    """策略层抛 ValueError（如请求体校验失败）也要映射成 400，不能漏成 500。"""
    import api.mail_imports as mail_imports_module

    class _Boom:
        descriptor = SimpleNamespace(type="microsoft")

        def get_snapshot(self, request):
            raise ValueError("坏掉的快照请求")

    monkeypatch.setattr(mail_imports_module.mail_import_registry, "get", lambda _t: _Boom())
    response = client.get("/api/mail-imports/snapshot?type=microsoft")
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "坏掉的快照请求"


# ─────────────────────────── 邮箱导入：执行 / 删除 ───────────────────────────


@pytest.fixture
def imported_emails(client):
    """跟踪并清理用例导入的邮箱行（同会话里其它用例共享临时库）。"""
    created: list[str] = []
    yield created
    if created:
        client.post(
            "/api/mail-imports/batch-delete",
            json={
                "type": "microsoft",
                "items": [{"email": email} for email in created],
            },
        )


def test_execute_unknown_type_is_rejected(client):
    """POST 的 type 是 Literal 字段 —— 请求体层就被 FastAPI 拦成 422。

    （对比：GET /snapshot 的 type 是裸 query 参数，由策略层抛 ValueError 再
    映射成 400。两条路径的差异是真实的，各自钉住。）
    """
    response = client.post("/api/mail-imports", json={"type": "unknown", "content": "x"})
    assert response.status_code == 422, response.text


def test_execute_empty_content_is_a_noop_success(client):
    response = client.post("/api/mail-imports", json={"type": "microsoft", "content": ""})
    assert response.status_code == 200, response.text
    summary = response.json()["summary"]
    assert summary == {"total": 0, "success": 0, "failed": 0}


def test_execute_imports_mailapi_rows_and_snapshot_reflects_them(
    client, imported_emails
):
    """走真实 ASGI 栈的导入链路：解析 → 落库 → 快照回读。

    用 ``mailapi_url`` 行而不是 OAuth 行：后者会触发真实微软探活（出网）。
    """
    marker = uuid.uuid4().hex
    first = f"probe-a-{marker}@icloud.com"
    second = f"probe-b-{marker}@icloud.com"

    response = client.post(
        "/api/mail-imports",
        json={
            "type": "microsoft",
            "content": (
                f"{first}----https://reg.example.com/m/{marker}a\n"
                f"{second}----https://reg.example.com/m/{marker}b"
            ),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    imported_emails.extend([first, second])

    assert body["summary"] == {"total": 2, "success": 2, "failed": 0}
    assert body["errors"] == []
    by_email = {item["email"]: item for item in body["snapshot"]["items"]}
    assert by_email[first]["account_type"] == "mailapi_url"
    assert by_email[first]["status"] == "unpooled", "新导入的账号默认不进池"
    assert by_email[first]["enabled"] is True
    assert by_email[first]["id"], "快照要带 id，界面靠它映射回账号"

    # 快照端点独立回读，同样能看到这两行
    snapshot = client.get("/api/mail-imports/snapshot?type=microsoft").json()
    emails = {item["email"] for item in snapshot["items"]}
    assert {first, second} <= emails


def test_execute_reports_per_row_errors(client, imported_emails):
    marker = uuid.uuid4().hex
    good = f"probe-good-{marker}@icloud.com"

    response = client.post(
        "/api/mail-imports",
        json={
            "type": "microsoft",
            "content": (
                f"{good}----https://reg.example.com/m/{marker}\n"
                f"{good}----https://reg.example.com/m/{marker}\n"
                "bad@icloud.com----x----y"
            ),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    imported_emails.append(good)

    # 批内重复 + 3 段格式错误 —— 各记一条 failed，成功的照常入库
    assert body["summary"] == {"total": 3, "success": 1, "failed": 2}
    joined = " ".join(body["errors"])
    assert "重复邮箱" in joined
    assert "格式错误" in joined


def test_execute_rejects_email_already_in_pool(client, imported_emails):
    """第二次导入同一个地址要明确报「已存在」，而不是静默重复入库。"""
    marker = uuid.uuid4().hex
    email = f"probe-dup-{marker}@icloud.com"
    payload = {
        "type": "microsoft",
        "content": f"{email}----https://reg.example.com/m/{marker}",
    }

    first = client.post("/api/mail-imports", json=payload)
    assert first.json()["summary"] == {"total": 1, "success": 1, "failed": 0}
    imported_emails.append(email)

    second = client.post("/api/mail-imports", json=payload)
    assert second.status_code == 200
    assert second.json()["summary"] == {"total": 1, "success": 0, "failed": 1}
    assert "已存在" in second.json()["errors"][0]


def test_execute_maps_strategy_runtime_error_to_400(client, monkeypatch):
    import api.mail_imports as mail_imports_module

    class _Boom:
        def execute(self, request):
            raise RuntimeError("运行时坏了")

    monkeypatch.setattr(mail_imports_module.mail_import_registry, "get", lambda _t: _Boom())
    response = client.post("/api/mail-imports", json={"type": "microsoft", "content": "x"})
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "运行时坏了"


def test_delete_missing_email_reports_400(client):
    """RuntimeError 映射成 400：空地址与不存在的地址各有一条明确信息。"""
    response = client.post("/api/mail-imports/delete", json={"type": "microsoft", "email": ""})
    assert response.status_code == 400
    assert "缺少" in response.json()["detail"]

    response = client.post(
        "/api/mail-imports/delete",
        json={"type": "microsoft", "email": "nobody@outlook.invalid"},
    )
    assert response.status_code == 400
    assert "未找到" in response.json()["detail"]


def test_delete_removes_row_and_snapshot_shrinks(client, imported_emails):
    marker = uuid.uuid4().hex
    email = f"probe-del-{marker}@icloud.com"
    client.post(
        "/api/mail-imports",
        json={"type": "microsoft", "content": f"{email}----https://reg.example.com/m/{marker}"},
    )
    imported_emails.append(email)

    before = client.get("/api/mail-imports/snapshot?type=microsoft").json()
    assert email in {item["email"] for item in before["items"]}

    response = client.post("/api/mail-imports/delete", json={"type": "microsoft", "email": email})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"] == {"total": 1, "success": 1, "failed": 0}
    assert body["meta"]["deleted_email"] == email

    after = client.get("/api/mail-imports/snapshot?type=microsoft").json()
    assert email not in {item["email"] for item in after["items"]}


def test_batch_delete_reports_found_and_missing(client, imported_emails):
    marker = uuid.uuid4().hex
    email = f"probe-bd-{marker}@icloud.com"
    client.post(
        "/api/mail-imports",
        json={"type": "microsoft", "content": f"{email}----https://reg.example.com/m/{marker}"},
    )
    imported_emails.append(email)

    response = client.post(
        "/api/mail-imports/batch-delete",
        json={
            "type": "microsoft",
            "items": [{"email": email}, {"email": "nobody@outlook.invalid"}],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"] == {"total": 2, "success": 1, "failed": 1}
    assert body["meta"]["deleted_emails"] == [email]
    assert any("未找到" in err for err in body["errors"])


def test_batch_delete_with_empty_items_is_a_noop(client):
    """空清单是合法的空操作（与代理批量删除的「空 → 400」语义不同）。"""
    response = client.post("/api/mail-imports/batch-delete", json={"type": "microsoft", "items": []})
    assert response.status_code == 200, response.text
    assert response.json()["summary"] == {"total": 0, "success": 0, "failed": 0}


# ─────────────────────────── 贡献服务器：纯函数 ───────────────────────────


def test_resolve_server_url_normalization(contrib_config):
    from api.contribution import DEFAULT_CONTRIBUTION_SERVER_URL, _resolve_server_url

    # 空 / None / 全空白 → 项目默认地址，且规范成「一个尾斜杠」
    assert _resolve_server_url(None) == DEFAULT_CONTRIBUTION_SERVER_URL
    assert _resolve_server_url("") == DEFAULT_CONTRIBUTION_SERVER_URL
    assert _resolve_server_url("   ") == DEFAULT_CONTRIBUTION_SERVER_URL
    assert DEFAULT_CONTRIBUTION_SERVER_URL.endswith("/")

    # 缺 scheme 补 http://
    assert _resolve_server_url("contrib.example:7317") == "http://contrib.example:7317/"
    # 多尾斜杠收敛成一个；已有 scheme 不被改写
    assert _resolve_server_url("https://contrib.example:7317///") == "https://contrib.example:7317/"
    assert _resolve_server_url("  http://a.b  ") == "http://a.b/"


def test_resolve_server_url_falls_back_to_config(contrib_config):
    from api.contribution import _resolve_server_url

    contrib_config["contribution_server_url"] = "cfg.example:1234"
    assert _resolve_server_url(None) == "http://cfg.example:1234/"

    # 请求里显式给了地址 → 优先于全局配置
    assert _resolve_server_url("explicit.example:99") == "http://explicit.example:99/"


def test_resolve_key_requires_a_configured_key(contrib_config):
    from api.contribution import _resolve_key

    with pytest.raises(HTTPException) as excinfo:
        _resolve_key(None)
    assert excinfo.value.status_code == 400
    assert "贡献 key" in excinfo.value.detail

    # 显式 key 优先，且会 strip
    assert _resolve_key("  pk-explicit  ") == "pk-explicit"

    # 回落全局配置
    contrib_config["contribution_key"] = "pk-config"
    assert _resolve_key(None) == "pk-config"
    assert _resolve_key("") == "pk-config"


def test_resolve_key_optional_returns_empty_instead_of_raising(contrib_config):
    from api.contribution import _resolve_key_optional

    assert _resolve_key_optional(None) == ""
    assert _resolve_key_optional("  ") == ""
    assert _resolve_key_optional("pk-1") == "pk-1"

    contrib_config["contribution_key"] = "pk-config"
    assert _resolve_key_optional(None) == "pk-config"


# ─────────────────────────── 贡献服务器：出网封装 ───────────────────────────


class _FakeResponse:
    """够用的 requests.Response 替身：状态码 + json()/text。"""

    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if payload is None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _install_router(monkeypatch, routes: dict[tuple[str, str], _FakeResponse]):
    """按 ``(method, path 后缀)`` 路由 mock 出网请求，并记录每次调用的 kwargs。

    替换的是 ``api.contribution`` 名字空间里的 ``requests``，不是全局 requests
    模块 —— 后者会影响同进程里其它仍在用真实 requests 的代码。
    """
    import api.contribution as contribution_module

    calls: list[dict] = []

    def _fake_request(**kwargs):
        calls.append(kwargs)
        url = str(kwargs.get("url", ""))
        method = str(kwargs.get("method", "")).upper()
        for (route_method, suffix), response in routes.items():
            if method == route_method and url.endswith(suffix):
                return response
        return _FakeResponse(404, {"detail": f"no route for {method} {url}"})

    fake_requests = SimpleNamespace(
        request=_fake_request, RequestException=requests.RequestException
    )
    monkeypatch.setattr(contribution_module, "requests", fake_requests)
    return calls


def test_request_json_builds_url_headers_and_payload(monkeypatch, contrib_config):
    from api.contribution import _request_json

    calls = _install_router(monkeypatch, {("POST", "/public/x"): _FakeResponse(200, {"ok": 1})})
    data = _request_json("post", "http://srv.test:7317/", "/public/x", "pk-1", payload={"a": 1})

    assert data == {"ok": 1}
    call = calls[0]
    assert call["method"] == "POST", "method 必须被大写"
    assert call["url"] == "http://srv.test:7317/public/x", "endpoint 前导斜杠要去掉再拼接"
    assert call["timeout"] == 15
    assert call["headers"]["X-Public-Key"] == "pk-1"
    assert call["headers"]["Authorization"] == "Bearer pk-1"
    assert call["json"] == {"a": 1}

    # 不给 key 时不能带鉴权头
    calls = _install_router(monkeypatch, {("GET", "/public/y"): _FakeResponse(200, {})})
    _request_json("GET", "http://srv.test:7317/", "/public/y")
    assert "headers" not in calls[0]


def test_request_json_wraps_non_dict_and_non_json(monkeypatch, contrib_config):
    from api.contribution import _request_json

    _install_router(monkeypatch, {("GET", "/public/list"): _FakeResponse(200, [1, 2])})
    assert _request_json("GET", "http://srv.test:7317/", "/public/list") == {"data": [1, 2]}

    _install_router(monkeypatch, {("GET", "/public/html"): _FakeResponse(200, None, "<html>oops</html>")})
    assert _request_json("GET", "http://srv.test:7317/", "/public/html") == {"raw": "<html>oops</html>"}


def test_request_json_extracts_error_detail(monkeypatch, contrib_config):
    from api.contribution import _request_json

    for payload, expected in (
        ({"detail": "plain"}, "plain"),
        ({"detail": {"message": "nested"}}, "nested"),
        ({"error": "err-field"}, "err-field"),
        ({"detail": {"code": 42}}, "42"),
    ):
        _install_router(monkeypatch, {("GET", "/public/e"): _FakeResponse(400, payload)})
        with pytest.raises(HTTPException) as excinfo:
            _request_json("GET", "http://srv.test:7317/", "/public/e")
        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == expected, payload

    # 非 JSON 的 5xx：detail 里要带上原始正文，不能只剩一句空话
    _install_router(monkeypatch, {("GET", "/public/5xx"): _FakeResponse(500, None, "<html>bad</html>")})
    with pytest.raises(HTTPException) as excinfo:
        _request_json("GET", "http://srv.test:7317/", "/public/5xx")
    assert excinfo.value.status_code == 500
    assert "bad" in excinfo.value.detail


def test_request_json_maps_connection_error_to_502(monkeypatch, contrib_config):
    import api.contribution as contribution_module
    from api.contribution import _request_json

    def _raise(**kwargs):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(
        contribution_module,
        "requests",
        SimpleNamespace(request=_raise, RequestException=requests.RequestException),
    )
    with pytest.raises(HTTPException) as excinfo:
        _request_json("GET", "http://srv.test:7317/", "/public/x")
    assert excinfo.value.status_code == 502
    assert "连接贡献服务器失败" in excinfo.value.detail


# ─────────────────────── 贡献服务器：端点候选回退 ───────────────────────


def test_quota_stats_falls_back_through_candidates(client, monkeypatch, contrib_config):
    """第一个候选 404 → 换下一个；命中后 key 信息同样走回退。"""
    calls = _install_router(
        monkeypatch,
        {
            ("GET", "/public/quota-stats"): _FakeResponse(404, {"detail": "not found"}),
            ("GET", "/public/quota/stats"): _FakeResponse(200, {"quota_remaining": 42}),
            ("GET", "/public/key-info"): _FakeResponse(404, {"detail": "not found"}),
            ("GET", "/public/key/info"): _FakeResponse(200, {"balance": 7}),
        },
    )

    response = client.post(
        "/api/contribution/quota-stats",
        json={"server_url": "http://contrib.test:7317", "key": "pk-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["server_method"] == "GET"
    assert body["server_endpoint"] == "/public/quota/stats"
    assert body["key_endpoint"] == "/public/key/info"
    assert body["data"]["server_info"] == {"quota_remaining": 42}
    assert body["data"]["key_info"] == {"balance": 7}

    # 探测顺序：server 候选依次尝试，命中即止；随后才轮到 key 候选
    assert [call["url"] for call in calls] == [
        "http://contrib.test:7317/public/quota-stats",
        "http://contrib.test:7317/public/quota/stats",
        "http://contrib.test:7317/public/key-info",
        "http://contrib.test:7317/public/key/info",
    ]


def test_quota_stats_without_key_reports_key_error(client, monkeypatch, contrib_config):
    calls = _install_router(
        monkeypatch,
        {("GET", "/public/quota-stats"): _FakeResponse(200, {"quota_remaining": 1})},
    )
    response = client.post("/api/contribution/quota-stats", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["key_info"] is None
    assert "未配置 key" in body["key_error"]
    assert len(calls) == 1, "没有 key 就不该去问 key 信息接口"


def test_quota_stats_all_candidates_failing_is_502_with_attempts(
    client, monkeypatch, contrib_config
):
    _install_router(monkeypatch, {})  # 所有路由 404
    response = client.post("/api/contribution/quota-stats", json={})
    assert response.status_code == 502, response.text
    detail = response.json()["detail"]
    assert "codex2api" in detail["message"]
    attempts = detail["attempts"]
    assert [a["endpoint"] for a in attempts] == [
        "/public/quota-stats",
        "/public/quota/stats",
        "/public/quota-stats",
        "/public/quota/stats",
    ], "四个候选（GET/POST 各两个）都要被尝试过"
    assert all(a["status_code"] == 404 for a in attempts)


def test_key_info_requires_key_and_reports_fallback(client, monkeypatch, contrib_config):
    # 未配置 key → 400（_resolve_key 的 HTTPException）
    response = client.post("/api/contribution/key-info", json={})
    assert response.status_code == 400
    assert "贡献 key" in response.json()["detail"]

    _install_router(
        monkeypatch,
        {
            ("GET", "/public/key-info"): _FakeResponse(404, {"detail": "not found"}),
            ("GET", "/public/key/info"): _FakeResponse(200, {"balance": 7}),
        },
    )
    response = client.post("/api/contribution/key-info", json={"key": "pk-1"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "ok": True,
        "method": "GET",
        "endpoint": "/public/key/info",
        "data": {"balance": 7},
    }


def test_key_info_all_failing_is_502(client, monkeypatch, contrib_config):
    _install_router(monkeypatch, {})
    response = client.post("/api/contribution/key-info", json={"key": "pk-1"})
    assert response.status_code == 502
    assert len(response.json()["detail"]["attempts"]) == 4


def test_redeem_validates_amount_and_reports_result(client, monkeypatch, contrib_config):
    # amount_usd 必须 > 0（schema 层直接 422）
    assert (
        client.post("/api/contribution/redeem", json={"key": "k", "amount_usd": 0}).status_code
        == 422
    )
    assert (
        client.post("/api/contribution/redeem", json={"key": "k", "amount_usd": -1}).status_code
        == 422
    )

    calls = _install_router(
        monkeypatch,
        {
            ("POST", "/public/redeem"): _FakeResponse(
                200, {"redeemed_amount_usd": 12.5, "code": "RDM-1"}
            )
        },
    )
    response = client.post(
        "/api/contribution/redeem",
        json={"server_url": "http://contrib.test:7317", "key": "pk-1", "amount_usd": 12.5},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["endpoint"] == "/public/redeem"
    assert body["redeemed_amount_usd"] == 12.5
    assert body["code"] == "RDM-1"
    assert "12.5" in body["message"] and "RDM-1" in body["message"]
    assert calls[0]["json"] == {"amount_usd": 12.5}


def test_redeem_falls_back_to_legacy_endpoint(client, monkeypatch, contrib_config):
    _install_router(
        monkeypatch,
        {
            ("POST", "/api/contribution/redeem"): _FakeResponse(200, {"code": "RDM-2"}),
        },
    )
    response = client.post(
        "/api/contribution/redeem",
        json={"server_url": "http://contrib.test:7317", "key": "pk-1", "amount_usd": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["endpoint"] == "/api/contribution/redeem"
    assert body["code"] == "RDM-2"
    # 金额缺失时 message 用 '-' 占位而不是 None
    assert "redeemed_amount_usd" not in body or body["redeemed_amount_usd"] is None
    assert "额度：-" in body["message"]


def test_redeem_all_failing_is_502(client, monkeypatch, contrib_config):
    _install_router(monkeypatch, {})
    response = client.post(
        "/api/contribution/redeem",
        json={"key": "pk-1", "amount_usd": 5},
    )
    assert response.status_code == 502
    assert len(response.json()["detail"]["attempts"]) == 2


def test_generate_key_sends_name_only_when_given(client, monkeypatch, contrib_config):
    calls = _install_router(
        monkeypatch, {("POST", "/public/generate"): _FakeResponse(200, {"key": "pk-new"})}
    )
    response = client.post(
        "/api/contribution/generate-key",
        json={"server_url": "http://contrib.test:7317", "name": "my-key"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["endpoint"] == "/public/generate"
    assert body["data"] == {"key": "pk-new"}
    assert calls[0]["json"] == {"name": "my-key"}

    calls = _install_router(
        monkeypatch, {("POST", "/public/generate"): _FakeResponse(200, {"key": "pk-2"})}
    )
    response = client.post("/api/contribution/generate-key", json={})
    assert response.status_code == 200
    assert "json" not in calls[0], "没给 name 时不该发空的 json 体"
