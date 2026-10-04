"""数据备份 / 迁移包（services/data_bundle.py + api/backup.py）。

覆盖：导出结构、round-trip 恢复、密钥恢复、篡改检测、zip-slip、
版本门禁、运行中任务拒绝、导入前自动备份。
"""

from __future__ import annotations

import io
import json
import sqlite3
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from services.data_bundle import (
    BUNDLE_FORMAT_VERSION,
    DEFAULT_DB_NAME,
    MANIFEST_NAME,
    BundleError,
    apply_import_bundle,
    build_export_bundle,
)


def _add_account(email: str, token: str) -> None:
    from core.db import account_repository
    from core.db.models_account import AccountModel

    account_repository.upsert(
        AccountModel(
            platform="chatgpt",
            email=email,
            password="pw",
            status="registered",
            extra_json=json.dumps({"access_token": token}),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )


def _emails() -> list[str]:
    from core.db import account_repository

    return sorted(row.email for row in account_repository.list_all_accounts())


class _HermeticBundleTest(unittest.TestCase):
    """隔离共享的任务存储状态。

    全量跑时 `api.tasks._task_store` 是**跨测试文件共享**的单例，别的测试可能
    留下 pending/running 记录 —— 那时导入守卫会（正确地）拒绝，而与本类的
    测试意图无关。这里统一把它当成「没有活跃任务」，守卫本身由
    :class:`ActiveTaskGuardTests` 专门验证。
    """

    def setUp(self):
        import services.data_bundle as bundle_mod

        self._orig_has_active = bundle_mod._has_active_tasks
        bundle_mod._has_active_tasks = lambda: False

    def tearDown(self):
        import services.data_bundle as bundle_mod

        bundle_mod._has_active_tasks = self._orig_has_active


class ExportBundleTests(_HermeticBundleTest):
    def test_export_produces_a_readable_zip_with_manifest(self):
        payload, filename = build_export_bundle()
        self.assertTrue(filename.endswith(".zip"))
        zf = zipfile.ZipFile(io.BytesIO(payload))
        names = zf.namelist()
        self.assertIn(MANIFEST_NAME, names)
        self.assertIn(DEFAULT_DB_NAME, names)

        manifest = json.loads(zf.read(MANIFEST_NAME))
        self.assertEqual(manifest["format_version"], BUNDLE_FORMAT_VERSION)
        # manifest 登记的库必须都在包里，且带 sha256
        for entry in manifest["databases"]:
            self.assertIn(entry["name"], names)
            self.assertTrue(entry["sha256"])
            self.assertGreater(entry["size"], 0)

    def test_exported_db_is_a_valid_sqlite_snapshot(self):
        """快照必须是可打开的完整库（不是撕裂的半写文件）。"""
        payload, _ = build_export_bundle()
        zf = zipfile.ZipFile(io.BytesIO(payload))
        data = zf.read(DEFAULT_DB_NAME)
        conn = sqlite3.connect(":memory:")
        # 用 serialize 反序列化验证完整性
        conn.deserialize(data)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        self.assertIn("accounts", tables)
        self.assertIn("configs", tables)

    def test_export_excludes_logs_and_backups(self):
        """日志与历史备份不进包（防套娃、可丢弃）。"""
        payload, _ = build_export_bundle()
        names = zipfile.ZipFile(io.BytesIO(payload)).namelist()
        for name in names:
            self.assertNotIn("logs/", name)
            self.assertNotIn("import_backups", name)
            self.assertNotIn("solver.log", name)


class RoundTripTests(_HermeticBundleTest):
    def test_import_restores_config_and_accounts(self):
        from core.config_store import config_store

        config_store.set("rt_marker", "before-export")
        _add_account("rt-keep@example.com", "AT-KEEP")

        payload, _ = build_export_bundle()

        # 导出后篡改：改配置 + 加账号
        config_store.set("rt_marker", "CHANGED")
        _add_account("rt-extra@example.com", "AT-EXTRA")
        self.assertIn("rt-extra@example.com", _emails())

        result = apply_import_bundle(payload, filename="rt.zip")
        self.assertTrue(result["ok"])
        self.assertIn(DEFAULT_DB_NAME, result["imported"])

        # 恢复到导出时状态
        self.assertEqual(config_store.get("rt_marker"), "before-export")
        emails = _emails()
        self.assertIn("rt-keep@example.com", emails)
        self.assertNotIn("rt-extra@example.com", emails)

    def test_import_makes_a_backup_first(self):
        """导入前自动备份当前数据 —— 导入错了能回退。"""
        _add_account("backup-target@example.com", "AT-1")
        payload, _ = build_export_bundle()
        # 备份里应有这个账号；先删掉它再导入，验证备份留住了旧状态
        result = apply_import_bundle(payload)
        backup = Path(result["backup"])
        self.assertTrue(backup.exists(), "导入没有生成备份")
        backup_db = backup / DEFAULT_DB_NAME
        self.assertTrue(backup_db.exists(), "备份里没有默认库")

        conn = sqlite3.connect(str(backup_db))
        emails = {
            row[0]
            for row in conn.execute("SELECT email FROM accounts").fetchall()
        }
        conn.close()
        self.assertIn("backup-target@example.com", emails)

    def test_round_trip_is_idempotent(self):
        """连续导入两次结果一致（第二次不应报错或丢数据）。"""
        _add_account("idem@example.com", "AT-IDEM")
        payload, _ = build_export_bundle()
        first = apply_import_bundle(payload)
        second = apply_import_bundle(payload)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertIn("idem@example.com", _emails())

    def test_failed_swap_rolls_back_to_pre_import_state(self):
        """替换阶段失败 → 自动回滚，数据目录恢复导入前状态。

        实测复现过的故障：迁移一旦在替换后抛错（如包内是坏库），
        数据目录停在「坏库 + 引擎已 dispose」，整个面板 500。
        现在由导入前自动备份做整体回滚，报错里带备份路径。

        **关键设计**：包内状态与导入前状态必须不同（否则「数据没变」
        无法区分「回滚了」和「换成了包里的」—— 空转测试）。
        包内 = in-bundle 账号 + key_a；导入前 = pre-import 账号 + key_b。
        """
        import base64

        from core.paths import CREDENTIAL_KEY_FILE

        # 1) 造「包内状态」并导出：in-bundle 账号 + key_a
        _add_account("in-bundle@example.com", "AT-BUNDLE")
        CREDENTIAL_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        key_a = base64.b64encode(b"a" * 32).decode()
        CREDENTIAL_KEY_FILE.write_text(key_a)
        payload, _ = build_export_bundle()

        # 2) 改成「导入前状态」：pre-import 账号 + key_b（与包内不同）
        key_b = base64.b64encode(b"b" * 32).decode()
        CREDENTIAL_KEY_FILE.write_text(key_b)
        _add_account("pre-import@example.com", "AT-PRE")

        # 3) 让替换后的 init_db 第一次调用抛错 → 触发回滚路径
        import core.db as db_mod

        original_init = db_mod.init_db
        calls = {"n": 0}

        def flaky_init():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟迁移失败")
            return original_init()

        db_mod.init_db = flaky_init
        try:
            with self.assertRaises(BundleError) as ctx:
                apply_import_bundle(payload)
            msg = str(ctx.exception)
            self.assertIn("回滚", msg, f"失败应报告回滚: {msg}")
        finally:
            db_mod.init_db = original_init

        # 4) 回滚成功 → 导入前状态还在。
        #    区分「回滚了」vs「换成了包里的」的关键断言：
        #    - pre-import 账号只在**导入前**存在（包是加它之前导出的）；
        #      若导入完成（无回滚），它会被包内快照替换掉 → 消失。
        #    - key_b 同理：包内是 key_a，导入完成会换成 key_a。
        emails = _emails()
        self.assertIn(
            "pre-import@example.com", emails, "回滚后导入前数据丢了"
        )
        self.assertEqual(
            CREDENTIAL_KEY_FILE.read_text(), key_b, "回滚后密钥没恢复（仍是包内的 key_a？）"
        )


class CredentialKeyTests(_HermeticBundleTest):
    def test_key_file_is_included_and_restored(self):
        """密钥必须一起导 —— 否则导入端加密字段全解不开。"""
        import base64

        from core.paths import CREDENTIAL_KEY_FILE

        CREDENTIAL_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        material = base64.b64encode(b"k" * 32).decode()
        CREDENTIAL_KEY_FILE.write_text(material)

        payload, _ = build_export_bundle()
        zf = zipfile.ZipFile(io.BytesIO(payload))
        self.assertIn("secrets/credential_key", zf.namelist())

        # 删掉密钥再导入 → 应恢复
        CREDENTIAL_KEY_FILE.unlink()
        result = apply_import_bundle(payload)
        self.assertTrue(result["key_restored"])
        self.assertEqual(CREDENTIAL_KEY_FILE.read_text(), material)

    def test_secret_box_cache_is_reset_after_import(self):
        """导入换密钥后，secret_box 的缓存必须失效。

        不重置的话缓存里还是旧密钥，新导入的密文全解不开 —— 症状是
        「导入成功但 iCloud 凭据全用不了」。
        """
        import base64

        from core.paths import CREDENTIAL_KEY_FILE
        from core.secret_box import secret_box

        CREDENTIAL_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        key_a = base64.b64encode(b"a" * 32).decode()
        key_b = base64.b64encode(b"b" * 32).decode()

        CREDENTIAL_KEY_FILE.write_text(key_a)
        secret_box.reset()
        envelope = secret_box.encrypt(b"payload-A")
        self.assertEqual(secret_box.decrypt(envelope), b"payload-A")

        # 用 key_b 造一个导入包并导入
        CREDENTIAL_KEY_FILE.write_text(key_b)
        payload, _ = build_export_bundle()
        CREDENTIAL_KEY_FILE.write_text(key_a)  # 先换回 A（模拟导入前的状态）
        secret_box.reset()
        apply_import_bundle(payload)

        # 导入后密钥是 B：A 加密的密文应解不开，B 加密的应能解开
        with self.assertRaises(Exception):
            secret_box.decrypt(envelope)
        fresh = secret_box.encrypt(b"payload-B")
        self.assertEqual(secret_box.decrypt(fresh), b"payload-B")

    def test_import_without_key_still_works(self):
        """老包/手工包没有密钥时：库照常导入，只是 key_restored=False。"""
        from core.paths import CREDENTIAL_KEY_FILE

        if CREDENTIAL_KEY_FILE.exists():
            CREDENTIAL_KEY_FILE.unlink()
        payload, _ = build_export_bundle()
        result = apply_import_bundle(payload)
        self.assertFalse(result["key_restored"])
        self.assertTrue(result["ok"])


class ImportValidationTests(_HermeticBundleTest):
    def test_rejects_non_zip(self):
        with self.assertRaises(BundleError) as ctx:
            apply_import_bundle(b"definitely not a zip")
        self.assertIn("ZIP", str(ctx.exception))

    def test_rejects_empty_body(self):
        with self.assertRaises(BundleError):
            apply_import_bundle(b"")

    def test_rejects_zip_without_manifest(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("random.db", b"x")
        with self.assertRaises(BundleError) as ctx:
            apply_import_bundle(buf.getvalue())
        self.assertIn(MANIFEST_NAME, str(ctx.exception))

    def test_rejects_future_format_version(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                MANIFEST_NAME,
                json.dumps({"format_version": BUNDLE_FORMAT_VERSION + 1, "databases": []}),
            )
        with self.assertRaises(BundleError) as ctx:
            apply_import_bundle(buf.getvalue())
        self.assertIn("版本", str(ctx.exception))

    def test_rejects_non_sqlite_member(self):
        """sha256 一致但内容不是 SQLite → 拒绝（不替换出坏库）。

        实测复现过的故障：构造 sha256 合法的非 SQLite 包导入后，数据目录
        被换成坏文件、引擎已 dispose，整个面板 500 直到手工修复。
        """
        payload, _ = build_export_bundle()
        src = zipfile.ZipFile(io.BytesIO(payload))
        members = {name: src.read(name) for name in src.namelist()}
        # 换成同样长度的非 SQLite 字节（长度不变不是必须，但更像"改过"）
        members[DEFAULT_DB_NAME] = b"not a sqlite database at all" * 100

        # 重建 manifest：sha256 指向新内容（模拟被改过 manifest 的包）
        import hashlib

        manifest = json.loads(members[MANIFEST_NAME])
        for entry in manifest["databases"]:
            if entry["name"] == DEFAULT_DB_NAME:
                entry["sha256"] = hashlib.sha256(members[DEFAULT_DB_NAME]).hexdigest()
                entry["size"] = len(members[DEFAULT_DB_NAME])
        members[MANIFEST_NAME] = json.dumps(manifest).encode()

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in members.items():
                zf.writestr(name, data)

        with self.assertRaises(BundleError) as ctx:
            apply_import_bundle(buf.getvalue())
        self.assertIn("SQLite", str(ctx.exception))

    def test_rejects_corrupted_zip_member(self):
        """成员数据损坏（CRC 不符）→ BundleError 而不是裸 BadZipFile。

        不包装的话异常穿透到 API 层变成 500 的英文底层报错，
        用户以为服务端坏了。
        """
        payload, _ = build_export_bundle()
        # 找到默认库成员的字节位置并翻一个 bit（CRC 就不符了）
        raw = bytearray(payload)
        needle = DEFAULT_DB_NAME.encode()
        pos = raw.find(needle)
        self.assertGreater(pos, 0, "包内找不到默认库成员名")
        # 在成员名之后的数据区翻一位（跳过文件名本身）
        flip = pos + len(needle) + 30
        raw[flip] ^= 0xFF

        with self.assertRaises(BundleError) as ctx:
            apply_import_bundle(bytes(raw))
        msg = str(ctx.exception)
        self.assertTrue(
            "损坏" in msg or "CRC" in msg or "sha256" in msg,
            f"损坏成员应被包装成可读的 BundleError: {msg}",
        )

    def test_rejects_tampered_content(self):
        """合法 ZIP + 改过的库内容 → sha256 校验必须抓到。"""
        import tempfile

        payload, _ = build_export_bundle()
        src = zipfile.ZipFile(io.BytesIO(payload))
        members = {name: src.read(name) for name in src.namelist()}

        # 在库里注入一张表（仍是合法 SQLite，但 sha256 变了）。
        # 用 TemporaryDirectory 而不是硬编码 /tmp 路径：并行跑测试时
        # 固定路径会互相覆盖，跑完也不该留残留文件。
        with tempfile.TemporaryDirectory(prefix="tamper-test-") as tmp:
            tmp_db = Path(tmp) / "tamper.db"
            tmp_db.write_bytes(members[DEFAULT_DB_NAME])
            conn = sqlite3.connect(str(tmp_db))
            conn.execute("CREATE TABLE IF NOT EXISTS injected (x TEXT)")
            conn.commit()
            conn.close()
            members[DEFAULT_DB_NAME] = tmp_db.read_bytes()

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in members.items():
                zf.writestr(name, data)

        with self.assertRaises(BundleError) as ctx:
            apply_import_bundle(buf.getvalue())
        self.assertIn("sha256", str(ctx.exception))

    def test_rejects_zip_slip(self):
        """成员名带 ../ → 拒绝（防写到 data/ 之外）。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                MANIFEST_NAME,
                json.dumps(
                    {
                        "format_version": BUNDLE_FORMAT_VERSION,
                        "databases": [{"name": "../../evil.db", "sha256": "x"}],
                    }
                ),
            )
            zf.writestr("../../evil.db", b"evil")
        with self.assertRaises(BundleError) as ctx:
            apply_import_bundle(buf.getvalue())
        self.assertIn("非法路径", str(ctx.exception))

    def test_rejects_when_registered_db_missing(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                MANIFEST_NAME,
                json.dumps(
                    {
                        "format_version": BUNDLE_FORMAT_VERSION,
                        "databases": [{"name": "missing.db", "sha256": "x"}],
                    }
                ),
            )
        with self.assertRaises(BundleError) as ctx:
            apply_import_bundle(buf.getvalue())
        self.assertIn("missing.db", str(ctx.exception))


class ActiveTaskGuardTests(unittest.TestCase):
    def test_import_refused_while_a_task_is_running(self):
        """有运行中任务时导入会把它脚下的库换掉 —— 必须拒绝。"""
        payload, _ = build_export_bundle()

        from api.tasks import _task_store

        original = _task_store.has_active
        # 走 `has_active_register_task()` 时会带 platform/source 关键字参数
        _task_store.has_active = lambda **kwargs: True  # type: ignore[method-assign]
        try:
            with self.assertRaises(BundleError) as ctx:
                apply_import_bundle(payload)
            self.assertIn("运行中", str(ctx.exception))
            # 状态冲突按 409 报（前端按 409 写分支）
            self.assertEqual(ctx.exception.status_code, 409)
        finally:
            _task_store.has_active = original  # type: ignore[method-assign]


class BackupApiTests(_HermeticBundleTest):
    """HTTP 层：导出下载、导入上传、信息预览。"""

    @classmethod
    def setUpClass(cls):
        import main as main_mod
        from fastapi.testclient import TestClient

        cls.client = TestClient(main_mod.app)

    def test_export_endpoint_returns_zip(self):
        r = self.client.get("/api/backup/export")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(r.headers.get("content-type"), "application/zip")
        self.assertIn("attachment", r.headers.get("content-disposition", ""))
        # 真的是 ZIP
        self.assertEqual(r.content[:2], b"PK")

    def test_info_endpoint_lists_databases(self):
        r = self.client.get("/api/backup/info")
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        self.assertIn("databases", body)
        self.assertTrue(body["databases"], "应至少列出默认库")
        names = [d["name"] for d in body["databases"]]
        self.assertIn(DEFAULT_DB_NAME, names)
        self.assertIn("has_credential_key", body)
        self.assertIn("has_active_tasks", body)

    def test_import_endpoint_round_trip(self):
        from core.config_store import config_store

        config_store.set("http_marker", "v1")
        export = self.client.get("/api/backup/export")
        bundle = export.content

        config_store.set("http_marker", "v2")

        r = self.client.post(
            "/api/backup/import",
            content=bundle,
            headers={"Content-Type": "application/zip"},
        )
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertTrue(r.json()["ok"])
        self.assertEqual(config_store.get("http_marker"), "v1")

    def test_import_endpoint_rejects_garbage(self):
        r = self.client.post(
            "/api/backup/import",
            content=b"not a zip at all",
            headers={"Content-Type": "application/zip"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("ZIP", r.json()["detail"])

    def test_import_endpoint_rejects_empty(self):
        r = self.client.post(
            "/api/backup/import",
            content=b"",
            headers={"Content-Type": "application/zip"},
        )
        self.assertEqual(r.status_code, 400)


class ConcurrentImportTests(_HermeticBundleTest):
    """并发导入必须串行化 —— 「备份→换库」窗口互相踩会丢数据。

    两个导入同时跑：各自备份到一半、换掉对方刚换的库，轻则备份互相覆盖，
    重则数据目录停在半新半旧。进程内一把互斥锁即可（重活都在线程池里，
    同进程）。
    """

    def test_a_second_import_waits_for_the_first(self):
        import threading

        import services.data_bundle as bundle_mod

        payload, _ = build_export_bundle()
        lock = getattr(bundle_mod, "_BUNDLE_LOCK", None)
        self.assertIsNotNone(lock, "data_bundle 缺少导入/导出互斥锁")

        entered = threading.Event()
        original_backup = bundle_mod._backup_current_data

        def spy_backup(reason="import"):
            entered.set()
            return original_backup(reason)

        bundle_mod._backup_current_data = spy_backup  # type: ignore[assignment]
        results: list = []
        errors: list = []

        def run():
            try:
                results.append(apply_import_bundle(payload, filename="concurrent.zip"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        lock.acquire()
        try:
            thread = threading.Thread(target=run)
            thread.start()
            # 锁被测试持有：导入必须被挡在临界区（备份）之外
            blocked = not entered.wait(timeout=1.0)
        finally:
            lock.release()
            bundle_mod._backup_current_data = original_backup  # type: ignore[assignment]

        thread.join(timeout=60)
        self.assertTrue(blocked, "第二个导入没等互斥锁就进入了「备份→换库」临界区")
        self.assertFalse(thread.is_alive(), "释放锁后导入仍未完成")
        self.assertFalse(errors, f"导入失败: {errors}")
        self.assertTrue(results and results[0]["ok"])


if __name__ == "__main__":
    unittest.main()
