"""远程 icloud-hme 客户端：API 秘钥鉴权、错误码映射、数据接口。

测试策略（按 TDD 技能「Doubles for external services」条）：
- 双端是**真实 HTTP 服务**（见 `tests/remote_hme_double.py`），不是 in-process
  mock —— 客户端如果没把请求发到双端（比如硬编码了生产地址），这里会直接
  连接失败，而不是拿到一句看似合理的上游报错。
- 双端**只认 `X-API-Key`**：缺头或值不对回 `401 INVALID_API_KEY`；客户端
  必须每个请求都带秘钥，且不再有 `/api/auth/login`、Cookie、CSRF 那套东西。
"""
from __future__ import annotations

import pytest

from platforms.icloud.errors import ICloudError
from platforms.icloud.remote_client import RemoteHMEClient
from tests.remote_hme_double import API_KEY, RemoteHMEDouble


@pytest.fixture(autouse=True)
def _isolated_gate():
    """每个测试用自己的熔断器。

    熔断是进程内单例，测试之间共享会让「上一个测试把 127.0.0.1:1 判成挂了」
    传染给下一个测试 —— 表现为莫名其妙的 UpstreamBusy 而不是真实结果。
    生产代码要的正是这种跨 worker 共享，测试要的恰恰相反。
    """
    from services.icloud.upstream_gate import UpstreamGate

    gate = UpstreamGate()
    yield gate
    gate.reset()


@pytest.fixture
def remote():
    double = RemoteHMEDouble()
    double.start()
    try:
        yield double
    finally:
        double.stop()


@pytest.fixture
def client(remote, _isolated_gate):
    return RemoteHMEClient(remote.base_url, API_KEY, timeout=5, gate=_isolated_gate)


# ------------------------------------------------------------------ 鉴权


class TestApiKeyAuth:
    def test_key_is_sent_on_every_request(self, remote, client):
        client.list_accounts()
        client.list_aliases("acc_1")
        assert remote.seen_keys == [API_KEY, API_KEY]

    def test_no_login_request_is_made(self, remote, client):
        client.create_alias("acc_1", "GitHub")
        assert all(path != "/api/auth/login" for _, path, _ in remote.requests)

    def test_bad_key_maps_to_invalid_config(self, remote):
        bad = RemoteHMEClient(remote.base_url, "sk-wrong-key", timeout=5)
        with pytest.raises(ICloudError) as excinfo:
            bad.list_accounts()
        assert excinfo.value.code == "invalid_config"

    def test_missing_key_fails_before_sending(self, remote):
        empty = RemoteHMEClient(remote.base_url, "", timeout=5)
        with pytest.raises(ICloudError) as excinfo:
            empty.list_accounts()
        assert excinfo.value.code == "invalid_config"
        assert remote.requests == []

    def test_unreachable_service_maps_to_upstream_unavailable(self):
        dead = RemoteHMEClient("http://127.0.0.1:1", API_KEY, timeout=1)
        with pytest.raises(ICloudError) as excinfo:
            dead.list_accounts()
        assert excinfo.value.code == "upstream_unavailable"

    def test_empty_base_url_fails_before_sending(self, remote):
        blank = RemoteHMEClient("", API_KEY, timeout=5)
        with pytest.raises(ICloudError) as excinfo:
            blank.list_accounts()
        assert excinfo.value.code == "invalid_config"
        assert remote.requests == []


class _ErrorResponse:
    """固定返回一个错误码的最小响应对象（钉住客户端的错误码映射表）。"""

    def __init__(self, code: str, status_code: int = 429) -> None:
        self.status_code = status_code
        self._code = code

    def json(self):
        return {"success": False, "code": self._code, "message": "上游拒绝"}


