"""远程收件的请求量控制：摘要缓存 + 主号级读信限流。

为什么需要
----------
`fetch_remote_messages` 每轮都要 `GET /api/inbox`；若列表里有 `preview` 为空
的邮件，还要再 `POST /api/inbox/previews` 批量补一次。实测一个 180 秒窗口、
5 并发的情况下，健康态 QPS 从 0.83 直接翻到 1.56 —— 翻倍的成本只为反复
补同一批邮件的摘要。

同时，`_account_lock` 只保护取号（`claim_alias`），读信完全没有限流：
5 个 worker 若共用同一主号，收件请求会叠在同一服务上。
"""
from __future__ import annotations

import threading
import unittest

from tests.remote_hme_double import API_KEY


class PreviewCacheTests(unittest.TestCase):
    """同一个 message_id 的摘要只补一次。"""

    def _client(self):
        from tests.remote_hme_double import API_KEY, RemoteHMEDouble
        from platforms.icloud.remote_client import RemoteHMEClient

        double = RemoteHMEDouble()
        double.start()
        self.addCleanup(double.stop)
        client = RemoteHMEClient(double.base_url, API_KEY, timeout=5, use_gate=False)
        return double, client

    def test_previews_are_cached_across_calls(self):
        from unittest import mock

        double, client = self._client()
        double.inbox_messages["acc_1"] = [
            {"id": "1", "from": "a@b.c", "to": "x@icloud.com", "subject": "s",
             "date": "2026-10-09T12:00:00Z", "preview": "", "folder": "INBOX"},
        ]
        from services.icloud.remote import clear_preview_cache, fetch_remote_messages

        clear_preview_cache()
        row = _row("acc_1", base_url=double.base_url)
        with mock.patch(
            "services.icloud.remote.remote_api_key", return_value=API_KEY
        ):
            fetch_remote_messages(row, limit=20, recipient="x@icloud.com")
            posts_first = _count_preview_calls(double)
            fetch_remote_messages(row, limit=20, recipient="x@icloud.com")
            posts_second = _count_preview_calls(double)
        clear_preview_cache()

        self.assertEqual(posts_first, 1, "第一轮应该补一次")
        self.assertEqual(
            posts_second, 1, f"第二轮又补了一次（共 {posts_second} 次），摘要没有缓存"
        )

    def test_cache_does_not_leak_between_services(self):
        """两个不同的远程服务各自的 message_id 可能重号，缓存不能串。"""
        from services.icloud.remote import _PREVIEW_CACHE, clear_preview_cache

        clear_preview_cache()
        _PREVIEW_CACHE.set("svc-a", "1", "验证码 111111")
        _PREVIEW_CACHE.set("svc-b", "1", "验证码 222222")
        self.assertEqual(_PREVIEW_CACHE.get("svc-a", "1"), "验证码 111111")
        self.assertEqual(_PREVIEW_CACHE.get("svc-b", "1"), "验证码 222222")
        clear_preview_cache()

    def test_cache_is_bounded(self):
        from services.icloud.remote import _PREVIEW_CACHE, clear_preview_cache

        clear_preview_cache()
        limit = _PREVIEW_CACHE.max_entries
        for index in range(limit + 50):
            _PREVIEW_CACHE.set("svc", str(index), f"code-{index}")
        self.assertLessEqual(len(_PREVIEW_CACHE), limit)
        # 最新的还在
        self.assertEqual(_PREVIEW_CACHE.get("svc", str(limit + 49)), f"code-{limit + 49}")
        clear_preview_cache()

    def test_empty_preview_is_cached_as_sentinel(self):
        """服务端也拿不到摘要时也要记住，否则每轮都重问那封解析不出的信。"""
        from services.icloud.remote import _PREVIEW_CACHE, clear_preview_cache

        clear_preview_cache()
        self.assertEqual(_PREVIEW_CACHE.get("svc", "9"), "", "没问过应为空串")
        _PREVIEW_CACHE.set("svc", "9", "")
        self.assertEqual(
            _PREVIEW_CACHE.get("svc", "9"),
            _PREVIEW_CACHE.EMPTY,
            "问过但没有摘要应记成哨兵，才能跳过重问",
        )
        clear_preview_cache()

    def test_sentinel_never_reaches_message_body(self):
        """哨兵值不能出现在邮件的 preview 里（那是验证码正文的位置）。"""
        from unittest import mock

        double, client = self._client()
        double.inbox_messages["acc_1"] = [
            {"id": "1", "from": "a@b.c", "to": "x@icloud.com", "subject": "s",
             "date": "2026-10-09T12:00:00Z", "preview": "", "folder": "INBOX"},
        ]
        from services.icloud.remote import (
            _PREVIEW_CACHE,
            clear_preview_cache,
            fetch_remote_messages,
        )

        clear_preview_cache()
        # 预先写入「问过但没有摘要」
        service = double.base_url
        _PREVIEW_CACHE.set(service, "1", "")
        row = _row("acc_1", base_url=service)
        with mock.patch("services.icloud.remote.remote_api_key", return_value=API_KEY):
            messages, _, _ = fetch_remote_messages(row, limit=20, recipient="x@icloud.com")
        clear_preview_cache()

        bodies = [getattr(m, "preview", "") for m in messages]
        self.assertNotIn(_PREVIEW_CACHE.EMPTY, bodies, "哨兵值漏进了邮件正文")
        # 而且不该再问一次服务端
        self.assertEqual(_count_preview_calls(double), 0, "已知无摘要的信又被重问了")


