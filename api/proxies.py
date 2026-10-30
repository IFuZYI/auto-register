from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlmodel import Session, select
from pydantic import BaseModel
from typing import Optional
from core.db import get_session, proxy_repository
from core.proxy_pool import proxy_pool

router = APIRouter(prefix="/proxies", tags=["proxies"])


class ProxyCreate(BaseModel):
    url: str
    region: str = ""
    #: `register`（可走注册链路）/ `bind`（可绑定给账号），见 `core.models.proxy`
    tags: list[str] = []


class ProxyBulkCreate(BaseModel):
    proxies: list[str]
    region: str = ""
    tags: list[str] = []


class ProxyTagsUpdate(BaseModel):
    tags: list[str] = []


class ProxyRebalanceRequest(BaseModel):
    #: 默认只预演 —— 均衡会改账号的绑定，误点一次就是 1000 个号换出口。
    dry_run: bool = True


class ProxyBatchDelete(BaseModel):
    ids: list[int]


@router.get("")
def list_proxies(session: Session = Depends(get_session)):
    return proxy_repository.list_all(session=session)


@router.post("")
def add_proxy(body: ProxyCreate, session: Session = Depends(get_session)):
    # 读写都走仓储 —— 只接写不接读会「写 JSON、读 SQLite」看不到刚写的行
    if proxy_repository.find_by_url(body.url, session=session):
        raise HTTPException(400, "代理已存在")
    return proxy_repository.add(
        url=body.url, region=body.region, tags=body.tags, session=session
    )


@router.post("/bulk")
def bulk_add_proxies(body: ProxyBulkCreate, session: Session = Depends(get_session)):
    added = 0
    for url in body.proxies:
        url = url.strip()
        if not url:
            continue
        existing = proxy_repository.find_by_url(url, session=session)
        if not existing:
            proxy_repository.add(
                url=url, region=body.region, tags=body.tags, session=session
            )
            added += 1
    return {"added": added}


@router.delete("/{proxy_id}")
def delete_proxy(proxy_id: int, session: Session = Depends(get_session)):
    found = proxy_repository.get_many([proxy_id], session=session)
    if not found:
        raise HTTPException(404, "代理不存在")
    proxy_repository.delete(found[0], session=session)
    return {"ok": True}


@router.post("/batch-delete")
def batch_delete_proxies(body: ProxyBatchDelete, session: Session = Depends(get_session)):
    if not body.ids:
        raise HTTPException(400, "代理 ID 列表不能为空")
    ids = list(dict.fromkeys(int(i) for i in body.ids))
    if len(ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 条代理")

    proxies = proxy_repository.get_many(ids, session=session)
    found_ids = {p.id for p in proxies if p.id is not None}
    for p in proxies:
        proxy_repository.delete(p, session=session)

    return {
        "deleted": len(found_ids),
        "not_found": [pid for pid in ids if pid not in found_ids],
        "total_requested": len(ids),
    }


@router.patch("/{proxy_id}/toggle")
def toggle_proxy(proxy_id: int, session: Session = Depends(get_session)):
    found = proxy_repository.get_many([proxy_id], session=session)
    if not found:
        raise HTTPException(404, "代理不存在")
    p = found[0]
    p.is_active = not p.is_active
    proxy_repository.save(p, session=session)
    return {"is_active": p.is_active}


@router.patch("/{proxy_id}/tags")
def update_proxy_tags(proxy_id: int, body: ProxyTagsUpdate, session: Session = Depends(get_session)):
    """设置代理标签（`register` / `bind`）。

    两个标签互相独立：一个代理可以既给注册用、也给账号绑（同一出口）。
    传空数组 = 两个用途都不参与，但它仍在池里（注册链路在没有带标签的代理时
    会回退到全部活跃代理）。
    """
    from core.models.proxy import parse_proxy_tags, proxy_tags_csv

    found = proxy_repository.get_many([proxy_id], session=session)
    if not found:
        raise HTTPException(404, "代理不存在")
    row = found[0]
    tags = parse_proxy_tags(body.tags)
    row.tags = tags
    proxy_repository.save(row, session=session)
    return {"id": proxy_id, "tags": tags, "tags_csv": proxy_tags_csv(tags)}


@router.get("/balance")
def proxy_balance(session: Session = Depends(get_session)):
    """每个代理绑定了多少账号（按平台拆分）+ 可绑定池概况。

    绑定关系在账号的 `extra_json["register_proxy"]` 里，这里把两边对起来：
    - `bound_total` / `bound_by_platform`：当前挂了几个号、各平台几个
    - `bindable`：该代理是否在可绑定池里（活跃 + 带 `bind` 标签）
    - `orphan_accounts`：绑定的代理已被删除的账号数（均衡要处理的那批）
    """
    from core.models.proxy import TAG_BIND, parse_proxy_tags
    from core.proxy_pool import proxy_pool
    from core.proxy_utils import normalize_proxy_url

    rows = proxy_repository.list_all(session=session)
    counts = proxy_pool.bound_counts()
    items = []
    known = set()
    for row in rows:
        url = getattr(row, "url", "")
        # 绑定计数按**归一化**的 URL 统计（`socks5://` 与 `socks5h://` 是同一个
        # 代理，`bound_counts` 就是这么归一的），所以这里查表也要用归一化的值，
        # 否则每条都查不到、全被算成「孤儿账号」（实测踩过）。
        normalized = normalize_proxy_url(url) or url
        known.add(normalized)
        stat = counts.get(normalized, {"total": 0, "platforms": {}})
        tags = parse_proxy_tags(getattr(row, "tags", ""))
        items.append(
            {
                "id": getattr(row, "id", None),
                "url": url,
                "region": getattr(row, "region", ""),
                "tags": tags,
                "is_active": bool(getattr(row, "is_active", True)),
                "bindable": bool(getattr(row, "is_active", True)) and TAG_BIND in tags,
                "bound_total": stat["total"],
                "bound_by_platform": stat["platforms"],
            }
        )
    orphans = {url: stat for url, stat in counts.items() if url not in known}
    return {
        "items": items,
        "bindable_count": sum(1 for item in items if item["bindable"]),
        "bound_accounts": sum(item["bound_total"] for item in items),
        "orphan_accounts": sum(stat["total"] for stat in orphans.values()),
        "orphan_by_proxy": orphans,
    }


@router.post("/balance")
def run_proxy_balance(body: ProxyRebalanceRequest):
    """把「绑定不合规」的账号均衡地挪到可绑定池。

    不合规 = 没绑定 / 绑定的代理没有 `bind` 标签 / 绑定的代理已删除。
    默认 `dry_run=true` 只返回会发生什么。
    """
    from core.proxy_pool import proxy_pool

    result = proxy_pool.rebalance(dry_run=body.dry_run)
    return {
        "dry_run": body.dry_run,
        "changed": len(result.get("changed", [])),
        "skipped": result.get("skipped", 0),
        "reason": result.get("reason", ""),
        "items": result.get("changed", [])[:200],
    }


@router.post("/check")
def check_proxies(background_tasks: BackgroundTasks):
    background_tasks.add_task(proxy_pool.check_all)
    return {"message": "检测任务已启动"}
