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
