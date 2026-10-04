"""协议层的邮箱抽象。

只保留 ``AuthFlow`` 真正会碰到的那部分接口 —— 本仓库的收件箱实现统一走
``core.base_mailbox.BaseMailbox``，注册引擎用 ``mailbox_adapter`` 把两者对上，
所以导入格式、WebUI 表单自描述那些东西在这里没有意义。

两个能力维度必须分清：

    pooled     号是"买来的、有限的、废了要换下一个"
               → 决定 AuthFlow 超时后要不要 retry、要不要 mark_dead
    ephemeral  地址是不是"每次都新造一个"
               → 决定 OpenAI 把它当新号还是老号

两者正交，四种组合都真实存在：接码池邮箱是 (True, False)，catch-all 域名是
(False, True)，iCloud 隐私邮箱中转是 (False, False)。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Optional

_RE_SPAN_CODE = re.compile(r"<span[^>]*>\s*(\d{6})\s*</span>", re.IGNORECASE)
_RE_EMAIL_ADDR = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RE_TS_BOUNDARY = re.compile(r"m=\+\d+\.")
_RE_TS_PARAM = re.compile(r"[?&]t=\d{10}")
_RE_OTP6 = re.compile(r"(?<!#)(?<!\d)(\d{6})(?!\d)")


class MailProviderError(Exception):
    """provider 统一异常。

    ``fatal=True`` 表示号本身废了（凭证失效 / 被封 / 收件链路不可用），
    ``fatal=False`` 表示环境或网络问题，号是无辜的。
    """

    def __init__(self, message: str, *, fatal: bool = False, kind: str = ""):
        super().__init__(message)
        self.fatal = fatal
        self.kind = kind


def extract_otp(raw: str, code_pattern: Optional[str] = None) -> Optional[str]:
    """从邮件原文提取 6 位 OTP。

    防误判规则按优先级依次是：HTML ``<span>`` 包裹的验证码最可信；其次跳过
    MIME header 只搜正文；再剔除邮箱地址（``user123456@x.com``）、时间戳
    (``m=+XXXXXX.`` / ``t=XXXXXXXXXX``) 和 hex 颜色值。
    """
    if not raw:
        return None

    matched = _RE_SPAN_CODE.search(raw)
    if matched:
        return matched.group(1)

    body_start = raw.find("\r\n\r\n")
    text = raw[body_start:] if body_start != -1 else raw

    text = _RE_EMAIL_ADDR.sub("", text)
    text = _RE_TS_BOUNDARY.sub("", text)
    text = _RE_TS_PARAM.sub("", text)

    pattern = re.compile(code_pattern) if code_pattern else _RE_OTP6
    matched = pattern.search(text)
    if not matched:
        return None
    return matched.group(1) if matched.groups() else matched.group(0)


class MailProvider(ABC):
    """AuthFlow 眼中的邮箱。"""

    kind: str = "base"
    display_name: str = "未命名"

    pooled: bool = False
    ephemeral: bool = False

    # "OpenAI 说这个邮箱已经注册过了" 算不算失败。买来的老号走
    # passwordless_login 拿 token 才是正常流程，不该判失败。
    accepts_existing_account: bool = False

    @abstractmethod
    def create_mailbox(self) -> str:
        """返回本次注册要用的邮箱地址。"""

    @abstractmethod
    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
        exclude_codes: Optional[set] = None,
    ) -> str:
        """阻塞等待 OTP，拿到返回 6 位码，超时抛 ``TimeoutError``。

        ``issued_after`` 是防串号时间窗：只接受这个时间点之后到达的邮件，
        避免读到上一轮遗留的旧验证码。实现必须尊重这个参数。

        ``exclude_codes`` 是**已试过、判定为错**的码：补发重试时把上一轮的码
        排除掉。不排除的话，补发的新邮件还没到时会把同一封信再读一遍、拿同一个
        错码再验一次 —— 白耗一次机会，还可能撞上服务端的
        ``max_check_attempts`` 直接锁死（实测踩过）。实现必须尊重这个参数。
        """

    def peek_otp(
        self,
        email_addr: str,
        issued_after: Optional[float] = None,
        wait: float = 0.0,
    ) -> Optional[str]:
        """瞄一眼收件箱里是不是已经有本轮的码，没有就返回 None。

        ``get_auth_url`` 会带 ``login_hint``，OpenAI 一看到就抢跑发码，比正式
        提交邮箱早约 20 秒。调用方先 peek 一下命中就不用再让服务端补发。

        与 ``wait_for_otp`` 的区别：不阻塞死等、拿不到返回 None 而不是抛异常、
        且必须是非破坏性的（不得把看过的邮件记进 seen 集合）。
        """
        return None

    @property
    def exhausted(self) -> bool:
        """本号是否已判定为不可用（收不到码 / 凭证失效）。"""
        return getattr(self, "_dead", False)

    def mark_dead(self, reason: str = "") -> None:
        """标记本号废掉。非池化 provider 默认无操作。"""
        if self.pooled:
            self._dead = True
