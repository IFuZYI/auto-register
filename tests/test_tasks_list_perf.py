"""任务列表接口的负载：轮询不该全量拖历史任务的日志。

背景（实测）：`GET /api/tasks` 一次返回 253 个任务、415 KB，其中 **52% 是
每个任务的完整日志**。前端 `RunningTasks` 每 2.5 秒拉一次全量 —— 浏览器
主线程实测帧间隔被拖到 4–8 秒（正常 16ms），页面几乎卡死。

这些任务里 253/253 都是**已结束**的。轮询唯一需要的是「还在跑的那些」，
历史任务应该按需取（点开日志时再拿单条）。

第二轮实测：日志裁掉后仍有 **5.5–6.1 MB**，其中 `errors` 占 **4.49 MB（96%）**
（715 个任务、运行中 0 个，最大单条 37 KB）。同一个开关（`include_logs`）
一并管住 `errors` —— 两个开关组合起来是四种语义，没人记得住。

这组测试钉住：
1. 默认响应不带历史任务的日志；
2. 仍在跑/等待的任务日志保留（实时日志面板要用）；
3. 显式要日志时仍能拿到（不能把功能删了）；
4. 默认响应不带历史任务的 `errors`（列表里没人读它）；
5. 仍在跑的任务 `errors` 保留（失败原因要实时看到）；
6. 显式要全量时 `errors` 也回来；
7. 单条端点始终全量（列表裁掉的字段在详情里补回来）；
8. 负载量级：一批历史 `errors` 不随列表下发。
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


def _make_finished_task(client, idx: int, error_count: int = 3, error_size: int = 200) -> str:
    """在**库里**造一条已结束的任务（带 30 条日志 + `error_count` 条错误），返回 task id。

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
        "errors": [f"[{i}] " + "y" * error_size for i in range(error_count)],
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
        "errors": ["[1] 运行中也要能看到失败原因"],
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    return tid


def _cleanup_running_task(client, tid: str) -> None:
    """收掉「运行中」的测试任务。

    **不能**直接 DELETE：运行中的任务会被 409 拒绝，行会一直留在库里，
    污染同进程的后续测试（复审实测）。先把内存 store 里的状态改掉，
    让它不再是「运行中」，再走正常删除。
    """
    from api import tasks as _api  # noqa: PLC0415
    from services.task_store_io import _upsert_task_run  # noqa: PLC0415

    try:
        _api._task_store.finish(tid, status="stopped", success=0, skipped=0, errors=[])
    except Exception:
        pass
    try:
        _upsert_task_run({"id": tid, "status": "stopped", "platform": "grok",
                          "source": "test", "total": 1, "logs": [],
                          "created_at": "2026-01-01T00:00:00+00:00",
                          "updated_at": "2026-01-01T00:00:00+00:00"})
    except Exception:
        pass
    client.delete(f"/api/tasks/{tid}")


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
        _cleanup_running_task(client, tid)


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


# ---------------------------------------------------------------- errors
#
# 第二轮实测：日志裁掉后 `/api/tasks` 仍有 5.5–6.1 MB，其中 `errors` 占
# 4.49 MB（96%，最大单条 37 KB），715 个任务里运行中 0 个 —— 全是历史任务的
# 失败明细。列表页只用一个 `errors.length` 显示「✗ 失败 N」，明细在单条详情里看。


def test_finished_task_errors_are_omitted_by_default(client):
    """历史任务的 `errors` 不进列表 —— 这是 5.5 MB → <1 MB 的关键。"""
    tid = _make_finished_task(client, 4, error_count=4)
    try:
        tasks = client.get("/api/tasks").json()
        row = next((t for t in tasks if t["id"] == tid), None)
        assert row is not None, "造的任务没出现在列表里"
        errors = row.get("errors") or []
        assert errors == [], (
            f"已结束任务的 errors 仍随列表下发（{len(errors)} 条）—— "
            "轮询时白白传输 4.49 MB"
        )
    finally:
        client.delete(f"/api/tasks/{tid}")


def test_running_task_errors_are_kept(client):
    """运行中的任务必须带 `errors`：失败原因要能实时看到。"""
    tid = _make_running_task(client, 2)
    try:
        tasks = client.get("/api/tasks").json()
        row = next((t for t in tasks if t["id"] == tid), None)
        assert row is not None
        assert row.get("errors"), "运行中任务的 errors 被吞了 —— 失败原因看不到"
    finally:
        _cleanup_running_task(client, tid)


