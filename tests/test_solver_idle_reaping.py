"""Solver 浏览器池的空闲回收。

背景（本机实测 2026-10-14）：solver 启动时按 `--thread`（默认 4）预热 4 个
camoufox，池一旦建好就永不释放 —— 实测空转 4h43m、`:8889` 上 0 个连接、
CPU 采样 0 ticks 的情况下仍占 1.0 GiB（RSS + Swap），把 1.9 GiB 的机器压到
swap 用掉 1.7 GiB。注册任务之间往往空闲数小时，这份常驻毫无收益。

这里钉死回收的全部前置条件与配套路径：TTL 判定、在跑任务时不得关池、
回收后按需补齐、补齐时不能把池撑大、关池失败不阻塞其余释放、关闭时不留孤儿。

浏览器全部用假替身，不启动真进程。
"""
import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

_SOLVER_DIR = Path(__file__).resolve().parents[1] / "services" / "turnstile_solver"
# 必须是 str：sys.path 里的 Path 对象会被 import 机制忽略，于是 `db_results`
# 这种 solver 目录内的同级模块 import 不到（api_solver 顶层就 import 它）。
if str(_SOLVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SOLVER_DIR))


def _make_server(thread=2, **env):
    """建一个不碰浏览器的 server 实例（__init__ 只建 Quart app 与配置）。"""
    from services.turnstile_solver.api_solver import TurnstileAPIServer

    with mock.patch.dict(os.environ, env, clear=False):
        return TurnstileAPIServer(
            headless=True,
            useragent=None,
            debug=False,
            browser_type="camoufox",
            thread=thread,
            proxy_support=False,
        )


def _run(coro):
    return asyncio.run(coro)


def _run_bounded(coro, timeout=5.0, what="operation"):
    """跑一个协程并加超时 —— 池被取空时 `browser_pool.get()` 会永久阻塞，
    没有超时的话「忘了补池」表现为整个测试进程挂死，而不是一条失败。"""
    async def _guarded():
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            raise AssertionError(
                f"{what} 在 {timeout}s 内没有返回：池是空的且没人补，"
                "请求会永远阻塞在 browser_pool.get()"
            )

    return asyncio.run(_guarded())


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeSession:
    """替身 AsyncCamoufox：只记 __aexit__ 有没有被调用。"""

    def __init__(self):
        self.exited = False

    async def __aexit__(self, *args):
        self.exited = True


def _seed_pool(srv, n):
    """往池里放 n 只假浏览器，返回它们的列表。"""
    browsers = [_FakeBrowser() for _ in range(n)]

    async def _seed():
        for i, b in enumerate(browsers):
            await srv.browser_pool.put((i + 1, b, {}))

    _run(_seed())
    return browsers


class EnsurePoolTests(unittest.TestCase):
    """补齐逻辑：缺多少补多少，够用就不动。"""

    def test_fills_whole_pool_when_empty(self):
        srv = _make_server(thread=3)
        calls = []

        async def _fake_init(count=None):
            calls.append(count)
            await srv.browser_pool.put((1, _FakeBrowser(), {}))

        srv._initialize_browser = _fake_init
        _run(srv._ensure_pool())
        self.assertEqual(calls, [3], "空池时应补齐 thread_count 只")

    def test_noop_when_pool_full(self):
        srv = _make_server(thread=2)

        async def _fake_init(count=None):
            raise AssertionError("池已满时不应再建浏览器")

        srv._initialize_browser = _fake_init
        _seed_pool(srv, 2)
        _run(srv._ensure_pool())  # 不应抛
        self.assertEqual(srv.browser_pool.qsize(), 2)

    def test_only_fills_the_missing_slots(self):
        """池少一只（浏览器崩了没放回）时只补一只，不重建整池。"""
        srv = _make_server(thread=4)
        calls = []

        async def _fake_init(count=None):
            calls.append(count)

        srv._initialize_browser = _fake_init
        _seed_pool(srv, 3)
        _run(srv._ensure_pool())
        self.assertEqual(calls, [1], "只缺 1 只就只补 1 只")

    def test_active_tasks_do_not_count_as_missing(self):
        """正在被任务持有的浏览器不算缺失 —— 否则并发任务会把池越补越大。"""
        srv = _make_server(thread=2)
        srv._active_tasks = 1
        calls = []

        async def _fake_init(count=None):
            calls.append(count)

        srv._initialize_browser = _fake_init
        _seed_pool(srv, 1)
        _run(srv._ensure_pool())
        self.assertEqual(calls, [], "1 只在池 + 1 只在用 = 满池，不该补")

    def test_concurrent_callers_only_initialize_once(self):
        """并发请求同时发现池空时，锁保证只拉起一次。"""
        srv = _make_server(thread=1)
        calls = []

        async def _fake_init(count=None):
            calls.append(count)
            await asyncio.sleep(0)  # 让出控制权，制造竞争窗口
            await srv.browser_pool.put((1, _FakeBrowser(), {}))

        srv._initialize_browser = _fake_init

        async def _race():
            await asyncio.gather(*(srv._ensure_pool() for _ in range(5)))

        _run(_race())
        self.assertEqual(len(calls), 1, "并发下池被重复拉起")


