import pytest

from core.browser_runtime import ensure_browser_display_available


def test_headed_browser_is_allowed_on_windows_without_display():
    ensure_browser_display_available(False, platform_name="win32", display="")


def test_headed_browser_requires_display_on_linux():
    with pytest.raises(RuntimeError, match="DISPLAY"):
        ensure_browser_display_available(False, platform_name="linux", display="")


def test_headless_browser_never_requires_display():
    ensure_browser_display_available(True, platform_name="linux", display="")
