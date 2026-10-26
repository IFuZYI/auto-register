"""面板请求必须校验证书，自签名要显式选择信任。

安全审查（2026-10-27）F3：9 处面板客户端**全部硬编码 `verify=False`**，
涵盖 CPA / Sub2API / chatgpt2api / grok2api / cliproxyapi 与面板对比。

后果：面板与本地之间如果有中间人（同机房、被劫持的路由、恶意代理），
攻击者出示任意证书就能收到面板 API Key **以及**推送过去的全部凭据
（AT / RT / id_token / sso，chatgpt2api 还含 password + totp_secret）。
证书校验这个控制被完全绕过。

修法：默认 `verify=True`；自签名面板由用户显式配置 CA bundle 或显式关闭校验
（`panel_tls_insecure` 开关），不把「不安全」当默认。
"""

from __future__ import annotations

import unittest
from unittest import mock


class VerifyHelperTests(unittest.TestCase):
    def setUp(self):
        from core import panel_tls

        panel_tls._reset_cache()

    def test_defaults_to_verifying(self):
        from core.panel_tls import verify_for

        with mock.patch("core.config_store.config_store.get", lambda k, d=None: ""):
            self.assertIs(verify_for("cpa"), True, "默认没校验证书")

    def test_insecure_switch_disables_verification(self):
        from core.panel_tls import verify_for

        def _get(key, default=None):
            return "1" if key == "panel_tls_insecure" else ""

        with mock.patch("core.config_store.config_store.get", _get):
            self.assertIs(verify_for("cpa"), False)

    def test_ca_bundle_is_used_when_configured(self):
        from core.panel_tls import verify_for

        def _get(key, default=None):
            return "/etc/ssl/my-ca.pem" if key == "panel_tls_ca_bundle" else ""

        with mock.patch("core.config_store.config_store.get", _get):
            self.assertEqual(verify_for("cpa"), "/etc/ssl/my-ca.pem")

    def test_broken_config_does_not_crash(self):
        """配置读取炸了也要退回「校验证书」，不能退回不安全。"""
        from core.panel_tls import verify_for

        def _boom(key, default=None):
            raise RuntimeError("config unavailable")

        with mock.patch("core.config_store.config_store.get", _boom):
            self.assertIs(verify_for("cpa"), True, "配置读取失败时退回了不校验")


class NoHardcodedInsecureVerifyTests(unittest.TestCase):
    """结构性：生产代码里不许再出现裸 `verify=False`。"""

    def test_no_bare_verify_false(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        offenders: list[str] = []
        for d in ("services", "platforms", "api", "core"):
            base = root / d
            if not base.exists():
                continue
            for path in base.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                in_docstring = False
                for lineno, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    # 跳过文档字符串/注释 —— 那里写 `verify=False` 是在**说明**
                    # 这个 bug，不是在犯它（`core/panel_tls.py` 的模块 docstring
                    # 就是这种）。
                    stripped = line.strip()
                    if stripped.count('"""') == 1:
                        in_docstring = not in_docstring
                        continue
                    if in_docstring or stripped.startswith("#"):
                        continue
                    if re.search(r"verify\s*=\s*False", line):
                        offenders.append(f"{path.relative_to(root)}:{lineno}")
        self.assertEqual(
            offenders, [],
            "仍有硬编码的 verify=False（应改用 core.panel_tls.verify_for）:\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
