"""外部系统同步（自动导入 / 回填）"""

from __future__ import annotations

from typing import Any

from services.chatgpt_sync import (
    _get_account_extra,
    persist_chatgpt2api_sync_result,
    persist_cpa_sync_result,
    persist_sub2api_sync_result,
    upload_chatgpt_account_to_cpa,
)


def _is_config_enabled(value: Any, default: bool = False) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "on", "enabled"}


#: 平台 → 「CPA 自动上传」开关的配置键。
#:
#: 用户要求「CPA 面板里的自动上传不同平台要分开开启」：ChatGPT 与 Grok 是两条
#: 独立的产物（前者传 codex 凭据，后者传 xai 凭据），常有一边先跑通、另一边还
#: 在调试。合成一个开关时开一边会连带把另一边也推上去。
_CPA_UPLOAD_ENABLED_KEYS: dict[str, str] = {
    "chatgpt": "cpa_upload_chatgpt_enabled",
    "grok": "cpa_upload_grok_enabled",
}


def cpa_upload_enabled_for(platform: str, config: Any = None) -> bool:
    """这个平台的账号是否要自动上传到 CPA。

    取值的三层顺序（先具体后笼统）：
    1. 平台专属开关（`cpa_upload_chatgpt_enabled` / `cpa_upload_grok_enabled`）
    2. 历史单开关 `cpa_enabled` —— 老配置里它=true 表示「两边都传」，
       不能因为新增了平台键就让老用户的自动上传静默失效
    3. 兜底：填了 CPA 地址就认为要传（与其它外部同步的默认口径一致）

    空串一律表示「没设置」，交给下一层 —— 而不是当成 false。
    """
    if config is None:
        from core.config_store import config_store

        config = config_store

    def _get(key: str) -> str:
        try:
            return str(config.get(key, "") or "").strip()
        except Exception:
            return ""

    key = _CPA_UPLOAD_ENABLED_KEYS.get(str(platform or "").strip().lower())
    if key:
        raw = _get(key)
        if raw:
            return _is_config_enabled(raw)

    raw_legacy = _get("cpa_enabled")
    if raw_legacy:
        return _is_config_enabled(raw_legacy)

    return bool(_get("cpa_api_url"))


def _pick_text(source: Any, *keys: str, default: str = "") -> str:
    if not isinstance(source, dict):
        return default
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        text = value.strip() if isinstance(value, str) else str(value).strip()
        if text:
            return text
    return default