class ClosePoolTests(unittest.TestCase):
    def test_closes_browsers_sessions_and_clears_pool(self):
        srv = _make_server(thread=2)
        browsers = _seed_pool(srv, 2)
        sessions = [_FakeSession(), _FakeSession()]
        srv._camoufox_sessions = sessions

        _run(srv._close_pool())

        self.assertTrue(all(b.closed for b in browsers), "浏览器未关闭")
        self.assertTrue(all(s.exited for s in sessions), "playwright 连接未释放")
        self.assertEqual(srv._camoufox_sessions, [])
        self.assertTrue(srv.browser_pool.empty())

    def test_close_failure_does_not_block_the_rest(self):
        """一个浏览器关不掉不能阻止其余释放 —— 否则回收等于没做。"""
        srv = _make_server(thread=2)

        class _Bad(_FakeBrowser):
            async def close(self):
                raise RuntimeError("browser already crashed")

        _run(srv.browser_pool.put((1, _Bad(), {})))
        good = _FakeBrowser()
        _run(srv.browser_pool.put((2, good, {})))
        session = _FakeSession()
        srv._camoufox_sessions = [session]

        _run(srv._close_pool())

        self.assertTrue(good.closed)
        self.assertTrue(session.exited)
        self.assertTrue(srv.browser_pool.empty())

    def test_close_pool_on_empty_pool_is_safe(self):
        srv = _make_server(thread=2)
        _run(srv._close_pool())
        self.assertTrue(srv.browser_pool.empty())


class ReapAllowedTests(unittest.TestCase):
    """三条前置条件逐条钉死 —— 少一条就会误关正在用的池。"""

    def _idle_server(self, thread=2, ttl=300, idle_for=None):
        srv = _make_server(thread=thread, SOLVER_IDLE_TTL=str(ttl))
        srv._idle_ttl = ttl
        _seed_pool(srv, thread)
        srv._last_used_at = time.time() - (ttl + 10 if idle_for is None else idle_for)
        return srv

    def test_reaps_when_idle_past_ttl(self):
        self.assertTrue(self._idle_server()._reap_allowed())

    def test_ttl_zero_disables_reaping(self):
        self.assertFalse(self._idle_server(ttl=0)._reap_allowed())

    def test_skips_while_a_task_is_running(self):
        srv = self._idle_server()
        srv._active_tasks = 1
        self.assertFalse(srv._reap_allowed(), "任务在跑时不能关池")

    def test_skips_before_ttl_elapsed(self):
        self.assertFalse(self._idle_server(idle_for=5)._reap_allowed())

    def test_skips_when_pool_already_empty(self):
        """空池没有东西可关 —— 不挡这条会让回收协程每个间隔都报一次「已回收」。"""
        srv = _make_server(thread=2, SOLVER_IDLE_TTL="1")
        srv._idle_ttl = 1
        srv._last_used_at = time.time() - 600
        self.assertFalse(srv._reap_allowed())

    def test_no_repeat_reap_after_first_one(self):
        """回收一次之后再调不得重复关闭/重复记录。"""
        srv = _make_server(thread=1, SOLVER_IDLE_TTL="1")
        srv._idle_ttl = 1
        _seed_pool(srv, 1)
        srv._last_used_at = time.time() - 60

        self.assertTrue(_run(srv._reap_if_idle()))
        for _ in range(3):
            self.assertFalse(
                _run(srv._reap_if_idle()), "空池上重复回收（日志会每个间隔刷一次）"
            )


