"""配置键的**唯一定义处**。

以前同一批键写在三个地方：`api/config.py` 的 `CONFIG_KEYS`（69 个字符串）、
同一个文件的 `SECRET_CONFIG_KEYS`（10 个）、以及前端设置页的表单字段。三份
名单没有任何机制保证一致 —— 加一个键忘了加进白名单，前端能填、后端静默丢弃；
加进口令集合忘了加进 `CONFIG_KEYS`，写都写不进去。

现在键的身份（名字、分组、是否口令）只在这里定义，`api/config.py` 从它派生：

    CONFIG_KEYS        = 全部键（顺序 = 界面顺序）
    SECRET_CONFIG_KEYS = 其中 secret=True 的

**默认值不在这里**：默认值是调用点的业务决定（`config_store.get(key, "browser")`
里的 `"browser"` 是该功能自己的默认），搬到这里会让「改一个功能的默认值」变成
改全局配置表。这里只管**身份**，不管**取值**。

`GET /api/config/schema` 把这张表原样吐给前端（不含任何值），前端可以据此渲染
表单，不必再维护第三份键名列表。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigKey:
    """一个配置键的身份。"""

    key: str
    #: 界面分组（前端按这个把字段归类；也用于 `GET /config/schema` 的聚合）
    group: str
    #: 口令类：`GET /api/config` 不回明文，只回 `<key>_set` 布尔
    secret: bool = False


#: 全部配置键。**顺序即界面顺序**（与改造前的 `CONFIG_KEYS` 完全一致）。
CONFIG_SCHEMA: tuple[ConfigKey, ...] = (
    # ---- 验证码 ----
    ConfigKey("yescaptcha_key", "captcha", secret=True),
    ConfigKey("twocaptcha_key", "captcha", secret=True),
    # 顺序与改造前的 `CONFIG_KEYS` 完全一致（界面按这个顺序渲染），
    # 所以 `default_executor` 排在 `default_captcha_solver` 前面 ——
    # 尽管分组不同。
    ConfigKey("default_executor", "register"),
    ConfigKey("default_captcha_solver", "captcha"),
    ConfigKey("register_retry_times", "register"),
    ConfigKey("register_count", "register"),
    ConfigKey("register_concurrency", "register"),
    ConfigKey("register_delay_seconds", "register"),
    ConfigKey("chatgpt_executor", "register"),
    ConfigKey("grok_executor", "register"),
    # ---- 邮箱 ----
    ConfigKey("mail_provider", "mail"),
    ConfigKey("mail_import_source", "mail"),
    ConfigKey("outlook_backend", "mail"),
    ConfigKey("mailbox_otp_timeout_seconds", "mail"),
    # ---- iCloud 本地/远端账号标注 ----
    ConfigKey("icloud_local_account_id", "icloud"),
    ConfigKey("icloud_local_label", "icloud"),
    ConfigKey("icloud_local_note", "icloud"),
    ConfigKey("icloud_remote_account_id", "icloud"),
    ConfigKey("icloud_remote_label", "icloud"),
    ConfigKey("icloud_remote_note", "icloud"),
    # ---- CPA ----
    ConfigKey("cpa_enabled", "cpa"),
    ConfigKey("cpa_api_url", "cpa"),
    ConfigKey("cpa_api_key", "cpa", secret=True),
    ConfigKey("cpa_upload_chatgpt_enabled", "cpa"),
    ConfigKey("cpa_upload_proxy_enabled", "cpa"),
    ConfigKey("chatgpt2api_upload_proxy_enabled", "cpa"),
    ConfigKey("cpa_auto_sync_enabled", "cpa"),
    # ---- Sub2API ----
    ConfigKey("sub2api_enabled", "sub2api"),
    ConfigKey("sub2api_api_url", "sub2api"),
    ConfigKey("sub2api_api_key", "sub2api", secret=True),
    ConfigKey("sub2api_group_ids", "sub2api"),
    ConfigKey("sub2api_upload_proxy_enabled", "sub2api"),
    ConfigKey("sub2api_auto_sync_enabled", "sub2api"),
    # ---- chatgpt2api ----
    ConfigKey("chatgpt2api_enabled", "chatgpt2api"),
    ConfigKey("chatgpt2api_api_url", "chatgpt2api"),
    ConfigKey("chatgpt2api_api_key", "chatgpt2api", secret=True),
    #: 导入内容：`atrt` = 只推 AT/RT；`full` = 完全账户（邮箱 + 密码 + AT/RT + 2FA 密钥）。
    #: 对端 ≥ 2.2.0 支持 `password` / `totp_secret`，带上才能「RT 失效后自动密码重登」；
    #: 但有人只想给号池喂可用凭证、不交出密码 —— 所以做成显式选择，默认 `full`
    #: （与加这个开关之前的行为一致，升级不改变既有部署）。
    ConfigKey("chatgpt2api_import_mode", "chatgpt2api"),
    ConfigKey("chatgpt2api_auto_sync_enabled", "chatgpt2api"),
    # ---- CLIProxyAPI ----
    ConfigKey("cliproxyapi_base_url", "cliproxyapi"),
    ConfigKey("cliproxyapi_management_key", "cliproxyapi", secret=True),
    # ---- 代理池 ----
    #: 绑定均衡：给账号挑绑定代理时选「当前挂得最少」的那个。
    #: 关掉则随机挑（用户要求「均衡配置」可开关）。
    ConfigKey("proxy_balance_enabled", "proxy"),
    #: 单个代理最多绑多少个账号；0 = 不限。
    #: 超过上限的代理不再接收新绑定（全部超限时忽略上限，避免无处可绑）。
    ConfigKey("proxy_balance_max_per_proxy", "proxy"),
    # ---- grok2api ----
    ConfigKey("grok2api_base_url", "grok2api"),
    ConfigKey("grok2api_username", "grok2api"),
    ConfigKey("grok2api_password", "grok2api", secret=True),
    ConfigKey("grok2api_enabled", "grok2api"),
    ConfigKey("grok2api_upload_proxy_enabled", "grok2api"),
    ConfigKey("grok2api_auto_sync_enabled", "grok2api"),
    # ---- ChatGPT 注册 ----
    ConfigKey("grok_register_mode", "grok"),
    ConfigKey("grok_send_code_min_interval", "grok"),
    ConfigKey("grok_oauth_exchange", "grok"),
    ConfigKey("chatgpt_registration_mode", "chatgpt"),
    ConfigKey("chatgpt_rt_route", "chatgpt"),
    ConfigKey("chatgpt_register_flow", "chatgpt"),
    # ---- 接码 ----
    ConfigKey("sms_enabled", "sms"),
    ConfigKey("sms_provider", "sms"),
    ConfigKey("sms_api_key", "sms", secret=True),
    ConfigKey("sms_service", "sms"),
    ConfigKey("sms_country", "sms"),
    ConfigKey("sms_auto_country", "sms"),
    ConfigKey("sms_allowed_countries", "sms"),
    ConfigKey("sms_auto_min_stock", "sms"),
    ConfigKey("sms_auto_max_price", "sms"),
    ConfigKey("sms_max_price", "sms"),
    ConfigKey("sms_fixed_price", "sms"),
    ConfigKey("sms_reuse_phone", "sms"),
    ConfigKey("sms_phone_success_max", "sms"),
    ConfigKey("sms_per_phone_timeout", "sms"),
    ConfigKey("sms_max_phone_attempts", "sms"),
    ConfigKey("sms_code_retries_per_phone", "sms"),
    # ---- 自动维护（本地定时任务；与「面板同步」是两件事）----
    #
    # 命名约定：`<作用域>_<动作>_<参数>`，作用域是 `log` / `chatgpt` / `grok`。
    #
    # 间隔一律 `<数字><单位>`：`30m` / `6h` / `1d`。用户 2026-10-31 明确要的是
    # 「执行间隔」而不是「执行时间」—— 凌晨三点跑是**一天一次**的特例，而
    # 「每 6 小时」这种粒度用固定时刻表达不了。调度走 `interval_seconds`。
    #
    # 日志自动清理：只按**保留天数**删，不提供「全清」——一个每天定点执行的
    # 删除动作不该有「抹掉全部」这种档位。
    ConfigKey("log_cleanup_enabled", "maintenance"),
    ConfigKey("log_cleanup_interval", "maintenance"),
    ConfigKey("log_cleanup_keep_days", "maintenance"),
    # ChatGPT 本地维护
    ConfigKey("chatgpt_auto_refresh_enabled", "maintenance"),
    ConfigKey("chatgpt_refresh_scope", "maintenance"),
    ConfigKey("chatgpt_refresh_random_count", "maintenance"),
    ConfigKey("chatgpt_refresh_interval", "maintenance"),
    ConfigKey("chatgpt_refresh_disable_banned", "maintenance"),
    ConfigKey("chatgpt_auto_probe_enabled", "maintenance"),
    ConfigKey("chatgpt_probe_mode", "maintenance"),
    ConfigKey("chatgpt_probe_sample_percent", "maintenance"),
    ConfigKey("chatgpt_probe_strategy", "maintenance"),
    ConfigKey("chatgpt_probe_interval", "maintenance"),
    ConfigKey("chatgpt_sync_strategy", "maintenance"),
    # Grok 本地维护（同构）
    ConfigKey("grok_auto_refresh_enabled", "maintenance"),
    ConfigKey("grok_refresh_scope", "maintenance"),
    ConfigKey("grok_refresh_random_count", "maintenance"),
    ConfigKey("grok_refresh_interval", "maintenance"),
    ConfigKey("grok_refresh_disable_banned", "maintenance"),
    ConfigKey("grok_auto_probe_enabled", "maintenance"),
    ConfigKey("grok_probe_mode", "maintenance"),
    ConfigKey("grok_probe_sample_percent", "maintenance"),
    ConfigKey("grok_probe_strategy", "maintenance"),
    ConfigKey("grok_probe_interval", "maintenance"),
    ConfigKey("grok_sync_strategy", "maintenance"),
    # ---- 贡献模式 ----
    ConfigKey("contribution_enabled", "contribution"),
    ConfigKey("contribution_server_url", "contribution"),
    ConfigKey("contribution_key", "contribution", secret=True),
    ConfigKey("contribution_mode", "contribution"),
    ConfigKey("custom_contribution_url", "contribution"),
    ConfigKey("custom_contribution_token", "contribution", secret=True),
)

#: 全部键名（顺序 = 界面顺序）。
CONFIG_KEYS: tuple[str, ...] = tuple(item.key for item in CONFIG_SCHEMA)

#: 口令类键。`GET /api/config` 对它们只回 `<key>_set` 布尔，不回明文。
SECRET_CONFIG_KEYS: frozenset[str] = frozenset(
    item.key for item in CONFIG_SCHEMA if item.secret
)

#: 键名 → 身份。
BY_KEY: dict[str, ConfigKey] = {item.key: item for item in CONFIG_SCHEMA}

#: 允许存在、但**不对外暴露**的内部键：只被后端自己读写，不该出现在设置页，
#: 也不该被 `PUT /api/config` 接受。
#:
#: `rt_marker`：补 RT 任务的幂等标记（`services/chatgpt_maintenance.py` 读它判断
#: 「这一轮是否已经补过」）。它不是用户配置，进白名单反而会被误改。
INTERNAL_KEYS: frozenset[str] = frozenset({"rt_marker"})


def is_known(key: str) -> bool:
    """这个键是否被登记过（含内部键）。"""
    return key in BY_KEY or key in INTERNAL_KEYS


def is_secret(key: str) -> bool:
    return key in SECRET_CONFIG_KEYS


def group_of(key: str) -> str:
    item = BY_KEY.get(key)
    return item.group if item else ""


def keys_in_group(group: str) -> tuple[str, ...]:
    return tuple(item.key for item in CONFIG_SCHEMA if item.group == group)


def schema_payload() -> dict:
    """`GET /api/config/schema` 的响应体。**不含任何值**，只有键的身份。"""
    groups: dict[str, list[dict]] = {}
    for item in CONFIG_SCHEMA:
        groups.setdefault(item.group, []).append(
            {"key": item.key, "secret": item.secret}
        )
    return {
        "keys": [item.key for item in CONFIG_SCHEMA],
        "secrets": sorted(SECRET_CONFIG_KEYS),
        "groups": groups,
    }


__all__ = [
    "BY_KEY",
    "CONFIG_KEYS",
    "CONFIG_SCHEMA",
    "ConfigKey",
    "INTERNAL_KEYS",
    "SECRET_CONFIG_KEYS",
    "group_of",
    "is_known",
    "is_secret",
    "keys_in_group",
    "schema_payload",
]
