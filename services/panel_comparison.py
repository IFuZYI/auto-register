"""本地账号 ↔ 远程面板的对比。

面板管理页选中一个面板后展示：本地有哪些账号、远端有哪些账号、哪些本地号
还没上传、**凭证是否同步**。核心判定有两层：

1. **凭证是否相同（主判据）** —— 用户要求：「本地和远端同步是判断 AT 这些是否
   相同，AT、RT 这种全相同就是同步」。所以两边都有账号时，比的是
   `access_token` / `refresh_token` / `session_token` 这些**凭证本体**：
   全相同 → `synced`（已同步）；有任一不同 → `credential_diff`（凭证不同）。
2. **谁更新（辅助信息）** —— 凭证不同时，再用时间戳说清是哪边动的（按小时比，
   分秒是噪声）。时间只作展示与排序，不再单独决定「同步」与否。

- 两边的时间戳格式不统一（远端 `last_refresh` 是 `+08:00` 带偏移的字符串，
  本地 `updated_at` 是 UTC datetime），必须先归一成 epoch 再比。
- 远端没有账号 = 没上传；本地有远端也有 = 已上传，再看凭证是否一致。
- 远端有本地没有 = 远端多出来的（别人传的、或本地删过），单独一档。
- 凭证比不了（远端接口不返回 AT/RT）→ `unknown_credential`，说清是比不了
  而不是「一致」—— 把「读不到」当成「相同」会给出错误的安全感。

结果带缓存：远端接口是外网调用，面板管理页每次渲染都打一遍既慢又容易被限流。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: 对比结论
STATE_LOCAL_ONLY = "local_only"          # 本地有、远端没有 → 未上传
STATE_REMOTE_ONLY = "remote_only"        # 远端有、本地没有 → 远端多余
STATE_SYNCED = "synced"                  # 两边都有，**凭证全相同** → 已同步
STATE_CREDENTIAL_DIFF = "credential_diff"  # 两边都有，凭证有差异 → 未同步
STATE_UNKNOWN_CREDENTIAL = "unknown_credential"  # 两边都有，但凭证比不了
STATE_UNKNOWN_TIME = "unknown_time"      # 两边都有，但至少一边没有时间可判

STATE_LABELS = {
    STATE_LOCAL_ONLY: "未上传",
    STATE_REMOTE_ONLY: "仅远端",
    STATE_SYNCED: "已同步",
    STATE_CREDENTIAL_DIFF: "凭证不同",
    STATE_UNKNOWN_CREDENTIAL: "无法比对",
    STATE_UNKNOWN_TIME: "无法比较",
}

#: 参与「是否同步」判定的凭证字段。
#:
#: 用户口径是「AT、RT 这种全相同就是同步」—— 列出的这些**全部**相同才算同步，
#: 任何一个不同就是未同步。`session_token` 也列进来是因为 ChatGPT 侧本地存的是
#: 它（CPA 侧上传的是 AT/RT），而 Grok 侧本地是 `sso`。
#:
#: 每个字段可以有多个键名（`access_token` / `accessToken`）—— 落库路径会写
#: camelCase，只认蛇形会把有值的账号当成"没有凭证"。
CREDENTIAL_FIELDS: tuple[tuple[str, ...], ...] = (
    ("access_token", "accessToken"),
    ("refresh_token", "refreshToken"),
    ("session_token", "sessionToken"),
    ("id_token", "idToken"),
    ("sso", "sso_token"),
)


def parse_timestamp(value: Any) -> Optional[datetime]:
    """把两边的各种时间写法解析成带时区的 datetime。

    认得的形态（都是实测见过的）：
    - ISO 8601 带偏移：`2026-03-31T12:00:00+08:00`
    - ISO 8601 带 Z：`2026-03-31T04:00:00Z`
    - 不带时区的 ISO：按 UTC 处理（本应用自己写的时间都是 UTC）
    - epoch 秒 / 毫秒（数字或数字字符串）
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _from_epoch(float(text))
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _from_epoch(seconds: float) -> Optional[datetime]:
    # 毫秒级 epoch（13 位）与秒级（10 位）都认；超过这个量级当毫秒处理
    if seconds > 1e11:
        seconds = seconds / 1000.0
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def compare_credentials(local: dict[str, Any], remote: dict[str, Any]) -> tuple[str, list[str]]:
    """按凭证本体判定是否同步（用户口径：AT/RT 全相同就是同步）。

    返回 `(结论, 有差异的字段名列表)`。结论取：

    - `synced` —— 所有**两边都有值**的凭证字段逐一相同。
    - `credential_diff` —— 至少一个字段两边都有值但不同。
    - `unknown_credential` —— 没有任何一个字段能比（两边都没凭证，
      或远端接口不返回凭证）。**不能当成 synced** —— 把"读不到"当"相同"
      会给出错误的安全感，用户会以为已经同步了。

    只比**两边都有值**的字段：远端不返回 refresh_token（CPA 常见）时，
    本地有值不算"不同"—— 那说明不了任何事，硬报差异是误报。
    但两边都有值就必须一致，这就是用户说的「AT、RT 这种全相同」。
    """
    local_extra = local if isinstance(local, dict) else {}
    remote_extra = remote if isinstance(remote, dict) else {}
    comparable = _comparable_fields(local_extra, remote_extra)
    changed = [name for name, left, right in comparable if left != right]
    if changed:
        return STATE_CREDENTIAL_DIFF, changed
    if not comparable:
        return STATE_UNKNOWN_CREDENTIAL, []
    return STATE_SYNCED, []