class ReapIfIdleTests(unittest.TestCase):
    def test_reaps_and_reports_true(self):
        srv = _make_server(thread=1, SOLVER_IDLE_TTL="1")
        srv._idle_ttl = 1
        browser = _FakeBrowser()
        session = _FakeSession()
        srv._camoufox_sessions = [session]
        _run(srv.browser_pool.put((1, browser, {})))
        srv._last_used_at = time.time() - 60

        self.assertTrue(_run(srv._reap_if_idle()))
        self.assertTrue(browser.closed)
        self.assertTrue(session.exited)
        self.assertTrue(srv.browser_pool.empty())

    def test_returns_false_and_keeps_pool_when_not_allowed(self):
        srv = _make_server(thread=1, SOLVER_IDLE_TTL="300")
        srv._idle_ttl = 300
        browser = _FakeBrowser()
        _run(srv.browser_pool.put((1, browser, {})))
        srv._last_used_at = time.time()

        self.assertFalse(_run(srv._reap_if_idle()))
        self.assertFalse(browser.closed)
        self.assertEqual(srv.browser_pool.qsize(), 1)

    def test_task_starting_mid_check_wins(self):
        """检查与关池之间来了任务：锁内第二次判定必须看到并放弃关池。"""
        srv = _make_server(thread=1, SOLVER_IDLE_TTL="1")
        srv._idle_ttl = 1
        browser = _FakeBrowser()
        _run(srv.browser_pool.put((1, browser, {})))
        srv._last_used_at = time.time() - 60

        closed = []
        original = srv._close_pool

        async def _spy():
            closed.append(1)
            await original()

        srv._close_pool = _spy
        # 第一次判定通过，进锁后第二次判定看到 active_tasks
        srv._reap_allowed = mock.Mock(side_effect=[True, False])

        self.assertFalse(_run(srv._reap_if_idle()))
        self.assertEqual(closed, [], "关池前没重新判定任务计数")
        self.assertFalse(browser.closed)


class IdleReaperLoopTests(unittest.TestCase):
    def test_start_skipped_when_ttl_zero(self):
        srv = _make_server(thread=1, SOLVER_IDLE_TTL="0")
        srv._idle_ttl = 0
        srv._start_idle_reaper()
        self.assertIsNone(srv._idle_reaper_task, "TTL=0 时不该起回收协程")

    def test_start_creates_task_when_ttl_positive(self):
        srv = _make_server(thread=1, SOLVER_IDLE_TTL="300")
        srv._idle_ttl = 300

        async def _run_start():
            srv._start_idle_reaper()
            task = srv._idle_reaper_task
            self.assertIsNotNone(task)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        _run(_run_start())

    def test_loop_calls_reap_each_interval(self):
        srv = _make_server(thread=1, SOLVER_IDLE_TTL="5")
        srv._idle_ttl = 5
        srv._idle_interval = 0
        calls = []

        async def _spy():
            calls.append(1)
            return False

        srv._reap_if_idle = _spy

        async def _run_loop():
            task = asyncio.create_task(srv._idle_reaper())
            for _ in range(50):
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        _run(_run_loop())
        self.assertGreaterEqual(len(calls), 2, "循环没有按间隔反复检查")

    def test_loop_survives_reap_errors(self):
        """一次回收报错不能让循环退出 —— 否则内存问题会在无人察觉时复发。"""
        srv = _make_server(thread=1, SOLVER_IDLE_TTL="5")
        srv._idle_ttl = 5
        srv._idle_interval = 0
        calls = []

        async def _boom():
            calls.append(1)
            raise RuntimeError("reap blew up")

        srv._reap_if_idle = _boom

        async def _run_loop():
            task = asyncio.create_task(srv._idle_reaper())
            for _ in range(50):
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        _run(_run_loop())
        self.assertGreaterEqual(len(calls), 2, "报错后循环停了")


