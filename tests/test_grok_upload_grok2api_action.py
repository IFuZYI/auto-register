"""Grok 手动上传 grok2api 动作的行为回归。

背景：注册流程末尾已有**自动**接入（`plugin.py` 的 `grok_grok2api_ingest`），
但已注册的老账号、注册时没配 grok2api、或自动接入失败的账号没有补传入口。
`upload_grok2api` 动作补上这个缺口。

这些用例钉住：凭据校验（缺 SSO 明确报错而非 500）、连接参数优先级
（params > 全局配置）、记账字段（两处都写、嵌套合并不覆盖 CPA）、
以及失败时 ok=False 且带原因。
"""

import unittest
from unittest.mock import MagicMock, patch

from core.base_platform import Account, AccountStatus, RegisterConfig


def _platform(**extra):
    from platforms.grok.plugin import GrokPlatform

    return GrokPlatform(config=RegisterConfig(extra=extra))


# 一份「已配置」的 grok2api 连接参数（测试里喂给 config_store）
_CFG = {
    "grok2api_base_url": "http://g2a.test",
    "grok2api_username": "admin",
    "grok2api_password": "pw",
}


def _config_store(values: dict):
    """把 `config_store.get` 换成读给定字典。

    动作里的连接参数走 `Grok2ApiClient.from_config()` → `config_store.get()`，
    而 config_store 读的是数据库（本机没配）。测试里替换掉它，
    才能把「已配置」这个前提造出来。
    """
    return patch("core.config_store.config_store.get", side_effect=lambda k, d="": values.get(k, d))


def _account(**account_extra):
    extra = {"sso": "sso-value", "access_token": "at"}
    extra.update(account_extra)
    return Account(
        platform="grok",
        email="a@b.com",
        password="pw",
        status=AccountStatus.REGISTERED,
        extra=extra,
    )


class UploadGrok2ApiActionTests(unittest.TestCase):
    def test_action_is_registered(self):
        ids = [a["id"] for a in _platform().get_platform_actions()]
        self.assertIn("upload_grok2api", ids)

    def test_missing_sso_reports_clearly(self):
        """缺 SSO 直接说明原因 —— grok2api 的 Web 导入只吃 SSO。"""
        p = _platform()
        result = p.execute_action("upload_grok2api", _account(sso=""), {})
        self.assertFalse(result["ok"])
        self.assertIn("SSO", result["error"])

    def test_unconfigured_client_reports_clearly(self):
        """地址/用户名/密码缺任一都要明确报「未配置」，而不是抛异常。"""
        p = _platform()
        with _config_store({"grok2api_base_url": "", "grok2api_username": "", "grok2api_password": ""}):
            result = p.execute_action("upload_grok2api", _account(), {})
        self.assertFalse(result["ok"])
        self.assertIn("未配置", result["error"])

    def test_success_records_both_fields(self):
        """成功时两处都写：grok2api_* 与 sync_statuses.grok2api。"""
        p = _platform()
        with _config_store(_CFG), patch(
            "platforms.grok.grok2api.Grok2ApiClient.ingest_sso",
            return_value=(True, "已接入（web + console + build + NSFW）"),
        ):
            result = p.execute_action("upload_grok2api", _account(), {})

        self.assertTrue(result["ok"], result)
        patch_data = result["account_extra_patch"]
        self.assertTrue(patch_data["grok2api_ingested"])
        self.assertIn("已接入", patch_data["grok2api_result"])
        self.assertTrue(patch_data["sync_statuses"]["grok2api"]["uploaded"])

    def test_failure_is_not_ok_and_carries_reason(self):
        p = _platform()
        with _config_store(_CFG), patch(
            "platforms.grok.grok2api.Grok2ApiClient.ingest_sso",
            return_value=(False, "上传失败: HTTP 500"),
        ):
            result = p.execute_action("upload_grok2api", _account(), {})

        self.assertFalse(result["ok"])
        self.assertIn("500", result["error"])
        self.assertFalse(result["account_extra_patch"]["grok2api_ingested"])

    def test_exception_does_not_propagate(self):
        """客户端抛异常时动作要带回原因，不能让 API 层收到未捕获异常。"""
        p = _platform()
        with _config_store(_CFG), patch(
            "platforms.grok.grok2api.Grok2ApiClient.ingest_sso",
            side_effect=RuntimeError("boom"),
        ):
            result = p.execute_action("upload_grok2api", _account(), {})

        self.assertFalse(result["ok"])
        self.assertIn("boom", result["error"])

    def test_params_override_global_config(self):
        """动作把 params 透传给 from_config（非空值优先于全局配置）。

        注意这里断言的是**动作传了什么**；`from_config` 自身的优先级
        由下面 `FromConfigPriorityTests` 直接测 —— 只测前者会假绿：
        把 from_config 整个 patch 掉后，无论它内部怎么实现测试都通过。
        """
        p = _platform()
        captured = {}

        def _fake_from_config(cls, **kwargs):
            captured.update(kwargs)
            client = MagicMock()
            client.configured = True
            client.ingest_sso.return_value = (True, "ok")
            return client

        with _config_store(_CFG), patch(
            "platforms.grok.grok2api.Grok2ApiClient.from_config",
            classmethod(_fake_from_config),
        ):
            p.execute_action(
                "upload_grok2api",
                _account(),
                {"api_url": "http://other.test", "username": "u2", "password": "p2"},
            )

        self.assertEqual(captured["api_url"], "http://other.test")
        self.assertEqual(captured["username"], "u2")

    def test_nsfw_flag_passes_through(self):
        """nsfw 参数要真的传给 ingest_sso（前端传的是字符串）。"""
        p = _platform()
        seen = {}

        def _fake_ingest(self, sso, email, *, nsfw=True, log=None):
            seen["nsfw"] = nsfw
            return True, "ok"

        with _config_store(_CFG), patch(
            "platforms.grok.grok2api.Grok2ApiClient.ingest_sso", _fake_ingest
        ):
            p.execute_action("upload_grok2api", _account(), {"nsfw": "0"})

        self.assertFalse(seen["nsfw"], "nsfw=0 没生效")

    def test_nsfw_defaults_on_when_absent(self):
        """不传参数时 nsfw 默认开（默认值由**动作**给出，不是 mock 的默认值）。

        钉住传参本身：`seen` 记录的是动作实际传下来的值，mock 签名里的
        `nsfw=True` 只是接收端 —— 动作若不传，这里会看到 mock 的默认值，
        断言仍是 True（假绿）。所以额外断言动作确实**显式**传了这个关键字。
        """
        p = _platform()
        seen = {}
        explicit = {}

        def _fake_ingest(self, sso, email, *, nsfw=True, log=None):
            seen["nsfw"] = nsfw
            # 用哨兵值区分「动作传了 True」与「动作没传、吃了默认值」
            return True, "ok"

        def _sentinel(self, sso, email, **kwargs):
            explicit["kw"] = set(kwargs.keys())
            return _fake_ingest(self, sso, email, **kwargs)

        with _config_store(_CFG), patch(
            "platforms.grok.grok2api.Grok2ApiClient.ingest_sso", _sentinel
        ):
            p.execute_action("upload_grok2api", _account(), {})

        self.assertTrue(seen["nsfw"])
        self.assertIn("nsfw", explicit["kw"], "动作没有显式传 nsfw（吃了接收端默认值）")
        self.assertNotIn(
            "derive", explicit["kw"], "derive 参数已删除，不该再传"
        )


