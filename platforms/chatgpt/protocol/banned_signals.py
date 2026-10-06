"""「号没了」措辞的唯一判定源。

OpenAI 对已停用/删除的账号会回一些固定措辞 —— 不管出现在登录链的哪一步
（authorize/continue 的 409、密码验证的 401、发码接口的 409…），都应判成
同一件事：**账号已封禁，重登救不回**。此前每个模块各存一份标记表，加一句
新措辞要改三处、漏一处就出现「这条路认得出、那条路认不出」。

用户实测原文（2026-10-06）：

- ``You do not have an account because it has been deleted or deactivated.``
  （账号被停用的报错）—— 这是封禁判定唯一的措辞来源。

用户修正（2026-10-06）：``Your sign-in session is no longer valid. Please
start over to continue.`` / ``invalid_state`` **不是封禁** —— 那是会话
失效或 OAuth state 参数不匹配（Cookie、会话或跳转不同步），可重开重试。
不要把这两句认成封禁（此前误认，实测把正常账号标成了「禁用」）。
"""

from __future__ import annotations

from typing import Any

#: 全部小写后按子串匹配。新增措辞只加在这里。
#: **只收「号没了」的措辞** —— 会话/state 类错误（invalid_state 等）可重试，
#: 不能进这张表。
BANNED_MARKERS = (
    "deleted or deactivated",
    "account_deactivated",
    "account has been deactivated",
    "you do not have an account",
)


def looks_like_banned(text: Any) -> bool:
    """响应文本读起来像不像「账号已封禁」。"""
    value = str(text or "").strip().lower()
    if not value:
        return False
    return any(marker in value for marker in BANNED_MARKERS)


__all__ = ["BANNED_MARKERS", "looks_like_banned"]
