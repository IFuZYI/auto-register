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

    def test_derive_and_nsfw_flags_pass_through(self):
        """derive/nsfw 参数要真的传给 ingest_sso（前端传的是字符串）。"""
        p = _platform()
        seen = {}

        def _fake_ingest(self, sso, email, *, derive=True, nsfw=True, tokens=None, log=None):
            seen["derive"] = derive
            seen["nsfw"] = nsfw
            return True, "ok"

        with _config_store(_CFG), patch(
            "platforms.grok.grok2api.Grok2ApiClient.ingest_sso", _fake_ingest
        ):
            p.execute_action("upload_grok2api", _account(), {"derive": "false", "nsfw": "0"})

        self.assertFalse(seen["derive"], "derive=false 没生效")
        self.assertFalse(seen["nsfw"], "nsfw=0 没生效")

    def test_derive_nsfw_default_on_when_absent(self):
        p = _platform()
        seen = {}

        def _fake_ingest(self, sso, email, *, derive=True, nsfw=True, tokens=None, log=None):
            seen["derive"] = derive
            seen["nsfw"] = nsfw
            return True, "ok"

        with _config_store(_CFG), patch(
            "platforms.grok.grok2api.Grok2ApiClient.ingest_sso", _fake_ingest
        ):
            p.execute_action("upload_grok2api", _account(), {})

        self.assertTrue(seen["derive"])
        self.assertTrue(seen["nsfw"])

    def test_account_tokens_are_forwarded_for_build_import(self):
        """账号已存的 token 要传给 ingest_sso —— Build 凭据直接导入用它。

        背景：grok2api 的 `convert-to-build`（Device Flow）已被上游加严挡住
        （`device/approve` 要求同意页的 consent_token + 浏览器头，服务端没跟上，
        永远 `failed:1`）。而我们注册时换的 token 用的就是 Build 的 client_id
        与 scope，本身就是 Build 凭据，直接 import 即可（实测 `created:1`）。
        """
        p = _platform()
        seen = {}

        def _fake_ingest(self, sso, email, *, derive=True, nsfw=True, tokens=None, log=None):
            seen["tokens"] = tokens
            return True, "ok"

        acc = _account()
        acc.extra = {
            "sso": "sso-token",
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "id_token": "idt-1",
        }
        with _config_store(_CFG), patch(
            "platforms.grok.grok2api.Grok2ApiClient.ingest_sso", _fake_ingest
        ):
            p.execute_action("upload_grok2api", acc, {})

        self.assertIsNotNone(seen["tokens"], "有 token 时必须传下去，否则会退回被挡住的转换路径")
        self.assertEqual(seen["tokens"]["access_token"], "at-1")
        self.assertEqual(seen["tokens"]["refresh_token"], "rt-1")
        self.assertEqual(seen["tokens"]["id_token"], "idt-1")

    def test_no_tokens_passes_none(self):
        """没有 token 的老账号传 None → ingest_sso 回退到 grok2api 自己的转换。"""
        p = _platform()
        seen = {}

        def _fake_ingest(self, sso, email, *, derive=True, nsfw=True, tokens=None, log=None):
            seen["tokens"] = tokens
            return True, "ok"

        acc = _account()
        acc.extra = {"sso": "sso-token"}
        with _config_store(_CFG), patch(
            "platforms.grok.grok2api.Grok2ApiClient.ingest_sso", _fake_ingest
        ):
            p.execute_action("upload_grok2api", acc, {})

        self.assertIsNone(seen["tokens"])


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


