"""account_manager - 多平台账号管理后台"""
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from core.db import init_db
from core.registry import load_all
from api.accounts import router as accounts_router
from api.tasks import router as tasks_router
from api.platforms import router as platforms_router
from api.proxies import router as proxies_router
from api.config import router as config_router
from api.actions import router as actions_router
from api.integrations import router as integrations_router
from api.auth import PANEL_AUTH_HEADERS, router as auth_router
from api.mail_imports import router as mail_imports_router
from api.outlook import router as outlook_router
from api.contribution import router as contribution_router
from api.icloud import router as icloud_router
from api.shared_mail import router as shared_mail_router
from api.sms import router as sms_router
from api.backup import router as backup_router

EXPECTED_CONDA_ENV = os.getenv("APP_CONDA_ENV", "account-manager")


def _detect_conda_env() -> str:
    conda_env = os.getenv("CONDA_DEFAULT_ENV")
    if conda_env:
        return conda_env

    prefix_parts = os.path.normpath(sys.prefix).split(os.sep)
    if "envs" in prefix_parts:
        idx = prefix_parts.index("envs")
        if idx + 1 < len(prefix_parts):
            return prefix_parts[idx + 1]
    return ""


def _read_git_version(root: str | None = None) -> str:
    """读一次磁盘上的 git 版本（短哈希；已跟踪文件有改动时带 `+dirty.<指纹>`）。

    两个坑（都实测过，见 `tests/test_runtime_version_stamp.py`）：

    1. **只算已跟踪文件的改动**（`--untracked-files=no`）。根目录长期有未跟踪
       目录（`.hermes/` 这类本地状态），若把它们算作脏，戳会被永久钉成
       `+dirty` —— 此后再改已跟踪文件戳也不变，`stale` 恒为 false，漏报的
       恰恰是这个端点要防的「改完代码没重启」。
    2. **脏时附改动内容的短指纹**。只标「脏/不脏」的话，「改了 → 启动 → 又改」
       两次都是同一个 `+dirty`，第二次改动仍被漏报。指纹取 `git diff HEAD`
       的哈希（含暂存与未暂存，不含未跟踪）。
    """
    import subprocess

    root = root or os.path.dirname(os.path.abspath(__file__))
    try:
        rev = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if rev.returncode != 0:
            return "未知"
        stamp = rev.stdout.strip()

        # 二进制安全：不解码成 str，避免 diff 里的非 UTF-8 字节抛 UnicodeDecodeError。
        # `git diff HEAD` 天然只覆盖已跟踪文件（暂存 + 未暂存），未跟踪文件不在内，
        # 这正是要的口径 —— 且 `git diff` 不接受 `--untracked-files`（实测 exit 129）。
        diff = subprocess.run(
            ["git", "-C", root, "diff", "HEAD", "--no-color"],
            capture_output=True, timeout=10,
        )
        if diff.returncode == 0 and diff.stdout.strip():
            import hashlib

            fingerprint = hashlib.sha256(diff.stdout).hexdigest()[:7]
            stamp += f"+dirty.{fingerprint}"
        return stamp
    except Exception:  # noqa: BLE001 - 版本打不出来不该挡住启动
        return "未知"


#: **进程启动那一刻**读到的代码版本。
#:
#: 必须在 import 期求值（模块顶层），不能每次请求现查 —— 现查报的是「此刻磁盘上
#: 的 HEAD」，不是「这个进程加载的代码」。实测踩过：进程 12:27 启动、12:39 才提交
#: 那版代码，端点却报出了 12:39 的哈希，看着像已经生效，实际跑的还是旧代码 ——
#: 正好是这个端点要防的误报，只是方向反了。
_BOOT_CODE_VERSION = _read_git_version()
_BOOT_AT = datetime.now(timezone.utc)


def _code_version() -> str:
    """这个进程**启动时**加载的代码版本。

    存在的理由：这个服务是手工拉起的（没有 systemd/supervisor），改完代码不重启
    就一直跑旧代码 —— 实测踩过：OTP 提取的修复 09:49 就提交了，但进程 08:41 起
    没重启过，之后 10:51 / 10:57 / 11:04 三次任务全在旧代码上跑，症状与修复前
    一模一样。把版本打出来，日志里一眼能对上「这版代码是什么时候的」。

    `/api/runtime` 还会返回磁盘上的**当前** HEAD（`disk_version`）—— 两者不一致
    就是「改完还没重启」，那正是这个端点最有用的时刻。
    """
    return _BOOT_CODE_VERSION