def _first_present(extra: dict[str, Any], aliases: tuple[str, ...]) -> str:
    """按别名顺序取第一个非空值（蛇形优先，兼容 camelCase 落库）。"""
    for name in aliases:
        value = str(extra.get(name) or "").strip()
        if value:
            return value
    return ""


def _count_compared(local: dict[str, Any], remote: dict[str, Any]) -> int:
    """实际比了几个凭证字段（两边都有值的才算）。

    与 `compare_credentials` 共用同一套「存在」判定（`_comparable_fields`）——
    各写一份的话，`CREDENTIAL_FIELDS` 增删时两处会走偏：界面上说「比了 2 个
    字段」，实际可能只比了 1 个。
    """
    return len(_comparable_fields(local, remote))


def _comparable_fields(local: dict[str, Any], remote: dict[str, Any]) -> list[tuple[str, str, str]]:
    """两边都有值的凭证字段：`[(规范名, 本地值, 远端值)]`。

    「有值」= 该字段的任一别名非空（`_first_present`）。这是
    `compare_credentials` 与 `_count_compared` 的共同口径。
    """
    local_extra = local if isinstance(local, dict) else {}
    remote_extra = remote if isinstance(remote, dict) else {}
    pairs: list[tuple[str, str, str]] = []
    for aliases in CREDENTIAL_FIELDS:
        left = _first_present(local_extra, aliases)
        right = _first_present(remote_extra, aliases)
        if left and right:
            pairs.append((aliases[0], left, right))
    return pairs


def compare_by_hour(local_time: Optional[datetime], remote_time: Optional[datetime]) -> str:
    """按**小时**粒度比先后（用户要求：不管分秒）。

    返回 `local_newer` / `remote_newer` / `time_synced` / `unknown_time`。
    同一小时内 → `time_synced`：两边各动过一次的话，分秒差异是噪声，报谁新都是误导。

    这个结论现在只作**辅助信息**（凭证不同时用来说明是哪边动的）与排序用；
    「是否同步」由 `compare_credentials` 决定。
    """
    if local_time is None or remote_time is None:
        return STATE_UNKNOWN_TIME
    local_hour = local_time.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    remote_hour = remote_time.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    if local_hour == remote_hour:
        return "time_synced"
    return "local_newer" if local_hour > remote_hour else "remote_newer"


def hour_bucket(value: Optional[datetime]) -> str:
    """时间的小时档位（`2026-03-31T04:00Z`），给前端展示"以小时为单位"的对比。"""
    if value is None:
        return ""
    return value.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    ).strftime("%Y-%m-%dT%H:00Z")


@dataclass
class RemoteAccount:
    """远端面板上的一个账号（各面板字段不同，这里只留对比用得上的）。"""

    email: str
    #: 这个远端账号属于哪个平台（`chatgpt` / `grok`）。
    #:
    #: CPA 面板同时托管两类凭据（codex / xai），而**同一个邮箱可能两边都有**
    #: （本项目的邮箱池按平台消耗：一个地址能注册 ChatGPT 也能注册 Grok）。
    #: 只按邮箱匹配会把两条不同的账号压成一行，所以匹配键是 (平台, 邮箱)。
    #:
    #: **fetcher 必须给行打上平台**（本地行永远带平台，见
    #: `_local_accounts_for_panel`）：留空的话键退化成纯邮箱，与本地行对不上，
    #: 整个面板会显示成「全部未上传 + 全部仅远端」—— 看起来像功能坏了。
    #: grok2api 面板实测踩过：22 个本地账号全部显示未上传，而两边邮箱 100% 重合。
    platform: str = ""
    #: 远端记录的最后更新时间（已解析）
    updated_at: Optional[datetime] = None
    #: 远端原始时间字符串，展示用
    updated_at_raw: str = ""
    #: 远端状态（active / disabled / error ...），展示用
    status: str = ""
    #: 远端标识（auth_index / id / name），展示与操作时定位用
    remote_id: str = ""
    #: 远端账号的**凭证**（AT / RT / session / id_token / sso）。
    #:
    #: 单独一个字段而不是塞进 `extra`：这是「是否同步」的判据，语义上与展示用的
    #: 元信息（套餐、状态）不同类。列表接口通常**不返回**它 —— 只有能取到凭证的
    #: fetcher 才填（如 CPA 的 download 端点），取不到就留空 dict，比对时会判
    #: `unknown_credential` 而不是误报「一致」。
    credentials: dict[str, Any] = field(default_factory=dict)
    #: 其它要展示的字段（套餐、订阅到期等），面板相关
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ComparisonRow:
    """一行对比结果：一个 (平台, 邮箱) 在本地与远端的对照。"""

    email: str
    state: str
    #: 这一行属于哪个平台（`chatgpt` / `grok`）。CPA 面板同时托管两类凭据，
    #: 前端按它把行分组，批量动作也按它分发到对应平台的接口。
    platform: str = ""
    #: 本地侧
    local_id: Optional[int] = None
    local_status: str = ""
    local_updated_at: Optional[datetime] = None
    local_updated_hour: str = ""
    local_has_refresh_token: bool = False
    #: 远端侧
    remote_id: str = ""
    remote_status: str = ""
    remote_updated_at: Optional[datetime] = None
    #: 远端原始时间字符串（远端写法可能带 +08:00 之类的偏移，回显原样）
    remote_updated_at_raw: str = ""
    remote_updated_hour: str = ""
    remote_extra: dict[str, Any] = field(default_factory=dict)
    #: 两边都有但内容不一致的字段名（值对比）
    differences: list[str] = field(default_factory=list)
    #: 凭证比对的差异字段（AT / RT / session 等）—— 「是否同步」的判据
    credential_differences: list[str] = field(default_factory=list)
    #: 凭证实际比了几个字段（0 = 比不了，见 STATE_UNKNOWN_CREDENTIAL）
    credential_compared: int = 0
    #: 谁更新（按小时，辅助信息）：local_newer / remote_newer / time_synced / ""
    time_relation: str = ""

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "platform": self.platform,
            "state": self.state,
            "label": self.label,
            "local_id": self.local_id,
            "local_status": self.local_status,
            "local_updated_at": _iso(self.local_updated_at),
            "local_updated_hour": self.local_updated_hour,
            "local_has_refresh_token": self.local_has_refresh_token,
            "remote_id": self.remote_id,
            "remote_status": self.remote_status,
            # 归一成 UTC 再给前端：远端原始串是 +08:00 的，直接摆出来会跟本地
            # 的 UTC 时间看着差 8 小时 —— 而"谁更新"正是这一列要回答的问题。
            "remote_updated_at": _iso(self.remote_updated_at),
            # 原始串留一份（tooltip 里显示远端到底是怎么写的）
            "remote_updated_at_raw": self.remote_updated_at_raw,
            "remote_updated_hour": self.remote_updated_hour,
            "remote_extra": self.remote_extra,
            "differences": self.differences,
            "credential_differences": self.credential_differences,
            "credential_compared": self.credential_compared,
            "time_relation": self.time_relation,
        }


