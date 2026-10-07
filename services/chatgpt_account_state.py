"""ChatGPT 账号状态判定辅助逻辑。

状态语义（用户要求 2026-10-06）：

- `registered`（正常）：正常能使用的账号；
- `expired`（过期）：AT 已过期（JWT exp 已过）—— 刷新可能救回；
- `invalid`（失效）：需要重新登录的（凭证被拒但 AT 未过期 / 缺凭证）；
- `banned`（禁用）：被封了的账号 —— 登录流程发掘。

判定优先级（探测发现凭证被拒时）：禁用 > 过期 > 失效。
"""

from __future__ import annotations

from typing import Any, Optional


INVALID_ACCOUNT_STATUS = "invalid"
#: 号已删除/停用（OpenAI 原话 "deleted or deactivated"）。与 `invalid` 的区别见
#: `apply_chatgpt_status_policy` —— 判重时封禁的邮箱仍算被占用。
BANNED_ACCOUNT_STATUS = "banned"


def _lower_text(value: Any) -> str:
    return str(value or "").strip().lower()


def access_token_expired(account: Any, *, now_seconds: Optional[int] = None) -> bool:
    """账号的 AT 是否已过期 —— chatgpt 专用薄包装（共享件在 account_status）。"""
    from services.account_status import access_token_expired as _expired

    return _expired(account, platform="chatgpt", now_seconds=now_seconds)


def is_account_deactivated_message(error_code: Any = "", message: Any = "") -> bool:
    code = _lower_text(error_code)
    text = _lower_text(message)
    if code in {"account_deactivated", "account_deleted"}:
        return True
    markers = (
        "deleted or deactivated",
        "account has been deleted or deactivated",
        "you do not have an account because it has been deleted or deactivated",
    )
    return any(marker in text for marker in markers)


# 401/403 的响应体里 OpenAI 说封号的措辞不止一种，全部小写后按子串匹配。
# 比 is_account_deactivated_message 宽：那个只认"已删除/已停用"，这里连
# 违规、滥用、封禁一起认，用在"凭证被吊销到底是过期还是封号"这种场合。
_BANNED_BODY_MARKERS = (
    "account_deactivated",
    "accountdeactivated",
    "deactivated",
    "disabled",
    "suspended",
    "banned",
    "violat",
    "potential abuse",
    "terminated",
)


def looks_like_banned_response(body_text: Any) -> bool:
    """凭证被拒的响应体读起来像不像封号。"""
    text = _lower_text(body_text)
    return any(marker in text for marker in _BANNED_BODY_MARKERS)


PLUS_STATUS_UNCHECKED = "unchecked"


def account_plus_status(account: Any) -> str:
    """账号上记录的 Plus 试用结论，没查过算 unchecked。"""
    extra: Any = {}
    getter = getattr(account, "get_extra", None)
    if callable(getter):
        extra = getter() or {}
    else:
        extra = getattr(account, "extra", {}) or {}
    check = extra.get("plus_check") if isinstance(extra, dict) else None
    if not isinstance(check, dict):
        return PLUS_STATUS_UNCHECKED
    return _lower_text(check.get("status")) or PLUS_STATUS_UNCHECKED


def filter_accounts_by_plus_status(accounts: Any, plus_status: Any) -> list:
    wanted = _lower_text(plus_status)
    if not wanted:
        return list(accounts)
    return [account for account in accounts if account_plus_status(account) == wanted]


def classify_local_probe_state(probe: dict[str, Any] | None) -> str:
    if not isinstance(probe, dict):
        return ""

    auth = probe.get("auth") if isinstance(probe.get("auth"), dict) else {}
    codex = probe.get("codex") if isinstance(probe.get("codex"), dict) else {}

    auth_state = _lower_text(auth.get("state"))
    auth_status = int(auth.get("http_status") or 0)
    auth_error_code = auth.get("error_code")
    auth_message = auth.get("message")

    if auth_state == "missing_access_token":
        return "auth_missing"
    if auth_status == 401 or auth_state in {"access_token_invalidated", "unauthorized"}:
        return "auth_401"
    if is_account_deactivated_message(auth_error_code, auth_message):
        return "auth_deactivated"
    if auth_status == 403 and auth_state in {"account_deactivated", "banned_like"}:
        return "auth_403"

    codex_state = _lower_text(codex.get("state"))
    codex_status = int(codex.get("http_status") or 0)
    codex_error_code = codex.get("error_code")
    codex_message = codex.get("message")

    if codex_status == 401 or codex_state in {"access_token_invalidated", "unauthorized"}:
        return "codex_401"
    if is_account_deactivated_message(codex_error_code, codex_message):
        return "codex_deactivated"
    if codex_status == 403 and codex_state == "account_deactivated":
        return "codex_403"

    return ""


def classify_remote_sync_state(sync: dict[str, Any] | None) -> str:
    if not isinstance(sync, dict):
        return ""

    remote_state = _lower_text(sync.get("remote_state"))
    status_code = int(sync.get("last_probe_status_code") or 0)
    error_code = sync.get("last_probe_error_code")
    message = sync.get("last_probe_message") or sync.get("status_message") or sync.get("message")

    if status_code == 401 or remote_state in {"access_token_invalidated", "unauthorized"}:
        return "remote_401"
    if is_account_deactivated_message(error_code, message):
        return "remote_deactivated"
    if status_code == 403 and remote_state in {"account_deactivated", "banned_like"}:
        return "remote_403"

    return ""


