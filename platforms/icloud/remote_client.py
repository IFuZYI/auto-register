"""icloud-hme 远程服务客户端（远程仓库链路）。

用途
----
把独立部署的 icloud-hme 服务（`reference/panel/icloud-hme`）当作 iCloud 隐私
邮箱的**远程仓库**：主号凭据与 SRP 登录都留在那个服务里，本机只存
「服务地址 + API 秘钥 + 远端账号 id」，通过它的 HTTP API 创建别名、列别名、
读邮件、停用/删除。

鉴权（2026-11-02 起）
-------------------
所有请求带 `X-API-Key` 请求头，不使用管理员密码登录，也没有 Cookie / CSRF
会话。秘钥由管理台「秘钥」页创建，按权限（accounts.list / aliases.list /
inbox.* 等）和账号范围授权；写操作需要对应的 `*.write` 权限。
"""


from __future__ import annotations

import logging
from typing import Any

import requests

from .errors import ICloudError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30

#: 服务端错误码 → 本项目惯用错误码。
_ERROR_CODE_MAP = {
    "INVALID_API_KEY": "invalid_config",
    "PERMISSION_DENIED": "permission_denied",
    "ACCOUNT_NOT_ALLOWED": "account_not_allowed",
    "RATE_LIMITED": "provider_rate_limited",
    "CREATION_COOLDOWN": "provider_rate_limited",
    "CREATION_HOURLY_LIMIT": "provider_rate_limited",
    "CREATION_LIMIT_REACHED": "provider_rate_limited",
    "VALIDATION_ERROR": "invalid_config",
    "ACCOUNT_NOT_FOUND": "invalid_config",
    "ALIAS_NOT_FOUND": "invalid_config",
    "TASK_NOT_FOUND": "invalid_config",
    "UPSTREAM_FAILURE": "upstream_unavailable",
    "INTERNAL_ERROR": "upstream_unavailable",
}

#: 撞上这些错误说明额度/频率/配置受限，重试没有意义。
NON_RETRYABLE_CODES = frozenset(
    {"provider_rate_limited", "invalid_config", "permission_denied", "account_not_allowed"}
)

#: 手动创建标签的最大长度（rune）—— 与服务端 `maxManualLabelLen` 对齐。
MAX_LABEL_LENGTH = 200


class RemoteHMEClient:
    """icloud-hme 客户端（API 秘钥鉴权，无会话状态）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        proxy: str = "",
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self._api_key = str(api_key or "").strip()
        self._proxy = str(proxy or "").strip() or None
        self._timeout = int(timeout or DEFAULT_TIMEOUT_SECONDS)
        self._session = requests.Session()

    # ------------------------------------------------------------ 请求

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.base_url:
            raise ICloudError("invalid_config", "未配置 iCloud 远程服务地址")
        if not self._api_key:
            raise ICloudError("invalid_config", "未配置 iCloud 远程服务 API 秘钥")
        url = f"{self.base_url}{path}"
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-API-Key"] = self._api_key
        try:
            response = self._session.request(
                method,
                url,
                timeout=self._timeout,
                headers=headers,
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

    def _authed_request(self, method: str, path: str, **kwargs: Any) -> Any:
        """兼容旧调用点的别名：API 秘钥模式下就是带头的普通请求。"""
        return self._request(method, path, **kwargs)

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
            try:
                labels = self.list_labels()
            except ICloudError:
                # 新 API 秘钥可能没有 alias-labels 权限；标签只是可选装饰，
                # 不能因为读不到名称库就挡住创建。
                labels = []
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
        offset: int = 0,
        start: str = "",
        end: str = "",
        refresh: bool = False,
    ) -> dict[str, Any]:
        """读收件箱。

        返回原始结构 `{account_id, alias, count, total, offset, method,
        warning, messages}`，由调用方决定怎么映射成本项目的邮件模型。

        新接口（2026-11-02）支持 `offset` / `start` / `end` / `refresh`：
        列表页要翻页、按日期过滤，旧实现只传 limit/days 会永远停在第一页。
        """
        target = str(account_id or "").strip()
        params: dict[str, Any] = {
            "account_id": target,
            "limit": max(1, min(int(limit or 20), 50)),
            "days": max(0, min(int(days if days is not None else 7), 3650)),
            "offset": max(0, min(int(offset or 0), 10000)),
        }
        if alias:
            params["alias"] = str(alias).strip()
        if start:
            params["start"] = str(start).strip()
        if end:
            params["end"] = str(end).strip()
        if refresh:
            params["refresh"] = 1
        data = self._authed_request("GET", "/api/inbox", params=params)
        if not isinstance(data, dict):
            return {"account_id": target, "messages": [], "count": 0, "total": 0}
        return data

    def fetch_previews(self, account_id: str, message_ids: list[str]) -> dict[str, str]:
        """批量补邮件摘要（最多 20 个 ID），返回 `id -> 摘要`。"""
        target = str(account_id or "").strip()
        ids = [str(item) for item in (message_ids or []) if str(item or "").strip()][:20]
        if not ids:
            return {}
        data = self._authed_request(
            "POST",
            "/api/inbox/previews",
            params={"account_id": target},
            json={"ids": ids},
        )
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value or "") for key, value in data.items()}

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
