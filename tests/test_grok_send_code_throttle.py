"""发码节流：两次发码之间的最小间隔（进程内全局）。

x.ai 对同一 IP 的取码有频率限制，太密会「声称已发信但不真发」，继续打会把
出口打进长冷却。间隔必须**跨任务共享**（限制按 IP 生效），所以这里的用例
既验「会等待」，也验「第二个任务不会各自从零开始计时」。
"""

import threading
import time
import unittest

import platforms.grok.plugin as plugin_mod
from platforms.grok.plugin import _throttle_send_code, _to_float


class ToFloatTests(unittest.TestCase):
    def test_parses_numbers(self):
        self.assertEqual(_to_float("240", 0.0), 240.0)
        self.assertEqual(_to_float(1.5, 0.0), 1.5)

    def test_empty_and_dirty_fall_back(self):
        for bad in ("", None, "  ", "abc", "12s"):
            self.assertEqual(_to_float(bad, 7.0), 7.0, repr(bad))


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        plugin_mod._SEND_CODE_LAST_TS = 0.0

    def tearDown(self):
        plugin_mod._SEND_CODE_LAST_TS = 0.0

    def test_first_call_does_not_wait(self):
        logs: list = []
        t0 = time.monotonic()
        _throttle_send_code(30, logs.append)
        self.assertLess(time.monotonic() - t0, 0.5, "首次发码不该等待")
        self.assertEqual(logs, [])

    def test_second_call_waits_the_remainder(self):
        logs: list = []
        _throttle_send_code(0.6, logs.append)   # 第一次：立即
        t0 = time.monotonic()
        _throttle_send_code(0.6, logs.append)   # 第二次：应等到 0.6s
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.5, f"实际只等了 {elapsed:.2f}s")
        self.assertTrue(any("发码节流" in line for line in logs), logs)

    def test_zero_interval_disables_throttle(self):
        _throttle_send_code(0, lambda _m: None)
        t0 = time.monotonic()
        _throttle_send_code(0, lambda _m: None)
        self.assertLess(time.monotonic() - t0, 0.3, "0 应表示不限速")

    def test_interval_is_shared_across_threads(self):
        """并发任务必须共享同一份计时 —— 否则限流保护形同虚设。"""
        _throttle_send_code(0.5, lambda _m: None)   # 主线程先占一次
        waited: list = []

        def worker():
            t0 = time.monotonic()
            _throttle_send_code(0.5, lambda _m: None)
            waited.append(time.monotonic() - t0)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertTrue(waited, "线程没跑到")
        self.assertGreaterEqual(waited[0], 0.3, f"另一线程未受同一间隔约束: {waited[0]:.2f}s")


if __name__ == "__main__":
    unittest.main()
