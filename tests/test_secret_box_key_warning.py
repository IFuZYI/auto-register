"""密钥文件缺失时**必须留痕** —— 静默新建一把是「数据不可读且无人知道」。

2026-11-04 独立审查（切片 D）建议：`core/secret_box._load_or_create_key()` 在
密钥文件不存在时会 `secrets.token_bytes` **静默新建**一把。DATA_DIR 没挂载 /
容器重建丢了卷 / 有人清了数据目录时，重启后旧密文**全部解不开**，而日志里
一个字都没有 —— 用户看到的是「账号都在，但 AT 全失效、注册全报错」。

这类故障最难排查的地方不是「坏了」，而是「坏了却没有任何线索指向密钥」。

契约：**新建密钥必须打 WARNING**，把「这意味着什么」写清楚。
首次部署时也会打一条 —— 那是可接受的噪声，换来的是故障时的唯一线索。
"""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class MissingKeyMustWarnTests(unittest.TestCase):
    def _load_with_new_key(self, tmp: Path) -> list[str]:
        from core import secret_box

        warnings: list[str] = []

        class _Capture:
            def warning(self, msg, *args, **_kw):
                warnings.append(str(msg) % args if args else str(msg))

            def debug(self, *_a, **_k):
                pass

            def info(self, *_a, **_k):
                pass

        with mock.patch.object(secret_box, "logger", _Capture(), create=True), \
                mock.patch.dict("os.environ", {"CREDENTIAL_ENCRYPTION_KEY": ""}), \
                mock.patch.object(secret_box, "_key_file", lambda: tmp / "credential_key"):
            key = secret_box._load_or_create_key()
        self.assertEqual(len(key), 32, "新建的密钥必须是 32 字节")
        return warnings

    def test_creating_a_new_key_emits_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            warnings = self._load_with_new_key(Path(tmp))
        self.assertTrue(
            warnings,
            "密钥文件不存在、静默新建了一把 —— 必须打 WARNING，"
            "否则「旧密文解不开」这件事没有任何线索",
        )
        joined = " ".join(warnings)
        for word in ("密钥", "新建"):
            self.assertIn(word, joined, f"告警要说清发生了什么，实际：{joined}")

    def test_existing_key_does_not_warn(self):
        """对照组：密钥已存在时不该刷告警（否则每次启动都响，告警就废了）。"""
        from core import secret_box

        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "credential_key"
            key_path.write_text(
                base64.b64encode(b"x" * 32).decode(), encoding="utf-8"
            )
            warnings: list[str] = []

            class _Capture:
                def warning(self, msg, *args, **_kw):
                    warnings.append(str(msg) % args if args else str(msg))

                def debug(self, *_a, **_k):
                    pass

                def info(self, *_a, **_k):
                    pass

            with mock.patch.object(secret_box, "logger", _Capture(), create=True), \
                    mock.patch.dict("os.environ", {"CREDENTIAL_ENCRYPTION_KEY": ""}), \
                    mock.patch.object(secret_box, "_key_file", lambda: key_path):
                key = secret_box._load_or_create_key()

        self.assertEqual(key, b"x" * 32)
        self.assertEqual(warnings, [], f"密钥已存在时不该告警，实际：{warnings}")
