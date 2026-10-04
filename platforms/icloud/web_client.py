"""iCloud Web 服务客户端：Session 校验与 Hide My Email 管理。

两组接口共用同一份 Cookie 会话与构建号，因此放在同一个客户端里。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlencode, urlparse, urlunparse

import requests

from .build_info import BuildInfoCache, is_official_service_url
from .constants import (
    DEFAULT_ALIAS_LABEL,
    DEFAULT_SYNC_LIMIT,
    ALIAS_STATUS_ACTIVE,
    ALIAS_STATUS_DISABLED,
    FALLBACK_CLOUD_BUILD,
    FALLBACK_CLOUD_MASTERING,
    endpoints_for,
    normalize_region,
)
from .cookies import (
    dsid_from_cookie_header,
    merge_response_cookies,
    normalize_cookies,
    quote_cookie_header,
)
from .credentials import ICloudCredentials
from .errors import ICloudError, invalid_config, invalid_response, upstream_unavailable
from .models import ImportedSession, PrivateEmail, SessionImportRequest, mask_secret, utcnow
from .transport import (
    WebTransport,
    envelope_error,
    find_string,
    http_error,
    nested_string,
    nested_value,
    web_headers,
)
from .utils import new_uuid, normalize_email_address

logger = logging.getLogger(__name__)

# 只有这两个动作对上游没有副作用：list 是纯读，generate 只是让 Apple 提议一个地址，
# 真正占用地址的是随后的 reserve。其余动作重试可能造成重复注销/删除。
_RETRIABLE_HME_ACTIONS = frozenset({"v2/hme/list", "v1/hme/generate"})


class ICloudWebClient:
    def __init__(self, transport: WebTransport, build_cache: BuildInfoCache) -> None:
        self._transport = transport
        self._builds = build_cache

    # ---------------------------------------------------------------- session

    def import_session(self, request: SessionImportRequest) -> ImportedSession:
        """用 setup/ws/1/validate 校验 Cookie，并解析出 HME 服务地址与 DSID。"""
        cookie_header, _ = normalize_cookies(request.cookie_header, request.cookies_json)
        if not cookie_header and not request.web_auth_token:
            raise invalid_config("需要 Cookie 或 Web 会话令牌")

        cookie_header, cookie_count = quote_cookie_header(cookie_header)
        validate_cookie_header = str(request.validate_cookie_header or "").strip()
        validate_cookie_header = (
            quote_cookie_header(validate_cookie_header)[0] if validate_cookie_header else cookie_header
        )

        region = normalize_region(request.region)
        client_id = str(request.client_id or "").strip() or new_uuid()
        builds = self._builds.get(self._transport, region)

        headers = web_headers(region, endpoints_for(region).setup, content_type="text/plain;charset=UTF-8")
        if validate_cookie_header:
            headers["Cookie"] = validate_cookie_header
        if request.web_auth_token and request.web_auth_token_header.strip():
            headers[request.web_auth_token_header.strip()] = request.web_auth_token

        url = _with_query(
            f"{endpoints_for(region).setup}/validate",
            {
                "clientBuildNumber": builds.cloud_build or FALLBACK_CLOUD_BUILD,
                "clientMasteringNumber": builds.cloud_mastering or FALLBACK_CLOUD_MASTERING,
                "clientId": client_id,
            },
        )
        try:
            response = self._transport.request("POST", url, data="null", headers=headers)
        except requests.RequestException as exc:
            raise upstream_unavailable("连接 iCloud session validate 失败", exc) from exc
        if not response.ok:
            raise http_error(response, "iCloud validate")

        payload = _decode_json(response, "iCloud validate")
        _ensure_terms_accepted(payload)

        cookie_header, cookie_count = merge_response_cookies(cookie_header, response.cookies, region)
        cookie_header, cookie_count = quote_cookie_header(cookie_header)

        dsid = nested_string(payload, "dsInfo", "dsid")
        service_url = nested_string(payload, "webservices", "premiummailsettings", "url")
        if not dsid or not service_url:
            raise invalid_response(
                "iCloud validate 响应缺少 dsInfo.dsid 或 webservices.premiummailsettings.url"
            )
        service_url = _normalize_service_url(service_url, "iCloud 返回的 HME 服务地址无效")
        mail_gateway_url = nested_string(payload, "webservices", "mccgateway", "url")
        if mail_gateway_url:
            mail_gateway_url = _normalize_service_url(
                mail_gateway_url, "iCloud 返回的旧版 Mail 服务地址无效"
            )

        credentials = ICloudCredentials(
            region=region,
            dsid=dsid,
            cookies=cookie_header,
            hme_service_url=service_url,
            mail_gateway_url=mail_gateway_url,
            client_id=client_id,
            client_build_number=builds.cloud_build,
            client_mastering_number=builds.cloud_mastering,
            mail_client_build_number=builds.mail_build,
            mail_client_mastering_number=builds.mail_mastering,
            web_auth_token=request.web_auth_token,
            web_auth_token_header=request.web_auth_token_header,
            imap_host=request.imap_host.strip(),
            imap_port=int(request.imap_port or 0),
            imap_username=request.imap_username.strip(),
            imap_password=request.imap_password,
            sync_limit=DEFAULT_SYNC_LIMIT,
        )
        account_email = normalize_email_address(
            nested_string(payload, "dsInfo", "primaryEmail")
            or nested_string(payload, "dsInfo", "appleId")
            or nested_string(payload, "dsInfo", "appleID")
        )
        return ImportedSession(
            credentials=credentials,
            account_email=account_email,
            masked_dsid=mask_secret(dsid),
            service_host=urlparse(service_url).hostname or "",
            cookie_count=cookie_count,
        )

    # -------------------------------------------------------------------- HME

    def list_private_emails(self, credentials: ICloudCredentials) -> list[PrivateEmail]:
        result = self._hme_request(credentials, "GET", "v2/hme/list")
        return _parse_private_email_list(result)

    def generate_private_email(
        self,
        credentials: ICloudCredentials,
        *,
        label: str = "",
        note: str = "",
    ) -> PrivateEmail:
        # Apple 的 reserve 不接受空标签，会回 {"errorCode":"400","errorMessage":"invalid Label"}。
        # 标签在界面上是选填的，所以这里兜住，避免用户留空就报一个看不懂的上游错误。
        label = label.strip() or DEFAULT_ALIAS_LABEL

        generated = self._hme_request(
            credentials, "POST", "v1/hme/generate", {"langCode": "en-us"}
        )
        address = find_string(generated, "hme", "address", "email")
        if not address:
            raise invalid_response("iCloud HME generate 响应缺少邮箱地址")

        reserved = self._hme_request(
            credentials,
            "POST",
            "v1/hme/reserve",
            {"hme": address, "label": label, "note": note},
        )
        address = find_string(reserved, "hme", "address", "email") or address
        return PrivateEmail(
            address=address.strip().lower(),
            label=label,
            note=note,
            status=ALIAS_STATUS_ACTIVE,
            provider_id=find_string(reserved, "anonymousId", "id", "identifier", "hmeId"),
            created_at=utcnow(),
        )

    def set_private_email_active(
        self,
        credentials: ICloudCredentials,
        *,
        address: str = "",
        provider_id: str = "",
        active: bool,
    ) -> None:
        """停用或重新激活一个隐私邮箱（不删除，可逆）。

        停用后 Apple 不再把邮件转发到该地址，但地址与历史都保留；重新激活即恢复。
        删除流程内部用 `v1/hme/deactivate` 做前置步骤，这里把它单独开放出来，
        让用户能在不销毁地址的前提下临时关掉某个别名。

        `provider_id` 缺失时按地址去上游列表里找——同步过的别名才有
        anonymousId，没同步过的本地行只能靠地址反查。
        """
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            target = str(address or "").strip().lower()
            if not target:
                raise ICloudError(
                    "upstream_rejected", "隐私邮箱缺少地址与 anonymousId，无法操作"
                )
            for candidate in self.list_private_emails(credentials):
                if candidate.address.lower() == target:
                    provider_id = candidate.provider_id
                    break
        if not provider_id:
            raise ICloudError(
                "upstream_rejected", "iCloud 隐私邮箱缺少 anonymousId，请先同步后重试"
            )

        action = "v1/hme/reactivate" if active else "v1/hme/deactivate"
        self._hme_request(credentials, "POST", action, {"anonymousId": provider_id})

    def delete_private_email(
        self,
        credentials: ICloudCredentials,
        *,
        address: str,
        provider_id: str = "",
        status: str = ALIAS_STATUS_ACTIVE,
    ) -> None:
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            for candidate in self.list_private_emails(credentials):
                if candidate.address.lower() == address.strip().lower():
                    provider_id = candidate.provider_id
                    status = candidate.status
                    break
        if not provider_id:
            raise ICloudError(
                "upstream_rejected", "iCloud 隐私邮箱缺少 anonymousId，请先同步后重试"
            )

        payload = {"anonymousId": provider_id}
        if status != ALIAS_STATUS_DISABLED:
            try:
                self._hme_request(credentials, "POST", "v1/hme/deactivate", payload)
            except ICloudError:
                # 地址可能已在上游停用，删除仍应继续。
                pass
        self._hme_request(credentials, "POST", "v1/hme/delete", payload)

    # ---------------------------------------------------------------- 内部实现

    def _hme_request(
        self,
        credentials: ICloudCredentials,
        method: str,
        action: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        try:
            return self._hme_request_once(credentials, method, action, payload)
        except ICloudError as exc:
            # 线上观察到：刚完成两步验证后的头一两次调用，Apple 偶发回一个不带原因的
            # success:false，隔一会儿用同一份凭据就能成功。对没有副作用的动作重试一次
            # 就能把这种抖动挡掉；reserve/delete 这类会改状态的绝不能重试。
            if exc.code != "upstream_rejected" or action not in _RETRIABLE_HME_ACTIONS:
                raise
            logger.warning("iCloud HME 拒绝了 %s，疑似上游抖动，重试一次", action)
            # 顺带强制重新探测构建号：万一哪天真是构建号过期导致的，这一步能自愈。
            self._builds.invalidate(_builds_region(credentials))
            return self._hme_request_once(credentials, method, action, payload)

    def _hme_request_once(
        self,
        credentials: ICloudCredentials,
        method: str,
        action: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        credentials = self._with_current_builds(credentials)
        _require_hme_credentials(credentials)
        url = self._hme_endpoint(credentials, action)

        headers = web_headers(
            credentials.region,
            credentials.hme_service_url,
            content_type="text/plain;charset=UTF-8",
        )
        headers["Cookie"] = quote_cookie_header(credentials.cookies)[0]
        if credentials.web_auth_token and credentials.web_auth_token_header.strip():
            headers[credentials.web_auth_token_header.strip()] = credentials.web_auth_token
        for key, value in credentials.extra_headers.items():
            if key.lower() not in {"host", "content-length", "cookie"}:
                headers[key] = value

        body = json.dumps(payload) if payload is not None else None
        try:
            response = self._transport.request(method, url, data=body, headers=headers)
        except requests.RequestException as exc:
            raise upstream_unavailable("连接 iCloud HME 服务失败", exc) from exc
        if not response.ok:
            raise http_error(response, "iCloud HME")

        envelope = _decode_json(response, "iCloud HME")
        if envelope.get("success") is False:
            # 面向用户的文案会刻意收敛（比如限流不回显上游原文），但排查时需要原文，
            # 否则只剩一句“HME 拒绝了请求”，完全无从下手。
            logger.error("iCloud HME 拒绝了 %s，原始响应: %s", action, json.dumps(envelope)[:600])
            raise envelope_error(envelope, "iCloud HME 拒绝了请求")
        result = envelope.get("result")
        return result if isinstance(result, dict) else envelope

    def _with_current_builds(self, credentials: ICloudCredentials) -> ICloudCredentials:
        """官方域名下强制使用最新构建号，自建反代则沿用凭据里保存的值。"""
        hme_official = is_official_service_url(credentials.hme_service_url)
        mail_official = is_official_service_url(credentials.mail_gateway_url)
        if not hme_official and not mail_official:
            return credentials

        builds = self._builds.get(self._transport, _builds_region(credentials))

        updated = ICloudCredentials.from_dict(credentials.to_dict())
        if hme_official:
            updated.client_build_number = builds.cloud_build
            updated.client_mastering_number = builds.cloud_mastering
        if mail_official:
            updated.mail_client_build_number = builds.mail_build
            updated.mail_client_mastering_number = builds.mail_mastering
        return updated

    @staticmethod
    def _hme_endpoint(credentials: ICloudCredentials, action: str) -> str:
        base = urlparse(credentials.hme_service_url.rstrip("/"))
        path = f"{base.path.rstrip('/')}/{action.lstrip('/')}"
        query = {
            "dsid": _effective_dsid(credentials),
            "clientBuildNumber": credentials.client_build_number.strip() or FALLBACK_CLOUD_BUILD,
            "clientMasteringNumber": credentials.client_mastering_number.strip()
            or FALLBACK_CLOUD_MASTERING,
        }
        if credentials.client_id:
            query["clientId"] = credentials.client_id
        if credentials.ckjs_build_version:
            query["ckjsBuildVersion"] = credentials.ckjs_build_version
        return urlunparse(base._replace(path=path, query=urlencode(sorted(query.items()))))


def _builds_region(credentials: ICloudCredentials) -> str:
    """凭据没写区域时，从服务地址推断，用于挑对构建号来源站点。"""
    region = credentials.region
    if str(region or "").strip():
        return region
    urls = f"{credentials.hme_service_url}{credentials.mail_gateway_url}".lower()
    return "china" if "icloud.com.cn" in urls else ""


def _require_hme_credentials(credentials: ICloudCredentials) -> None:
    missing = [
        name
        for name, value in (
            ("dsid", credentials.dsid),
            ("hme_service_url", credentials.hme_service_url),
            ("cookie", credentials.cookies),
            ("client_id", credentials.client_id),
            ("client_build_number", credentials.client_build_number),
            ("client_mastering_number", credentials.client_mastering_number),
        )
        if not str(value or "").strip()
    ]
    if missing:
        raise invalid_config("HME 会话缺少字段: " + ", ".join(missing))
    parsed = urlparse(credentials.hme_service_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise invalid_config("hme_service_url 必须是有效的 HTTP(S) 地址")


def _effective_dsid(credentials: ICloudCredentials) -> str:
    stored = credentials.dsid.strip()
    from_cookie = dsid_from_cookie_header(credentials.cookies)
    if not from_cookie or from_cookie == stored:
        return stored
    if _is_legacy_rounded_dsid(stored, from_cookie):
        return from_cookie
    raise invalid_config("iCloud 会话中的账号标识不一致，请重新登录主号")


def _is_legacy_rounded_dsid(stored: str, exact: str) -> bool:
    """旧版本用 float 解析 DSID 会造成末位舍入，这类差异可以安全地按 Cookie 修正。"""
    if not stored or not exact or stored == exact or not stored.isdigit() or not exact.isdigit():
        return False
    return stored == f"{float(exact):.0f}"


def _parse_private_email_list(result: Mapping[str, Any]) -> list[PrivateEmail]:
    entries = None
    for key in ("hmeEmails", "hmeList"):
        value = next((item for name, item in result.items() if name.lower() == key.lower()), None)
        if value is not None:
            if not isinstance(value, list):
                raise invalid_response("iCloud HME 邮箱列表格式无效")
            entries = value
            break
    if entries is None:
        raise invalid_response("iCloud HME 列表响应缺少邮箱列表")

    private_emails: list[PrivateEmail] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise invalid_response("iCloud HME 邮箱列表包含无效条目")
        address = normalize_email_address(_scalar(entry, "hme", "address", "email"))
        if not address:
            raise invalid_response("iCloud HME 列表包含无效邮箱地址")
        if address in seen:
            continue
        seen.add(address)
        private_emails.append(
            PrivateEmail(
                address=address,
                label=_scalar(entry, "label").strip(),
                note=_scalar(entry, "note").strip(),
                status=_alias_status(entry),
                provider_id=_scalar(entry, "anonymousId", "id", "identifier", "hmeId").strip(),
                created_at=_entry_created_at(entry),
            )
        )
    return private_emails


def _alias_status(entry: Mapping[str, Any]) -> str:
    for key in ("isActive", "active", "enabled"):
        value = next((item for name, item in entry.items() if name.lower() == key.lower()), None)
        if isinstance(value, bool):
            return ALIAS_STATUS_ACTIVE if value else ALIAS_STATUS_DISABLED
    status = _scalar(entry, "status").strip().lower()
    if any(marker in status for marker in ("inactive", "disabled", "deactivated")):
        return ALIAS_STATUS_DISABLED
    return ALIAS_STATUS_ACTIVE


def _entry_created_at(entry: Mapping[str, Any]) -> datetime:
    for key in ("createTimestamp", "createdAt", "created", "createDate"):
        value = next((item for name, item in entry.items() if name.lower() == key.lower()), None)
        parsed = _parse_web_time(value)
        if parsed is not None:
            return parsed
    return utcnow()


def _parse_web_time(value: Any) -> Optional[datetime]:
    numeric: Optional[float] = None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            numeric = float(text)
        except ValueError:
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                return None
    if numeric is None or numeric <= 0:
        return None
    if numeric > 1e14:
        numeric /= 1_000_000
    elif numeric > 1e11:
        numeric /= 1000
    return datetime.fromtimestamp(numeric, tz=timezone.utc)


def _scalar(entry: Mapping[str, Any], *keys: str) -> str:
    lowered = {str(name).lower(): value for name, value in entry.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _with_query(url: str, query: Mapping[str, str]) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query=urlencode(sorted(query.items()))))


def _normalize_service_url(value: str, error_message: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if not parsed.scheme or not parsed.hostname:
        raise invalid_response(error_message)
    netloc = parsed.hostname if parsed.port == 443 else parsed.netloc
    normalized = urlunparse(
        parsed._replace(netloc=netloc, path=parsed.path.rstrip("/"), query="", fragment="")
    )
    return normalized.rstrip("/")


def _decode_json(response: requests.Response, service_name: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise invalid_response(f"{service_name} 返回了无效 JSON", exc) from exc
    if not isinstance(payload, dict):
        raise invalid_response(f"{service_name} 返回了非对象 JSON")
    return payload


def _ensure_terms_accepted(payload: Mapping[str, Any]) -> None:
    if nested_value(payload, "dsInfo", "termsUpdateNeeded") is True:
        raise ICloudError(
            "upstream_rejected",
            "iCloud 账号需要先接受最新版服务条款：请在浏览器登录 iCloud 网页接受条款后重试",
        )
