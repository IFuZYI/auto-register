"""取号的互斥必须**有守卫**：读-改-写全程在锁内。

2026-11-04 独立审查（切片 F）实测：把 `claim_alias` 里的

    with _facade._account_lock(account_id):

改成 `if True:`（等价于删掉互斥），跑相关 61 条测试**全绿** ——
也就是说「同一个别名不会被两个线程领到」这个不变量，在整个套件里没有任何守卫。

单进程内 RLock 之外的代码路径本来就是对的（仓储有 `update_where` 条件更新原语
但那一步没走它），所以线上没炸；但这条不变量一旦被删，症状是**两个注册任务
拿到同一个隐私邮箱**，两边都会失败且日志里看不出原因 —— 值得钉住。

设计说明（为什么不用「起两个线程碰运气」）：并发缺陷靠调度窗口，测试要么偶发
要么白跑。这里用**确定性的「何时被求值」**判定 —— 假锁记录自己何时被持有，
假候选查询记录「我读池子时锁在不在」。删掉锁或换成 `if True:` 都会立刻转红。
真实并发的冒烟放在 `tests/test_icloud_alias_pool.py`（那边才有能种号池的
`icloud_db` 夹具）—— 夹具不跨文件，写在这里只会永远 skip，等于没跑。
"""

from __future__ import annotations

import unittest
from unittest import mock


class ClaimMustHoldTheAccountLockTests(unittest.TestCase):
    """`claim_alias` 的读-改-写必须在主号锁内完成。"""

    @classmethod
    def setUpClass(cls):
        # 门面 `_facade` 是「延迟解析」的：它按 `sys.modules["services.icloud_service"]`
        # 找调用点，所以必须先把这个模块导进来，否则 `_facade` 自己会 KeyError。
        import services.icloud_service  # noqa: F401

    class _RecordingLock:
        def __init__(self):
            self.held = False

        def __enter__(self):
            self.held = True
            return self

        def __exit__(self, *exc):
            self.held = False
            return False

    def test_the_pool_read_happens_while_the_lock_is_held(self):
        from core.db.icloud_alias_repository import icloud_alias_repository
        import services.icloud_service as icloud_service
        from services.icloud import aliases

        # 打在**门面真正解析的那个名字**上：`_facade` 是 `__getattr__` 代理，
        # 它按 `sys.modules["services.icloud_service"]` 取属性 ——
        # 对代理本身用 `patch.object` 会报「没有这个属性」。
        lock = self._RecordingLock()
        seen: list[bool] = []
        real_find = icloud_alias_repository.find_candidates

        def _spy(**kwargs):
            seen.append(lock.held)
            return real_find(**kwargs)

        class _Row:
            enabled = True
            email = "main-account@example.com"

        with mock.patch.object(icloud_service, "_account_lock", lambda _aid: lock), \
                mock.patch.object(aliases, "get_account", lambda _aid: _Row()), \
                mock.patch.object(icloud_alias_repository, "find_candidates", _spy):
            # 账号不存在也没关系：只要它**进过**锁并且读过池子就够了；
            # 真读到了就往下走，读不到抛的错也不影响「何时被求值」这个断言。
            try:
                aliases.claim_alias(1)
            except Exception:  # noqa: BLE001 - 这条测试只关心锁的持有区间
                pass

        self.assertTrue(seen, "claim_alias 必须去读号池（否则这条测试什么都没测到）")
        self.assertEqual(
            seen, [True],
            "读池子时主号锁**必须**已被持有 —— 否则两个线程会读到同一份快照并双发同一个别名",
        )
