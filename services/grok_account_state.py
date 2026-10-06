"""Grok 账号状态判定。

状态语义（用户要求 2026-10-06，与全站统一）：

- `registered`（正常）：正常能用；
- `expired`（过期）：AT 已过期（JWT exp 已过）—— 刷新可能救回；
- `invalid`（失效）：需要重新登录 —— **对齐 grok2api 的 reauthRequired 语义**：
  SSO / OAuth 被上游明确拒绝（401、`invalid_grant` 等永久错误），
  与 grok2api 的 `markSSOCredentialRejected`（"SSO credential rejected" →
  reauthRequired）同一口径；
- `banned`（禁用）：被封（上游明确 `blocked-user` / `user is blocked` ——
  对齐 grok2api 的 `IsDefinitiveAccountBlockBody`）。

判定优先级：禁用 > 过期 > 失效。正向确认（probe 200 / 402 无额度 / 刷新成功）
时过期/失效恢复「正常」；禁用不自动恢复。
"""

from __future__ import annotations

from typing import Any, Optional

from core.base_platform import AccountStatus

#: grok2api 对「账号被封」的判定词（`IsDefinitiveAccountBlockBody` 同款）。
_BLOCKED_MARKERS = ("blocked-user", "user is blocked")


def looks_like_grok_blocked(text: Any) -> bool:
    """响应文本读起来像不像「账号被封」（对齐 grok2api）。"""
    value = str(text or "").strip().lower()
    if not value:
        return False
    return any(marker in value for marker in _BLOCKED_MARKERS)


def _probe_rejected(code: Optional[int], summary: str) -> Optional[str]:
    """探测结果是否说明凭证被拒 —— 返回理由（None = 没被拒）。

    与 `GrokPlatform._probe_verdict` 同口径：402/403 无额度仍算有效；
    403 带 invalid / revoked / expired / unauthenticated 字样才是被拒；
    401 被拒。
    """
    if code is None:
        return None
    if code == 401:
        return "probe_401"
    if code in (402, 403):
        low = str(summary or "").lower()
        for keyword in ("invalid", "revoked", "expired", "unauthenticated"):
            if keyword in low:
                return f"probe_{code}_credential"
        return None
    if code == 200:
        return None
    if code == 426:
        # 版本闸：探测本身没生效，不是账号问题
        return None
    return f"probe_{code}"


def _refresh_reason(refresh_kind: str, refresh_error: str) -> Optional[str]:
    """刷新失败分类 → 判定理由（None = 不动状态）。

    - permanent（invalid_grant 等）→ 失效（需要重新登录）；
    - configuration（invalid_client 等）→ 网关问题，不是账号问题；
    - retryable / unknown → 瞬时或未知，保守不动。
    """
    kind = str(refresh_kind or "").strip().lower()
    if kind == "permanent":
        return "refresh_permanent"
    return None


def apply_grok_status_policy(
    account: Any,
    *,
    probe_code: Optional[int] = None,
    probe_summary: str = "",
    refresh_kind: str = "",
    refresh_error: str = "",
    remote_state: str = "",
    usable: bool = False,
    sso_rejected: bool = False,
) -> str:
    """按探测/刷新/同步结论落账号状态，返回判定理由（空串 = 没判定）。

    - `probe_code` / `probe_summary`：测活动作（probe / check_valid）的结果；
    - `refresh_kind` / `refresh_error`：probe_refresh 的分类结果
      （permanent / configuration / retryable / unknown）；
    - `remote_state`：CPA 同步的远端状态（usable / access_token_invalidated /
      unauthorized / ...，口径见 `services/cliproxyapi_sync.py`）；
    - `usable=True`：明确的正向信号（刷新成功 / 探测 200 / 402 无额度）；
    - `sso_rejected=True`：SSO 被上游明确拒绝（对齐 grok2api 的
      markSSOCredentialRejected）。

    优先级：禁用 > 过期 > 失效。正向确认时过期/失效恢复「正常」。
    """
    # ① 封禁（最高优先级）
    if looks_like_grok_blocked(probe_summary) or looks_like_grok_blocked(refresh_error):
        setattr(account, "status", AccountStatus.BANNED.value)
        return "grok_blocked"

    # ② 被拒（探测 / 刷新 / SSO / 远端同步）→ 过期 or 失效
    reason = ""
    if sso_rejected:
        reason = "sso_rejected"
    remote = str(remote_state or "").strip().lower()
    if not reason and remote in ("access_token_invalidated", "unauthorized"):
        reason = "remote_invalidated"
    if not reason:
        rejected = _probe_rejected(probe_code, probe_summary)
        if rejected:
            reason = rejected
    if not reason:
        refresh_reason = _refresh_reason(refresh_kind, refresh_error)
        if refresh_reason:
            reason = refresh_reason

    if reason:
        from services.account_status import access_token_expired

        # 「明确需要重新登录」的判定不受 AT 过期影响：SSO 被拒 / 刷新永久
        # 失败（invalid_grant）时刷新救不回，按失效落 —— 与用户语义一致
        # （失效 = 需要重新登录的）。
        if reason in ("refresh_permanent", "sso_rejected", "remote_invalidated"):
            setattr(account, "status", AccountStatus.INVALID.value)
        elif access_token_expired(account, platform="grok"):
            setattr(account, "status", AccountStatus.EXPIRED.value)
        else:
            setattr(account, "status", AccountStatus.INVALID.value)
        return reason

    # ③ 无异常信号：正向确认时恢复「正常」（禁用不动）
    # 200 / 402 / 403 都是「凭证有效」的探测结论（与 `_probe_verdict` 同口径：
    # 402 无额度、403 权限类拒绝仍算有效；带 invalid 字样的 403 已在上面的
    # 被拒分支处理，走不到这里）。
    probe_usable = probe_code in (200, 402, 403)
    if usable or probe_usable or remote == "usable":
        current = str(getattr(account, "status", "") or "").strip().lower()
        if current in {"expired", "invalid"}:
            setattr(account, "status", AccountStatus.REGISTERED.value)
            return "recovered"
    return ""


__all__ = [
    "apply_grok_status_policy",
    "looks_like_grok_blocked",
]
