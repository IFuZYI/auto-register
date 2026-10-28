"""线路夹具的自我测试：证明夹具**能抓住错误**。

Task 1 的产物是 `tests/route_harness.py`（注册线路验证的公共夹具，供
Task 2-7 使用，见 `.superpowers/sdd/full-route-verification-plan/`）。
夹具的天职是「帮线路跑通」，所以它自己坏了最容易被放过 —— 这些测试全都
把错误数据喂进去、要求夹具抛断言：夹具抓不住错误，线路的绿就是假的。

每条测试用自己的邮箱，并在 `finally` 里 `cleanup_accounts` 清行走。
"""

from __future__ import annotations

from unittest import mock

import pytest

from tests.route_harness import (
    FakeHttp,
    FakeResponse,
    a_response,
    assert_account_persisted,
    assert_no_secret_leak,
    cleanup_accounts,
    make_account,
)


def test_assert_account_persisted_catches_missing_rt() -> None:
    """extra 里没有 refresh_token → 断言必须抛。

    变异判据：把 harness 里的 RT 检查去掉，这条必须变红。
    """
    email = "harness-missing-rt@example.com"
    make_account(
        email=email,
        password="pw-harness",
        token="at-harness",
        extra={"access_token": "at-harness"},  # 有 AT、没 RT
    )
    try:
        with pytest.raises(AssertionError, match="refresh_token"):
            assert_account_persisted(email, expect_rt=True)
    finally:
        cleanup_accounts(email)


def test_assert_no_secret_leak_catches_the_secret() -> None:
    """完整凭证出现 = 泄漏；掩码/前缀出现不算。

    变异判据：把 harness 泄漏检查去掉（或反过来把前缀也算泄漏）必须变红。
    """
    with pytest.raises(AssertionError):
        assert_no_secret_leak("token=abc123", ["abc123"])
    # 掩码显示与「只露前缀」是排查日志的常规做法 —— 不该被当成泄漏
    assert_no_secret_leak("token=***", ["abc123"])
    assert_no_secret_leak("token=abc1…", ["abc123"])


def test_fake_http_records_requests_and_returns_in_sequence() -> None:
    """FakeHttp：按顺序出响应，并把每次请求记下来（URL / body / headers）。

    变异判据：不记录请求（len 断言红）或响应不按顺序（状态码断言红）。
    """
    with FakeHttp(
        [
            a_response({"step": 1}, status=201),
            a_response({"step": 2}, status=202),
        ]
    ) as http:
        first = http.post(
            "https://chatgpt.com/step-1", json={"step": 1}, headers={"X-Trace": "t1"}
        )
        second = http.post("https://chatgpt.com/step-2", json={"step": 2})

    assert len(http.requests) == 2
    assert [first.status_code, second.status_code] == [201, 202]
    assert first.json() == {"step": 1}
    assert http.requests[0].url == "https://chatgpt.com/step-1"
    assert http.requests[0].method == "POST"
    assert http.requests[0].body == {"step": 1}
    assert http.requests[0].headers == {"X-Trace": "t1"}
    assert http.requests[1].url == "https://chatgpt.com/step-2"
    assert http.requests[1].body == {"step": 2}


def test_assert_account_persisted_catches_wrong_status() -> None:
    """凭证齐全但状态不是 registered → 断言必须抛（计划要求「状态正确」）。

    变异判据：把 harness 的状态检查去掉，这条必须变红。
    """
    email = "harness-wrong-status@example.com"
    make_account(
        email=email,
        password="pw-harness",
        token="at-harness",
        status="banned",
        extra={"access_token": "at-harness", "refresh_token": "rt-harness"},
    )
    try:
        with pytest.raises(AssertionError, match="状态"):
            assert_account_persisted(email)
    finally:
        cleanup_accounts(email)


def test_fake_http_drives_real_redirect_chain_without_network() -> None:
    """真实 ``AuthFlow`` 的 warmup + 重定向链 + ``get_auth_session`` 全程离线跑通。

    这是本夹具存在的意义：光有 ``status_code``/``json`` 不够。``get_auth_session``
    必经 ``resp.raise_for_status()``（``redirect.py:413``），3xx 分支读
    ``resp.headers.get("Location")``，warmup 用 ``session.cookies.get_dict()``
    判成功。缺任何一样，真实线路就 ``AttributeError`` 或跑回网络。

    ``create_http_session`` 被替换成返回本夹具：调用次数 == 1 证明线路没有
    「warmup 失败 → 重建真实 session」逃逸到网络（``auth_flow.py:387`` 最多重建
    4 次）。
    """
    from platforms.chatgpt.protocol.auth_flow import AuthFlow
    from platforms.chatgpt.protocol.config import Config

    http = FakeHttp(
        [
            # warmup：GET 首页（响应本身无所谓，判据是 cookie jar 里有 oai-did）
            a_response(text="<html>ok</html>"),
            FakeResponse(302, headers={"location": "https://auth.openai.com/authorize?x=1"}),
            FakeResponse(
                302,
                headers={
                    "Location": (
                        "https://chatgpt.com/api/auth/callback/openai?code=abc&state=s"
                    )
                },
            ),
            a_response({"sessionToken": "", "accessToken": "at-from-response"}),
        ],
        cookies={"oai-did": "dev-1", "__Secure-next-auth.session-token": "st-from-cookie"},
    )
    factory_calls: list[dict] = []

    def _factory(**kwargs):
        factory_calls.append(kwargs)
        return http

    with mock.patch(
        "platforms.chatgpt.protocol.auth_flow.create_http_session",
        side_effect=_factory,
    ):
        flow = AuthFlow(Config())
        warmed = flow.warmup()
        callback, final = flow.follow_redirect_chain("https://auth.openai.com/authorize")
        session_token, access_token = flow.get_auth_session()

    assert warmed is True, "warmup 没从 cookie jar 读到 oai-did（jar 接口不对）"
    assert callback == "https://chatgpt.com/api/auth/callback/openai?code=abc&state=s"
    assert final == callback
    # 重定向链真的逐跳走了：第一跳到 Location 指向的地址
    assert http.requests[1].url == "https://auth.openai.com/authorize"
    assert http.requests[2].url == "https://auth.openai.com/authorize?x=1"
    # get_auth_session：session_token 来自 cookie jar，AT 来自响应 JSON
    assert access_token == "at-from-response"
    assert session_token == "st-from-cookie"
    # **没有逃逸到网络**：只在构造 AuthFlow 时建了一次 session，且返回的是本夹具
    assert len(factory_calls) == 1, "线路重建了 session —— 说明有真实网络逃逸"


