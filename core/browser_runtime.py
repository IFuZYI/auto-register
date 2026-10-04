"""Browser runtime helpers for headless/headed resolution."""

import logging
import os
import sys
from collections.abc import Iterable

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def parse_env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None:
        return None

    value = str(raw).strip().lower()
    if not value:
        return None
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False

    logger.warning("忽略无效布尔环境变量 %s=%r", name, raw)
    return None


def resolve_browser_headless(
    requested_headless: bool | None,
    *,
    default_headless: bool = True,
    override_env_names: Iterable[str] = ("PLAYWRIGHT_HEADLESS", "REGISTER_HEADLESS"),
) -> tuple[bool, str]:
    for env_name in override_env_names:
        override = parse_env_bool(env_name)
        if override is not None:
            return override, f"env:{env_name}={str(override).lower()}"

    if requested_headless is not None:
        return bool(
            requested_headless
        ), f"requested:{str(bool(requested_headless)).lower()}"

    return bool(default_headless), f"default:{str(bool(default_headless)).lower()}"


def ensure_browser_display_available(
    headless: bool,
    *,
    platform_name: str | None = None,
    display: str | None = None,
) -> None:
    """Fail early only when a Linux headed session has no graphical display.

    Windows and macOS provide GUI sessions differently and must not be gated by
    the X11 ``DISPLAY`` environment variable. Optional arguments make this
    platform rule deterministic to test without mutating process globals.
    """
    if headless:
        return
    current_platform = platform_name or sys.platform
    if not current_platform.startswith("linux"):
        return
    current_display = os.getenv("DISPLAY") if display is None else display
    if current_display:
        return

    raise RuntimeError(
        "当前为 Linux 有头浏览器模式，但未检测到 DISPLAY。"
        "Docker 内请启用 Xvfb；本地 Linux 请先启动图形环境或改用无头模式。"
    )
