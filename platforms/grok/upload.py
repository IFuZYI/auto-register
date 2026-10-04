"""CPA / Sub2API 上传。

参考：
- reference/grok/grokRegister-cpa/sso_to_auth_json.py:745-786（CPA Management API）
- reference/grok/cloudTemp-grokzhuce/.env.example（Sub2API 分组解析）
- platforms/chatgpt/sub2api_upload.py（本项目现有实现风格）
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .oauth_device import cpa_auth_filename


def _http_post(url: str, *, headers: dict, payload: Any, timeout: int = 30) -> tuple[bool, str]:
    try:
        from curl_cffi import requests as curl_requests

        resp = curl_requests.post(
            url, headers=headers, json=payload, impersonate="chrome", timeout=timeout
        )
    except Exception as exc:
        return False, f"请求异常: {exc}"
    text = str(resp.text or "")[:300]
    if 200 <= int(resp.status_code) < 300:
        return True, f"HTTP {resp.status_code} {text}"
    return False, f"HTTP {resp.status_code} {text}"


def upload_to_cpa(
    record: dict,
    api_url: str = "",
    api_key: str = "",
    proxy: str = "",
    timeout: int = 30,
) -> tuple[bool, str]:
    """上传 CPA auth 记录到 CLIProxyAPI Management API。

    必须走 **multipart**（字段名 `file`，文件名 `<email>.json`）：
    CLIProxyAPI 的 `POST /v0/management/auth-files` 只认 multipart 或
    `?name=xxx.json` 的裸 JSON 体。此前这里发的是 `{"name":…,"content":…}`
    的 JSON —— 实测三个候选路径全 404（`/v0/management/auth-files` 收到 JSON
    体时没有匹配路由），于是 Grok 的 CPA 上传从来没成功过。
    对照实现：`platforms/chatgpt/cpa_upload.py` 用的就是 multipart。

    `proxy` 非空时写进记录顶层 `proxy_url`（CPA 读它给这个账号固定出口）。
    由「上传代理」开关控制（见 `services.chatgpt_sync.upload_proxy_for`）。

    出处：sso_to_auth_json.py:745-786（原参考实现）+ CLIProxyAPI
    `internal/api/handlers/management/auth_files_crud.go:UploadAuthFile`。
    """
    api_url = str(api_url or "").strip().rstrip("/")
    if not api_url:
        return False, "未配置 CPA API URL"

    # 密钥也要 strip：配置页粘贴时极易带上首尾空白/换行，拼进 Bearer 头
    # 会被对端判为鉴权失败（`Bearer  xxx` 与 `Bearer xxx` 不等价）。
    api_key = str(api_key or "").strip()

    filename = cpa_auth_filename(record)
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # 代理写进记录本体（上传请求本身不走代理 —— 代理是给 CPA 调上游用的）。
    payload_record = dict(record)
    proxy_url = str(proxy or "").strip()
    if proxy_url:
        payload_record["proxy_url"] = proxy_url

    content = json.dumps(payload_record, ensure_ascii=False, indent=2).encode("utf-8")
    mime = None
    try:
        from curl_cffi import CurlMime
        from curl_cffi import requests as curl_requests

        mime = CurlMime()
        mime.addpart(name="file", data=content, filename=filename, content_type="application/json")
        resp = curl_requests.post(
            f"{api_url}/v0/management/auth-files",
            multipart=mime,
            headers=headers,
            timeout=timeout,
            impersonate="chrome",
        )
        status = int(getattr(resp, "status_code", 0) or 0)
        if 200 <= status < 300:
            return True, f"{filename} 已上传"
        return False, f"上传失败: HTTP {status} {str(resp.text or '')[:200]}"
    except Exception as exc:  # noqa: BLE001 - 上传失败要带原因回调用方
        return False, f"上传异常: {type(exc).__name__}: {str(exc)[:150]}"
    finally:
        if mime is not None:
            try:
                mime.close()
            except Exception:
                pass


def upload_to_sub2api(
    record: dict,
    api_url: str = "",
    api_key: str = "",
    group: str = "",
    timeout: int = 30,
) -> tuple[bool, str]:
    """上传到 Sub2API 管理接口。

    参考 cloudTemp .env.example：只填分组名称，ID 由服务端解析。
    """
    api_url = str(api_url or "").strip().rstrip("/")
    if not api_url:
        return False, "未配置 Sub2API URL"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key

    payload = {
        "type": "xai",
        "auth_kind": "oauth",
        "email": record.get("email", ""),
        "access_token": record.get("access_token", ""),
        "refresh_token": record.get("refresh_token", ""),
        "id_token": record.get("id_token", ""),
        "expired": record.get("expired", ""),
    }
    if group:
        payload["group_name"] = group
        payload["group"] = group

    last = ""
    for path in ("/api/admin/accounts", "/api/accounts", "/admin/api/accounts"):
        ok, msg = _http_post(f"{api_url}{path}", headers=headers, payload=payload, timeout=timeout)
        if ok:
            return True, f"{record.get('email', '')} 已导入 Sub2API"
        last = msg
    return False, f"上传失败: {last}"


def resolve_sub2api_group(
    api_url: str,
    api_key: str,
    group_name: str,
    timeout: int = 20,
) -> Optional[str]:
    """按分组名称解析 group_id（参考 cloudTemp 的只读缓存策略）。"""
    if not api_url or not group_name:
        return None
    api_url = str(api_url).strip().rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        from curl_cffi import requests as curl_requests

        for path in ("/api/admin/groups", "/api/groups"):
            try:
                resp = curl_requests.get(
                    f"{api_url}{path}", headers=headers, impersonate="chrome", timeout=timeout
                )
                if int(resp.status_code) != 200:
                    continue
                doc = resp.json()
                items = doc.get("data") if isinstance(doc, dict) else doc
                if isinstance(items, dict):
                    items = items.get("items") or items.get("list") or []
                for item in items or []:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or item.get("group_name") or "")
                    if name == group_name:
                        return str(item.get("id") or item.get("group_id") or "")
            except Exception:
                continue
    except Exception:
        pass
    return None


__all__ = ["upload_to_cpa", "upload_to_sub2api", "resolve_sub2api_group"]
