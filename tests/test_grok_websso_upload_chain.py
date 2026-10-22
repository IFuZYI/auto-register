"""「上传 grok2api」（websso）这条链路的端到端回归：请求必须真的打出去。

用户要求：「注册 grok 时不需要注册 build 了，只要 websso（上传 grok2api）那个。」

这个文件钉的是**链路还在通电**，因为这条链路上任何一个环空转，症状都不是
「少传一个 build」，而是 SSO 根本没传上去、`grok2api_ingested` 却记成成功：

  - 手动动作 `from_config()` 读不到全局配置 → 永远「未配置」，补传入口形同虚设；
  - SSO 只在 token 列（grok 的 token 列是 SSO 镜像）时读不到 → 误报「账号没有 SSO」；
  - 自动接入的开关缺省被当成关闭 → 注册完静默跳过上传；
  - 上传文件名被改 → grok2api 认不出 token 类型。

**桩为什么会骗人（实测踩过两次）**：生产代码用 curl_cffi 的 `CurlMime`
装载负载（`multipart=`），负载不出现在 `files=` 里 —— 只记 `files` 的桩
看到空列表，断言直接跳过，"SSO 有没有真传上去"从没被验证。所以这里把
`CurlMime` 整个换成 `_FakeMultipart` 记录器，并在 `addpart` 的**关键字名**上
对齐真实签名（名字对不上时抛的 TypeError 会被 `_request` 的 catch-all
吞成「请求异常」，症状看起来像业务失败）。

断言落在可观测行为上：请求路径、上传文件名、负载内容、Bearer 头。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from core.base_platform import Account, AccountStatus, RegisterConfig

_G2A_CFG = {
    "grok2api_base_url": "http://g2a.test",
    "grok2api_username": "admin",
    "grok2api_password": "pw",
}


def setUpModule():
    """清掉进程级客户端缓存 —— 这些用例都靠 `from_config` 走真实配置路径。

    `Grok2ApiClient._CLIENT_CACHE` 是**进程全局**的（登录 token 缓存在实例上）。
    不清的话：本文件跑过一次后，缓存里留着 `(http://g2a.test, admin, "")` 的实例，
    同进程里后跑的其他文件（或换个用例改配置）会拿到带旧 password 的实例 ——
    结果取决于执行顺序（独立评审发现：跑完本文件后缓存确实被留下）。
    """

    from platforms.grok.grok2api import _CLIENT_CACHE

    _CLIENT_CACHE.clear()


def tearDownModule():
    from platforms.grok.grok2api import _CLIENT_CACHE

    _CLIENT_CACHE.clear()


class _Resp:
    def __init__(self, status: int = 200, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _config_store(values: dict):
    return patch(
        "core.config_store.config_store.get",
        side_effect=lambda k, d="": values.get(k, d),
    )


def _account(**extra) -> Account:
    base = {"sso": "sso-value"}
    base.update(extra)
    return Account(
        platform="grok",
        email="a@b.c",
        password="pw",
        status=AccountStatus.REGISTERED,
        extra=base,
    )


class _FakeMultipart:
    """替身 CurlMime：只实现生产代码用到的 `addpart` / `close`。"""

    def __init__(self, parts: list[bytes] | None = None):
        self.parts: list[dict] = []
        if parts:
            self.parts.append({"data": parts[0]})

    def addpart(self, field, *, filename="", data="", content_type="", **kwargs):
        # 真实签名是 `addpart(name, *, filename, data, content_type)`；
        # 关键字名与位置都对上，否则替身会在生产代码里抛 TypeError，
        # 而那个 TypeError 会被 `_request` 的 catch-all 吞成「请求异常」。
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.parts.append(
            {"field": field, "filename": filename, "content_type": content_type, "data": data}
        )

    def close(self):
        pass


class _G2ARecorder:
    """记录打到 grok2api 的所有请求，并**用替身接管 multipart**。

    生产代码用 curl_cffi 的 `CurlMime` 装载上传负载（`multipart=` 参数），
    负载不出现在 `files=` 里 —— 只记 `files` 的桩会看到空列表，
    于是「SSO 有没有真的传上去」这件事被静默跳过。这里把 `CurlMime`
    整个换成记录器，断言才落得住脚。
    """

    def __init__(self):
        self.paths: list[str] = []
        self.imports: list[bytes] = []
        self.logins = 0
        self.bearers: list[str] = []
        self.filenames: list[list[str]] = []
        self._mp_patches: list = []

    def __enter__(self):
        for target in ("curl_cffi.CurlMime",):
            p = patch(target, _FakeMultipart)
            try:
                p.start()
                self._mp_patches.append(p)
            except (AttributeError, ImportError):
                pass
        return self

    def __exit__(self, *exc):
        for p in self._mp_patches:
            p.stop()
        self._mp_patches.clear()
        return False

    def __call__(self, method, path, **kwargs):
        self.paths.append(path)
        auth = str((kwargs.get("headers") or {}).get("authorization") or "")
        if auth:
            self.bearers.append(auth)
        multipart = kwargs.get("multipart")
        if multipart is not None and hasattr(multipart, "parts"):
            self.filenames.append(
                [str(part.get("filename") or "") for part in multipart.parts]
            )
            for part in multipart.parts:
                data = part.get("data") or b""
                if data:
                    self.imports.append(
                        data if isinstance(data, bytes) else str(data).encode()
                    )
        if path.endswith("/auth/login"):
            self.logins += 1
            return _Resp(200, {"data": {"tokens": {"accessToken": "adm-token"}}})
        if "web/import" in path:
            return _Resp(200, text='data: {"created":1,"updated":0,"skipped":0,"failed":0}\n')
        if path.startswith("/api/admin/v1/accounts?"):
            return _Resp(200, {"data": {"items": [
                {"id": "41", "email": "a@b.c", "provider": "grok_web"},
            ]}})
        return _Resp(200, text='data: {"ok":true}\n')


class ManualUploadReachesGrok2ApiTests(unittest.TestCase):
    """手动动作：配置只填全局面板设置时，SSO 必须真的传上去。"""

    def test_upload_action_posts_sso_to_web_import(self):
        from platforms.grok.plugin import GrokPlatform

        with _G2ARecorder() as rec, _config_store(_G2A_CFG), patch(
            "curl_cffi.requests.request", side_effect=rec
        ):
            platform = GrokPlatform(config=RegisterConfig(extra={}))
            result = platform.execute_action("upload_grok2api", _account(), {})

        self.assertTrue(result.get("ok"), result)
        self.assertTrue(
            any("web/import" in p for p in rec.paths),
            f"没有打到 Web 导入端点: {rec.paths}",
        )
        self.assertTrue(rec.imports, "导入请求里没有文件负载")
        # 上传文件名固定（grok2api 靠它识别 token 类型，改名会 500）
        self.assertIn(
            "grok-web-sso-tokens.txt",
            " ".join(str(x) for x in rec.filenames[-1:]),
            "上传文件名不是 grok-web-sso-tokens.txt",
        )
        self.assertIn(b"sso-value", rec.imports[-1], "负载里没有 SSO")
        # 登录走的是进程内实例缓存（TTL 9 分钟），同参数的 client 直接复用 ——
        # 所以这里只能断言「有可用的 Bearer」，不能要求每次都重新登录。
        self.assertTrue(rec.bearers, "导入请求没带管理面 token")
        self.assertTrue(all(b.startswith("Bearer ") for b in rec.bearers))

    def test_missing_sso_is_reported_without_calling_grok2api(self):
        """没有 SSO 时明确报错，且**不发请求**（避免用空凭据污染远端）。"""
        from platforms.grok.plugin import GrokPlatform

        with _G2ARecorder() as rec, _config_store(_G2A_CFG), patch(
            "curl_cffi.requests.request", side_effect=rec
        ):
            platform = GrokPlatform(config=RegisterConfig(extra={}))
            account = Account(
                platform="grok", email="a@b.c", password="",
                status=AccountStatus.REGISTERED, extra={},
            )
            result = platform.execute_action("upload_grok2api", account, {})

        self.assertFalse(result.get("ok"))
        self.assertIn("SSO", str(result.get("error") or ""))
        self.assertEqual(rec.paths, [], f"不该发请求: {rec.paths}")

    def test_sso_read_from_token_column_mirror(self):
        """extra 没有 sso、token 列有值（grok 的 token 列 = SSO 镜像）时仍要能传。"""
        from platforms.grok.plugin import GrokPlatform

        with _G2ARecorder() as rec, _config_store(_G2A_CFG), patch(
            "curl_cffi.requests.request", side_effect=rec
        ):
            platform = GrokPlatform(config=RegisterConfig(extra={}))
            account = Account(
                platform="grok", email="a@b.c", password="",
                token="mirror-sso-value",
                status=AccountStatus.REGISTERED, extra={},
            )
            result = platform.execute_action("upload_grok2api", account, {})

        self.assertTrue(result.get("ok"), result)
        self.assertTrue(rec.imports, "导入请求里没有文件负载")
        self.assertIn(b"mirror-sso-value", rec.imports[-1])


class AutoIngestReachesGrok2ApiTests(unittest.TestCase):
    """注册末尾的自动接入：开关缺省（没填）时必须按「已配置就开」跑起来。"""

    def _run_auto_ingest(self, extra: dict) -> _G2ARecorder:
        from platforms.grok import plugin as plugin_mod

        # 注册本体（浏览器路径）用桩替换：本用例只关心它之后的上传段。
        with _G2ARecorder() as rec, _config_store(_G2A_CFG), patch(
            "curl_cffi.requests.request", side_effect=rec
        ), patch(
            "platforms.grok.register_browser.register_grok_via_browser",
            return_value={
                "ok": True,
                "sso": "auto-sso-value",
                "password": "pw",
                "already_registered": False,
            },
        ), patch(
            "platforms.grok.register_browser.exchange_oauth_via_browser",
            return_value=None,
        ), patch(
            "core.environment.check_camoufox_ready",
            return_value=type("V", (), {"ok": True, "message": ""})(),
        ):
            platform = plugin_mod.GrokPlatform(config=RegisterConfig(extra=extra))
            platform._log_fn = lambda _m: None
            account = platform.register(email="auto@b.c", password="pw")
        self.assertEqual(account.email, "auto@b.c")
        return rec

    def test_auto_ingest_uploads_sso_when_switches_unset(self):
        rec = self._run_auto_ingest({})
        self.assertTrue(
            any("web/import" in p for p in rec.paths),
            f"注册末尾没有上传 SSO（开关缺省被当成关闭）: {rec.paths}",
        )
        self.assertTrue(rec.imports, "导入请求里没有文件负载")
        self.assertIn(b"auto-sso-value", rec.imports[-1])

    def test_auto_ingest_respects_explicit_off(self):
        """显式关掉时才真的不传 —— 与缺省区分开。"""
        rec = self._run_auto_ingest({"grok2api_enabled": "0"})
        self.assertEqual(
            [p for p in rec.paths if "web/import" in p], [],
            f"显式关闭后仍在传: {rec.paths}",
        )


class ClientCacheIsolationTests(unittest.TestCase):
    """本模块必须清掉进程级客户端缓存（独立评审发现它此前会留下残留）。

    `Grok2ApiClient._CLIENT_CACHE` 是进程全局的；不清的话，本文件跑完后同进程的
    后续用例会拿到带旧 password 的实例 —— 结果取决于执行顺序。
    """

    def test_module_teardown_clears_the_shared_cache(self):
        from platforms.grok import grok2api

        grok2api._CLIENT_CACHE[("http://leak.test", "admin", "")] = object()
        self.assertTrue(grok2api._CLIENT_CACHE, "前置：缓存里确实有东西")
        tearDownModule()
        self.assertEqual(
            grok2api._CLIENT_CACHE, {},
            "tearDownModule 没清缓存 —— 同进程后续用例会拿到陈旧实例",
        )


if __name__ == "__main__":
    unittest.main()
