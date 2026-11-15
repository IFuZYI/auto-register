"""等验证码时的收件范围：读得**窄**，且降级必须能被发现。

为什么需要
----------
2026-11-14 事故的真正根因不在熔断，而在这条链路：

1. 等码轮询发 `limit=50, days=3650`（`modules/mail/icloud_remote.py`）。
   上游 icloud-hme 的 IMAP 信封预算是 `max(15s, limit × 0.3s)`、上限 45s ——
   `limit=50` 时预算正好卡在 15s，而实测 `limit=50` 的冷读中位 14.4s、
   峰值 34.5s（`limit=10` 只有中位 5.2s、峰值 19.3s）。
2. 超 15s 上游就**降级 Web API**。而降级路径按收件人过滤是不可靠的：
   `WebClient.FindByAlias` 只对 To/主题/发件人做子串匹配，收件人信息缺失时
   命中 0 封 —— 实测同一封存在的邮件，`method=imap` 读到 1 封，
   `method=web_api` 读到 0 封。
3. `count=0` 不是错误：它不触发熔断、不带异常，于是 worker 判定「没有新邮件」，
   一直等到 60s 超时 —— 日志里表现为「等码超时」。

所以修法不是把客户端读超时调大（那只会让客户端安静地等一个已经被降级的
结果），而是：
- 等码只读最近几封、只读近几天（不给自己制造 15s 悬崖、不 SEARCH 上千封）；
- 降级本身要能被上层识别，别把「读不到」当成「没有」。
"""
from __future__ import annotations

import pytest

from platforms.icloud.errors import ICloudError
from tests.remote_hme_double import API_KEY, RemoteHMEDouble


@pytest.fixture
def remote():
    double = RemoteHMEDouble()
    double.start()
    try:
        yield double
    finally:
        double.stop()


def _client(remote):
    from platforms.icloud.remote_client import RemoteHMEClient

    return RemoteHMEClient(remote.base_url, API_KEY, timeout=5, use_gate=False)


def _remote_row(remote):
    """「已接入远程账号」的行对象（不落库，只喂给 fetch_remote_messages）。"""
    from unittest import mock

    row = mock.Mock()
    row.source = "remote"
    row.email = "owner@icloud.com"
    row.base_url = remote.base_url
    row.remote_account_id = "acc_1"
    row.proxy = ""
    return row


@pytest.fixture(autouse=True)
def _stub_secret_box():
    """`remote_client()` 会解密库里的 API 秘钥。单测不建库，直接把解密器替掉。

    替的是**加密边界**（外部依赖），不是被测行为：客户端仍真实发 HTTP 到
    测试双端，所以「参数到底传了什么」照样能被断言到。
    """
    from unittest import mock

    import services.icloud.remote as remote_module

    with mock.patch.object(remote_module, "remote_api_key", lambda row: API_KEY):
        yield


