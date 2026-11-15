"""韧性改动的上线顺序守卫：熔断必须在发请求**之前**生效。

为什么需要这个测试
------------------
2026-11-13 的一次改动把「轮询容错」排在「上游熔断」之前，结果被真实数据
证伪（用本仓库 `_run_polling_wait` 实测）：

    现状 fail-fast         -> 对故障上游发 1 次请求就整轮失败
    只加容错（无熔断）      -> 发 4 次请求（试探更多，上游压力 x4）
    先熔断、再容错          -> 发 2 次请求（熔断打开后不再试）

也就是说**两项改动的上线顺序会决定它们互相抵消还是互相增强**。这条约束
无法用「文档里写了」来保证 —— 一旦有人把熔断检查移到 `_request` 之后、
或从 `fetch_inbox` 路径上摘掉，请求量就会悄悄涨回去，而且不会有任何报错。

这里钉的是**行为**，不是措辞：构造一个「一直失败」的上游，断言实际发出去
的请求数被熔断压住。
"""
from __future__ import annotations

import unittest

from platforms.icloud.errors import ICloudError
from services.icloud.upstream_gate import UpstreamBusy, UpstreamGate, is_upstream_busy


class BreakerPrecedesRequestTests(unittest.TestCase):
    def test_open_breaker_prevents_the_http_call_entirely(self):
        """熔断打开时，客户端一个请求都不该发出去。"""
        from platforms.icloud.remote_client import RemoteHMEClient

        gate = UpstreamGate(threshold=1, cooldown_seconds=600)
        gate.note_failure("http://127.0.0.1:1")
        client = RemoteHMEClient("http://127.0.0.1:1", "sk-x", timeout=1, gate=gate)

        called: list[str] = []
        client._session.request = lambda *a, **kw: called.append("sent")

        with self.assertRaises(UpstreamBusy):
            client.list_accounts()
        self.assertEqual(called, [], "熔断打开时仍然发出了 HTTP 请求")

    def test_breaker_trips_after_threshold_transport_failures(self):
        """连续传输失败要把熔断打开，否则容错会无限试探。"""
        from platforms.icloud.remote_client import RemoteHMEClient

        gate = UpstreamGate(threshold=2, cooldown_seconds=600)
        client = RemoteHMEClient("http://127.0.0.1:1", "sk-x", timeout=1, gate=gate)

        import requests

        def boom(*args, **kwargs):
            raise requests.ConnectionError("down")

        client._session.request = boom

        for _ in range(2):
            with self.assertRaises(ICloudError):
                client.list_accounts()
        with self.assertRaises(UpstreamBusy):
            client.list_accounts()

    def test_workflow_stays_bounded_when_upstream_keeps_failing(self):
        """端到端：容错 + 熔断一起用时，尝试次数必须有界。

        这是那条顺序约束的可执行版本 —— 只挂容错不挂熔断时，同样的
        `_run_polling_wait` 会一直试探（直到错误预算），熔断把它压住。
        """
        from core.mailboxes.base import BaseMailbox, MailboxAccount

        gate = UpstreamGate(threshold=2, cooldown_seconds=600)
        key = "svc"

        class _Probe(BaseMailbox):
            def __init__(self):
                self.requests = 0

            def get_email(self):
                return MailboxAccount(email="a@b.c")

            def get_current_ids(self, account):
                return set()

            def wait_for_code(self, account, **kwargs):
                raise NotImplementedError

            def run(self):
                def poll_once():
                    gate.check(key)
                    self.requests += 1
                    gate.note_failure(key)
                    raise ICloudError("upstream_unavailable", "down")

                try:
                    self._run_polling_wait(
                        timeout=180,
                        poll_interval=5,
                        poll_once=poll_once,
                        is_transient=lambda exc: isinstance(
                            exc, (ICloudError, UpstreamBusy)
                        ),
                        max_consecutive_errors=8,
                        is_fatal_busy=is_upstream_busy,
                    )
                except Exception:
                    pass
                return self.requests

        import time

        probe = _Probe()
        started = time.monotonic()
        requests = probe.run()
        elapsed = time.monotonic() - started
        # 无熔断时这里会是 8（错误预算），熔断把它压到阈值以内
        self.assertLess(requests, 4, f"熔断没有压住请求量：{requests} 次")
        # 熔断打开后必须立刻放弃，而不是在冷却期里空等到 180 秒时限。
        # 前几次真实请求各自要等一个轮询间隔（5s）才轮到下一次尝试，所以
        # 「阈值 2 + 间隔 5s」下约 10 秒才触发熔断是**正确**的 —— 这里钉的是
        # 「不会空等满 180 秒」，不是「秒回」。
        self.assertLess(elapsed, 30, f"熔断打开后仍空等了 {elapsed:.1f}s（时限 180s）")


if __name__ == "__main__":
    unittest.main()