def _is_banned_status(status: Any) -> bool:
    """账号当前状态是否已是「禁用」（大小写/空白容忍）。"""
    return _lower_text(status) == BANNED_ACCOUNT_STATUS


def _is_banned_reason(reason: str) -> bool:
    """这个判定理由说的是「号没了」还是「凭证过期」。

    封禁类理由里带 `deactivated`（`auth_deactivated` / `codex_deactivated` /
    `remote_deactivated`）—— 那是 OpenAI 明说号已删除/停用，怎么登都回不来。
    其余（401 / 403 无封禁措辞）只是凭证失效，重登还有救。
    """
    return "deactivated" in str(reason or "").lower()


def probe_confirms_usable(probe: dict[str, Any] | None) -> bool:
    """探测结果是否**明确确认**凭证可用（用于状态恢复）。

    只认正向状态：auth 的 `access_token_valid`，或 codex 的 `usable`。
    缺省/失败态一律不算 —— 「没查出问题」不等于「确认可用」。
    """
    if not isinstance(probe, dict):
        return False
    auth_raw = probe.get("auth")
    codex_raw = probe.get("codex")
    auth = auth_raw if isinstance(auth_raw, dict) else {}
    codex = codex_raw if isinstance(codex_raw, dict) else {}
    if _lower_text(auth.get("state")) == "access_token_valid":
        return True
    if _lower_text(codex.get("state")) == "usable":
        return True
    return False


def apply_chatgpt_status_policy(
    account: Any,
    *,
    local_probe: dict[str, Any] | None = None,
    remote_sync: dict[str, Any] | None = None,
    banned: bool = False,
    usable: bool = False,
) -> str:
    """按探测/同步结论落账号状态，返回判定理由（空串 = 没判定）。

    **禁用 / 过期 / 失效三档要分开**（用户要求 2026-10-06）：

    - `banned`（禁用）：号已删除/停用（"deleted or deactivated"），怎么登
      都登不回来。判重语义上它与 registered 同侧：号虽然废了但**邮箱仍被
      这个平台占用**，不该被当成"没注册过"再拿这个邮箱去注册一次。
    - `expired`（过期）：AT 已过期（JWT exp 已过）—— 刷新/重登可能救回。
    - `invalid`（失效）：凭证被拒但 AT 未过期（被吊销/会话失效）或缺凭证
      —— 需要重新登录，走流程登录。

    判定优先级：禁用 > 过期 > 失效。封禁措辞与过期同时存在时按禁用落
    （号都没了，谈过期没意义）。

    **正向恢复**：探测明确可用（`probe_confirms_usable`）或刷新成功
    （`usable=True`）时，过期/失效恢复为「正常」—— 状态跟着实际可用性走。
    禁用不自动恢复（强判断，要人工确认）；且**禁用是粘性的**——弱信号
    （401 / 缺凭证 / 远端失效）不得把它降级回过期/失效（用户要求
    2026-10-07「禁用的不参与同步」的同源问题：降级会让刚发掘的封禁被
    下一次状态同步洗掉）。

    这条判定必须留在这里（共享策略）而不是各个 action 的调用点：探测、同步、
    刷新三条路都会得出"号没了"的结论，只在其中一条上特判的话，另外两条仍然
    把封号写成 invalid。

    `banned=True` 给刷新链用 —— 那条路已经自己认过封禁措辞
    （`login_refresh.looks_like_banned`），这里只负责落状态。
    """
    reason = classify_local_probe_state(local_probe) or classify_remote_sync_state(remote_sync)
    if banned:
        reason = reason or "refresh_banned"

    if reason:
        if banned or _is_banned_reason(reason):
            setattr(account, "status", BANNED_ACCOUNT_STATUS)
        elif _is_banned_status(getattr(account, "status", "")) and not _is_banned_reason(reason):
            # 禁用是**粘性**的：弱信号（401 / 缺凭证）不得把它降级回过期/失效。
            # 封禁要人工确认才解除（同「不因一次可用探测复活」的强判断语义）；
            # 降级会让刚发掘出的封禁被下一次状态同步洗掉、账号重新进重试队列。
            return ""
        elif reason == "auth_missing":
            # 没有 AT 可探测 —— 需要重新登录拿凭证
            setattr(account, "status", INVALID_ACCOUNT_STATUS)
        elif access_token_expired(account):
            # 凭证被拒且 AT 的 exp 已过 → 「过期」（刷新可能救回）
            setattr(account, "status", "expired")
        else:
            setattr(account, "status", INVALID_ACCOUNT_STATUS)
        return reason

    # 无异常信号：正向确认时恢复「正常」（过期/失效 → registered）。
    # 禁用不动 —— 它是强判断，不因一次可用探测就复活。
    remote_usable = (
        isinstance(remote_sync, dict)
        and _lower_text(remote_sync.get("remote_state")) == "usable"
    )
    if usable or probe_confirms_usable(local_probe) or remote_usable:
        current = str(getattr(account, "status", "") or "").strip().lower()
        if current in {"expired", "invalid"}:
            setattr(account, "status", "registered")
            return "recovered"
    return reason
