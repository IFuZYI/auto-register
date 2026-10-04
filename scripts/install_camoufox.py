import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from platformdirs import user_cache_dir


def _download(url: str, target: Path) -> None:
    """下载文件，瞬时失败（连接重置/超时）重试 3 次。

    与 Dockerfile 里 playwright 安装的重试口径一致：受限网络下一次抖动
    不该炸掉整个镜像构建。3 次都失败时抛 SystemExit（构建日志里给出 URL）。
    """
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            print(f"Downloading: {url}")
            urllib.request.urlretrieve(url, target)
            return
        except Exception as exc:  # noqa: BLE001 - 网络异常种类多，统一重试
            last_error = exc
            if attempt < 3:
                print(f"Download failed ({attempt}/3), retrying: {exc}", file=sys.stderr)
                time.sleep(5 * attempt)
    raise SystemExit(f"下载失败（已重试 3 次）: {url} - {last_error}")


def main() -> None:
    version = os.environ["CAMOUFOX_VERSION"]
    release = os.environ["CAMOUFOX_RELEASE"]
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

    tag = f"v{version}-{release}"
    asset_name = f"camoufox-{version}-{release}-lin.{arch}.zip"
    asset_url = f"https://github.com/daijro/camoufox/releases/download/{tag}/{asset_name}"
    addon_url = "https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi"
    install_dir = Path(user_cache_dir("camoufox"))
    temp_dir = Path(tempfile.mkdtemp(prefix="camoufox-install-"))

    try:
        if install_dir.exists():
            shutil.rmtree(install_dir)
        install_dir.mkdir(parents=True, exist_ok=True)

        archive_path = temp_dir / asset_name
        _download(asset_url, archive_path)
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(install_dir)

        version_path = install_dir / "version.json"
        version_path.write_text(
            json.dumps({"version": version, "release": release}),
            encoding="utf-8",
        )

        addon_dir = install_dir / "addons" / "UBO"
        addon_dir.mkdir(parents=True, exist_ok=True)
        addon_path = temp_dir / "ublock-origin.xpi"
        _download(addon_url, addon_path)
        with zipfile.ZipFile(addon_path) as zf:
            zf.extractall(addon_dir)

        for path in install_dir.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
            else:
                path.chmod(0o644)

        binary = install_dir / "camoufox-bin"
        if binary.exists():
            binary.chmod(0o755)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
