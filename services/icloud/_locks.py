"""主号粒度锁与时间工具。

独立成文件的原因：`claim_alias`（aliases.py）持锁时会调 `generate_alias`
（同 aliases.py），而账号链路的其它写操作也要拿同一把锁。锁必须是
**全进程唯一**的一份，且必须是 **RLock**（可重入），否则池子捞空走
生成分支时永久自锁。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

# Apple 对每个主号限制每滚动小时最多成功生成 5 个隐私邮箱。
HOURLY_ALIAS_LIMIT = 5
DEFAULT_MESSAGE_LIMIT = 50

# 同一主号的隐私邮箱生成必须串行，否则并发注册会互相挤占小时额度。
_ACCOUNT_LOCKS: dict[int, threading.RLock] = {}
_ACCOUNT_LOCKS_GUARD = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _account_lock(account_id: int) -> threading.RLock:
    """主号粒度的串行锁。

    必须是 **RLock**（可重入）：`claim_alias` 在持锁状态下会调
    `generate_alias`，后者自己也要拿同一把锁。用 `threading.Lock` 时
    池子捞空走生成分支就永久自锁 —— 任务卡在建邮箱这一步且不报错。
    """
    with _ACCOUNT_LOCKS_GUARD:
        return _ACCOUNT_LOCKS.setdefault(int(account_id), threading.RLock())


#: 同一主号的**读信**并发上限。
#:
#: `_account_lock` 只保护取号（`claim_alias` 及其生成分支），读信完全不受它
#: 约束 —— 5 个 worker 若共用同一主号（常见：一个主号下有很多别名），
#: 每轮轮询的 `fetch_account_messages` 会叠在同一个上游服务上。
#:
#: 用 Semaphore 而不是 Lock：读信是幂等只读操作，没必要串到一次一个；
#: 限成 2 是为了「削峰」而不是「互斥」。上限取 2 是因为远程服务单账号的
#: 收件本身也是串行 IMAP/Web 调用，再多并发只会排队。
READ_CONCURRENCY_PER_ACCOUNT = 2

_READ_SEMAPHORES: dict[int, threading.Semaphore] = {}
_READ_SEMAPHORES_GUARD = threading.Lock()


def read_semaphore(account_id: int) -> threading.Semaphore:
    """取该主号的读信信号量（进程内唯一，惰性创建）。"""
    with _READ_SEMAPHORES_GUARD:
        return _READ_SEMAPHORES.setdefault(
            int(account_id), threading.Semaphore(READ_CONCURRENCY_PER_ACCOUNT)
        )


def reset_read_semaphores() -> None:
    """清空读信信号量（测试用：避免用例之间共享额度）。"""
    with _READ_SEMAPHORES_GUARD:
        _READ_SEMAPHORES.clear()
