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

#: 分层超时：建连与读分开。
#:
#: 用一个 30s 的单值超时会把两种故障混为一谈 ——「DNS/路由不通」本来 1 秒
#: 就该失败，却要陪跑到 30s；而「服务活着但慢」需要的是耐心。分开之后，
#: 出口挂掉时 worker 5 秒内就能拿到可重试失败、把别名放回池子。
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5

#: 读超时**必须**长于上游自己的单请求预算，否则「慢」会被记成「挂」。
#:
#: 上游 icloud-hme 单次收件箱请求的预算是：信封阶段 `limit × 0.3s`，下限
#: 15s、上限 45s（`imapEnvelopeBudget`），正文阶段整体 30s。原先这里是 20s
#: —— 客户端**必然先于上游放弃**，把一次「上游还在正常读 IMAP」的请求记成
#: 传输失败（2026-11-14 实测：51 次 Read timed out 就是这么来的），再以此
#: 打开熔断，把 4 个主号一起封掉 9 分钟。
#:
#: 取 45s（上限）+ 余量：比上游最长预算还长一点，让上游先给出它自己的结论。
DEFAULT_READ_TIMEOUT_SECONDS = 50

#: 连接池大小。收信是 5 并发（注册任务的 concurrency）同时打同一个服务，
#: 每个 worker 每次读信都新建 TCP+TLS 连接是纯浪费，也更容易撞上游限流。
CONNECTION_POOL_SIZE = 8

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
        timeout: int | None = None,
        use_gate: bool = True,
        gate=None,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self._api_key = str(api_key or "").strip()
        self._proxy = str(proxy or "").strip() or None
        # 不传 timeout = 用「一个请求最多能花多久」的默认值（= 读预算），
        # 而不是把读预算再压回一个更小的数：那正是把「慢」判成「挂」的源头。
        self._timeout = int(timeout) if timeout else DEFAULT_READ_TIMEOUT_SECONDS
        # `use_gate=False` 给测试与探活用：那些场景要的是「这一次请求的真实
        # 结果」，不该被进程内其它 worker 的失败记账影响。
        self._use_gate = bool(use_gate)
        self._gate_override = gate
        self._session = requests.Session()
        # 连接池：同一服务反复读信时复用连接，省掉每轮的 TCP+TLS 握手。
        from requests.adapters import HTTPAdapter

        adapter = HTTPAdapter(
            pool_connections=CONNECTION_POOL_SIZE,
            pool_maxsize=CONNECTION_POOL_SIZE,
        )
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def timeout_pair(self) -> tuple[int, int]:
        """返回 `(connect, read)` 二元组。

        不传 `timeout` 时用两个各自的默认值：建连 5s、读 50s（长于上游 45s
        预算）。显式传一个小的 `timeout` 时（测试、探活）两半都收敛到它 ——
        没人希望传了 `timeout=3` 之后还要等 50 秒的读超时。
        """
        connect = min(DEFAULT_CONNECT_TIMEOUT_SECONDS, self._timeout)
        read = min(DEFAULT_READ_TIMEOUT_SECONDS, self._timeout)
        return connect, read

    def _gate(self):
        """取熔断器：显式注入的优先，否则用进程内单例。"""
        if self._gate_override is not None:
            return self._gate_override
        from services.icloud.upstream_gate import global_gate

        return global_gate()

    # ------------------------------------------------------------ 请求

    def _request(
        self, method: str, path: str, *, account: str = "", **kwargs: Any
    ) -> Any:
        """发一次请求。

        `account` 是这次请求**归属的主号**（收件/别名/创建都按主号发）。
        熔断因此能分两级：这个主号的读不动只挡这个主号，多个主号一起坏或
        连不上才当成整台服务不可用（见 `UpstreamGate` 的 docstring）。
        """
        if not self.base_url:
            raise ICloudError("invalid_config", "未配置 iCloud 远程服务地址")
        if not self._api_key:
            raise ICloudError("invalid_config", "未配置 iCloud 远程服务 API 秘钥")

        # 熔断检查放在**发请求之前**：服务已经判定不可用时，连建连都不该尝试
        # —— 那 5 秒的 connect 超时也是白等。抛的是可重试失败，地址不消耗。
        gate = self._gate() if self._use_gate else None
        if gate is not None:
            gate.check(self.base_url, account=account)

        url = f"{self.base_url}{path}"
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-API-Key"] = self._api_key
        try:
            response = self._session.request(
                method,
                url,
                timeout=self.timeout_pair(),
                headers=headers,
                proxies={"http": self._proxy, "https": self._proxy} if self._proxy else None,
                **kwargs,
            )
        except requests.RequestException as exc:
            # 只把「连不上/读超时」记成失败。HTTP 4xx/5xx 是业务语义，
            # 走到下面按错误码处理，不该把整个服务判成熔断。
            if gate is not None:
                if account:
                    # 有主号上下文 → 记主号级。连不上（DNS/拒绝/断连）说明是
                    # 整个服务的问题，两级都记；只是这个主号读得慢则只记主号。
                    gate.note_account_failure(self.base_url, account)
                    if not isinstance(exc, (requests.Timeout,)):
                        gate.note_failure(self.base_url)
                else:
                    gate.note_failure(self.base_url)
            raise ICloudError(
                "upstream_unavailable", f"iCloud 远程服务不可达：{exc}"
            ) from exc

        if gate is not None:
            gate.note_success(self.base_url)
            if account:
                gate.note_account_success(self.base_url, account)

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
        """探测远端账号登录态（低风险 validate）。

        只读秘钥降级：`POST /accounts/:id/check` 需要**写级**权限，而注册用的
        那把 sk- 通常只有只读档（2026-11-14 线上实测：`/api/accounts`、
        `/api/inbox`、`/api/aliases` 全部 200，只有 `/check` 回 403
        `PERMISSION_DENIED`）。检测是只读语义，不该因为「钥匙不能写」就恒失败
        —— 前端只会显示一句「API 秘钥权限不足」，看着像上游挂了。

        降级探测 = `GET /api/accounts` 里能读到这个账号且 `status` 不是错误态。
        它比 `/check` 弱（不刷新远端 `last_validated`），所以**只在权限不足时**用；
        其它错误（上游 5xx、账号不存在）照常抛，不能把「读不出来」说成正常。
        """
        target = str(account_id or "").strip()
        try:
            data = self._authed_request(
                "POST", f"/api/accounts/{target}/check", account=target
            )
        except ICloudError as exc:
            if exc.code != "permission_denied":
                raise
            return self._read_only_probe(target)
        return data if isinstance(data, dict) else {}

    def _read_only_probe(self, account_id: str) -> dict[str, Any]:
        """只读探活：账号在列表里就算可用（`status` 原样回传，交给上层判定）。"""
        summary = self.account_summary(account_id)
        status = str(summary.get("status") or "").strip()
        return {
            "id": str(summary.get("id") or account_id),
            "status": status or "active",
            "read_only": True,
        }

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
            "POST",
            "/api/create",
            json={"account_id": target, "label": chosen},
            account=target,
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
        data = self._authed_request(
            "GET", "/api/aliases", params={"account_id": target}, account=target
        )
        aliases = data.get("aliases") if isinstance(data, dict) else None
        return aliases if isinstance(aliases, list) else []

    def delete_alias(self, account_id: str, anonymous_id: str) -> None:
        target = str(account_id or "").strip()
        self._authed_request(
            "DELETE",
            f"/api/aliases/{anonymous_id}",
            json={"account_id": target},
            account=target,
        )

    def set_alias_active(self, account_id: str, anonymous_id: str, active: bool) -> None:
        target = str(account_id or "").strip()
        action = "reactivate" if active else "deactivate"
        self._authed_request(
            "POST",
            f"/api/aliases/{anonymous_id}/{action}",
            json={"account_id": target},
            account=target,
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
        data = self._authed_request("GET", "/api/inbox", params=params, account=target)
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
            account=target,
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
            account=target,
        )
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ 探活

    def health(self) -> dict[str, Any]:
        """探活：能被 API 秘钥列出账号就算可用。"""
        accounts = self.list_accounts()
        return {"ok": True, "accounts": len(accounts)}


__all__ = [
    "RemoteHMEClient",
    "NON_RETRYABLE_CODES",
    "MAX_LABEL_LENGTH",
]
