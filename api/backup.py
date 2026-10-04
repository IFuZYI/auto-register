"""数据备份 / 迁移接口。

- ``GET /api/backup/export``：下载完整备份包（ZIP）
- ``POST /api/backup/import``：上传备份包并导入（原始字节体，非 multipart）
- ``GET /api/backup/info``：导出前预览（有哪些库、多大、能否导入）

为什么不用 ``UploadFile``（multipart）：那需要 ``python-multipart`` 依赖，
而本项目依赖面刻意保持窄（见 requirements.txt）。原始字节体 + 前端 fetch
把文件当 body 发即可，语义一样且少一个依赖。

安全：两个端点都在 ``/api/`` 下，走 main.py 的认证中间件 —— 与其它业务
接口同一道门（设了面板密码就要求登录）。
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from services.data_bundle import (
    MAX_BUNDLE_BYTES,
    BundleError,
    apply_import_bundle,
    build_export_bundle,
    collect_bundle_info,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backup", tags=["backup"])


@router.get("/export")
def export_bundle():
    """导出完整数据 + 配置，返回 ZIP 下载。"""
    try:
        payload, filename = build_export_bundle()
    except BundleError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 未知错误要给用户一句能懂的话
        logger.exception("导出失败")
        raise HTTPException(500, f"导出失败: {exc}") from exc

    # filename* 用 RFC 5987 编码，中文/特殊字符也能正确落盘
    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": disposition},
    )


@router.post("/import")
async def import_bundle(request: Request):
    """导入备份包。

    请求体是 ZIP 的**原始字节**（``Content-Type: application/zip``）。
    校验通过才落盘；落盘前自动备份当前数据（见 services/data_bundle.py）。

    重活（解压 / sha256 / 换库 / init_db）放线程池：本端点是 async，
    直接在事件循环里跑同步阻塞会**冻结整个进程**（uvicorn 单进程）——
    导入期间连登录态检查都停摆。
    """
    # 先按 Content-Length 预检：超过上限的请求体不读进内存直接拒。
    # 不预检的话 2GB 的包会先在内存里攒齐（join 时瞬时双份）再报错。
    declared = request.headers.get("content-length", "").strip()
    if declared.isdigit() and int(declared) > MAX_BUNDLE_BYTES:
        raise HTTPException(413, "导入包超过大小上限（2GB）—— 拒绝接收")

    body = await request.body()
    if not body:
        raise HTTPException(400, "请求体为空 —— 请以 ZIP 原始字节提交备份文件")

    filename = ""
    disposition = request.headers.get("X-Bundle-Filename", "")
    if disposition:
        filename = disposition[:200]

    try:
        result = await run_in_threadpool(apply_import_bundle, body, filename=filename)
    except BundleError as exc:
        # 运行中任务守卫按 409 报（前端与模块 docstring 都按 409 写），
        # 其余包问题 400。见 BundleError.status_code。
        raise HTTPException(getattr(exc, "status_code", 400), str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("导入失败")
        raise HTTPException(500, f"导入失败: {exc}") from exc

    return result


@router.get("/info")
def backup_info():
    """导出前预览：包里会有哪些库、各自多大、当前是否适合导入。"""
    try:
        return collect_bundle_info()
    except Exception as exc:  # noqa: BLE001 - 预览失败也要给可读文案
        logger.exception("读取数据概况失败")
        raise HTTPException(500, f"读取数据概况失败: {exc}") from exc
