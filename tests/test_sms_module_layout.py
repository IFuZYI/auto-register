"""sms 拆包后，模块级可变状态必须仍是「同一个对象」。

`_SMS_CACHE` 是进程内共享的号码复用缓存，测试会直接赋值重置它
（`sms_service._SMS_CACHE = None`）。若拆包后门面与实现各持一份，
「重置」只会重置其中一份，复用逻辑读到的还是脏缓存 —— 表现为
「上一个测试的号被下一个测试复用」，随机失败、极难排查。
"""
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


if __name__ == "__main__":
    unittest.main()
