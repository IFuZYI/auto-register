"""Camoufox 安装（构建期脚本，由 Dockerfile COPY 进镜像执行）。

职责：下载 camoufox 浏览器包并解压到缓存目录，附带安装 uBlock Origin。

**版本配对（核心不变量）**：浏览器 build 必须与已安装的 camoufox Python 包
严格配对 —— 包自带 ``browser-pin.json`` 钉死它发布时配套的浏览器 build，
库启动时会校验（不匹配直接 CamoufoxNotInstalled 拒绝启动）。

事故背景：脚本曾写死下载 135.0.1-beta.24（平铺布局），而 pip 解析到的新包
（0.5.7）配对 156.0.1-beta.34（多版本布局）—— 构建成功但 Solver 运行时
拒绝启动。修复：版本**优先读包自带 pin**，库升级时浏览器自动跟着升；
包不带 pin（开发版）时回退 CAMOUFOX_VERSION / CAMOUFOX_RELEASE 环境变量；
两者都没有则报错退出（不存在安全的默认版本）。

下载策略（对齐 Dockerfile 里 playwright 安装的重试口径）：
- 瞬时失败（连接重置 / 超时 / 5xx）重试 3 次；
- 4xx 中仅限流类（408/425/429）重试，其余（403/404/406 等确定性拒绝）
  立即放弃该源 —— 重试没有意义；
- 多源资产按候选源顺序回退，并对下载内容做校验（坏包换源）；
- uBlock Origin 只是可选增强：全部源失败只警告、不中断构建。
  事故背景：构建环境对 addons.mozilla.org 返回 406，旧实现把它当致命
  错误，重试 3 次后 SystemExit 炸掉了整个镜像构建（而同一环境 GitHub
  可达）。UBO 缺失只影响广告过滤，不影响 Solver 工作。
- 主资产（camoufox 包）下载 / 解压失败仍然致命：不能静默产出半残镜像。

**布局（0.5.x 多版本约定，与 camoufox 包的 multiversion.py 对齐）**：
- 版本目录：``browsers/<repo>/<version>-<build>/``（含 version.json）
- 兼容标记：``<install>/.0.5_FLAG``（缺了会被库当旧布局整个清掉重下）
- 激活配置：``<install>/config.json`` 的 ``active_version``
- 附加组件：``<install>/addons/UBO``（多版本布局下仍在安装根）
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir

_RETRIES = 3

# 4xx 里仍值得重试的返回码：408 请求超时、425 过早、429 限流
_RETRYABLE_HTTP_CODES = {408, 425, 429}

# uBlock Origin 候选源：AMO 优先（官方分发渠道），GitHub 官方签名版兜底。
# 事故：部分构建环境对 addons.mozilla.org 返回 406（Not Acceptable），
# 而 GitHub 可达 —— 单源实现会让一个可选组件拖垮整个构建。
# 升级版本时同步改这里的固定直链（避免构建期访问 GitHub Releases API
# 触发匿名限流，与 camoufox 主包同一口径）。
UBLOCK_SOURCES = [
    "https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi",
    "https://github.com/gorhill/uBlock/releases/download/1.75.0/uBlock0_1.75.0.firefox.signed.xpi",
]

_DEFAULT_REPO = "daijro/camoufox"
_DEFAULT_REPO_NAME = "official"


class DownloadError(Exception):
    """下载或解压失败（是否致命由调用方决定）。"""


@dataclass(frozen=True)
class TargetInfo:
    """要安装的浏览器构建。"""

    version: str
    build: str
    repo: str = _DEFAULT_REPO
    repo_name: str = _DEFAULT_REPO_NAME

    @property
    def tag(self) -> str:
        return f"v{self.version}-{self.build}"

    @property
    def version_dir_name(self) -> str:
        return f"{self.version}-{self.build}"

    @property
    def active_version(self) -> str:
        return f"browsers/{self.repo_name}/{self.version_dir_name}"


def _camoufox_package_dir() -> Path | None:
    """定位已安装的 camoufox 包目录（读 browser-pin.json 用），找不到返回 None。"""
    try:
        spec = importlib.util.find_spec("camoufox")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations)))


def _resolve_target(pkg_dir: Path | None, env: Mapping[str, str]) -> tuple[TargetInfo, str]:
    """确定要安装的浏览器版本：包自带 pin 优先，其次环境变量，都没有则退出。

    返回 (目标, 来源描述)，来源用于构建日志（pin / env）。
    """
    if pkg_dir is not None:
        pin_file = pkg_dir / "browser-pin.json"
        if pin_file.exists():
            try:
                data = json.loads(pin_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"警告: browser-pin.json 无法解析，忽略: {exc}", file=sys.stderr)
                data = None
            if (
                isinstance(data, dict)
                and data.get("version")
                and data.get("build")
            ):
                return (
                    TargetInfo(
                        version=str(data["version"]),
                        build=str(data["build"]),
                        repo=str(data.get("repo") or _DEFAULT_REPO),
                        repo_name=str(data.get("repo_name") or _DEFAULT_REPO_NAME).lower(),
                    ),
                    f"browser-pin.json ({pin_file})",
                )
    version = env.get("CAMOUFOX_VERSION")
    release = env.get("CAMOUFOX_RELEASE")
    if version and release:
        return TargetInfo(version=version, build=release), "CAMOUFOX_VERSION / CAMOUFOX_RELEASE"
    raise SystemExit(
        "无法确定 camoufox 浏览器版本：安装的 camoufox 包不带 browser-pin.json，"
        "且未设置 CAMOUFOX_VERSION / CAMOUFOX_RELEASE。"
    )


def _download(url: str, target: Path) -> None:
    """下载单个 URL；瞬时失败重试，4xx 确定性拒绝快失败。"""
    last_error: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            print(f"Downloading: {url}")
            urllib.request.urlretrieve(url, target)
            return
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in _RETRYABLE_HTTP_CODES:
                raise DownloadError(f"{url} -> HTTP {exc.code} {exc.reason}") from exc
            last_error = exc
        except Exception as exc:  # noqa: BLE001 - 网络异常种类多，统一重试
            last_error = exc
        if attempt < _RETRIES:
            print(f"Download failed ({attempt}/{_RETRIES}), retrying: {last_error}", file=sys.stderr)
            time.sleep(5 * attempt)
    raise DownloadError(f"下载失败（已重试 {_RETRIES} 次）: {url} - {last_error}")


def _download_first_available(
    sources: list[str],
    target: Path,
    validate: Callable[[Path], None] | None = None,
) -> str:
    """按候选源顺序下载到 target；返回成功源的 URL，全部失败抛 DownloadError。

    validate 在每个源下载完成后校验内容（如：是否为有效附加组件包），
    校验失败视同该源失败，继续尝试下一源。
    """
    errors: list[str] = []
    for index, url in enumerate(sources):
        try:
            _download(url, target)
            if validate is not None:
                validate(target)
            return url
        except DownloadError as exc:
            errors.append(f"{url} ({exc})")
            if index < len(sources) - 1:
                print(f"Source failed, trying next: {exc}", file=sys.stderr)
    raise DownloadError("所有候选源均失败: " + "; ".join(errors))


def _extract_zip(archive: Path, dest: Path) -> None:
    """解压 zip；损坏（HTML 错误页 / 截断）抛 DownloadError。"""
    try:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    except (zipfile.BadZipFile, OSError) as exc:
        raise DownloadError(f"压缩包无法解压: {archive.name} ({exc})") from exc


def _validate_addon_zip(target: Path) -> None:
    """确认下载内容是有效附加组件包（含 manifest.json），否则抛 DownloadError。"""
    try:
        with zipfile.ZipFile(target) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise DownloadError(f"无效的附加组件包: {target.name} ({exc})") from exc
    if "manifest.json" not in names:
        raise DownloadError(f"附加组件包缺少 manifest.json: {target.name}")


def _install_ublock(install_dir: Path, temp_dir: Path) -> None:
    """安装 uBlock Origin（可选增强）。

    多源回退 + 内容校验；任何失败只警告不中断 —— UBO 缺失不影响 Solver
    工作，不能因此炸掉镜像构建。失败不留半残目录（camoufox 运行时见到
    无 manifest 的目录会尝试联网重下，留残骸只会误导排障）。
    """
    addon_dir = install_dir / "addons" / "UBO"
    archive_path = temp_dir / "ublock-origin.xpi"
    try:
        used = _download_first_available(UBLOCK_SOURCES, archive_path, validate=_validate_addon_zip)
        shutil.rmtree(addon_dir, ignore_errors=True)
        addon_dir.mkdir(parents=True, exist_ok=True)
        _extract_zip(archive_path, addon_dir)
    except DownloadError as exc:
        shutil.rmtree(addon_dir, ignore_errors=True)
        print(f"警告: uBlock Origin 安装失败（可选增强，跳过）: {exc}", file=sys.stderr)
        return
    print(f"uBlock Origin installed from {used}")


def _finalize_permissions(install_dir: Path) -> None:
    """目录 755、文件 644；两个启动器恢复可执行位。

    zipfile.extractall 不保留压缩包里的权限位（实测 camoufox-bin 落盘为
    644），而库启动时 exec 的正是它 —— 必须显式补 +x，否则启动报权限错误。
    """
    for path in install_dir.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.chmod(0o644)
    for name in ("camoufox-bin", "camoufox"):
        for binary in install_dir.rglob(name):
            if binary.is_file():
                binary.chmod(0o755)


def _uses_multiversion_layout(pkg_dir: Path | None) -> bool:
    """包是否为 0.5.x+（多版本布局）。

    判据：包内存在 multiversion.py（0.5.0 起引入）。0.4.x 没有它，
    缓存布局是平铺式（version.json 与 camoufox-bin 直接放安装根）。
    """
    return pkg_dir is not None and (pkg_dir / "multiversion.py").exists()


def main() -> None:
    arch_map = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "i386": "i686",
        "i686": "i686",
        "x86": "i686",
    }
    machine = os.uname().machine.lower()
    arch = arch_map.get(machine)
    if not arch:
        raise SystemExit(f"Unsupported Camoufox arch: {machine}")

    pkg_dir = _camoufox_package_dir()
    target, source = _resolve_target(pkg_dir, os.environ)
    multiversion = _uses_multiversion_layout(pkg_dir)
    layout = "multiversion" if multiversion else "flat"
    print(f"Camoufox target: {target.repo_name} {target.version_dir_name} (from {source}, {layout} layout)")

    asset_name = f"camoufox-{target.version}-{target.build}-lin.{arch}.zip"
    asset_url = f"https://github.com/{target.repo}/releases/download/{target.tag}/{asset_name}"
    install_dir = Path(user_cache_dir("camoufox"))
    temp_dir = Path(tempfile.mkdtemp(prefix="camoufox-install-"))

    try:
        if install_dir.exists():
            shutil.rmtree(install_dir)
        install_dir.mkdir(parents=True, exist_ok=True)

        archive_path = temp_dir / asset_name
        if multiversion:
            version_dir = install_dir / "browsers" / target.repo_name / target.version_dir_name
        else:
            version_dir = install_dir
        try:
            _download(asset_url, archive_path)
            version_dir.mkdir(parents=True, exist_ok=True)
            _extract_zip(archive_path, version_dir)
        except DownloadError as exc:
            raise SystemExit(f"Camoufox 包下载/解压失败（构建终止）: {exc}") from exc

        (version_dir / "version.json").write_text(
            json.dumps({"version": target.version, "build": target.build}),
            encoding="utf-8",
        )
        if multiversion:
            # 兼容标记：缺了它，库启动时会把整个缓存目录当旧布局清掉重下
            (install_dir / ".0.5_FLAG").touch()
            # 激活配置：告诉库用哪个版本目录
            (install_dir / "config.json").write_text(
                json.dumps({"active_version": target.active_version}),
                encoding="utf-8",
            )

        _install_ublock(install_dir, temp_dir)
        _finalize_permissions(install_dir)

        binary = version_dir / "camoufox-bin"
        if not binary.exists():
            raise SystemExit(f"Camoufox 包内容异常：缺少 {binary.relative_to(install_dir)}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