def _iso(value: Optional[datetime]) -> str:
    return value.astimezone(timezone.utc).isoformat() if value else ""


#: 内容对比时逐项比对的字段（本地 extra ↔ 远端记录）。
#: 只列**两边语义相同**的：套餐、订阅到期这类。凭证本身（AT/RT）不逐字节比 ——
#: 远端每次刷新都会换，比出来永远是"不一致"，没有信息量。
_CONTENT_FIELDS = ("plan_type", "chatgpt_subscription_active_until")


def diff_fields(local_extra: dict[str, Any], remote: RemoteAccount) -> list[str]:
    """两边都有、但值不一样的字段名。"""
    local = local_extra if isinstance(local_extra, dict) else {}
    remote_extra = remote.extra if isinstance(remote.extra, dict) else {}
    changed: list[str] = []
    for name in _CONTENT_FIELDS:
        left = str(local.get(name) or "").strip()
        right = str(remote_extra.get(name) or "").strip()
        if left and right and left != right:
            changed.append(name)
    return changed


def _refresh_token_from_extra(extra: dict[str, Any]) -> str:
    """账号 extra 里的 refresh_token。

    两个键都认（`refresh_token` / `refreshToken`）：通用结果落库路径
    （`api/actions.py` 的 tracked_keys）会原样写 camelCase，只读蛇形的话这些
    账号在对比面板里会被标成「无RT」—— 恰好是这一栏要审的那个凭证。
    口径与 `services.chatgpt_rt_backfill.account_refresh_token` 一致。
    """
    if not isinstance(extra, dict):
        return ""
    return str(extra.get("refresh_token") or extra.get("refreshToken") or "").strip()


def _match_key(platform: str, email: str) -> str:
    """匹配键：`平台|邮箱`（平台为空时退化成纯邮箱）。

    CPA 面板同时托管 ChatGPT 与 Grok 的凭据，而同一个邮箱可能两边都注册过
    （邮箱池按平台消耗）。只按邮箱匹配会把两条不同的账号压成一行。
    """
    key = str(email or "").strip().lower()
    plat = str(platform or "").strip().lower()
    return f"{plat}|{key}" if plat else key


