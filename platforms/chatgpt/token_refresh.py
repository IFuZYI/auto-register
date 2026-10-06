"""
Token 刷新模块
支持 Session Token 和 OAuth Refresh Token 两种刷新方式
"""

from __future__ import annotations

import logging
import json
import time
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta

from curl_cffi import requests as cffi_requests

from platforms.chatgpt.protocol.banned_signals import looks_like_banned
from platforms.chatgpt.protocol.response_summary import describe_error

# from ..config.settings import get_settings  # removed: external dep
# from ..database.session import get_db  # removed: external dep
# from ..database import crud  # removed: external dep
# from ..database.models import Account  # removed: external dep

logger = logging.getLogger(__name__)


@dataclass
class TokenRefreshResult:
    """Token 刷新结果"""
    success: bool
    access_token: str = ""
    refresh_token: str = ""
    expires_at: Optional[datetime] = None
    error_message: str = ""
    #: 新 AT 是否**真的**通过了 /backend-api/me 校验。
    #:
    #: 与 `success` 分开：`success` 只说「这次刷新调用本身拿到了 AT」，
    #: 而拿到的 AT 可能是服务端返回的一个**已失效**的令牌（实测：某些账号
    #: 刷新接口回 200 + AT，但那个 AT 打任何接口都是 401）。只看 `success`
    #: 会把它当成功写回库，下一个任务拿着废 AT 全线失败。
    verified: bool = False
    #: 新 AT 与旧值**不同**（真的换发了）。
    #:
    #: 实测（2026-10-06）：AT 未到期时，session 端点返回的 AT 与请求方
    #: 已有的**完全一致**（iat/exp 不变）—— 服务端不签发新令牌。这不是
    #: 错误（旧 AT 仍有效），但界面上不能把它说成「已换新」。`refreshed`
    #: 让调用方能如实区分「真的换发了」与「服务端认为无需换发」；
    #: **未换发不算刷新成功** —— 插件层据此继续走登录流程换发新 AT
    #: （用户修正 2026-10-06）。
    refreshed: bool = False
    #: 校验失败时的原因（区分「AT 无效」与「网络没打通」）。
    verify_message: str = ""
    #: 登录链认出「账号已封禁」（OpenAI 原话 "deleted or deactivated"）。
    banned: bool = False
    #: 最终拿到 AT 走的是哪条路：oauth / session / password_2fa / password_only
    strategy: str = ""
    #: 登录链顺带换到的其它凭证（只有走登录流程时才有）
    session_token: str = ""
    id_token: str = ""
    cookie_header: str = ""


