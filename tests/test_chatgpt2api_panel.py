"""chatgpt2api 面板：注册表、动作、上传格式。

对端口径来自仓库内的参考实现
（`reference/register/reg-factory/tools/export_chatgpt2api.py` 与
`common/session_export.build_chatgpt2api_account`）：

* `POST <host>/api/accounts`，body `{"accounts": [...]}`
* 认证 `Authorization: Bearer <admin key>`
* 每个对象**只有 access_token 必需**；普通网页号**不带 `type: "codex"`**
  （带了会被对端当成 codex 源）
* 重复 token 对端按 skipped 处理（幂等）
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _account(**kw):
    base = {
        "email": "user@example.com",
        "access_token": "eyJhbGciOiJIUzI1NiJ9.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjLTEiLCJjaGF0Z3B0X3BsYW5fdHlwZSI6ImZyZWUifX0.sig",
        "token": "",
        "client_id": "app_x",
    }
    base.update(kw)

    class _A:
        pass

    a = _A()
    for k, v in base.items():
        setattr(a, k, v)
    return a


class RegistryTests(unittest.TestCase):
    def test_panel_is_registered(self):
        from services.panel_registry import PANELS_BY_KEY

        self.assertIn("chatgpt2api", PANELS_BY_KEY)
        panel = PANELS_BY_KEY["chatgpt2api"]
        self.assertEqual(panel["url_key"], "chatgpt2api_api_url")
        self.assertEqual(panel["secret_key"], "chatgpt2api_api_key")
        self.assertEqual(panel["upload_action"], "upload_chatgpt2api")
        self.assertEqual(panel["platform"], "chatgpt")
        # 列表自带 `status_label` + `credential_availability` —— 读回来写回本地
        self.assertEqual(panel.get("sync_action"), "sync_chatgpt2api_status")

    def test_config_keys_exist_and_are_whitelisted(self):
        from api.config import CONFIG_KEYS, SECRET_CONFIG_KEYS

        for key in ("chatgpt2api_enabled", "chatgpt2api_api_url", "chatgpt2api_api_key"):
            self.assertIn(key, CONFIG_KEYS)
        self.assertIn("chatgpt2api_api_key", SECRET_CONFIG_KEYS,
                      "管理密钥必须打码，不能明文下发")

    def test_action_is_declared_on_the_chatgpt_platform(self):
        actions = {a["id"] for a in _chatgpt_actions()}
        self.assertIn("upload_chatgpt2api", actions)

    def test_upload_action_is_panel_scoped(self):
        """面板动作要从账号页菜单里隐藏（scope=panel）。"""
        actions = {a["id"]: a for a in _chatgpt_actions()}
        self.assertEqual(actions["upload_chatgpt2api"].get("scope"), "panel")


def _chatgpt_actions():
    from platforms.chatgpt.plugin import ChatGPTPlatform

    return ChatGPTPlatform.get_platform_actions(ChatGPTPlatform.__new__(ChatGPTPlatform))


class UploadFormatTests(unittest.TestCase):
    def test_account_object_matches_reference_shape(self):
        """导入对象只带对端认的字段，且不带 type=codex。"""
        from platforms.chatgpt.chatgpt2api_upload import build_chatgpt2api_account

        item = build_chatgpt2api_account(_account())
        self.assertIn("access_token", item)
        self.assertEqual(item["source_type"], "web")
        self.assertEqual(item["email"], "user@example.com")
        self.assertEqual(item["account_id"], "acc-1")
        self.assertEqual(item["type"], "free")
        # 普通网页号不能带 codex 标记，也不能塞 refresh/id token
        self.assertNotEqual(str(item.get("type", "")).lower(), "codex")
        self.assertNotIn("refresh_token", item)
        self.assertNotIn("id_token", item)

    def test_codex_marker_never_leaks_into_the_payload(self):
        """套餐类型解析成 codex 时也不能把它写进 `type`。

        对端看到 `type == "codex"` 会把这个号当 codex 源走另一条解析路径
        （参考实现的注释专门警告过）。上游字段漂移时静默传错极难排查，
        所以这里必须挡住 —— 这条测试是那次「变异没被抓到」的补丁。
        """
        from platforms.chatgpt.chatgpt2api_upload import build_chatgpt2api_account

        # JWT 里不带 plan 声明，只让账号属性给出 codex —— 这才走到
        # `plan_type` 分支（JWT 里的 chatgpt_plan_type 优先级更高）。
        import base64 as _b64
        import json as _json

        payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "acc-1"}}
        seg = _b64.urlsafe_b64encode(_json.dumps(payload).encode()).decode().rstrip("=")
        token = f"eyJhbGciOiJIUzI1NiJ9.{seg}.sig"

        acct = _account(access_token=token, plan_type="codex")
        item = build_chatgpt2api_account(acct)
        self.assertNotEqual(str(item.get("type", "")).lower(), "codex")
        self.assertNotIn("type", item)

    def test_missing_access_token_is_a_clean_error(self):
        from platforms.chatgpt.chatgpt2api_upload import build_chatgpt2api_account

        with self.assertRaises(ValueError):
            build_chatgpt2api_account(_account(access_token="", token=""))

    def test_post_uses_bearer_auth_and_accounts_envelope(self):
        from platforms.chatgpt.chatgpt2api_upload import upload_to_chatgpt2api

        captured = {}

        def fake_post(url, headers=None, json=None, **kw):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _Resp(200, {"added": 1, "skipped": 0, "refreshed": 0})

        with mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.cffi_requests.post", side_effect=fake_post
        ):
            ok, msg = upload_to_chatgpt2api(
                _account(), api_url="http://c2a.local", api_key="ADMIN"
            )

        self.assertTrue(ok, msg)
        self.assertEqual(captured["url"], "http://c2a.local/api/accounts")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer ADMIN")
        self.assertIn("accounts", captured["json"])
        self.assertEqual(len(captured["json"]["accounts"]), 1)

    def test_idempotent_resend_counts_as_success(self):
        """重复上传返回 added=0/skipped=1，这是成功（幂等），不是失败。"""
        from platforms.chatgpt.chatgpt2api_upload import upload_to_chatgpt2api

        with mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.cffi_requests.post",
            return_value=_Resp(200, {"added": 0, "skipped": 1, "refreshed": 0}),
        ):
            ok, msg = upload_to_chatgpt2api(_account(), api_url="http://c2a", api_key="k")
        self.assertTrue(ok, msg)
        self.assertIn("跳过", msg)

    def test_http_error_is_reported_not_raised(self):
        from platforms.chatgpt.chatgpt2api_upload import upload_to_chatgpt2api

        with mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.cffi_requests.post",
            return_value=_Resp(401, {"message": "invalid key"}),
        ):
            ok, msg = upload_to_chatgpt2api(_account(), api_url="http://c2a", api_key="bad")
        self.assertFalse(ok)
        # 报错要带上对端给的原因（比裸状态码有用）
        self.assertIn("invalid key", msg)

    def test_errors_with_no_import_are_reported_as_failure(self):
        """`errors` 非空且一个号都没进（added=0/skipped=0）→ 必须报失败。

        评审发现：原实现无条件返回 `ok=True`，消息却是「上传成功（错误 [...]）」
        —— 自相矛盾，且调用方会把失败记成成功，账号静默丢失。
        """
        from platforms.chatgpt.chatgpt2api_upload import upload_to_chatgpt2api

        with mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.cffi_requests.post",
            return_value=_Resp(200, {"added": 0, "skipped": 0, "errors": ["token expired"]}),
        ):
            ok, msg = upload_to_chatgpt2api(_account(), api_url="http://c2a", api_key="k")
        self.assertFalse(ok, f"errors 非空且没导入任何号，却报成功: {msg}")
        self.assertIn("失败", msg)

    def test_idempotent_resend_is_still_success(self):
        """幂等重传（added=0/skipped=1）**不受**上面那条影响，仍是成功。"""
        from platforms.chatgpt.chatgpt2api_upload import upload_to_chatgpt2api

        with mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.cffi_requests.post",
            return_value=_Resp(200, {"added": 0, "skipped": 1, "errors": ["minor"]}),
        ):
            ok, msg = upload_to_chatgpt2api(_account(), api_url="http://c2a", api_key="k")
        self.assertTrue(ok, f"幂等重传被误判为失败: {msg}")

    def test_verify_false_is_the_project_wide_convention(self):
        """`verify=False` 是项目既有的上传约定，不是本模块的疏漏。

        审查员曾把「向外部服务发送凭据时关闭 TLS 校验」标为安全问题。
        核查：CPA（`platforms/chatgpt/cpa_upload.py`）与 Sub2API
        （`platforms/chatgpt/sub2api_upload.py`）上传器**早就**这样写，
        早于本模块；自签/内网部署的对端很常见。这是**已知取舍**，要改应该
        全局一起改（例如引入 `verify_ssl` 配置项），而不是只改这一处。
        这条测试把这个事实钉住，避免后续审查反复把它当成新引入的缺陷。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for rel in (
            "platforms/chatgpt/cpa_upload.py",
            "platforms/chatgpt/sub2api_upload.py",
            "platforms/chatgpt/chatgpt2api_upload.py",
        ):
            src = (root / rel).read_text(encoding="utf-8")
            self.assertIn(
                "verify=False", src,
                f"{rel} 不再用 verify=False —— 若是有意改为校验，请同时更新本测试与其余上传器",
            )

    def test_missing_config_is_reported(self):
        from platforms.chatgpt.chatgpt2api_upload import upload_to_chatgpt2api

        ok, msg = upload_to_chatgpt2api(_account(), api_url="", api_key="")
        self.assertFalse(ok)
        self.assertIn("未配置", msg)

    def test_connection_errors_are_retried(self):
        """出口代理抖动要退避重试（对齐参考实现）。"""
        from platforms.chatgpt.chatgpt2api_upload import upload_to_chatgpt2api

        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("tls reset")
            return _Resp(200, {"added": 1, "skipped": 0})

        with mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.cffi_requests.post", side_effect=flaky
        ), mock.patch("platforms.chatgpt.chatgpt2api_upload.time.sleep", lambda *_: None):
            ok, msg = upload_to_chatgpt2api(_account(), api_url="http://c2a", api_key="k")

        self.assertTrue(ok, msg)
        self.assertEqual(calls["n"], 3, "应重试到成功")

    def test_deterministic_errors_are_not_retried(self):
        """确定性错误（非法参数等）不该重试 —— 重试只会白等退避时间。

        评审发现：原实现用 `except Exception` 捕获一切并重试 4 次，注释却
        写着「只对连接类异常」。参考实现（export_chatgpt2api.py:import_accounts）
        只捕获 `(ConnectionError, Timeout)`。这里钉住「非连接异常 → 只调一次」。
        """
        from platforms.chatgpt.chatgpt2api_upload import upload_to_chatgpt2api

        calls = {"n": 0}

        def boom(*a, **kw):
            calls["n"] += 1
            raise ValueError("unsupported impersonate value")

        with mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.cffi_requests.post", side_effect=boom
        ), mock.patch("platforms.chatgpt.chatgpt2api_upload.time.sleep") as sleeper:
            ok, msg = upload_to_chatgpt2api(_account(), api_url="http://c2a", api_key="k")

        self.assertFalse(ok)
        self.assertEqual(calls["n"], 1, f"确定性错误被重试了 {calls['n']} 次")
        sleeper.assert_not_called()


