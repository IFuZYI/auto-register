"""任务列表接口的负载：轮询不该全量拖历史任务的日志。

背景（实测）：`GET /api/tasks` 一次返回 253 个任务、415 KB，其中 **52% 是
每个任务的完整日志**。前端 `RunningTasks` 每 2.5 秒拉一次全量 —— 浏览器
主线程实测帧间隔被拖到 4–8 秒（正常 16ms），页面几乎卡死。

这些任务里 253/253 都是**已结束**的。轮询唯一需要的是「还在跑的那些」，
历史任务应该按需取（点开日志时再拿单条）。

这组测试钉住：
1. 默认响应不带历史任务的日志（体积降到十分之一以下）；
2. 仍在跑/等待的任务日志保留（实时日志面板要用）；
3. 显式要日志时仍能拿到（不能把功能删了）。
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def client():
    sys.path.insert(0, ROOT)
    import main  # noqa: PLC0415

    return TestClient(main.app)


def _make_finished_task(client, idx: int) -> str:
    """在**库里**造一条已结束的任务（带 30 条日志），返回 task id。

    走 `_upsert_task_run`（列表接口读的就是这张表）—— 内存 store 里的任务
    不会出现在列表里，用它造数据会让测试前置条件不成立。
    """
    from services.task_store_io import _upsert_task_run  # noqa: PLC0415

    tid = f"perf_finished_{idx}"
    _upsert_task_run({
        "id": tid,
        "platform": "grok",
        "source": "test",
        "status": "done",
        "total": 1,
        "success": 1,
        "logs": [f"[{i}] " + "x" * 200 for i in range(30)],
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    return tid


def _make_running_task(client, idx: int) -> str:
    """造一条**真正在跑**的任务：库里一行 + 内存 store 里也注册。

    只写库不够 —— `_finalize_orphan_tasks` 会把「库里有、内存里没有」的
    running 行判成孤儿并收尾成 stopped（那是重启后的清理逻辑），
    于是这条任务不再是 running，日志自然被裁掉。
    """
    from api import tasks as _api  # noqa: PLC0415
    from services.task_store_io import _upsert_task_run  # noqa: PLC0415

    tid = f"perf_running_{idx}"
    _api._task_store.create(tid, platform="grok", source="test", total=1)
    _api._task_store.mark_running(tid)
    _api._task_store.append_log(tid, "[1] hello")
    _upsert_task_run({
        "id": tid,
        "platform": "grok",
        "source": "test",
        "status": "running",
        "total": 1,
        "logs": ["[1] hello"],
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    return tid


def test_finished_task_logs_are_omitted_by_default(client):
    """历史任务的日志不进列表响应 —— 这是 415 KB → 十几 KB 的关键。"""
    tid = _make_finished_task(client, 1)
    try:
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        tasks = resp.json()
        row = next((t for t in tasks if t["id"] == tid), None)
        assert row is not None, "造的任务没出现在列表里"
        logs = row.get("logs") or []
        assert logs == [], (
            f"已结束任务的日志仍随列表下发（{len(logs)} 条）—— "
            "轮询时白白传输、前端还要渲染"
        )
    finally:
        client.delete(f"/api/tasks/{tid}")


def test_running_task_logs_are_kept(client):
    """还在跑的任务必须带日志（实时日志面板依赖它）。"""
    tid = _make_running_task(client, 1)
    try:
        tasks = client.get("/api/tasks").json()
        row = next((t for t in tasks if t["id"] == tid), None)
        assert row is not None
        assert row.get("logs"), "运行中的任务日志被吞了 —— 实时面板会空着"
    finally:
        # **不能**用 DELETE：运行中的任务会被 409 拒绝，行会一直留在库里，
        # 污染同进程的后续测试（复审实测）。先把内存 store 里的状态改掉，
        # 让它不再是「运行中」，再走正常删除。
        from api import tasks as _api  # noqa: PLC0415
        from services.task_store_io import _upsert_task_run  # noqa: PLC0415

        try:
            _api._task_store.finish(tid, status="stopped", success=0, skipped=0, errors=[])
        except Exception:
            pass
        try:
            _upsert_task_run({**{"id": tid}, "status": "stopped", "platform": "grok",
                              "source": "test", "total": 1, "logs": [],
                              "created_at": "2026-01-01T00:00:00+00:00",
                              "updated_at": "2026-01-01T00:00:00+00:00"})
        except Exception:
            pass
        client.delete(f"/api/tasks/{tid}")


def test_logs_can_be_requested_explicitly(client):
    """显式 `include_logs=1` 时仍给全量 —— 功能不能被删掉。"""
    tid = _make_finished_task(client, 2)
    try:
        tasks = client.get("/api/tasks?include_logs=1").json()
        row = next((t for t in tasks if t["id"] == tid), None)
        assert row is not None
        assert row.get("logs"), "显式要日志却没给"
    finally:
        client.delete(f"/api/tasks/{tid}")


def test_single_task_endpoint_still_returns_logs(client):
    """点开单条任务时日志仍可取（历史任务的日志入口）。"""
    tid = _make_finished_task(client, 3)
    try:
        resp = client.get(f"/api/tasks/{tid}")
        assert resp.status_code == 200
        assert resp.json().get("logs"), "单条任务接口丢了日志"
    finally:
        client.delete(f"/api/tasks/{tid}")
