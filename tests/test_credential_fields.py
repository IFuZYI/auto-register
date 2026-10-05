"""凭证字段注册表（`core/credential_fields.py`）的契约。

背景（用户要求）：「有 sso_token AT RT 这些 请好好思考、整理数据结构。」

整理前的乱象（实测）：
- **六处各自维护字段表**：`panel_comparison.CREDENTIAL_FIELDS`（5 字段）、
  `panel_sync._PULLABLE_FIELDS`（4）、`panel_push._PUSHABLE_FIELDS`（4）、
  `account_export` 的导出字段（4，缺 sso）、`api/accounts._IMPORT_EXTRA_KEYS`
  （5，缺 sso）、`api/actions.tracked_keys`（混 camelCase + 三个零生产方的死键）；
- **sso 能同步但不能导出**：grok 账号 JSON 往返丢 SSO（真 bug）；
- **session_token 参与对比但不参与同步**（说不清是故意还是漏了）；
- **token 列语义混乱**：grok 注册写 SSO、AT 刷新又把它盖成 AT（32 行里 12 行
  是 AT），读侧 `or account.token` 的兜底会拿错值。

整理后：所有消费方都从注册表取字段表；token 列定义为「平台主凭证的镜像」
（chatgpt → AT，grok → SSO），写路径与迁移都按这个口径收敛。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class RegistryShapeTests(unittest.TestCase):
    """注册表本身的形状：字段名、别名、标签。"""

    def test_canonical_fields(self):
        from core.credential_fields import CREDENTIAL_FIELDS

        names = [f.name for f in CREDENTIAL_FIELDS]
        self.assertEqual(
            names,
            ["access_token", "refresh_token", "session_token", "id_token", "sso"],
            "凭证字段的规范名集合变了 —— 各消费方都从这里取，改这里要全量对账",
        )

    def test_every_field_has_camel_and_snake_aliases(self):
        """每个字段至少认两种拼写：蛇形（规范）+ camelCase。

        `sso` 的别名是 `sso` / `sso_token`（面板导出格式用的是后者）。
        """
        from core.credential_fields import CREDENTIAL_FIELDS

        expected = {
            "access_token": ("access_token", "accessToken"),
            "refresh_token": ("refresh_token", "refreshToken"),
            "id_token": ("id_token", "idToken"),
            "session_token": ("session_token", "sessionToken"),
            "sso": ("sso", "sso_token"),
        }
        for field in CREDENTIAL_FIELDS:
            self.assertEqual(
                field.aliases,
                expected[field.name],
                f"{field.name} 的别名表变了（读侧兼容范围会跟着变）",
            )
            self.assertEqual(field.aliases[0], field.name, "规范名必须排第一位（读取优先级）")

    def test_no_alias_collisions(self):
        """一个别名只能属于一个字段 —— 撞名会让读取结果取决于遍历顺序。"""
        from core.credential_fields import CREDENTIAL_FIELDS

        seen: dict[str, str] = {}
        for field in CREDENTIAL_FIELDS:
            for alias in field.aliases:
                self.assertNotIn(
                    alias, seen,
                    f"别名 {alias!r} 同时属于 {seen.get(alias)} 与 {field.name}",
                )
                seen[alias] = field.name

    def test_fields_have_short_labels(self):
        """字段带短标签（AT / RT …），对比页的差异展示用它们。"""
        from core.credential_fields import CREDENTIAL_FIELDS

        labels = {f.name: f.label for f in CREDENTIAL_FIELDS}
        self.assertEqual(labels["access_token"], "AT")
        self.assertEqual(labels["refresh_token"], "RT")
        self.assertEqual(labels["sso"], "SSO")
        for name, label in labels.items():
            self.assertTrue(label, f"{name} 没有标签")


class ScopeTests(unittest.TestCase):
    """用途开关：对比 / 同步 / 导出。"""

    def test_compare_scope_is_all_fields(self):
        from core.credential_fields import compare_aliases

        self.assertEqual(len(compare_aliases()), 5)

    def test_sync_scope_matches_compare_for_now(self):
        """同步范围与对比范围当前一致（session_token 也纳入同步）。

        整理前 session_token 只参与对比不参与同步 —— 那种「能比出来却没法
        同步」的半截状态说不清是故意还是漏了。统一规则：凭证字段全量参与
        对比与同步；面板实际消费哪些由各上传器决定。
        """
        from core.credential_fields import compare_aliases, sync_aliases

        self.assertEqual(sync_aliases(), compare_aliases())

    def test_export_scope_includes_sso(self):
        """导出必须包含 sso —— grok 账号 JSON 往返丢 SSO 是整理前实测的 bug。"""
        from core.credential_fields import export_names

        self.assertIn("sso", export_names(), "导出字段缺 sso：grok 账号往返会丢 SSO")

    def test_export_scope_is_all_credential_fields(self):
        from core.credential_fields import export_names

        self.assertEqual(
            set(export_names()),
            {"access_token", "refresh_token", "id_token", "session_token", "sso"},
        )


class HelperTests(unittest.TestCase):
    """取值 / 写值助手。"""

    def test_first_present_prefers_canonical(self):
        from core.credential_fields import first_present, field_aliases

        aliases = field_aliases("access_token")
        self.assertEqual(
            first_present({"access_token": "snake", "accessToken": "camel"}, aliases),
            "snake",
            "两处都有值时规范名优先",
        )
        self.assertEqual(first_present({"accessToken": "camel"}, aliases), "camel")
        self.assertEqual(first_present({}, aliases), "")
        self.assertEqual(first_present({"access_token": "  "}, aliases), "")

    def test_get_credential_reads_aliases(self):
        from core.credential_fields import get_credential

        self.assertEqual(get_credential({"sso_token": "s1"}, "sso"), "s1")
        self.assertEqual(get_credential({"refreshToken": "r1"}, "refresh_token"), "r1")
        self.assertEqual(get_credential({}, "access_token"), "")

    def test_canonical_writes_normalizes_to_snake_case(self):
        from core.credential_fields import canonical_writes

        writes = canonical_writes({"accessToken": "a1", "refreshToken": "r1", "other": "x"})
        self.assertEqual(
            writes,
            {"access_token": "a1", "refresh_token": "r1"},
            "写侧要归一到规范名、且只收凭证字段",
        )

    def test_canonical_writes_skips_blank(self):
        from core.credential_fields import canonical_writes

        self.assertEqual(canonical_writes({"access_token": "  ", "sso": ""}), {})


class TokenColumnTests(unittest.TestCase):
    """token 列 = 平台主凭证的镜像（历史遗留列）。"""

    def test_platform_mapping(self):
        from core.credential_fields import token_column_field

        self.assertEqual(token_column_field("chatgpt"), "access_token")
        self.assertEqual(token_column_field("grok"), "sso")
        self.assertEqual(token_column_field("Grok"), "sso", "平台名大小写不敏感")
        self.assertEqual(token_column_field("unknown"), "")

    def test_every_panel_platform_is_mapped(self):
        """面板覆盖的平台都必须在映射表里 —— 漏了的话镜像逻辑会静默不生效。"""
        from core.credential_fields import token_column_field
        from services.panel_comparison_cache import _PANEL_PLATFORMS

        for platforms in _PANEL_PLATFORMS.values():
            for platform in platforms:
                self.assertTrue(
                    token_column_field(platform),
                    f"平台 {platform} 没有 token 列映射 —— 面板凭证合并会拿错字段",
                )

    def test_sync_token_column_writes_platform_mirror(self):
        from core.credential_fields import sync_token_column

        class _Row:
            token = ""

        chatgpt = _Row()
        written = sync_token_column(chatgpt, "chatgpt", {"access_token": "at-1", "sso": "s1"})
        self.assertEqual(written, "access_token")
        self.assertEqual(chatgpt.token, "at-1")

        grok = _Row()
        written = sync_token_column(grok, "grok", {"access_token": "at-1", "sso": "s1"})
        self.assertEqual(written, "sso", "grok 的镜像字段是 sso —— AT 不许再盖进 token 列")
        self.assertEqual(grok.token, "s1")

    def test_sync_token_column_noop_without_value(self):
        from core.credential_fields import sync_token_column

        class _Row:
            token = "keep"

        row = _Row()
        self.assertEqual(sync_token_column(row, "grok", {"access_token": "at"}), "")
        self.assertEqual(row.token, "keep", "没有镜像值时不碰列")
        self.assertEqual(sync_token_column(row, "unknown", {"sso": "s"}), "")


class DerivedListTests(unittest.TestCase):
    """三个消费方的字段表必须与注册表同源（之前各自维护、已经漂移）。"""

    def test_panel_lists_come_from_registry(self):
        from core.credential_fields import compare_aliases, sync_aliases
        from services import panel_comparison, panel_push, panel_sync

        self.assertEqual(panel_comparison.CREDENTIAL_FIELDS, compare_aliases())
        self.assertEqual(panel_sync._PULLABLE_FIELDS, sync_aliases())
        self.assertEqual(panel_push._PUSHABLE_FIELDS, sync_aliases())

    def test_first_present_is_shared(self):
        """三个面板模块不再各写一份 `_first_present`。"""
        import inspect

        from services import panel_comparison, panel_push, panel_sync

        for mod in (panel_comparison, panel_sync, panel_push):
            src = inspect.getsource(mod)
            self.assertNotIn(
                "def _first_present",
                src,
                f"{mod.__name__} 还留着自己的 _first_present —— 统一用注册表的",
            )


class ImportExportWiringTests(unittest.TestCase):
    """导出/导入的字段表接入注册表。"""

    def test_import_keys_include_sso(self):
        from api.accounts import _IMPORT_EXTRA_KEYS

        self.assertIn("sso", _IMPORT_EXTRA_KEYS, "JSON 导入缺 sso：grok 账号往返会丢 SSO")

    def test_export_fields_include_sso(self):
        from services.account_export import EXPORT_FIELDS, _render_json

        self.assertIn("sso", EXPORT_FIELDS, "导出字段表缺 sso")
        # _render_json 的字段清单也要带上（渲染时才真正写出）
        import inspect

        src = inspect.getsource(_render_json)
        self.assertIn('"sso"', src, "_render_json 没有输出 sso")

    def test_tracked_keys_are_registry_driven(self):
        """动作结果落库（api/actions）不再用手写集合 —— 走注册表归一。"""
        import inspect

        from api import actions

        src = inspect.getsource(actions._apply_action_result)
        self.assertNotIn(
            "tracked_keys", src,
            "还留着旧的 tracked_keys 手写集合 —— 字段口径会与注册表漂移",
        )
        self.assertNotIn(
            "extra.update(data)", src,
            "动作结果还在整包写进 extra —— 展示字段（message/status）不该落库",
        )
        self.assertIn(
            "canonical_writes", src,
            "动作结果落库没走注册表的 canonical_writes",
        )


class SsoRoundTripTests(unittest.TestCase):
    """grok 账号的 JSON 导出 → 导入必须保住 SSO（整理前的真 bug）。"""

    def test_sso_survives_round_trip(self):
        from core.db.models_account import AccountModel
        from services.account_export import _render_json
        import json

        model = AccountModel(
            platform="grok", email="sso-roundtrip@example.com", password="p",
        )
        model.set_extra({
            "sso": "sso-value-1",
            "access_token": "at-1",
            "refresh_token": "rt-1",
        })
        exported = json.loads(_render_json([model]))
        self.assertEqual(
            exported[0].get("sso"), "sso-value-1",
            "导出的 JSON 里没有 sso —— grok 账号换机器导入就丢 SSO",
        )


class ChatgptSyncAliasTests(unittest.TestCase):
    """`build_chatgpt_sync_account` 要认 camelCase 别名（之前只读蛇形）。"""

    def test_reads_camel_case_refresh_token(self):
        from services.chatgpt_sync import build_chatgpt_sync_account

        class _Account:
            email = "a@x.com"
            user_id = ""
            token = ""

            def get_extra(self):
                return {"accessToken": "at-c", "refreshToken": "rt-c", "idToken": "id-c"}

        obj = build_chatgpt_sync_account(_Account())
        self.assertEqual(obj.access_token, "at-c")
        self.assertEqual(obj.refresh_token, "rt-c", "camelCase 的 RT 被漏读了")
        self.assertEqual(obj.id_token, "id-c")


def _fresh_engine(tmp: str, name: str):
    """建一个只含账号表的新库（与生产 create_all 同源）。"""
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    from core.db.models_account import ACCOUNT_TABLES

    engine = create_engine(f"sqlite:///{Path(tmp) / name}")
    SQLModel.metadata.create_all(engine, tables=ACCOUNT_TABLES)
    return engine


def _insert_row(engine, platform: str, email: str, token: str, extra: dict) -> None:
    import json

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO accounts (platform, email, password, user_id, region, "
            "token, status, cashier_url, extra_json, created_at, updated_at) "
            "VALUES (?, ?, 'p', '', '', ?, 'registered', '', ?, "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (platform, email, token, json.dumps(extra)),
        )


class TokenColumnMigrationTests(unittest.TestCase):
    """启动迁移把 grok 的 token 列修回 SSO 镜像（整理前被刷新路径盖成 AT）。

    线上实测（整理前）：32 个 grok 账号里 12 个 token 列是 AT、20 个是 SSO ——
    同一个列在不同代码路径被当两种东西用。整理后 token 列 = 平台主凭证镜像
    （grok → sso），迁移把「等于 extra.access_token 且与 extra.sso 不符」的
    行修回 SSO。
    """

    def test_grok_row_overwritten_by_at_is_restored(self):
        import tempfile

        from core.db.migrations import _normalize_grok_token_column

        with tempfile.TemporaryDirectory() as tmp:
            engine = _fresh_engine(tmp, "grok-token.db")
            _insert_row(
                engine, "grok", "a@x.ai", "AT-OLD",
                {"sso": "SSO-1", "access_token": "AT-OLD"},
            )
            _normalize_grok_token_column(engine)
            with engine.begin() as conn:
                token = conn.exec_driver_sql(
                    "SELECT token FROM accounts WHERE email='a@x.ai'"
                ).fetchone()[0]
            self.assertEqual(token, "SSO-1", "被 AT 盖掉的 token 列应修回 SSO")

    def test_correct_and_foreign_rows_are_untouched(self):
        import tempfile

        from core.db.migrations import _normalize_grok_token_column

        with tempfile.TemporaryDirectory() as tmp:
            engine = _fresh_engine(tmp, "grok-token2.db")
            # 已经是 SSO 镜像 → 不动
            _insert_row(engine, "grok", "ok@x.ai", "SSO-2", {"sso": "SSO-2", "access_token": "AT"})
            # chatgpt 的 token 列语义就是 AT → 不动
            _insert_row(engine, "chatgpt", "c@x.ai", "AT-C", {"access_token": "AT-C"})
            # token 列既不是 sso 也不是 AT（来历不明）→ 不乱动
            _insert_row(engine, "grok", "weird@x.ai", "WEIRD", {"sso": "SSO-3"})
            _normalize_grok_token_column(engine)
            with engine.begin() as conn:
                rows = {
                    row[0]: row[1]
                    for row in conn.exec_driver_sql(
                        "SELECT email, token FROM accounts"
                    ).fetchall()
                }
            self.assertEqual(rows["ok@x.ai"], "SSO-2")
            self.assertEqual(rows["c@x.ai"], "AT-C")
            self.assertEqual(rows["weird@x.ai"], "WEIRD")

    def test_migration_is_idempotent(self):
        import tempfile

        from core.db.migrations import _normalize_grok_token_column

        with tempfile.TemporaryDirectory() as tmp:
            engine = _fresh_engine(tmp, "grok-token3.db")
            _insert_row(engine, "grok", "a@x.ai", "AT-OLD", {"sso": "SSO-1", "access_token": "AT-OLD"})
            for _ in range(3):
                _normalize_grok_token_column(engine)
            with engine.begin() as conn:
                token = conn.exec_driver_sql(
                    "SELECT token FROM accounts WHERE email='a@x.ai'"
                ).fetchone()[0]
            self.assertEqual(token, "SSO-1")

    def test_wired_into_startup_migrations(self):
        """迁移必须接进启动链 —— 函数写好了但没接线等于没做。"""
        import inspect

        from core.db.migrations import _run_one

        src = inspect.getsource(_run_one)
        self.assertIn(
            "_normalize_grok_token_column(engine)",
            src,
            "_run_one 没有调用 _normalize_grok_token_column —— 老库不会修",
        )


if __name__ == "__main__":
    unittest.main()
