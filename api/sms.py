"""手机接码配置的自检接口。

设置页保存 API Key 之前得先知道这把 key 到底能不能用、哪个国家有货，
所以这里只提供两个只读探针：查余额、查国家排名。真正的租号发生在注册链路里。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.sms_service import (
    SMS_DEFAULT_COUNTRY,
    SMS_DEFAULT_SERVICE,
    SMS_PROVIDERS,
    OPENAI_SMS_COUNTRIES,
    SMS_COUNTRY_NAMES_CN,
    create_sms_provider,
    resolve_sms_settings,
)

router = APIRouter(prefix="/sms", tags=["sms"])


class SmsProbeRequest(BaseModel):
    """探针入参，留空则回落到已保存的全局配置。"""

    provider: str = ""
    api_key: str = ""
    service: str = ""
    limit: int = 20


def _build_provider(body: SmsProbeRequest):
    settings = resolve_sms_settings()
    if body.provider:
        settings["sms_provider"] = body.provider
    if body.api_key:
        settings["sms_api_key"] = body.api_key
    if body.service:
        settings["sms_service"] = body.service

    provider_key = str(settings.get("sms_provider") or "smsbower")
    try:
        return create_sms_provider(provider_key, settings), settings
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/providers")
def list_sms_providers():
    return {
        "items": [
            {"value": key, "label": meta["label"], "base_url": meta["base_url"]}
            for key, meta in SMS_PROVIDERS.items()
        ],
        "default_service": SMS_DEFAULT_SERVICE,
        "openai_sms_countries": sorted(OPENAI_SMS_COUNTRIES),
    }


@router.get("/country-options")
def list_sms_country_options():
    """设置页国家下拉的选项：平台的国家 ID 配中文名。

    平台接口只认数字 ID，让人对着 ``52`` 猜是哪个国家没有意义；标签里同时保留
    ID，既能按中文搜也能按数字搜。
    """
    return {
        "items": [
            {
                "value": country_id,
                "name": name,
                "label": f"{name} ({country_id})",
                "openai_sms_whitelisted": country_id in OPENAI_SMS_COUNTRIES,
            }
            for country_id, name in SMS_COUNTRY_NAMES_CN.items()
        ],
        "default_country": SMS_DEFAULT_COUNTRY,
    }


@router.post("/balance")
def get_sms_balance(body: SmsProbeRequest):
    provider, settings = _build_provider(body)
    try:
        balance = provider.get_balance()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"查询余额失败: {exc}") from exc
    return {"provider": settings.get("sms_provider"), "balance": balance}


@router.post("/countries")
def get_sms_countries(body: SmsProbeRequest):
    provider, settings = _build_provider(body)
    service = str(body.service or settings.get("sms_service") or SMS_DEFAULT_SERVICE)
    try:
        rows = provider.get_top_countries(service=service)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"查询国家排名失败: {exc}") from exc

    limit = max(1, min(int(body.limit or 20), 200))
    return {
        "service": service,
        "items": [
            {
                "country": str(row.get("country") or ""),
                "name": SMS_COUNTRY_NAMES_CN.get(str(row.get("country") or ""), ""),
                "price": row.get("price"),
                "count": row.get("count"),
                "openai_sms_whitelisted": str(row.get("country") or "") in OPENAI_SMS_COUNTRIES,
            }
            for row in rows[:limit]
        ],
    }
