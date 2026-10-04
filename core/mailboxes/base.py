"""邮箱渠道基类与账号对象。

所有渠道都从这里继承。本模块不 import 任何具体渠道，也不依赖
modules/、services/、platforms/（依赖方向见 docs/EXTENDING.md）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Callable
import time


@dataclass
class MailboxAccount:
    email: str
    account_id: str = ""
    extra: dict = None  # 平台额外信息


class BaseMailbox(ABC):
    #: 本次使用它的**注册平台**名（由任务层注入，如 "chatgpt" / "grok"）。
    #:
    #: 邮箱的「消耗」是**按平台**记账的：同一个地址注册过 ChatGPT 之后还能
    #: 注册 Grok（两个平台的账号体系互不相干）。渠道靠它知道该把「谁用掉的」
    #: 记到哪一列、以及取号时该跳过谁用过的地址。留空 = 未知平台，
    #: 此时退回旧的全局行为（对所有平台一起锁），宁可保守也不要错发。
    _platform_name: str = ""

    def _log(self, message: str) -> None:
        log_fn = getattr(self, "_log_fn", None)
        if callable(log_fn):
            log_fn(message)

    def _checkpoint(self, *, consume_skip: bool = True) -> None:
        task_control = getattr(self, "_task_control", None)
        if task_control is None:
            return
        task_control.checkpoint(
            consume_skip=consume_skip,
            attempt_id=getattr(self, "_task_attempt_token", None),
        )

    def _sleep_with_checkpoint(self, seconds: float) -> None:
        remaining = max(float(seconds or 0), 0.0)
        while remaining > 0:
            self._checkpoint()
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def _run_polling_wait(
        self,
        *,
        timeout: int,
        poll_interval: float,
        poll_once: Callable[[], Optional[str]],
        timeout_message: str | None = None,
    ) -> str:
        timeout_seconds = max(int(timeout or 0), 1)
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            self._checkpoint()
            code = poll_once()
            if code:
                return code

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sleep_with_checkpoint(min(float(poll_interval), remaining))

        self._checkpoint()
        raise TimeoutError(timeout_message or f"等待验证码超时 ({timeout_seconds}s)")

    @abstractmethod
    def get_email(self) -> MailboxAccount:
        """获取一个可用邮箱"""
        ...

    @abstractmethod
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        """等待并返回验证码，code_pattern 为自定义正则（默认匹配6位数字）"""
        ...

    def _safe_extract(self, text: str, pattern: str = None) -> Optional[str]:
        """通用验证码提取逻辑：若有捕获组则返回 group(1)，否则返回 group(0)。

        两层防误判（都是实测踩过的，见下方注释）：
        1. **先剥掉邮箱地址**：邮件头/转发标记里常带邮箱，本地部分可能正好含
           6 位数字（实测 `X-ICLOUD-HME: ... f=sample123456@icloud.com` →
           兜底正则抓到 `123456`，而真码是正文里的 `406553`）。
        2. **兜底正则要求字母数字边界**：`(?<![a-zA-Z0-9])` / `(?![a-zA-Z0-9])`，
           防止从 `u24681357` 这类紧贴字母数字的串里截出 6 位。
        """
        import re

        text = str(text or "")
        if not text:
            return None

        # 先剥邮箱地址：本地部分含数字时会把兜底正则带偏（实测踩过）。
        text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", " ", text)

        patterns = []
        if pattern:
            patterns.append(pattern)

        # 先匹配带明显语义的验证码，避免误提取 MIME boundary、时间戳等 6 位数字。
        patterns.extend(
            [
                r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code|验证码|校验码|动态码|認證碼|驗證碼)[^0-9]{0,30}(\d{6})",
                r"(?is)\bcode\b[^0-9]{0,12}(\d{6})",
                # 兜底：两条边界都要。
                #   `(?<!#)` 排除 hex 色值 —— 邮件 HTML 里 `color: #202123`
                #     会先于真码出现，实测抓到它而不是验证码。
                #   `(?<![a-zA-Z0-9])` / `(?![a-zA-Z0-9])` 排除紧贴字母数字的串
                #     （`u24681357`、`sample123456` 这类）。
                r"(?<!#)(?<![a-zA-Z0-9])(\d{6})(?![a-zA-Z0-9])",
            ]
        )

        for regex in patterns:
            m = re.search(regex, text)
            if m:
                # 兼容逻辑：若 pattern 中有捕获组则取 group(1)，否则取 group(0)
                return m.group(1) if m.groups() else m.group(0)
        return None

    def _decode_raw_content(self, raw: str) -> str:
        """解析邮件原始文本 (借鉴自 Fugle)，处理 Quoted-Printable 和 HTML 实体"""
        import quopri, html, re

        text = str(raw or "")
        if not text:
            return ""
        # 简单切分 Header 和 Body
        if "\r\n\r\n" in text:
            text = text.split("\r\n\r\n", 1)[1]
        elif "\n\n" in text:
            text = text.split("\n\n", 1)[1]
        try:
            # 处理 Quoted-Printable
            decoded_bytes = quopri.decodestring(text)
            text = decoded_bytes.decode("utf-8", errors="ignore")
        except Exception:
            pass
        # 清除 HTML 标签并反转义
        text = html.unescape(text)
        text = re.sub(r"(?im)^content-(?:type|transfer-encoding):.*$", " ", text)
        text = re.sub(r"(?im)^--+[_=\w.-]+$", " ", text)
        text = re.sub(r"(?i)----=_part_[\w.]+", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set:
        """返回当前邮件 ID 集合（用于过滤旧邮件）"""
        ...

    # 常见邮件字段名：各渠道返回的 JSON 字段五花八门，统一在这里拼成一段可搜索文本。
    _SEARCH_TEXT_KEYS = (
        "subject",
        "from",
        "from_address",
        "sender",
        "preview",
        "text",
        "content",
        "body",
        "html",
        "html_content",
        "raw",
        "raw_content",
        "mail_text",
    )

    def _build_search_text(self, message: dict) -> str:
        """把邮件 JSON 拼成可搜索文本（无字段可用时退化为整体 JSON）。

        定义在基类而不是某个渠道里：mail.tm 等渠道的正文藏在 html 数组、
        `text` 字段为空，需要按同一套字段名拼接 + 解码。
        """
        import json

        parts = []
        for key in self._SEARCH_TEXT_KEYS:
            value = message.get(key)
            if value:
                parts.append(str(value))

        if not parts:
            parts.append(json.dumps(message, ensure_ascii=False))

        text = " ".join(parts).strip()
        return self._decode_raw_content(text) or text

    def _yyds_safe_extract(self, text: str, pattern: str = None) -> Optional[str]:
        """通用验证码提取逻辑：若有捕获组则返回 group(1)，否则返回 group(0)"""
        import re

        text = str(text or "")
        if not text:
            return None

        # [修复点 1]：优先过滤掉所有 URL 链接，直接从根源防止提取到追踪链接（如 SendGrid）里的随机数字
        text = re.sub(r"https?://\S+", "", text)

        patterns = []
        if pattern:
            # [修复点 2]：如果外部传入了纯 \d{6} 的粗糙正则，自动为其加上字母数字边界
            if pattern in (r"\d{6}", r"(\d{6})"):
                patterns.append(r"(?<![a-zA-Z0-9])(\d{6})(?![a-zA-Z0-9])")
            else:
                patterns.append(pattern)

        # 先匹配带明显语义的验证码，避免误提取 MIME boundary、时间戳等 6 位数字。
        patterns.extend(
            [
                r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code|验证码|校验码|动态码|認證碼|驗證碼)[^0-9]{0,30}(\d{6})",
                r"(?is)\bcode\b[^0-9]{0,12}(\d{6})",
                # [修复点 3]：修改兜底正则，严格要求 6 位数字前后不能有字母或数字（防止匹配 u24681357）
                r"(?<![a-zA-Z0-9])(\d{6})(?![a-zA-Z0-9])",
            ]
        )

        for regex in patterns:
            m = re.search(regex, text)
            if m:
                # 兼容逻辑：若 pattern 中有捕获组则取 group(1)，否则取 group(0)
                return m.group(1) if m.groups() else m.group(0)
        return None

    def _yyds_decode_raw_content(self, raw: str) -> str:
        """解析邮件原始文本 (借鉴自 Fugle)，处理 Quoted-Printable 和 HTML 实体"""
        import quopri, html, re

        text = str(raw or "")
        if not text:
            return ""
            
        # [修复点 4]：只有在明确包含常见邮件 Header 时，才进行 \r\n\r\n 切分。
        # 否则会把已解析好的正文在第一个空行处错误截断 —— 上游返回的纯文本正文
        # 常带普通换行，没有 Header 时不该按邮件原始报文的规则切。
        if re.search(r"(?im)^(?:Return-Path|Received|Date|From|To|Subject|Content-Type):", text):
            if "\r\n\r\n" in text:
                text = text.split("\r\n\r\n", 1)[1]
            elif "\n\n" in text:
                text = text.split("\n\n", 1)[1]
                
        try:
            # 处理 Quoted-Printable
            decoded_bytes = quopri.decodestring(text)
            text = decoded_bytes.decode("utf-8", errors="ignore")
        except Exception:
            pass
        # 清除 HTML 标签并反转义
        text = html.unescape(text)
        text = re.sub(r"(?im)^content-(?:type|transfer-encoding):.*$", " ", text)
        text = re.sub(r"(?im)^--+[_=\w.-]+$", " ", text)
        text = re.sub(r"(?i)----=_part_[\w.]+", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text