class TestOtpReadScope:
    """钉住等码路径真正发给上游的参数。"""

    def test_otp_poll_limit_is_between_too_small_and_too_wide(self):
        """等码的 limit 有**下界和上界**，两头都会丢信/超时。

        下界（实测 2026-11-14，同一别名同一时刻）：
          limit=5 → 0 封；limit=10 → 0 封；limit≥15 → 2 封（稳定复现）
          原因：上游 `forEachByRecipientRange` 用 `need = offset + limit` 截候选，
          太小就把该别名的信截掉 —— 比「慢」严重得多，是**丢信**。

        上界：
          `limit=50` 让 IMAP 信封预算顶到 15s 悬崖（`max(15s, limit×0.3s)`），
          实测冷读峰值 34.5s，超时即降级 Web API（按收件人过滤不可靠 → 0 封）。
        """
        from unittest import mock

        from modules.mail.icloud_remote import ICloudRemoteMailbox

        mailbox = ICloudRemoteMailbox(account_id="1")
        captured: dict = {}

        service = mock.Mock()
        service.release_stale_claims = lambda: None
        service.renew_alias_claim = lambda *a, **k: None

        def fetch_account_messages(account_id, *, limit=50, recipient=""):
            captured["limit"] = limit
            captured["recipient"] = recipient
            return []

        def fetch_account_messages_detailed(account_id, *, limit=50, recipient=""):
            # 渠道现在优先走 detailed（要拿 method/warning），limit 走同一条路
            return fetch_account_messages(
                account_id, limit=limit, recipient=recipient
            ), "imap", ""

        service.fetch_account_messages = fetch_account_messages
        service.fetch_account_messages_detailed = fetch_account_messages_detailed
        mailbox._service = lambda: service  # type: ignore[method-assign]

        account = _account()
        mailbox.get_current_ids(account)

        assert captured["limit"] >= 15, (
            f"等码只读 {captured['limit']} 封 —— 实测 limit<15 会被上游截掉该别名的信"
            "（同一别名 limit=5/10 读到 0 封，limit>=15 读到 2 封）"
        )
        assert captured["limit"] < 50, (
            f"等码读了 {captured['limit']} 封 —— 上游 IMAP 信封预算会被顶到 15s 悬崖"
        )

    def test_remote_read_uses_a_narrow_day_window(self, remote):
        """时间窗要窄：3650 天会让上游 SEARCH 出上千封再本地分页。"""
        from services.icloud.remote import fetch_remote_messages

        fetch_remote_messages(_remote_row(remote), limit=5, recipient="a@icloud.com")

        _, path, params = remote.requests[-1]
        assert path == "/api/inbox"
        assert int(params["days"]) <= 7, (
            f"时间窗 {params['days']} 天 —— 上游要 SEARCH 上千封才分页"
        )


class TestEndToEndPassthrough:
    """从渠道到上游的**完整链路**上，窄参数不能在中途被丢掉。"""

    def test_service_layer_passes_the_channel_limit_upstream(self, remote, monkeypatch):
        """`services.icloud.messages` 必须把渠道给的 limit 传进远端请求。

        中间任何一层写死一个小值或吞掉参数，窄读就白改了 —— 而单测各自
        通过、日志里依旧是 50 封，是最难查的那种回退。
        """
        import services.icloud_service  # noqa: F401 - 门面按名字查这个模块
        from services.icloud import messages as messages_mod
        from tests.remote_hme_double import API_KEY
        from platforms.icloud.remote_client import RemoteHMEClient

        client = RemoteHMEClient(remote.base_url, API_KEY, timeout=5, use_gate=False)
        row = _remote_row(remote)

        monkeypatch.setattr(messages_mod, "get_account", lambda account_id: row)
        monkeypatch.setattr(
            messages_mod, "read_semaphore", lambda account_id: __import__("contextlib").nullcontext()
        )
        import services.icloud.remote as rem

        monkeypatch.setattr(rem, "remote_client", lambda r, proxy=None, timeout=None: client)

        messages_mod.fetch_account_messages_detailed(1, limit=20, recipient="a@icloud.com")

        _, path, params = remote.requests[-1]
        assert path == "/api/inbox"
        assert int(params["limit"]) == 20, "服务层把渠道的 limit 改掉了"
        assert int(params["days"]) <= 7, "服务层没有把时间窗收窄"


class TestDegradedReadIsVisible:
    """降级读到的 0 封，不能被当成「这个别名没有邮件」。"""

    def test_degraded_read_is_reported_not_swallowed(self, remote):
        """上游降级（web_api）时，调用方必须能拿到这个信号。

        `method` 是本模块的接口承诺（imap / web_api / remote），远程分支恒为
        "remote"，所以降级信号只能落在 `warning` 上。现状：warning 恒为空串
        —— 于是「IMAP 挂了、降级读不到」与「确实没有新邮件」完全一样。
        """
        from services.icloud.remote import fetch_remote_messages

        remote.degrade_to_web_api = True

        messages, method, warning = fetch_remote_messages(
            _remote_row(remote), limit=5, recipient="a@icloud.com"
        )

        assert messages == []
        assert method == "remote", "远程分支的 method 承诺不能改"
        assert "web_api" in warning, f"降级没暴露给调用方: {warning!r}"
        assert "不可靠" in warning, "没有说清降级的后果（0 封≠没邮件）"

    def test_healthy_read_still_reports_no_warning(self, remote):
        """没有降级时不能乱报 warning（否则上层会把正常读当成故障）。"""
        from services.icloud.remote import fetch_remote_messages

        messages, method, warning = fetch_remote_messages(
            _remote_row(remote), limit=5, recipient="a@icloud.com"
        )

        assert method == "remote"
        assert warning == ""

    def test_degraded_first_read_marks_the_baseline_untrusted(self, remote):
        """降级时基线必须标成「不可信」，且日志要说出这是降级。

        契约：首读失败**不打断**注册（返回空集），但必须置 `_baseline_degraded`。
        真实事故里 worker 把「降级后 0 封」当成「收件箱本来就没信」，于是
        历史邮件可能被当成本轮新信、而真正的新信读不到 —— 日志里只剩「等码超时」。
        """
        remote.degrade_to_web_api = True
        mailbox = _wired_mailbox(remote)
        logs: list[str] = []
        mailbox._log = logs.append

        ids = mailbox.get_current_ids(_account())

        assert ids == set(), "首读失败不该打断注册"
        assert mailbox._baseline_degraded, "降级没有被标成基线不可信"
        # 日志要带上游给的原因（这里双端回的是 Web API 降级说明）
        assert any("降级" in line and "Web API" in line for line in logs), (
            f"日志没体现「上游降级」: {logs}"
        )


