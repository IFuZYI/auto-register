"""环境变量取值：**写错了就退回默认值，不许在 import 期炸掉**。

2026-11-04 独立审查（切片 A1）：`platforms/grok/exit_probe.py` 与
`platforms/grok/turnstile_mint.py` 都在模块顶层用 `float(os.getenv(...))` /
`int(os.getenv(...))` 解析环境变量。运维在 `.env` 里写一句
`GROK_REACH_MAX_SECONDS=8s`（极自然的笔误）→ `ValueError` 发生在 **import 期**
→ `platforms.grok` 整个加载失败，注册入口从界面上消失，而报错栈指向一个跟
「代理/注册」毫无关系的行号，排查方向会被带偏。

这里的契约：**配置读不懂时，用默认值 + 一条 warning**，永远不抛。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)


def env_number(
    name: str,
    default: Any,
    *,
    cast: Callable[[str], Any] = float,
) -> Any:
    """读一个数值型环境变量；缺失 / 空白 / 解析失败都返回 `default`。

    `default` 的类型决定返回值类型：调用方传 `8.0` 就拿回 float，传 `200` 就拿回 int
    （`cast` 只负责把字符串变成数，最终按 `default` 的类型收口 —— 否则
    `env_number("X", 200, cast=float)` 会悄悄把 int 变成 float，把下游
    `%d` 格式化弄坏）。
    """
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    text = str(raw).strip()
    try:
        value = cast(text)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "环境变量 %s=%r 解析失败（%s），改用默认值 %r", name, text, exc, default
        )
        return default
    # 按默认值的类型收口：默认是 int 就取整，默认是 float 就转 float。
    try:
        return type(default)(value) if default is not None else value
    except (TypeError, ValueError):
        return default
