"""Kiro 复用 AWS 来源账号时的进程内独占认领。

普通 iCloud 取号锁保护的是别名的领取；这里保护的是另一个资源：已有 AWS
账号的**整个登录、等码、换凭据和落库过程**。只锁查询候选会让所有 worker
同时看到同一个「尚无 Kiro 记录」的账号，随后一起登录、一起读同一封 OTP。

项目约定同一份数据只运行一个后端进程（docs/MAINTENANCE.md §1.6），所以
这里与邮箱池一样使用进程内锁，不新增跨进程租约或数据库字段。认领不是
带固定期限的租约：长任务等码多久都不会被误回收，本轮结束或进程退出即释放。
"""
from __future__ import annotations

import threading
from types import TracebackType


_SELECTION_LOCK = threading.Lock()
_CLAIMED_EMAILS: set[str] = set()

#: `task_key -> {email: 原因}`：本任务内**复用失败**过的来源。
#:
#: 为什么必须是模块级而不是 platform 实例字段：生产链路每个 attempt 都新建
#: 一次 platform（`services/task_runners.py:678`），实例字段会在下一轮被清空，
#: 「失败过的号」记不住 —— 而它正是 FIFO 每次都排第一的那个，重试等于反复
#: 撞同一堵墙（实测 2026-11-14 一个任务 55 次全撞同一个号，28 轮零成功）。
#:
#: 按任务分键的意义：失败可能只是上游抖动，不该让**别的任务**也躲开这个号，
#: 更不该写库把账号永久判死（55 个 AWS 里只剩 3 个可用，永久判死等于烧号）。
_FAILED_SOURCES: dict[str, dict[str, str]] = {}


def note_source_failure(task_key: str, email: str, reason: str = "") -> None:
    """把「这个任务试过这个来源且失败了」记下来，让本任务后续轮次换候选。"""
    key = str(task_key or "").strip()
    address = str(email or "").strip().lower()
    if not key or not address:
        return
    with _SELECTION_LOCK:
        _FAILED_SOURCES.setdefault(key, {})[address] = str(reason or "").strip() or "复用失败"


def failed_sources(task_key: str) -> dict[str, str]:
    """该任务里失败过的来源（副本，调用方可安全遍历）。"""
    key = str(task_key or "").strip()
    with _SELECTION_LOCK:
        return dict(_FAILED_SOURCES.get(key, {}))


def clear_source_failures(task_key: str) -> None:
    """清掉一个任务的失败记录（任务结束 / 测试用）。"""
    key = str(task_key or "").strip()
    with _SELECTION_LOCK:
        _FAILED_SOURCES.pop(key, None)


class SourceClaim:
    """一个邮箱的独占锁；同一 worker 内可重入，退出最外层时释放。"""

    def __init__(self, email: str) -> None:
        self.email = email
        self._depth = 0

    def acquire(self) -> bool:
        with _SELECTION_LOCK:
            if self.email in _CLAIMED_EMAILS:
                return False
            _CLAIMED_EMAILS.add(self.email)
            self._depth += 1
            return True

    def release(self) -> None:
        with _SELECTION_LOCK:
            if self._depth == 0:
                return
            self._depth -= 1
            if self._depth == 0:
                _CLAIMED_EMAILS.discard(self.email)

    def __enter__(self) -> "SourceClaim":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def try_acquire(email: str) -> SourceClaim | None:
    """尝试认领邮箱；已被其他 worker 使用时返回 None，不排队等同一账号。"""
    key = str(email or "").strip().lower()
    if not key:
        return None
    claim = SourceClaim(key)
    return claim if claim.acquire() else None


__all__ = [
    "SourceClaim",
    "clear_source_failures",
    "failed_sources",
    "note_source_failure",
    "try_acquire",
]