class AutoUploadWiringTests(unittest.TestCase):
    def test_external_sync_uploads_to_chatgpt2api(self):
        """行为验证：开关打开时 `sync_account` 真的会把账号传给 chatgpt2api。

        评审指出旧版只 `assertIn("upload_to_chatgpt2api", src)` 读源码文本 ——
        把调用挪进死分支、写反条件、或丢弃返回值都不会变红，抓不到它声称
        要抓的机制。改为断言运行时的 post 调用与请求体。
        """
        import services.external_sync as es

        class _Cfg:
            def get(self, key, default=""):
                return {
                    "chatgpt2api_api_url": "http://c2a",
                    "chatgpt2api_api_key": "k",
                    "chatgpt2api_enabled": "1",
                }.get(key, default)

        captured = {}

        def fake_post(url, headers=None, json=None, **kw):
            captured["url"] = url
            captured["json"] = json
            return _Resp(200, {"added": 1, "skipped": 0, "errors": []})

        acct = _account()
        acct.platform = "chatgpt"
        acct.extra = {"access_token": acct.access_token}

        with mock.patch("core.config_store.config_store", _Cfg()), mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.cffi_requests.post", side_effect=fake_post
        ):
            es.sync_account(acct)

        self.assertIn("/api/accounts", captured.get("url", ""), "没打到导入端点")
        accounts = (captured.get("json") or {}).get("accounts") or []
        self.assertEqual(len(accounts), 1, f"请求体没带上账号: {captured.get('json')}")
        self.assertEqual(accounts[0].get("access_token"), acct.access_token)

    def test_auto_upload_respects_the_toggle(self):
        """开关关闭时不该上传。

        ⚠️ 这条测试曾经是**空转的**：`_account()` 默认没有 access_token，
        而 `sync_account` 在到达上传分支前就会因「账号没有 access_token」返回，
        于是 `post.assert_not_called()` 无论开关如何都成立（实测：去掉
        `c2a_enabled` 判断后测试照样通过）。现在 fixture 带上 access_token，
        并额外断言「开关打开时**确实**会调 post」——两条一起才构成有效对照。
        """
        import services.external_sync as es

        def _cfg(enabled: str):
            class _Cfg:
                def get(self, key, default=""):
                    return {
                        "chatgpt2api_api_url": "http://c2a",
                        "chatgpt2api_api_key": "k",
                        "chatgpt2api_enabled": enabled,
                    }.get(key, default)

            return _Cfg()

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"added": 1, "skipped": 0, "errors": []}

            text = ""

        acct = _account()
        acct.platform = "chatgpt"
        # `sync_account` 从 `account.get_extra()` / `account.extra` 取凭据
        # （见 services/chatgpt_sync.py:_get_account_extra），**不是**直接读
        # `account.access_token`。没有 extra 就取不到 token，上传分支不会执行 ——
        # 这正是这条测试原先空转的原因。
        acct.extra = {"access_token": acct.access_token}

        # 对照 A：开关关闭 → 不该上传
        with mock.patch("core.config_store.config_store", _cfg("0")), mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.cffi_requests.post"
        ) as post_off:
            es.sync_account(acct)
            post_off.assert_not_called()

        # 对照 B：开关打开 → **必须**上传（证明 A 不是空转）
        with mock.patch("core.config_store.config_store", _cfg("1")), mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.cffi_requests.post",
            return_value=_Resp(),
        ) as post_on:
            es.sync_account(acct)
            self.assertTrue(
                post_on.called,
                "开关打开时没上传 —— 说明关闭时的 assert_not_called 是空转",
            )

    def test_upload_result_is_persisted_for_the_ui(self):
        """上传结果要写进 `sync_statuses.chatgpt2api`，否则界面永远不显示状态。

        评审发现：原实现只把结果 append 进返回值，没像 CPA/Sub2API 那样
        `persist_*_sync_result` —— 前端账号列表的通用上传状态标签读
        `sync_statuses.<面板名>`，缺这一步就等于上传状态永远空白。
        """
        import services.external_sync as es

        class _Cfg:
            def get(self, key, default=""):
                return {
                    "chatgpt2api_api_url": "http://c2a",
                    "chatgpt2api_api_key": "k",
                    "chatgpt2api_enabled": "1",
                }.get(key, default)

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"added": 1, "skipped": 0, "errors": []}

            text = ""

        acct = _account()
        acct.platform = "chatgpt"
        acct.extra = {"access_token": acct.access_token}

        with mock.patch("core.config_store.config_store", _Cfg()), mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.cffi_requests.post",
            return_value=_Resp(),
        ):
            es.sync_account(acct)

        statuses = (getattr(acct, "extra", {}) or {}).get("sync_statuses") or {}
        self.assertIn(
            "chatgpt2api",
            statuses,
            "上传结果没写进 sync_statuses.chatgpt2api —— 界面不会显示上传状态",
        )
        self.assertTrue(statuses["chatgpt2api"].get("last_attempt_ok"))

    def test_ui_section_exists(self):
        src = (
            ROOT / "frontend/src/components/settings/PanelConfigPanel.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("chatgpt2api_api_url", src)
        self.assertIn("chatgpt2api_api_key", src)
        self.assertIn("chatgpt2api_enabled", src)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}"

    def json(self):
        return self._payload