class TestOtpPollKeepsWaitingOnDegradedRead:
    """降级读不到信时，等码循环必须**继续等待**，而不是当成「没收到码」。"""

    def test_degraded_poll_keeps_polling_until_timeout(self, remote):
        """降级期间轮询次数要 > 1（说明没被当成「没信」提前收工）。"""
        remote.degrade_to_web_api = True
        mailbox = _wired_mailbox(remote)
        polls: list[int] = []

        original = mailbox._messages_detailed

        def counting(account, limit=5):
            polls.append(1)
            return original(account, limit=limit)

        mailbox._messages_detailed = counting  # type: ignore[method-assign]

        # 带上发码时间窗：没有时间窗 + 基线不可信时，渠道会按既有契约
        # 「保守不猜码」立刻放弃，那是另一条正确行为，不是这里要测的。
        import time as _time

        # timeout 必须大于一个轮询间隔（渠道固定 5s），否则第二次轮询前就到点
        with pytest.raises(TimeoutError):
            mailbox.wait_for_code(_account(), timeout=7, otp_sent_at=_time.time())

        assert len(polls) > 1, f"降级把等码提前终结了（只轮询 {len(polls)} 次）"

    def test_degraded_poll_is_logged_as_transient(self, remote):
        """继续等待时日志要说清「降级」，不能静默空转。"""
        remote.degrade_to_web_api = True
        mailbox = _wired_mailbox(remote)
        logs: list[str] = []
        mailbox._log = logs.append

        import time as _time

        with pytest.raises(TimeoutError):
            mailbox.wait_for_code(_account(), timeout=2, otp_sent_at=_time.time())

        assert any("降级" in line for line in logs), f"降级没有留下痕迹: {logs}"


def _account():
    from core.base_mailbox import MailboxAccount

    return MailboxAccount(
        email="a@icloud.com",
        account_id="1:a@icloud.com",
        extra={"icloud_account_id": 1, "icloud_alias_id": 1},
    )


def _wired_mailbox(remote):
    """把渠道的业务调用接到测试双端（不碰真实 iCloud 库）。"""
    from unittest import mock

    from modules.mail.icloud_remote import ICloudRemoteMailbox

    mailbox = ICloudRemoteMailbox(account_id="1")
    client = _client(remote)

    service = mock.Mock()
    service.release_stale_claims = lambda: None
    service.renew_alias_claim = lambda *a, **k: None

    def fetch_account_messages_detailed(account_id, *, limit=50, recipient=""):
        data = client.fetch_inbox(
            str(account_id), alias=recipient, limit=limit, days=3
        )
        return data.get("messages") or [], data.get("method") or "", data.get("warning") or ""

    def fetch_account_messages(account_id, *, limit=50, recipient=""):
        return fetch_account_messages_detailed(
            account_id, limit=limit, recipient=recipient
        )[0]

    service.fetch_account_messages = fetch_account_messages
    service.fetch_account_messages_detailed = fetch_account_messages_detailed
    mailbox._service = lambda: service  # type: ignore[method-assign]
    return mailbox


if __name__ == "__main__":
    pytest.main([__file__, "-q"])