class FromConfigPriorityTests(unittest.TestCase):
    """`Grok2ApiClient.from_config` 的参数优先级（不 patch 它自己，直接测）。"""

    def test_explicit_values_win_over_config_store(self):
        from platforms.grok.grok2api import Grok2ApiClient

        with _config_store(_CFG):
            c = Grok2ApiClient.from_config(
                api_url="http://explicit.test", username="u9", password="p9"
            )
        self.assertEqual(c.base_url, "http://explicit.test")
        self.assertEqual(c.username, "u9")
        self.assertEqual(c.password, "p9")

    def test_blank_values_fall_back_to_config_store(self):
        from platforms.grok.grok2api import Grok2ApiClient

        with _config_store(_CFG):
            c = Grok2ApiClient.from_config(api_url="", username="", password="")
        self.assertEqual(c.base_url, "http://g2a.test")
        self.assertEqual(c.username, "admin")
        self.assertEqual(c.password, "pw")

    def test_username_defaults_to_admin_when_unset(self):
        """用户名缺省是 admin —— 与面板占位符一致。"""
        from platforms.grok.grok2api import Grok2ApiClient

        with _config_store({"grok2api_base_url": "http://x.test", "grok2api_password": "pw"}):
            c = Grok2ApiClient.from_config()
        self.assertEqual(c.username, "admin")


class MergePatchKeepsSiblingsTests(unittest.TestCase):
    """嵌套合并不能把已有的 CPA 状态覆盖掉。"""

    def test_nested_sync_statuses_merge(self):
        from api.actions import _merge_extra_patch

        base = {"sync_statuses": {"cpa": {"uploaded": True}}}
        patch_data = {"sync_statuses": {"grok2api": {"uploaded": True}}}
        out = _merge_extra_patch(base, patch_data)

        self.assertTrue(out["sync_statuses"]["cpa"]["uploaded"], "CPA 状态被覆盖了")
        self.assertTrue(out["sync_statuses"]["grok2api"]["uploaded"])


class SyncRecorderTests(unittest.TestCase):
    def test_recorder_shape_matches_cpa(self):
        """grok2api 的记账形状要与 CPA 一致，前端 uploadSyncMeta 才能直接复用。"""
        from services.chatgpt_sync import record_cpa_sync_result, record_grok2api_sync_result

        cpa = record_cpa_sync_result({}, True, "ok")
        g2a = record_grok2api_sync_result({}, True, "ok")

        self.assertEqual(sorted(cpa.keys()), sorted(g2a.keys()))
        self.assertTrue(g2a["uploaded"])
        self.assertIn("uploaded_at", g2a)

    def test_recorder_failure_marks_not_uploaded(self):
        from services.chatgpt_sync import record_grok2api_sync_result

        state = record_grok2api_sync_result({}, False, "HTTP 500")
        self.assertFalse(state["uploaded"])
        self.assertFalse(state["last_attempt_ok"])
        self.assertEqual(state["last_message"], "HTTP 500")