def _print_runtime_info() -> None:
    current_env = _detect_conda_env()
    print(f"[Runtime] Python: {sys.executable}")
    print(f"[Runtime] Conda Env: {current_env or '未检测到'}")
    print(f"[Runtime] 代码版本: {_code_version()}（改完代码必须重启进程才生效）")
    if EXPECTED_CONDA_ENV == "docker":
        return
    if current_env and current_env != EXPECTED_CONDA_ENV:
        print(
            f"[WARN] 当前环境为 '{current_env}'，推荐使用 '{EXPECTED_CONDA_ENV}' 启动，"
            "否则 Turnstile Solver 可能因依赖缺失而无法启动。"
        )
    elif not current_env:
        print(
            f"[WARN] 未检测到 conda 环境，推荐使用 '{EXPECTED_CONDA_ENV}' 启动，"
            "否则 Turnstile Solver 可能因依赖缺失而无法启动。"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 先装断管保护：服务常被 `... | tee` 或编辑器终端拉起，那个进程一走
    # stdout 读端就消失，之后每次 print 都抛 BrokenPipeError —— 任务线程里
    # 会把它当成注册失败，收尾路径再抛一次还会跳过 finish() 让任务卡在
    # running（见 core/console.py）。
    from core.console import guard_stdout

    guard_stdout()
    _print_runtime_info()
    # 先把旧布局的数据搬进 data/（见 core/paths.py），再初始化数据库：
    # 顺序反了会先建出一个空的 data/account_manager.db，迁移时反倒要处理冲突。
    from core.paths import DATA_DIR, migrate_legacy_paths
    for note in migrate_legacy_paths():
        print(f"[数据] {note}")
    print(f"[OK] 数据目录: {DATA_DIR}")
    init_db()
    load_all()
    print("[OK] 数据库初始化完成")
    # 绑 2FA 的密钥写前日志：绑定中途进程被杀、或 DB 写撞锁没写进去的，
    # 在这里补回库（服务端不下发第二次，不补就等于把号锁死）
    from services.chatgpt_two_factor import recover_pending_totp_secrets

    recovered = recover_pending_totp_secrets(log=lambda msg: print(msg))
    if recovered:
        print(f"[OK] 已从写前日志补写 {recovered} 个 2FA 密钥")
    from core.registry import list_platforms
    print(f"[OK] 已加载平台: {[p['name'] for p in list_platforms()]}")
    # 业务周期任务自注册（core.scheduler 不认识具体业务，见其模块 docstring）
    from core.scheduler import registered_jobs, scheduler
    print(f"[OK] 周期任务: {registered_jobs()}")
    scheduler.start()
    from services.solver_manager import start_async
    start_async()
    yield
    from core.scheduler import scheduler as _scheduler
    _scheduler.stop()
    from services.solver_manager import stop
    stop()


app = FastAPI(title="account-manager", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/auth/") or not path.startswith("/api/"):
        return await call_next(request)
    from core.config_store import config_store as _cs
    if not _cs.get("auth_password_hash", ""):
        return await call_next(request)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            {"detail": "未认证，请先登录"}, status_code=401, headers=PANEL_AUTH_HEADERS
        )
    try:
        from api.auth import verify_token
        verify_token(auth_header[7:])
    except HTTPException as e:
        return JSONResponse(
            {"detail": e.detail},
            status_code=e.status_code,
            headers=PANEL_AUTH_HEADERS if e.status_code == 401 else None,
        )
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(accounts_router, prefix="/api")
app.include_router(tasks_router, prefix="/api")
app.include_router(platforms_router, prefix="/api")
app.include_router(proxies_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(actions_router, prefix="/api")
app.include_router(integrations_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(mail_imports_router, prefix="/api")
app.include_router(outlook_router, prefix="/api")
app.include_router(contribution_router, prefix="/api")
app.include_router(icloud_router, prefix="/api")
app.include_router(sms_router, prefix="/api")
app.include_router(backup_router, prefix="/api")
# 隐私邮箱的免登录邮件页，不带 /api 前缀就绕开了鉴权中间件
app.include_router(shared_mail_router)


@app.get("/api/solver/status")
def solver_status():
    from services.solver_manager import is_running
    return {"running": is_running()}


@app.get("/api/runtime")
def runtime_info():
    """当前进程的运行信息。

    `code_version` 是**进程启动时加载的那版代码**（不是磁盘上的最新提交）；
    `disk_version` 是磁盘上此刻的 HEAD。两者不一致 = 改完代码还没重启 ——
    这个服务手工拉起、不重启就还跑旧代码（实测踩过：修复提交后没重启，之后三次
    任务全在旧代码上跑，症状与修复前一模一样）。

    `code_version` 刻意在 import 期算好（见 `_BOOT_CODE_VERSION`）：现查磁盘的话
    提交之后它会跟着变，看着像已经生效，把「没重启」这件事藏起来。
    """
    return {
        "code_version": _code_version(),
        "disk_version": _read_git_version(),
        "stale": _code_version() != _read_git_version(),
        "booted_at": _BOOT_AT.isoformat(),
        "pid": os.getpid(),
        "python": sys.executable,
    }


@app.post("/api/solver/restart")
def solver_restart():
    from services.solver_manager import stop, start_async
    stop()
    start_async()
    return {"message": "重启中"}


_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(_static_dir, "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        """SPA 兜底：前端路由（/accounts、/tasks…）刷新时要能拿到 index.html。

        **`/api/` 前缀必须排除**：不排除的话，任何拼错或已删除的 API 路径都会
        被这里兜住，返回 `200 + HTML` —— 调用方看到「成功」，然后在解析 JSON
        时炸掉，排查方向完全被带偏（实测：`/api/icloud/pool-summary` 这个不存在
        的路由返回 200 和整页 HTML）。API 命名空间下未命中的路径应当老老实实
        404，让错误立刻可见。
        """
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(os.path.join(_static_dir, "index.html"))


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload_enabled = os.getenv("APP_RELOAD", "0").lower() in {"1", "true", "yes"}
    uvicorn.run("main:app", host=host, port=port, reload=reload_enabled)
