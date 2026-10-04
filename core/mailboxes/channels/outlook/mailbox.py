"""OutlookMailbox：微软邮箱本地账号池（号池取号、OAuth、Graph/IMAP 取件）。"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

from ....proxy_utils import build_requests_proxy_config
from ...base import BaseMailbox, MailboxAccount
from .backends import (
    MailApiUrlOtpBackend,
    OutlookGraphMailboxBackend,
    OutlookImapMailboxBackend,
    OutlookMailboxBackend,
)


class OutlookMailbox(BaseMailbox):
    """微软邮箱（Outlook / Hotmail）本地账号池（Graph / IMAP 策略）"""

    # 类级别锁：保证多线程并发时取号互斥，防止多个实例取到同一个邮箱
    _pop_lock = threading.Lock()

    def __init__(
        self,
        imap_server: str = "",
        imap_port: int | str = 993,
        token_endpoint: str = "",
        backend: str = "graph",
        graph_api_base: str = "",
        mail_import_source: str = "",
        proxy: str = None,
    ):
        self._lock = threading.Lock()
        self._mail_import_source = str(mail_import_source or "").strip().lower()
        self._pool_account_type = self._resolve_pool_account_type(mail_import_source)
        self._proxy = build_requests_proxy_config(proxy)
        self._imap_servers = []
        if imap_server:
            self._imap_servers.append(str(imap_server).strip())
        else:
            try:
                from core.outlook_constants import OUTLOOK_IMAP_SERVERS

                self._imap_servers.extend(
                    [
                        str(OUTLOOK_IMAP_SERVERS.get("NEW") or "").strip(),
                        str(OUTLOOK_IMAP_SERVERS.get("OLD") or "").strip(),
                    ]
                )
            except Exception:
                self._imap_servers.extend(
                    ["outlook.live.com", "outlook.office365.com"]
                )
        self._imap_servers = [
            host for host in self._imap_servers if isinstance(host, str) and host
        ]
        try:
            self._imap_port = int(imap_port or 993)
        except (TypeError, ValueError):
            self._imap_port = 993
        self._token_endpoint = str(token_endpoint or "").strip()
        self._backend_name = self._normalize_backend_name(backend)
        self._graph_api_base = (
            str(graph_api_base or "").strip() or "https://graph.microsoft.com/v1.0"
        )
        self._imap_folder_names = ["INBOX", "Junk", "Deleted Items", "Trash"]
        self._graph_folder_names = ["inbox", "junkemail", "deleteditems"]
        self._backends: dict[str, OutlookMailboxBackend] = {
            "imap": OutlookImapMailboxBackend(self),
            "graph": OutlookGraphMailboxBackend(self),
            "mailapi_url": MailApiUrlOtpBackend(self),
        }

    @staticmethod
    def _normalize_backend_name(value: Any) -> str:
        backend = str(value or "graph").strip().lower() or "graph"
        return backend if backend in {"graph", "imap"} else "graph"

    @staticmethod
    def _normalize_account_type(value: Any) -> str:
        account_type = str(value or "").strip().lower()
        if account_type in {"mailapi_url", "microsoft_oauth"}:
            return account_type
        return "microsoft_oauth"

    @staticmethod
    def _resolve_pool_account_type(mail_import_source: Any) -> str:
        from ....mail_import_sources import resolve_pool_account_type

        return resolve_pool_account_type(mail_import_source)

    @staticmethod
    def _describe_pool_account_type(account_type: Any) -> str:
        from ....mail_import_sources import describe_pool_account_type

        return describe_pool_account_type(account_type)

    @staticmethod
    def _account_type_matches(account_type: Any, wanted: str):
        """老库里 account_type 可能是 NULL 或空串，那些行都是 OAuth 号。"""
        from sqlalchemy import func, or_

        normalized = func.lower(func.trim(func.coalesce(account_type, "")))
        if wanted == "microsoft_oauth":
            return or_(normalized == "microsoft_oauth", normalized == "")
        return normalized == wanted

    def _is_mailapi_account(self, account: MailboxAccount) -> bool:
        extra = getattr(account, "extra", None) or {}
        account_type = self._normalize_account_type(extra.get("account_type"))
        if account_type == "mailapi_url":
            return True
        return bool(str(extra.get("mailapi_url") or "").strip())

    def _pop_account(self) -> dict:
        from sqlalchemy import func, or_
        from sqlmodel import Session, select
        from core.db import (
            AccountModel,
            OutlookAccountModel,
            _utcnow,
            email_used_by_platform,
            mailbox_pool_session,
            normalize_email,
        )

        wanted_type = self._pool_account_type
        if wanted_type:
            self._log(
                "[微软邮箱] 号池筛选: "
                f"mail_import_source={self._mail_import_source or '(未设置)'} "
                f"只取 account_type={wanted_type}"
                f"（{self._describe_pool_account_type(wanted_type)}）"
            )
        else:
            self._log("[微软邮箱] 号池筛选: 未指定导入类型，整池取号")

        with OutlookMailbox._pop_lock:
            # 「这个平台已经用过的地址不再拿来建号」这条过滤必须**先查主库拿到
            # 名单**，不能写成子查询塞进下面这条 SQL。
            #
            # 踩过的坑：这里原本是 `func.lower(email).notin_(select(AccountModel.email))`，
            # 而 `accounts` 表在默认库、`outlook_accounts` 在池库（两个文件）。SQLite
            # 不跨库解析表名，整条语句直接抛 `no such table: accounts` —— 500 发生在
            # 导入/取号那一刻，看起来像「导入功能坏了」，实际是库没分对。
            #
            # **按平台过滤**：只排除「本平台」已经注册过的地址。邮箱是一次性的，
            # 但只对用掉它的那个平台一次性 —— 一个地址注册过 ChatGPT 之后还能
            # 注册 Grok，所以不能把别的平台的账号也算进来（那会让池子迅速枯竭）。
            from core.db import current_engine

            wanted_platform = str(getattr(self, "_platform_name", "") or "").strip().lower()
            registered_emails: set[str] = set()
            with Session(current_engine()) as main_session:
                query = select(AccountModel.email)
                if wanted_platform:
                    query = query.where(AccountModel.platform == wanted_platform)
                registered_emails = {
                    normalize_email(email)
                    for email in main_session.exec(query).all()
                    if str(email or "").strip()
                }

            with mailbox_pool_session("outlook") as session:
                # 导入记录永久保留，取号只筛 available 并标记 in_use；同时按
                # accounts 表兜一道，注册过的地址不再拿来创建新账号。
                #
                # 注意这里**只认 available**：`unpooled`（未入池）的账号是用户
                # 导入后还没勾选「导入邮箱池」的，取号必须跳过它们 —— 用户明确
                # 要求「选择导入才能导入邮箱池，不是全部导入」。
                #
                # `used` 也在候选里：邮箱只对**用掉它的那个平台**一次性，
                # 被别的平台用过的地址还能再给本平台用（`used_platforms`
                # 见 core/db/base.py）。真正该排除的是「本平台已注册过」——
                # 那条由上面的 `registered_emails` 过滤兜住，所以这里放开
                # `used` 是安全的；本平台没用过的 `used` 行会在下面按
                # `used_platforms` 再筛一遍。
                query = (
                    select(OutlookAccountModel)
                    .where(OutlookAccountModel.enabled == True)
                    .where(
                        or_(
                            OutlookAccountModel.status == "available",
                            OutlookAccountModel.status == "used",
                            OutlookAccountModel.status == None,
                            OutlookAccountModel.status == "",
                        )
                    )
                    .order_by(OutlookAccountModel.id)
                )
                if registered_emails:
                    query = query.where(
                        func.lower(OutlookAccountModel.email).notin_(registered_emails)
                    )
                if wanted_type:
                    query = query.where(
                        self._account_type_matches(
                            OutlookAccountModel.account_type, wanted_type
                        )
                    )
                account = None
                for candidate in session.exec(query).all():
                    status = str(getattr(candidate, "status", "") or "").strip().lower()
                    if status == "used":
                        # 至少有一个平台用掉了它；只有本平台没用过才复用。
                        # 平台未知时保守跳过（宁可少发，也不要把一个已经
                        # 注册过的地址再发给同一个平台）。
                        if not wanted_platform:
                            continue
                        if email_used_by_platform(
                            getattr(candidate, "used_platforms", ""), wanted_platform
                        ):
                            continue
                    account = candidate
                    break
                if not account:
                    available_left = select(func.count(OutlookAccountModel.id)).where(
                        OutlookAccountModel.enabled == True,
                        or_(
                            OutlookAccountModel.status == "available",
                            OutlookAccountModel.status == None,
                            OutlookAccountModel.status == "",
                        ),
                    )
                    total_left = session.exec(available_left).one()
                    # 池里一个可领的都没有。先看是不是「导入了但没入池」——
                    # 那种情况下「都已经注册过了/请导入新邮箱」是误导：用户刚
                    # 粘贴完一屏邮箱，看到「请导入」只会以为自己没导入成功。
                    if not total_left:
                        unpooled_left = session.exec(
                            select(func.count(OutlookAccountModel.id)).where(
                                OutlookAccountModel.status == "unpooled"
                            )
                        ).one()
                        if unpooled_left:
                            raise RuntimeError(
                                f"微软邮箱账号池里有 {unpooled_left} 个已导入但**未入池**的"
                                "地址：它们还没被勾选「导入邮箱池」，注册不会取用。"
                                "请到「邮箱服务 > Outlook（本地）」的预览表里勾选要用的"
                                "邮箱，点「导入邮箱池」后再启动任务。"
                            )
                    if wanted_type:
                        same_type_left = session.exec(
                            available_left.where(
                                self._account_type_matches(
                                    OutlookAccountModel.account_type, wanted_type
                                )
                            )
                        ).one()
                        label = self._describe_pool_account_type(wanted_type)
                        if same_type_left:
                            raise RuntimeError(
                                f"微软邮箱账号池里剩下的 {same_type_left} 个 {label} 地址"
                                "都已经注册过了，请导入新的邮箱"
                            )
                        raise RuntimeError(
                            f"邮箱导入类型选的是 {label}，但微软邮箱账号池里没有"
                            f" account_type={wanted_type} 的账号"
                            f"（池里还剩 {total_left} 个其它类型的账号，不会拿来顶替），"
                            "请按该类型导入邮箱，或到设置页改回对应的导入类型"
                        )
                    if total_left:
                        raise RuntimeError(
                            f"微软邮箱账号池里剩下的 {total_left} 个地址都已经注册过了，"
                            "请导入新的邮箱"
                        )
                    raise RuntimeError("微软邮箱账号池为空，请先在设置页批量导入")

                payload = {
                    "id": account.id,
                    "email": account.email,
                    "password": account.password,
                    "client_id": account.client_id,
                    "refresh_token": account.refresh_token,
                    "account_type": getattr(account, "account_type", "microsoft_oauth"),
                    "mailapi_url": getattr(account, "mailapi_url", ""),
                }
                account.status = "in_use"
                account.last_used = _utcnow()
                account.updated_at = _utcnow()
                session.add(account)
                session.commit()
                return payload

    def get_email(self) -> MailboxAccount:
        payload = self._pop_account()
        email = str(payload.get("email") or "").strip()
        if not email:
            raise RuntimeError("微软邮箱账号邮箱为空")
        password = str(payload.get("password") or "")
        client_id = str(payload.get("client_id") or "")
        refresh_token = str(payload.get("refresh_token") or "")
        account_type = self._normalize_account_type(payload.get("account_type"))
        mailapi_url = str(payload.get("mailapi_url") or "").strip()
        auth_mode = (
            "mailapi_url"
            if account_type == "mailapi_url"
            else ("oauth" if client_id and refresh_token else "password")
        )
        self._log(f"[微软邮箱] 取出账号: {email}（已从本地池移除）")
        self._log(
            "[微软邮箱] 账号认证信息: "
            f"has_password={bool(password)} "
            f"has_client_id={bool(client_id)} "
            f"has_refresh_token={bool(refresh_token)} "
            f"has_mailapi_url={bool(mailapi_url)} "
            f"account_type={account_type} "
            f"auth_mode={auth_mode}"
        )
        return MailboxAccount(
            email=email,
            account_id=str(payload.get("id") or ""),
            extra={
                "provider": "microsoft",
                "password": password,
                "client_id": client_id,
                "refresh_token": refresh_token,
                "account_type": account_type,
                "mailapi_url": mailapi_url,
                "outlook_backend": self._backend_name,
            },
        )

    def requeue_account(self, account: MailboxAccount) -> None:
        from sqlmodel import Session, select
        from core.db import OutlookAccountModel, _utcnow, mailbox_pool_session

        email = str(getattr(account, "email", "") or "").strip()
        extra = getattr(account, "extra", None) or {}
        if not email:
            return

        password = str(extra.get("password") or "")
        client_id = str(extra.get("client_id") or "")
        refresh_token = str(extra.get("refresh_token") or "")
        account_type = self._normalize_account_type(extra.get("account_type"))
        mailapi_url = str(extra.get("mailapi_url") or "")

        with self._lock:
            with mailbox_pool_session("outlook") as session:
                existing = session.exec(
                    select(OutlookAccountModel).where(OutlookAccountModel.email == email)
                ).first()
                if existing:
                    existing.password = password
                    existing.client_id = client_id
                    existing.refresh_token = refresh_token
                    existing.account_type = account_type
                    existing.mailapi_url = mailapi_url
                    existing.enabled = True
                    existing.status = "available"
                    existing.updated_at = _utcnow()
                    session.add(existing)
                else:
                    session.add(
                        OutlookAccountModel(
                            email=email,
                            password=password,
                            client_id=client_id,
                            refresh_token=refresh_token,
                            account_type=account_type,
                            mailapi_url=mailapi_url,
                            enabled=True,
                            status="available",
                            created_at=_utcnow(),
                            updated_at=_utcnow(),
                        )
                    )
                session.commit()
        self._log(f"[微软邮箱] 账号已回退到本地池: {email}")

    def set_account_status(self, account: MailboxAccount, status: str) -> None:
        """更新已领取邮箱的生命周期状态，保留原始导入记录。"""
        allowed = {"available", "in_use", "used", "failed"}
        normalized = str(status or "").strip().lower()
        if normalized not in allowed:
            raise ValueError(f"未知邮箱池状态: {status}")
        from sqlmodel import Session, select
        from core.db import (
            OutlookAccountModel,
            _utcnow,
            mailbox_pool_session,
            mark_email_used_by,
        )

        email = str(getattr(account, "email", "") or "").strip()
        account_id = str(getattr(account, "account_id", "") or "").strip()
        wanted_platform = str(getattr(self, "_platform_name", "") or "").strip().lower()
        with self._lock:
            with mailbox_pool_session("outlook") as session:
                row = None
                if account_id.isdigit():
                    row = session.get(OutlookAccountModel, int(account_id))
                if row is None and email:
                    row = session.exec(
                        select(OutlookAccountModel).where(OutlookAccountModel.email == email)
                    ).first()
                if row is None:
                    return
                row.status = normalized
                if normalized == "used":
                    # 记下「被哪个平台用掉的」—— 邮箱只对用掉它的那个平台
                    # 一次性，别的平台取号时还能再领它（`used_platforms`
                    # 见 core/db/base.py）。
                    row.used_platforms = mark_email_used_by(
                        getattr(row, "used_platforms", ""), wanted_platform
                    )
                row.updated_at = _utcnow()
                session.add(row)
                session.commit()

    @staticmethod
    def import_accounts_to_pool(account_ids: list[int]) -> dict:
        """把指定的未入池账号转成可领取（`unpooled` → `available`）。

        与 iCloud 别名池的 `import_aliases_to_pool` 同一套语义：**只动 unpooled
        的行**，已经在池里（available/in_use/used）或失败的账号一律跳过并计数。
        跳过而不是无脑改，是因为这个动作可能是重复点击或界面状态过期 —— 把
        `in_use` 改回 `available` 会让同一个号被领两次，注册任务互相顶掉邮件。

        返回 `{changed, skipped, remaining_unpooled}` 供界面给准确提示。
        """
        from sqlmodel import select
        from core.db import OutlookAccountModel, _utcnow, mailbox_pool_session

        wanted = [int(i) for i in (account_ids or []) if str(i).strip().isdigit()]
        if not wanted:
            return {"changed": 0, "skipped": 0, "remaining_unpooled": 0}

        changed = 0
        with OutlookMailbox._pop_lock:
            with mailbox_pool_session("outlook") as session:
                rows = session.exec(
                    select(OutlookAccountModel).where(OutlookAccountModel.id.in_(wanted))
                ).all()
                for row in rows:
                    if str(getattr(row, "status", "") or "").strip().lower() != "unpooled":
                        continue
                    row.status = "available"
                    row.updated_at = _utcnow()
                    session.add(row)
                    changed += 1
                session.commit()

                remaining = session.exec(
                    select(OutlookAccountModel).where(
                        OutlookAccountModel.status == "unpooled"
                    )
                ).all()

        return {
            "changed": changed,
            "skipped": max(len(wanted) - changed, 0),
            "remaining_unpooled": len(remaining),
        }

    @staticmethod
    def pool_status_summary() -> dict:
        """号池状态计数，含 `unpooled`（未入池）与 `total`。"""
        from sqlalchemy import func
        from sqlmodel import select
        from core.db import OutlookAccountModel, mailbox_pool_session

        with mailbox_pool_session("outlook") as session:
            rows = session.exec(
                select(OutlookAccountModel.status, func.count(OutlookAccountModel.id)).group_by(
                    OutlookAccountModel.status
                )
            ).all()

        summary = {"unpooled": 0, "available": 0, "in_use": 0, "used": 0, "failed": 0}
        total = 0
        for status, count in rows:
            key = str(status or "available").strip().lower() or "available"
            summary[key] = summary.get(key, 0) + int(count or 0)
            total += int(count or 0)
        summary["total"] = total
        return summary

    def _token_endpoints(self) -> list[str]:
        if self._token_endpoint:
            return [self._token_endpoint]
        try:
            from core.outlook_constants import MICROSOFT_TOKEN_ENDPOINTS

            return [
                MICROSOFT_TOKEN_ENDPOINTS.get("CONSUMERS", ""),
                MICROSOFT_TOKEN_ENDPOINTS.get("LIVE", ""),
                MICROSOFT_TOKEN_ENDPOINTS.get("COMMON", ""),
            ]
        except Exception:
            return [
                "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                "https://login.live.com/oauth20_token.srf",
                "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            ]

    def _oauth_scope_candidates(
        self,
        preferred_backend: str | None = None,
    ) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        try:
            from core.outlook_constants import MICROSOFT_SCOPES

            scope_map = {
                "imap_new": str(MICROSOFT_SCOPES.get("IMAP_NEW") or "").strip(),
                "outlook_default": "https://outlook.office.com/.default offline_access",
                "graph_default": str(MICROSOFT_SCOPES.get("GRAPH_API") or "").strip(),
                "empty": "",
            }
        except Exception:
            scope_map = {
                "imap_new": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
                "outlook_default": "https://outlook.office.com/.default offline_access",
                "graph_default": "https://graph.microsoft.com/.default",
                "empty": "",
            }

        backend = self._normalize_backend_name(preferred_backend or self._backend_name)
        ordered_labels = (
            ["graph_default", "outlook_default", "imap_new", "empty"]
            if backend == "graph"
            else ["imap_new", "outlook_default", "graph_default", "empty"]
        )
        raw_candidates = [(label, scope_map.get(label, "")) for label in ordered_labels]

        seen = set()
        for label, scope in raw_candidates:
            key = (str(label or "").strip(), str(scope or "").strip())
            if key in seen:
                continue
            seen.add(key)
            candidates.append(key)
        return candidates

    def probe_oauth_availability(
        self,
        *,
        email: str,
        client_id: str,
        refresh_token: str,
        preferred_backend: str | None = None,
    ) -> dict[str, Any]:
        if not client_id or not refresh_token:
            self._log(
                f"[微软邮箱] OAuth token 跳过: email={email} has_client_id={bool(client_id)} has_refresh_token={bool(refresh_token)}"
            )
            return {
                "ok": False,
                "reason": "missing_oauth_credentials",
                "message": "缺少 client_id 或 refresh_token，无法通过微软邮箱可用性检测",
            }

        import requests

        last_error = ""
        for endpoint in self._token_endpoints():
            endpoint = str(endpoint or "").strip()
            if not endpoint:
                continue
            for scope_label, scope in self._oauth_scope_candidates(preferred_backend):
                payload = {
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                }
                if scope:
                    payload["scope"] = scope
                try:
                    self._log(
                        "[微软邮箱] OAuth token 请求: "
                        f"email={email} endpoint={endpoint} scope_label={scope_label} has_scope={bool(scope)}"
                    )
                    resp = requests.post(
                        endpoint,
                        data=payload,
                        timeout=20,
                        proxies=self._proxy,
                    )
                    self._log(
                        "[微软邮箱] OAuth token 响应: "
                        f"email={email} endpoint={endpoint} scope_label={scope_label} status={resp.status_code}"
                    )
                except Exception as exc:
                    last_error = str(exc)
                    self._log(
                        "[微软邮箱] OAuth token 请求异常: "
                        f"email={email} endpoint={endpoint} scope_label={scope_label} error={exc}"
                    )
                    continue

                body_text = str(resp.text or "")[:500]
                if resp.status_code >= 400:
                    self._log(f"[微软邮箱] OAuth token 失败响应: {body_text[:200]}")
                    lowered = body_text.lower()
                    if "invalid_grant" in lowered and "service abuse mode" in lowered:
                        return {
                            "ok": False,
                            "reason": "service_abuse_mode",
                            "message": "微软邮箱可用性检测未通过，账号处于 service abuse mode",
                            "status_code": resp.status_code,
                            "endpoint": endpoint,
                            "scope_label": scope_label,
                        }
                    last_error = body_text or f"HTTP {resp.status_code}"
                    continue

                try:
                    data = resp.json() if resp.content else {}
                    access_token = str(data.get("access_token") or "").strip()
                    if access_token:
                        expires_in = data.get("expires_in")
                        try:
                            expires_in_value = max(int(expires_in or 0), 0)
                        except (TypeError, ValueError):
                            expires_in_value = 0
                        self._log(
                            f"[微软邮箱] OAuth access token 获取成功: {email} (scope_label={scope_label})"
                        )
                        return {
                            "ok": True,
                            "reason": "ok",
                            "message": "微软邮箱可用性检测通过",
                            "access_token": access_token,
                            "scope_label": scope_label,
                            "endpoint": endpoint,
                            "expires_in": expires_in_value,
                        }

                    self._log(
                        f"[微软邮箱] OAuth token 响应未包含 access_token: keys={sorted(list(data.keys()))[:10]}"
                    )
                    last_error = body_text or "OAuth 响应未包含 access_token"
                except Exception as exc:
                    last_error = body_text or str(exc) or "OAuth 响应解析失败"
                    self._log(
                        "[微软邮箱] OAuth token 响应解析异常: "
                        f"email={email} endpoint={endpoint} scope_label={scope_label} error={exc}"
                    )
                    continue

        return {
            "ok": False,
            "reason": "oauth_token_failed",
            "message": f"微软邮箱可用性检测未通过: {last_error or 'OAuth token 获取失败'}",
        }

    def _fetch_oauth_token_bundle(
        self,
        *,
        email: str,
        client_id: str,
        refresh_token: str,
        preferred_backend: str | None = None,
    ) -> dict[str, Any]:
        probe = self.probe_oauth_availability(
            email=email,
            client_id=client_id,
            refresh_token=refresh_token,
            preferred_backend=preferred_backend,
        )
        if probe.get("ok"):
            return {
                "access_token": str(probe.get("access_token") or ""),
                "scope_label": probe.get("scope_label", ""),
                "expires_in": probe.get("expires_in", 0),
                "endpoint": probe.get("endpoint", ""),
            }
        self._log(f"[微软邮箱] OAuth token 获取失败，回退密码登录: {email}")
        return {"reason": str(probe.get("reason") or "")}

    def _fetch_oauth_token(
        self,
        *,
        email: str,
        client_id: str,
        refresh_token: str,
        preferred_backend: str | None = None,
    ) -> str:
        bundle = self._fetch_oauth_token_bundle(
            email=email,
            client_id=client_id,
            refresh_token=refresh_token,
            preferred_backend=preferred_backend,
        )
        return str(bundle.get("access_token") or "").strip()

    def _get_oauth_access_token(
        self,
        account: MailboxAccount,
        *,
        preferred_backend: str | None = None,
    ) -> str:
        extra = account.extra or {}
        client_id = str(extra.get("client_id") or "").strip()
        refresh_token = str(extra.get("refresh_token") or "").strip()
        email_addr = str(account.email or "").strip()
        if not client_id or not refresh_token:
            raise RuntimeError("微软邮箱 OAuth 凭据缺失，无法获取 access token")

        cache = extra.setdefault("_oauth_token_cache", {})
        cache_key = self._normalize_backend_name(preferred_backend or self._backend_name)
        cached = cache.get(cache_key) if isinstance(cache, dict) else None
        now = time.time()
        if isinstance(cached, dict):
            access_token = str(cached.get("access_token") or "").strip()
            expires_at = float(cached.get("expires_at") or 0)
            if access_token and expires_at > now + 60:
                return access_token

        bundle = self._fetch_oauth_token_bundle(
            email=email_addr,
            client_id=client_id,
            refresh_token=refresh_token,
            preferred_backend=cache_key,
        )
        access_token = str(bundle.get("access_token") or "").strip()
        if cache_key == "graph":
            extra["_oauth_backend_capability"] = (
                "graph" if bundle.get("scope_label") == "graph_default" else "imap"
            )
        if not access_token:
            reason = bundle.get("reason", "")
            suffix = f" [{reason}]" if reason else ""
            raise RuntimeError(f"微软邮箱 OAuth access token 获取失败{suffix}")

        expires_in = bundle.get("expires_in")
        try:
            expires_in_value = max(int(expires_in or 0), 0)
        except (TypeError, ValueError):
            expires_in_value = 0
        if isinstance(cache, dict):
            cache[cache_key] = {
                "access_token": access_token,
                "expires_at": now + expires_in_value if expires_in_value else now + 300,
                "scope_label": bundle.get("scope_label", ""),
            }
        return access_token

    def _imap_auth_oauth(self, imap_conn, *, email: str, access_token: str) -> None:
        auth_string = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
        imap_conn.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))

    def _open_imap(self, account: MailboxAccount):
        import imaplib

        email_addr = str(account.email or "").strip()
        extra = account.extra or {}
        password = str(extra.get("password") or "").strip()
        client_id = str(extra.get("client_id") or "").strip()
        refresh_token = str(extra.get("refresh_token") or "").strip()

        access_token = ""
        if client_id and refresh_token:
            access_token = self._get_oauth_access_token(
                account,
                preferred_backend="imap",
            )

        last_error = None
        for host in self._imap_servers:
            if not host:
                continue
            if access_token:
                try:
                    imap_conn = imaplib.IMAP4_SSL(host, self._imap_port, timeout=30)
                    self._imap_auth_oauth(
                        imap_conn, email=email_addr, access_token=access_token
                    )
                    return imap_conn
                except Exception as exc:
                    last_error = exc
                    try:
                        imap_conn.logout()
                    except Exception:
                        pass
            if password:
                try:
                    imap_conn = imaplib.IMAP4_SSL(host, self._imap_port, timeout=30)
                    imap_conn.login(email_addr, password)
                    return imap_conn
                except Exception as exc:
                    last_error = exc
                    try:
                        imap_conn.logout()
                    except Exception:
                        pass

        raise RuntimeError(f"微软邮箱 IMAP 登录失败: {last_error}")

    def _resolve_backend(self, account: MailboxAccount) -> OutlookMailboxBackend:
        extra = account.extra if isinstance(account.extra, dict) else {}
        account.extra = extra
        if self._is_mailapi_account(account):
            return self._backends["mailapi_url"]
        override = self._normalize_backend_name(
            extra.get("outlook_backend") or self._backend_name
        )
        if override == "graph":
            has_oauth = bool(
                str(extra.get("client_id") or "").strip()
                and str(extra.get("refresh_token") or "").strip()
            )
            if not has_oauth:
                self._log(
                    "[微软邮箱] Graph 后端需要 OAuth 凭据，当前账号缺少 client_id/refresh_token，自动切换 IMAP"
                )
                override = "imap"
        return self._backends.get(override) or self._backends["graph"]

    def _graph_headers(self, *, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Prefer": 'outlook.body-content-type="text"',
        }

    def _graph_request_json(
        self,
        *,
        method: str,
        path: str,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import requests

        url = f"{self._graph_api_base.rstrip('/')}/{path.lstrip('/')}"
        resp = requests.request(
            method,
            url,
            headers=self._graph_headers(access_token=access_token),
            params=params or None,
            timeout=20,
            proxies=self._proxy,
        )
        if resp.status_code >= 400:
            preview = (resp.text or "")[:300]
            raise RuntimeError(
                f"Outlook Graph 请求失败: HTTP {resp.status_code} {preview}"
            )
        return resp.json() if resp.content else {}

    def _graph_list_messages(
        self,
        *,
        access_token: str,
        folder: str,
    ) -> list[dict[str, Any]]:
        data = self._graph_request_json(
            method="GET",
            path=f"/me/mailFolders/{folder}/messages",
            access_token=access_token,
            params={
                "$top": "25",
                "$orderby": "receivedDateTime DESC",
                "$select": "id,subject,bodyPreview,body,receivedDateTime,from,internetMessageId",
            },
        )
        value = data.get("value") or []
        return value if isinstance(value, list) else []

    def _graph_get_message(
        self,
        *,
        access_token: str,
        message_id: str,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        return self._graph_request_json(
            method="GET",
            path=f"/me/messages/{quote(str(message_id or '').strip(), safe='')}",
            access_token=access_token,
            params={
                "$select": "id,subject,bodyPreview,body,uniqueBody,receivedDateTime,from,internetMessageId",
            },
        )

    def _graph_message_text(self, message: dict[str, Any]) -> str:
        subject = str((message or {}).get("subject") or "").strip()
        preview = str((message or {}).get("bodyPreview") or "").strip()

        body = (message or {}).get("body") or {}
        body_content = (
            str(body.get("content") or "").strip() if isinstance(body, dict) else ""
        )
        unique_body = (message or {}).get("uniqueBody") or {}
        unique_body_content = (
            str(unique_body.get("content") or "").strip()
            if isinstance(unique_body, dict)
            else ""
        )
        combined = " ".join(
            part for part in [subject, preview, body_content, unique_body_content] if part
        )
        return self._decode_raw_content(combined)

    def _decode_header_value(self, value: str) -> str:
        from email.header import decode_header

        if not value:
            return ""
        parts = decode_header(value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                try:
                    decoded.append(part.decode(charset or "utf-8", errors="ignore"))
                except Exception:
                    decoded.append(part.decode("utf-8", errors="ignore"))
            else:
                decoded.append(str(part))
        return "".join(decoded)

    def _extract_message_text(self, message) -> str:
        subject = self._decode_header_value(message.get("Subject", ""))
        body_chunks = []
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                content_type = part.get_content_type()
                if content_type not in ("text/plain", "text/html"):
                    continue
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    body_chunks.append(payload.decode(charset, errors="ignore"))
                except Exception:
                    body_chunks.append(payload.decode("utf-8", errors="ignore"))
        else:
            payload = message.get_payload(decode=True)
            if payload is None:
                payload = message.get_payload()
            if isinstance(payload, bytes):
                try:
                    body_chunks.append(payload.decode("utf-8", errors="ignore"))
                except Exception:
                    body_chunks.append(payload.decode("latin1", errors="ignore"))
            elif payload:
                body_chunks.append(str(payload))

        combined = (subject + " " + " ".join(body_chunks)).strip()
        return self._decode_raw_content(combined)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            backend = self._resolve_backend(account)
            self._log(f"[微软邮箱] 当前收信后端: {backend.backend_name}")
            return backend.get_current_ids(account)
        except Exception as exc:
            self._log(f"[微软邮箱] 获取当前邮件 ID 失败: {exc}")
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        backend = self._resolve_backend(account)
        self._log(f"[微软邮箱] OTP 收信后端: {backend.backend_name}")
        return backend.wait_for_code(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
            code_pattern=code_pattern,
            **kwargs,
        )