def sync_account(account) -> list[dict[str, Any]]:
    """根据平台将账号同步到外部系统。"""
    from core.config_store import config_store

    platform = getattr(account, "platform", "")
    results: list[dict[str, Any]] = []

    def _build_chatgpt_upload_account():
        class _A:
            pass

        a = _A()
        a.email = account.email
        extra = _get_account_extra(account)
        a.access_token = _pick_text(extra, "access_token", "accessToken") or account.token
        a.refresh_token = _pick_text(extra, "refresh_token", "refreshToken")
        a.id_token = _pick_text(extra, "id_token", "idToken")
        a.session_token = _pick_text(extra, "session_token", "sessionToken")
        a.client_id = _pick_text(extra, "client_id", "clientId", default="app_EMoamEEZ73f0CkXaXp7hrann")
        return a

    if platform == "chatgpt":
        upload_account = _build_chatgpt_upload_account()

        # 贡献模式优先级最高：开启后仅上传到贡献服务器，避免重复上报到其它平台。
        contribution_enabled = _is_config_enabled(config_store.get("contribution_enabled", "0"))
        if contribution_enabled:
            contribution_mode = str(config_store.get("contribution_mode", "codex") or "codex").strip().lower()

            if contribution_mode == "custom":
                # 自定义贡献系统模式
                custom_url = str(config_store.get("custom_contribution_url", "") or "").strip()
                custom_token = str(config_store.get("custom_contribution_token", "") or "").strip()
                if not custom_url:
                    msg = "自定义贡献服务器地址未配置"
                    persist_cpa_sync_result(account, False, msg)
                    results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                    return results
                if not custom_token:
                    msg = "自定义贡献系统 token 未配置（请先绑定邮箱）"
                    persist_cpa_sync_result(account, False, msg)
                    results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                    return results

                try:
                    import requests
                    from platforms.chatgpt.cpa_upload import generate_token_json

                    # 生成完整的 token JSON
                    extra = _get_account_extra(account)
                    token_json = generate_token_json(account)

                    # 如果 token_json 中没有 refresh_token，从 extra 获取
                    if not token_json.get("refresh_token"):
                        refresh_token = _pick_text(extra, "refresh_token", "refreshToken")
                        print(f"[DEBUG] extra keys: {list(extra.keys())}")
                        print(f"[DEBUG] refresh_token from extra: {refresh_token[:20] if refresh_token else 'EMPTY'}")
                        if refresh_token:
                            token_json["refresh_token"] = refresh_token
                    if not token_json.get("access_token"):
                        access_token = _pick_text(extra, "access_token", "accessToken") or getattr(account, "token", "")
                        if access_token:
                            token_json["access_token"] = access_token
                    if not token_json.get("id_token"):
                        id_token = _pick_text(extra, "id_token", "idToken")
                        if id_token:
                            token_json["id_token"] = id_token
                    if not token_json.get("client_id"):
                        client_id = _pick_text(extra, "client_id", "clientId")
                        if client_id:
                            token_json["client_id"] = client_id

                    refresh_token = str(token_json.get("refresh_token") or "").strip()
                    access_token = str(token_json.get("access_token") or "").strip()

                    # 验证必须有 refresh_token
                    print(f"[DEBUG] Final token_json keys: {list(token_json.keys())}")
                    print(f"[DEBUG] Final refresh_token: {refresh_token[:20] if refresh_token else 'EMPTY'}")
                    if not refresh_token:
                        msg = "账号缺少 refresh_token"
                        persist_cpa_sync_result(account, False, msg)
                        results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                        return results

                    resp = requests.post(
                        f"{custom_url.rstrip('/')}/api/upload",
                        json={
                            "email": account.email,
                            "refresh_token": refresh_token,
                            "access_token": access_token,
                            "token_json": token_json,
                        },
                        headers={"Authorization": f"Bearer {custom_token}"},
                        timeout=15,
                    )
                    data = resp.json()
                    if resp.status_code >= 400:
                        msg = data.get("error") or data.get("message") or str(data)
                        persist_cpa_sync_result(account, False, msg)
                        results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                        return results

                    msg = f"上传成功: {data.get('message', '')}"
                    persist_cpa_sync_result(account, True, msg)
                    results.append({"name": "CustomContribution", "ok": True, "msg": msg})
                    return results
                except Exception as exc:
                    msg = f"上传到自定义贡献系统失败: {exc}"
                    persist_cpa_sync_result(account, False, msg)
                    results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                    return results
            else:
                # codex2api 模式（原有逻辑）
                contribution_url = str(config_store.get("contribution_server_url", "") or "").strip()
                contribution_key = str(config_store.get("contribution_key", "") or "").strip()
                if not contribution_url:
                    msg = "Contribution 服务器地址未配置"
                    persist_cpa_sync_result(account, False, msg)
                    results.append({"name": "Contribution", "ok": False, "msg": msg})
                    return results

                ok, msg = upload_chatgpt_account_to_cpa(
                    account,
                    api_url=contribution_url,
                    api_key=contribution_key or None,
                )
                persist_cpa_sync_result(account, ok, msg)
                results.append({"name": "Contribution", "ok": ok, "msg": msg})
                return results

        cpa_url = str(config_store.get("cpa_api_url", "") or "").strip()
        # 按平台开关（用户要求 ChatGPT / Grok 分开开启）；未设平台键时回落
        # 历史单开关 `cpa_enabled`，再回落「填了地址就传」。
        if cpa_upload_enabled_for("chatgpt") and cpa_url:
            ok, msg = upload_chatgpt_account_to_cpa(account)
            persist_cpa_sync_result(account, ok, msg)
            results.append({"name": "CPA", "ok": ok, "msg": msg})

        # 关键逻辑：ChatGPT 现在支持同时回填 CPA 和 Sub2API，互不覆盖、分别上报结果。
        sub2api_url = str(config_store.get("sub2api_api_url", "") or "").strip()
        sub2api_key = str(config_store.get("sub2api_api_key", "") or "").strip()
        sub2api_enabled = _is_config_enabled(
            config_store.get("sub2api_enabled", ""),
            default=bool(sub2api_url and sub2api_key),
        )
        if sub2api_enabled and sub2api_url and sub2api_key:
            from platforms.chatgpt.sub2api_upload import upload_to_sub2api

            ok, msg = upload_to_sub2api(
                upload_account,
                api_url=sub2api_url,
                api_key=sub2api_key,
            )
            persist_sub2api_sync_result(account, ok, msg)
            results.append({"name": "Sub2API", "ok": ok, "msg": msg})

        # chatgpt2api：普通网页号池，与 CPA 的 codex 凭据互不相干，所以独立开关。
        c2a_url = str(config_store.get("chatgpt2api_api_url", "") or "").strip()
        c2a_key = str(config_store.get("chatgpt2api_api_key", "") or "").strip()
        c2a_enabled = _is_config_enabled(
            config_store.get("chatgpt2api_enabled", ""),
            default=bool(c2a_url and c2a_key),
        )
        if c2a_enabled and c2a_url and c2a_key:
            from platforms.chatgpt.chatgpt2api_upload import upload_to_chatgpt2api
            from services.chatgpt_sync import upload_proxy_for

            ok, msg = upload_to_chatgpt2api(
                upload_account,
                api_url=c2a_url,
                api_key=c2a_key,
                proxy=upload_proxy_for("chatgpt2api", _get_account_extra(account)),
            )
            persist_chatgpt2api_sync_result(account, ok, msg)
            results.append({"name": "chatgpt2api", "ok": ok, "msg": msg})

    return results
