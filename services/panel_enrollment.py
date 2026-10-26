"""面板登记：上传到某面板**并**纳入其同步范围。

术语见 `GLOSSARY.md`：**登记**是「推送 + 记住这次推送」，与「拉回」区分开。
决策见 `docs/adr/0002-panel-enrollment-and-bidirectional-sync.md`。

**登记只由本模块读写。** 旧的 `sync_statuses[<panel>].uploaded` 是粘性布尔
（`services/chatgpt_sync.py` 里 `or bool(ok)`，一旦为真永不复位），它区分不了
「登记过但远端后来删了」与「现在完整」；而且前端从不读它。本模块**不**把它
当判据，它降级为诊断信息。
"""

from __future__ import annotations

from typing import Any

#: `extra` 里存登记表的键。形状：
#: `{"<panel>": {"at": "<ISO 时间>", "remote_id": "<远端标识，可为空>"}}`
PANEL_UPLOADS_KEY = "panel_uploads"


def _table(extra: Any) -> dict[str, Any]:
    """取登记表；脏数据一律当空表（一个坏键不该让整个列表 500）。"""
    if not isinstance(extra, dict):
        return {}
    raw = extra.get(PANEL_UPLOADS_KEY)
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def enrolled_panels(extra: Any) -> set[str]:
    """这个账号已登记到哪些面板。"""
    return set(_table(extra).keys())


def is_enrolled(extra: Any, panel: str) -> bool:
    """是否已登记到该面板（`panel` 按小写归一）。"""
    name = str(panel or "").strip().lower()
    if not name:
        return False
    return name in _table(extra)


def get_enrollment(extra: Any, panel: str) -> dict[str, Any]:
    """取登记记录（没有则空 dict）。"""
    name = str(panel or "").strip().lower()
    record = _table(extra).get(name)
    return dict(record) if isinstance(record, dict) else {}


def enroll(
    extra: dict[str, Any],
    panel: str,
    *,
    remote_id: str = "",
    ok: bool,
    message: str = "",
) -> None:
    """上传成功后登记该面板。**上传失败不登记。**

    失败还登记的话，对比表会显示「已登记但远端没有」—— 那正是用户要摆脱的
    自相矛盾状态（远端被删、本地还标着已上传）。
    """
    name = str(panel or "").strip().lower()
    if not name or not isinstance(extra, dict):
        return
    table = _table(extra)
    if not ok:
        # 失败只更新诊断信息，不写登记表
        existing = table.get(name)
        if isinstance(existing, dict):
            existing["last_attempt_ok"] = False
            existing["last_message"] = str(message or "")
            table[name] = existing
            extra[PANEL_UPLOADS_KEY] = table
        return

    from datetime import datetime, timezone

    table[name] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "remote_id": str(remote_id or ""),
        "last_attempt_ok": True,
        "last_message": str(message or ""),
    }
    extra[PANEL_UPLOADS_KEY] = table


def revoke(extra: dict[str, Any], panel: str) -> bool:
    """撤销登记。返回是否真的删掉了（用于调用方判断要不要落库）。"""
    name = str(panel or "").strip().lower()
    if not name or not isinstance(extra, dict):
        return False
    table = _table(extra)
    if name not in table:
        return False
    del table[name]
    if table:
        extra[PANEL_UPLOADS_KEY] = table
    else:
        extra.pop(PANEL_UPLOADS_KEY, None)
    return True


def reconcile_with_remote(extra: dict[str, Any], panel: str, *, present: bool) -> bool:
    """用「远端到底有没有这个账号」校正本地登记。

    远端被人工删掉时自动撤销登记（用户要求，ADR-0002）。不撤的话本地一直
    以为「已登记」，同步会反复去拉一个不存在的账号。

    返回是否发生了变更。
    """
    if present:
        return False
    return revoke(extra, panel)
