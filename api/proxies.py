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


class ProxyBulkCreate(BaseModel):
    proxies: list[str]
    region: str = ""


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
    return proxy_repository.add(url=body.url, region=body.region, session=session)


@router.post("/bulk")
def bulk_add_proxies(body: ProxyBulkCreate, session: Session = Depends(get_session)):
    added = 0
    for url in body.proxies:
        url = url.strip()
        if not url:
            continue
        existing = proxy_repository.find_by_url(url, session=session)
        if not existing:
            proxy_repository.add(url=url, region=body.region, session=session)
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


@router.post("/check")
def check_proxies(background_tasks: BackgroundTasks):
    background_tasks.add_task(proxy_pool.check_all)
    return {"message": "检测任务已启动"}
