"""口令配置的读写契约：**输入后不可查看**。

背景：以前前端只是把口令清空后再回填表单，但 `GET /api/config` 本身仍在
明文下发 —— 打开开发者工具的网络面板、或直接 curl 就能读到全部口令。
真正的「输入后不可查看」必须让明文根本不离开服务端。

本文件钉住三条：
1. `GET /api/config` 对任何口令键都只回空串，绝不回明文；
2. 同时回 `<key>_set` 布尔，界面据此显示「已配置」（否则用户无从判断）；
3. `PUT` 时空串 = 不修改（否则页面一保存就把口令抹掉），显式 `null` = 清空。
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    import core.db as db
    from sqlmodel import Session, SQLModel, create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'config.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)

    from core.config_store import ConfigStore
    import api.config as config_module

    monkeypatch.setattr(config_module, "config_store", ConfigStore())

    from main import app

    return TestClient(app)


SECRETS = (
    "yescaptcha_key",
    "twocaptcha_key",
    "cpa_api_key",
    "sub2api_api_key",
    "grok2api_password",
    "sms_api_key",
    "contribution_key",
    "custom_contribution_token",
)


class SecretNeverLeavesTheServerTests(unittest.TestCase):
    def test_get_config_blanks_every_secret(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch = __import__("pytest").MonkeyPatch()
            try:
                client = _client(Path(tmp), monkeypatch)
                # 先写一批口令
                payload = {key: f"secret-value-{key}" for key in SECRETS}
                r = client.put("/api/config", json={"data": payload})
                self.assertEqual(r.status_code, 200, r.text)

                body = client.get("/api/config").json()
                for key in SECRETS:
                    self.assertEqual(body.get(key), "",
                                     f"{key} 明文泄漏到 GET /api/config 了")
                    self.assertTrue(body.get(f"{key}_set"),
                                    f"{key} 已设置，但 {key}_set 不是 true")
            finally:
                monkeypatch.undo()

    def test_get_config_reports_unset_secrets_as_false(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch = __import__("pytest").MonkeyPatch()
            try:
                client = _client(Path(tmp), monkeypatch)
                body = client.get("/api/config").json()
                for key in SECRETS:
                    self.assertEqual(body.get(key), "")
                    self.assertFalse(body.get(f"{key}_set"),
                                     f"{key} 未设置，但 {key}_set 不是 false")
            finally:
                monkeypatch.undo()

    def test_empty_secret_on_put_keeps_the_stored_value(self):
        """空串 = 不修改：页面保存时不会把已存口令抹掉。"""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch = __import__("pytest").MonkeyPatch()
            try:
                client = _client(Path(tmp), monkeypatch)
                client.put("/api/config", json={"data": {"sms_api_key": "keep-me"}})

                # 模拟「页面回填空串后原样保存」
                r = client.put("/api/config", json={"data": {"sms_api_key": ""}})
                self.assertEqual(r.status_code, 200, r.text)

                body = client.get("/api/config").json()
                self.assertTrue(body.get("sms_api_key_set"),
                                "空串提交把已存口令覆盖掉了")
            finally:
                monkeypatch.undo()

    def test_explicit_null_clears_the_secret(self):
        """显式 null = 清空（有意的删除动作）。"""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch = __import__("pytest").MonkeyPatch()
            try:
                client = _client(Path(tmp), monkeypatch)
                client.put("/api/config", json={"data": {"sms_api_key": "delete-me"}})
                r = client.put("/api/config", json={"data": {"sms_api_key": None}})
                self.assertEqual(r.status_code, 200, r.text)

                body = client.get("/api/config").json()
                self.assertFalse(body.get("sms_api_key_set"),
                                 "显式 null 没有清空口令")
            finally:
                monkeypatch.undo()

    def test_secret_set_flags_are_not_writable(self):
        """`<key>_set` 是只读标记，提交回来不该污染配置。"""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch = __import__("pytest").MonkeyPatch()
            try:
                client = _client(Path(tmp), monkeypatch)
                r = client.put(
                    "/api/config",
                    json={"data": {"sms_api_key": "real", "sms_api_key_set": False}},
                )
                self.assertEqual(r.status_code, 200, r.text)
                body = client.get("/api/config").json()
                self.assertTrue(body.get("sms_api_key_set"),
                                "只读标记被写进库并覆盖了真实状态")
            finally:
                monkeypatch.undo()

    def test_non_secret_keys_still_round_trip(self):
        """非口令键必须照旧明文往返，别把整份配置都打码了。"""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch = __import__("pytest").MonkeyPatch()
            try:
                client = _client(Path(tmp), monkeypatch)
                client.put("/api/config", json={"data": {"cpa_api_url": "http://x:1"}})
                body = client.get("/api/config").json()
                self.assertEqual(body.get("cpa_api_url"), "http://x:1")
            finally:
                monkeypatch.undo()


if __name__ == "__main__":
    unittest.main()
