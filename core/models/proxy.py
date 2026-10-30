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


#: 代理标签。**只认这两个**，其余值一律丢弃（脏数据不该影响选路）。
TAG_REGISTER = "register"
TAG_BIND = "bind"
VALID_PROXY_TAGS = (TAG_REGISTER, TAG_BIND)


def parse_proxy_tags(raw: Any) -> list[str]:
    """把库里/接口里的各种形态收敛成有序去重的标签列表。

    接受 `"register,bind"`、`["bind", "register"]`、`None`；未知值直接丢掉 ——
    拼错的标签如果被保留，选路时会出现「看起来配了、实际没生效」。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(part).strip() for part in raw]
    else:
        items = []
    seen: list[str] = []
    for item in items:
        lowered = item.lower()
        if lowered in VALID_PROXY_TAGS and lowered not in seen:
            seen.append(lowered)
    return seen


def proxy_tags_csv(tags: Any) -> str:
    """标签 → 落库形态（逗号串，按固定顺序，便于 diff）。"""
    parsed = parse_proxy_tags(tags)
    return ",".join(tag for tag in VALID_PROXY_TAGS if tag in parsed)


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
    #: 标签（见 `ProxyModel.tags` 的说明）。`register` = 可走注册链路，
    #: `bind` = 可绑定给账号。空列表 = 两个用途都不参与。
    tags: list[str] = field(default_factory=list)

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
            tags=parse_proxy_tags(getattr(model, "tags", "")),
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
            tags=parse_proxy_tags(record.get("tags")),
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
