"""`core.console` 断管保护的回归测试。

背景（本机实测 2026-10-14）：`_BrokenPipeTolerantStream.__getattr__` 里写的是
`return getattr(self._stream, name)`。当 `_stream` 这个 slot 尚未写入时，读
`self._stream` 本身又会触发 `__getattr__`，于是无限递归 —— 表现不是
`RecursionError`，而是整个解释器 SIGSEGV。

触发路径：`solver_manager.start()` 在后台线程里 `print()`，pytest 捕获流在
该线程运行期间已被替换，于是整个测试进程段错误、退出码 139、跑不到一半就死。
这也是「跑全量测试必须先 `-s`」这个说法的真实原因 —— 根因在包装流，不在测试。

本模块把「半构造实例上的每个入口都不得抛/递归」逐条钉死。
"""
import io
import sys
import unittest
from pathlib import Path

sys_root = Path(__file__).resolve().parents[1]
if str(sys_root) not in sys.path:
    sys.path.insert(0, str(sys_root))

from core.console import _BrokenPipeTolerantStream, guard_stdout  # noqa: E402


def _half_built():
    """只 __new__ 不 __init__：两个 slot 都是空的，正是崩溃现场的形态。"""
    return _BrokenPipeTolerantStream.__new__(_BrokenPipeTolerantStream)


class HalfBuiltStreamTests(unittest.TestCase):
    """slot 未设时，任何入口都不能递归。"""

    def test_write_does_not_recurse(self):
        w = _half_built()
        self.assertEqual(w.write("x"), 1, "半构造实例的 write 应静默吞掉并报写满")

    def test_flush_does_not_recurse(self):
        _half_built().flush()

    def test_unknown_attribute_raises_attribute_error(self):
        with self.assertRaises(AttributeError):
            _half_built().isatty()

    def test_slot_names_themselves_raise_attribute_error(self):
        """`_stream` / `_broken` 绝不能透传 —— 透传就是递归的那一步。"""
        w = _half_built()
        for name in ("_stream", "_broken"):
            with self.assertRaises(AttributeError, msg=name):
                getattr(w, name)

    def test_write_marks_broken_so_later_writes_are_cheap(self):
        w = _half_built()
        w.write("x")
        self.assertTrue(w._broken)


class LiveStreamTests(unittest.TestCase):
    def test_writes_pass_through(self):
        sink = io.StringIO()
        w = _BrokenPipeTolerantStream(sink)
        w.write("hello")
        w.flush()
        self.assertEqual(sink.getvalue(), "hello")

    def test_attributes_proxy_to_wrapped_stream(self):
        w = _BrokenPipeTolerantStream(io.StringIO())
        self.assertFalse(w.isatty())

    def test_broken_pipe_is_swallowed_and_remembered(self):
        class _Boom:
            def __init__(self):
                self.calls = 0

            def write(self, data):
                self.calls += 1
                raise BrokenPipeError("pipe gone")

            def flush(self):
                raise BrokenPipeError("pipe gone")

        boom = _Boom()
        w = _BrokenPipeTolerantStream(boom)
        self.assertEqual(w.write("a"), 1)
        self.assertEqual(w.write("b"), 1)
        w.flush()
        self.assertEqual(boom.calls, 1, "断管后不该继续碰底层流")
        self.assertTrue(w._broken)

    def test_closed_stream_value_error_is_swallowed(self):
        class _Closed:
            def write(self, data):
                raise ValueError("I/O operation on closed file")

        w = _BrokenPipeTolerantStream(_Closed())
        self.assertEqual(w.write("a"), 1)
        self.assertTrue(w._broken)

    def test_copy_and_pickle_do_not_recurse(self):
        """`copy` / `pickle` 会在半构造状态探属性 —— 这条路径曾经必段错误。"""
        import copy
        import pickle

        w = _BrokenPipeTolerantStream(io.StringIO())
        copy.copy(w)
        pickle.dumps(w)


class GuardStdoutTests(unittest.TestCase):
    def test_guard_is_idempotent(self):
        import os

        originals = (sys.stdout, sys.stderr)
        try:
            guard_stdout()
            first = (sys.stdout, sys.stderr)
            self.assertIsInstance(first[0], _BrokenPipeTolerantStream)
            guard_stdout()  # 再装一次不该嵌套
            self.assertIs(sys.stdout, first[0], "重复调用产生了嵌套包装")
            self.assertIs(sys.stderr, first[1])
        finally:
            sys.stdout, sys.stderr = originals
        del os

    def test_guarded_stdout_survives_a_broken_pipe(self):
        originals = (sys.stdout, sys.stderr)
        try:
            class _Boom:
                def write(self, data):
                    raise BrokenPipeError("gone")

                def flush(self):
                    raise BrokenPipeError("gone")

            sys.stdout = _Boom()
            guard_stdout()
            sys.stdout.write("this must not raise")  # 不抛即通过
            sys.stdout.flush()
        finally:
            sys.stdout, sys.stderr = originals


if __name__ == "__main__":
    unittest.main()
