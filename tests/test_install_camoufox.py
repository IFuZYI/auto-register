"""scripts/install_camoufox.py 的下载与安装布局契约。

背景一：受限网络（Docker Hub 被墙那类环境）下下载会偶发连接重置 / 超时；
一次抖动就炸掉整个镜像构建。与 Dockerfile 里 playwright 安装的重试口径
一致：瞬时失败重试 3 次。

背景二（406 事故）：构建环境对 addons.mozilla.org 返回 HTTP 406（确定性
拒绝）。旧实现把 uBlock Origin 的下载当致命错误 —— 重试 3 次后 SystemExit
炸掉整个构建。但 UBO 只是可选的广告过滤增强，camoufox 运行时对它的缺失
本就是容错的。修复后的契约：
- 每个资产按候选源顺序回退（AMO 不可达时改走 GitHub 官方签名版）；
- 4xx 属确定性失败：立即换下一源，不做无谓重试；
- UBO 全部源失败 → 警告并继续（构建不因可选组件失败）；
- 主资产（camoufox 包）失败 → 仍然致命（不能产出半残镜像）。

背景三（配对事故）：pip 解析到的 camoufox 包（0.5.7）自带 browser-pin.json，
钉死它配对的浏览器 build（156.0.1-beta.34），且只认多版本布局
（browsers/<repo>/<version>-<build>/ + version.json + .0.5_FLAG）。脚本
写死下载 135.0.1-beta.24 平铺布局 → 运行时 CamoufoxNotInstalled 拒绝启动
（且旧版本低于新版库的 playwright 最低要求）。修复后的契约：
- 浏览器版本**优先读包自带 pin**（库升级时浏览器自动跟着升，永不错配）；
- 包不带 pin 时退回 CAMOUFOX_VERSION / CAMOUFOX_RELEASE 环境变量，
  两者都没有则报错退出（不存在安全的默认值）；
- 安装到多版本布局 + .0.5_FLAG + config.json（active_version）。
"""

from __future__ import annotations

import contextlib
import io
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import zipfile
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


def _http_error(code: int, reason: str = "error") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid/asset", code, reason, None, None)


def _zip_bytes(entries: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in (entries or {"manifest.json": b"{}"}).items():
            zf.writestr(name, data)
    return buf.getvalue()


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
                with self.assertRaises(self.mod.DownloadError):
                    self.mod._download("https://example.invalid/pkg.zip", dest)
            self.assertEqual(calls["n"], 3, "持续失败应恰好重试 3 次后放弃")

    def test_4xx_fails_fast_without_retrying(self):
        """406（事故返回码）属确定性拒绝：立即放弃，不浪费 3 次重试。"""
        calls = []

        def fake(url, target):
            calls.append(url)
            raise _http_error(406, "Not Acceptable")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(self.mod.urllib.request, "urlretrieve", fake), \
                 mock.patch.object(self.mod.time, "sleep", lambda _s: None):
                with self.assertRaises(self.mod.DownloadError):
                    self.mod._download("https://example.invalid/pkg.zip", Path(tmp) / "f")
            self.assertEqual(calls, ["https://example.invalid/pkg.zip"], "4xx 应快失败不重试")

    def test_5xx_is_retried(self):
        """5xx 属服务端瞬时故障：仍按 3 次重试口径处理。"""
        calls = []

        def fake(url, target):
            calls.append(url)
            raise _http_error(500, "Internal Server Error")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(self.mod.urllib.request, "urlretrieve", fake), \
                 mock.patch.object(self.mod.time, "sleep", lambda _s: None):
                with self.assertRaises(self.mod.DownloadError):
                    self.mod._download("https://example.invalid/pkg.zip", Path(tmp) / "f")
            self.assertEqual(len(calls), 3, "5xx 应重试满 3 次")


class MultiSourceFallbackTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_script_module()

    def test_download_first_available_falls_back_to_second_source(self):
        calls = []

        def fake(url, target):
            calls.append(url)
            if url.startswith("https://amo.invalid/"):
                raise _http_error(406, "Not Acceptable")
            Path(target).write_bytes(b"good-bytes")

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "asset.bin"
            with mock.patch.object(self.mod.urllib.request, "urlretrieve", fake), \
                 mock.patch.object(self.mod.time, "sleep", lambda _s: None):
                used = self.mod._download_first_available(
                    ["https://amo.invalid/ubo.xpi", "https://github.invalid/ubo.xpi"], dest
                )
            self.assertEqual(used, "https://github.invalid/ubo.xpi")
            self.assertEqual(dest.read_bytes(), b"good-bytes")
            self.assertEqual(calls, ["https://amo.invalid/ubo.xpi", "https://github.invalid/ubo.xpi"],
                             "首选源 4xx 快失败后应立即回退下一源")

    def test_download_first_available_all_sources_fail_raises(self):
        calls = []

        def fake(url, target):
            calls.append(url)
            if url.startswith("https://a.invalid/"):
                raise _http_error(406, "Not Acceptable")
            raise OSError("reset")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(self.mod.urllib.request, "urlretrieve", fake), \
                 mock.patch.object(self.mod.time, "sleep", lambda _s: None):
                with self.assertRaises(self.mod.DownloadError):
                    self.mod._download_first_available(
                        ["https://a.invalid/x", "https://b.invalid/x"], Path(tmp) / "f"
                    )
            self.assertEqual(len(calls), 4, "首选源 1 次快失败 + 次源 3 次重试")

    def test_ublock_sources_amo_first_then_github_signed_fallback(self):
        urls = self.mod.UBLOCK_SOURCES
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].startswith("https://addons.mozilla.org/"))
        self.assertTrue(urls[1].startswith("https://github.com/gorhill/uBlock/releases/download/"))
        self.assertTrue(urls[1].endswith(".firefox.signed.xpi"))


