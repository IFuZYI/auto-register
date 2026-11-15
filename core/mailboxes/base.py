"""邮箱渠道基类与账号对象。

所有渠道都从这里继承。本模块不 import 任何具体渠道，也不依赖
modules/、services/、platforms/（依赖方向见 docs/EXTENDING.md）。"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

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
        """睡 `seconds` 秒，可被停止/跳过立刻打断。

        优先走控制器的 `sleep()`（Event 唤醒，响应性与等待长度无关）；
        控制器没提供时退回碎步轮询 —— 测试替身和旧实现都能用。
        """
        control = getattr(self, "_task_control", None)
        sleeper = getattr(control, "sleep", None) if control is not None else None
        if callable(sleeper):
            sleeper(seconds, attempt_id=getattr(self, "_task_attempt_token", None))
            return
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
        is_transient: Callable[[BaseException], bool] | None = None,
        max_consecutive_errors: int = 0,
        interval_schedule: tuple | None = None,
        is_fatal_busy: Callable[[BaseException], bool] | None = None,
    ) -> str:
        """轮询直到拿到码或超时。

        `is_transient` 是**可选**的容错钩子：给了它，`poll_once()` 抛出的
        异常若是「暂时性」的就只当这一轮没读到、继续轮询；没给则完全维持
        旧行为（异常直接穿透）—— 其它渠道零风险。

        为什么需要它：实测 2026-11-13，`icloud-hme` 读超时抛的
        `ICloudError("upstream_unavailable")` 会穿透整个循环，**一次 30 秒
        超时就判整轮注册失败**，既不会补发重试也不会多等一个周期。两批任务
        里约 80% 的失败是这一条。

        `max_consecutive_errors` 是**放弃阈值**（0 = 不因错误放弃）：
        服务真挂时不能让每个 worker 陪跑到时限，连续失败够多就把最后一次
        错误原样抛出去 —— 上层据此判断是「服务问题」而不是「没收到码」。
        """
        timeout_seconds = max(int(timeout or 0), 1)
        deadline = time.monotonic() + timeout_seconds
        consecutive_errors = 0

        while time.monotonic() < deadline:
            self._checkpoint()
            try:
                code = poll_once()
            except Exception as exc:
                # **先问「熔断了吗」，再问「能不能等」** —— 顺序不能反。
                #
                # 熔断表达的是「这个服务现在不值得试」：再等一个轮询周期只会
                # 被同一条熔断再拒一次。它的判据与「暂时性错误」是两套东西，
                # 所以不能挂在 `is_transient` 后面：那样只要熔断异常没被列进
                # 可恢复集合，这条分支就永远走不到（2026-11-14 实测：它确实是
                # 死代码 —— `is_recoverable(UpstreamBusy)` 为 False，异常在上一
                # 行就穿透了）。反过来，一旦有人把熔断加进可恢复集合，5 个
                # worker 就会各自空等到时限，正是熔断想避免的浪费。
                if is_fatal_busy is not None and is_fatal_busy(exc):
                    self._log(f"[收码] 上游熔断中，本轮立刻放弃（不空等）: {exc}")
                    raise
                if is_transient is None or not is_transient(exc):
                    raise
                consecutive_errors += 1
                self._log(
                    f"[收码] 收件通道暂时不可用（连续第 {consecutive_errors} 次），"
                    f"继续轮询: {exc}"
                )
                if max_consecutive_errors and consecutive_errors >= max_consecutive_errors:
                    self._log(
                        f"[收码] 连续 {consecutive_errors} 次读不到收件箱，"
                        f"本轮放弃（服务疑似不可用，而非没收到码）"
                    )
                    raise
            else:
                if code:
                    return code
                consecutive_errors = 0

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if interval_schedule:
                elapsed = timeout_seconds - max(remaining, 0.0)
                interval = self._poll_interval_for(elapsed, interval_schedule)
            else:
                interval = float(poll_interval)
            self._sleep_with_checkpoint(min(interval, remaining))

        self._checkpoint()
        # 走到这里说明时限用尽；此时抛超时（而不是上一次的上游错误）——
        # 中途有过的暂时失败已经被上面的容错循环吸收了。
        raise TimeoutError(timeout_message or f"等待验证码超时 ({timeout_seconds}s)")

    @abstractmethod
    def get_email(self) -> MailboxAccount:
        """获取一个可用邮箱"""
        ...

    @staticmethod
    def _poll_interval_for(elapsed: float, schedule: tuple | None = None) -> float:
        """按「已经等了多久」选轮询间隔。

        验证码通常在前 20 秒就到了；后面那一百多秒的恒定 5 秒轮询基本是
        空转 —— 实测一个 180 秒窗口要打出 30 次请求，其中 24 次是白打。
        分段退避能在命中延迟几乎不变的前提下把请求量砍掉一半。

        `schedule` 形如 `((0, 3.0), (20, 5.0), (60, 10.0))`：每项是
        「从第几秒起」+「间隔」，按 elapsed 落在哪一段取。
        """
        if not schedule:
            return 5.0
        interval = schedule[0][1]
        for threshold, value in schedule:
            if elapsed >= threshold:
                interval = value
            else:
                break
        return float(interval)

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
            logger.debug("忽略异常（保留原行为）", exc_info=True)
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
            logger.debug("忽略异常（保留原行为）", exc_info=True)
        # 清除 HTML 标签并反转义
        text = html.unescape(text)
        text = re.sub(r"(?im)^content-(?:type|transfer-encoding):.*$", " ", text)
        text = re.sub(r"(?im)^--+[_=\w.-]+$", " ", text)
        text = re.sub(r"(?i)----=_part_[\w.]+", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text
