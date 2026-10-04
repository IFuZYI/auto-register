"""面板管理页的面板操作：注册表声明 + 批量动作接线。

用户要求：「CPA 未上传 / Sub2API 未上传 / 同步当前筛选 CLIProxyAPI 状态 这种
都集成到面板管理里面去。」

实现方式：面板注册表声明每个面板对应的平台与动作 id（前后端共用一份契约），
面板管理页按对比结果里的 `local_id` 发起批量动作 —— 对比已经知道哪些账号
没传上去，动作就在同一页发，不用跳到账号列表再筛一遍。
"""

from __future__ import annotations

import unittest
from unittest import mock

from services.panel_registry import PANELS_BY_KEY, list_panels


class PanelActionDeclarationTests(unittest.TestCase):
    """注册表要声明每个面板能跑什么动作。"""

    def test_every_panel_declares_a_platform(self):
        for panel in list_panels():
            with self.subTest(panel=panel["key"]):
                self.assertTrue(
                    str(panel.get("platform") or "").strip(),
                    f"面板 {panel['key']} 没有声明 platform —— 面板管理页发不出动作",
                )

    def test_every_panel_declares_an_upload_action(self):
        """三个面板都有上传动作（CPA / Sub2API / grok2api）。"""
        for panel in list_panels():
            with self.subTest(panel=panel["key"]):
                self.assertTrue(
                    str(panel.get("upload_action") or "").strip(),
                    f"面板 {panel['key']} 没有声明 upload_action",
                )

    def test_declared_actions_exist_on_the_platform(self):
        """声明的动作 id 必须是平台真的实现了的 —— 否则按钮点了报未知操作。"""
        from core.registry import get, load_all

        # 平台注册表是启动时扫出来的（`lifespan` 里调 load_all），
        # 单测里没有 lifespan，得自己加载一次。
        load_all()

        for panel in list_panels():
            platform = panel.get("platform")
            cls = get(platform)
            # 实例化只为读 get_platform_actions（不需要 config）
            instance = cls(config=None)
            available = {a.get("id") for a in instance.get_platform_actions()}

            for field in ("upload_action", "sync_action"):
                action_id = str(panel.get(field) or "").strip()
                if not action_id:
                    continue
                with self.subTest(panel=panel["key"], field=field):
                    self.assertIn(
                        action_id, available,
                        f"面板 {panel['key']} 声明的 {field}={action_id} "
                        f"在 {platform} 上不存在（可用：{sorted(available)}）",
                    )

    def test_cpa_declares_the_sync_action(self):
        """CLIProxyAPI 状态同步是 cpa 面板专有的（用户点名的那个操作）。"""
        self.assertEqual(
            PANELS_BY_KEY["cpa"].get("sync_action"), "sync_cliproxyapi_status"
        )

    def test_sub2api_declares_the_sync_action(self):
        """Sub2API 列表自带权威状态（active/inactive/error）—— 读回来写回本地。

        用户要求「每个平台都最好都有 同步远端状态」。早前声明为空是因为当时
        只想到「拉状态」这件事不存在；实际上列表接口本身就返回状态。
        """
        self.assertEqual(PANELS_BY_KEY["sub2api"].get("sync_action"), "sync_sub2api_status")

    def test_panel_platforms_match_the_comparison_mapping(self):
        """注册表声明的 platform 要与对比模块的面板→平台映射一致。

        两处不一致的后果：对比页显示的是 A 平台的账号，按钮却对 B 平台发动作。
        """
        from services.panel_comparison_cache import _PANEL_PLATFORMS

        for panel in list_panels():
            key = panel["key"]
            if key not in _PANEL_PLATFORMS:
                continue
            with self.subTest(panel=key):
                self.assertIn(
                    panel.get("platform"),
                    _PANEL_PLATFORMS[key],
                    f"面板 {key} 注册表声明 platform={panel.get('platform')}，"
                    f"但对比模块认为它对应 {_PANEL_PLATFORMS[key]}",
                )