class MainFlowTests(unittest.TestCase):
    """main() 的致命 / 非致命语义。"""

    def setUp(self):
        self.mod = _load_script_module()

    def _run_main(self, fake, install_dir: Path, env_extra: dict | None = None) -> str:
        env = {"CAMOUFOX_VERSION": "135.0.1", "CAMOUFOX_RELEASE": "beta.24"}
        env.update(env_extra or {})
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(self.mod, "user_cache_dir", lambda _app: str(install_dir)), \
             mock.patch.object(self.mod.urllib.request, "urlretrieve", fake), \
             mock.patch.object(self.mod.time, "sleep", lambda _s: None), \
             contextlib.redirect_stderr(stderr):
            self.mod.main()
        return stderr.getvalue()

    def test_main_survives_amo_406_via_github_fallback(self):
        """事故复现：AMO 406 时构建必须成功（UBO 从 GitHub 兜底拿到）。"""
        calls = []

        def fake(url, target):
            calls.append(url)
            if url.startswith("https://addons.mozilla.org/"):
                raise _http_error(406, "Not Acceptable")
            if "ublock" in url.lower():
                Path(target).write_bytes(_zip_bytes())
            else:
                Path(target).write_bytes(_zip_bytes({"camoufox-bin": b"#!/bin/sh\n"}))

        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp) / "camoufox"
            self._run_main(fake, install_dir)
            self.assertTrue(
                any("manifest.json" == p.name for p in install_dir.rglob("manifest.json")),
                "UBO 应经 GitHub 回退就位（多版本布局下 addons 在 install 根）",
            )
            self.assertTrue(any("github.com/gorhill" in u for u in calls))

    def test_main_continues_when_all_ubo_sources_fail(self):
        """UBO 全部源不可达 → 警告继续（可选增强不该炸构建）。"""
        def fake(url, target):
            if "ublock" in url.lower():
                raise _http_error(404, "Not Found")
            Path(target).write_bytes(_zip_bytes({"camoufox-bin": b"#!/bin/sh\n"}))

        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp) / "camoufox"
            stderr = self._run_main(fake, install_dir)
            self.assertFalse((install_dir / "addons" / "UBO").exists(),
                             "UBO 不可用时不留下空目录残骸")
            self.assertIn("uBlock Origin", stderr, "应打印可选组件警告")

    def test_main_asset_failure_is_fatal(self):
        """camoufox 主资产拿不到 → 构建必须失败（不能静默产出半残镜像）。"""
        def fake(url, target):
            raise _http_error(404, "Not Found")

        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp) / "camoufox"
            with self.assertRaises(SystemExit):
                self._run_main(fake, install_dir)

    def test_ubo_corrupt_payload_falls_back_to_next_source(self):
        """AMO 返回 200 但内容不是 zip（网关错误页）→ 回退 GitHub 签名版。"""
        calls = []

        def fake(url, target):
            calls.append(url)
            if url.startswith("https://addons.mozilla.org/"):
                Path(target).write_bytes(b"<html>captive portal</html>")
            elif "ublock" in url.lower():
                Path(target).write_bytes(_zip_bytes())
            else:
                Path(target).write_bytes(_zip_bytes({"camoufox-bin": b"#!/bin/sh\n"}))

        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp) / "camoufox"
            self._run_main(fake, install_dir)
            self.assertTrue(
                any("manifest.json" == p.name for p in install_dir.rglob("manifest.json")),
                "内容校验失败也应回退到下一源",
            )
            self.assertTrue(any("github.com/gorhill" in u for u in calls))

    def test_ubo_all_payloads_corrupt_continues_with_warning(self):
        """UBO 全部源都返回坏内容 → 同样降级继续。"""
        def fake(url, target):
            if "ublock" in url.lower():
                Path(target).write_bytes(b"<html>error</html>")
                return
            Path(target).write_bytes(_zip_bytes({"camoufox-bin": b"#!/bin/sh\n"}))

        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp) / "camoufox"
            stderr = self._run_main(fake, install_dir)
            self.assertFalse((install_dir / "addons" / "UBO").exists())
            self.assertIn("uBlock Origin", stderr)

    def test_main_asset_corrupt_zip_is_fatal(self):
        """主资产下载损坏（截断 / HTML 错误页）→ 构建失败。"""
        def fake(url, target):
            if "ublock" in url.lower():
                Path(target).write_bytes(_zip_bytes())
                return
            Path(target).write_bytes(b"<html>not a zip</html>")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self._run_main(fake, Path(tmp) / "camoufox")