class TestErrorCodeMap:
    """`_ERROR_CODE_MAP` 的每条服务端错误码都必须映射到既定语义。

    这层映射是「上游说什么 → 上层怎么处理（退避/停用/换秘钥）」的唯一依据，
    漏一条就会静默降级成 `upstream_error`（502），所以逐条钉住 —— 变异测试
    证明删掉 RATE_LIMITED / UPSTREAM_FAILURE 两条时，其余测试全绿。
    """

    @pytest.mark.parametrize(
        ("upstream", "expected"),
        [
            ("INVALID_API_KEY", "invalid_config"),
            ("PERMISSION_DENIED", "permission_denied"),
            ("ACCOUNT_NOT_ALLOWED", "account_not_allowed"),
            ("RATE_LIMITED", "provider_rate_limited"),
            ("CREATION_COOLDOWN", "provider_rate_limited"),
            ("CREATION_HOURLY_LIMIT", "provider_rate_limited"),
            ("CREATION_LIMIT_REACHED", "provider_rate_limited"),
            ("VALIDATION_ERROR", "invalid_config"),
            ("ACCOUNT_NOT_FOUND", "invalid_config"),
            ("ALIAS_NOT_FOUND", "invalid_config"),
            ("TASK_NOT_FOUND", "invalid_config"),
            ("UPSTREAM_FAILURE", "upstream_unavailable"),
            ("INTERNAL_ERROR", "upstream_unavailable"),
        ],
    )
    def test_every_mapped_code(self, upstream, expected):
        from unittest import mock

        client = RemoteHMEClient("http://hme.example", "sk-test-key", timeout=5)
        with mock.patch.object(
            client._session, "request", return_value=_ErrorResponse(upstream)
        ):
            with pytest.raises(ICloudError) as excinfo:
                client.list_accounts()
        assert excinfo.value.code == expected

    def test_unknown_code_falls_back_to_upstream_error(self):
        from unittest import mock

        client = RemoteHMEClient("http://hme.example", "sk-test-key", timeout=5)
        with mock.patch.object(
            client._session, "request", return_value=_ErrorResponse("SOMETHING_NEW")
        ):
            with pytest.raises(ICloudError) as excinfo:
                client.list_accounts()
        assert excinfo.value.code == "upstream_error"

    def test_write_requests_work_without_csrf(self, remote, client):
        client.create_alias("acc_1", "GitHub")
        client.delete_alias("acc_1", "anon-9")
        client.set_alias_active("acc_1", "anon-9", False)
        client.set_alias_active("acc_1", "anon-9", True)
        assert remote.active_calls == [("anon-9", False), ("anon-9", True)]
        assert remote.deleted_aliases == ["anon-9"]
        assert ("POST", "/api/create") in [(m, p) for m, p, _ in remote.requests]


class TestApiKeyMode:
    """2026-11-02 新鉴权：`X-API-Key`，无登录/CSRF。"""

    @staticmethod
    def _response(data):
        class _Resp:
            status_code = 200

            def json(self):
                return {"success": True, "data": data}

        return _Resp()

    def test_api_key_is_sent_and_no_login_request(self):
        from unittest import mock

        client = RemoteHMEClient("http://hme.example", "sk-test-key", timeout=5)
        with mock.patch.object(
            client._session, "request", return_value=self._response([{"id": "acc_1"}])
        ) as request:
            assert client.list_accounts()[0]["id"] == "acc_1"
        assert request.call_args.kwargs["headers"]["X-API-Key"] == API_KEY
        # 注意：`call.args[1]` 是**绝对 URL**（`http://hme.example/api/...`），
        # 直接与裸路径 `/api/auth/login` 比较永远为真 —— 这里必须比 URL 的 path，
        # 否则「不再走登录接口」这条断言是假的（变异测试证实过）。
        from urllib.parse import urlparse

        paths = [urlparse(call.args[1]).path for call in request.call_args_list]
        assert paths, "断言必须在真的发过请求时才成立"
        assert "/api/auth/login" not in paths, "API 秘钥模式不应再走登录接口"

    def test_write_request_carries_key_at_http_layer(self):
        """写操作（POST/DELETE）在 HTTP 层也必须带 X-API-Key，而不是只在读路径上。"""
        from unittest import mock

        client = RemoteHMEClient("http://hme.example", "sk-test-key", timeout=5)
        with mock.patch.object(
            client._session, "request", return_value=self._response({"success": True})
        ) as request:
            client.delete_alias("acc_1", "anon-9")
            client.create_alias("acc_1", "GitHub")
        for call in request.call_args_list:
            assert call.kwargs["headers"]["X-API-Key"] == "sk-test-key"

    def test_over_long_manual_label_is_truncated_before_send(self):
        """手动标签超过上限必须先截断，再发出去（服务端只校验、不会帮你截）。"""
        from unittest import mock

        from platforms.icloud.remote_client import MAX_LABEL_LENGTH

        client = RemoteHMEClient("http://hme.example", "sk-test-key", timeout=5)
        with mock.patch.object(
            client._session, "request", return_value=self._response({"email": "a@icloud.com"})
        ) as request:
            client.create_alias("acc_1", "x" * (MAX_LABEL_LENGTH + 50))
        sent = request.call_args.kwargs["json"]["label"]
        assert len(sent) == MAX_LABEL_LENGTH
        assert sent == "x" * MAX_LABEL_LENGTH

    def test_inbox_passes_new_pagination_and_refresh_params(self):
        from unittest import mock

        client = RemoteHMEClient("http://hme.example", "sk-test-key", timeout=5)
        with mock.patch.object(
            client._session,
            "request",
            return_value=self._response({"messages": [], "count": 0, "total": 0}),
        ) as request:
            client.fetch_inbox(
                "acc_1",
                limit=20,
                offset=40,
                days=30,
                start="2026-10-01",
                end="2026-10-31",
                refresh=True,
            )
        params = request.call_args.kwargs["params"]
        assert params["offset"] == 40
        assert params["start"] == "2026-10-01"
        assert params["end"] == "2026-10-31"
        assert params["refresh"] == 1
        assert params["days"] == 30

    def test_previews_posts_ids_and_returns_mapping(self):
        from unittest import mock

        client = RemoteHMEClient("http://hme.example", "sk-test-key", timeout=5)
        with mock.patch.object(
            client._session,
            "request",
            return_value=self._response({"1042": "code 123456"}),
        ) as request:
            out = client.fetch_previews("acc_1", ["1042", "Junk:88"])
        assert out == {"1042": "code 123456"}
        assert request.call_args.kwargs["json"] == {"ids": ["1042", "Junk:88"]}


