"""缓存并发行为：单飞（single-flight）与命中。

回归：缓存未命中时锁在重建前就被放掉，N 个并发请求会各跑一遍远端抓取
（grok2api 实测 300–550ms），最后互相覆盖缓存。面板管理页开两个标签、
或连点两次「刷新」就能触发。
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from services.panel_comparison_cache import clear_cache, get_panel_comparison


def _fake_local_rows():
    return [{"id": 1, "email": "a@example.com", "status": "registered",
             "updated_at": None, "extra": {}}]


class SingleFlightTests(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def tearDown(self):
        clear_cache()

    def _run_concurrent(self, n=6, delay=0.35):
        """N 个线程同时请求同一个面板，返回 (fetcher 调用次数, 各自结果)。"""
        calls = {"n": 0}
        lock = threading.Lock()

        def slow_fetch(**_kwargs):
            with lock:
                calls["n"] += 1
            time.sleep(delay)  # 模拟远端往返
            return []

        results: list = [None] * n
        start = threading.Barrier(n)

        def worker(i):
            start.wait()  # 尽量同时起跑
            results[i] = get_panel_comparison("cpa")

        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=_fake_local_rows(),
        ), mock.patch(
            "services.panel_comparison_cache._panel_credentials",
            return_value={"api_url": "http://cpa.test", "api_key": "k"},
        ), mock.patch(
            "services.panel_comparison_cache.FETCHERS",
            {"cpa": slow_fetch},
        ):
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        return calls["n"], results

    def test_concurrent_misses_trigger_exactly_one_fetch(self):
        """并发未命中只应触发一次远端抓取（其余等锁吃结果）。"""
        fetch_count, results = self._run_concurrent(n=6)

        self.assertEqual(
            fetch_count, 1,
            f"6 个并发请求触发了 {fetch_count} 次远端抓取 —— 单飞失效，"
            f"每个请求都白跑了一遍远端",
        )
        for r in results:
            self.assertIsNotNone(r, "有请求没拿到结果")
            self.assertEqual(r["panel"], "cpa")

    def test_waiters_get_the_fresh_result_not_a_stale_one(self):
        """等锁的请求拿到的应是刚重建的数据（不是空/旧缓存）。"""
        fetch_count, results = self._run_concurrent(n=4)
        self.assertEqual(fetch_count, 1)
        # 每个结果都带本地行数据 → 确实走了重建路径而不是拿到空 payload
        self.assertTrue(all(r["local_count"] == 1 for r in results))

    def test_hit_after_rebuild_does_not_refetch(self):
        """重建完之后的请求命中缓存，不再打远端。"""
        calls = {"n": 0}

        def fetch(**_kwargs):
            calls["n"] += 1
            return []

        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=_fake_local_rows(),
        ), mock.patch(
            "services.panel_comparison_cache._panel_credentials",
            return_value={"api_url": "http://cpa.test", "api_key": "k"},
        ), mock.patch(
            "services.panel_comparison_cache.FETCHERS",
            {"cpa": fetch},
        ):
            first = get_panel_comparison("cpa")
            second = get_panel_comparison("cpa")

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(calls["n"], 1)

    def test_refresh_still_bypasses_cache_even_with_single_flight(self):
        """`refresh=1`（同步到最新）必须真的重拉，不能被单飞吃掉。"""
        calls = {"n": 0}

        def fetch(**_kwargs):
            calls["n"] += 1
            return []

        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=_fake_local_rows(),
        ), mock.patch(
            "services.panel_comparison_cache._panel_credentials",
            return_value={"api_url": "http://cpa.test", "api_key": "k"},
        ), mock.patch(
            "services.panel_comparison_cache.FETCHERS",
            {"cpa": fetch},
        ):
            get_panel_comparison("cpa")
            refreshed = get_panel_comparison("cpa", refresh=True)

        self.assertEqual(calls["n"], 2)
        self.assertFalse(refreshed["cached"])

    def test_different_panels_do_not_block_each_other(self):
        """不同面板各有各的重建锁，不该互相串行。"""
        order: list[str] = []

        def make_fetch(name, delay):
            def _fetch(**_kwargs):
                time.sleep(delay)
                order.append(name)
                return []
            return _fetch

        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=_fake_local_rows(),
        ), mock.patch(
            "services.panel_comparison_cache._panel_credentials",
            return_value={"api_url": "http://p.test", "api_key": "k"},
        ), mock.patch(
            "services.panel_comparison_cache.FETCHERS",
            {"cpa": make_fetch("cpa", 0.3), "sub2api": make_fetch("sub2api", 0.05)},
        ):
            t1 = threading.Thread(target=get_panel_comparison, args=("cpa",))
            t2 = threading.Thread(target=get_panel_comparison, args=("sub2api",))
            t0 = time.monotonic()
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
            elapsed = time.monotonic() - t0

        # 并行的话总时长接近较慢那个（0.3s）；串行会是 0.35s+
        self.assertLess(elapsed, 0.6, f"两个面板被串行化了（{elapsed:.2f}s）")
        # sub2api 更快，应先完成
        self.assertEqual(order[0], "sub2api")


if __name__ == "__main__":
    unittest.main()
