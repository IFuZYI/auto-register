"""Solver 默认只绑本机回环。

安全审查（2026-10-27）F10：`_solver_bind_host()` 默认 `0.0.0.0`，而 solver 的
HTTP 接口（`/turnstile`、`/result`）**完全没有鉴权**。默认绑全网卡意味着：

- 任何人可让服务器用**自己的出口 IP** 打开任意 URL（`/turnstile?url=...`），
  既是 SSRF 也是匿名代理；
- 每个浏览器上下文约 250MB，打满就能逼出空闲回收，把正在跑的注册任务拖垮
  —— 一条**绕过面板登录**的拒绝服务路径。

compose 与 systemd 都把端口显式映射到 127.0.0.1，但「裸 `python main.py`」
这种启动方式会直接暴露。默认值该是安全的那个。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock


class SolverBindDefaultTests(unittest.TestCase):
    def test_default_is_loopback(self):
        from services.solver_manager import _solver_bind_host

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SOLVER_BIND_HOST", None)
            self.assertEqual(
                _solver_bind_host(), "127.0.0.1",
                "solver 默认绑 0.0.0.0 —— 无鉴权接口暴露到全网卡",
            )

    def test_explicit_override_still_works(self):
        from services.solver_manager import _solver_bind_host

        with mock.patch.dict(os.environ, {"SOLVER_BIND_HOST": "0.0.0.0"}):
            self.assertEqual(_solver_bind_host(), "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
