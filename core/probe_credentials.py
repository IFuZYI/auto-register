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


__all__ = ["persist_probe_credentials"]
