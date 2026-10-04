"""手机接码服务：provider 抽象、SmsBower/HeroSMS 实现、租号控制器。

拆分理由：原 `services/sms_service.py` 1158 行里混着纯数据（国家名表）、
纯函数（解析/兜底转换）与有状态实现（provider + 控制器）。拆开后
「加一个接码平台」只需动 `providers.py`，不必在一屏里翻国家名表。

⚠️ 三个模块级可变状态（`_SMS_CACHE` / `_SMS_CACHE_LOCK` / `_SMS_VERIFY_LOCK`）
与 `_cache_file` **不在本包定义**，而是留在门面 `services/sms_service.py`，
由 `providers.py` / `controller.py` 反向读取 —— 保证测试重置的那一份与实现使用的那一份是同一个。
"""
from services.sms.constants import (  # noqa: F401
    OPENAI_SMS_COUNTRIES,
    SMS_COUNTRY_NAMES_CN,
    SMS_DEFAULT_COUNTRY,
    SMS_DEFAULT_SERVICE,
    SMS_PHONE_LIFETIME,
    SMS_PROVIDERS,
    SMS_STATUS_CANCELED,
)
from services.sms.controller import (  # noqa: F401
    PhoneCallbackController,
    build_phone_callback,
    create_sms_provider,
    resolve_sms_settings,
)
from services.sms.parsing import (  # noqa: F401
    country_label,
)
from services.sms.providers import (  # noqa: F401
    BaseSmsProvider,
    SmsActivateProvider,
    SmsActivation,
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