class PinDrivenLayoutTests(unittest.TestCase):
    """配对驱动：浏览器版本跟包走，布局按 0.5.x 多版本约定。"""

    def setUp(self):
        self.mod = _load_script_module()

    def _fake_pkg_tree(self, tmp: Path, pin: dict | None) -> Path:
        """造一个假 site-packages/camoufox 包目录（含可选 browser-pin.json）。

        同时放置 multiversion.py 以模拟 0.5.x+ 包（新布局检测依据）。
        """
        pkg = Path(tmp) / "site-packages" / "camoufox"
        pkg.mkdir(parents=True)
        (pkg / "multiversion.py").write_text("# fake", encoding="utf-8")
        if pin is not None:
            (pkg / "browser-pin.json").write_text(json.dumps(pin), encoding="utf-8")
        return pkg

    def _fake_legacy_pkg_tree(self, tmp: Path) -> Path:
        """造一个 0.4.x 风格包（无 multiversion.py、无 pin）。"""
        pkg = Path(tmp) / "site-packages" / "camoufox"
        pkg.mkdir(parents=True)
        (pkg / "pkgman.py").write_text("# fake", encoding="utf-8")
        return pkg

    def test_legacy_package_uses_flat_layout(self):
        """0.4.x 包（无 multiversion.py）→ 平铺布局：版本目录在安装根。"""
        calls = []

        def fake(url, target):
            calls.append(url)
            if "ublock" in url.lower():
                Path(target).write_bytes(_zip_bytes())
            else:
                Path(target).write_bytes(_zip_bytes({"camoufox-bin": b"#!/bin/sh\n"}))

        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp) / "camoufox"
            pkg = self._fake_legacy_pkg_tree(tmp)
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {
                     "CAMOUFOX_VERSION": "135.0.1", "CAMOUFOX_RELEASE": "beta.24"}), \
                 mock.patch.object(self.mod, "user_cache_dir", lambda _app: str(install_dir)), \
                 mock.patch.object(self.mod, "_camoufox_package_dir", lambda: pkg), \
                 mock.patch.object(self.mod.urllib.request, "urlretrieve", fake), \
                 mock.patch.object(self.mod.time, "sleep", lambda _s: None), \
                 contextlib.redirect_stderr(stderr):
                self.mod.main()
            # 平铺布局：安装根直接有 version.json 与 camoufox-bin
            self.assertTrue((install_dir / "version.json").exists(), "平铺布局应有根 version.json")
            self.assertTrue((install_dir / "camoufox-bin").exists())
            self.assertFalse((install_dir / ".0.5_FLAG").exists(), "旧布局不应写 .0.5_FLAG")
            self.assertFalse((install_dir / "browsers").exists(), "旧布局不应建 browsers/")

    def test_read_pin_prefers_browser_pin_json(self):
        """包自带 pin 时读它（版本 + build + repo 名）。"""
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._fake_pkg_tree(tmp, {
                "tag": "v156.0.1-beta.34", "repo": "daijro/camoufox",
                "repo_name": "Official", "version": "156.0.1", "build": "beta.34",
            })
            info, _src = self.mod._resolve_target(pkg, {})
            self.assertEqual((info.version, info.build), ("156.0.1", "beta.34"))
            self.assertEqual(info.repo_name.lower(), "official")

    def test_read_pin_falls_back_to_env_when_no_pin(self):
        """包不带 pin（开发版）→ 回退 CAMOUFOX_VERSION / CAMOUFOX_RELEASE。"""
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._fake_pkg_tree(tmp, None)
            info, _src = self.mod._resolve_target(
                pkg, {"CAMOUFOX_VERSION": "135.0.1", "CAMOUFOX_RELEASE": "beta.24"}
            )
            self.assertEqual((info.version, info.build), ("135.0.1", "beta.24"))
            self.assertEqual(info.repo_name.lower(), "official")

    def test_read_pin_requires_some_source(self):
        """既无 pin 又无环境变量 → 报错退出（不存在安全的默认版本）。"""
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._fake_pkg_tree(tmp, None)
            with self.assertRaises(SystemExit):
                self.mod._resolve_target(pkg, {})

    def test_pin_ignores_env_override(self):
        """有 pin 时环境变量不参与（库升级自动带动浏览器升级，永不错配）。"""
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._fake_pkg_tree(tmp, {
                "tag": "v156.0.1-beta.34", "repo": "daijro/camoufox",
                "repo_name": "Official", "version": "156.0.1", "build": "beta.34",
            })
            info, _src = self.mod._resolve_target(
                pkg, {"CAMOUFOX_VERSION": "135.0.1", "CAMOUFOX_RELEASE": "beta.24"}
            )
            self.assertEqual((info.version, info.build), ("156.0.1", "beta.34"))

    def test_layout_uses_multiversion_dirs_and_compat_flag(self):
        """安装布局：browsers/<repo>/<version>-<build>/ + version.json +
        .0.5_FLAG + config.json(active_version)。"""
        calls = []

        def fake(url, target):
            calls.append(url)
            if "ublock" in url.lower():
                Path(target).write_bytes(_zip_bytes())
            else:
                Path(target).write_bytes(_zip_bytes({"camoufox-bin": b"#!/bin/sh\n"}))

        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp) / "camoufox"
            self._run_main_pinned(fake, install_dir)
            version_dir = install_dir / "browsers" / "official" / "156.0.1-beta.34"
            self.assertTrue((version_dir / "version.json").exists(), "版本目录应含 version.json")
            meta = json.loads((version_dir / "version.json").read_text())
            self.assertEqual((meta["version"], meta["build"]), ("156.0.1", "beta.34"))
            self.assertTrue((install_dir / ".0.5_FLAG").exists(), "缺少 .0.5_FLAG 会被库当旧布局清掉")
            config = json.loads((install_dir / "config.json").read_text())
            self.assertEqual(config["active_version"], "browsers/official/156.0.1-beta.34")
            self.assertTrue((version_dir / "camoufox-bin").exists())
            self.assertTrue(any("daijro/camoufox/releases/download/v156.0.1-beta.34" in u for u in calls),
                            f"应下载 pin 指定的构建: {calls}")

    def _run_main_pinned(self, fake, install_dir: Path) -> str:
        """跑 main()，其中伪装 site-packages 里有带 pin 的 camoufox 包（0.5.x 布局）。"""
        tmp_pkg_root = Path(install_dir).parent / "site-packages"
        pkg = tmp_pkg_root / "camoufox"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "multiversion.py").write_text("# fake", encoding="utf-8")
        (pkg / "browser-pin.json").write_text(json.dumps({
            "tag": "v156.0.1-beta.34", "repo": "daijro/camoufox",
            "repo_name": "Official", "version": "156.0.1", "build": "beta.34",
        }), encoding="utf-8")
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(self.mod, "user_cache_dir", lambda _app: str(install_dir)), \
             mock.patch.object(self.mod, "_camoufox_package_dir", lambda: pkg), \
             mock.patch.object(self.mod.urllib.request, "urlretrieve", fake), \
             mock.patch.object(self.mod.time, "sleep", lambda _s: None), \
             contextlib.redirect_stderr(stderr):
            os.environ.pop("CAMOUFOX_VERSION", None)
            os.environ.pop("CAMOUFOX_RELEASE", None)
            self.mod.main()
        return stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
