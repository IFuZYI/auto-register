import unittest
from unittest import mock

from core.base_mailbox import MailboxAccount, OutlookMailbox, create_mailbox


class _SelectFallbackImap:
    def __init__(self):
        self.calls = []

    def select(self, folder, readonly=False):
        self.calls.append((folder, readonly))
        if readonly:
            raise RuntimeError("EXAMINE command error: BAD Command Argument Error. 12")
        return "OK", []


class _FakeResponse:
    def __init__(self, status_code, payload=None, text="", json_error=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or ""
        self.content = b"{}" if payload is not None or json_error is not None else b""
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return dict(self._payload)


class OutlookMailboxOAuthTests(unittest.TestCase):
    def test_imap_select_falls_back_to_mutable_select_when_examine_is_rejected(self):
        mailbox = OutlookMailbox()
        connection = _SelectFallbackImap()

        with mock.patch.object(mailbox, "_log") as log:
            result = mailbox._backends["imap"]._select_folder(connection, "INBOX")

        self.assertEqual(result[0], "OK")
        self.assertEqual(connection.calls, [("INBOX", True), ("INBOX", False)])
        log.assert_called_once()

    def test_create_mailbox_outlook_defaults_to_graph_backend(self):
        mailbox = create_mailbox("outlook", extra={})

        self.assertIsInstance(mailbox, OutlookMailbox)
        self.assertEqual(mailbox._backend_name, "graph")

    def test_graph_backend_delegates_to_imap_after_imap_scope_is_detected(self):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={"client_id": "client-id", "refresh_token": "refresh-token"},
        )
        with mock.patch.object(
            mailbox,
            "_get_oauth_access_token",
            side_effect=lambda _account, **_kwargs: account.extra.update(_oauth_backend_capability="imap") or "imap-token",
        ), mock.patch.object(
            mailbox._backends["imap"],
            "get_current_ids",
            return_value={"INBOX:1"},
        ) as imap_ids:
            result = mailbox._backends["graph"].get_current_ids(account)
        self.assertEqual(result, {"INBOX:1"})
        imap_ids.assert_called_once_with(account)

    @mock.patch("requests.post")
    def test_fetch_oauth_token_graph_backend_prefers_graph_scope(self, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")

        responses = [
            _FakeResponse(
                400,
                text='{"error":"invalid_grant","error_description":"scopes requested are unauthorized"}',
            ),
            _FakeResponse(
                200,
                payload={"access_token": "access-token-demo"},
                text='{"access_token":"access-token-demo"}',
            ),
        ]
        mock_post.side_effect = responses

        token = mailbox._fetch_oauth_token(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertEqual(token, "access-token-demo")
        self.assertEqual(mock_post.call_count, 2)

        first_scope = mock_post.call_args_list[0].kwargs["data"].get("scope", "")
        second_scope = mock_post.call_args_list[1].kwargs["data"].get("scope", "")
        self.assertEqual(
            first_scope,
            "https://graph.microsoft.com/.default",
        )
        self.assertEqual(
            second_scope,
            "https://outlook.office.com/.default offline_access",
        )

    @mock.patch("requests.post")
    def test_fetch_oauth_token_imap_backend_prefers_imap_scope(self, mock_post):
        mailbox = OutlookMailbox(
            token_endpoint="https://token.example.test",
            backend="imap",
        )
        mock_post.side_effect = [
            _FakeResponse(
                200,
                payload={"access_token": "imap-token"},
                text='{"access_token":"imap-token"}',
            ),
        ]

        token = mailbox._fetch_oauth_token(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertEqual(token, "imap-token")
        self.assertEqual(
            mock_post.call_args.kwargs["data"].get("scope", ""),
            "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
        )

    @mock.patch("requests.post")
    def test_probe_oauth_availability_detects_service_abuse_mode(self, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        mock_post.return_value = _FakeResponse(
            400,
            text='{"error":"invalid_grant","error_description":"User account is found to be in service abuse mode."}',
        )

        result = mailbox.probe_oauth_availability(
            email="demo@hotmail.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "service_abuse_mode")
        self.assertIn("service abuse mode", result["message"])

    @mock.patch("requests.post")
    def test_probe_oauth_availability_returns_ok_when_access_token_is_obtained(self, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        mock_post.return_value = _FakeResponse(
            200,
            payload={"access_token": "ok-token", "expires_in": 3600},
            text='{"access_token":"ok-token","expires_in":3600}',
        )

        result = mailbox.probe_oauth_availability(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["access_token"], "ok-token")

    @mock.patch("requests.post")
    @mock.patch("requests.request")
    def test_wait_for_code_uses_graph_backend_by_default(self, mock_request, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "client_id": "client-id",
                "refresh_token": "refresh-token",
            },
        )
        mock_post.return_value = _FakeResponse(
            200,
            payload={"access_token": "graph-token", "expires_in": 3600},
            text='{"access_token":"graph-token","expires_in":3600}',
        )
        mock_request.return_value = _FakeResponse(
            200,
            payload={
                "value": [
                    {
                        "id": "message-1",
                        "subject": "OpenAI verification code",
                        "bodyPreview": "Your verification code is 123456",
                    }
                ]
            },
        )

        code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "123456")
        self.assertIn(
            "/me/mailFolders/inbox/messages",
            str(mock_request.call_args.args[1]),
        )

    @mock.patch("requests.get")
    def test_wait_for_code_uses_mailapi_backend_for_mailapi_account(self, mock_get):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mailapi.icu/key?type=html&orderNo=abc123",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text="<html><body>Your OpenAI verification code is 246810</body></html>",
        )

        code = mailbox.wait_for_code(
            account,
            timeout=5,
            before_ids=set(),
        )

        self.assertEqual(code, "246810")
        self.assertTrue(mock_get.call_count >= 1)
        self.assertEqual(
            mock_get.call_args.kwargs.get("timeout"),
            15,
        )

    @mock.patch("requests.post")
    @mock.patch("requests.request")
    def test_wait_for_code_reads_deleteditems_folder_when_inbox_has_no_new_code(self, mock_request, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "client_id": "client-id",
                "refresh_token": "refresh-token",
            },
        )
        mock_post.return_value = _FakeResponse(
            200,
            payload={"access_token": "graph-token", "expires_in": 3600},
            text='{"access_token":"graph-token","expires_in":3600}',
        )
        mock_request.side_effect = [
            _FakeResponse(200, payload={"value": []}),
            _FakeResponse(200, payload={"value": []}),
            _FakeResponse(
                200,
                payload={
                    "value": [
                        {
                            "id": "deleted-message-1",
                            "subject": "OpenAI verification code",
                            "bodyPreview": "Your verification code is 654321",
                        }
                    ]
                },
            ),
        ]

        code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "654321")
        requested_urls = [str(call.args[1]) for call in mock_request.call_args_list]
        self.assertTrue(any("/me/mailFolders/deleteditems/messages" in url for url in requested_urls))

    @mock.patch("requests.post")
    def test_fetch_oauth_token_returns_empty_when_probe_gets_malformed_json_on_2xx(self, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        mock_post.side_effect = [
            _FakeResponse(
                200,
                text="not-json",
                json_error=ValueError("malformed json"),
            ),
            _FakeResponse(
                200,
                text="still-not-json",
                json_error=ValueError("malformed json again"),
            ),
        ]

        token = mailbox._fetch_oauth_token(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertEqual(token, "")
        attempted_scopes = [
            call.kwargs["data"].get("scope", "")
            for call in mock_post.call_args_list
        ]
        self.assertIn(
            "https://graph.microsoft.com/.default",
            attempted_scopes,
        )
        self.assertIn(
            "https://outlook.office.com/.default offline_access",
            attempted_scopes,
        )


if __name__ == "__main__":
    unittest.main()


class OAuthFailureClassificationTests(unittest.TestCase):
    """OAuth 换 token 失败必须**区分可重试与永久**，否则两种故障同形。

    2026-11-15 实测（邮箱鲁棒性专项）：
    - `probe_oauth_availability` 对**每种**失败都只返回 `ok=False`（除了
      service_abuse_mode 一种特例），调用方 `_fetch_oauth_token_bundle`
      于是统一走「回退密码登录」；
    - 而回填账号通常**没有密码**（只有 client_id/refresh_token）→ 直接失败，
      根本不进入等码轮询。
    - 实测：token 端点返回 503（瞬时）与返回 invalid_grant（永久）在日志里
      长得一样，都表现为「OAuth token 获取失败，回退密码登录」，
      排查时无法判断该重试还是该换号。

    这里钉的行为：`probe_oauth_availability` 的返回必须带**可判定**的
    `retryable` 标记 —— 5xx / 网络错误 = 可重试；invalid_grant = 永久。
    """

    def _probe(self, mock_post, status: int, body: str) -> dict:
        mock_post.return_value = _FakeResponse(status, payload={}, text=body)
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        return mailbox.probe_oauth_availability(
            email="d@outlook.com",
            client_id="cid",
            refresh_token="rt",
        )

    @mock.patch("requests.post")
    def test_transient_5xx_is_marked_retryable(self, mock_post):
        result = self._probe(mock_post, 503, '{"error":"temporarily_unavailable"}')
        self.assertFalse(result.get("ok"))
        self.assertTrue(
            result.get("retryable"),
            f"503 被标成永久失败 —— 瞬时抖动会被当成账号坏了: {result.get('reason')}",
        )

    @mock.patch("requests.post")
    def test_invalid_grant_is_not_retryable(self, mock_post):
        result = self._probe(
            mock_post, 400,
            '{"error":"invalid_grant","error_description":"AADSTS70000"}',
        )
        self.assertFalse(result.get("ok"))
        self.assertFalse(
            result.get("retryable"),
            "invalid_grant 被标成可重试 —— 会无限重试一个已失效的凭据",
        )

    @mock.patch("requests.post")
    def test_network_error_is_retryable(self, mock_post):
        mock_post.side_effect = RuntimeError("connection reset")
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        result = mailbox.probe_oauth_availability(
            email="d@outlook.com", client_id="cid", refresh_token="rt",
        )
        self.assertFalse(result.get("ok"))
        self.assertTrue(result.get("retryable"), f"网络错误被当成永久失败: {result}")


class TransientOAuthFailureShouldNotKillTheRoundTests(unittest.TestCase):
    """瞬时 OAuth 失败必须**抛可重试**错误，而不是让整轮等码当场结束。

    2026-11-15 实测（邮箱鲁棒性专项）：`_get_oauth_access_token` 在拿不到
    token 时抛**普通** `RuntimeError`。上层 `_run_polling_wait` 没有
    `is_transient` 钩子，于是这一次异常直接穿透整轮 —— 一次 503 定生死。

    正确的形状：可重试的失败要标出来（`retryable=True`），让调用方/轮询层能
    「等一个周期再来」；永久失败（invalid_grant）保持立即失败，别浪费配额。
    """

    def _mailbox(self):
        return OutlookMailbox(token_endpoint="https://token.example.test")

    @mock.patch("requests.post")
    def test_transient_503_raises_a_retryable_error(self, mock_post):
        mock_post.return_value = _FakeResponse(503, payload={}, text='{"error":"unavailable"}')
        mb = self._mailbox()
        account = MailboxAccount(
            email="d@outlook.com",
            extra={"client_id": "cid", "refresh_token": "rt"},
        )
        with self.assertRaises(Exception) as ctx:
            mb._get_oauth_access_token(account)
        self.assertTrue(
            getattr(ctx.exception, "retryable", False),
            f"503 抛的是不可重试错误（{type(ctx.exception).__name__}），"
            f"一次抖动就会结束整轮等码",
        )

    @mock.patch("requests.post")
    def test_invalid_grant_raises_a_non_retryable_error(self, mock_post):
        mock_post.return_value = _FakeResponse(
            400, payload={}, text='{"error":"invalid_grant"}'
        )
        mb = self._mailbox()
        account = MailboxAccount(
            email="d@outlook.com",
            extra={"client_id": "cid", "refresh_token": "rt"},
        )
        with self.assertRaises(Exception) as ctx:
            mb._get_oauth_access_token(account)
        self.assertFalse(
            getattr(ctx.exception, "retryable", True),
            "invalid_grant 被标成可重试 —— 会无限重试一个已失效的凭据",
        )


class TransientTokenFailureFallsBackToPollingTests(unittest.TestCase):
    """Graph 后端的 token 预检失败（瞬时）不该直接结束整轮。

    2026-11-15 实测：`OutlookGraphMailboxBackend.wait_for_code` 在进入轮询
    循环**之前**先调 `_get_oauth_access_token`。一次 503 就在这里抛出去，
    连一次轮询都没发生 —— 而轮询本身每 5 秒会重试取 token，本可以自愈。

    钉的行为：预检遇到**可重试**失败时，不抛，而是继续进轮询
    （由轮询里的重试与错误预算兜住）；遇到**永久**失败时仍然立刻抛。
    """

    def _backend(self):
        from core.mailboxes.channels.outlook.backends import OutlookGraphMailboxBackend

        mb = OutlookMailbox(token_endpoint="https://t.example.test")
        return mb, OutlookGraphMailboxBackend(mb)

    def test_transient_precheck_failure_still_enters_polling(self):
        """预检遇到**可重试**失败时不抛，仍进入轮询（由轮询自身重试）。

        注意：要让 Graph 后端**不被降级到 IMAP**，必须让 token 那次成功返回，
        因为 `_oauth_backend_capability` 只有在拿到 token 后才会被置成 graph。
        所以第一次预检抛可重试错、之后返回 token —— 后面的轮询会正常走 Graph。
        """
        from core.mailboxes.channels.outlook.backends import OutlookGraphMailboxBackend

        mb, backend = self._backend()
        account = MailboxAccount(
            email="d@outlook.com",
            extra={"client_id": "cid", "refresh_token": "rt", "outlook_backend": "graph"},
        )
        calls = {"n": 0}

        def _flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _RetryableOAuth("503 transient")
            # 置上 graph 能力，避免后续降级到 IMAP
            account.extra["_oauth_backend_capability"] = "graph"
            return "tok"

        with mock.patch.object(mb, "_get_oauth_access_token", side_effect=_flaky), \
             mock.patch.object(mb, "_graph_list_messages", return_value=[]):
            with self.assertRaises(TimeoutError):
                backend.wait_for_code(account, timeout=2)

        self.assertGreaterEqual(calls["n"], 1, "预检一次都没跑")

    def test_permanent_precheck_failure_still_raises(self):
        mb, backend = self._backend()
        account = MailboxAccount(
            email="d@outlook.com",
            extra={"client_id": "cid", "refresh_token": "rt", "outlook_backend": "graph"},
        )

        class _Permanent(Exception):
            retryable = False

        with mock.patch.object(
            mb, "_get_oauth_access_token", side_effect=_Permanent("invalid_grant")
        ):
            with self.assertRaises(_Permanent):
                backend.wait_for_code(account, timeout=2)


class _RetryableOAuth(Exception):
    retryable = True


class PermanentMarkerOutranksHttpStatusTests(unittest.TestCase):
    """`invalid_grant` 不管配哪个 HTTP 状态都必须是**永久**失败。

    2026-11-15 变异验证暴露的测试漏洞：原先只测了「400 + invalid_grant」，
    而分类器的 4xx 回退分支本来就返回「永久」——删掉 marker 检查后结果不变，
    变异**无法被捕获**。

    真实场景里微软会用 400 配 `invalid_grant`（永久），也会用 400 配
    `invalid_request`（我们拼错了 scope，换 scope 可能可行）。两者都靠
    **错误码**区分，不能靠状态码。所以必须有「同一状态、不同错误码」的对照。
    """

    def _probe(self, mock_post, status: int, body: str) -> dict:
        mock_post.return_value = _FakeResponse(status, payload={}, text=body)
        mb = OutlookMailbox(token_endpoint="https://token.example.test")
        return mb.probe_oauth_availability(
            email="d@outlook.com", client_id="cid", refresh_token="rt",
        )

    @mock.patch("requests.post")
    def test_same_status_different_codes_are_classified_differently(self, mock_post):
        permanent = self._probe(mock_post, 400, '{"error":"invalid_grant"}')
        self.assertFalse(
            permanent.get("retryable"),
            "400 + invalid_grant 被判成可重试 —— 会无限重试失效凭据",
        )
        # 同一状态、非永久错误码 → 可重试（换 scope/端点有可能成功）
        other = self._probe(mock_post, 400, '{"error":"invalid_request"}')
        self.assertTrue(
            other.get("retryable"),
            "400 + invalid_request 被判成永久 —— 换 scope 的尝试被提前放弃",
        )

    def test_classifier_reads_the_error_code_not_only_the_status(self):
        """直接钉分类函数：同状态、不同 body 必须给不同结论。"""
        from core.mailboxes.channels.outlook.mailbox import _is_permanent_oauth_error

        self.assertTrue(_is_permanent_oauth_error('{"error":"invalid_grant"}', 400))
        self.assertFalse(_is_permanent_oauth_error('{"error":"invalid_request"}', 400))
        self.assertFalse(_is_permanent_oauth_error("", 503))
        self.assertTrue(_is_permanent_oauth_error("", 401))


class PollTransientClassificationTests(unittest.TestCase):
    """Outlook 轮询必须能区分「值得再等」与「等到也没用」。

    2026-11-15（邮箱鲁棒性专项）：三个 backend 的 `_run_polling_wait` 之前
    只传 timeout/interval/poll_once，没有 `is_transient` /
    `max_consecutive_errors` —— 与 iCloud 渠道（两处都接了）不一致。
    后果是服务持续不可用时**空转到时限**，而永久错误（凭据失效）
    也陪着等，两种故障在日志里同形。
    """

    def test_network_failures_are_transient(self):
        from core.mailboxes.channels.outlook.backends import _is_transient_poll_error

        for text in ("Read timed out", "503 Service Unavailable",
                     "Connection reset by peer", "429 Too Many Requests"):
            self.assertTrue(
                _is_transient_poll_error(RuntimeError(text)),
                f"{text!r} 被判成永久失败 —— 一次抖动会终结整轮",
            )

    def test_credential_failures_are_not_transient(self):
        from core.mailboxes.channels.outlook.backends import _is_transient_poll_error

        for text in ("invalid_grant: AADSTS70000", "HTTP 401", " revoked "):
            self.assertFalse(
                _is_transient_poll_error(RuntimeError(text)),
                f"{text!r} 被判成可重试 —— 会白等到时限",
            )

    def test_retryable_attribute_wins(self):
        """带 `retryable=True` 的异常按可重试处理，不看文本。"""
        from core.mailboxes.channels.outlook.backends import _is_transient_poll_error

        class _E(Exception):
            retryable = True

        self.assertTrue(_is_transient_poll_error(_E("whatever")))

    def test_persistent_failure_gives_up_before_the_deadline(self):
        """整轮所有文件夹都读不到时，要在**时限前**放弃（错误预算生效）。

        2026-11-15 实测到的真实缺口：三个 backend 的 `poll_once` 内部
        `except ... continue` 把异常吃掉，`_run_polling_wait` 永远看不到
        —— 于是 60s 时限里干等 62s，`max_consecutive_errors=4` 形同虚设。

        这里走 IMAP 后端（无 OAuth 凭据时的默认路径），让每次读信都抛
        瞬时错误，断言**远早于**时限就结束。
        """
        from core.mailboxes.channels.outlook.backends import OutlookImapMailboxBackend

        mb = OutlookMailbox()
        mb._log = lambda *_: None
        backend = OutlookImapMailboxBackend(mb)
        account = MailboxAccount(email="d@outlook.com", extra={})

        with mock.patch.object(
            mb, "_open_imap", side_effect=RuntimeError("503 Service Unavailable")
        ):
            started = __import__("time").monotonic()
            with self.assertRaises(Exception):
                backend.wait_for_code(account, timeout=60)
            elapsed = __import__("time").monotonic() - started

        self.assertLess(
            elapsed, 40,
            f"服务持续不可用时陪跑了 {elapsed:.0f}s（时限 60s）—— 错误预算没生效",
        )
