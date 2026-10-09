"""icloud-hme 远程服务客户端（远程仓库链路）。

用途
----
把独立部署的 icloud-hme 服务（`reference/panel/icloud-hme`）当作 iCloud 隐私
邮箱的**远程仓库**：主号凭据与 SRP 登录都留在那个服务里，本机只存
「服务地址 + 管理员密码 + 远端账号 id」，通过它的 HTTP API 创建别名、列别名、
读邮件、停用/删除。

对接的接口（见 reference/panel/icloud-hme/API.md）
--------------------------------------------------
- ``POST /api/auth/login``       管理员密码换会话 Cookie + ``csrf_token``
- ``GET  /api/auth/session``     校验会话是否有效（服务重启过）
- ``GET  /api/accounts``         列出远端账号（接入时选账号用）
- ``GET  /api/alias-labels``     别名标签名称库
- ``POST /api/create``           创建 HME 别名（label 自由输入，≤200 字符）
- ``GET  /api/aliases``          列出别名
- ``POST /api/aliases/:id/deactivate|reactivate``  停用/激活
- ``DELETE /api/aliases/:id``    删除
- ``GET  /api/inbox``            读邮件（IMAP 优先，Web API 回退）
- ``GET  /api/inbox/:id``        读单封邮件全文
- ``POST /api/accounts/:id/check``  探测账号登录态

关键约束
--------
- 非 GET 请求必须带 ``X-CSRF-Token``，否则 ``403 CSRF_INVALID``；
  token 每次登录都会变，所以必须在取完会话之后拼、重登后的重试也要用新 token。
- 服务端错误码放在响应体 ``code`` 字段，这里映射成本项目惯用的错误码，
  让上层（任务重试、渠道切换）能用同一套判断逻辑。
- ``/api/inbox`` 的 ``limit`` 上限是 50（服务端以代码为准）。
- 登录失败会限流（同 IP 15 分钟 5 次），所以会话在实例内缓存复用，
  不每个请求重新登录。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import requests

from .errors import ICloudError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
#: 会话有效期 12 小时；提前一点重建，避免请求正好卡在过期瞬间。
_SESSION_REFRESH_SECONDS = 11 * 3600

#: 服务端错误码 → 本项目惯用错误码。
_ERROR_CODE_MAP = {
    "AUTH_REQUIRED": "session_expired",
    "INVALID_CREDENTIALS": "invalid_config",
    "RATE_LIMITED": "provider_rate_limited",
    "CREATION_COOLDOWN": "provider_rate_limited",
    "CREATION_HOURLY_LIMIT": "provider_rate_limited",
    "CREATION_LIMIT_REACHED": "provider_rate_limited",
    "CSRF_INVALID": "session_expired",
    "VALIDATION_ERROR": "invalid_config",
    "ACCOUNT_NOT_FOUND": "invalid_config",
    "TASK_NOT_FOUND": "invalid_config",
    "UPSTREAM_UNAUTHORIZED": "session_expired",
    "UPSTREAM_FAILURE": "upstream_unavailable",
    "INTERNAL_ERROR": "upstream_unavailable",
}

#: 撞上这些错误说明额度/频率/配置受限，重试没有意义。
NON_RETRYABLE_CODES = frozenset(
    {"provider_rate_limited", "invalid_config", "session_expired"}
)

#: 手动创建标签的最大长度（rune）—— 与服务端 `maxManualLabelLen` 对齐。
MAX_LABEL_LENGTH = 200


class RemoteHMEClient:
    """带会话复用与自动重登的 icloud-hme 客户端。

    一个任务里通常要连发几十个请求（列表 + 逐封正文），每次重新登录既慢
    又会踩服务端的登录限流（同 IP 15 分钟 5 次失败），所以会话在实例内
    缓存；CSRF token 每次登录都会变，必须在取完会话之后再拼。
    """

    def __init__(
        self,
        base_url: str,
        admin_password: str = "",
        *,
        proxy: str = "",
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self._password = str(admin_password or "")
        self._proxy = str(proxy or "").strip() or None
        self._timeout = int(timeout or DEFAULT_TIMEOUT_SECONDS)
        self._session = requests.Session()
        self._csrf_token = ""
        self._logged_in_at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 会话

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.base_url:
            raise ICloudError("invalid_config", "未配置 iCloud 远程服务地址")
        url = f"{self.base_url}{path}"
        try:
            response = self._session.request(
                method,
                url,
                timeout=self._timeout,
                proxies={"http": self._proxy, "https": self._proxy} if self._proxy else None,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ICloudError(
                "upstream_unavailable", f"iCloud 远程服务不可达：{exc}"
            ) from exc

        payload: dict[str, Any] = {}
        try:
            body = response.json()
            if isinstance(body, dict):
                payload = body
        except ValueError:
            payload = {}

        if response.status_code >= 400 or payload.get("success") is False:
            raw_code = str(payload.get("code") or "").strip()
            code = _ERROR_CODE_MAP.get(raw_code, "upstream_error")
            message = str(payload.get("message") or "").strip()
            if not message:
                message = f"iCloud 远程服务返回 {response.status_code}"
            raise ICloudError(code, message)

        return payload.get("data")

    def _login(self) -> None:
        """管理员登录，拿会话 Cookie 与 CSRF token。"""
        if not self._password:
            raise ICloudError("invalid_config", "未配置 iCloud 远程服务管理员密码")
        data = self._request("POST", "/api/auth/login", json={"password": self._password})
        token = ""
        if isinstance(data, dict):
            token = str(data.get("csrf_token") or "").strip()
        if not token:
            raise ICloudError("upstream_unavailable", "登录成功但未返回 csrf_token")
        self._csrf_token = token
        self._logged_in_at = time.monotonic()

    def _ensure_session(self, *, force: bool = False) -> None:
        with self._lock:
            fresh = self._csrf_token and (
                time.monotonic() - self._logged_in_at < _SESSION_REFRESH_SECONDS
            )
            if fresh and not force:
                return
            self._login()

    def _authed_request(
        self, method: str, path: str, *, _retried: bool = False, **kwargs: Any
    ) -> Any:
        """带会话的请求；会话过期时自动重登一次。"""
        self._ensure_session()
        extra_headers = dict(kwargs.pop("headers", {}) or {})
        # CSRF token 每次登录都会变，所以必须在取完会话之后再拼，
        # 重登后的重试也要用新 token（否则会一直 403）。
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            extra_headers["X-CSRF-Token"] = self._csrf_token
        try:
            return self._request(method, path, headers=extra_headers, **kwargs)
        except ICloudError as exc:
            # 服务重启会让内存会话失效，但 Cookie 还在浏览器里 —— 重登一次即可。
            if exc.code == "session_expired" and not _retried:
                self._ensure_session(force=True)
                return self._authed_request(method, path, _retried=True, **kwargs)
            raise

    # ------------------------------------------------------------ 账号

    def list_accounts(self) -> list[dict[str, Any]]:
        data = self._authed_request("GET", "/api/accounts")
        return data if isinstance(data, list) else []

    def account_summary(self, account_id: str) -> dict[str, Any]:
        target = str(account_id or "").strip()
        for item in self.list_accounts():
            if str(item.get("id") or "") == target:
                return item
        raise ICloudError("invalid_config", f"远程服务里不存在账号：{target}")

    def check_account(self, account_id: str) -> dict[str, Any]:
        """探测远端账号登录态（低风险 validate）。"""
        target = str(account_id or "").strip()
        data = self._authed_request("POST", f"/api/accounts/{target}/check")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ 别名

    def list_labels(self) -> list[str]:
        data = self._authed_request("GET", "/api/alias-labels")
        return [str(item) for item in data] if isinstance(data, list) else []

    def create_alias(self, account_id: str, label: str = "") -> dict[str, Any]:
        """创建 HME 别名。

        label 由服务端自由校验（≤200 字符）；调用方没给时从名称库挑第一个，
        保证行为可复现。
        """
        target = str(account_id or "").strip()
        chosen = str(label or "").strip()
        if not chosen:
            labels = self.list_labels()
            if labels:
                chosen = labels[0]
            else:
                chosen = "隐私邮箱"
        chosen = chosen[:MAX_LABEL_LENGTH]

        data = self._authed_request(
            "POST", "/api/create", json={"account_id": target, "label": chosen}
        )
        payload = data if isinstance(data, dict) else {}
        return {
            "email": str(payload.get("email") or "").strip(),
            "label": str(payload.get("label") or chosen),
            "created_at": str(payload.get("created_at") or ""),
            "account_id": str(payload.get("account_id") or target),
        }

    def list_aliases(self, account_id: str = "") -> list[dict[str, Any]]:
        target = str(account_id or "").strip()
        data = self._authed_request("GET", "/api/aliases", params={"account_id": target})
        aliases = data.get("aliases") if isinstance(data, dict) else None
        return aliases if isinstance(aliases, list) else []

    def delete_alias(self, account_id: str, anonymous_id: str) -> None:
        target = str(account_id or "").strip()
        self._authed_request(
            "DELETE",
            f"/api/aliases/{anonymous_id}",
            json={"account_id": target},
        )

    def set_alias_active(self, account_id: str, anonymous_id: str, active: bool) -> None:
        target = str(account_id or "").strip()
        action = "reactivate" if active else "deactivate"
        self._authed_request(
            "POST",
            f"/api/aliases/{anonymous_id}/{action}",
            json={"account_id": target},
        )

    # ------------------------------------------------------------ 邮件

    def fetch_inbox(
        self,
        account_id: str = "",
        *,
        alias: str = "",
        limit: int = 20,
        days: int = 7,
    ) -> dict[str, Any]:
        """读收件箱。

        返回原始结构 `{account_id, alias, count, method, warning, messages}`，
        由调用方决定怎么映射成本项目的邮件模型。
        """
        target = str(account_id or "").strip()
        params: dict[str, Any] = {
            "account_id": target,
            "limit": max(1, min(int(limit or 20), 50)),
            "days": max(0, int(days or 7)),
        }
        if alias:
            params["alias"] = str(alias).strip()
        data = self._authed_request("GET", "/api/inbox", params=params)
        if not isinstance(data, dict):
            return {"account_id": target, "messages": []}
        return data

    def fetch_message(self, account_id: str, message_id: str) -> dict[str, Any]:
        """取单封邮件全文（列表里的 preview 最多 2000 字）。"""
        target = str(account_id or "").strip()
        data = self._authed_request(
            "GET",
            f"/api/inbox/{message_id}",
            params={"account_id": target},
        )
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ 探活

    def health(self) -> dict[str, Any]:
        """探活：能登录并列出账号就算可用。"""
        accounts = self.list_accounts()
        return {"ok": True, "accounts": len(accounts)}


__all__ = [
    "RemoteHMEClient",
    "NON_RETRYABLE_CODES",
    "MAX_LABEL_LENGTH",
]
