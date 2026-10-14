"""控制台输出保护：stdout 管道断开时不让整条业务链陪葬。

## 为什么需要它

服务通常是被别的进程拉起来的（`... | tee`、编辑器的终端面板、CI 包装脚本、
本项目的 xvfb-run 包装）。那个进程一走，stdout 管道的**读端就消失了**，
此后每一次 `print()` 都抛 `BrokenPipeError: [Errno 32] Broken pipe`。

后果远不止「少一行日志」：注册流程的邮箱渠道、平台实现里到处都有 `print()`
做操作员提示，任务线程里任何一个 print 抛异常，都会被上层
`except Exception` 当成「注册失败」——一次纯粹的输出问题，被记成一次真实的
注册失败，还可能白白消耗一个邮箱号。

最严重的一处是任务收尾：`_log()` 自己就是 print，异常处理器里再调 `_log()`
就再抛一次，`finish()` 被整个跳过 —— 任务永远停在 `running`，前端上看着像
卡住，DB 里也收不了尾（实测过：任务创建后 67ms 死亡，日志只有两行）。

## 做法

`guard_stdout()` 把 `sys.stdout`/`sys.stderr` 换成容错包装：第一次写失败后
记下 `_broken`，之后所有写入直接丢弃，既不抛异常也不再进异常路径。任务日志
本来就有内存与 DB 副本，stdout 只是给操作员看的控制台输出，不值得为它把任务
打断。

**不动底层 fd**：早先的版本在写失败时 `os.dup2` 把 fd 接到 /dev/null，运行期
看着更彻底，但会踩到别人的输出栈 —— pytest 的捕获、日志 handler 都持有自己的
引用，dup2 会让它们在收尾时报 `[Errno 9] Bad file descriptor`。包装流自己的
`_broken` 标记已经足够：写入根本不再下发到那个 fd。

只在服务入口（`main.py`）调用一次；测试环境不装，免得把真实错误也吞掉。
"""

from __future__ import annotations

import sys
from typing import Any


def safe_print(text: str) -> None:
    """写 stdout；管道断开时静默丢弃，绝不抛异常。

    装了 `guard_stdout()` 之后这条 except 基本不会命中（包装流自己就吞掉了），
    它是给没装保护的场合兜底的 —— 例如只 import 了某个模块的测试或脚本。
    """
    try:
        print(text)
    except (OSError, ValueError):
        # BrokenPipeError 是 OSError 的子类；已关闭的流可能抛 ValueError。
        pass


class _BrokenPipeTolerantStream:
    """代理原始流，把 BrokenPipeError 变成静默丢弃。

    `__slots__` 的两个属性都可能**读不到**：实例可能被 `copy`/`__reduce_ex__`
    在半构造状态下取属性（`object.__reduce_ex__` 会探测 `__getstate__` 之类），
    此时 `_stream` 尚未写入 slot。因此所有访问都走 `_slot()`，取不到就当成
    「已断管」处理 —— 本模块的契约就是绝不抛异常。
    """

    __slots__ = ("_stream", "_broken")

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._broken = False

    def _slot(self, name: str) -> Any:
        """读 slot，读不到返回 None。

        直接写 `self._stream` 会在 slot 未设时触发 `__getattr__`，而
        `__getattr__` 内部又要读 `self._stream` —— 无限递归到段错误
        （实测：solver 的启动线程写 stdout，整个 pytest 进程 SIGSEGV）。
        走 `object.__getattribute__` 就绕开了 `__getattr__`。
        """
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            return None

    def write(self, data: str) -> int:
        if self._slot("_broken"):
            # 已断管：直接报告「写完了」，不再碰底层流，也不再付异常开销。
            return len(data)
        stream = self._slot("_stream")
        if stream is None:
            # 半构造状态：没有可写的流，按断管处理（绝不抛异常）。
            self._mark_broken()
            return len(data)
        try:
            return stream.write(data)
        except (BrokenPipeError, OSError, ValueError):
            self._mark_broken()
            return len(data)

    def _mark_broken(self) -> None:
        try:
            object.__setattr__(self, "_broken", True)
        except AttributeError:
            pass

    def flush(self) -> None:
        if self._slot("_broken"):
            return
        stream = self._slot("_stream")
        if stream is None:
            self._mark_broken()
            return
        try:
            stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            self._mark_broken()

    def __getattr__(self, name: str) -> Any:
        # 两个 slot 名绝不能透传：slot 未设时 `getattr(self._stream, ...)` 会再
        # 触发一次 `__getattr__`，无限递归（见 `_slot` 的说明）。
        if name in ("_stream", "_broken"):
            raise AttributeError(name)
        stream = self._slot("_stream")
        if stream is None:
            raise AttributeError(name)
        return getattr(stream, name)


def guard_stdout() -> None:
    """给 sys.stdout/sys.stderr 装上断管保护（幂等）。"""
    for attr in ("stdout", "stderr"):
        stream = getattr(sys, attr, None)
        if stream is None or isinstance(stream, _BrokenPipeTolerantStream):
            continue
        setattr(sys, attr, _BrokenPipeTolerantStream(stream))


__all__ = ["guard_stdout", "safe_print"]