class SolveWrapsAccountingTests(unittest.TestCase):
    """计数与时间戳必须包住整段 —— 否则回收会在任务跑的时候把池关掉。"""

    def _server_with_inner(self, inner):
        srv = _make_server(thread=1)
        srv._last_used_at = 0

        async def _ensure():
            pass

        srv._ensure_pool = _ensure
        srv._solve_turnstile_inner = inner
        return srv

    def test_active_counter_and_timestamp_bracket_inner_call(self):
        seen = {}

        async def _inner(**kwargs):
            seen["active"] = srv._active_tasks
            seen["last_used"] = srv._last_used_at

        srv = self._server_with_inner(_inner)
        _run(srv._solve_turnstile(task_id="t", url="u", sitekey="k"))

        self.assertEqual(seen["active"], 1, "任务进行中计数必须是 1")
        self.assertGreater(seen["last_used"], 0, "任务进行中要刷新空闲计时")
        self.assertEqual(srv._active_tasks, 0, "任务结束必须归还计数")

    def test_counter_released_even_when_inner_raises(self):
        async def _inner(**kwargs):
            raise RuntimeError("solve blew up")

        srv = self._server_with_inner(_inner)
        with self.assertRaises(RuntimeError):
            _run(srv._solve_turnstile(task_id="t", url="u", sitekey="k"))
        self.assertEqual(srv._active_tasks, 0, "异常路径没归还计数，回收将永久失效")

    def test_ensure_pool_runs_before_inner(self):
        order = []

        async def _inner(**kwargs):
            order.append("inner")

        srv = self._server_with_inner(_inner)

        async def _ensure():
            order.append("ensure")

        srv._ensure_pool = _ensure
        _run(srv._solve_turnstile(task_id="t", url="u", sitekey="k"))
        self.assertEqual(order, ["ensure", "inner"])

    def test_result_is_passed_through(self):
        async def _inner(**kwargs):
            return "TOKEN-1"

        srv = self._server_with_inner(_inner)
        self.assertEqual(
            _run(srv._solve_turnstile(task_id="t", url="u", sitekey="k")), "TOKEN-1"
        )


class ReapedPoolIsUsableAgainTests(unittest.TestCase):
    """回收之后再来的请求能正常解题 —— 回收不能把服务变成一次性。"""

    def test_request_after_reap_reinitializes_and_solves(self):
        srv = _make_server(thread=1, SOLVER_IDLE_TTL="1")
        srv._idle_ttl = 1
        inits = []

        async def _fake_init(count=None):
            inits.append(count)
            await srv.browser_pool.put((1, _FakeBrowser(), {}))

        srv._initialize_browser = _fake_init
        _seed_pool(srv, 1)
        srv._last_used_at = time.time() - 60
        self.assertTrue(_run(srv._reap_if_idle()))
        self.assertTrue(srv.browser_pool.empty())

        solved = []

        async def _inner(**kwargs):
            index, browser, cfg = await srv.browser_pool.get()
            solved.append(index)
            await srv.browser_pool.put((index, browser, cfg))

        srv._solve_turnstile_inner = _inner
        _run_bounded(
            srv._solve_turnstile(task_id="t", url="u", sitekey="k"),
            what="回收后的第一个请求",
        )

        self.assertEqual(inits, [1], "回收后请求没有重新拉起池")
        self.assertEqual(solved, [1])

    def test_first_ever_request_on_empty_pool_does_not_hang(self):
        """池还没建（启动预热失败/被回收）时来的请求必须自己补池，不能挂死。"""
        srv = _make_server(thread=2)
        inits = []

        async def _fake_init(count=None):
            inits.append(count)
            for i in range(count or 0):
                await srv.browser_pool.put((i + 1, _FakeBrowser(), {}))

        srv._initialize_browser = _fake_init
        solved = []

        async def _inner(**kwargs):
            index, browser, cfg = await srv.browser_pool.get()
            solved.append(index)
            await srv.browser_pool.put((index, browser, cfg))

        srv._solve_turnstile_inner = _inner
        _run_bounded(
            srv._solve_turnstile(task_id="t", url="u", sitekey="k"),
            what="空池上的第一个请求",
        )
        self.assertEqual(inits, [2], "空池上的请求没有补齐池")
        self.assertEqual(len(solved), 1)


