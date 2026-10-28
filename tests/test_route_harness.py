"""线路夹具的自我测试：证明夹具**能抓住错误**。

Task 1 的产物是 `tests/route_harness.py`（注册线路验证的公共夹具，供
Task 2-7 使用，见 `.superpowers/sdd/full-route-verification-plan/`）。
夹具的天职是「帮线路跑通」，所以它自己坏了最容易被放过 —— 这些测试全都
把错误数据喂进去、要求夹具抛断言：夹具抓不住错误，线路的绿就是假的。

每条测试用自己的邮箱，并在 `finally` 里 `cleanup_accounts` 清行走。
"""

from __future__ import annotations

import pytest

from tests.route_harness import (
    FakeHttp,
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