def test_fake_response_raise_for_status_and_case_insensitive_headers() -> None:
    """``raise_for_status`` 只对 4xx/5xx 抛；``Location`` 两种拼写都读得到。

    变异判据：去掉 ``raise_for_status`` → 这里 ``pytest.raises`` 不满足；
    把 headers 换成普通 dict → 小写 ``location`` 读不到。
    """
    from requests.exceptions import HTTPError

    ok = a_response({"a": 1})
    ok.raise_for_status()  # 2xx 不抛

    redirect = FakeResponse(302, headers={"Location": "https://x/y"})
    redirect.raise_for_status()  # 3xx 不抛：重定向链要自己跟随
    assert redirect.headers.get("Location") == "https://x/y"
    assert redirect.headers.get("location") == "https://x/y"

    with pytest.raises(HTTPError):
        a_response(status=403).raise_for_status()
    with pytest.raises(HTTPError):
        a_response(status=500).raise_for_status()


def test_fake_http_cookie_jar_supports_warmup_and_signup_reads() -> None:
    """cookie jar：``get_dict`` / ``set`` / 迭代（``.name``）/ ``get`` / ``clear``。

    warmup 的判据是 ``get_dict()`` 里有 ``oai-did``；signup 直接迭代 jar 读
    ``cookie.name``。两种读法都要成立，否则真实线路中途 AttributeError。

    变异判据：删掉 ``FakeHttp.cookies``（或只给个空 dict）→ 这条红。
    """
    http = FakeHttp([a_response({})], cookies={"oai-did": "dev-1"})
    assert http.cookies.get_dict() == {"oai-did": "dev-1"}
    assert [c.name for c in http.cookies] == ["oai-did"]

    http.cookies.set("__Secure-next-auth.session-token", "st-1")
    assert http.cookies.get("__Secure-next-auth.session-token") == "st-1"
    assert http.cookies.get_dict()["__Secure-next-auth.session-token"] == "st-1"

    http.cookies.clear()
    assert http.cookies.get_dict() == {}


def test_assert_account_persisted_reads_token_column_like_the_project() -> None:
    """只在 ``token`` 列上有 AT、extra 为空 → 仍算「有 AT」（规范读法）。

    ``platforms/chatgpt/plugin.py`` 的读法是
    ``get_credential(extra, ...) or token_column_credential(row, platform, ...)``。
    夹具只读 extra 会把「历史行 / 导入行只写了列」误判成缺凭证。

    变异判据：把 ``_credential`` 退回只读 extra → 这条红（抛「缺少 AT」）。
    """
    email = "harness-column-only@example.com"
    make_account(
        email=email,
        password="pw-harness",
        token="at-in-column-only",
        extra={"refresh_token": "rt-in-extra"},
    )
    try:
        data = assert_account_persisted(email, expect_at=True, expect_rt=True)
        assert data["token"] == "at-in-column-only"
    finally:
        cleanup_accounts(email)


def test_make_account_persists_created_at_and_keeps_zero() -> None:
    """``created_at`` 真的落库（int 秒也不静默丢）；``0`` 不被当成空值。

    变异判据：``created_at`` 原样塞 int（仓储只认 datetime）→ 时间断言红；
    把 ``_as_text`` 换回 ``str(v or "")`` → ``user_id="0"`` 变空串。
    """
    email = "harness-created-at@example.com"
    make_account(
        email=email,
        password="pw-harness",
        user_id=0,
        created_at=1_700_000_000,
        extra={"access_token": "at-h", "refresh_token": "rt-h"},
    )
    try:
        data = assert_account_persisted(email)
        assert data["user_id"] == "0", "合法的 0 被当成空值吞掉了"
        from core.db import account_repository

        row = account_repository.find_by_email("chatgpt", email)
        assert row.created_at.year == 2023, f"created_at 没落库: {row.created_at!r}"
    finally:
        cleanup_accounts(email)


def test_fake_response_json_returns_fresh_copy() -> None:
    """``json()`` 交出副本：线路就地改返回值不得污染下一次读。

    变异判据：直接返回 ``self._json_body`` → 第二次读会看到被改过的键。
    """
    body = {"accessToken": "at-1"}
    resp = a_response(body)
    first = resp.json()
    first["accessToken"] = "tampered"
    first.pop("accessToken")
    assert resp.json() == {"accessToken": "at-1"}
    assert body == {"accessToken": "at-1"}, "传入的原始 dict 被就地改掉了"

