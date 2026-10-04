"""scripts/install_camoufox.py 的下载重试。

背景：受限网络（Docker Hub 被墙那类环境）下 GitHub release 下载会偶发
连接重置 / 超时；一次抖动就炸掉整个镜像构建。Dockerfile 里 playwright
安装已有 3 次重试，camoufox 的两次下载也应对齐这个口径。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install_camoufox.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("install_camoufox", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["install_camoufox"] = module
    spec.loader.exec_module(module)
    return module


class DownloadRetryTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_script_module()

    def test_transient_failure_is_retried_until_success(self):
        calls = {"n": 0}

        def flaky(url, target):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("connection reset by peer")
            Path(target).write_bytes(b"payload")

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "pkg.zip"
            with mock.patch.object(self.mod.urllib.request, "urlretrieve", flaky), \
                 mock.patch.object(self.mod.time, "sleep", lambda _s: None):
                self.mod._download("https://example.invalid/pkg.zip", dest)
            self.assertEqual(calls["n"], 3, "瞬时失败应重试到成功")
            self.assertEqual(dest.read_bytes(), b"payload")

    def test_persistent_failure_gives_up_after_three_attempts(self):
        calls = {"n": 0}

        def always_fail(url, target):
            calls["n"] += 1
            raise OSError("no route to host")

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "pkg.zip"
            with mock.patch.object(self.mod.urllib.request, "urlretrieve", always_fail), \
                 mock.patch.object(self.mod.time, "sleep", lambda _s: None):
                with self.assertRaises(SystemExit):
                    self.mod._download("https://example.invalid/pkg.zip", dest)
            self.assertEqual(calls["n"], 3, "持续失败应恰好重试 3 次后放弃")


if __name__ == "__main__":
    unittest.main()