class PanelActionContractTests(unittest.TestCase):
    """前端拿得到这些字段（注册表经 `/api/integrations/panels` 原样下发）。"""

    def test_panels_endpoint_exposes_action_fields(self):
        import main as main_mod
        from fastapi.testclient import TestClient

        with TestClient(main_mod.app) as client:
            r = client.get("/api/integrations/panels")
        self.assertEqual(r.status_code, 200, r.text[:200])
        items = r.json()["items"]
        self.assertTrue(items)
        for item in items:
            with self.subTest(panel=item["key"]):
                self.assertIn("platform", item)
                self.assertIn("upload_action", item)
                self.assertIn("sync_action", item)


class PanelScopeTests(unittest.TestCase):
    """面板动作必须在后端声明为 `scope: "panel"`。

    用户要求：「在平台管理中各平台不再有 CPA 这种，这些全部移到面板管理功能区，
    方便管理，平台管理主要管账号就行。」

    账号页的菜单按 `scope` 过滤（`Accounts.tsx` 的 `ActionMenu`），所以这个字段
    是「动作不出现在账号页」的**唯一依据** —— 漏标一个，那个动作就会又冒回账号
    页的菜单里。声明本身要留在后端：面板注册表与批量端点都按动作 id 找它。
    """

    #: 这些动作的目标是外部面板，不该出现在账号页
    PANEL_SCOPED_ACTIONS = {
        "chatgpt": {"upload_cpa", "upload_sub2api", "sync_cliproxyapi_status"},
        "grok": {"upload_cpa", "upload_sub2api", "upload_grok2api"},
    }

    #: 这些动作属于账号本身，必须留在账号页（漏掉它们才是回归）
    ACCOUNT_SCOPED_ACTIONS = {
        "chatgpt": {
            "probe_local_status",
            "check_plus_trial",
            "refresh_token",
            "backfill_refresh_token",
            "bind_2fa",
        },
        "grok": {
            "probe",
            "probe_refresh",
            "refresh_token",
            "refresh_oauth",
            "export_cpa_json",
        },
    }

    def _actions(self, platform: str) -> dict:
        from core.registry import get, load_all

        load_all()
        instance = get(platform)(config=None)
        return {a["id"]: a for a in instance.get_platform_actions()}

    def test_panel_actions_are_marked_with_panel_scope(self):
        for platform, expected in self.PANEL_SCOPED_ACTIONS.items():
            actions = self._actions(platform)
            for action_id in sorted(expected):
                with self.subTest(platform=platform, action=action_id):
                    self.assertIn(action_id, actions, f"{platform} 没有声明 {action_id}")
                    self.assertEqual(
                        actions[action_id].get("scope"),
                        "panel",
                        f"{platform} 的 {action_id} 没有标 scope=panel —— "
                        "它会重新出现在账号页的菜单里",
                    )

    def test_account_actions_stay_on_the_account_page(self):
        """账号自身的动作不能被误标成 panel —— 标错了它们在账号页就消失了。"""
        for platform, expected in self.ACCOUNT_SCOPED_ACTIONS.items():
            actions = self._actions(platform)
            for action_id in sorted(expected):
                with self.subTest(platform=platform, action=action_id):
                    self.assertIn(action_id, actions, f"{platform} 没有声明 {action_id}")
                    self.assertNotEqual(
                        actions[action_id].get("scope"),
                        "panel",
                        f"{platform} 的 {action_id} 是账号动作，不该标 panel",
                    )

    def test_accounts_page_filters_panel_scoped_actions(self):
        """账号页的菜单必须真的按 scope 过滤（不能只靠后端不返回）。"""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1] / "frontend/src/pages/Accounts.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("a?.scope !== 'panel'", src, "账号页菜单没有按 scope 过滤")
        # 过滤必须作用在 `actions`（菜单数据源）上，而不是别的地方。
        # 按源码里的实际形状钉住：`...actions` 紧跟 `.filter((a) => a?.scope !== 'panel')`。
        self.assertIn(
            "...actions\n      .filter((a) => a?.scope !== 'panel')",
            src,
            "过滤没作用在菜单的 actions 数据源上",
        )

    def test_accounts_page_no_longer_has_the_upload_buttons(self):
        """批量上传按钮/状态列/同步菜单项都从账号页拿掉。"""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1] / "frontend/src/pages/Accounts.tsx"
        ).read_text(encoding="utf-8")
        for gone in (
            "handleBatchUploadCpa",
            "handleBatchUploadGrok2api",
            "uploadCpaButtonLabel",
            "runBatchUpload",
            "CliproxySyncSummary",
            "uploadSyncMeta",
        ):
            with self.subTest(symbol=gone):
                self.assertNotIn(gone, src, f"账号页还留着 {gone}")

    def test_panel_page_still_offers_the_batch_actions(self):
        """搬到面板页之后，那三个批量动作必须在面板页有对应按钮。"""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "frontend/src/components/settings/PanelComparisonPanel.tsx"
        ).read_text(encoding="utf-8")
        for needed in ("push-to-remote", "sync-from-remote", "sync-remote"):
            with self.subTest(action=needed):
                self.assertIn(needed, src, f"面板页缺少 {needed} 操作")