class ImportBuildTokensTests(unittest.TestCase):
    """`import_build_tokens`：把已有的 OAuth token 当 Build 凭据导入。

    为什么需要这条路（实测）：
    grok2api 的 `convert-to-build` 走 xAI Device Flow，其 `device/approve`
    已被上游加严 —— 要求同意页里的 `consent_token` + 完整浏览器头，
    否则一律 403 `Request could not be verified`（试过纯 HTTP 的各种头组合，
    只有真浏览器点按钮才过）。而**我们注册时换的 token 用的就是 Build 的
    client_id 与 scope**，本身就是 Build 凭据：直接 POST `/accounts/import`
    返回 `created:1, failed:0`，grok2api 还会自动把新 Build 账号与同邮箱的
    Web 账号互相关联。
    """

    def _client(self):
        from platforms.grok.grok2api import Grok2ApiClient

        c = Grok2ApiClient("http://g2a.test", "admin", "pw")
        c._token = "tok"
        return c

    def _resp(self, text, status=200):
        from unittest.mock import MagicMock

        return MagicMock(status_code=status, text=text)

    def test_created_one_is_success(self):
        c = self._client()
        text = ('event: complete\ndata: {"created":1,"updated":0,"skipped":0,'
                '"failed":0,"synced":1,"syncFailed":0}\n\n')
        captured = {}

        def _fake(method, url, **kwargs):
            captured["url"] = url
            captured["files"] = kwargs.get("files")
            return self._resp(text)

        with patch.object(c, "_request", _fake):
            ok, msg = c.import_build_tokens(
                email="a@b.c", access_token="at", refresh_token="rt", id_token="idt"
            )

        self.assertTrue(ok, msg)
        self.assertIn("created=1", msg)
        # 必须打到 build 的导入端点，且文件名/结构正确
        self.assertIn("/accounts/import", captured["url"])
        fname, content, ctype = captured["files"]["file"]
        self.assertTrue(fname.endswith(".json"), fname)
        self.assertEqual(ctype, "application/json")
        import json as _json
        doc = _json.loads(content.decode("utf-8"))
        entry = doc["accounts"][0]
        self.assertEqual(entry["provider"], "grok_build")
        self.assertEqual(entry["access_token"], "at")
        self.assertEqual(entry["refresh_token"], "rt")
        self.assertEqual(entry["email"], "a@b.c")
        # client_id 必须是 Build 那个（与 constants.CLIENT_ID 一致）
        self.assertEqual(entry["client_id"], "b1a00492-073a-47ea-816f-4c329264a828")

    def test_failed_count_is_failure(self):
        c = self._client()
        text = 'event: complete\ndata: {"created":0,"failed":1}\n\n'
        with patch.object(c, "_request", return_value=self._resp(text)):
            ok, _ = c.import_build_tokens(email="a@b.c", access_token="at")
        self.assertFalse(ok)

    def test_missing_access_token_short_circuits(self):
        """没有 access_token 直接报错，不发请求（避免打空请求）。"""
        c = self._client()
        with patch.object(c, "_request") as m:
            ok, msg = c.import_build_tokens(email="a@b.c", access_token="")
        self.assertFalse(ok)
        self.assertIn("access_token", msg)
        m.assert_not_called()

    def test_ingest_prefers_import_over_convert_when_tokens_given(self):
        """给了 token 就必须走 import，不能再去调被挡住的 convert-to-build。"""
        c = self._client()
        calls = []

        def _fake_import_sso(sso):
            return True, "created=1"

        def _fake_find_web(email):
            return {"id": "41", "provider": "grok_web"}

        def _fake_sync_console(ids):
            return True, "ok"

        def _fake_import_build(**kwargs):
            calls.append(("import", kwargs.get("access_token")))
            return True, "created=1"

        def _fake_convert(ids, strategy="missing"):
            calls.append(("convert", None))
            return True, "should not be called"

        def _fake_find_any(email):
            return {"id": "41"}

        def _fake_setup(web_id, nsfw=True):
            return True, ["accept-terms"], []

        with patch.object(c, "import_sso", _fake_import_sso), \
             patch.object(c, "find_web_account_by_email", _fake_find_web), \
             patch.object(c, "sync_to_console", _fake_sync_console), \
             patch.object(c, "import_build_tokens", _fake_import_build), \
             patch.object(c, "convert_to_build", _fake_convert), \
             patch.object(c, "find_web_account_by_email", _fake_find_web), \
             patch.object(c, "account_setup", _fake_setup):
            ok, msg = c.ingest_sso(
                "sso", "a@b.c",
                tokens={"access_token": "at-1", "refresh_token": "rt-1"},
            )

        self.assertTrue(ok, msg)
        self.assertIn(("import", "at-1"), calls)
        self.assertNotIn(("convert", None), calls, "有 token 时不该再调 convert-to-build")
        self.assertIn("build", msg)

    def test_ingest_uses_web_account_id_for_derive(self):
        """派生必须用 Web 号池的 id。

        `sync-to-console` / `convert-to-build` 只接受 provider=grok_web 的 id，
        拿同邮箱的 build/console id 会被拒 `accountPoolMismatch`（实测）。
        而 `find_account_by_email` 不筛 provider，同邮箱下常返回 build 那条。
        """
        c = self._client()
        seen_ids = []

        def _fake_import_sso(sso):
            return True, "created=1"

        def _fake_find_web(email):
            return {"id": "WEB-41", "provider": "grok_web"}

        def _fake_sync_console(ids):
            seen_ids.append(list(ids))
            return True, "ok"

        def _fake_convert(ids, strategy="missing"):
            seen_ids.append(list(ids))
            return True, "ok"

        def _fake_setup(web_id, nsfw=True):
            return True, [], []

        with patch.object(c, "import_sso", _fake_import_sso), \
             patch.object(c, "find_web_account_by_email", _fake_find_web), \
             patch.object(c, "sync_to_console", _fake_sync_console), \
             patch.object(c, "convert_to_build", _fake_convert), \
             patch.object(c, "account_setup", _fake_setup):
            c.ingest_sso("sso", "a@b.c")  # 不给 token → 走 convert 回退

        self.assertTrue(seen_ids, "两个派生都没被调用")
        for ids in seen_ids:
            self.assertEqual(ids, ["WEB-41"], "必须用 Web 号池的 id")


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