def build_comparison(
    local_accounts: list[dict[str, Any]],
    remote_accounts: list[RemoteAccount],
) -> list[ComparisonRow]:
    """把两边的账号列表对照起来，逐 (平台, 邮箱) 产出一行。

    `local_accounts` 每项：`{id, email, platform, status, updated_at, extra}`。
    匹配键是 (平台, 邮箱)：邮箱按小写归一（两边大小写都可能不一致），
    平台参与匹配是因为 CPA 上同一个邮箱可能同时有 ChatGPT 与 Grok 两条凭据。
    平台为空的老数据退化成只按邮箱（单平台面板与历史调用方不受影响）。
    """
    remote_by_key: dict[str, RemoteAccount] = {}
    for item in remote_accounts:
        key = _match_key(getattr(item, "platform", ""), item.email)
        if key:
            remote_by_key[key] = item

    rows: list[ComparisonRow] = []
    seen: set[str] = set()

    for account in local_accounts:
        email = str(account.get("email") or "").strip()
        platform = str(account.get("platform") or "").strip()
        key = _match_key(platform, email)
        if not key or key in seen:
            continue
        seen.add(key)

        local_updated = parse_timestamp(account.get("updated_at"))
        raw_extra = account.get("extra")
        extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
        row = ComparisonRow(
            email=email,
            platform=platform,
            state=STATE_LOCAL_ONLY,
            local_id=account.get("id"),
            local_status=str(account.get("status") or ""),
            local_updated_at=local_updated,
            local_updated_hour=hour_bucket(local_updated),
            local_has_refresh_token=bool(_refresh_token_from_extra(extra)),
        )

        remote = remote_by_key.pop(key, None)
        if remote is None:
            rows.append(row)
            continue

        row.remote_id = remote.remote_id
        row.remote_status = remote.status
        row.remote_updated_at = remote.updated_at
        row.remote_updated_at_raw = remote.updated_at_raw
        row.remote_updated_hour = hour_bucket(remote.updated_at)
        row.remote_extra = dict(remote.extra or {})
        row.differences = diff_fields(extra, remote)
        # 主判据：凭证是否相同（用户口径「AT、RT 全相同就是同步」）
        credential_state, credential_diff = compare_credentials(
            extra, remote.credentials
        )
        row.credential_differences = credential_diff
        row.credential_compared = _count_compared(extra, remote.credentials)
        row.state = credential_state
        # 辅助信息：凭证不同时，时间说明是哪边动的（按小时）
        row.time_relation = compare_by_hour(local_updated, remote.updated_at)
        rows.append(row)

    # 远端多出来的（本地没有）
    for key, remote in sorted(remote_by_key.items()):
        rows.append(
            ComparisonRow(
                email=remote.email,
                platform=str(getattr(remote, "platform", "") or ""),
                state=STATE_REMOTE_ONLY,
                remote_id=remote.remote_id,
                remote_status=remote.status,
                remote_updated_at=remote.updated_at,
                remote_updated_at_raw=remote.updated_at_raw,
                remote_updated_hour=hour_bucket(remote.updated_at),
                remote_extra=dict(remote.extra or {}),
            )
        )

    # 需要动作的排最前：未上传 → 仅远端 → 凭证不同 → 比不了 → 已同步
    order = {
        STATE_LOCAL_ONLY: 0,
        STATE_REMOTE_ONLY: 1,
        STATE_CREDENTIAL_DIFF: 2,
        STATE_UNKNOWN_CREDENTIAL: 3,
        STATE_UNKNOWN_TIME: 4,
        STATE_SYNCED: 5,
    }
    # 平台参与排序键：同一状态下把同一平台的行排在一起（CPA 有两类凭据时
    # 分组更清楚），也保证排序稳定。
    rows.sort(key=lambda item: (order.get(item.state, 9), item.platform, item.email.lower()))
    return rows


def summarize(rows: list[ComparisonRow]) -> dict[str, int]:
    """各状态计数，给页面顶部的统计条用。"""
    summary = {state: 0 for state in STATE_LABELS}
    for row in rows:
        summary[row.state] = summary.get(row.state, 0) + 1
    summary["total"] = len(rows)
    return summary


# ── 远端拉取（每个面板一份适配器） ──


#: 一次对比最多为多少个账号抓完整凭证。
#:
#: 列表接口不返回凭证，得逐个走 download（实测 ~94ms/次）。只对**本地也有的**
#: 账号抓（那些才是要比的），再设个上限兜底 —— 本地几千号时不至于把一次刷新
#: 拖成几分钟。超出的账号凭证留空 → 判 `unknown_credential`（说清"没比"），
#: 不会误报成「已同步」。
_MAX_CREDENTIAL_FETCH = 200

#: 列表接口的翻页上限（三个 fetcher 共用）。上限兜底：别让一个坏接口
#: 把页面拖死 —— 正常面板不会有几十页账号。
_MAX_LIST_PAGES = 20

#: chatgpt2api 把「账号被禁用」放在 `status` 文案里（yukkcat 版中文）。
#: 收集成集合而不是内联 `== "禁用"`：对端改文案时这里一处维护，
#: 且英文部署（basketikun 变体）的 `disabled` 也能覆盖。
_CHATGPT2API_DISABLED_STATUSES = frozenset({"禁用", "disabled", "inactive"})


def _wanted_emails(emails: Optional[set[str]]) -> set[str]:
    """归一化「本地也有的邮箱」集合（匹配键口径）。

    三个远端 fetcher 共用：匹配键是 (平台, 邮箱)，邮箱大小写/空白必须先
    归一，否则同一个地址在两边写法不同就静默失配（整个面板显示未上传）。
    """
    return {str(e or "").strip().lower() for e in (emails or set()) if str(e or "").strip()}


#: CPA（CLIProxyAPI）auth-file 的 provider 名 → 本项目平台名。
#:
#: CLIProxyAPI 里 ChatGPT 走 `codex`（Codex OAuth）、Grok 走 `xai`。
#: 对比时按它把远端记录归到对应平台 —— 同一个邮箱可能在两边都有凭据。
CPA_PROVIDER_PLATFORMS: dict[str, str] = {
    "codex": "chatgpt",
    "xai": "grok",
}