class TokenRefreshManager:
    """
    Token 刷新管理器
    支持两种刷新方式：
    1. Session Token 刷新（优先）
    2. OAuth Refresh Token 刷新
    """

    # OpenAI OAuth 端点
    SESSION_URL = "https://chatgpt.com/api/auth/session"
    TOKEN_URL = "https://auth.openai.com/oauth/token"

    def __init__(self, proxy_url: Optional[str] = None):
        """
        初始化 Token 刷新管理器

        Args:
            proxy_url: 代理 URL
        """
        self.proxy_url = proxy_url
        from .constants import OAUTH_CLIENT_ID, OAUTH_REDIRECT_URI
        self._oauth_client_id = OAUTH_CLIENT_ID
        self._oauth_redirect_uri = OAUTH_REDIRECT_URI

    def _create_session(self) -> cffi_requests.Session:
        """创建 HTTP 会话"""
        session = cffi_requests.Session(impersonate="chrome120", proxy=self.proxy_url)
        return session

    def refresh_by_session_token(self, session_token: str) -> TokenRefreshResult:
        """
        使用 Session Token 刷新

        Args:
            session_token: 会话令牌

        Returns:
            TokenRefreshResult: 刷新结果
        """
        result = TokenRefreshResult(success=False)

        try:
            session = self._create_session()

            # 设置会话 Cookie
            session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
                path="/"
            )

            # 请求会话端点
            response = session.get(
                self.SESSION_URL,
                headers={
                    "accept": "application/json",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                },
                timeout=30
            )

            if response.status_code != 200:
                # 非 200 也要读响应体：号被停用时服务端会在 body 里写明
                # 「deleted or deactivated」（用户实测 2026-10-06），只报
                # 「HTTP 401」会把封号与普通过期混成同一种失败。
                body_text = str(getattr(response, "text", "") or "")
                if looks_like_banned(body_text):
                    result.banned = True
                    result.error_message = f"账号已封禁（session 端点拒绝: HTTP {response.status_code}）"
                else:
                    result.error_message = f"Session token 刷新失败: HTTP {response.status_code}"
                logger.warning(result.error_message)
                return result

            data = response.json()

            # 提取 access_token
            access_token = data.get("accessToken")
            if not access_token:
                result.error_message = "Session token 刷新失败: 未找到 accessToken"
                logger.warning(result.error_message)
                return result

            # 保存轮换后的 session token：OpenAI 对每次 /api/auth/session 响应
            # 都会下发新的 `sessionToken`（滑动窗口，实测 2026-10-06：连续两次
            # 请求返回的 ST 都不同）。此前只读 accessToken，轮换值从未保存 ——
            # 旧值滑出窗口后账号就登不上了。只写非空值。
            rotated = str(data.get("sessionToken") or data.get("session_token") or "").strip()
            if rotated:
                result.session_token = rotated
                try:
                    session.cookies.set(
                        "__Secure-next-auth.session-token",
                        rotated,
                        domain=".chatgpt.com",
                        path="/",
                    )
                except Exception:  # noqa: BLE001 - cookie jar 写失败不阻断刷新结果
                    pass

            # 提取过期时间
            expires_at = None
            expires_str = data.get("expires")
            if expires_str:
                try:
                    expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                except:
                    pass

            result.success = True
            result.access_token = access_token
            result.expires_at = expires_at

            logger.info(f"Session token 刷新成功，过期时间: {expires_at}")
            return result

        except Exception as e:
            result.error_message = f"Session token 刷新异常: {str(e)}"
            logger.error(result.error_message)
            return result

    def refresh_by_oauth_token(
        self,
        refresh_token: str,
        client_id: Optional[str] = None
    ) -> TokenRefreshResult:
        """
        使用 OAuth Refresh Token 刷新

        Args:
            refresh_token: OAuth 刷新令牌
            client_id: OAuth Client ID

        Returns:
            TokenRefreshResult: 刷新结果
        """
        result = TokenRefreshResult(success=False)

        try:
            session = self._create_session()

            # 使用配置的 client_id 或默认值
            client_id = client_id or self._oauth_client_id

            # 构建请求体
            token_data = {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "redirect_uri": self._oauth_redirect_uri
            }

            response = session.post(
                self.TOKEN_URL,
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    "accept": "application/json"
                },
                data=token_data,
                timeout=30
            )

            if response.status_code != 200:
                # 同 session 端点：错误体里可能带着「号没了」的措辞。
                body_text = str(getattr(response, "text", "") or "")
                if looks_like_banned(body_text):
                    result.banned = True
                    result.error_message = f"账号已封禁（OAuth 端点拒绝: HTTP {response.status_code}）"
                else:
                    result.error_message = f"OAuth token 刷新失败: HTTP {response.status_code}"
                logger.warning(f"{result.error_message}, 服务端说: {describe_error(response.text)}")
                return result

            data = response.json()

            # 提取令牌
            access_token = data.get("access_token")
            new_refresh_token = data.get("refresh_token", refresh_token)
            expires_in = data.get("expires_in", 3600)

            if not access_token:
                result.error_message = "OAuth token 刷新失败: 未找到 access_token"
                logger.warning(result.error_message)
                return result

            # 计算过期时间
            expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

            result.success = True
            result.access_token = access_token
            result.refresh_token = new_refresh_token
            result.expires_at = expires_at

            logger.info(f"OAuth token 刷新成功，过期时间: {expires_at}")
            return result

        except Exception as e:
            result.error_message = f"OAuth token 刷新异常: {str(e)}"
            logger.error(result.error_message)
            return result

    def refresh_account(self, account: Account) -> TokenRefreshResult:
        """
        刷新账号的 Token

        优先级：
        1. Session Token 刷新
        2. OAuth Refresh Token 刷新

        两条都拿到 AT 之后都会**再校验一次**（`verify_access_token`）：
        刷新接口回 200 不代表这个 AT 真能用 —— 实测有账号拿回一个打接口就
        401 的令牌。校验不通过的结果会把 `verified` 留成 False，调用方据此
        决定「别写库」或「换下一条路」。

        `refreshed` 区分「真的换发了新 AT」与「服务端返回原值」：AT 未到期时
        session 端点会原样返回旧 AT（实测 2026-10-06），此时 `success=True`
        但 `refreshed=False` —— 调用方（插件层）据此继续走登录流程换发新 AT
        （用户修正：未换发不算刷新成功），而不是把它当「已换新」写库。

        Args:
            account: 账号对象

        Returns:
            TokenRefreshResult: 刷新结果
        """
        previous_at = str(getattr(account, "access_token", "") or "").strip()

        # 优先尝试 Session Token
        session_result: Optional[TokenRefreshResult] = None
        if account.session_token:
            logger.info(f"尝试使用 Session Token 刷新账号 {account.email}")
            result = self.refresh_by_session_token(account.session_token)
            if result.success:
                result.strategy = "session"
                result.refreshed = bool(result.access_token) and result.access_token != previous_at
                self._verify(result)
                if result.verified:
                    return result
                session_result = result
                logger.warning(
                    "Session Token 刷出的 AT 未通过校验（%s），尝试 OAuth 刷新",
                    result.verify_message or "未知原因",
                )
            elif result.banned:
                # 封号是终局结论：session 端点已明确「号没了」，再试 OAuth
                # 与登录链只会被同样拒绝 —— 直接返回，不浪费一次网络往返。
                logger.warning("Session Token 刷新被拒：账号已封禁，不再尝试其它刷新方式")
                return result

        # 尝试 OAuth Refresh Token
        if account.refresh_token:
            logger.info(f"尝试使用 OAuth Refresh Token 刷新账号 {account.email}")
            oauth_result = self.refresh_by_oauth_token(
                refresh_token=account.refresh_token,
                client_id=account.client_id
            )
            if oauth_result.success:
                oauth_result.strategy = "oauth"
                # OAuth 端点签发的 AT 带新 iat —— 与旧值不同才算真换发
                oauth_result.refreshed = bool(oauth_result.access_token) and oauth_result.access_token != previous_at
                self._verify(oauth_result)
                return oauth_result
            # OAuth 这条路**失败**时不能直接把它还回去：session 那条路可能刚拿回
            # 一个没通过校验的 AT（success=True + verified=False），调用方正是靠
            # 这个信号去走登录兜底。丢掉它的话，两条刷新都失败 → 报「刷新失败」→
            # 登录兜底被跳过，而那是我们最后一条拿 AT 的路。
            logger.warning(
                "OAuth 刷新失败（%s），回看 session 结果",
                oauth_result.error_message or "未知原因",
            )
            if session_result is not None:
                return session_result
            return oauth_result

        # 没得可试了。session 那条路**拿到过**一个没通过校验的 AT 就把它还回去：
        # success=True + verified=False 正是「刷新调用成了但这个 AT 不能用」的语义，
        # 调用方据此去走登录兜底。这里若改报「没有可用的刷新方式」，就把这个线索
        # 丢了 —— 明明刚拿回一个 AT，却说没有刷新方式，日志里对不上。
        if session_result is not None:
            return session_result

        # 无可用刷新方式
        return TokenRefreshResult(
            success=False,
            error_message="账号没有可用的刷新方式（缺少 session_token 和 refresh_token）"
        )

    def _verify(self, result: TokenRefreshResult) -> None:
        """给刷新结果补上「新 AT 真的能用吗」这一项（原地修改）。"""
        ok, reason = self.verify_access_token(result.access_token)
        result.verified = ok
        result.verify_message = reason
        if not ok:
            logger.warning("刷新拿到的 AT 未通过校验: %s", reason or "未知原因")

    def verify_access_token(self, access_token: str) -> Tuple[bool, str]:
        """新刷出来的 AT 到底能不能用 —— 打一次 `/backend-api/me`。

        与 `validate_token` 的区别：这里**把网络故障与令牌失效分开报**。
        `validate_token` 把所有异常都归成「无效」，用它做「刷新成功与否」的
        判据时，一次网络抖动就会把刚刷好的 AT 判死、账号被误标失效。

        返回 `(是否可用, 不可用的原因)`。
        """
        token = str(access_token or "").strip()
        if not token:
            return False, "刷新结果里没有 access_token"
        try:
            session = self._create_session()
            response = session.get(
                "https://chatgpt.com/backend-api/me",
                headers={
                    "authorization": f"Bearer {token}",
                    "accept": "application/json",
                },
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 - 网络故障不等于令牌失效
            return False, f"校验请求失败（网络/代理问题，不代表 AT 无效）: {exc}"

        if response.status_code == 200:
            return True, ""
        if response.status_code in (401, 403):
            return False, f"新 AT 被服务端拒绝（HTTP {response.status_code}）"
        return False, f"校验返回意外状态码 HTTP {response.status_code}"

    def validate_token(self, access_token: str) -> Tuple[bool, Optional[str]]:
        """
        验证 Access Token 是否有效

        Args:
            access_token: 访问令牌

        Returns:
            Tuple[bool, Optional[str]]: (是否有效, 错误信息)
        """
        try:
            session = self._create_session()

            # 调用 OpenAI API 验证 token
            response = session.get(
                "https://chatgpt.com/backend-api/me",
                headers={
                    "authorization": f"Bearer {access_token}",
                    "accept": "application/json"
                },
                timeout=30
            )

            if response.status_code == 200:
                return True, None
            elif response.status_code == 401:
                return False, "Token 无效或已过期"
            elif response.status_code == 403:
                return False, "账号可能被封禁"
            else:
                return False, f"验证失败: HTTP {response.status_code}"

        except Exception as e:
            return False, f"验证异常: {str(e)}"
