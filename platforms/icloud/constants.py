"""iCloud 端点、区域与默认参数。"""

from __future__ import annotations

from dataclasses import dataclass

REGION_GLOBAL = "global"
REGION_CHINA = "china"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.3.1 Safari/605.1.15"
)
OAUTH_CLIENT_ID = "d39ba9916b7251055b22c7f910e2ea796ee65e98b2ddecea8f5dde8d9d1a815d"

# Apple 页面探测失败时使用的兜底构建号。这组值会随 iCloud 发版过期，过期后 Apple 会
# 直接拒绝请求且不说明原因，所以探测失败时会打警告日志并强制重新探测一次（见
# build_info.py）。若日志里频繁出现构建号告警，从 www.icloud.com 页面的
# data-cw-private-build-number 属性取新值更新这里。
FALLBACK_CLOUD_BUILD = "2630Build35"
FALLBACK_CLOUD_MASTERING = "2630Build35"
FALLBACK_MAIL_BUILD = "2630Hotfix39"
FALLBACK_MAIL_MASTERING = "2630Hotfix39"

# Apple 要求隐私邮箱必须带标签，界面上留空时用这个。
DEFAULT_ALIAS_LABEL = "隐私邮箱"

DEFAULT_IMAP_HOST = "imap.mail.me.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_SYNC_LIMIT = 50
MAX_SYNC_LIMIT = 200
DEFAULT_TIMEOUT_SECONDS = 30

LOGIN_STATUS_VERIFICATION_REQUIRED = "verification_required"
LOGIN_STATUS_COMPLETED = "completed"

DELIVERY_PUSH = "trusted_devices"
DELIVERY_SMS = "sms"
DELIVERY_SMS_SELECT = "sms_selection_required"

ALIAS_STATUS_ACTIVE = "active"
ALIAS_STATUS_DISABLED = "disabled"

# 号池取用状态。与 `active`/`disabled` 是**两个维度**：
#   - `active`/`disabled` 说的是「这个别名在 Apple 那边还能不能收信」（可逆开关）；
#   - `unpooled`/`available`/`in_use`/`used` 说的是「它进没进号池、注册任务用过没有」。
# 一个别名可以既是 active 又是 used —— 注册成功但别名仍开着继续收信。
# Outlook 号池（`core/mailboxes/channels/outlook/`）用的是同一套语义，
# 两边的池页面因此能显示一致的「未使用 / 使用中 / 已使用」标签。
POOL_STATUS_AVAILABLE = "available"
POOL_STATUS_IN_USE = "in_use"
POOL_STATUS_USED = "used"
# `unpooled`（未入池）是 iCloud 独有的**第三态**：生成/同步下来的别名默认落在这里，
# 注册取号跳过它。必须在面板上手动勾选「导入邮箱池」才转成 available。
#
# 为什么要有它：以前生成/同步出来的别名一律直接是 available，等于「从 Apple 拉
# 下来多少就自动全部可领」。用户要的是「账号邮箱要选择导入才能导入邮箱池，不是
# 全部导入」——把入池动作显式化。Outlook 号池没有这一态（它的导入面板本身就是
# 显式动作），所以它不在 POOL_STATUSES 里（那个元组用于校验「可手动设置的值」）。
POOL_STATUS_UNPOOLED = "unpooled"
POOL_STATUSES = (POOL_STATUS_AVAILABLE, POOL_STATUS_IN_USE, POOL_STATUS_USED)

# Apple 转发隐私邮件时用于保留信封收件人的头部，按优先级排列。
DELIVERY_HEADER_NAMES = (
    "Delivered-To",
    "X-Original-To",
    "Original-Recipient",
    "Envelope-To",
    "X-Envelope-To",
    "X-Forwarded-To",
)


@dataclass(frozen=True)
class RegionEndpoints:
    auth: str
    setup: str
    home: str
    origin: str


_ENDPOINTS = {
    REGION_CHINA: RegionEndpoints(
        auth="https://idmsa.apple.com.cn/appleauth/auth",
        setup="https://setup.icloud.com.cn/setup/ws/1",
        home="https://www.icloud.com.cn",
        origin="https://www.icloud.com.cn",
    ),
    REGION_GLOBAL: RegionEndpoints(
        auth="https://idmsa.apple.com/appleauth/auth",
        setup="https://setup.icloud.com/setup/ws/1",
        home="https://www.icloud.com",
        origin="https://www.icloud.com",
    ),
}


def normalize_region(region: str | None) -> str:
    if str(region or "").strip().lower() in {"cn", "china", "icloud.com.cn"}:
        return REGION_CHINA
    return REGION_GLOBAL


def endpoints_for(region: str | None) -> RegionEndpoints:
    return _ENDPOINTS[normalize_region(region)]


def cookie_domain_for(region: str | None) -> str:
    return "icloud.com.cn" if normalize_region(region) == REGION_CHINA else "icloud.com"
