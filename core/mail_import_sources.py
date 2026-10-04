"""邮箱导入视图与号池账号类型的映射（纯数据，无 IO）。

放在 core 的原因：`core/mailboxes/channels/outlook/` 需要按视图筛选号池里
该取哪一类账号（OAuth 还是 MailAPI URL），而 core 不能反向依赖 services。
`services/mail_imports/import_source.py` 从这里转出，保持原有导入路径可用。

历史：这里曾有 `applemail`（小苹果 / AppleMail，appleemail.top）视图 —— 它
不是 iCloud 邮箱，只是名字带 Apple 的第三方临时邮箱服务，已弃用删除。
旧库里若存过 `mail_provider=applemail`，由 `normalize_mail_import_source`
兜底收敛到 Outlook 视图（见 `_LEGACY_SOURCE_ALIASES`）。
"""

from __future__ import annotations

MAIL_IMPORT_SOURCE_OUTLOOK = "outlook"
MAIL_IMPORT_SOURCE_HOTMAIL = "hotmail"
MAIL_IMPORT_SOURCE_MAILAPI = "mailapi"

MAIL_IMPORT_SOURCES = (
    MAIL_IMPORT_SOURCE_OUTLOOK,
    MAIL_IMPORT_SOURCE_HOTMAIL,
    MAIL_IMPORT_SOURCE_MAILAPI,
)

# 旧版本只存 microsoft/applemail 两个值：microsoft 落到默认视图 Outlook；
# applemail 已弃用，同样收敛到 Outlook —— 老库里那批地址本就存在微软号池里，
# 视图没了不能让用户直接取不到号。这是**故意的读侧兜底**，不是死代码。
_LEGACY_SOURCE_ALIASES = {
    "microsoft": MAIL_IMPORT_SOURCE_OUTLOOK,
    "applemail": MAIL_IMPORT_SOURCE_OUTLOOK,
}

# mail_provider 取这些值时，界面上显示的是"邮箱导入"
MAIL_IMPORT_PROVIDERS = ("microsoft", "outlook")

# 已删除的邮箱渠道 → 现在真正跑起来的 provider。
#
# 为什么要收敛：这些渠道（LuckMail / Laoudo / 各临时邮箱 / 远程 icloud-hme）
# 已整体删除，老库里存过它们的部署在 `create_mailbox()` 会直接抛
# `ValueError: 未知邮箱提供商`。UI 侧曾只在 `GET /api/config` 里收敛，于是
# 出现「界面显示 microsoft、但任务拿到 applemail 炸掉」——失败点离配置页
# 很远，且看不到任何被删渠道的字样。收敛放在这里（core 的纯数据层）让读写
# 两侧共用同一张表。
#
# 全部指向空串而不是某个存活渠道：用户要求「留空，注册任务里必须显式选」，
# 把死值悄悄改写成 Outlook 会让任务在用户不知情的情况下消耗微软号池。
# 空串让上层报出「未配置邮箱服务」，用户自己重选。
#
# 清单必须覆盖**每一个**已删除的渠道名。漏一个的效果是：老库里的值原样透传到
# `create_mailbox()`，报错信息变成一句干巴巴的 `未知邮箱提供商: 'luckmail'`，
# 而配置页上看起来一切正常 —— 排查时根本联想不到是渠道被删了。
_LEGACY_PROVIDER_ALIASES = {
    # 一次性临时邮箱（含第三方服务，与 iCloud 无关的 applemail）
    "aitre": "",
    "applemail": "",
    "cfworker": "",
    "cloudmail": "",
    "duckmail": "",
    "freemail": "",
    "gptmail": "",
    "laoudo": "",
    "luckmail": "",
    "maliapi": "",
    "mailtm": "",
    "moemail": "",
    "opentrashmail": "",
    "skymail": "",
    "tempmail_lol": "",
    # 远程 icloud-hme 平台（本地 iCloud 隐私邮箱是另一个渠道：icloud_local）
    "icloud_hme": "",
    # 早期把 outlook 也当别名收敛到 microsoft；现在 outlook 是真实渠道名，
    # 不再收敛（收敛会把用户的显式选择改成另一个名字，反而绕）
}


