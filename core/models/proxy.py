"""代理的**唯一**表示。

以前有两个：SQLite 分支返回 `ProxyModel`（7 个字段），JSON 分支返回 `_JsonProxy`
（只有 4 个：id / url / region / is_active）。后果不是「风格不统一」，而是**功能
缺失**：`success_count` / `fail_count` / `last_checked` 在 JSON 部署下被静默丢掉，
代理池的「成功率」永远显示 0。

现在两条后端都返回 `ProxyRecord`，字段与 `proxies` 表一一对应。

主键**保持 int**：代理不是账号，`ORDER BY id` 在这里没有承重语义，但也没有换
UUID 的理由（ADR-0008 只动 `accounts`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class ProxyRecord:
    """一行代理。字段与 `proxies` 表同名同序。"""

    url: str
    id: Optional[int] = None
    region: str = ""
    success_count: int = 0
    fail_count: int = 0
    is_active: bool = True
    last_checked: Optional[datetime] = None

    # ---------------------------------------------------------------- 转换
    @classmethod
    def from_model(cls, model: Any) -> "ProxyRecord":
        return cls(
            url=str(getattr(model, "url", "") or ""),
            id=getattr(model, "id", None),
            region=str(getattr(model, "region", "") or ""),
            success_count=int(getattr(model, "success_count", 0) or 0),
            fail_count=int(getattr(model, "fail_count", 0) or 0),
            is_active=bool(getattr(model, "is_active", True)),
            last_checked=getattr(model, "last_checked", None),
        )

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> "ProxyRecord":
        last: Any = record.get("last_checked")
        if isinstance(last, str) and last:
            try:
                last = datetime.fromisoformat(last.replace("Z", "+00:00"))
            except ValueError:
                last = None
        if not isinstance(last, datetime):
            last = None
        return cls(
            url=str(record.get("url") or ""),
            id=record.get("id"),
            region=str(record.get("region") or ""),
            success_count=int(record.get("success_count") or 0),
            fail_count=int(record.get("fail_count") or 0),
            is_active=bool(record.get("is_active", True)),
            last_checked=last,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "region": self.region,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "is_active": self.is_active,
            "last_checked": self.last_checked.isoformat() if self.last_checked else None,
        }

    def __repr__(self) -> str:  # 便于排障
        return f"<ProxyRecord id={self.id} url={self.url!r}>"


__all__ = ["ProxyRecord"]
