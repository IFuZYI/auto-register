"""平台面板的三件新增能力（2026-10-30 用户要求）。

1. **ChatGPT2API 的「导入内容」**：`atrt` 只推 AT/RT，`full` 推完全账户
   （账号密码 + AT/RT + 2FA）。用户原话：「ChatGPT2API 面板配置同步增加导入
   设置，支持仅 ATRT 和完全账户（账号密码 ATRT2FA）」。
2. **四个面板都支持「上传代理」**：账号绑定哪条代理，面板就绑哪条。
   用户原话：「平台都支持上传代理功能（账号对应绑定代理）」。
3. **每个面板都有「面板与本地同步」的自动维护**，对应自动维护子菜单里的卡片。

这三件都必须**行为级验证**：`assertIn("password", 源码)` 这类断言挡不住
「写进去了但被一个 if 挡住」—— 本项目已经因为字面量断言吃过亏。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from platforms.chatgpt import chatgpt2api_upload as c2a
from services import panel_proxy_binding as ppb


def _account(**extra):
    """一个够用的假账号：AT 是合法 JWT（payload 能解出 email / plan）。"""
    import base64
    import json

    def seg(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    at = ".".join(
        [
            seg({"alg": "none"}),
            seg(
                {
                    "email": "a@example.com",
                    "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
                }
            ),
            "sig",
        ]
    )
    return SimpleNamespace(
        email="a@example.com",
        access_token=at,
        token=at,
        refresh_token="rt-value",
        id_token="idt-value",
        password="pw-value",
        plan_type="plus",
        extra={"totp_secret": "TOTPSECRET", **extra},
    )


class _ConfigPatcher:
    """把 `config_store.get` 换成一张表。"""

    def __init__(self, values: dict[str, str]):
        self.values = values

    def __enter__(self):
        from core import config_store as module

        self._orig = module.config_store.get
        values = self.values

        def fake_get(key, default=None):
            return values.get(key, default if default is not None else "")

        module.config_store.get = fake_get
        return self

    def __exit__(self, *exc):
        from core import config_store as module

        module.config_store.get = self._orig


class ChatGPT2ApiImportModeTests(unittest.TestCase):
    """`chatgpt2api_import_mode` 决定推不推密码与 2FA 密钥。"""

    def _build(self, mode: str) -> dict:
        with _ConfigPatcher({"chatgpt2api_import_mode": mode}):
            return c2a.build_chatgpt2api_account(_account())

    def test_full_mode_sends_password_and_totp(self):
        item = self._build("full")
        self.assertEqual(item.get("password"), "pw-value")
        self.assertEqual(item.get("totp_secret"), "TOTPSECRET")
        self.assertIn("refresh_token", item)

    def test_atrt_mode_omits_the_keys_entirely(self):
        """`atrt` 下**键都不出现**，不是置空。

        对端对已存在的账号做 `merged[k] = v` —— 传空串会抹掉对端已有的密码、
        让开着 2FA 的号永久锁死。所以「不发这个键」才是唯一的正确写法。
        """
        item = self._build("atrt")
        self.assertNotIn("password", item, "仅 AT/RT 模式不该下发密码（空值会抹掉对端的）")
        self.assertNotIn("totp_secret", item, "仅 AT/RT 模式不该下发 2FA 密钥")
        # AT / RT 仍然要在 —— 不然「仅 AT/RT」就名不副实
        self.assertIn("access_token", item)
        self.assertIn("refresh_token", item)

    def test_atrt_mode_does_not_lose_access_token(self):
        item = self._build("atrt")
        self.assertTrue(str(item["access_token"]).count(".") == 2)

    def test_unknown_or_empty_mode_falls_back_to_full(self):
        """空串 / 未知值回落 `full` —— 升级不改变既有部署的推送内容。"""
        for value in ("", "  ", "whatever"):
            with self.subTest(value=value):
                item = self._build(value)
                self.assertIn("password", item, f"{value!r} 应该按 full 处理")

    def test_config_read_failure_keeps_full(self):
        """读配置抛异常时仍按 full：宁可多推一个字段，也不要静默降级。"""
        from core import config_store as module

        original = module.config_store.get
        try:
            module.config_store.get = mock.Mock(side_effect=RuntimeError("boom"))
            item = c2a.build_chatgpt2api_account(_account())
        finally:
            module.config_store.get = original
        self.assertIn("password", item)


class ParseProxyUrlTests(unittest.TestCase):
    def test_splits_socks5h_with_credentials(self):
        got = ppb.parse_proxy_url("socks5h://user:p%40ss@1.2.3.4:6740")
        self.assertEqual(
            got,
            {"protocol": "socks5h", "host": "1.2.3.4", "port": 6740, "username": "user", "password": "p@ss"},
        )

    def test_splits_without_credentials(self):
        got = ppb.parse_proxy_url("http://1.2.3.4:8080")
        self.assertEqual(got["username"], "")
        self.assertEqual(got["protocol"], "http")

    def test_rejects_garbage(self):
        for bad in ("", "not a url", "socks4://1.2.3.4:1", "http://host-without-port"):
            with self.subTest(bad=bad):
                self.assertIsNone(ppb.parse_proxy_url(bad), f"{bad!r} 不该被解析成代理")


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class EnsureSub2ApiProxyTests(unittest.TestCase):
    """Sub2API 的账号只认 `proxy_id`，所以要先确保面板里有那条代理。"""

    def test_reuses_existing_proxy(self):
        listing = _FakeResp(payload={"data": {"items": [
            {"id": 17, "host": "1.2.3.4", "port": 6740, "username": "user"},
        ]}})
        with mock.patch("curl_cffi.requests.get", return_value=listing) as get, \
             mock.patch("curl_cffi.requests.post") as post:
            proxy_id, note = ppb.ensure_sub2api_proxy(
                "https://panel", "key", "socks5h://user:pw@1.2.3.4:6740"
            )
        self.assertEqual(proxy_id, 17)
        self.assertIn("复用", note)
        post.assert_not_called(), "已有代理时不该再建一条"

    def test_creates_when_missing(self):
        listing = _FakeResp(payload={"items": []})
        created = _FakeResp(status_code=201, payload={"data": {"id": 42}})
        with mock.patch("curl_cffi.requests.get", return_value=listing), \
             mock.patch("curl_cffi.requests.post", return_value=created) as post:
            proxy_id, note = ppb.ensure_sub2api_proxy(
                "https://panel", "key", "socks5h://user:pw@1.2.3.4:6740"
            )
        self.assertEqual(proxy_id, 42)
        self.assertIn("已创建", note)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["host"], "1.2.3.4")
        self.assertEqual(body["port"], 6740)
        # socks5h 是 Sub2API 的合法枚举值之一（binding 里列了）
        self.assertEqual(body["protocol"], "socks5h")

    def test_same_host_different_username_is_a_different_proxy(self):
        """幂等键含用户名但**不含密码**：换密码还是同一条，换账号是另一条。"""
        listing = _FakeResp(payload={"items": [
            {"id": 17, "host": "1.2.3.4", "port": 6740, "username": "other"},
        ]})
        created = _FakeResp(status_code=201, payload={"data": {"id": 99}})
        with mock.patch("curl_cffi.requests.get", return_value=listing), \
             mock.patch("curl_cffi.requests.post", return_value=created):
            proxy_id, _ = ppb.ensure_sub2api_proxy(
                "https://panel", "key", "socks5h://user:pw@1.2.3.4:6740"
            )
        self.assertEqual(proxy_id, 99, "用户名不同应该建新记录，而不是复用别的出口账号")

    def test_failure_returns_none_not_raises(self):
        with mock.patch("curl_cffi.requests.get", return_value=_FakeResp(payload={})), \
             mock.patch("curl_cffi.requests.post", side_effect=RuntimeError("net down")):
            proxy_id, note = ppb.ensure_sub2api_proxy(
                "https://panel", "key", "socks5h://user:pw@1.2.3.4:6740"
            )
        self.assertIsNone(proxy_id)
        self.assertIn("异常", note)


class UploadProxySwitchCoverageTests(unittest.TestCase):
    """四个面板都要有「上传代理」开关，且开关键真在配置白名单里。"""

    def test_all_four_panels_have_a_switch(self):
        from services.chatgpt_sync import _UPLOAD_PROXY_SWITCHES

        self.assertEqual(
            set(_UPLOAD_PROXY_SWITCHES),
            {"cpa", "chatgpt2api", "sub2api", "grok2api"},
        )

    def test_every_switch_key_is_in_the_config_schema(self):
        """开关键漏进白名单 → 界面能点、保存被丢弃。"""
        from core.config_schema import CONFIG_KEYS
        from services.chatgpt_sync import _UPLOAD_PROXY_SWITCHES

        for panel, key in _UPLOAD_PROXY_SWITCHES.items():
            with self.subTest(panel=panel):
                self.assertIn(key, CONFIG_KEYS, f"{key} 不在配置白名单里，保存会被静默丢弃")

    def test_switch_off_returns_empty_proxy(self):
        from services.chatgpt_sync import upload_proxy_for

        with _ConfigPatcher({"sub2api_upload_proxy_enabled": "0"}):
            self.assertEqual(
                upload_proxy_for("sub2api", {"register_proxy": "socks5h://u:p@h:1"}),
                "",
            )

    def test_switch_on_returns_the_bound_proxy(self):
        from services.chatgpt_sync import upload_proxy_for

        with _ConfigPatcher({"sub2api_upload_proxy_enabled": "1"}):
            self.assertEqual(
                upload_proxy_for("sub2api", {"register_proxy": "socks5h://u:p@h:1"}),
                "socks5h://u:p@h:1",
            )

    def test_account_without_proxy_takes_no_action(self):
        from services.chatgpt_sync import upload_proxy_for

        with _ConfigPatcher({"grok2api_upload_proxy_enabled": "1"}):
            self.assertEqual(upload_proxy_for("grok2api", {}), "")


class PanelAutoSyncRegistryTests(unittest.TestCase):
    """自动维护子菜单的「每个平台一张卡」背后都必须有一个真的任务。"""

    def test_every_panel_has_a_sync_entry(self):
        from services.chatgpt_maintenance import PANEL_AUTO_SYNC

        self.assertEqual(
            set(PANEL_AUTO_SYNC),
            {"chatgpt2api", "sub2api", "cpa", "grok2api"},
        )

    def test_switch_key_exists_and_is_boolean_normalized_anywhere(self):
        from core.config_schema import CONFIG_KEYS
        from services.chatgpt_maintenance import PANEL_AUTO_SYNC

        for panel, (switch_key, _platform, _delete, _source) in PANEL_AUTO_SYNC.items():
            with self.subTest(panel=panel):
                self.assertIn(switch_key, CONFIG_KEYS)

    def test_interval_is_zero_when_switch_off(self):
        from services.chatgpt_maintenance import get_panel_auto_sync_interval_seconds

        with _ConfigPatcher({"cpa_auto_sync_enabled": "0", "cpa_api_url": "http://panel"}):
            self.assertEqual(get_panel_auto_sync_interval_seconds("cpa"), 0)

    def test_interval_is_zero_when_panel_url_missing(self):
        """开关开着但地址空着 → 不调度。

        否则每 10 分钟跑一轮、每轮在任务历史里留一条「未配置」，
        真正要看的历史被淹掉。
        """
        from services.chatgpt_maintenance import get_panel_auto_sync_interval_seconds

        with _ConfigPatcher({"cpa_auto_sync_enabled": "1", "cpa_api_url": ""}):
            self.assertEqual(get_panel_auto_sync_interval_seconds("cpa"), 0)

    def test_interval_is_positive_when_enabled_and_configured(self):
        from services.chatgpt_maintenance import get_panel_auto_sync_interval_seconds

        with _ConfigPatcher({"cpa_auto_sync_enabled": "1", "cpa_api_url": "http://panel"}):
            self.assertGreater(get_panel_auto_sync_interval_seconds("cpa"), 0)

    def test_unknown_panel_is_not_scheduled(self):
        from services.chatgpt_maintenance import get_panel_auto_sync_interval_seconds

        self.assertEqual(get_panel_auto_sync_interval_seconds("nope"), 0)

    def test_delete_old_only_for_append_style_panels(self):
        """新建式面板（sub2api / chatgpt2api）必须删旧记录；覆盖式不必。"""
        from services.chatgpt_maintenance import PANEL_AUTO_SYNC

        for panel in ("sub2api", "chatgpt2api"):
            self.assertTrue(PANEL_AUTO_SYNC[panel][2], f"{panel} 是新建式面板，必须删旧记录")
        for panel in ("cpa", "grok2api"):
            self.assertFalse(PANEL_AUTO_SYNC[panel][2], f"{panel} 是覆盖式面板，删旧记录是白跑")


class PanelLabelCaseTests(unittest.TestCase):
    """平台名要大写：`ChatGPT2API` / `Grok2API`（用户明确要求）。"""

    def test_labels_are_capitalised(self):
        from services.panel_registry import PANELS

        labels = {p["key"]: p["label"] for p in PANELS}
        self.assertEqual(labels["chatgpt2api"], "ChatGPT2API")
        self.assertEqual(labels["grok2api"], "Grok2API")

    def test_keys_are_still_lowercase_contract(self):
        """**只改显示名，不动 key** —— key 是 API 与前端动作 id 的公开契约。"""
        from services.panel_registry import PANELS

        keys = {p["key"] for p in PANELS}
        self.assertIn("chatgpt2api", keys)
        self.assertIn("grok2api", keys)


if __name__ == "__main__":
    unittest.main()
