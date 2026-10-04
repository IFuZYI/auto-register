"""账号多格式导出。

一处注册、到处可用：``EXPORT_FIELDS`` 定义能导出哪些字段怎么取值，
``EXPORT_FORMATS`` 用字段拼出一个个格式。加格式只要往 ``EXPORT_FORMATS``
里加一行，接口和前端下拉框自动跟上。

分隔符统一是 ``----``（和市面上的号商格式一致）。**空字段照样占位**：
``a@b.com----pw----`` 这种尾巴不能省，否则按列切的脚本会错位。
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

SEPARATOR = "----"


def _extra_of(account: Any) -> dict:
    getter = getattr(account, "get_extra", None)
    if callable(getter):
        try:
            return getter() or {}
        except Exception:  # noqa: BLE001 - 脏 JSON 不该让整次导出失败
            return {}
    raw = getattr(account, "extra_json", None)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    extra = getattr(account, "extra", None)
    return extra if isinstance(extra, dict) else {}


def _text(value: Any) -> str:
    if value is None or value is False:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _attr(name: str) -> Callable[[Any, dict], str]:
    return lambda account, _extra: _text(getattr(account, name, ""))


def _extra_key(*names: str) -> Callable[[Any, dict], str]:
    def read(_account: Any, extra: dict) -> str:
        for name in names:
            value = extra.get(name)
            if _text(value):
                return _text(value)
        return ""

    return read


def _access_token(account: Any, extra: dict) -> str:
    return _text(extra.get("access_token")) or _text(getattr(account, "token", ""))


@dataclass(frozen=True)
class ExportField:
    id: str
    label: str
    read: Callable[[Any, dict], str]


EXPORT_FIELDS: dict[str, ExportField] = {
    f.id: f
    for f in (
        ExportField("platform", "平台", _attr("platform")),
        ExportField("email", "邮箱", _attr("email")),
        ExportField("password", "密码", _attr("password")),
        ExportField("totp_secret", "2FA 密钥", _extra_key("totp_secret")),
        ExportField("access_token", "AccessToken", _access_token),
        ExportField("refresh_token", "RefreshToken", _extra_key("refresh_token", "refreshToken")),
        ExportField("id_token", "IdToken", _extra_key("id_token")),
        ExportField("session_token", "SessionToken", _extra_key("session_token")),
        ExportField("phone_number", "手机号", _extra_key("phone_number")),
        ExportField("bound_email", "绑定邮箱", _extra_key("bound_email")),
        ExportField("user_id", "UID", _attr("user_id")),
        ExportField("status", "状态", _attr("status")),
        ExportField("region", "地区", _attr("region")),
        ExportField("cashier_url", "试用链接", _attr("cashier_url")),
        ExportField("created_at", "注册时间", _attr("created_at")),
    )
}


@dataclass(frozen=True)
class ExportFormat:
    id: str
    label: str
    description: str
    columns: tuple[str, ...] = ()
    extension: str = "txt"
    # 单列格式（一行一个 token）里空行没有任何意义，直接把这一行丢掉；
    # 多列格式相反，空字段必须留着占位。
    skip_empty_rows: bool = False
    renderer: Optional[Callable[[Sequence[Any]], str]] = field(default=None, compare=False)

    @property
    def sample(self) -> str:
        if self.renderer is not None or not self.columns:
            return ""
        return SEPARATOR.join(EXPORT_FIELDS[c].label for c in self.columns)


def _render_csv(accounts: Sequence[Any]) -> str:
    columns = (
        "platform",
        "email",
        "password",
        "status",
        "region",
        "user_id",
        "totp_secret",
        "access_token",
        "refresh_token",
        "cashier_url",
        "created_at",
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for account in accounts:
        extra = _extra_of(account)
        writer.writerow([EXPORT_FIELDS[c].read(account, extra) for c in columns])
    return buffer.getvalue().rstrip("\n")


def _render_json(accounts: Sequence[Any]) -> str:
    rows = []
    for account in accounts:
        extra = _extra_of(account)
        rows.append(
            {
                field_id: EXPORT_FIELDS[field_id].read(account, extra)
                for field_id in (
                    "platform",
                    "email",
                    "password",
                    "totp_secret",
                    "access_token",
                    "refresh_token",
                    "id_token",
                    "session_token",
                    "status",
                    "created_at",
                )
            }
        )
    return json.dumps(rows, ensure_ascii=False, indent=2)


_FORMAT_LIST: tuple[ExportFormat, ...] = (
    ExportFormat(
        id="email_pw",
        label="邮箱----密码",
        description="最常见的账密两列",
        columns=("email", "password"),
    ),
    ExportFormat(
        id="email_pw_2fa",
        label="邮箱----密码----2FA",
        description="带 TOTP 密钥，可直接导入验证器",
        columns=("email", "password", "totp_secret"),
    ),
    ExportFormat(
        id="email_pw_2fa_at",
        label="邮箱----密码----2FA----AT",
        description="再带上 access_token",
        columns=("email", "password", "totp_secret", "access_token"),
    ),
    ExportFormat(
        id="email_pw_2fa_rt",
        label="邮箱----密码----2FA----RT",
        description="再带上 refresh_token，喂给中转服务用",
        columns=("email", "password", "totp_secret", "refresh_token"),
    ),
    ExportFormat(
        id="email_pw_2fa_at_rt",
        label="邮箱----密码----2FA----AT----RT",
        description="一行带齐登录与调用所需的全部凭证",
        columns=("email", "password", "totp_secret", "access_token", "refresh_token"),
    ),
    ExportFormat(
        id="email_pw_2fa_phone",
        label="邮箱----密码----2FA----手机号",
        description="手机号注册的号带上号码，没有号码则留空占位",
        columns=("email", "password", "totp_secret", "phone_number"),
    ),
    ExportFormat(
        id="email_pw_rt",
        label="邮箱----密码----RT",
        description="不需要 2FA 列时的精简版",
        columns=("email", "password", "refresh_token"),
    ),
    ExportFormat(
        id="email_2fa",
        label="邮箱----2FA",
        description="只补验证器，不带密码",
        columns=("email", "totp_secret"),
    ),
    ExportFormat(
        id="at",
        label="AccessToken（一行一个）",
        description="没有 AT 的账号自动跳过",
        columns=("access_token",),
        skip_empty_rows=True,
    ),
    ExportFormat(
        id="rt",
        label="RefreshToken（一行一个）",
        description="没有 RT 的账号自动跳过",
        columns=("refresh_token",),
        skip_empty_rows=True,
    ),
    ExportFormat(
        id="totp",
        label="2FA 密钥（一行一个）",
        description="没绑 2FA 的账号自动跳过",
        columns=("totp_secret",),
        skip_empty_rows=True,
    ),
    ExportFormat(
        id="csv",
        label="CSV（全字段表格）",
        description="带表头，Excel 可直接打开",
        extension="csv",
        renderer=_render_csv,
    ),
    ExportFormat(
        id="json",
        label="JSON（全字段）",
        description="给脚本二次处理用",
        extension="json",
        renderer=_render_json,
    ),
)

EXPORT_FORMATS: dict[str, ExportFormat] = {f.id: f for f in _FORMAT_LIST}
DEFAULT_EXPORT_FORMAT = "email_pw_2fa"


def list_export_formats() -> list[dict]:
    """给前端下拉框用的元信息。"""
    return [
        {
            "id": f.id,
            "label": f.label,
            "description": f.description,
            "extension": f.extension,
            "columns": [EXPORT_FIELDS[c].label for c in f.columns],
            "sample": f.sample,
        }
        for f in _FORMAT_LIST
    ]


def resolve_export_format(format_id: str) -> ExportFormat:
    fmt = EXPORT_FORMATS.get(str(format_id or "").strip())
    if fmt is None:
        raise KeyError(format_id)
    return fmt


def render_account_row(account: Any, fmt: ExportFormat) -> Optional[str]:
    """单个账号渲染成一行，``None`` 表示这一行该被丢掉。"""
    extra = _extra_of(account)
    values = [EXPORT_FIELDS[c].read(account, extra) for c in fmt.columns]
    if fmt.skip_empty_rows and not any(values):
        return None
    return SEPARATOR.join(values)


def render_accounts(accounts: Sequence[Any], format_id: str) -> str:
    fmt = resolve_export_format(format_id)
    if fmt.renderer is not None:
        return fmt.renderer(accounts)
    lines = []
    for account in accounts:
        line = render_account_row(account, fmt)
        if line is not None:
            lines.append(line)
    return "\n".join(lines)


def export_filename(platform: str, format_id: str) -> str:
    fmt = resolve_export_format(format_id)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{platform or 'accounts'}_{fmt.id}_{stamp}.{fmt.extension}"
