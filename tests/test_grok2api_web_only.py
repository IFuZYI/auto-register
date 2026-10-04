"""grok2api 接入只做 Web 导入：Console / Build 派生已按用户要求移除。

用户要求：「grok2api 导入只需要 grokweb，build 和 console 不需要，这两个用户
可以自己在 grok2api 中手动转换。」

移除的东西（全部只服务于派生）：

- `Grok2ApiClient.sync_to_console` / `convert_to_build` / `_derive` —— 调
  grok2api 的 `sync-to-console` / `convert-to-build` 两个派生端点；
- `Grok2ApiClient.import_build_tokens` + `_BUILD_CLIENT_ID` —— 把 OAuth token
  当 Build 凭据直接导入（那条路是为了绕开被上游加严挡住的 Device Flow 转换）；
- `ingest_sso` 的 `derive` / `tokens` 参数 —— 派生入口与它的 token 通道；
- `find_account_by_email` —— 派生时代码路径用过的不筛 provider 版本，**零调用**
  （派生移除后它彻底没有消费者）；
- `upload_grok2api` 动作的 `derive` 参数声明。

保留的（Web 导入本身与 NSFW 设置）：

- `import_sso`：上传 SSO（文件名必须是 `grok-web-sso-tokens.txt`）；
- `find_web_account_by_email`：账号级动作只对 Web 账号有效，必须按
  `provider=grok_web` 找；
- `account_setup` + `nsfw`：条款 / 生日 / NSFW 顺序不能换；
- 自动接入与手动上传两条路径。

这个模块的 docstring 里原有「派生格式 grok2api 不会自动做」之类的说明 ——
用户明确说这两类凭据他自己在 grok2api 里手动转换，所以我们不再碰。
"""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from platforms.grok.grok2api import Grok2ApiClient


def _client() -> Grok2ApiClient:
    c = Grok2ApiClient("http://g2a.test", "admin", "pw")
    c._token = "tok"
    c._token_exp = 10**12
    return c


class DerivationSurfaceRemovedTests(unittest.TestCase):
    """派生相关的公开方法与常量必须整块消失，而不是留着不用。"""

    def test_derive_methods_are_gone(self):
        for name in (
            "sync_to_console",
            "convert_to_build",
            "import_build_tokens",
            "_derive",
        ):
            self.assertFalse(
                hasattr(Grok2ApiClient, name),
                f"`{name}` 还在 —— Console/Build 派生已按用户要求移除，"
                "留着会被当成可用能力",
            )

    def test_build_client_id_constant_is_gone(self):
        from platforms.grok import grok2api as mod

        self.assertFalse(
            hasattr(mod, "_BUILD_CLIENT_ID"),
            "_BUILD_CLIENT_ID 只服务于 import_build_tokens，应随它一起删除",
        )

    def test_unscoped_find_is_gone(self):
        """`find_account_by_email`（不筛 provider）零调用，随派生一起删。

        派生被移除后它彻底没有消费者 —— 留着的话，下次有人要用「按邮箱找账号」
        会拿到同邮箱的 build/console 记录（实测 `accountPoolMismatch`）。
        """
        self.assertFalse(hasattr(Grok2ApiClient, "find_account_by_email"))
        self.assertTrue(
            hasattr(Grok2ApiClient, "find_web_account_by_email"),
            "按 Web 号池查账号是账号级动作（NSFW）的依赖，必须保留",
        )


class IngestSsoOnlyUploadsWebTests(unittest.TestCase):
    """`ingest_sso` 只做「上传 + NSFW」，不再派生。"""

    def test_ingest_sso_has_no_derive_parameters(self):
        params = inspect.signature(Grok2ApiClient.ingest_sso).parameters
        for gone in ("derive", "tokens"):
            self.assertNotIn(
                gone,
                params,
                f"`ingest_sso` 仍有 `{gone}` 参数 —— 派生入口应已删除",
            )
        self.assertIn("nsfw", params, "NSFW 设置要保留（用户没说要去掉）")

    def test_ingest_uploads_and_sets_nsfw_only(self):
        c = _client()
        calls = []

        def _fake_import_sso(sso):
            calls.append("upload")
            return True, "created=1"

        def _fake_find_web(email):
            return {"id": "41", "provider": "grok_web"}

        def _fake_setup(web_id, nsfw=True):
            calls.append("setup")
            return True, ["accept-terms", "birth-date", "nsfw"], []

        with patch.object(c, "import_sso", _fake_import_sso), patch.object(
            c, "find_web_account_by_email", _fake_find_web
        ), patch.object(c, "account_setup", _fake_setup):
            ok, summary = c.ingest_sso("sso", "a@b.c")

        self.assertTrue(ok, summary)
        self.assertEqual(calls, ["upload", "setup"])
        self.assertIn("web", summary)
        self.assertIn("NSFW", summary)
        # 派生字样不该再出现在摘要里
        for gone in ("console", "build"):
            self.assertNotIn(gone, summary.lower(), f"摘要里还有 {gone} 派生字样")

    def test_nsfw_can_be_skipped(self):
        c = _client()
        with patch.object(c, "import_sso", return_value=(True, "created=1")), patch.object(
            c, "find_web_account_by_email", return_value={"id": "1"}
        ), patch.object(c, "account_setup") as setup:
            ok, _ = c.ingest_sso("sso", "a@b.c", nsfw=False)
        self.assertTrue(ok)
        setup.assert_not_called()

    def test_upload_failure_is_still_fatal(self):
        c = _client()
        with patch.object(c, "import_sso", return_value=(False, "HTTP 500")):
            ok, summary = c.ingest_sso("sso", "a@b.c")
        self.assertFalse(ok)
        self.assertIn("上传失败", summary)


class ActionDeclarationDropsDeriveTests(unittest.TestCase):
    """面板动作不再声明派生开关（前端本来也没渲染过这些 params）。"""

    def test_upload_action_has_no_derive_param(self):
        from core.base_platform import RegisterConfig
        from platforms.grok.plugin import GrokPlatform

        actions = {
            a["id"]: a for a in GrokPlatform(config=RegisterConfig()).get_platform_actions()
        }
        keys = [p["key"] for p in actions["upload_grok2api"].get("params") or []]
        self.assertNotIn("derive", keys, "derive 开关已删除")
        self.assertIn("nsfw", keys, "NSFW 开关要保留")
        # 连接参数不动
        for keep in ("api_url", "username", "password"):
            self.assertIn(keep, keys)


class PluginCallSitesDroppedDeriveTests(unittest.TestCase):
    """插件的两个调用点都不再传 derive / tokens。"""

    def _src(self) -> str:
        from platforms.grok import plugin as mod

        return inspect.getsource(mod)

    def test_no_derive_reference_remains(self):
        src = self._src()
        for gone in (
            "grok2api_auto_derive",
            "sync_to_console",
            "convert_to_build",
            "import_build_tokens",
            "derive=",
        ):
            self.assertNotIn(
                gone, src, f"插件里还留着 {gone} —— 派生已移除"
            )


if __name__ == "__main__":
    unittest.main()
