"""icloud 拆包后，两把「必须唯一」的东西不能各持一份。"""
import unittest


class SharedLockTests(unittest.TestCase):
    def test_account_lock_is_shared_between_claim_and_generate(self):
        """claim_alias 持锁时会调 generate_alias —— 两处必须是同一把 RLock。

        用普通 Lock 会永久自锁（池子捞空走生成分支）；用两份 RLock
        则两个线程可能同时给同一主号发号，撞 Apple 的每小时 5 个额度。
        """
        from services.icloud import _locks

        lock = _locks._account_lock(1)
        self.assertIs(lock, _locks._account_lock(1))
        # 可重入：同一线程能连拿两次
        self.assertTrue(lock.acquire(blocking=False))
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()
        lock.release()

    def test_claim_alias_calls_generate_through_the_facade(self):
        """monkeypatch 打在门面上必须生效（patch 门面 = patch 调用点）。"""
        import inspect

        from services import icloud_service

        src = inspect.getsource(icloud_service.claim_alias)
        self.assertIn("_facade.generate_alias(", src)


if __name__ == "__main__":
    unittest.main()
