"""Grok 收码必须在上游持续故障时提前放弃，而不是空等满超时。

真实缺陷（审计 2026-11-14）：Grok 的 iCloud 读码路径自己写了一层 `except`，
只把异常记成 `consecutive_errors` 并继续轮询 ——
对比 `modules/mail/icloud_local.py` 的 `_run_polling_wait`（`is_transient`、
`max_consecutive_errors=4`、`is_fatal_busy`），这里没有任何短路。

后果：iCloud 网关挂掉时，每个 Grok 账号都要空等满 240 秒、烧掉一个别名领用，
日志里只显示「未收到验证码」—— 与「服务不可用」无法区分，排查方向全错。

这里只钉住可观测行为：连续上游故障时应**提前**返回空串，而不是跑满 timeout。
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from platforms.grok.register_browser import _read_code
from platforms.icloud.errors import upstream_unavailable


class GrokCodeReadCircuitTests(unittest.TestCase):
    def test_repeated_upstream_outage_gives_up_early(self):
        """连续 4 次上游故障后放弃，不等满 240 秒。"""
        calls = {"n": 0}

        def _always_outage(alias_id, limit=15):
            calls["n"] += 1
            raise upstream_unavailable("iCloud 网关 503")

        class _Service:
            fetch_alias_messages = staticmethod(_always_outage)
            renew_alias_claim = staticmethod(lambda alias_id: 1)

        start = time.monotonic()
        with patch("services.icloud_service", _Service(), create=True):
            code = _read_code(
                alias_id=1,
                # 真实等码超时就是 240 秒；熔断应在 ~40 秒（4 次 × 10 秒间隔）
                # 就收手，远早于跑满。
                timeout=240.0,
                log=lambda *_: None,
            )
        elapsed = time.monotonic() - start

        self.assertEqual(code, "", "上游不可用时应返回空串")
        self.assertLess(
            elapsed, 90.0,
            f"上游持续故障仍跑满了超时（{elapsed:.1f}s）—— "
            "每个号都会白等，且日志与「没收到码」无法区分",
        )
        self.assertLessEqual(
            calls["n"], 6,
            f"熔断前调用了 {calls['n']} 次上游；应当在少量重试后就停手",
        )

    def test_transient_blip_does_not_give_up(self):
        """偶发抖动不能熔断 —— 第 2 次就成功的场景必须正常拿到码。"""
        state = {"n": 0}

        from platforms.icloud.models import MailAddress, MailMessage

        good = MailMessage(
            provider_message_id="m1",
            mailbox="inbox",
            subject="Grok confirmation code",
            text_body="Your code is ABC-123",
            sender=MailAddress(email="noreply@x.ai"),
        )

        def _flaky(alias_id, limit=15):
            state["n"] += 1
            if state["n"] == 1:
                raise upstream_unavailable("瞬时 503")
            return [good]

        class _Service:
            fetch_alias_messages = staticmethod(_flaky)
            renew_alias_claim = staticmethod(lambda alias_id: 1)

        with patch("services.icloud_service", _Service(), create=True):
            # 轮询间隔固定 10 秒，超时给 30 秒才够跨过一次失败。
            code = _read_code(alias_id=2, timeout=30.0, log=lambda *_: None)

        self.assertEqual(code, "ABC-123", "瞬发抖动后恢复时应当能正常取到码")


if __name__ == "__main__":
    unittest.main()