def normalize_mail_provider(value: object, *, default: str = "") -> str:
    """把 mail_provider 收敛成当前真实存在的 provider 名。

    `default` 只在值为空时生效（调用方各自的兜底不同：有的想保留空串让上层
    报「未配置」）。
    """
    text = str(value or "").strip().lower()
    if not text:
        return default
    return _LEGACY_PROVIDER_ALIASES.get(text, text)


POOL_ACCOUNT_TYPE_MICROSOFT_OAUTH = "microsoft_oauth"
POOL_ACCOUNT_TYPE_MAILAPI_URL = "mailapi_url"

# 三个微软视图共用一张 outlook_accounts 表，但表里混着两类账号；视图决定该取哪一类
_SOURCE_POOL_ACCOUNT_TYPES = {
    MAIL_IMPORT_SOURCE_OUTLOOK: POOL_ACCOUNT_TYPE_MICROSOFT_OAUTH,
    MAIL_IMPORT_SOURCE_HOTMAIL: POOL_ACCOUNT_TYPE_MICROSOFT_OAUTH,
    MAIL_IMPORT_SOURCE_MAILAPI: POOL_ACCOUNT_TYPE_MAILAPI_URL,
}

POOL_ACCOUNT_TYPE_LABELS = {
    POOL_ACCOUNT_TYPE_MICROSOFT_OAUTH: "Outlook / Hotmail（OAuth）",
    POOL_ACCOUNT_TYPE_MAILAPI_URL: "MailAPI URL",
}


def normalize_mail_import_source(value: object, mail_provider: object = "") -> str:
    """把任意输入收敛成三个合法视图之一，缺值时兜底 Outlook。

    `mail_provider` 参数保留是为了兼容旧调用签名（历史上它用来把
    `mail_provider=applemail` 反推成 applemail 视图，现在已无此视图）。
    """
    text = str(value or "").strip().lower()
    text = _LEGACY_SOURCE_ALIASES.get(text, text)
    if text in MAIL_IMPORT_SOURCES:
        return text
    return MAIL_IMPORT_SOURCE_OUTLOOK


def resolve_mail_provider_from_source(value: object) -> str:
    """视图 → 真正跑起来的邮箱服务。三个视图共用同一个号池。"""
    return "microsoft"


def resolve_pool_account_type(value: object) -> str:
    """视图 → 微软号池里该取哪一类账号，不筛选时返回空串。

    选了 MailAPI URL 却发到手上一个 OAuth 号（或者反过来）等于换了套取码方式，
    注册必然卡在收不到验证码上，所以这里不做互相兜底。没存过视图的老库同样返回空串，
    保持"整池随便取"的旧行为，免得没进过设置页的人突然取不到号。
    """
    text = str(value or "").strip().lower()
    text = _LEGACY_SOURCE_ALIASES.get(text, text)
    return _SOURCE_POOL_ACCOUNT_TYPES.get(text, "")


def describe_pool_account_type(account_type: object) -> str:
    text = str(account_type or "").strip().lower()
    return POOL_ACCOUNT_TYPE_LABELS.get(text, text)


def align_source_with_provider(value: object, mail_provider: object) -> str:
    """收敛成三个合法视图之一（缺值时兜底 Outlook）。

    历史：这里曾按 `mail_provider` 反推视图（provider=applemail → applemail
    视图）。applemail 视图删除后，三个视图都落在微软号池上，provider 不再
    携带视图信息，所以实现已不再使用它 —— 参数保留只为兼容调用签名。
    """
    return normalize_mail_import_source(value, mail_provider)


__all__ = [
    "MAIL_IMPORT_PROVIDERS",
    "MAIL_IMPORT_SOURCES",
    "MAIL_IMPORT_SOURCE_HOTMAIL",
    "MAIL_IMPORT_SOURCE_MAILAPI",
    "MAIL_IMPORT_SOURCE_OUTLOOK",
    "POOL_ACCOUNT_TYPE_LABELS",
    "POOL_ACCOUNT_TYPE_MAILAPI_URL",
    "POOL_ACCOUNT_TYPE_MICROSOFT_OAUTH",
    "align_source_with_provider",
    "describe_pool_account_type",
    "normalize_mail_import_source",
    "normalize_mail_provider",
    "resolve_mail_provider_from_source",
    "resolve_pool_account_type",
]
