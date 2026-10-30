"""能力层（capability layer）：把「动作 id」收敛成 6 个能力。

## 为什么要它

平台对外的动作 id 有 17 个（ChatGPT 11 + Grok 6），但**语义**只有 6 种：
探测、刷新、补 RT、绑 2FA、上传、同步面板状态。这 17 个 id 是各平台自己起的名字，
于是策略代码只能逐个字符串比较：

    if action_id == "probe_local_status": ...
    elif action_id == "sync_cliproxyapi_status": ...
    elif action_id in ("backfill_refresh_token", "bind_2fa"): ...

26 处这样的比较散在 `api/actions.py`、`services/action_execution.py` 和两个
plugin 里。加一个新平台、改一个 id，就要全局搜一遍。

## 分工

* **能力（Capability）** 是粗轴：决定**策略**（要不要落状态、要不要记上传账、
  界面上归到哪一组）。
* **动作 id** 是细轴：决定**实现**（`probe_local_status` 与 `check_plus_trial`
  同属 PROBE，但实现完全不同）。

所以能力层不替代 id，它给 id 一个**分类**。策略代码以后按能力分支，
实现代码继续按 id 分支 —— 两边都不再需要认识对方那一层的细节。

## 单一事实源

`ACTION_CAPABILITIES` 是唯一映射处。`tests/test_capability_registry.py` 会拿
两个平台真实声明的动作列表跟它比对：**声明了却没登记的动作**会当场测试失败，
而不是等到运行时走进某个 `else` 分支才暴露。
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable


class Capability(str, Enum):
    """平台能力的粗分类。值即接口/日志里出现的稳定字符串。"""

    #: 探测账号当前是否可用（不换凭证）。含「查订阅」「探本地状态」等变体。
    PROBE = "probe"
    #: 用现有凭证换新凭证（AT/RT 刷新、重换 OAuth）。
    REFRESH = "refresh"
    #: 缺 RT 时用密码/SSO 走一遍登录链把 RT 补回来。
    BACKFILL = "backfill"
    #: 绑定 TOTP 二次验证。
    BIND_2FA = "bind_2fa"
    #: 把账号推送到外部面板（CPA / sub2api / chatgpt2api / grok2api）。
    UPLOAD = "upload"
    #: 从外部面板读回状态写本地（不探活 —— 面板自己会刷 token）。
    SYNC_STATUS = "sync_status"


#: `action_id` → 能力。**唯一的映射处**；新增动作必须在这里登记。
ACTION_CAPABILITIES: dict[str, Capability] = {
    # ---- ChatGPT ----
    "probe_local_status": Capability.PROBE,
    "check_plus_trial": Capability.PROBE,
    "sync_cliproxyapi_status": Capability.SYNC_STATUS,
    "sync_sub2api_status": Capability.SYNC_STATUS,
    "sync_chatgpt2api_status": Capability.SYNC_STATUS,
    "refresh_token": Capability.REFRESH,
    "backfill_refresh_token": Capability.BACKFILL,
    "bind_2fa": Capability.BIND_2FA,
    "upload_cpa": Capability.UPLOAD,
    "upload_sub2api": Capability.UPLOAD,
    "upload_chatgpt2api": Capability.UPLOAD,
    # ---- Grok ----
    "probe": Capability.PROBE,
    "probe_refresh": Capability.PROBE,
    "refresh_oauth": Capability.REFRESH,
    "sync_grok2api_status": Capability.SYNC_STATUS,
    "upload_grok2api": Capability.UPLOAD,
}


def capability_of(action_id: str) -> Capability | None:
    """动作 id → 能力。未登记返回 `None`（不抛：调用方要能走「未知动作」分支）。"""
    return ACTION_CAPABILITIES.get(str(action_id or "").strip())


def require_capability(action_id: str) -> Capability:
    """同上，但未登记直接抛 —— 用在「这里必须认识它」的位置。"""
    capability = capability_of(action_id)
    if capability is None:
        raise KeyError(f"未登记的动作 id：{action_id!r}（见 core/capabilities.py）")
    return capability


def actions_with(capability: Capability) -> tuple[str, ...]:
    """该能力下的全部动作 id（声明顺序）。"""
    return tuple(
        action_id
        for action_id, value in ACTION_CAPABILITIES.items()
        if value is capability
    )


def group_by_capability(action_ids: Iterable[str]) -> dict[Capability | None, list[str]]:
    """把一组动作 id 按能力分组，未登记的动作进 `None` 键。"""
    grouped: dict[Capability | None, list[str]] = {}
    for action_id in action_ids:
        grouped.setdefault(capability_of(action_id), []).append(action_id)
    return grouped