class ShutdownTests(unittest.TestCase):
    """退出时不留孤儿：协程停掉、池关掉。"""

    def test_shutdown_cancels_reaper_and_closes_pool(self):
        srv = _make_server(thread=1)
        browser = _FakeBrowser()
        session = _FakeSession()
        srv._camoufox_sessions = [session]
        _run(srv.browser_pool.put((1, browser, {})))
        seen = {}

        async def _scenario():
            srv._idle_ttl = 300
            srv._start_idle_reaper()
            task = srv._idle_reaper_task
            await srv._shutdown()
            # 必须在**循环还活着的时候**采样：`asyncio.run` 收尾会把所有未完成的
            # 任务一并取消，退出后再看 `done()` 永远是 True —— 那样这条断言就
            # 测不出「_shutdown 没有取消协程」这个变异。
            seen["done"] = task.done()
            seen["cancelled"] = task.cancelled()

        _run(_scenario())

        self.assertTrue(seen["done"], "回收协程没被停掉，会带着池一起活过退出")
        self.assertTrue(seen["cancelled"], "回收协程应是 cancel 结束的")
        self.assertIsNone(srv._idle_reaper_task)
        self.assertTrue(browser.closed)
        self.assertTrue(session.exited)
        self.assertTrue(srv.browser_pool.empty())

    def test_shutdown_without_reaper_is_safe(self):
        srv = _make_server(thread=1)
        _run(srv._shutdown())
        self.assertTrue(srv.browser_pool.empty())


class IdleTtlParsingTests(unittest.TestCase):
    """TTL / 间隔的解析：非法值退回默认，不让服务起不来。"""

    def test_defaults(self):
        from services.turnstile_solver.api_solver import (
            DEFAULT_IDLE_INTERVAL,
            DEFAULT_IDLE_TTL,
        )

        srv = _make_server(thread=1)
        self.assertEqual(srv._idle_ttl, DEFAULT_IDLE_TTL)
        self.assertEqual(srv._idle_interval, DEFAULT_IDLE_INTERVAL)

    def test_env_override(self):
        srv = _make_server(thread=1, SOLVER_IDLE_TTL="42", SOLVER_IDLE_INTERVAL="7")
        self.assertEqual(srv._idle_ttl, 42.0)
        self.assertEqual(srv._idle_interval, 7.0)

    def test_garbage_and_negative_fall_back_to_default(self):
        from services.turnstile_solver.api_solver import DEFAULT_IDLE_TTL

        for bad in ("", "abc", "-5"):
            srv = _make_server(thread=1, SOLVER_IDLE_TTL=bad)
            self.assertEqual(srv._idle_ttl, DEFAULT_IDLE_TTL, bad)

    def test_zero_is_honoured(self):
        """0 是「关掉回收」的有效值，不能被当成非法值退回默认。"""
        srv = _make_server(thread=1, SOLVER_IDLE_TTL="0")
        self.assertEqual(srv._idle_ttl, 0.0)


class BrowserSessionIsolationTests(unittest.TestCase):
    """每个浏览器一个独立 camoufox 会话 —— 共用时 __aexit__ 只关得掉最后一个。"""

    def test_each_browser_gets_its_own_session(self):
        srv = _make_server(thread=3)
        sessions = []

        class _FakeAsyncCamoufox:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                sessions.append(self)

            async def start(self):
                return _FakeBrowser()

        import services.turnstile_solver.api_solver as mod

        with mock.patch.object(mod, "AsyncCamoufox", _FakeAsyncCamoufox):
            _run(srv._initialize_browser())

        self.assertEqual(len(sessions), 3, "每个浏览器应有一个独立会话")
        self.assertEqual(len(srv._camoufox_sessions), 3)
        self.assertEqual(srv.browser_pool.qsize(), 3)

    def test_browser_indices_are_monotonic_across_refills(self):
        """补齐池时编号不能从 1 重数 —— 会和池里已有的撞号，日志无法区分。"""
        srv = _make_server(thread=2)

        class _FakeAsyncCamoufox:
            def __init__(self, **kwargs):
                pass

            async def start(self):
                return _FakeBrowser()

        import services.turnstile_solver.api_solver as mod

        with mock.patch.object(mod, "AsyncCamoufox", _FakeAsyncCamoufox):
            _run(srv._initialize_browser(2))
            _run(srv._initialize_browser(1))

        indices = []
        while not srv.browser_pool.empty():
            idx, _b, _c = srv.browser_pool.get_nowait()
            indices.append(idx)
        self.assertEqual(len(set(indices)), len(indices), f"编号重复: {indices}")


if __name__ == "__main__":
    unittest.main()