class RemoteFetchTests(unittest.TestCase):
    """chatgpt2api 的远端拉取（面板管理页的对比数据源）。

    用户报过「未知面板: chatgpt2api」—— 面板页一打开就拉 `/comparison`，
    而 `FETCHERS` 里没有这个 key → 404。注册面板时**必须同步注册**：
    对比 fetcher（本类）、本地平台映射、凭据读取，三处缺一不可。
    """

    def test_panel_is_registered_in_fetchers(self):
        """钉住用户报的那个 404 根因。"""
        from services.panel_comparison import FETCHERS
        from services.panel_comparison_cache import _PANEL_PLATFORMS

        self.assertIn(
            "chatgpt2api", FETCHERS,
            "面板没注册对比 fetcher —— 面板页会报「未知面板: chatgpt2api」",
        )
        self.assertIn("chatgpt2api", _PANEL_PLATFORMS)

    def test_comparison_endpoint_no_longer_404s(self):
        """端到端：走一遍 API 层，确认不再抛「未知面板」。"""
        from services.panel_comparison_cache import clear_cache, get_panel_comparison

        clear_cache("chatgpt2api")
        # 未配置地址时应返回可展示的 remote_error，而不是抛 ValueError
        with mock.patch(
            "services.panel_comparison_cache._panel_credentials",
            return_value={"api_url": "", "api_key": ""},
        ):
            payload = get_panel_comparison("chatgpt2api")

        self.assertEqual(payload["panel"], "chatgpt2api")
        self.assertIn("未配置", payload["remote_error"])

    def test_credentials_come_from_the_config_keys(self):
        from services.panel_comparison_cache import _panel_credentials

        class _Cfg:
            def get_all(self):
                return {
                    "chatgpt2api_api_url": "http://c2a.local/",
                    "chatgpt2api_api_key": "secret",
                }

        with mock.patch("core.config_store.config_store", _Cfg()):
            creds = _panel_credentials("chatgpt2api")
        self.assertEqual(creds["api_url"], "http://c2a.local/")
        self.assertEqual(creds["api_key"], "secret")

    def test_list_rows_carry_the_chatgpt_platform(self):
        """匹配键是 (平台, 邮箱)：远端行不带 platform 就永远匹配不上本地行。

        实测踩过同款 bug（grok2api 面板 22 个本地账号全部显示「未上传」，
        而两边邮箱 100% 重合）—— 这里钉住 chatgpt2api 不再犯。
        """
        from services.panel_comparison import fetch_chatgpt2api_remote_accounts

        payload = {
            "items": [
                {
                    "id": "acct_1",
                    "email": "a@example.com",
                    "plan": "free",
                    "status_label": "正常",
                    "last_used_at": 1790961719,
                    "created_at": 1790485432,
                }
            ],
            "total": 1, "page": 1, "page_size": 500,
        }
        with mock.patch("requests.get", return_value=_FakeResponse(payload)):
            rows = fetch_chatgpt2api_remote_accounts(
                api_url="http://c2a.local", api_key="k"
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].platform, "chatgpt")
        self.assertEqual(rows[0].email, "a@example.com")
        self.assertEqual(rows[0].remote_id, "acct_1")
        self.assertEqual(rows[0].status, "正常", "yukkcat 只有 status_label")
        self.assertIsNotNone(rows[0].updated_at, "epoch 秒要能解析")

    def test_credentials_are_exported_for_local_accounts_only(self):
        """yukkcat 的列表不带 token —— 凭证走 export，且只为本地也有的账号抓。

        与 CPA 同一个理由：仅远端的行不参与凭证比对，不值得为它们付导出的钱。
        """
        from services.panel_comparison import fetch_chatgpt2api_remote_accounts

        list_payload = {
            "items": [
                {"id": "acct_local", "email": "local@example.com",
                 "status_label": "正常", "created_at": 1790485432},
                {"id": "acct_remote", "email": "only-remote@example.com",
                 "status_label": "正常", "created_at": 1790485432},
            ],
            "total": 2, "page": 1, "page_size": 500,
        }
        export_calls = []

        def fake_post(url, headers=None, json=None, timeout=None, verify=None):
            export_calls.append(json)
            return _FakeResponse([
                {"management_id": "acct_local", "access_token": "AT-LOCAL",
                 "refresh_token": "RT-LOCAL"},
            ])

        with mock.patch("requests.get", return_value=_FakeResponse(list_payload)), \
             mock.patch("requests.post", side_effect=fake_post):
            rows = fetch_chatgpt2api_remote_accounts(
                api_url="http://c2a.local", api_key="k",
                emails={"local@example.com"},
            )

        self.assertEqual(len(export_calls), 1, "应只发一次 export")
        self.assertEqual(
            export_calls[0]["account_ids"], ["acct_local"],
            "只为本地也有的账号抓凭证 —— 抓多了是白付开销",
        )
        by_email = {r.email: r for r in rows}
        self.assertEqual(by_email["local@example.com"].credentials.get("access_token"), "AT-LOCAL")
        self.assertEqual(by_email["only-remote@example.com"].credentials, {})

    def test_export_400_retries_without_the_missing_ids(self):
        """列表之后有账号被删 → 对端整批 400。去掉缺的重试，别丢整批凭证。"""
        from services.panel_comparison import fetch_chatgpt2api_remote_accounts

        list_payload = {
            "items": [
                {"id": "gone", "email": "gone@example.com",
                 "status_label": "正常", "created_at": 1790485432},
                {"id": "here", "email": "here@example.com",
                 "status_label": "正常", "created_at": 1790485432},
            ],
            "total": 2, "page": 1, "page_size": 500,
        }
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None, verify=None):
            calls.append(list(json["account_ids"]))
            if len(calls) == 1:
                return _FakeResponse(
                    {"detail": {"error": "one or more accounts were not found",
                                "errors": [{"id": "gone", "code": "account_not_found"}]}},
                    status_code=400,
                )
            return _FakeResponse([{"management_id": "here", "access_token": "AT-HERE"}])

        with mock.patch("requests.get", return_value=_FakeResponse(list_payload)), \
             mock.patch("requests.post", side_effect=fake_post):
            rows = fetch_chatgpt2api_remote_accounts(
                api_url="http://c2a.local", api_key="k",
                emails={"gone@example.com", "here@example.com"},
            )

        self.assertEqual(len(calls), 2, "应重试一次")
        self.assertNotIn("gone", calls[1], "重试要去掉缺失的 id")
        by_email = {r.email: r for r in rows}
        self.assertEqual(
            by_email["here@example.com"].credentials.get("access_token"), "AT-HERE",
            "重试后拿到的凭证要写回对应行",
        )

    def test_basketikun_variant_tokens_come_from_the_list(self):
        """basketikun 变体的列表直接带 access_token —— 就地取，不发 export。"""
        from services.panel_comparison import fetch_chatgpt2api_remote_accounts

        list_payload = {
            "items": [
                {"id": "1", "email": "a@example.com", "access_token": "AT-FROM-LIST",
                 "status": "正常", "created_at": "2026-10-02 05:56:43"},
            ],
        }
        with mock.patch("requests.get", return_value=_FakeResponse(list_payload)), \
             mock.patch("requests.post") as post:
            rows = fetch_chatgpt2api_remote_accounts(
                api_url="http://c2a.local", api_key="k",
                emails={"a@example.com"},
            )

        post.assert_not_called()
        self.assertEqual(rows[0].credentials.get("access_token"), "AT-FROM-LIST")
        self.assertEqual(rows[0].platform, "chatgpt")

    def test_missing_url_raises(self):
        from services.panel_comparison import fetch_chatgpt2api_remote_accounts

        with self.assertRaises(RuntimeError):
            fetch_chatgpt2api_remote_accounts(api_url="", api_key="k")

    def test_http_error_raises_with_status(self):
        from services.panel_comparison import fetch_chatgpt2api_remote_accounts

        with mock.patch("requests.get", return_value=_FakeResponse({}, status_code=500)):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_chatgpt2api_remote_accounts(api_url="http://x", api_key="k")
        self.assertIn("500", str(ctx.exception))


class BatchUploadPersistsStatusTests(unittest.TestCase):
    """面板页的「上传未上传」走批量动作 —— 结果要写进 sync_statuses。

    与 sub2api 同形：不写的话界面上永远看不到上传状态（评审发现过同类问题）。
    """

    def test_apply_action_result_records_chatgpt2api(self):
        from api.actions import _apply_action_result

        acct = _account()
        acct.extra = {"access_token": acct.access_token}
        acct.get_extra = lambda: dict(acct.extra)
        acct.set_extra = lambda value: setattr(acct, "extra", dict(value))

        result = {"ok": True, "data": "上传成功"}
        _apply_action_result("chatgpt", "upload_chatgpt2api", acct, result, session=None)

        statuses = acct.extra.get("sync_statuses") or {}
        self.assertIn(
            "chatgpt2api", statuses,
            "批量上传结果没写进 sync_statuses.chatgpt2api",
        )
        self.assertTrue(statuses["chatgpt2api"].get("last_attempt_ok"))


if __name__ == "__main__":
    unittest.main()
