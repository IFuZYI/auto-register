"""微软邮箱的四种取码后端：IMAP / Graph / MailAPI URL（策略模式）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from ...base import BaseMailbox, MailboxAccount


class OutlookMailboxBackend(ABC):
    """Outlook 收信后端策略。"""

    backend_name: str = ""

    def __init__(self, mailbox: "BaseMailbox"):
        # 注：构造时实际传的是 `OutlookMailbox`（定义在 mailbox.py，与本模块
        # 循环引用）。注解用基类 `BaseMailbox` 表达，保证 `typing.get_type_hints`
        # 运行时可解析（此前写 "OutlookMailbox" 未定义 → 反射工具链 NameError）。
        self.mailbox = mailbox

    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set:
        ...

    @abstractmethod
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
        **kwargs,
    ) -> str:
        ...


class OutlookImapMailboxBackend(OutlookMailboxBackend):
    backend_name = "imap"

    def _select_folder(self, imap_conn, folder: str):
        """Prefer read-only selection, then fall back to SELECT for Outlook IMAP."""
        try:
            return imap_conn.select(folder, readonly=True)
        except Exception as exc:
            self.mailbox._log(
                f"[微软邮箱][IMAP] folder={folder} EXAMINE 失败，改用 SELECT: {exc}"
            )
            return imap_conn.select(folder, readonly=False)

    def get_current_ids(self, account: MailboxAccount) -> set:
        imap_conn = None
        try:
            imap_conn = self.mailbox._open_imap(account)
            seen: set[str] = set()
            for folder in self.mailbox._imap_folder_names:
                try:
                    status, _ = self._select_folder(imap_conn, folder)
                except Exception as exc:
                    self.mailbox._log(
                        f"[微软邮箱][IMAP] folder={folder} select 失败，跳过: {exc}"
                    )
                    continue
                if status != "OK":
                    continue
                status, data = imap_conn.uid("search", None, "ALL")
                if status != "OK":
                    continue
                ids = data[0].split() if data and data[0] else []
                for uid in ids[-100:]:
                    uid_str = (
                        uid.decode("utf-8", errors="ignore")
                        if isinstance(uid, bytes)
                        else str(uid)
                    )
                    if uid_str:
                        seen.add(f"{folder}:{uid_str}")
            return seen
        finally:
            try:
                if imap_conn:
                    imap_conn.logout()
            except Exception:
                pass

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
        **kwargs,
    ) -> str:
        from email import message_from_bytes
        from email.utils import parsedate_to_datetime
        from email.policy import default as email_default_policy

        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        otp_sent_at = kwargs.get("otp_sent_at")
        try:
            otp_cutoff = float(otp_sent_at) - 2 if otp_sent_at else None
        except (TypeError, ValueError):
            otp_cutoff = None
        keyword_lower = str(keyword or "").strip().lower()

        def poll_once() -> Optional[str]:
            for folder in self.mailbox._imap_folder_names:
                imap_conn = None
                try:
                    self.mailbox._log(f"[微软邮箱][IMAP] folder={folder} 开始轮询")
                    imap_conn = self.mailbox._open_imap(account)
                    self.mailbox._log(f"[微软邮箱][IMAP] folder={folder} IMAP 登录成功")
                    status, _ = self._select_folder(imap_conn, folder)
                    if status != "OK":
                        self.mailbox._log(
                            f"[微软邮箱][IMAP] folder={folder} select 失败: status={status}"
                        )
                        continue
                    status, data = imap_conn.uid("search", None, "ALL")
                    if status != "OK":
                        self.mailbox._log(
                            f"[微软邮箱][IMAP] folder={folder} search 失败: status={status}"
                        )
                        continue
                    ids = data[0].split() if data and data[0] else []
                    if len(ids) > 50:
                        ids = ids[-50:]
                    new_uids = []
                    for uid in ids:
                        uid_str = (
                            uid.decode("utf-8", errors="ignore")
                            if isinstance(uid, bytes)
                            else str(uid)
                        )
                        seen_key = f"{folder}:{uid_str}"
                        if not uid_str or seen_key in seen:
                            continue
                        seen.add(seen_key)
                        new_uids.append(uid)
                    self.mailbox._log(
                        f"[微软邮箱][IMAP] folder={folder} uid_total={len(ids)} new_uid_count={len(new_uids)}"
                    )
                    for uid in new_uids:
                        status, msg_data = imap_conn.uid("fetch", uid, "(RFC822)")
                        if status != "OK":
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} fetch 失败: uid={uid!r} status={status}"
                            )
                            continue
                        raw = None
                        for item in msg_data or []:
                            if isinstance(item, tuple) and item[1]:
                                raw = item[1]
                                break
                        if not raw:
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} fetch 空响应: uid={uid!r}"
                            )
                            continue
                        msg = message_from_bytes(raw, policy=email_default_policy)
                        subject = self.mailbox._decode_header_value(msg.get("Subject", ""))
                        text = self.mailbox._extract_message_text(msg)
                        if otp_cutoff:
                            try:
                                message_ts = parsedate_to_datetime(
                                    msg.get("Date", "") or ""
                                ).timestamp()
                            except (TypeError, ValueError, OverflowError):
                                message_ts = 0
                            if message_ts and message_ts < otp_cutoff:
                                self.mailbox._log(
                                    f"[微软邮箱][IMAP] folder={folder} 跳过发码前旧邮件: uid={uid_str}"
                                )
                                continue
                        self.mailbox._log(
                            f"[微软邮箱][IMAP] folder={folder} 命中新邮件 subject={subject or '-'}"
                        )
                        if keyword_lower and keyword_lower not in text.lower():
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} 跳过关键字不匹配邮件"
                            )
                            continue
                        code = self.mailbox._safe_extract(text, code_pattern)
                        if not code:
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} 未提取到验证码"
                            )
                            continue
                        if code in exclude_codes:
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} 跳过已尝试验证码: {code}"
                            )
                            continue
                        self.mailbox._log(
                            f"[微软邮箱][IMAP] folder={folder} 验证码提取成功: {code}"
                        )
                        return code
                except Exception as exc:
                    self.mailbox._log(
                        f"[微软邮箱][IMAP] folder={folder} IMAP 查询异常: {exc}"
                    )
                    continue
                finally:
                    try:
                        if imap_conn:
                            imap_conn.logout()
                    except Exception:
                        pass
            return None

        return self.mailbox._run_polling_wait(
            timeout=timeout,
            poll_interval=5,
            poll_once=poll_once,
        )


class OutlookGraphMailboxBackend(OutlookMailboxBackend):
    backend_name = "graph"

    def get_current_ids(self, account: MailboxAccount) -> set:
        if str((account.extra or {}).get("_oauth_backend_capability") or "").strip().lower() == "imap":
            self.mailbox._log("[微软邮箱] Graph OAuth scope 不可用，当前 token 仅支持 IMAP，自动切换 IMAP")
            return self.mailbox._backends["imap"].get_current_ids(account)
        access_token = self.mailbox._get_oauth_access_token(
            account,
            preferred_backend=self.backend_name,
        )
        if str((account.extra or {}).get("_oauth_backend_capability") or "").strip().lower() != "graph":
            self.mailbox._log("[微软邮箱] Graph OAuth scope 不可用，当前 token 仅支持 IMAP，自动切换 IMAP")
            return self.mailbox._backends["imap"].get_current_ids(account)
        seen: set[str] = set()
        for folder in self.mailbox._graph_folder_names:
            try:
                messages = self.mailbox._graph_list_messages(
                    access_token=access_token,
                    folder=folder,
                )
                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if message_id:
                        seen.add(f"{folder}:{message_id}")
            except RuntimeError as exc:
                if "HTTP 401" in str(exc):
                    # 401 → token 失效，强制刷新后重试一次
                    self.mailbox._log(
                        f"[微软邮箱][Graph] get_current_ids folder={folder} 遇到 401，强制刷新 token"
                    )
                    _cache = (account.extra or {}).get("_oauth_token_cache")
                    if isinstance(_cache, dict):
                        _cache.pop(
                            self.mailbox._normalize_backend_name(self.backend_name), None
                        )
                    access_token = self.mailbox._get_oauth_access_token(
                        account,
                        preferred_backend=self.backend_name,
                    )
                    try:
                        messages = self.mailbox._graph_list_messages(
                            access_token=access_token,
                            folder=folder,
                        )
                        for message in messages:
                            message_id = str(message.get("id") or "").strip()
                            if message_id:
                                seen.add(f"{folder}:{message_id}")
                    except Exception:
                        pass
                else:
                    raise
        return seen

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
        **kwargs,
    ) -> str:
        if str((account.extra or {}).get("_oauth_backend_capability") or "").strip().lower() == "imap":
            self.mailbox._log("[微软邮箱] Graph OAuth scope 不可用，当前 token 仅支持 IMAP，自动切换 IMAP")
            return self.mailbox._backends["imap"].wait_for_code(
                account,
                keyword=keyword,
                timeout=timeout,
                before_ids=before_ids,
                code_pattern=code_pattern,
                **kwargs,
            )
        self.mailbox._get_oauth_access_token(account, preferred_backend=self.backend_name)
        if str((account.extra or {}).get("_oauth_backend_capability") or "").strip().lower() != "graph":
            self.mailbox._log("[微软邮箱] Graph OAuth scope 不可用，当前 token 仅支持 IMAP，自动切换 IMAP")
            return self.mailbox._backends["imap"].wait_for_code(
                account,
                keyword=keyword,
                timeout=timeout,
                before_ids=before_ids,
                code_pattern=code_pattern,
                **kwargs,
            )
        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        keyword_lower = str(keyword or "").strip().lower()

        # 标记是否已做过一次 401 强制刷 token，避免无限循环
        _token_refreshed = False

        def _force_refresh_token() -> str:
            """清除 OAuth 缓存，强制重新获取 access token。"""
            _cache = (account.extra or {}).get("_oauth_token_cache")
            if isinstance(_cache, dict):
                _cache.pop(
                    self.mailbox._normalize_backend_name(self.backend_name), None
                )
            return self.mailbox._get_oauth_access_token(
                account,
                preferred_backend=self.backend_name,
            )

        def poll_once() -> Optional[str]:
            nonlocal _token_refreshed
            access_token = self.mailbox._get_oauth_access_token(
                account,
                preferred_backend=self.backend_name,
            )
            for folder in self.mailbox._graph_folder_names:
                try:
                    self.mailbox._log(f"[微软邮箱][Graph] folder={folder} 开始轮询")
                    messages = self.mailbox._graph_list_messages(
                        access_token=access_token,
                        folder=folder,
                    )
                    new_messages = []
                    for message in messages:
                        message_id = str(message.get("id") or "").strip()
                        seen_key = f"{folder}:{message_id}"
                        if not message_id or seen_key in seen:
                            continue
                        seen.add(seen_key)
                        new_messages.append(message)
                    self.mailbox._log(
                        f"[微软邮箱][Graph] folder={folder} message_total={len(messages)} new_count={len(new_messages)}"
                    )
                    for message in new_messages:
                        subject = str(message.get("subject") or "").strip()
                        text = self.mailbox._graph_message_text(message)
                        self.mailbox._log(
                            f"[微软邮箱][Graph] folder={folder} 命中新邮件 subject={subject or '-'}"
                        )
                        if keyword_lower and keyword_lower not in text.lower():
                            self.mailbox._log(
                                f"[微软邮箱][Graph] folder={folder} 跳过关键字不匹配邮件"
                            )
                            continue
                        code = self.mailbox._safe_extract(text, code_pattern)
                        if not code:
                            message_id = str(message.get("id") or "").strip()
                            if message_id:
                                detail = self.mailbox._graph_get_message(
                                    access_token=access_token,
                                    message_id=message_id,
                                )
                                text = self.mailbox._graph_message_text(detail)
                                code = self.mailbox._safe_extract(text, code_pattern)
                        if not code:
                            self.mailbox._log(
                                f"[微软邮箱][Graph] folder={folder} 未提取到验证码"
                            )
                            continue
                        if code in exclude_codes:
                            self.mailbox._log(
                                f"[微软邮箱][Graph] folder={folder} 跳过已尝试验证码: {code}"
                            )
                            continue
                        self.mailbox._log(
                            f"[微软邮箱][Graph] folder={folder} 验证码提取成功: {code}"
                        )
                        return code
                except Exception as exc:
                    exc_str = str(exc)
                    # 401 → token 失效，强制刷新后重试一次
                    if "HTTP 401" in exc_str and not _token_refreshed:
                        _token_refreshed = True
                        self.mailbox._log(
                            f"[微软邮箱][Graph] folder={folder} 遇到 401，强制刷新 token 后重试"
                        )
                        try:
                            access_token = _force_refresh_token()
                            messages = self.mailbox._graph_list_messages(
                                access_token=access_token,
                                folder=folder,
                            )
                            new_messages = []
                            for message in messages:
                                message_id = str(message.get("id") or "").strip()
                                seen_key = f"{folder}:{message_id}"
                                if not message_id or seen_key in seen:
                                    continue
                                seen.add(seen_key)
                                new_messages.append(message)
                            for message in new_messages:
                                subject = str(message.get("subject") or "").strip()
                                text = self.mailbox._graph_message_text(message)
                                if keyword_lower and keyword_lower not in text.lower():
                                    continue
                                code = self.mailbox._safe_extract(text, code_pattern)
                                if not code:
                                    mid = str(message.get("id") or "").strip()
                                    if mid:
                                        detail = self.mailbox._graph_get_message(
                                            access_token=access_token,
                                            message_id=mid,
                                        )
                                        text = self.mailbox._graph_message_text(detail)
                                        code = self.mailbox._safe_extract(text, code_pattern)
                                if code and code not in exclude_codes:
                                    self.mailbox._log(
                                        f"[微软邮箱][Graph] folder={folder} 刷新 token 后验证码提取成功: {code}"
                                    )
                                    return code
                        except Exception as retry_exc:
                            self.mailbox._log(
                                f"[微软邮箱][Graph] folder={folder} 刷新 token 后仍然失败: {retry_exc}"
                            )
                        continue
                    self.mailbox._log(
                        f"[微软邮箱][Graph] folder={folder} 查询异常: {exc}"
                    )
                    continue
            return None

        return self.mailbox._run_polling_wait(
            timeout=timeout,
            poll_interval=5,
            poll_once=poll_once,
        )


class MailApiUrlOtpBackend(OutlookMailboxBackend):
    backend_name = "mailapi_url"

    @staticmethod
    def _code_key(code: str) -> str:
        return f"mailapi_code:{str(code or '').strip()}"

    def _fetch_mailapi_text(self, account: MailboxAccount) -> str:
        import requests

        extra = account.extra or {}
        url = str(extra.get("mailapi_url") or "").strip()
        if not url:
            raise RuntimeError("mailapi_url 为空，无法轮询取码")
        response = requests.get(
            url,
            timeout=15,
            proxies=getattr(self.mailbox, "_proxy", None),
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"MailAPI 取码请求失败: HTTP {response.status_code}"
            )
        return str(response.text or "")

    def _extract_code(self, text: str, code_pattern: str | None) -> str:
        # MailAPI 返回的是网页/JSON，不是原始邮件：按邮件头切分会从第一个空行处
        # 把正文腰斩（分享页的 <style> 里就有空行），码常常正好落在被砍掉的那半边。
        # 提取也走剥链接的那版，免得把 SendGrid 追踪链接里的数字当成验证码。
        normalized_text = self.mailbox._yyds_decode_raw_content(text) or str(text or "")
        return str(self.mailbox._yyds_safe_extract(normalized_text, code_pattern) or "").strip()

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            text = self._fetch_mailapi_text(account)
            code = self._extract_code(text, None)
            return {self._code_key(code)} if code else set()
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
        **kwargs,
    ) -> str:
        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        keyword_lower = str(keyword or "").strip().lower()

        def poll_once() -> Optional[str]:
            try:
                text = self._fetch_mailapi_text(account)
            except Exception as exc:
                self.mailbox._log(f"[MailAPI] 拉取失败: {exc}")
                return None

            if keyword_lower and keyword_lower not in str(text).lower():
                return None
            code = self._extract_code(text, code_pattern)
            if not code:
                return None
            if code in exclude_codes:
                self.mailbox._log(f"[MailAPI] 跳过已尝试验证码: {code}")
                return None
            code_key = self._code_key(code)
            if code_key in seen:
                return None
            seen.add(code_key)
            self.mailbox._log(f"[MailAPI] 收到验证码: {code}")
            return code

        return self.mailbox._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )

