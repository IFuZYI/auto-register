"""「号没了」措辞的唯一判定源。

OpenAI 对已停用/删除的账号会回一些固定措辞 —— 不管出现在登录链的哪一步
（authorize/continue 的 409、密码验证的 401、发码接口的 409…），都应判成
同一件事：**账号已封禁，重登救不回**。此前每个模块各存一份标记表，加一句
新措辞要改三处、漏一处就出现「这条路认得出、那条路认不出」。

用户实测原文（2026-10-06）：

- ``You do not have an account because it has been deleted or deactivated.``
  （账号被停用的报错）
- ``Your sign-in session is no longer valid. Please start over to continue.``
  （封禁账号走登录流程时遇到）

注意与普通 OAuth 流程的 ``invalid_state``（可重开入口重试）区分：这里只认
完整的「号没了」句子，``invalid_state`` 单独出现不算。
"""

from __future__ import annotations

from typing import Any

#: 全部小写后按子串匹配。新增措辞只加在这里。
BANNED_MARKERS = (
    "deleted or deactivated",
    "account_deactivated",
    "account has been deactivated",
    "you do not have an account",
    "sign-in session is no longer valid",
)


def looks_like_banned(text: Any) -> bool:
    """响应文本读起来像不像「账号已封禁」。"""
    value = str(text or "").strip().lower()
    if not value:
        return False
    return any(marker in value for marker in BANNED_MARKERS)


__all__ = ["BANNED_MARKERS", "looks_like_banned"]
