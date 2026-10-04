"""MailAPI URL 号池的取码链路。

`邮箱----mailapi_url` 这类账号运行时不走 Graph/IMAP，而是反复 GET 那个 URL 再从
返回的网页里抠验证码。这里盯两件事：号池取号后确实路由到 MailAPI 后端，以及页面
真拿到手时能把码抠出来——用一份真实的隐私邮箱分享页当样本。
"""

from pathlib import Path

import pytest

from core.base_mailbox import MailApiUrlOtpBackend, MailboxAccount, OutlookMailbox

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "shared_mail_chatgpt_otp.html"


def _mailapi_account(url: str = "https://reg.example.com/m/tok") -> MailboxAccount:
    return MailboxAccount(
        email="alias.sample@icloud.com",
        account_id="1",
        extra={"account_type": "mailapi_url", "mailapi_url": url},
    )


def test_shared_mail_page_yields_the_chatgpt_code():
    """分享页把邮件正文塞在 iframe 的 srcdoc 里，整页转义了一遍还得抠得出来。"""
    backend = MailApiUrlOtpBackend(OutlookMailbox())

    code = backend._extract_code(FIXTURE.read_text(encoding="utf-8"), None)

    assert code == "993177"


def test_content_before_a_blank_line_is_not_thrown_away():
    """网页不是原始邮件，不能按邮件头那样从第一个空行处腰斩——码常在被砍掉的那半边。"""
    backend = MailApiUrlOtpBackend(OutlookMailbox())
    page = "<p>验证码 246813</p>\n\n<style>.pad{padding:0}</style>"

    assert backend._extract_code(page, None) == "246813"


def test_digits_inside_a_tracking_link_are_not_mistaken_for_the_code():
    backend = MailApiUrlOtpBackend(OutlookMailbox())
    page = "<p>http://t.example.com/wf/open?upn=u001.abc123456def</p><p>887766</p>"

    assert backend._extract_code(page, None) == "887766"


def test_wait_for_code_polls_the_url_until_a_new_code_shows_up(monkeypatch):
    backend = MailApiUrlOtpBackend(OutlookMailbox())
    pages = iter(["<p>还没有邮件</p>", "<p>验证码 135790</p>"])
    monkeypatch.setattr(backend, "_fetch_mailapi_text", lambda _account: next(pages))
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    assert backend.wait_for_code(_mailapi_account(), timeout=30) == "135790"


def test_missing_url_is_reported_instead_of_silently_polling_nothing():
    backend = MailApiUrlOtpBackend(OutlookMailbox())
    account = MailboxAccount(email="a@b.com", extra={"account_type": "mailapi_url"})

    with pytest.raises(RuntimeError, match="mailapi_url 为空"):
        backend._fetch_mailapi_text(account)


def test_mailapi_accounts_route_to_the_mailapi_backend():
    """池子里 OAuth 和 MailAPI 是混着放的，选后端只能看账号自己的类型。"""
    mailbox = OutlookMailbox()

    assert mailbox._resolve_backend(_mailapi_account()) is mailbox._backends["mailapi_url"]

    oauth_account = MailboxAccount(
        email="demo@outlook.com",
        extra={
            "account_type": "microsoft_oauth",
            "client_id": "cid",
            "refresh_token": "rt",
        },
    )
    assert mailbox._resolve_backend(oauth_account) is mailbox._backends["graph"]
