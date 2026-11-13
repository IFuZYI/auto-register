"""探测/刷新换回的新凭据写回账号行（动作链路与单账号检测、调度器共用）。

放在 `core/` 是因为 `core/scheduler.py` 不能反向 import `services`；
`services.action_execution` 再导出一次，保持原有 import 路径。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def persist_probe_credentials(acc_model, detail: dict, session=None) -> bool:
    """把 `detail` 里的 access_token/refresh_token/expires_in 写回账号行。

    返回是否发生写入。写回失败只记日志，不影响探测结论。
    """
    detail = detail or {}
    if not detail.get("usable") or not (detail.get("access_token") or detail.get("refresh_token")):
        return False
    try:
        extra = dict(acc_model.get_extra() or {})
        if detail.get("access_token"):
            extra["access_token"] = str(detail["access_token"])
        if detail.get("refresh_token"):
            extra["refresh_token"] = str(detail["refresh_token"])
        if detail.get("expires_in"):
            extra["expires_at"] = int(
                datetime.now(timezone.utc).timestamp() * 1000
            ) + int(detail["expires_in"]) * 1000
        acc_model.set_extra(extra)
        if detail.get("refresh_token"):
            acc_model.token = str(detail["refresh_token"])
        return True
    except Exception as exc:  # noqa: BLE001 - 写回失败不影响探测结论
        logger.debug("探测凭据写回失败: %s", exc)
        return False


def mark_probe_checked(acc_model, detail: dict | None = None) -> bool:
    """盖一个「这个账号刚被探测过」的时间戳（`extra["probe_checked_at"]`，毫秒）。

    给「最久未检测优先」的抽检轮转用（`services.local_maintenance.last_probe_at`）。
    AWS / Kiro 的探测不像 ChatGPT 那样会落 `chatgpt_local.checked_at` —— 不盖
    这个戳的话读到的永远是 0，排序退化成 id 序，**每轮都挑同一批号**。

    **失败也要盖**：400 / 403 / 网络不明同样是「已经看过一眼」。只在成功时写，
    失效号会一直排在最前、每轮都被重测。`detail` 只为签名与
    `persist_probe_credentials` 对齐（写戳与结论无关），保留给日志排查。
    """
    try:
        extra = dict(acc_model.get_extra() or {})
        extra["probe_checked_at"] = int(datetime.now(timezone.utc).timestamp() * 1000)
        acc_model.set_extra(extra)
        return True
    except Exception as exc:  # noqa: BLE001 - 写戳失败不影响探测结论
        logger.debug("探测时间写回失败（detail=%s）: %s", (detail or {}).get("code"), exc)
        return False


__all__ = ["mark_probe_checked", "persist_probe_credentials"]
