"""环境预检与错误分类 —— 环境没就绪时快速失败，别烧重试轮。

事故（2026-10-06）：camoufox 包升级到 0.5.7（配对浏览器 156.0.1-beta.34），
本机缓存里还是旧浏览器 152.0.4-beta.31。注册任务启动后在浏览器启动处炸
`CamoufoxNotInstalled`，且被当成**可重试失败** —— 两轮重试全烧在同一个
环境错误上，还白分配/释放了邮箱别名、白推了代理游标。

两条防线：

1. **预检**（`check_camoufox_ready`）：浏览器路径的平台在注册前检查
   camoufox 浏览器与包版本配对（库的 `installed_verstr()` 就是配对校验的
   唯一判定源），不满足时以可操作的中文原因快速失败；
2. **错误分类**（`classify_environment_error` / `is_environment_error_message`）：
   环境类错误（浏览器缺失、Node 缺失…）判为不可重试 —— 重开一轮是同样
   结局，由注册 runner 既有的 dead-end 逻辑提前收手。

环境错误与业务错误的边界：只有能明确归因到「本机运行时环境」的错误才算
（消息里带 camoufox / Node 运行时的特征字样）。普通 FileNotFoundError、
网络失败、业务拒绝都不算 —— 误判会让本该重试的失败不重试。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.task_runtime import NonRetryableRegisterError

_CAMOUFOX_HINT = (
    "运行 `python -m camoufox fetch` 安装与当前 camoufox 包配对的浏览器"
    "（升级过 camoufox 包之后必须重跑一次）"
)
_NODE_HINT = (
    "安装 Node.js 18+（Sentinel PoW 必须在 Node 沙箱里跑 OpenAI 的 sdk.js），"
    "或用 OPENAI_SENTINEL_NODE_PATH 指定 node 可执行文件的绝对路径"
)
_GEOIP_HINT = (
    "运行 `pip install camoufox[geoip]` 安装 GeoIP 依赖"
    "（带代理注册时浏览器要按出口 IP 计算时区/语言指纹）"
)


@dataclass(frozen=True)
class EnvironmentVerdict:
    """环境判定结果。

    `ok=False` 时 `message` 是可直接展示给用户的完整报错（含修复指引）；
    `classify_environment_error` 的返回值里 `hint` 单独给出修复命令。
    """

    ok: bool = True
    is_environment: bool = False
    non_retryable: bool = False
    message: str = ""
    hint: str = ""


class EnvironmentNotReadyError(NonRetryableRegisterError):
    """本机运行时环境没就绪（浏览器/Node 缺失等）。

    语义上就是 `NonRetryableRegisterError` —— 重开一轮是同样的结局。
    继承它，注册 runner 的 `isinstance(e, NonRetryableRegisterError)`
    判定直接生效（不必再维护第二条判定链）。
    """


def _looks_environment_like(text: str) -> tuple[bool, str]:
    """文本读起来像不像「本机运行时环境没就绪」。返回 (是否, 修复提示)。"""
    low = str(text or "").lower()
    if not low:
        return False, ""
    # camoufox 浏览器缺失/版本不配对。真实异常文本两种形态：
    #   ① 异常对象形态：CamoufoxNotInstalled("official 156.0.1-beta.34, ...")
    #   ② 文本化形态（register_browser 的兜底 except 会这样记）：
    #      "CamoufoxNotInstalled: official 156.0.1-beta.34, ..."
    if "camoufoxnotinstalled" in low:
        return True, _CAMOUFOX_HINT
    if "camoufox" in low and "not installed" in low:
        return True, _CAMOUFOX_HINT
    if "camoufox fetch" in low:
        return True, _CAMOUFOX_HINT
    if "no module named 'camoufox'" in low or 'no module named "camoufox"' in low:
        return True, "运行 `pip install camoufox` 安装 camoufox 包"
    # GeoIP extra 缺失（带代理注册时 geoip=True 的依赖）。
    # 真实异常文本："NotInstalledGeoIPExtra: Please install the geoip extra
    # to use this feature: pip install camoufox[geoip]"
    if "notinstalledgeoip" in low or "camoufox[geoip]" in low:
        return True, _GEOIP_HINT
    # Node 运行时缺失（Sentinel PoW 依赖）
    if "找不到 node 运行时" in low:
        return True, _NODE_HINT
    return False, ""


def classify_environment_error(exc: Any) -> EnvironmentVerdict:
    """异常（或错误文本）是否属于「本机环境没就绪」。"""
    text = str(exc)
    is_env, hint = _looks_environment_like(text)
    if not is_env:
        return EnvironmentVerdict(ok=True)
    return EnvironmentVerdict(
        ok=False,
        is_environment=True,
        non_retryable=True,
        message=text[:300],
        hint=hint,
    )


def is_environment_error_message(text: Any) -> bool:
    """错误文本是否像环境问题（runner 用它把这类失败标成不可重试）。"""
    return _looks_environment_like(str(text))[0]


def _camoufox_installed_verstr() -> str:
    """薄封装：便于测试注入，也让核心逻辑不直接依赖 camoufox 内部结构。"""
    from camoufox.pkgman import installed_verstr

    return installed_verstr()


def _camoufox_pinned_spec() -> str:
    """包配对的浏览器版本（`<version>-<build>`）；读不到返回空串。"""
    try:
        from camoufox.browser_pin import load_pin

        pin = load_pin()
        return pin.spec if pin is not None else ""
    except Exception:  # noqa: BLE001 - pin 读不到不拦（开发版无 pin）
        return ""


def check_camoufox_ready() -> EnvironmentVerdict:
    """camoufox 浏览器与包版本是否配对就绪（含代理路径的 GeoIP 依赖）。

    `installed_verstr()` 是库自己的配对校验入口：包带 pin 时它只在缓存里
    找到**配对版本**才返回，否则抛 `CamoufoxNotInstalled`（继承
    FileNotFoundError）—— 所以这里不需要自己比对版本号。
    """
    try:
        installed = _camoufox_installed_verstr()
    except ImportError as exc:
        return EnvironmentVerdict(
            ok=False,
            is_environment=True,
            non_retryable=True,
            message=f"camoufox 包未安装（{exc}）。修复：运行 `pip install camoufox` 并执行 `python -m camoufox fetch` 安装配对浏览器",
            hint="运行 `pip install camoufox` 安装 camoufox 包",
        )
    except FileNotFoundError as exc:
        # CamoufoxNotInstalled（继承 FileNotFoundError）：配对浏览器没装。
        pinned = _camoufox_pinned_spec()
        version_note = f"（包配对 {pinned}）" if pinned else ""
        return EnvironmentVerdict(
            ok=False,
            is_environment=True,
            non_retryable=True,
            message=f"camoufox 浏览器未安装{version_note}：{exc}。修复：{_CAMOUFOX_HINT}",
            hint=_CAMOUFOX_HINT,
        )
    except Exception:  # noqa: BLE001 - 探测本身出错时保守放行，别把别的错拦成环境错
        return EnvironmentVerdict(ok=True)
    if not str(installed or "").strip():
        return EnvironmentVerdict(
            ok=False,
            is_environment=True,
            non_retryable=True,
            message=f"camoufox 浏览器版本探测为空。修复：{_CAMOUFOX_HINT}",
            hint=_CAMOUFOX_HINT,
        )
    return check_geoip_extra()


def check_geoip_extra() -> EnvironmentVerdict:
    """代理路径（`geoip=True`）依赖的 maxminddb 是否可用。

    实测事故（2026-10-06）：带代理注册时浏览器启动炸
    `NotInstalledGeoIPExtra: pip install camoufox[geoip]`。它是与
    CamoufoxNotInstalled 同类的环境缺口 —— 提前拦，别烧重试轮。
    """
    try:
        from camoufox.geolocation import geoip_allowed

        geoip_allowed()  # 不可用时抛 NotInstalledGeoIPExtra（ImportError 子类）
    except ImportError:
        return EnvironmentVerdict(
            ok=False,
            is_environment=True,
            non_retryable=True,
            message=f"camoufox GeoIP 依赖未安装（带代理注册需要它）。修复：{_GEOIP_HINT}",
            hint=_GEOIP_HINT,
        )
    except Exception:  # noqa: BLE001 - 探测本身出错时保守放行
        return EnvironmentVerdict(ok=True)
    return EnvironmentVerdict(ok=True)


def check_node_ready() -> EnvironmentVerdict:
    """Sentinel PoW 依赖的 Node 运行时是否可用（ChatGPT 协议链的环境缺口）。

    缺 Node 的表象是「注册流程一切正常但验证码永远收不到」（服务端静默丢弃），
    排查成本极高 —— 在任务开始前就把它拦下来。解析口径与
    `sentinel_quickjs._resolve_node_binary` 保持一致：`OPENAI_SENTINEL_NODE_PATH`
    优先，否则按 PATH 找 `node`。
    """
    import os
    import shutil

    node = (os.getenv("OPENAI_SENTINEL_NODE_PATH", "") or "").strip() or "node"
    if os.path.isabs(node):
        found = os.path.exists(node)
    else:
        found = shutil.which(node) is not None
    if not found:
        return EnvironmentVerdict(
            ok=False,
            is_environment=True,
            non_retryable=True,
            message=(
                f"找不到 Node 运行时（{node}）：Sentinel PoW 必须在 Node 沙箱里跑 "
                "OpenAI 的 sdk.js，缺少它会导致验证码邮件被服务端静默丢弃。"
                f"修复：{_NODE_HINT}"
            ),
            hint=_NODE_HINT,
        )
    return EnvironmentVerdict(ok=True)


__all__ = [
    "EnvironmentNotReadyError",
    "EnvironmentVerdict",
    "check_camoufox_ready",
    "check_geoip_extra",
    "check_node_ready",
    "classify_environment_error",
    "is_environment_error_message",
]
