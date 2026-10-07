"""api/shared_mail.py 未覆盖分支补齐。

tests/test_shared_mail_page.py 已覆盖：token 不可猜、免登录出最新一封、
面板有密码时仍可打开、HTML 转义、纯文本正文、空收件箱、未知 token 404、
老库回填 token。本文件只补剩余分支：

* `_body_frame` 空正文分支（无 html、无 text、无 snippet）
* `_sender` 的四种变体（名+邮箱 / 只有名 / 只有邮箱 / 都没有）
* `_received_at` 的 None、datetime、非 datetime 三条路径
* 端点错误路径：非 alias_not_found 的 ICloudError -> 502，未知异常 -> 500

无需落库：直接打桩 `services.icloud_service.fetch_latest_shared_message`，
端点里 `icloud_service` 与它是同一个模块对象。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api.shared_mail import _body_frame, _received_at, _sender
from platforms.icloud.errors import ICloudError
from platforms.icloud.models import MailAddress, MailMessage


def _message(**overrides) -> MailMessage:
    base = dict(
        provider_message_id="uid-1",
        mailbox="INBOX",
        subject="主题",
        sender=MailAddress(email="noreply@tm.openai.com", name="OpenAI"),
        received_at=datetime(2026, 8, 21, 5, 20, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return MailMessage(**base)


# ------------------------------------------------------------- _body_frame


def test_body_frame_without_any_body_says_so():
    frame = _body_frame(_message(html_body="", text_body="", snippet=""))

    assert frame == '<div class="empty">这封邮件没有正文</div>'


def test_body_frame_whitespace_only_body_counts_as_empty():
    frame = _body_frame(_message(html_body="  \n ", text_body="\t", snippet=" "))

    assert "这封邮件没有正文" in frame


def test_body_frame_falls_back_to_snippet_when_text_body_is_missing():
    frame = _body_frame(_message(html_body="", text_body="", snippet="来自摘要 654321"))

    assert '<div class="text">来自摘要 654321</div>' == frame


def test_body_frame_escapes_plain_text():
    frame = _body_frame(_message(html_body="", text_body="<b>x</b> & y"))

    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in frame
    assert "<b>x</b>" not in frame


def test_body_frame_prefers_html_over_text():
    frame = _body_frame(_message(html_body="<p>html</p>", text_body="text"))

    assert "srcdoc=" in frame
    assert "<p>html</p>" not in frame  # 只以转义形式进 srcdoc
    assert "&lt;p&gt;html&lt;/p&gt;" in frame


# ----------------------------------------------------------------- _sender


def test_sender_with_name_and_email():
    rendered = _sender(_message(sender=MailAddress(email="a@b.com", name="张三")))

    assert rendered == "张三 <span>&lt;a@b.com&gt;</span>"


def test_sender_with_name_only():
    rendered = _sender(_message(sender=MailAddress(email="", name="只有名字")))

    assert rendered == "只有名字"


def test_sender_with_email_only():
    rendered = _sender(_message(sender=MailAddress(email="only@x.com")))

    assert rendered == "only@x.com"


def test_sender_with_neither_becomes_unknown():
    rendered = _sender(_message(sender=MailAddress(email="", name="  ")))

    assert rendered == "未知发件人"


def test_sender_escapes_both_parts():
    rendered = _sender(_message(sender=MailAddress(email="a<b>@x.com", name="<img>")))

    assert "<img>" not in rendered
    assert "&lt;img&gt;" in rendered
    assert "&lt;b&gt;" in rendered


# ------------------------------------------------------------- _received_at


def test_received_at_none_is_blank():
    assert _received_at(_message(received_at=None)) == ""


def test_received_at_formats_the_datetime_in_local_time():
    moment = datetime(2026, 8, 21, 5, 20, tzinfo=timezone.utc)

    rendered = _received_at(_message(received_at=moment))

    assert rendered == moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def test_received_at_non_datetime_falls_back_to_str():
    """老数据/桩对象可能塞进来的是字符串，别让页面 500。"""
    assert _received_at(_message(received_at="昨天")) == "昨天"


def test_received_at_naive_datetime_is_treated_as_local():
    naive = datetime(2026, 8, 21, 5, 20)

    assert _received_at(_message(received_at=naive)) == "2026-08-21 05:20:00"


# ----------------------------------------------------------- 端点错误路径


@pytest.fixture
def client():
    """端点在 main.app 上；`icloud_service` 与 services 包共享同一模块对象。"""
    from main import app

    return TestClient(app)


def test_page_shows_empty_body_message_without_error(client, monkeypatch):
    import services.icloud_service as icloud_service

    message = _message(html_body="", text_body="", snippet="")
    monkeypatch.setattr(
        icloud_service, "fetch_latest_shared_message", lambda token: ("alias@icloud.com", message)
    )

    response = client.get("/m/any-token")

    assert response.status_code == 200
    assert "这封邮件没有正文" in response.text


def test_other_icloud_error_becomes_502_and_keeps_the_reason(client, monkeypatch):
    import services.icloud_service as icloud_service

    def _fail(_token):
        raise ICloudError("upstream_unavailable", "iCloud Mail 暂时不可用（HTTP 503）")

    monkeypatch.setattr(icloud_service, "fetch_latest_shared_message", _fail)

    response = client.get("/m/any-token")

    assert response.status_code == 502
    assert "暂时读不到邮件" in response.text
    assert "iCloud Mail 暂时不可用（HTTP 503）" in response.text


def test_unexpected_exception_becomes_500_without_leaking_the_stack(client, monkeypatch):
    import services.icloud_service as icloud_service

    def _crash(_token):
        raise RuntimeError("secret stack detail")

    monkeypatch.setattr(icloud_service, "fetch_latest_shared_message", _crash)

    response = client.get("/m/any-token")

    assert response.status_code == 500
    assert "服务异常，请稍后再试。" in response.text
    assert "secret stack detail" not in response.text


def test_error_pages_keep_no_store_headers(client, monkeypatch):
    import services.icloud_service as icloud_service

    monkeypatch.setattr(
        icloud_service,
        "fetch_latest_shared_message",
        lambda _token: (_ for _ in ()).throw(ICloudError("upstream_unavailable", "挂了")),
    )

    response = client.get("/m/any-token")

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
