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

## 凭据字段（对端 ≥ 2.2.0，2026-10-26）

对端可写字段已扩到 `access_token` / `refresh_token` / `id_token` / `email` /
`password` / `totp_secret` / `proxy` / `type` / `source_type`。对端文档明说
「务必带 `password` + `totp_secret`。RT 会失效，没有密码就只能人工救」——
本模块因此额外下发 `refresh_token` / `id_token` / `password` / `totp_secret`。

**只发非空字段，这条是硬要求，不是格式洁癖。** 对端 `addAccountPayloads` 对已
存在的账号做 `merged = copyMap(current); for k, v := range incoming { merged[k] = v }`
—— incoming 的每个键都会覆盖，**包括空串**（而且 `skipped` 的账号照样走这段
合并）。所以传 `password: ""` 会抹掉对端已有的密码、传 `totp_secret: ""` 会让
开了 2FA 的号永久锁死。**永远不要为了「字段形状稳定」把空值填进去。**

`source_type` 与是否带 RT 无关：对端 `prepareAccountPayload` 只在
`type == "codex"` 或 `export_type == "codex"` 时置 `SourceCodex`（本模块传
`source_type: "web"`，不受影响）。旧注释里「多传 RT 会让对端当成另一种源」是
2.2.0 之前的猜测，已证伪 —— 对端支持且推荐带上。

字段支持依赖对端 ≥ 2.2.0：老版本会忽略不认识的字段，不会因为多传而坏掉。

与 CPA / Sub2API 的区别：这里传的是**普通网页号**（`access_token` 必需），
不生成 codex 凭证（`type` 里绝不出现 `codex`）。
"""

from __future__ import annotations
from core.panel_tls import verify_for

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


def _account_extra(account) -> dict[str, Any]:
    """取账号的 `extra`（duck-typed 对象可能压根没有这个属性）。"""
    extra = getattr(account, "extra", None)
    return extra if isinstance(extra, dict) else {}


def _import_mode() -> str:
    """`chatgpt2api_import_mode`：`atrt`（只推 AT/RT）或 `full`（完全账户）。

    默认 `full` —— 与加这个开关之前的行为一致（那时总是尽量带上
    `password` / `totp_secret`）。升级到本版本不会改变既有部署的推送内容。

    取不到配置（config_store 异常）时同样回落 `full`：宁可多推一个字段，
    也不要因为读配置失败而静默降级成「只推 AT/RT」—— 那会让对端的
    「RT 失效自动重登」失效，而用户完全看不出来。
    """
    try:
        from core.config_store import config_store

        raw = str(config_store.get("chatgpt2api_import_mode", "") or "").strip().lower()
    except Exception as exc:  # noqa: BLE001 - 读配置失败不该让上传失败
        logger.debug("读取 chatgpt2api_import_mode 失败，按 full 处理: %s", exc)
        return "full"
    return "atrt" if raw == "atrt" else "full"


def build_chatgpt2api_account(account, proxy: str = "") -> dict[str, Any]:
    """把本地账号转成 chatgpt2api 的导入对象。

    必填 `access_token`；可选 `email` / `account_id` / `type`（套餐类型，对端
    自己再归一）/ `proxy`，以及对端 ≥ 2.2.0 才认的 `refresh_token` /
    `id_token` / `password` / `totp_secret`。

    **四个凭据字段一律「非空才下发」。** 对端对已存在的账号按
    `merged = copyMap(current); for k, v := range incoming { merged[k] = v }` 合并
    —— 每个 incoming 键都会覆盖，**空串也会**（`skipped` 的账号同样走这段合并）。
    发 `password: ""` 就抹掉对端已有密码、发 `totp_secret: ""` 就让 2FA 号永久
    锁死。所以这里绝不能为「字段形状稳定」把空值塞进去。

    `id_token` 只从 extra / 账号属性取，**不取 `generate_token_json` 的返回值**：
    那里在没有真 IDT 时会用 `_build_compat_id_token` 现造一个签名是假值的兼容
    token（docstring 写明仅供本地解析），发给对端等于用假凭证覆盖真 IDT，而且它
    永远非空 —— 会让「非空才下发」形同虚设。`refresh_token` 没有这个问题
    （`generate_token_json` 原样透传，不会合成），所以两处都取。

    `proxy` 非空时作为 `proxy` 字段带上（对端 `_add_account_payloads` 认它，
    custom 模式直接是代理 URL）。由「上传代理」开关控制。

    对端 < 2.2.0 会忽略这些新字段，不会因为多传而失败。
    """
    from core.credential_fields import get_credential

    extra = _account_extra(account)
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

    # 四个凭据字段：先 extra（认 camelCase 别名），再账号属性兜底。
    refresh_token = _first_non_empty(
        get_credential(extra, "refresh_token"),
        getattr(account, "refresh_token", ""),
        token_data.get("refresh_token"),
    )
    id_token = _first_non_empty(
        get_credential(extra, "id_token"),
        getattr(account, "id_token", ""),
        # 故意不取 token_data["id_token"] —— 见 docstring：可能是本地合成的假 IDT
    )
    password = _first_non_empty(extra.get("password"), getattr(account, "password", ""))
    totp_secret = _first_non_empty(extra.get("totp_secret"))

    item: dict[str, Any] = {"access_token": access_token, "source_type": "web"}
    if email:
        item["email"] = email
    if account_id:
        item["account_id"] = account_id
    proxy_url = str(proxy or "").strip()
    if proxy_url:
        item["proxy"] = proxy_url
    if refresh_token:
        item["refresh_token"] = refresh_token
    if id_token:
        item["id_token"] = id_token
    # 「导入内容」= 完全账户时才带密码与 2FA 密钥。
    #
    # 只推 AT/RT 模式（`chatgpt2api_import_mode=atrt`）下**连键都不出现** ——
    # 不是置空：对端 `merged[k] = v` 会让空串覆盖掉已有密码（docstring 那条
    # 硬要求的同一个理由）。不出现就完全不碰对端已存的东西。
    if _import_mode() == "full":
        if password:
            item["password"] = password
        if totp_secret:
            # 开了 2FA 的号没有它，对端「失效后自动密码重登」必失败（只能人工救）
            item["totp_secret"] = totp_secret
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
                verify=verify_for("chatgpt2api"),
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