# ------------------------------------------------------------------ 错误码映射


class TestErrorMapping:
    def test_creation_cooldown_maps_to_rate_limited(self, remote, client):
        remote.create_error = {"code": "CREATION_COOLDOWN", "message": "至少等待 20 分钟"}
        with pytest.raises(ICloudError) as excinfo:
            client.create_alias("acc_1", "GitHub")
        assert excinfo.value.code == "provider_rate_limited"

    def test_creation_daily_limit_maps_to_rate_limited(self, remote, client):
        remote.create_error = {"code": "CREATION_LIMIT_REACHED", "message": "今日上限"}
        with pytest.raises(ICloudError) as excinfo:
            client.create_alias("acc_1", "GitHub")
        assert excinfo.value.code == "provider_rate_limited"

    def test_unknown_error_code_maps_to_upstream_error(self, remote, client):
        remote.create_error = {"code": "SOMETHING_NEW", "message": "新错误"}
        with pytest.raises(ICloudError) as excinfo:
            client.create_alias("acc_1", "GitHub")
        assert excinfo.value.code == "upstream_error"


# ------------------------------------------------------------------ 数据接口


class TestDataEndpoints:
    def test_list_accounts(self, remote, client):
        accounts = client.list_accounts()
        assert accounts[0]["id"] == "acc_1"

    def test_create_alias_passes_account_and_label(self, remote, client):
        created = client.create_alias("acc_1", "GitHub")
        assert created["account_id"] == "acc_1"
        assert created["label"] == "GitHub"

    def test_create_alias_defaults_label_from_library(self, remote, client):
        """没给 label 时从远端名称库挑一个（服务端要求 label 必填）。"""
        created = client.create_alias("acc_1")
        assert created["label"] == "GitHub"

    def test_list_aliases(self, remote, client):
        remote.aliases["acc_1"] = [
            {"email": "a@icloud.com", "anonymousId": "anon-1", "label": "GitHub", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
        ]
        aliases = client.list_aliases("acc_1")
        assert aliases[0]["email"] == "a@icloud.com"

    def test_fetch_inbox(self, remote, client):
        remote.inbox_messages["acc_1"] = [
            {
                "id": "1042",
                "from": "GitHub <noreply@github.com>",
                "to": "newalias@icloud.com",
                "subject": "验证",
                "date": "2026-10-09T12:00:00Z",
                "preview": "验证码 123456",
                "folder": "INBOX",
            }
        ]
        data = client.fetch_inbox("acc_1", alias="newalias@icloud.com", limit=20)
        assert data["messages"][0]["id"] == "1042"
        assert remote.requests[-1][2].get("limit") == "20"

    def test_fetch_inbox_clamps_limit(self, remote, client):
        # 服务端 limit 上限是 50（以服务端代码为准，API.md 写的 100 是旧的）
        client.fetch_inbox("acc_1", limit=999)
        assert remote.requests[-1][2].get("limit") == "50"

    def test_fetch_message(self, remote, client):
        message = client.fetch_message("acc_1", "1042")
        assert message["body"] == "您的验证码是 123456"

    def test_check_account(self, remote, client):
        summary = client.check_account("acc_1")
        assert summary["status"] == "active"


# ------------------------------------------------------- 分层超时与连接复用


class TestLayeredTimeout:
    """读超时 30s 意味着「服务卡死」时每个 worker 白等 30s 才失败。

    建连不通（DNS 错、路由不可达）应该在几秒内失败 —— 那和「服务活着但慢」
    是两个不同的故障，用一个 30s 的单值超时会把它们混为一谈。
    """

    def test_connect_and_read_timeouts_are_separate(self):
        """建连要快、读要耐心：两者是不同故障，不能共用一个数。"""
        client = RemoteHMEClient("http://127.0.0.1:1", API_KEY, timeout=120)
        connect, read = client.timeout_pair()
        assert connect == 5, "建连超时该保持小值，否则 DNS/路由不通要白等"
        assert read > connect, "读超时不该小于建连超时"

    def test_explicit_timeout_still_feeds_both_halves(self):
        """显式给了一个小 timeout 时，两半都不能超过它 —— 否则测试会变慢。"""
        client = RemoteHMEClient("http://127.0.0.1:1", API_KEY, timeout=3)
        assert client.timeout_pair() == (3, 3)

    def test_unreachable_host_fails_within_connect_timeout(self):
        """端口 1 没有监听：建连立刻被拒，不该等满读超时。"""
        import time

        client = RemoteHMEClient("http://127.0.0.1:1", API_KEY, timeout=30)
        started = time.monotonic()
        with pytest.raises(ICloudError) as excinfo:
            client.list_accounts()
        elapsed = time.monotonic() - started
        assert excinfo.value.code == "upstream_unavailable"
        assert elapsed < 10, f"建连失败等了 {elapsed:.1f}s，说明还在用单个长超时"


class TestConnectionReuse:
    """每个 worker 每次读信都新建 TCP+TLS 连接，在 5 并发下是纯浪费。"""

    def test_session_carries_a_pooling_adapter(self):
        from requests.adapters import HTTPAdapter

        client = RemoteHMEClient("http://127.0.0.1:1", API_KEY)
        adapters = client._session.adapters
        assert adapters, "Session 没有挂 adapter"
        assert all(
            isinstance(adapter, HTTPAdapter) and adapter._pool_maxsize > 1
            for adapter in adapters.values()
        ), f"连接池大小没有放大: {adapters}"

    def test_repeated_calls_reuse_one_session(self, remote, client):
        client.list_accounts()
        client.list_accounts()
        # 同一个 Session 两次请求；连接池复用由 adapter 保证，这里钉住
        # 「没有为每次请求新建 Session 或 adapter」。
        assert len(client._session.adapters) >= 1
        assert len(remote.requests) == 2


class TestGateWiring:
    """客户端必须把「连不上」记进熔断，且在发请求前就拦住。"""

    def test_client_uses_injected_gate(self, _isolated_gate):
        client = RemoteHMEClient(
            "http://127.0.0.1:1", API_KEY, timeout=1, gate=_isolated_gate
        )
        with pytest.raises(ICloudError):
            client.list_accounts()
        assert _isolated_gate.failure_count("http://127.0.0.1:1") == 1

    def test_open_gate_blocks_before_any_request(self, remote, _isolated_gate):
        """熔断打开后不该再发请求 —— 请求次数要停在 0。"""
        from services.icloud.upstream_gate import UpstreamGate

        gate = UpstreamGate(threshold=1, cooldown_seconds=60)
        gate.note_failure(remote.base_url)
        client = RemoteHMEClient(remote.base_url, API_KEY, timeout=5, gate=gate)
        with pytest.raises(Exception) as excinfo:
            client.list_accounts()
        assert "熔断" in str(excinfo.value)
        assert remote.requests == [], "熔断打开时仍然发了请求"

    def test_success_clears_failure_count(self, remote, _isolated_gate):
        _isolated_gate.note_failure(remote.base_url)
        client = RemoteHMEClient(
            remote.base_url, API_KEY, timeout=5, gate=_isolated_gate
        )
        client.list_accounts()
        assert _isolated_gate.failure_count(remote.base_url) == 0

    def test_http_error_status_does_not_open_gate(self, remote, _isolated_gate):
        """4xx 是业务语义（秘钥错/参数错），不是「服务挂了」，不该熔断。"""
        remote.create_error = {"code": "CREATION_COOLDOWN", "message": "冷却中"}
        client = RemoteHMEClient(
            remote.base_url, API_KEY, timeout=5, gate=_isolated_gate
        )
        with pytest.raises(ICloudError):
            client.create_alias("acc_1", "GitHub")
        assert _isolated_gate.failure_count(remote.base_url) == 0

    def test_gate_can_be_disabled_for_probes(self, remote):
        """探活/测试要真实结果，不能被别的 worker 的失败记账挡住。"""
        from services.icloud.upstream_gate import UpstreamGate

        gate = UpstreamGate(threshold=1, cooldown_seconds=60)
        gate.note_failure(remote.base_url)
        client = RemoteHMEClient(remote.base_url, API_KEY, timeout=5, use_gate=False)
        assert client.list_accounts()[0]["id"] == "acc_1"


# --------------------------------------------------- 只读秘钥下的「检测」


class TestCheckWithReadOnlyKey:
    """`POST /accounts/:id/check` 要写级权限，而注册用的那把 sk- 通常只有只读档。

    2026-11-14 线上实测：库里的秘钥 `GET /api/accounts`、`/api/inbox`、
    `/api/aliases` 全部 200，只有 `/check` 回 403 `PERMISSION_DENIED` ——
    于是 iCloud 页的「检测」按钮对每个远程主号恒失败，报「API 秘钥权限不足」。
    检测是只读语义，不该因为「这把钥匙不能写」而彻底不可用。
    """

    def test_falls_back_to_read_only_probe_when_check_is_denied(self, remote, client):
        remote.check_error = {"code": "PERMISSION_DENIED", "message": "API 秘钥权限不足"}

        summary = client.check_account("acc_1")

        assert summary["status"] == "active"
        # 走的必须是读接口，而不是偷偷又打了一次 /check 然后吞掉 403
        assert ("GET", "/api/accounts") in [
            (method, path) for method, path, _ in remote.requests
        ]

    def test_denied_check_does_not_survive_as_an_error(self, remote, client):
        """降级成功就是成功 —— 不能让 permission_denied 冒到上层变成 403。"""
        remote.check_error = {"code": "PERMISSION_DENIED", "message": "API 秘钥权限不足"}
        assert client.check_account("acc_1")["id"] == "acc_1"

    def test_other_check_failures_still_raise(self, remote, client):
        """只对「权限不足」降级。上游真挂了要报错，不能一律假装正常。"""
        remote.check_error = {"code": "UPSTREAM_FAILURE", "message": "上游故障"}

        with pytest.raises(ICloudError) as ctx:
            client.check_account("acc_1")
        assert ctx.value.code == "upstream_unavailable"

    def test_read_only_probe_reports_a_missing_account(self, remote, client):
        """降级探测不是「无条件成功」：远端没有这个账号时要照样报错。"""
        remote.check_error = {"code": "PERMISSION_DENIED", "message": "API 秘钥权限不足"}

        with pytest.raises(ICloudError) as ctx:
            client.check_account("acc_missing")
        assert ctx.value.code == "invalid_config"

    def test_read_only_probe_surfaces_a_broken_service(self, remote, client):
        """降级路径自己撞上 5xx 时必须如实抛出，不能把「读不出来」当成 active。"""
        remote.check_error = {"code": "PERMISSION_DENIED", "message": "API 秘钥权限不足"}
        remote.accounts_error = {"code": "INTERNAL_ERROR", "message": "内部错误"}

        with pytest.raises(ICloudError):
            client.check_account("acc_1")


# ------------------------------------------------ 慢 ≠ 挂；一个主号 ≠ 全部主号


class TestSlowUpstreamIsNotAnOutage:
    """读超时必须比上游自己的预算长，否则「慢」会被记成「挂」。

    2026-11-14 实测：上游 IMAP 整体预算 30s（信封阶段 45s），客户端读超时
    只有 20s —— 客户端**必然**先放弃，然后把这次「上游还在正常干活」的请求
    记成一次传输失败。5 个 worker 各撞一次，熔断阈值（5）就到了。
    """

    def test_default_read_timeout_covers_the_upstream_budget(self):
        """默认客户端（生产就用这套默认值）的读超时不得短于上游自己的预算。

        上游 `imapEnvelopeBudget` 最高 45s：客户端若比它短，正常但慢的收件
        一定被客户端先判死，而这次「成功前的等待」会被记成一次上游失败。
        """
        client = RemoteHMEClient("http://127.0.0.1:1", API_KEY)
        connect, read = client.timeout_pair()
        assert read >= 45, f"读超时 {read}s 短于上游 IMAP 预算（最高 45s）"
        assert connect <= 5, "建连超时不该跟着读超时一起放大"

    def test_production_factory_does_not_shorten_the_read_timeout(self, monkeypatch):
        """`services.icloud.remote.remote_client()` 也要给足读预算。

        生产链路从不显式传 timeout，用的是工厂默认值 —— 工厂自己写死 30 的话，
        客户端常量改成 45 也白改。
        """
        from services.icloud import remote as remote_module

        monkeypatch.setattr(remote_module, "remote_api_key", lambda row: "sk-x")
        row = type(
            "Row",
            (),
            {"source": "remote", "base_url": "http://127.0.0.1:1",
             "email": "a@icloud.com", "proxy": ""},
        )()

        _, read = remote_module.remote_client(row).timeout_pair()

        assert read >= 45



class TestBreakerIsScopedPerAccount:
    """熔断按「服务」分键时，一个主号的收件箱卡死会封掉同一服务下的**所有**主号。

    2026-11-14 实测：4 个远程主号共用一个 icloud-hme，`fuzy202609` 的读请求
    超时把 `base_url` 这个键顶开，`fuzy_public` / `fuzy202610` / `fuzy202611`
    立刻全部被拒（376 行「熔断中」日志、212 次直接失败）。故障面从「一个
    信箱」放大成了「整批任务」。
    """

    def test_one_stuck_account_does_not_block_the_others(self, remote, _isolated_gate):
        # acc_1 的读请求永远等不到（客户端 1s 读超时），acc_2 正常。
        from services.icloud.upstream_gate import UpstreamBusy, UpstreamGate

        remote.slow_accounts = {"acc_1": 5.0}
        gate = UpstreamGate(threshold=2, cooldown_seconds=60)
        client = RemoteHMEClient(remote.base_url, API_KEY, timeout=1, gate=gate)

        # 撞满阈值（2 次传输失败），随后这个主号被熔断挡住
        for _ in range(2):
            with pytest.raises(ICloudError):
                client.fetch_inbox("acc_1", limit=5)
        with pytest.raises(UpstreamBusy):
            client.fetch_inbox("acc_1", limit=5)

        # 同一个服务、另一个主号：必须还能读
        assert client.fetch_inbox("acc_2", limit=5)["messages"] == []

    def test_service_level_outage_still_blocks_every_account(
        self, remote, _isolated_gate
    ):
        """按主号分键不能把「整个服务挂了」也拆散 —— 那时谁都不该再试。"""
        from services.icloud.upstream_gate import UpstreamBusy, UpstreamGate

        remote.down = True
        gate = UpstreamGate(threshold=2, cooldown_seconds=60)
        client = RemoteHMEClient(remote.base_url, API_KEY, timeout=2, gate=gate)
        for _ in range(2):
            with pytest.raises(ICloudError):
                client.fetch_inbox("acc_1", limit=5)

        # 换一个从没失败过的主号也照样被挡：服务级已经打开
        with pytest.raises(UpstreamBusy):
            client.fetch_inbox("acc_9", limit=5)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