class BatchLimitContractTests(unittest.TestCase):
    """面板页的批量动作要能处理超过后端单次上限的账号数。

    后端 `_resolve_batch_accounts` 对 `account_ids` 有 1000 的上限，超了整批
    400 —— 本地账号上千时一个都传不上去。前端必须分批。
    """

    def test_backend_still_enforces_the_limit(self):
        """钉住后端的实际上限值 —— 前端的分批大小要跟它一致。"""
        import inspect

        from api import actions

        src = inspect.getsource(actions._resolve_batch_accounts)
        self.assertIn("1000", src, "后端上限变了？前端的分批大小要跟着改")

    def test_frontend_chunks_before_sending(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "frontend/src/components/settings/PanelComparisonPanel.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("BATCH_ACTION_LIMIT", src, "前端没做分批")
        self.assertIn("chunks", src)
        # 分批大小与后端一致
        self.assertIn("const BATCH_ACTION_LIMIT = 1000", src)

    def test_push_is_disabled_when_remote_is_unavailable(self):
        """远端读不到时禁用「更新远程凭证」。

        此时所有本地账号都退化成 `local_only`（对比拿不到远端那一侧），
        方向判定（未上传 / 本地较新）全部失效 —— 推上去等于把全部本地账号
        重传一遍，新建式面板（sub2api / chatgpt2api）会留下整批重复记录。
        保守做法：直接禁用，等远端恢复再推。
        """
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "frontend/src/components/settings/PanelComparisonPanel.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("remoteUnavailable", src)
        # push 按钮的 disabled 条件必须包含 remoteUnavailable
        push_block = src.split('data-hermes-action="push-to-remote"', 1)[0]
        button_block = push_block.rsplit("<Button", 1)[1]
        self.assertIn(
            "remoteUnavailable", button_block,
            "「更新远程凭证」按钮在远端读取失败时没禁用 —— 会把全部本地账号重传",
        )

    def test_filter_bar_has_no_unreachable_state(self):
        """筛选条不摆不可达的状态（unknown_time 现在永远不是行状态）。"""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "frontend/src/components/settings/PanelComparisonPanel.tsx"
        ).read_text(encoding="utf-8")
        order_block = src.split("const STATE_ORDER = [")[1].split("]")[0]
        self.assertNotIn(
            "'unknown_time'", order_block,
            "unknown_time 不是行状态，摆成筛选项会恒为 0",
        )