class ImportSsoSseParsingTests(unittest.TestCase):
    """`import_sso` 必须按 SSE 负载判成败，不能做字面量子串匹配。

    端到端跑真实 HTTP 时抓到的 bug：原实现查 `'"created":1' in text`，
    而 Go 的 json.Marshal 与 Python 的 json.dumps 输出不同（后者带空格，
    即 `"created": 1`）。只要序列化风格一变就误判成失败 ——
    而 mock 掉 `_request` 的单元测试完全看不到。
    """

    def _client(self):
        from platforms.grok.grok2api import Grok2ApiClient

        c = Grok2ApiClient("http://g2a.test", "admin", "pw")
        c._token = "tok"
        c._token_exp = 10**12
        return c

    def _resp(self, text, status=200):
        from unittest.mock import MagicMock

        r = MagicMock()
        r.status_code = status
        r.text = text
        return r

    def test_accepts_created_with_space(self):
        """带空格的 JSON（Python json.dumps 风格）也要判成功。"""
        c = self._client()
        text = 'event: complete\ndata: {"created": 1, "updated": 0, "failed": 0}\n\n'
        with patch.object(c, "_request", return_value=self._resp(text)):
            ok, msg = c.import_sso("sso")
        self.assertTrue(ok, msg)

    def test_accepts_created_without_space(self):
        """不带空格（Go json.Marshal 风格）同样成功。"""
        c = self._client()
        text = 'event: complete\ndata: {"created":1,"updated":0}\n\n'
        with patch.object(c, "_request", return_value=self._resp(text)):
            ok, _ = c.import_sso("sso")
        self.assertTrue(ok)

    def test_updated_counts_as_success(self):
        """重复上传是幂等的：created:0 / updated:1 算成功。"""
        c = self._client()
        text = 'event: complete\ndata: {"created": 0, "updated": 1}\n\n'
        with patch.object(c, "_request", return_value=self._resp(text)):
            ok, _ = c.import_sso("sso")
        self.assertTrue(ok)

    def test_all_zero_is_failure(self):
        """created/updated/skipped 全 0 且 failed>0 → 失败。"""
        c = self._client()
        text = 'event: complete\ndata: {"created": 0, "updated": 0, "skipped": 0, "failed": 1}\n\n'
        with patch.object(c, "_request", return_value=self._resp(text)):
            ok, msg = c.import_sso("sso")
        self.assertFalse(ok, msg)

    def test_skipped_without_failure_is_success(self):
        """已被导入过（skipped>0, failed:0）→ 成功。"""
        c = self._client()
        text = 'event: complete\ndata: {"created": 0, "updated": 0, "skipped": 1, "failed": 0}\n\n'
        with patch.object(c, "_request", return_value=self._resp(text)):
            ok, _ = c.import_sso("sso")
        self.assertTrue(ok)

    def test_progress_only_response_is_failure(self):
        """只有 progress、没有 complete → 判失败（别把半截流当成功）。"""
        c = self._client()
        text = 'event: progress\ndata: {"completed": 1, "total": 2}\n\n'
        with patch.object(c, "_request", return_value=self._resp(text)):
            ok, _ = c.import_sso("sso")
        self.assertFalse(ok)

    def test_non_200_is_failure_even_with_created(self):
        c = self._client()
        text = 'event: complete\ndata: {"created": 1}\n\n'
        with patch.object(c, "_request", return_value=self._resp(text, status=500)):
            ok, _ = c.import_sso("sso")
        self.assertFalse(ok)


class MultipartEncodingTests(unittest.TestCase):
    """multipart 必须走 CurlMime。

    curl_cffi 0.16.x 的 `files=` 参数还在签名上，但一调用就抛
    `NotImplementedError: files is not supported, use multipart`。
    这个坑只有真发请求才暴露（requirements 锁 `curl-cffi>=0.16.2,<0.17`）。
    """

    def test_request_builds_multipart_via_curlmime(self):
        from unittest.mock import MagicMock

        from platforms.grok.grok2api import Grok2ApiClient

        c = Grok2ApiClient("http://g2a.test", "admin", "pw")
        captured = {}

        def _fake_request(method, url, **kwargs):
            captured.update(kwargs)
            return MagicMock(status_code=200, text="{}")

        with patch("curl_cffi.requests.request", _fake_request):
            c._request(
                "POST",
                "/x",
                files={"file": ("grok-web-sso-tokens.txt", b"sso", "text/plain")},
            )

        self.assertIn("multipart", captured, "必须用 multipart 参数")
        self.assertIsNotNone(captured["multipart"], "multipart 不该是 None")
        self.assertNotIn("files", captured, "files= 会抛 NotImplementedError")


if __name__ == "__main__":
    unittest.main()
