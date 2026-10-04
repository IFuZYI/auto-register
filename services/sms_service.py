"""手机接码服务（sms-activate 协议系）。实现在 `services/sms/`。

供 ChatGPT 注册链路命中 ``add-phone`` 时自动租号、收码、验证。

目前接入 SmsBower 与 HeroSMS，两家共用 sms-activate 的 ``handler_api.php`` 协议，
差别只在 base_url 和固定价格参数的写法上，所以由同一个 provider 类覆盖。

三段式用法（``AuthFlow._handle_add_phone_via_sms`` 就是这么调的）：

    controller = build_phone_callback(config, log_fn=...)
    phone = controller.get_phone()          # 租号
    code = controller.get_code(timeout=80)  # 等短信
    controller.report_success()             # 业务侧验证通过

⚠️ OpenAI 自 2025 年起对大部分国家改用 WhatsApp 验证，纯 SMS 路径实测只有泰国
（country_id=52）稳定可用。其它国家可能抽到 WhatsApp 号导致收不到短信，所以
``OPENAI_SMS_COUNTRIES`` 白名单之外的国家在自动选号时会打告警但不阻止。

────────────────────────────────────────────────────────────────────────
本文件是**门面**（历史导入路径）。实现在 `services/sms/`。

以下名字**刻意留在门面**，因为测试既读又写它们：

* ``_SMS_CACHE`` / ``_SMS_CACHE_LOCK`` / ``_SMS_VERIFY_LOCK`` —— 测试直接赋值重置
  （``sms_service._SMS_CACHE = None``）。实现侧一律经门面读取，保证「测试重置的
  那一份」与「实现使用的那一份」是同一个对象。
* ``_cache_file`` —— 测试 ``mock.patch.object(sms_service, "_cache_file", …)``
  把它替换成临时路径。实现侧必须经门面调用 ``_facade._cache_file()``，patch 才生效。
* ``logger`` —— 测试 ``patch.object(sms_service.logger, "warning")``。

因此**顺序不能变**：这些名字必须在 import `services.sms` 之前定义好，
否则实现包反向读取门面时取不到它们。
"""
from __future__ import annotations

import hashlib  # noqa: F401  （保留旧模块泄漏的导入名，维持导入面逐字节一致）
import json  # noqa: F401
import logging
import threading
import time  # noqa: F401
from abc import ABC, abstractmethod  # noqa: F401
from dataclasses import dataclass, field  # noqa: F401
from pathlib import Path
from typing import Callable, Optional  # noqa: F401

import requests  # noqa: F401

logger = logging.getLogger(__name__)

_SMS_CACHE_LOCK = threading.Lock()
_SMS_VERIFY_LOCK = threading.RLock()
_SMS_CACHE: Optional[dict] = None  # 跨线程共享的号码复用缓存


def _cache_file() -> Path:
    # 原 services/sms_service.py 是 parents[1]；本文件仍在 services/ 下，保持不变。
    cache_dir = Path(__file__).resolve().parents[1] / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / ".sms_phone_cache.json"


from services.sms import (  # noqa: E402,F401  （必须在状态定义之后）
    BaseSmsProvider,
    OPENAI_SMS_COUNTRIES,
    PhoneCallbackController,
    SMS_COUNTRY_NAMES_CN,
    SMS_DEFAULT_COUNTRY,
    SMS_DEFAULT_SERVICE,
    SMS_PHONE_LIFETIME,
    SMS_PROVIDERS,
    SMS_STATUS_CANCELED,
    SmsActivateProvider,
    SmsActivation,
    build_phone_callback,
    country_label,
    create_sms_provider,
    resolve_sms_settings,
)
from services.sms.parsing import (  # noqa: E402,F401  （私有助手，测试直接取用）
    _hash_secret,
    _make_sms_candidate,
    _parse_sms_status_text,
    _safe_bool,
    _safe_float,
    _safe_int,
    _status_token,
)

__all__ = [
    "BaseSmsProvider",
    "OPENAI_SMS_COUNTRIES",
    "PhoneCallbackController",
    "SMS_COUNTRY_NAMES_CN",
    "SMS_DEFAULT_COUNTRY",
    "SMS_DEFAULT_SERVICE",
    "SMS_PHONE_LIFETIME",
    "SMS_PROVIDERS",
    "SMS_STATUS_CANCELED",
    "SmsActivateProvider",
    "SmsActivation",
    "build_phone_callback",
    "country_label",
    "create_sms_provider",
    "resolve_sms_settings",
]