class ReadSemaphoreTests(unittest.TestCase):
    """同一主号的读信请求要限流，避免 5 个 worker 叠在同一服务上。"""

    def test_semaphore_is_per_account(self):
        from services.icloud._locks import read_semaphore, reset_read_semaphores

        reset_read_semaphores()
        first = read_semaphore(1)
        again = read_semaphore(1)
        other = read_semaphore(2)
        self.assertIs(first, again, "同一主号必须是同一个信号量")
        self.assertIsNot(other, first, "不同主号不该共用一个信号量")

    def test_semaphore_limits_concurrency(self):
        from services.icloud._locks import read_semaphore, reset_read_semaphores

        reset_read_semaphores()
        sem = read_semaphore(7)
        self.assertEqual(sem._value, 2, f"读信并发上限应为 2，实际 {sem._value}")

    def test_semaphore_is_reentrant_safe_for_nested_reads(self):
        """嵌套取信号量不能自锁 —— 用有上限的计数而不是锁。"""
        from services.icloud._locks import read_semaphore, reset_read_semaphores

        reset_read_semaphores()
        sem = read_semaphore(3)
        acquired = []
        for _ in range(2):
            self.assertTrue(sem.acquire(timeout=1))
            acquired.append(True)
        for _ in acquired:
            sem.release()

    def test_waiting_threads_do_not_deadlock(self):
        from services.icloud._locks import read_semaphore, reset_read_semaphores

        reset_read_semaphores()
        sem = read_semaphore(9)
        order: list[str] = []

        def worker(name):
            with sem:
                order.append(name)

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(order), 6)


def _row(account_id: str, base_url: str = ""):
    """最小 ICloudAccountModel 替身。

    `remote_api_key()` 走 `remote_password_cipher` 解密，这里直接给一段
    `secret_box` 能解的密文太脆 —— 测试改 patch `remote_api_key` 更清楚。
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        id=1,
        email="owner@icloud.com",
        source="remote",
        base_url=base_url,
        remote_account_id=account_id,
        proxy="",
        remote_password_cipher="",
    )


def _count_preview_calls(double) -> int:
    return sum(1 for _, path, _ in double.requests if path == "/api/inbox/previews")


if __name__ == "__main__":
    unittest.main()