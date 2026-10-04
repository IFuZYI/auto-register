"""tasks 拆包后，测试依赖的三类耦合点必须原样成立。

`api/tasks.py` 里有 6 处 inspect.getsource 与 8 处 mock.patch 直接钉住函数对象，
拆文件时最容易踩的坑就是「名字还在，但 getsource 拿到的是转发壳」。
"""
import inspect
import unittest


class TaskRunnerLayoutTests(unittest.TestCase):
    def test_runners_are_importable_from_api_tasks(self):
        from api.tasks import (  # noqa: F401
            _run_account_batch_task,
            _run_backfill_rt,
            _run_bind_2fa,
            _run_register,
        )

    def test_getsource_still_sees_the_real_body(self):
        """搬走的是实现本身，不是转发壳 —— 否则源码断言全失效。"""
        from api import tasks as tasks_mod

        src = inspect.getsource(tasks_mod._run_register)
        self.assertIn('account.extra["register_proxy"] = _proxy', src)

        src_batch = inspect.getsource(tasks_mod._run_account_batch_task)
        self.assertIn("_resolve_proxy_for_account(", src_batch)
        self.assertIn("redact_proxy_url(chosen)", src_batch)

        src_fields = inspect.getsource(tasks_mod._load_account_fields)
        self.assertIn('"register_proxy"', src_fields)

    def test_patch_targets_still_resolve(self):
        """patch("api.tasks._save_task_log") 必须还能取到属性。"""
        from api import tasks as tasks_mod

        for name in (
            "_save_task_log",
            "_log",
            "_run_backfill_rt",
            "_run_bind_2fa",
            "_task_store",
            "_account_already_registered",
        ):
            self.assertTrue(hasattr(tasks_mod, name), f"api.tasks 缺 {name}")

    def test_store_io_helpers_are_still_on_api_tasks(self):
        from api import tasks as tasks_mod

        for name in (
            "_persist_task_snapshot",
            "_get_persisted_task",
            "_list_persisted_tasks",
            "_finalize_orphan_tasks",
            "_ensure_task_exists",
            "_ensure_task_mutable",
            "_get_task_snapshot",
            "_normalize_snapshot",
            "_upsert_task_run",
            "_json_dumps",
            "_json_loads",
            "_utcnow",
        ):
            self.assertTrue(hasattr(tasks_mod, name), f"api.tasks 缺 {name}")

    def test_dedup_text_markers_survive(self):
        """任务层判重标记必须在位（读 api/tasks.py 找标记，跳过逻辑在 task_runners）。"""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        src = (root / "api" / "tasks.py").read_text(encoding="utf-8")
        self.assertIn("_account_already_registered", src)
        runners = (root / "services" / "task_runners.py").read_text(encoding="utf-8")
        self.assertIn("邮箱已注册", runners)


if __name__ == "__main__":
    unittest.main()