def fetch_cpa_remote_accounts(
    *,
    api_url: str = "",
    api_key: str = "",
    emails: Optional[set[str]] = None,
    providers: tuple[str, ...] = (),
) -> list[RemoteAccount]:
    """CPA / CLIProxyAPI：读 `/v0/management/auth-files`。

    只取 `providers` 里的类型 —— 那个面板里还可能有别的 provider，混进来会把
    "仅远端" 一栏塞满无关账号。缺省取 `CPA_PROVIDER_PLATFORMS` 的全部
    （`codex` + `xai`）：CPA 同时托管 ChatGPT 与 Grok 两类凭据。

    每条记录带上 `platform`（由 provider 映射）—— 对比的匹配键是
    (平台, 邮箱)，同一个邮箱在 CPA 上可能同时有 codex 与 xai 两条。

    `emails` 给定时（本地已有的邮箱集合），只为这些账号抓完整凭证 ——
    列表接口不含 AT/RT，逐个 download 有成本（~94ms/次），没必要为
    "仅远端"的账号付这份钱（那些行不需要比凭证）。
    """
    from services.cliproxyapi_sync import get_auth_file_credentials, list_auth_files

    allowed = {str(p or "").strip().lower() for p in (providers or ()) if str(p or "").strip()}
    if not allowed:
        allowed = set(CPA_PROVIDER_PLATFORMS)
    wanted = _wanted_emails(emails)
    files = list_auth_files(api_url=api_url or None, api_key=api_key or None)
    accounts: list[RemoteAccount] = []
    fetched = 0
    for item in files:
        provider = str(item.get("provider") or item.get("type") or "").strip().lower()
        if allowed and provider not in allowed:
            continue
        email = str(item.get("email") or "").strip()
        if not email:
            continue
        updated_raw = str(
            item.get("last_refresh") or item.get("updated_at") or item.get("modtime") or ""
        ).strip()
        raw_id_token = item.get("id_token")
        id_token: dict[str, Any] = raw_id_token if isinstance(raw_id_token, dict) else {}

        # 只为"本地也有"的账号抓凭证（其余行不参与凭证比对）
        credentials: dict[str, Any] = {}
        name = str(item.get("name") or "").strip()
        if wanted and email.lower() in wanted and name and fetched < _MAX_CREDENTIAL_FETCH:
            try:
                credentials = get_auth_file_credentials(
                    name, api_url=api_url or None, api_key=api_key or None
                )
                fetched += 1
            except Exception as exc:  # noqa: BLE001 - 单个账号抓不到不该毁整次对比
                logger.warning("CPA 账号 %s 凭证读取失败: %s", email, exc)

        accounts.append(
            RemoteAccount(
                email=email,
                platform=CPA_PROVIDER_PLATFORMS.get(provider, ""),
                updated_at=parse_timestamp(updated_raw),
                updated_at_raw=updated_raw,
                status=str(item.get("status") or "").strip(),
                remote_id=str(item.get("auth_index") or name or "").strip(),
                credentials=credentials,
                extra={
                    "name": name,
                    "disabled": bool(item.get("disabled")),
                    "unavailable": bool(item.get("unavailable")),
                    "plan_type": str(id_token.get("plan_type") or "").strip(),
                    "chatgpt_subscription_active_until": str(
                        id_token.get("chatgpt_subscription_active_until") or ""
                    ).strip(),
                },
            )
        )
    return accounts


