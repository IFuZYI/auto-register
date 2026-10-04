"""add-phone 接码循环：什么错该换号，什么错该立刻收手。

租一个号是要花钱的，把"流程状态失效"当成"这个号不行"会在几十秒里把额度烧光
（线上实测一次烧了 29 个号，每个都是秒失败的同一句报错）。
"""

import unittest

from platforms.chatgpt.protocol.auth_flow import AuthFlow


class _FakeController:
    """只实现 _do_sms_loop 用到的那几个 controller 方法。"""

    provider_key = "smsbower"

    def __init__(self, *, per_phone_timeout: int = 40, max_attempts: int = 30):
        self.config = {
            "sms_per_phone_timeout": str(per_phone_timeout),
            "sms_max_phone_attempts": str(max_attempts),
            "sms_code_retries_per_phone": "1",
        }
        self.rented: list[str] = []
        self.refunds: list[str] = []
        self.cleanups = 0

    def get_phone(self) -> str:
        phone = f"+234900000{len(self.rented):04d}"
        self.rented.append(phone)
        return phone

    def mark_send_failed(self, reason: str = "") -> None:
        self.refunds.append(reason)

    def mark_send_succeeded(self) -> None:
        pass

    def get_code(self, timeout: int = 0) -> str:
        return ""

    def report_success(self) -> None:
        pass

    def cleanup(self) -> None:
        self.cleanups += 1


def _flow(send_error: Exception) -> AuthFlow:
    """绕开 AuthFlow.__init__（要建 http 客户端），只装循环用得到的东西。"""
    flow = AuthFlow.__new__(AuthFlow)
    flow._get_env = lambda key, default="": default

    def _send(_phone: str):
        raise send_error

    flow._add_phone_send = _send
    return flow


class AddPhoneSmsLoopTests(unittest.TestCase):
    def test_flow_state_error_stops_instead_of_burning_numbers(self):
        ctrl = _FakeController(max_attempts=30)
        flow = _flow(RuntimeError("Invalid authorization step."))

        with self.assertRaises(RuntimeError):
            flow._do_sms_loop(ctrl)

        # 一个号就该收手：这错和号码无关，换号只是重复同一句报错
        self.assertEqual(len(ctrl.rented), 1)
        self.assertEqual(len(ctrl.refunds), 1)

    def test_unrecognized_error_still_tries_the_next_number(self):
        ctrl = _FakeController(max_attempts=3)
        flow = _flow(RuntimeError("upstream hiccup"))

        with self.assertRaises(RuntimeError):
            flow._do_sms_loop(ctrl)

        self.assertEqual(len(ctrl.rented), 3)
        self.assertEqual(len(ctrl.refunds), 3)

    def test_same_error_three_times_in_a_row_stops_the_round(self):
        ctrl = _FakeController(max_attempts=30)
        flow = _flow(RuntimeError("upstream hiccup"))

        with self.assertRaises(RuntimeError):
            flow._do_sms_loop(ctrl)

        self.assertEqual(len(ctrl.rented), 3)

    def test_rejected_phone_also_tries_the_next_number(self):
        ctrl = _FakeController(max_attempts=3)
        flow = _flow(RuntimeError("phone_number_already_in_use"))

        with self.assertRaises(RuntimeError):
            flow._do_sms_loop(ctrl)

        self.assertEqual(len(ctrl.rented), 3)

    def test_rate_limit_stops_the_whole_round(self):
        ctrl = _FakeController(max_attempts=30)
        flow = _flow(RuntimeError("Too many phone verification attempts"))

        with self.assertRaises(RuntimeError):
            flow._do_sms_loop(ctrl)

        self.assertEqual(len(ctrl.rented), 1)


if __name__ == "__main__":
    unittest.main()