class MultiPlatformPanelTests(unittest.TestCase):
    """CPA 面板同时服务 ChatGPT 与 Grok。

    用户要求：「CPA 面板能支持多个平台的，gpt 和 grok 都支持。」

    CLIProxyAPI 本身就同时托管两类凭据（`codex` = ChatGPT OAuth、
    `xai` = Grok），所以这不是硬凑的 —— 缺的是我们这边的接线：
    对比只按邮箱匹配（同邮箱的两个平台账号会压成一行）、远端只取 codex、
    Grok 的 CPA 上传走的是 JSON body（实测 404，必须 multipart）。
    """

    def test_cpa_declares_both_platforms(self):
        panel = PANELS_BY_KEY["cpa"]
        self.assertEqual(sorted(panel.get("platforms") or []), ["chatgpt", "grok"])
        # 兼容字段仍在（老消费方读它们）
        self.assertEqual(panel.get("platform"), "chatgpt")

    def test_cpa_declares_actions_per_platform(self):
        panel = PANELS_BY_KEY["cpa"]
        uploads = panel.get("upload_actions") or {}
        syncs = panel.get("sync_actions") or {}
        self.assertEqual(uploads.get("chatgpt"), "upload_cpa")
        self.assertEqual(uploads.get("grok"), "upload_cpa")
        self.assertEqual(syncs.get("chatgpt"), "sync_cliproxyapi_status")
        # Grok 侧的同步动作也必须存在（两边都支持，不能只挂一半）
        self.assertEqual(syncs.get("grok"), "sync_cliproxyapi_status")

    def test_declared_per_platform_actions_exist(self):
        """按平台声明的动作都要在对应平台上真的实现。"""
        from core.registry import get, load_all

        load_all()
        panel = PANELS_BY_KEY["cpa"]
        for field in ("upload_actions", "sync_actions"):
            for platform, action_id in (panel.get(field) or {}).items():
                with self.subTest(field=field, platform=platform, action=action_id):
                    instance = get(platform)(config=None)
                    available = {a.get("id") for a in instance.get_platform_actions()}
                    self.assertIn(
                        action_id, available,
                        f"CPA 在 {platform} 上声明了 {action_id}，但那个平台没有它",
                    )

    def test_comparison_covers_both_platforms(self):
        from services.panel_comparison_cache import _PANEL_PLATFORMS

        self.assertEqual(sorted(_PANEL_PLATFORMS["cpa"]), ["chatgpt", "grok"])

    def test_same_email_on_both_platforms_makes_two_rows(self):
        """同一个邮箱在两个平台上各有一条时，对比必须产出两行。

        邮箱池按平台消耗 —— 一个地址能注册 ChatGPT 也能注册 Grok。只按邮箱
        匹配会把两条不同的账号压成一行，其中一个平台的账号就永远显示不出来。
        """
        from services.panel_comparison import RemoteAccount, build_comparison

        locals_ = [
            {"id": 1, "email": "same@x.com", "platform": "chatgpt", "status": "registered", "extra": {}},
            {"id": 2, "email": "same@x.com", "platform": "grok", "status": "registered", "extra": {}},
        ]
        remotes = [
            RemoteAccount(email="same@x.com", platform="chatgpt", remote_id="r-gpt"),
            RemoteAccount(email="same@x.com", platform="grok", remote_id="r-grok"),
        ]
        rows = build_comparison(locals_, remotes)
        self.assertEqual(len(rows), 2, f"同邮箱双平台应产出 2 行，实得 {len(rows)}")
        self.assertEqual(
            sorted((r.platform, r.remote_id) for r in rows),
            [("chatgpt", "r-gpt"), ("grok", "r-grok")],
        )

    def test_cross_platform_email_does_not_false_match(self):
        """本地只有 chatgpt，远端只有 grok（同邮箱）→ 不能判成已同步。"""
        from services.panel_comparison import RemoteAccount, build_comparison

        locals_ = [
            {"id": 1, "email": "same@x.com", "platform": "chatgpt", "status": "registered", "extra": {}},
        ]
        remotes = [
            RemoteAccount(email="same@x.com", platform="grok", remote_id="r-grok"),
        ]
        rows = build_comparison(locals_, remotes)
        states = sorted(r.state for r in rows)
        self.assertEqual(
            states, ["local_only", "remote_only"],
            f"跨平台同邮箱不该互相匹配，实得 {states}",
        )

    def test_platform_is_exposed_to_the_frontend(self):
        """对比行要带 `platform` —— 前端按它分发批量动作。"""
        from services.panel_comparison import RemoteAccount, build_comparison

        rows = build_comparison(
            [{"id": 1, "email": "a@x.com", "platform": "grok", "status": "registered", "extra": {}}],
            [],
        )
        self.assertEqual(rows[0].platform, "grok")
        self.assertEqual(rows[0].to_dict().get("platform"), "grok")

    def test_cpa_provider_mapping(self):
        from services.panel_comparison import CPA_PROVIDER_PLATFORMS

        self.assertEqual(CPA_PROVIDER_PLATFORMS["codex"], "chatgpt")
        self.assertEqual(CPA_PROVIDER_PLATFORMS["xai"], "grok")

    def test_cpa_fetch_defaults_to_both_providers(self):
        """CPA 远端拉取缺省要同时取 codex 与 xai（不是只取 codex）。

        行为验证（不只看源码字面）：给两份记录（codex + xai），缺省调用要两条都返回。
        """
        from unittest.mock import patch

        from services.panel_comparison import fetch_cpa_remote_accounts

        files = [
            {"provider": "codex", "email": "gpt@x.com", "name": "gpt@x.com.json", "auth_index": "a"},
            {"provider": "xai", "email": "grok@x.com", "name": "xai-grok@x.com.json", "auth_index": "b"},
            {"provider": "gemini", "email": "other@x.com", "name": "other@x.com.json", "auth_index": "c"},
        ]
        with patch("services.cliproxyapi_sync.list_auth_files", return_value=files):
            out = fetch_cpa_remote_accounts(api_url="http://cpa.test")

        platforms = sorted(r.platform for r in out)
        self.assertEqual(
            platforms, ["chatgpt", "grok"],
            f"缺省应同时取 codex 与 xai（且滤掉其它 provider），实得 {platforms}",
        )


