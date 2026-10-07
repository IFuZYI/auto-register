"""sms 拆包后，模块级可变状态必须仍是「同一个对象」。

`_SMS_CACHE` 是进程内共享的号码复用缓存，测试会直接赋值重置它
（`sms_service._SMS_CACHE = None`）。若拆包后门面与实现各持一份，
「重置」只会重置其中一份，复用逻辑读到的还是脏缓存 —— 表现为
「上一个测试的号被下一个测试复用」，随机失败、极难排查。
"""
import subprocess
import sys
import unittest

from services import sms_service


class SharedStateTests(unittest.TestCase):
    def test_cache_lock_is_the_same_object(self):
        from services.sms import providers

        self.assertIs(sms_service._SMS_CACHE_LOCK, providers._SMS_CACHE_LOCK)

    def test_verify_lock_is_the_same_object(self):
        from services.sms import providers

        self.assertIs(sms_service._SMS_VERIFY_LOCK, providers._SMS_VERIFY_LOCK)

    def test_cache_file_resolves_to_the_same_function(self):
        from services.sms import providers

        self.assertIs(sms_service._cache_file, providers._cache_file)


class DirectImportOrderTests(unittest.TestCase):
    """先 import 实现包（不经门面）不能炸。

    实测（修复前）：全新进程里 `import services.sms` → `KeyError:
    'services.sms_service'` —— providers 在模块加载期经 `_facade` 读取门面
    的三个状态名，门面还没进 `sys.modules` 就炸。生产代码都走门面（先
    import `services.sms_service`）所以没暴露，但任何直接 `import
    services.sms` 的用法（新脚本、REPL 调试、静态工具）都会中招。
    """

    def test_import_services_sms_first_in_fresh_interpreter(self):
        result = subprocess.run(
            [sys.executable, "-c", "import services.sms; print('ok')"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        )
        self.assertEqual(
            result.returncode, 0,
            f"`import services.sms` 在全新进程里失败:\n{result.stderr[-1500:]}",
        )
        self.assertIn("ok", result.stdout)

    def test_import_submodule_first_in_fresh_interpreter(self):
        """直接 import 子模块（providers / controller）同样不能炸。"""
        for mod in ("services.sms.providers", "services.sms.controller"):
            result = subprocess.run(
                [sys.executable, "-c", f"import {mod}; print('ok')"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
            )
            self.assertEqual(
                result.returncode, 0,
                f"`import {mod}` 在全新进程里失败:\n{result.stderr[-1500:]}",
            )

    def test_attribute_read_after_direct_import_in_fresh_interpreter(self):
        """直接 import 后**读取**转发属性（锁 / 缓存 / _cache_file）也不能炸。

        这是真正的运行时路径：`providers._SMS_CACHE_LOCK` 经模块级
        `__getattr__`（PEP 562）转发到 `_facade`，`_facade._module` 必须
        在门面缺席时现场加载它。只测 import 的话，把 `_module` 的惰性加载
        删掉测试仍然全绿（变异验证实测 ESCAPED）—— 读一次属性才覆盖到。
        """
        code = (
            "import services.sms.providers as p;"
            "assert p._SMS_CACHE_LOCK is not None;"
            "assert p._SMS_VERIFY_LOCK is not None;"
            "assert p._cache_file is not None;"
            "import services.sms_service as f;"
            "assert p._SMS_CACHE_LOCK is f._SMS_CACHE_LOCK;"
            "print('ok')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        )
        self.assertEqual(
            result.returncode, 0,
            f"直接 import 后读转发属性失败:\n{result.stderr[-1500:]}",
        )
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