def test_errors_can_be_requested_explicitly(client):
    """显式 `include_logs=1` 时 `errors` 也给全量 —— 一个开关同时管两者。"""
    tid = _make_finished_task(client, 5, error_count=4)
    try:
        tasks = client.get("/api/tasks?include_logs=1").json()
        row = next((t for t in tasks if t["id"] == tid), None)
        assert row is not None
        assert len(row.get("errors") or []) == 4, "显式要全量却裁掉了 errors"
    finally:
        client.delete(f"/api/tasks/{tid}")


def test_single_task_endpoint_still_returns_errors(client):
    """点开单条任务时 `errors` 仍可取（列表裁掉的明细在这里补回来）。"""
    tid = _make_finished_task(client, 6, error_count=4)
    try:
        resp = client.get(f"/api/tasks/{tid}")
        assert resp.status_code == 200
        assert len(resp.json().get("errors") or []) == 4, "单条任务接口丢了 errors"
    finally:
        client.delete(f"/api/tasks/{tid}")


def test_list_payload_stays_under_one_megabyte(client):
    """一批历史任务的 `errors` 不随列表下发 —— 负载量级必须降下来。

    造 40 个已结束任务、每个 10 条 1200 字符的错误（合计约 480 KB）。列表响应
    只该为它们多出「一行元数据」的量级（几十 KB），不该把错误正文拖上来；
    同时用 `?include_logs=1` 确认这批错误**真的进了库** —— 否则「没多出来」
    可能只是没造出数据。
    """
    baseline_bytes = len(client.get("/api/tasks").content)
    tids = [_make_finished_task(client, 100 + i, error_count=10, error_size=1200)
            for i in range(40)]
    try:
        default = client.get("/api/tasks")
        full = client.get("/api/tasks?include_logs=1")
        assert default.status_code == 200 and full.status_code == 200

        # 前置条件：错误确实在库里（全量响应能看见 10 条）
        full_row = next((t for t in full.json() if t["id"] == tids[0]), None)
        assert full_row is not None, "造的任务没出现在列表里"
        assert len(full_row.get("errors") or []) == 10, "造的错误没进库，用例白跑"

        default_bytes = len(default.content)
        assert default_bytes < 1_000_000, (
            f"默认列表仍有 {default_bytes} 字节（> 1 MB）"
        )
        growth = default_bytes - baseline_bytes
        assert growth < 100_000, (
            f"40 个历史任务让默认列表多出 {growth} 字节 —— errors 仍随列表下发"
            f"（这批错误正文约 480 KB）"
        )
    finally:
        for tid in tids:
            client.delete(f"/api/tasks/{tid}")



# ---------------------------------------------------- SQL 层不取大字段
#
# 上面两条只省了**传输**：763 行的 `logs_json` / `errors_json` 仍被
# `SELECT *` 读进内存再在 Python 里清空 —— 实测 SELECT 86–518ms、
# 序列化再 200–320ms。改成 SQL 层只取轻量列后：42–82ms（快 6–10 倍）。


def test_list_query_does_not_read_heavy_columns(client):
    """列表查询不该把 `logs_json` / `errors_json` 读进内存。

    用 SQL 语句记录器验证，而不是看耗时 —— 耗时在 CI 上不稳定，但「有没有
    查这两列」是确定的事实。
    """
    from sqlalchemy import event
    from core.db import current_engine

    statements: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    engine = current_engine()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        client.get("/api/tasks")
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    touched = [s for s in statements if "task_runs" in s]
    assert touched, "没抓到 task_runs 的查询语句（记录器没生效）"
    # 运行中/等待中的任务**允许**读大字段（实时面板要明细），所以只看
    # 「不带状态过滤」的那条 —— 它取的是全部历史任务，763 行里 99% 是已结束的。
    unfiltered = [
        s for s in touched
        if "logs_json" in s or "errors_json" in s
    ]
    heavy_all = [s for s in unfiltered if "WHERE" not in s.upper()]
    assert not heavy_all, (
        "全量列表查询仍 SELECT 了 logs_json / errors_json —— 763 行的大字段被"
        f"读进内存后才清空，白花 6–10 倍时间: {heavy_all[:1]}"
    )


def test_list_with_logs_still_reads_heavy_columns(client):
    """`?include_logs=1` 必须仍然读大字段 —— 不能为了快把它整个丢了。"""
    tid = _make_finished_task(client, 7, error_count=3)
    try:
        tasks = client.get("/api/tasks?include_logs=1").json()
        row = next((t for t in tasks if t["id"] == tid), None)
        assert row is not None
        assert len(row.get("logs") or []) == 30, "include_logs=1 时日志丢了"
        assert len(row.get("errors") or []) == 3, "include_logs=1 时错误丢了"
    finally:
        client.delete(f"/api/tasks/{tid}")