class GrokCpaSyncTests(unittest.TestCase):
    """Grok 账号的 CPA 状态同步：按 xai 记录匹配 + 探活。

    与 ChatGPT 版的差别：匹配 provider 固定 `xai`（不筛的话一个 Grok 账号
    可能匹配到同邮箱的 codex 记录，同步结果全错）。
    """

    def test_match_is_provider_scoped(self):
        from services.cliproxyapi_sync import _match_auth_file

        class Acc:
            email = "same@x.com"

        files = [
            {"provider": "codex", "email": "same@x.com", "name": "same@x.com.json", "auth_index": "gpt"},
            {"provider": "xai", "email": "same@x.com", "name": "xai-same@x.com.json", "auth_index": "grok"},
        ]
        self.assertEqual(_match_auth_file(Acc(), files, provider="codex")["auth_index"], "gpt")
        self.assertEqual(_match_auth_file(Acc(), files, provider="xai")["auth_index"], "grok")

    def test_sync_batch_matches_xai_and_probes(self):
        """同步要匹配 xai 记录并探活；没匹配到报 not_found（不是 unreachable）。"""
        from unittest.mock import patch

        from services.cliproxyapi_sync import sync_grok_cliproxyapi_status_batch

        class Acc:
            id = 7
            email = "g@x.com"

        files = [{"provider": "xai", "email": "g@x.com", "name": "xai-g@x.com.json", "auth_index": "idx-1"}]
        with patch("services.cliproxyapi_sync.list_auth_files", return_value=files), \
             patch("services.cliproxyapi_sync._probe_grok_remote_auth",
                   return_value={"last_probe_status_code": 200, "remote_state": "usable"}):
            results = sync_grok_cliproxyapi_status_batch([Acc()])

        self.assertIn(7, results)
        self.assertTrue(results[7]["uploaded"])
        self.assertEqual(results[7]["remote_state"], "usable")
        self.assertEqual(results[7]["auth_index"], "idx-1")

    def test_sync_batch_reports_not_found(self):
        from unittest.mock import patch

        from services.cliproxyapi_sync import sync_grok_cliproxyapi_status_batch

        class Acc:
            id = 8
            email = "missing@x.com"

        with patch("services.cliproxyapi_sync.list_auth_files", return_value=[]):
            results = sync_grok_cliproxyapi_status_batch([Acc()])
        self.assertFalse(results[8]["uploaded"])
        self.assertEqual(results[8]["remote_state"], "not_found")

    def test_grok_probe_marks_payment_required_as_still_valid(self):
        """402/403（没额度）仍算账号有效 —— 与 ChatGPT 侧同口径。"""
        from unittest.mock import patch

        from services.cliproxyapi_sync import _probe_grok_remote_auth

        with patch("services.cliproxyapi_sync._request_json",
                   return_value={"status_code": 403, "body": '{"code":"personal-team-blocked:spending-limit"}'}):
            out = _probe_grok_remote_auth("idx-1")
        self.assertEqual(out["remote_state"], "payment_required")
        self.assertEqual(out["last_probe_status_code"], 403)

    def test_grok_probe_401_is_invalidated(self):
        from unittest.mock import patch

        from services.cliproxyapi_sync import _probe_grok_remote_auth

        with patch("services.cliproxyapi_sync._request_json",
                   return_value={"status_code": 401, "body": '{"error":"unauthorized"}'}):
            out = _probe_grok_remote_auth("idx-1")
        self.assertEqual(out["remote_state"], "access_token_invalidated")

    def test_grok_action_is_panel_scoped(self):
        """Grok 的同步动作也要标 scope=panel（否则它会冒回账号页菜单）。"""
        from core.registry import get, load_all

        load_all()
        actions = {a["id"]: a for a in get("grok")(config=None).get_platform_actions()}
        self.assertIn("sync_cliproxyapi_status", actions)
        self.assertEqual(actions["sync_cliproxyapi_status"].get("scope"), "panel")


