"""把「哪些表在哪个后端」讲清楚 —— 启动时打一次。

## 为什么要有它

JSON 后端目前覆盖 **5/7 张表**，剩下的 iCloud / 邮箱池仍在 SQLite ——
于是设了 `STORAGE_BACKEND=json` 的部署**照样会建 `account_manager.db`**。

不把这个说出来，用户会以为「设了 json 就不该有 .db 文件」，然后去查一个
不存在的问题。文档里写了，但**启动日志才是他会看到的地方**。

## 为什么邮箱池刻意不接

那些表的查询含 `OR` 状态条件、`func.lower(email)` 归一、账号类型匹配 ——
JSON 没有 SQL（见 `core/storage/__init__.py` 的说明），移植就得**在 Python
里重写一遍同样的规则**。两份定义必然漂移（本仓已因同类问题踩过多次，
例如 `_export_names` 手写清单漏了 sso），所以留到「有查询层」时再做，
而不是先接上再说。

这份报告是那个决定的**可见后果**：用户能看到边界在哪，而不是自己猜。
"""

from __future__ import annotations

from typing import Optional

#: 走 JSON 白名单的表（`JSON_TABLES`）之外的、需要单独说明的领域。
#: 账号与配置不走白名单 —— 它们由 `AccountRepository` / `ConfigStore`
#: 自己的分派处理，但同样属于「JSON 覆盖」。
_EXTRA_JSON_DOMAINS = (
    ("配置", "config.json"),
    ("账号", "accounts.json"),
)

#: 仍留在 SQLite 的表，以及为什么。
#:
#: **这张表必须与 `JSON_TABLES` 保持一致** —— 实测踩过：iCloud 两张表接完
#: 后忘了更新这里，启动日志同时说「已走 json: icloud_accounts」和
#: 「仍走 sqlite: icloud_accounts」，自相矛盾。
_SQLITE_ONLY: tuple[tuple[str, str], ...] = (
    (
        "outlook_accounts",
        "取号查询含 OR + lower() + 账号类型匹配；谓词层已能表达（core/storage/query.py），"
        "但 26 个访问点尚未改造",
    ),
)


def describe_coverage(backend: str) -> list[str]:
    """返回几行可直接打印的说明。"""
    name = str(backend or "").strip().lower()
    if name != "json":
        return [f"[storage] 存储后端: {name} —— 全部表都在 {name}"]

    from core.storage.json_table import JSON_TABLES

    lines = ["[storage] 存储后端: json"]
    covered = ", ".join(f"{label}（{file}）" for label, file in _EXTRA_JSON_DOMAINS)
    if JSON_TABLES:
        covered += "、" + "、".join(f"{t}（{t}.json）" for t in sorted(JSON_TABLES))
    lines.append(f"[storage]   已走 json: {covered}")

    if _SQLITE_ONLY:
        detail = "、".join(f"{table}" for table, _why in _SQLITE_ONLY)
        lines.append(
            f"[storage]   仍走 sqlite: {detail} —— 所以**会**建 account_manager.db，"
            f"这是已知状态不是故障"
        )
        # 只把第一张表的理由展开，避免日志过长
        lines.append(f"[storage]   原因: {_SQLITE_ONLY[0][1]}")
    return lines


__all__ = ["describe_coverage"]
