"""面板请求的 TLS 校验策略。

安全审查（2026-10-27）F3：此前 9 处面板客户端**全部硬编码 `verify=False`**，
涵盖 CPA / Sub2API / chatgpt2api / grok2api / cliproxyapi 与面板对比。

后果：本地与面板之间若有中间人（同机房、被劫持的路由、恶意代理），攻击者
出示任意证书即可收到面板 API Key **以及**推送的全部凭据（AT / RT / id_token /
sso，chatgpt2api 还含 password + totp_secret）—— 证书校验这个控制被完全绕过。

**默认校验**。自签名面板（内网自建）由用户显式二选一：

- `panel_tls_ca_bundle` = CA 文件路径 —— 推荐，仍然校验，只是信任自签 CA；
- `panel_tls_insecure` = `1` —— 完全关闭校验，只在临时排障时用。

把「不安全」变成**显式选择**而不是默认值，是这个模块存在的全部理由。
"""

from __future__ import annotations

import logging
from typing import Union

logger = logging.getLogger(__name__)

#: `requests` 的 `verify` 参数类型：True / False / CA bundle 路径
VerifyValue = Union[bool, str]

_CONFIG_KEY_CA = "panel_tls_ca_bundle"
_CONFIG_KEY_INSECURE = "panel_tls_insecure"

#: 已经警告过的面板（同一次进程内只吵一次，避免刷日志）
_warned: set[str] = set()


def _reset_cache() -> None:
    """测试用：清掉「已警告」记录。"""
    _warned.clear()


def _config(key: str) -> str:
    from core.config_store import config_store

    return str(config_store.get(key, "") or "").strip()


def verify_for(panel: str = "") -> VerifyValue:
    """该面板请求该用的 `verify` 值。

    读取顺序：CA bundle 优先（仍然校验），其次 insecure 开关。
    **任何异常都退回 `True`** —— 配置读不出来时宁可连不上，也不静默降级成
    不校验。
    """
    try:
        ca_bundle = _config(_CONFIG_KEY_CA)
        if ca_bundle:
            return ca_bundle
        insecure = _config(_CONFIG_KEY_INSECURE)
    except Exception:  # noqa: BLE001 - 配置层故障不该降级成不安全
        logger.warning("读取 TLS 配置失败，按「校验证书」处理", exc_info=True)
        return True

    if insecure in {"1", "true", "yes", "on"}:
        key = str(panel or "").strip().lower() or "?"
        if key not in _warned:
            _warned.add(key)
            logger.warning(
                "面板 %s 的 TLS 证书校验已被显式关闭（panel_tls_insecure=1）"
                "—— 推送的凭据可被中间人截获，排障后请尽快改回",
                key,
            )
        return False
    return True


__all__ = ["VerifyValue", "verify_for"]
