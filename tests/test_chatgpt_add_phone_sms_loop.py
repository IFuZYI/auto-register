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


class SmsValidateSuccessReturnTests(unittest.TestCase):
    """validate 通过后返回值的正确性（NameError 回归）。

    回归（既存 bug，模块 docstring 记载）：`_do_sms_loop` 的
    `return next_url or continue_url or ""` 中 `continue_url` 是未定义名字。
    只要 validate 通过且 `next_url` 为空（服务端校验通过但没给下一跳，
    或 next_url 提取为空），就触发 NameError → 被 except 吞掉 →
    明明验证成功却调 `mark_code_failed`（通知接码平台「码失败」）并继续
    等下一条码 —— 白耗号码窗口，流程卡死。

    修复：`_do_sms_loop` 接收 `continue_url` 参数（调用方
    `_handle_add_phone_via_sms` 本来就持有它），成功路径返回
    `next_url or continue_url or ""`。
    """

    def _flow_with_validate(self, validate_response: dict):
        """构造能跑到 validate 的 flow（send 成功、拿到码、validate 通过）。"""
        flow = AuthFlow.__new__(AuthFlow)
        flow._get_env = lambda key, default="": default

        flow._add_phone_send = lambda phone: {
            "page": {"type": "phone_otp_verification"},
            "continue_url": "https://auth.openai.com/phone-verification",
        }
        flow._phone_otp_validate = lambda code: validate_response
        return flow

    def _ctrl(self):
        ctrl = _FakeController(max_attempts=1)
        ctrl.get_code = lambda timeout=0: "123456"
        ctrl.failures: list[str] = []
        ctrl.mark_code_failed = lambda reason="": ctrl.failures.append(reason)
        ctrl.successes = 0

        def _report_success():
            ctrl.successes += 1

        ctrl.report_success = _report_success
        return ctrl

    def test_empty_next_url_returns_continue_url_not_nameerror(self):
        """validate 通过但 next_url 为空 → 返回 continue_url（不抛 NameError）。"""
        ctrl = self._ctrl()
        flow = self._flow_with_validate({})  # 响应无 continue_url → next_url 为空

        result = flow._do_sms_loop(ctrl, continue_url="https://fallback.example/next")

        self.assertEqual(
            result, "https://fallback.example/next",
            "validate 通过后应返回 continue_url 兜底值（此前 NameError 把成功吞成失败）",
        )
        self.assertEqual(ctrl.successes, 1, "report_success 应被调用")
        self.assertEqual(ctrl.failures, [], "成功路径不该报 mark_code_failed")

    def test_next_url_wins_over_continue_url(self):
        """next_url 有值时优先返回它。"""
        ctrl = self._ctrl()
        flow = self._flow_with_validate(
            {"continue_url": "https://auth.openai.com/phone-verification/done"}
        )

        result = flow._do_sms_loop(ctrl, continue_url="https://fallback.example/next")

        self.assertEqual(result, "https://auth.openai.com/phone-verification/done")
        self.assertEqual(ctrl.failures, [])

    def test_both_empty_returns_empty_string(self):
        """next_url 与 continue_url 都为空 → 返回空串（不炸）。"""
        ctrl = self._ctrl()
        flow = self._flow_with_validate({})

        result = flow._do_sms_loop(ctrl, continue_url="")

        self.assertEqual(result, "")
        self.assertEqual(ctrl.failures, [])

    def test_handler_passes_continue_url_through(self):
        """`_handle_add_phone_via_sms` 要把 continue_url 透传给 `_do_sms_loop`。"""
        flow = AuthFlow.__new__(AuthFlow)
        flow._get_env = lambda key, default="": default
        seen: dict = {}

        def _loop(ctrl, *, continue_url=""):
            seen["continue_url"] = continue_url
            return "ok"

        flow._do_sms_loop = _loop

        class _Ctrl:
            def cleanup(self):
                pass

            def _release_lock(self):
                pass

        flow._sms_callback = _Ctrl()
        result = flow._handle_add_phone_via_sms("https://keep.example/x")

        self.assertEqual(result, "ok")
        self.assertEqual(
            seen.get("continue_url"), "https://keep.example/x",
            "continue_url 没透传 —— NameError 修复不完整",
        )


if __name__ == "__main__":
    unittest.main()
