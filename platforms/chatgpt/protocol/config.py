"""注册协议的最小配置。

协议层只关心出口代理，其余行为（OTP 超时、Codex 交换开关等）一律通过
``AuthFlow(env_overrides=...)`` 注入，不读进程环境变量。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    """ChatGPT 注册最小配置。"""

    # 出口代理 URL，例：socks5://user:pass@host:port
    # 留 None 走系统直连
    proxy: Optional[str] = None
