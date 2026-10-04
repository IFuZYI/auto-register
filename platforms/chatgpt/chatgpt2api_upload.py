"""chatgpt2api 上传功能。

同名项目有两个不同作者的实现（yukkcat 版与 basketikun 版），上传器同一份代码
两边都能用；本项目的**读取对比**按 yukkcat 版对接（列表不带 token、凭证走
export），上传协议两边一致：

* `POST <host>/api/accounts`，body `{"accounts": [...]}`，
  认证头 `Authorization: Bearer <key>`。
* 每个对象只有 `access_token` 必需；**普通网页号不要带 `type: "codex"`**
  —— 带了会被对端当成 codex 源，走另一条解析路径。
* 重复 access_token 对端按 `skipped` 处理，所以重传是幂等的。
* 响应回 `{added, skipped, refreshed, errors}`。

与 CPA / Sub2API 的区别：这里传的是**普通网页号**（只要 access_token），
不传 refresh_token / id_token，也不生成 codex 凭证。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Tuple

from curl_cffi import requests as cffi_requests

from platforms.chatgpt.cpa_upload import generate_token_json

logger = logging.getLogger(__name__)


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "").strip()
    except Exception:
        return ""


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """解 JWT 的 payload 段（不验签，只读声明）。失败返回空 dict。"""
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_auth(payload: dict[str, Any]) -> dict[str, Any]:
    section = payload.get("https://api.openai.com/auth")
    return section if isinstance(section, dict) else {}


def _extract_profile(payload: dict[str, Any]) -> dict[str, Any]:
    section = payload.get("https://api.openai.com/profile")
    return section if isinstance(section, dict) else {}


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def build_chatgpt2api_account(account, proxy: str = "") -> dict[str, Any]:
    """把本地账号转成 chatgpt2api 的导入对象。

    只放对端认的字段：`access_token`（必需）+ 可选的 `email` / `account_id` /
    `type`（套餐类型，对端自己再归一）。**不放** `refresh_token` / `id_token`
    —— 普通网页号用不上，且多传会让对端把它当另一种源。

    `proxy` 非空时作为 `proxy` 字段带上（对端 `_add_account_payloads` 认它，
    custom 模式直接是代理 URL）。由「上传代理」开关控制。
    """
    token_data = generate_token_json(account)
    access_token = str(
        token_data.get("access_token")
        or getattr(account, "access_token", "")
        or getattr(account, "token", "")
        or ""
    ).strip()
    if not access_token:
        raise ValueError("账号没有 access_token，无法导入 chatgpt2api")

    access_payload = _decode_jwt_payload(access_token)
    access_auth = _extract_auth(access_payload)
    profile = _extract_profile(access_payload)

    email = _first_non_empty(
        getattr(account, "email", ""),
        token_data.get("email"),
        profile.get("email"),
        access_payload.get("email"),
    )
    account_id = _first_non_empty(
        access_auth.get("chatgpt_account_id"),
        token_data.get("account_id"),
    )
    plan_type = _first_non_empty(
        access_auth.get("chatgpt_plan_type"),
        getattr(account, "plan_type", ""),
    )

    item: dict[str, Any] = {"access_token": access_token, "source_type": "web"}
    if email:
        item["email"] = email
    if account_id:
        item["account_id"] = account_id
    proxy_url = str(proxy or "").strip()
    if proxy_url:
        item["proxy"] = proxy_url
    if plan_type:
        # 对端会把这个值当归一化前的套餐类型（free / Plus / Pro…）。
        #
        # ⚠️ 绝不能让 `codex` 出现在这里：对端看到 `type == "codex"` 会把这个
        # 号当成 codex 源走另一条解析路径（参考实现的注释专门警告过）。
        # 套餐类型理论上不会等于 codex，但这是**跨服务契约**——上游字段漂移
        # 时静默传错比报错难查得多，所以这里显式挡一道。
        if plan_type.strip().lower() != "codex":
            item["type"] = plan_type
        else:
            logger.warning(
                "chatgpt2api：账号 %s 的套餐类型解析为 codex，已忽略该字段"
                "（否则对端会把它当 codex 源）",
                email or "(未知邮箱)",
            )
    return item


def upload_to_chatgpt2api(
    account,
    api_url: str | None = None,
    api_key: str | None = None,
    proxy: str = "",
) -> Tuple[bool, str]:
    """上传单个账号到 chatgpt2api。

    `proxy` 非空时随导入对象带上（对端认 `proxy` 字段）。由「上传代理」
    开关控制（见 `services.chatgpt_sync.upload_proxy_for`）。

    返回 `(ok, message)`。失败不抛异常 —— 调用方（注册链路 / 批量动作）
    要能把单号失败当成一条结果记下来，而不是中断整批。
    """
    api_url = str(api_url or _get_config_value("chatgpt2api_api_url")).strip()
    api_key = str(api_key or _get_config_value("chatgpt2api_api_key")).strip()

    if not api_url:
        return False, "chatgpt2api 地址未配置"
    if not api_key:
        return False, "chatgpt2api 管理密钥未配置"

    try:
        payload = {"accounts": [build_chatgpt2api_account(account, proxy=proxy)]}
    except ValueError as exc:
        # 预期的业务错误（如「账号没有 access_token」）：消息本身就是给用户看的
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        # 非预期异常（generate_token_json 抛错、返回非 dict 导致 AttributeError 等）。
        # 本函数的契约是「失败不抛异常 —— 调用方要能把单号失败当成一条结果记下来」，
        # 所以这里必须兜底，否则整个批量上传会被一个坏号中断。
        logger.error(
            "chatgpt2api 构建导入对象失败: %s: %s", type(exc).__name__, exc
        )
        return False, f"构建导入对象失败: {type(exc).__name__}: {str(exc)[:160]}"

    url = f"{api_url.rstrip('/')}/api/accounts"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    # 出口代理偶发抖动（TLS / 连接重置），**只对连接类异常**退避重试 ——
    # 与 reference 实现（`export_chatgpt2api.py:import_accounts`）同一口径：
    # 那里只捕获 `(ConnectionError, Timeout)`，其余异常立即失败。
    # 确定性错误（URL 非法、impersonate 不支持）重试 4 次只会白等 12 秒。
    from curl_cffi.requests.exceptions import ConnectionError as _CffiConnErr
    from curl_cffi.requests.exceptions import Timeout as _CffiTimeoutErr

    _retryable = (_CffiConnErr, _CffiTimeoutErr, ConnectionError, TimeoutError)

    last_err = ""
    response = None
    for attempt in range(4):
        try:
            response = cffi_requests.post(
                url,
                headers=headers,
                json=payload,
                proxies=None,
                verify=False,
                timeout=60,
                impersonate="chrome110",
            )
            break
        except _retryable as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < 3:
                time.sleep(2 + attempt * 2)
                continue
            logger.error("chatgpt2api 上传连接异常（重试用尽）: %s", last_err)
            return False, f"上传异常（重试用尽）: {last_err[:160]}"
        except Exception as exc:  # noqa: BLE001 - 确定性错误重试无意义，直接失败
            logger.error("chatgpt2api 上传异常: %s: %s", type(exc).__name__, exc)
            return False, f"上传异常: {type(exc).__name__}: {str(exc)[:160]}"

    if response is None:
        return False, "上传异常: 未收到响应"

    if response.status_code >= 400:
        detail = f"HTTP {response.status_code}"
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("message") or body.get("error") or body.get("detail") or detail)
        except Exception:
            detail = f"{detail} - {response.text[:200]}"
        return False, f"上传失败: {detail}"

    try:
        data = response.json()
    except Exception:
        return True, f"上传成功（HTTP {response.status_code}）"

    if not isinstance(data, dict):
        return True, "上传成功"

    added = data.get("added")
    skipped = data.get("skipped")
    errors = data.get("errors")
    parts = []
    if added is not None:
        parts.append(f"新增 {added}")
    if skipped is not None:
        parts.append(f"跳过 {skipped}")
    if errors:
        parts.append(f"错误 {errors}")
    summary = "，".join(parts) if parts else "上传成功"

    # `errors` 非空 = 对端在导入后的同步阶段报错了。此时若**一个新号都没进去**
    # （added=0）且没有跳过（skipped=0），说明这个号根本没被接受 —— 不能再报
    # 成功，否则调用方（注册链路）会把失败记成成功，账号静默丢失。
    # 注意 `added=0, skipped=1` 仍算成功：那是幂等重传的正常结果。
    if errors and not added and not skipped:
        return False, f"上传失败（{summary}）"
    return True, f"上传成功（{summary}）"
