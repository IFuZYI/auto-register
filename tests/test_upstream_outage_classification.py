"""服务故障 vs 账号问题的区分：前者不该烧重试轮次。

问题
----
`task_runners` 只把「环境错误」（浏览器/Node 缺失）判成不可重试。但
真实日志里占比最高的失败是**上游服务不可达**（实测两批任务里约 80% 是
`icloud-hme` 读超时）。这种失败重开一轮只是再等一次超时：

- 消耗掉一个邮箱别名（本轮已经领了）；
- 消耗掉重试轮次；
- 结果完全一样（服务还是挂的）。

正确做法是把「服务不可用」和「这个号有问题」分开：前者应该停止整批并
提示用户，后者才值得换号重试。
"""
from __future__ import annotations

import unittest


class UpstreamOutageClassificationTests(unittest.TestCase):
    def test_remote_read_timeout_is_an_outage(self):
        from platforms.icloud.errors import ICloudError, is_upstream_outage

        exc = ICloudError(
            "upstream_unavailable",
            "iCloud 远程服务不可达：Read timed out. (read timeout=30)",
        )
        self.assertTrue(is_upstream_outage(exc))

    def test_connection_refused_is_an_outage(self):
        from platforms.icloud.errors import ICloudError, is_upstream_outage

        exc = ICloudError("upstream_unavailable", "HTTPSConnectionPool: Connection refused")
        self.assertTrue(is_upstream_outage(exc))

    def test_breaker_rejection_is_an_outage(self):
        from services.icloud.upstream_gate import UpstreamBusy
        from platforms.icloud.errors import is_upstream_outage

        self.assertTrue(is_upstream_outage(UpstreamBusy("http://x", 12.5)))

    def test_wrong_mailbox_is_not_an_outage(self):
        """「没收到码」是这个号/这封信的问题，值得换号重试 —— 别判成服务故障。"""
        from platforms.icloud.errors import ICloudError, is_upstream_outage

        self.assertFalse(is_upstream_outage(ICloudError("otp_timeout", "180 秒内没收到验证码")))
        self.assertFalse(is_upstream_outage(TimeoutError("等码超时")))

    def test_rate_limit_is_not_an_outage(self):
        """额度用完是「主号暂时不能建别名」，和「服务读不了信」不是一回事。"""
        from platforms.icloud.errors import ICloudError, is_upstream_outage

        self.assertFalse(is_upstream_outage(ICloudError("provider_rate_limited", "每小时 5 个")))

    def test_plain_exception_is_not_an_outage(self):
        from platforms.icloud.errors import is_upstream_outage

        self.assertFalse(is_upstream_outage(ValueError("boom")))


class AttemptClassificationTests(unittest.TestCase):
    """task_runners 的 `non_retryable` 必须包含服务故障。"""

    def test_outage_marks_attempt_non_retryable(self):
        import inspect

        from services import task_runners

        source = inspect.getsource(task_runners)
        self.assertIn(
            "is_upstream_outage",
            source,
            "task_runners 没有把服务故障判成不可重试，故障期间会白烧重试轮次",
        )


if __name__ == "__main__":
    unittest.main()