def _sub2api_extract_items(payload: Any) -> list:
    """从 Sub2API 的列表响应里挖出账号数组。

    响应形状不统一（实测 + 参考实现都见过）：
    - `{code:0, data:{items:[...]}}`（Sub2API 约定）
    - `{items:[...]}` / `{data:[...]}` / `{records:[...]}` / `{list:[...]}`
    逐层找数组，比写死一条路径稳。抄的是 reg-factory `extractItems` 的口径。
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "accounts", "data", "records", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _sub2api_extract_items(value)
            if nested:
                return nested
    return []


def _sub2api_unwrap(item: dict) -> dict:
    """账号对象可能被包在 `account` 里（参考实现里专门有个 unwrap）。"""
    nested = item.get("account")
    return nested if isinstance(nested, dict) else item


#: Sub2API 上属于「本应用推上去的 ChatGPT 号」的 platform 取值。
#: 空值也算（老数据没填 platform 字段，不能因此把它们全滤掉）。
_SUB2API_OPENAI_PLATFORMS = {"", "openai", "chatgpt", "codex"}


def fetch_sub2api_remote_accounts(*, api_url: str = "", api_key: str = "") -> list[RemoteAccount]:
    """Sub2API：`GET /api/v1/admin/accounts`（分页）。

    分页参数与响应形状按 reg-factory 的实测用法：`page` / `page_size`，
    响应可能是 `{code:0,data:{items}}` 也可能是裸 `{items}` —— 解析见
    `_sub2api_extract_items`。认证走 `x-api-key`。
    """
    import requests

    base = str(api_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("Sub2API API URL 未配置")
    key = str(api_key or "").strip()

    accounts: list[RemoteAccount] = []
    page = 1
    while page <= _MAX_LIST_PAGES:
        response = requests.get(
            f"{base}/api/v1/admin/accounts",
            params={"page": page, "page_size": 100},
            headers={
                "x-api-key": key,
                "Accept": "application/json, text/plain, */*",
            },
            timeout=20,
            verify=False,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Sub2API 列表请求失败: HTTP {response.status_code}")
        payload = response.json() if response.content else {}
        # 业务错误码：Sub2API 用 `{code:非0, message}` 表达失败
        if isinstance(payload, dict) and "code" in payload and int(payload.get("code") or 0) != 0:
            raise RuntimeError(
                f"Sub2API 列表失败: {payload.get('message') or payload.get('msg') or payload.get('code')}"
            )
        items = _sub2api_extract_items(payload)
        if not items:
            break
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            item = _sub2api_unwrap(raw_item)
            name = str(item.get("name") or "").strip()
            raw_extra = item.get("extra")
            extra_obj: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
            email = name or str(extra_obj.get("email") or "").strip()
            if not email or "@" not in email:
                continue
            # 只留 OpenAI/ChatGPT 的账号 —— 与 CPA 那边只取 codex 同一个理由：
            # Sub2API 上还挂着 Grok 等别的平台的号，混进来会把「仅远端」一栏
            # 塞满无关账号（本应用只把 ChatGPT 号推给它）。
            platform = str(item.get("platform") or "").strip().lower()
            if platform and platform not in _SUB2API_OPENAI_PLATFORMS:
                continue
            updated_raw = str(
                item.get("updated_at") or item.get("last_used_at") or item.get("created_at") or ""
            ).strip()
            # 凭证：Sub2API 的列表项带 `credentials` 对象（上传时写进去的
            # AT/RT/id_token）。字段名两种写法都认 —— 参考实现读的是
            # `credentials.access_token` / `credentials.accessToken`。
            raw_creds = item.get("credentials")
            credentials: dict[str, Any] = raw_creds if isinstance(raw_creds, dict) else {}
            accounts.append(
                RemoteAccount(
                    email=email,
                    # 匹配键是 (平台, 邮箱)：本地行永远带 platform，远端记录不带
                    # 就永远匹配不上（整个面板会显示成"全部未上传 + 全部仅远端"）。
                    platform="chatgpt",
                    updated_at=parse_timestamp(updated_raw),
                    updated_at_raw=updated_raw,
                    status=str(item.get("status") or "").strip(),
                    remote_id=str(item.get("id") or item.get("db_id") or item.get("account_id") or "").strip(),
                    credentials=credentials,
                    extra={
                        "platform": str(item.get("platform") or "").strip(),
                        "type": str(item.get("type") or "").strip(),
                        "group_ids": item.get("group_ids") or [],
                    },
                )
            )
        if len(items) < 100:
            break
        page += 1
    return accounts


def fetch_grok2api_remote_accounts(
    *,
    api_url: str = "",
    api_key: str = "",
    emails: Optional[set[str]] = None,
) -> list[RemoteAccount]:
    """grok2api：复用 `Grok2ApiClient` 的登录 + 分页账号列表。

    `api_key` 这个位置实际传的是账号密码（grok2api 用 username/password 登录，
    见 `Grok2ApiClient.from_config`）；调用方从配置里取好再传进来。

    字段名按实测的响应来（不是猜的）：时间在 `lastUsedAt` / `createdAt` /
    `observedModelAt`，状态在 `authStatus` + `enabled`，没有 `status` 字段。
    同一条记录还会带一个 `linkedAccounts`（grok_web 子账号），邮箱与父记录
    相同 —— 按邮箱去重，只保留最近有活动的那条，否则同一个号会出现两行、
    把"仅远端"一栏灌满重复项。

    **凭证**走 `/api/admin/v1/accounts/export?provider=grok_web`（实测返回
    `sso_token`）—— 列表接口不含凭证。只为 `emails` 里的账号查（见
    `_MAX_CREDENTIAL_FETCH` 的理由）。
    """
    from platforms.grok.grok2api import Grok2ApiClient

    wanted = _wanted_emails(emails)
    client = Grok2ApiClient.from_config(api_url=api_url)
    client.login()

    # 凭证表：email → {sso_token}（导出接口按 provider 全量给，一次拿完）
    credential_by_email: dict[str, dict[str, Any]] = {}
    if wanted:
        credential_by_email = _fetch_grok2api_credentials(client)

    by_email: dict[str, RemoteAccount] = {}
    page = 1
    while page <= _MAX_LIST_PAGES:
        resp = client._request(
            "GET",
            f"/api/admin/v1/accounts?page={page}&pageSize=100",
            headers=client._auth_headers(),
            timeout=20,
        )
        if int(getattr(resp, "status_code", 0) or 0) != 200:
            raise RuntimeError(f"grok2api 列表请求失败: HTTP {getattr(resp, 'status_code', 0)}")
        data = ((resp.json() or {}).get("data") or {})
        items = data.get("items") or []
        if not isinstance(items, list) or not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            email = str(item.get("email") or "").strip()
            if not email:
                continue
            # 优先"最近用过"，没有就看创建时间（新号还没用过）
            updated_raw = str(
                item.get("lastUsedAt")
                or item.get("observedModelAt")
                or item.get("createdAt")
                or ""
            ).strip()
            candidate = RemoteAccount(
                email=email,
                # 同 sub2api：远端记录必须带 platform，否则 (平台, 邮箱)
                # 匹配键对不上本地行 —— grok2api 面板实测踩过这个坑。
                platform="grok",
                updated_at=parse_timestamp(updated_raw),
                updated_at_raw=updated_raw,
                status=str(item.get("authStatus") or "").strip(),
                remote_id=str(item.get("id") or "").strip(),
                credentials=credential_by_email.get(email.lower(), {}),
                extra={
                    "provider": str(item.get("provider") or "").strip(),
                    "disabled": not bool(item.get("enabled", True)),
                    "linked": len(item.get("linkedAccounts") or []),
                },
            )
            existing = by_email.get(email.lower())
            if existing is None or _is_newer(candidate, existing):
                by_email[email.lower()] = candidate
        if len(items) < 100:
            break
        page += 1
    return list(by_email.values())


def _fetch_grok2api_credentials(client) -> dict[str, dict[str, Any]]:
    """拉 grok2api 的凭证导出表（email → {sso_token, ...}）。

    导出接口按 provider 给全量数据（不是按账号查），一次调用就够。
    `grok_web` 是 SSO 那条线（实测 12 条含 `sso_token`）；`grok_build` 在这套
    部署里是空的（返回 `{"accounts": []}`），所以只取 web。
    """
    result: dict[str, dict[str, Any]] = {}
    try:
        resp = client._request(
            "GET",
            "/api/admin/v1/accounts/export?provider=grok_web&limit=1000",
            headers=client._auth_headers(),
            timeout=30,
        )
        if int(getattr(resp, "status_code", 0) or 0) != 200:
            return result
        payload = resp.json() or {}
        for entry in payload.get("accounts") or []:
            if not isinstance(entry, dict):
                continue
            email = str(entry.get("email") or "").strip().lower()
            if not email:
                continue
            result[email] = {
                "sso": str(entry.get("sso_token") or "").strip(),
                "id_token": str(entry.get("token") or "").strip(),
            }
    except Exception as exc:  # noqa: BLE001 - 凭证拿不到时照常出列表（判 unknown）
        logger.warning("grok2api 凭证导出失败: %s", exc)
    return result


def _is_newer(candidate: RemoteAccount, current: RemoteAccount) -> bool:
    """谁的时间更晚（都没有时间时按 remote_id 大小，保证结果稳定）。"""
    left, right = candidate.updated_at, current.updated_at
    if left is None:
        return False
    if right is None:
        return True
    if left != right:
        return left > right
    return str(candidate.remote_id) > str(current.remote_id)


#: chatgpt2api 列表接口的分页大小（yukkcat 变体服务端 clamp 上限就是 500）。
_CHATGPT2API_PAGE_SIZE = 500
#: 单次 export 请求最多要多少个账号的凭证（列表分页大小的子集，够用）。
_CHATGPT2API_EXPORT_BATCH = 200


def _chatgpt2api_credentials_from_item(item: dict[str, Any]) -> dict[str, Any]:
    """列表项里直接带凭证就用它（basketikun 变体的列表返回完整账号含 AT）。

    yukkcat 变体的列表**不返回**任何 token（只有 `access_token_status` 这类
    生命周期标签），凭证得走 export 接口 —— 见 `_chatgpt2api_export_credentials`。
    """
    credentials: dict[str, Any] = {}
    for field in ("access_token", "refresh_token", "id_token"):
        value = str(item.get(field) or "").strip()
        if value:
            credentials[field] = value
    return credentials


def _chatgpt2api_missing_ids(response: Any) -> set[str]:
    """从 export 的 400 响应里解析「没找到」的 account_id 集合。

    对端的错误形状：`{"detail": {"error": ..., "errors": [{"id": ...}, ...]}}`。
    解析不出来就返回空集（调用方会当成"整批失败"处理，不会误以为能重试）。
    """
    try:
        detail = (response.json() or {}).get("detail") or {}
    except Exception:  # noqa: BLE001 - 非 JSON 错误体
        return set()
    errors = detail.get("errors") if isinstance(detail, dict) else None
    if not isinstance(errors, list):
        return set()
    return {
        str(entry.get("id") or "").strip()
        for entry in errors
        if isinstance(entry, dict) and str(entry.get("id") or "").strip()
    }


def _chatgpt2api_export_credentials(
    *, base: str, key: str, account_ids: list[str], timeout: int = 60
) -> dict[str, dict[str, Any]]:
    """按 management_id 批量导出凭证（yukkcat 变体的 `POST /api/accounts/export`）。

    返回 `{management_id: {access_token, refresh_token, id_token}}`。
    取不到的账号**不出现**在结果里 —— 调用方按「没有凭证」处理（判
    `unknown_credential` 说清"比不了"，不会误报成「已同步」）。
    """
    import requests

    result: dict[str, dict[str, Any]] = {}
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    pending = [str(i).strip() for i in account_ids if str(i or "").strip()]

    for start in range(0, len(pending), _CHATGPT2API_EXPORT_BATCH):
        batch = pending[start : start + _CHATGPT2API_EXPORT_BATCH]

        def _post(ids: list[str]):
            return requests.post(
                f"{base}/api/accounts/export",
                headers=headers,
                json={"account_ids": ids, "format": "json"},
                timeout=timeout,
                verify=False,
            )

        try:
            response = _post(batch)
        except Exception as exc:  # noqa: BLE001 - 凭证拿不到时照常出列表
            logger.warning("chatgpt2api 凭证导出失败: %s", exc)
            continue

        if response.status_code == 400:
            # 有账号在列表之后被删掉了 → 对端整批 400 并列出缺的 id。
            # 去掉缺失的重试一次；再不行就放弃这一批（凭证留空 = 判"比不了"）。
            missing = _chatgpt2api_missing_ids(response)
            batch = [i for i in batch if i not in missing]
            if not batch:
                continue
            try:
                response = _post(batch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("chatgpt2api 凭证导出重试失败: %s", exc)
                continue

        if response.status_code != 200:
            logger.warning("chatgpt2api 凭证导出 HTTP %s", response.status_code)
            continue

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            # 解析失败要留痕：静默 continue 会让整批账号的凭证无声丢弃，
            # 界面上只表现为「比不了」，排查时没有任何线索。
            logger.warning(
                "chatgpt2api 凭证导出响应解析失败 (HTTP %s): %s",
                response.status_code,
                exc,
            )
            continue
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            # 单账号导出时对端返回裸对象（`items[0] if len(items)==1`）
            entries = [data]
        else:
            entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            account_id = str(
                entry.get("management_id") or entry.get("id") or ""
            ).strip()
            credentials = _chatgpt2api_credentials_from_item(entry)
            if account_id and credentials:
                result[account_id] = credentials
    return result


def fetch_chatgpt2api_remote_accounts(
    *,
    api_url: str = "",
    api_key: str = "",
    emails: Optional[set[str]] = None,
) -> list[RemoteAccount]:
    """chatgpt2api：`GET /api/accounts`（分页）。

    两个同名不同源的实现都兼容（字段形状实测过，不是猜的）：

    - **yukkcat 变体**（本项目对接的部署）：列表**不返回** token —— 只有
      `access_token_status` / `credential_availability` 这类生命周期标签。
      凭证走 `POST /api/accounts/export`（按 `account_ids` = 列表里的 `id`，
      即 management_id）批量取。
    - **basketikun 变体**：列表直接带 `access_token`，就地取。

    `emails` 给定时（本地已有的邮箱集合），**只为这些账号**抓凭证 —— 与
    CPA 同一理由：仅远端的行不参与凭证比对，不值得为它们付导出的钱。
    """
    import requests

    base = str(api_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("chatgpt2api 地址未配置")
    key = str(api_key or "").strip()
    wanted = _wanted_emails(emails)
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }

    accounts: list[RemoteAccount] = []
    #: management_id → email，列表不带凭证时按它去 export
    credential_targets: dict[str, str] = {}
    seen_ids: set[str] = set()
    page = 1
    while page <= _MAX_LIST_PAGES:
        response = requests.get(
            f"{base}/api/accounts",
            params={"page": page, "page_size": _CHATGPT2API_PAGE_SIZE},
            headers=headers,
            timeout=20,
            verify=False,
        )
        if response.status_code != 200:
            raise RuntimeError(f"chatgpt2api 列表请求失败: HTTP {response.status_code}")
        payload = response.json() if response.content else {}
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            break

        new_count = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            email = str(item.get("email") or "").strip()
            if not email:
                continue
            remote_id = str(item.get("id") or item.get("management_id") or "").strip()
            dedupe_key = remote_id or email.lower()
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            new_count += 1

            updated_raw = str(
                item.get("last_used_at") or item.get("created_at") or ""
            ).strip()
            credentials = _chatgpt2api_credentials_from_item(item)
            if not credentials and wanted and email.lower() in wanted and remote_id:
                credential_targets.setdefault(remote_id, email.lower())

            accounts.append(
                RemoteAccount(
                    email=email,
                    # 匹配键是 (平台, 邮箱)：不带这个字段就永远匹配不上本地行
                    # （整个面板会显示成"全部未上传 + 全部仅远端"，实测踩过）。
                    platform="chatgpt",
                    updated_at=parse_timestamp(updated_raw),
                    updated_at_raw=updated_raw,
                    # yukkcat 只有 `status_label`（中文），basketikun 有 `status`。
                    status=str(
                        item.get("status_label") or item.get("status") or ""
                    ).strip(),
                    remote_id=remote_id,
                    credentials=credentials,
                    extra={
                        # 展示口径与 CPA 的 extra 对齐（前端读这两个键）
                        "plan_type": str(item.get("plan") or item.get("type") or "").strip(),
                        "disabled": bool(item.get("disabled"))
                        or item.get("enabled") is False
                        or str(item.get("status") or "").strip()
                        in _CHATGPT2API_DISABLED_STATUSES,
                    },
                )
            )

        # 翻页停止条件：不满一页、或整页都是重复（basketikun 不分页，
        # 第二页会把同样的账号再给一遍 —— 没有新账号就收手）。
        if len(items) < _CHATGPT2API_PAGE_SIZE or new_count == 0:
            break
        page += 1

    # 列表不带凭证的变体（yukkcat）：只为本地也有的账号批量导出。
    if credential_targets:
        targets = list(credential_targets)[:_MAX_CREDENTIAL_FETCH]
        if len(credential_targets) > len(targets):
            logger.warning(
                "chatgpt2api 凭证导出超过上限 %s，只取前 %s 个",
                _MAX_CREDENTIAL_FETCH,
                len(targets),
            )
        fetched = _chatgpt2api_export_credentials(
            base=base, key=key, account_ids=targets
        )
        if fetched:
            for account in accounts:
                credentials = fetched.get(account.remote_id)
                if credentials:
                    account.credentials = credentials
    return accounts


#: 面板 key → 拉取函数
FETCHERS: dict[str, Callable[..., list[RemoteAccount]]] = {
    "cpa": fetch_cpa_remote_accounts,
    "sub2api": fetch_sub2api_remote_accounts,
    "grok2api": fetch_grok2api_remote_accounts,
    "chatgpt2api": fetch_chatgpt2api_remote_accounts,
}


__all__ = [
    "ComparisonRow",
    "CREDENTIAL_FIELDS",
    "FETCHERS",
    "RemoteAccount",
    "STATE_CREDENTIAL_DIFF",
    "STATE_LABELS",
    "STATE_LOCAL_ONLY",
    "STATE_REMOTE_ONLY",
    "STATE_SYNCED",
    "STATE_UNKNOWN_CREDENTIAL",
    "STATE_UNKNOWN_TIME",
    "build_comparison",
    "compare_by_hour",
    "compare_credentials",
    "hour_bucket",
    "parse_timestamp",
    "summarize",
]