class GrokCpaUploadFormatTests(unittest.TestCase):
    """Grok 的 CPA 上传必须走 multipart。

    实测：原实现发 `{"name":…,"content":…}` 的 JSON body，CLIProxyAPI 的
    `POST /v0/management/auth-files` 没有匹配路由 → 三个候选路径全 404，
    Grok 的 CPA 上传从来没成功过（远端 644 条 auth-file 全是 codex，xai 0 条）。
    """

    def test_upload_uses_multipart(self):
        import inspect

        from platforms.grok import upload as mod

        src = inspect.getsource(mod.upload_to_cpa)
        self.assertIn("CurlMime", src, "必须用 CurlMime 构造 multipart")
        self.assertIn("multipart=mime", src, "必须用 multipart= 参数发出去")
        self.assertNotIn('json=payload', src, "不能再用 JSON body（404）")

    def test_upload_posts_to_the_auth_files_endpoint(self):
        from unittest.mock import MagicMock, patch

        from platforms.grok.upload import upload_to_cpa

        captured = {}

        def _fake_post(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return MagicMock(status_code=200, text='{"status":"ok"}')

        with patch("curl_cffi.requests.post", _fake_post):
            ok, _ = upload_to_cpa(
                {"email": "a@b.com", "type": "xai", "access_token": "at"},
                api_url="http://cpa.test", api_key="k",
            )

        self.assertTrue(ok)
        self.assertEqual(captured["url"], "http://cpa.test/v0/management/auth-files")
        self.assertIn("multipart", captured["kwargs"])
        self.assertEqual(captured["kwargs"]["headers"]["Authorization"], "Bearer k")

    def test_upload_reports_http_errors(self):
        from unittest.mock import MagicMock, patch

        from platforms.grok.upload import upload_to_cpa

        with patch("curl_cffi.requests.post",
                   return_value=MagicMock(status_code=401, text="nope")):
            ok, msg = upload_to_cpa({"email": "a@b.com"}, api_url="http://cpa.test")
        self.assertFalse(ok)
        self.assertIn("401", msg)


class Grok2ApiToggleTests(unittest.TestCase):
    """grok2api 的「启用自动上传」开关。

    用户要求：「grok2api 的配置增加启用自动上传按钮，能手动开关。」
    """

    def test_config_key_is_whitelisted(self):
        """键必须在白名单里 —— 否则 PUT /api/config 会静默丢弃它。"""
        from api.config import CONFIG_KEYS

        self.assertIn("grok2api_enabled", CONFIG_KEYS)

    def test_register_respects_the_switch(self):
        """关掉时注册流程不碰 grok2api。"""
        import inspect

        from platforms.grok import plugin as mod

        src = inspect.getsource(mod.GrokPlatform._register_via_browser)
        self.assertIn('extra.get("grok2api_enabled")', src,
                      "注册流程要读这个开关")

    def test_frontend_has_the_toggle(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "frontend/src/components/settings/PanelConfigPanel.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("grok2api_enabled", src)
        self.assertIn("'grok2api_enabled'", src, "要进 BOOLEAN_KEYS 才会归一成 0/1")
        self.assertIn("label: '启用自动上传'", src)


    def test_platform_tag_uses_contrast_safe_tokens(self):
        """平台标签用调过对比度的 token，不用 antd 预设色。

        `geekblue` 在暗色下是 rgb(82,115,224)，压在自己的浅底上只有 4.16:1
        —— 低于 AA 4.5，对比度门禁实测抓到过。`processing` / `purple` 两个
        预设在本项目 CSS 里都有 token 覆盖（`index.css` 的 tag 段），
        是全站统一达标的那套。
        """
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "frontend/src/components/settings/PanelComparisonPanel.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("PLATFORM_LABELS", src, "平台列要有人话标签")
        self.assertNotIn("'geekblue'", src, "geekblue 暗色下 4.16:1，别用")


class RemotePlatformTagTests(unittest.TestCase):
    """每个 fetcher 产出的 RemoteAccount **必须带 platform**。

    匹配键是 (平台, 邮箱)，而本地行永远带 platform。远端记录不带的话键对不上，
    整个面板会显示成「全部未上传 + 全部仅远端」—— 看起来像功能坏了，其实是
    匹配不上。grok2api 面板实测踩过：22 个本地账号全部显示未上传，而两边
    邮箱 100% 重合。

    这个类遍历 `FETCHERS` 做**通用**断言（不是逐面板点名），以后新增面板
    漏带 platform 会直接变红。
    """

    def test_every_fetcher_declares_a_platform_on_its_rows(self):
        from services.panel_comparison import FETCHERS
        from services.panel_comparison_cache import _PANEL_PLATFORMS as CACHE_MAP

        # 两个映射必须一致（注册表测试只覆盖了声明字段那一侧）
        self.assertEqual(
            set(FETCHERS), set(CACHE_MAP),
            "FETCHERS 与 _PANEL_PLATFORMS 的面板 key 集合不一致",
        )
        for key, platforms in CACHE_MAP.items():
            with self.subTest(panel=key):
                self.assertTrue(platforms, f"面板 {key} 没有声明本地平台")
                # 单平台面板：fetcher 必须给行打上那个平台
                if len(platforms) == 1:
                    expected = platforms[0]
                    rows = self._rows_for(key)
                    self.assertTrue(rows, f"{key} 的 fetcher 没产出任何行")
                    for row in rows:
                        self.assertEqual(
                            row.platform, expected,
                            f"{key} 的远端行 platform={row.platform!r}，"
                            f"应为 {expected!r} —— 不带平台就永远匹配不上本地行",
                        )

    def _rows_for(self, panel_key: str):
        """用假响应喂每个 fetcher，拿到它产出的行。"""
        from services import panel_comparison as pc

        if panel_key == "grok2api":
            class _Client:
                def login(self, **kw):
                    return "t"

                def _request(self, method, path, headers=None, timeout=None):
                    class _R:
                        status_code = 200

                        @staticmethod
                        def json():
                            return {"data": {"items": [{
                                "id": "1", "email": "x@example.com", "enabled": True,
                                "authStatus": "active",
                                "lastUsedAt": "2026-10-02T05:56:43+08:00",
                            }]}}

                    return _R()

                def _auth_headers(self, content_type=""):
                    return {}

            with mock.patch(
                "platforms.grok.grok2api.Grok2ApiClient.from_config",
                return_value=_Client(),
            ):
                return pc.fetch_grok2api_remote_accounts(api_url="http://x", api_key="k")

        if panel_key in ("sub2api", "chatgpt2api"):
            class _R:
                status_code = 200
                content = b"{}"

                @staticmethod
                def json():
                    return {"items": [{
                        "id": "1", "email": "x@example.com", "name": "x@example.com",
                        "status": "active", "platform": "openai",
                        "created_at": "2026-10-02T05:56:43Z",
                    }]}

            fetcher = pc.FETCHERS[panel_key]
            with mock.patch("requests.get", return_value=_R()):
                return fetcher(api_url="http://x", api_key="k")

        if panel_key == "cpa":
            with mock.patch(
                "services.cliproxyapi_sync.list_auth_files",
                return_value=[{
                    "provider": "codex", "email": "x@example.com",
                    "name": "x", "auth_index": "1",
                    "last_refresh": "2026-10-02T05:56:43Z",
                }],
            ):
                return pc.fetch_cpa_remote_accounts(api_url="http://x", api_key="k")

        self.fail(f"测试没有为面板 {panel_key} 准备假响应 —— 新增面板时补上")


if __name__ == "__main__":
    unittest.main()
