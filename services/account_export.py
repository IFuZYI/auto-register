"""账号多格式导出。

一处注册、到处可用：``EXPORT_FIELDS`` 定义能导出哪些字段怎么取值，
``EXPORT_FORMATS`` 用字段拼出一个个格式。加格式只要往 ``EXPORT_FORMATS``
里加一行，接口和前端下拉框自动跟上。

分隔符统一是 ``----``（和市面上的号商格式一致）。**空字段照样占位**：
``a@b.com----pw----`` 这种尾巴不能省，否则按列切的脚本会错位。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

SEPARATOR = "----"


def _extra_of(account: Any) -> dict:
    """账号的 extra；**解密失败必须炸出来**，不许静默变成空。

    `get_extra()` 对脏 JSON 自己就返回 `{}`，所以能从这里抛出来的基本只有
    `CredentialKeyError`（密钥文件被换过 / 丢了 / `CREDENTIAL_ENCRYPTION_KEY`
    写错）。原来那句 `except Exception: return {}` 的注释写的是「脏 JSON 不该让
    整次导出失败」—— 而那个场景根本不经过这里（2026-11-04 独立审查，切片 D）。

    吞掉的后果：密钥坏掉时导出仍 200、格式合法，只是凭证列全空；用户据此做
    迁移，到目标环境才发现拿到的是一批「有邮箱密码但没 AT/2FA」的号。
    """
    from core.secret_box import CredentialKeyError

    getter = getattr(account, "get_extra", None)
    if callable(getter):
        try:
            return getter() or {}
        except CredentialKeyError as exc:
            who = str(
                getattr(account, "email", "") or getattr(account, "id", "") or "?"
            )
            raise CredentialKeyError(
                f"账号 {who} 的凭证无法解密"
                f"（密钥文件或 CREDENTIAL_ENCRYPTION_KEY 是不是变过？）: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 其它异常仍不让整次导出失败
            logger.debug("读账号 extra 失败（按空处理）: %s", exc)
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
    # token 列的兜底按平台镜像规则取：chatgpt 的列镜像 AT（可兜底），
    # grok 的列镜像 SSO（**不能**当 AT 导出 —— 否则 grok 账号的「AT」列
    # 会导出成 SSO cookie）。
    from core.credential_fields import get_credential, token_column_credential

    platform = str(getattr(account, "platform", "") or "")
    return get_credential(extra, "access_token") or token_column_credential(
        account, platform, "access_token"
    )


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
        ExportField("id_token", "IdToken", _extra_key("id_token", "idToken")),
        ExportField("session_token", "SessionToken", _extra_key("session_token", "sessionToken")),
        # grok 账号的主凭证是 SSO —— 导出/导入必须带上，否则换机器导入丢 SSO
        # （整理前实测的真 bug：导出没有 sso 字段，grok 账号往返即丢）。
        ExportField("sso", "SSO", _extra_key("sso", "sso_token")),
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
    # 凭证字段从注册表展开（`export_names()`）—— 手写清单会与注册表漂移
    # （整理前正是手写清单，sso 缺失导致 grok 账号往返丢字段）。
    from core.credential_fields import export_names

    rows = []
    for account in accounts:
        extra = _extra_of(account)
        rows.append(
            {
                # `export_names()` 里可能含**只在 extra 里**的键（如 `cookies`，
                # 见 `credential_fields.EXPORT_ONLY_EXTRA_KEYS`）—— 它们没有
                # `EXPORT_FIELDS` 读取器，直接从 extra 取值。
                field_id: (
                    EXPORT_FIELDS[field_id].read(account, extra)
                    if field_id in EXPORT_FIELDS
                    else str(extra.get(field_id) or "")
                )
                for field_id in (
                    "platform",
                    "email",
                    "password",
                    "totp_secret",
                    *export_names(),
                    # 2026-11-04 独立审查（切片 E，L3）：这三个字段此前**不导出**，
                    # 而 `api/accounts.py` 的 JSON 导入端专门有
                    # `for column in ("user_id", "region", "status", "cashier_url")`
                    # 的分支 —— 导入端准备接收、导出端从来不写，那些分支对本仓库
                    # 自己的格式是**死的**。结果：往返后 user_id/region/cashier_url
                    # 全变空串，`created: 1` 且无任何提示。
                    "user_id",
                    "region",
                    "cashier_url",
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


def formats_by_column_count(count: int) -> list["ExportFormat"]:
    """列数对应的导出格式（**可能多个** —— 调用方必须处理歧义）。"""
    return [f for f in _FORMAT_LIST if len(f.columns) == count]


def parse_delimited_line(line: str, *, source_format: str = "") -> dict:
    """把一行 `----` 分隔的文本解析成字段字典。

    **列 → 字段的映射取自导出注册表**（`ExportFormat.columns`）—— 两边同一份
    定义，不会漂移。

    `source_format` 给定时按它解析；不给时按列数自动识别，**列数对应多个格式
    就抛错** —— 不猜：猜错会把 AT 写进 `refresh_token`，那是静默的错误数据，
    比报错严重得多。
    """
    parts = [p.strip() for p in str(line or "").split(SEPARATOR)]
    if len(parts) < 2:
        raise ValueError("不是合法的分隔行（列数不足）")

    if source_format:
        fmt = EXPORT_FORMATS.get(str(source_format).strip())
        if fmt is None:
            raise ValueError(f"未知的导入格式: {source_format}")
        if len(fmt.columns) != len(parts):
            raise ValueError(
                f"行有 {len(parts)} 列，与格式 {source_format} 的 "
                f"{len(fmt.columns)} 列不符"
            )
    else:
        candidates = formats_by_column_count(len(parts))
        if len(candidates) > 1:
            names = "、".join(f.id for f in candidates)
            raise ValueError(
                f"{len(parts)} 列对应多个格式（{names}）—— 请在导入时指定来源格式"
            )
        if not candidates:
            raise ValueError(f"没有 {len(parts)} 列的导出格式")
        fmt = candidates[0]

    return {name: value for name, value in zip(fmt.columns, parts)}


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
