"""account_manager - 多平台账号管理后台"""
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
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

    存在的理由：改完代码不重启，服务就一直跑旧代码（systemd 下需要
    `systemctl restart`，裸跑需要手动重启；`APP_RELOAD=1` 才有热重载）——
    实测踩过：OTP 提取的修复 09:49 就提交了，但进程 08:41 起
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
    from services import chatgpt_maintenance  # noqa: F401  注册自动维护任务
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


def _accepts_gzip(header: str) -> bool:
    """客户端是否接受 gzip（正确处理 q=0 与 `*`）。

    只做 `"gzip" in header` 是不够的：`gzip;q=0` 明确表示**不要** gzip，
    而 `*` 表示「什么都行」（含 gzip）。两者都会被朴素子串匹配判错。
    """
    text = str(header or "").lower()
    if not text:
        return False
    gzip_q = None
    star_q = None
    for part in text.split(","):
        token, _, params = part.strip().partition(";")
        token = token.strip()
        q = 1.0
        for param in params.split(";"):
            param = param.strip()
            if param.startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    q = 1.0
        if token == "gzip":
            gzip_q = q
        elif token == "*":
            star_q = q
    if gzip_q is not None:
        return gzip_q > 0
    if star_q is not None:
        return star_q > 0
    return False


def _etag_matches(headers_in, etag: str) -> bool:
    """`If-None-Match` 是否命中（支持逗号分隔与 `W/` 弱比较）。"""
    def _norm(value: str) -> str:
        return str(value or "").strip().removeprefix("W/").strip('"')

    target = _norm(etag)
    for key, value in headers_in:
        if key.lower() != b"if-none-match":
            continue
        for candidate in value.decode("latin-1").split(","):
            cand = candidate.strip()
            if cand == "*":
                return True
            if _norm(cand) == target:
                return True
    return False


class _GzipStatic(StaticFiles):
    """给静态资源加 gzip + 长缓存。

    **为什么不用 `GZipMiddleware`**：那个中间件按响应体大小决定是否压缩，
    对 1.5 MB 的包确实会压，但它压完仍标 `content-length` 为原长（Starlette
    的实现会丢掉长度），而且不区分「带哈希的 assets」与「入口 HTML」——
    后者绝不能长缓存（发版后用户会一直拿到旧资源名）。
    这里直接按请求的 `Accept-Encoding` 决定，并给 `assets/*` 打长缓存：
    文件名带内容哈希，内容变了 URL 就变，可以放心 `immutable`。

    **压缩只在启动时做一次**（`_precompress`），不在每个请求上压 —— 1.5 MB
    的 gzip 要几十毫秒，每次请求都算等于把 CPU 白送出去。
    """

    #: 小于这个尺寸不值得压（压了反而多一次解码）。
    MIN_SIZE = 1024

    #: 预压的总字节上限。启动时**串行**压 level-9，不设上限的话一个
    #: sourcemap 很多的构建会让 import 变成秒级停顿（且每个 worker 各付一次）。
    #: 超出的文件在首次请求时按需压（那时只压一个，代价可接受）。
    MAX_PRECOMPRESS_BYTES = 8 * 1024 * 1024

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #: rel path → (mtime_ns, size, gzip bytes)。存 mtime/size 是为了让缓存
        #: 能感知文件被替换 —— 见 `_gzip_for`。
        self._gzip_cache: dict[str, tuple[int, int, bytes]] = {}
        self._precompress()

    #: 不值得再压的格式（已经压缩过，再压只烧 CPU 与内存）。
    _SKIP_EXT = frozenset({
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
        ".woff", ".woff2", ".zip", ".gz", ".br", ".mp4", ".webm", ".pdf",
    })

    def _precompress(self) -> None:
        import gzip

        root = str(self.directory)
        budget = self.MAX_PRECOMPRESS_BYTES
        # 递归：`assets/` 今天是平的，但 Vite 完全可能产出子目录
        # （字体、图片分目录）。只扫一层会让子目录里的文件永远不被压缩。
        for dirpath, _dirnames, filenames in os.walk(root):
            if budget <= 0:
                break
            for name in filenames:
                path = os.path.join(dirpath, name)
                if os.path.splitext(name)[1].lower() in self._SKIP_EXT:
                    continue
                try:
                    if os.path.getsize(path) < self.MIN_SIZE:
                        continue
                    with open(path, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                # **按相对路径做 key**：只用 basename 的话，
                # `assets/x.js` 与 `assets/sub/x.js` 会互相串味
                # （复审实测：两个不同文件返回了同一份字节）。
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                self._gzip_cache[rel] = (
                    stat.st_mtime_ns, stat.st_size, gzip.compress(data, 9)
                )
                budget -= len(data)

    def _gzip_for(self, rel: str) -> bytes | None:
        """取某个相对路径的压缩体；文件变了就重压。

        为什么不能只在启动时压一次：`npm run build` 会**就地替换** `assets/`
        （vite `emptyOutDir: true`），文件名还带新 hash。缓存不失效的话，
        重建后不重启服务就拿不到新变体 —— 用户下到的是未压缩文件（体积差 3 倍）。
        实测这个现象复发过 2 次（测试跑到一半构建前端时 6 条 gzip 用例变红）。

        失效判据用 `(mtime_ns, size)`：mtime 能覆盖「同名替换」，size 作为
        兜底（同一秒内的替换 mtime 可能相同）。文件没变时返回**同一个**缓存
        对象 —— 每次请求都重压等于把 CPU 白送出去。
        """
        import gzip
        import os as _os

        if _os.path.splitext(rel)[1].lower() in self._SKIP_EXT:
            return None
        path = _os.path.join(str(self.directory), rel)
        try:
            stat = _os.stat(path)
            if stat.st_size < self.MIN_SIZE:
                return None
        except OSError:
            return None

        key = (stat.st_mtime_ns, stat.st_size)
        entry = self._gzip_cache.get(rel)
        if entry is not None and entry[0] == key[0] and entry[1] == key[1]:
            return entry[2]

        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        compressed = gzip.compress(data, 9)
        self._gzip_cache[rel] = (key[0], key[1], compressed)
        return compressed

    async def get_response(self, path: str, scope):  # noqa: D102 - Starlette 钩子
        from starlette.responses import Response

        resp = await super().get_response(path, scope)
        if resp.status_code == 304:
            # Starlette 的 `NotModifiedResponse` 只复制固定几个头 ——
            # 这里补回缓存策略与变体标记，否则共享缓存无法区分
            # 「gzip 体」与「原始体」（复审实测：304 只带 `vary: Origin`）。
            resp.headers["cache-control"] = "public, max-age=31536000, immutable"
            resp.headers["vary"] = "Accept-Encoding"
            return resp
        if resp.status_code != 200:
            return resp

        # 带内容哈希 → 可以永久缓存。
        # `Vary` 必须**两个变体都带**：只标在 gzip 分支上的话，先被缓存住的
        # identity 体可能被发给支持 gzip 的客户端（反之亦然）。
        resp.headers["cache-control"] = "public, max-age=31536000, immutable"
        resp.headers["vary"] = "Accept-Encoding"

        # `Range` / `HEAD` / 条件请求交给 `StaticFiles` 自己处理（它支持 206、
        # 304、`If-Range`）。我们**只在完整 GET 且客户端要 gzip 时才替换响应**：
        # 字节区间是按原始文件算的，压过的 body 与 `content-range` 对不上，
        # 客户端会解压失败（实测：`Range: bytes=0-99` 返回 206 + 原始字节 +
        # `content-encoding: gzip`）。
        if scope.get("method") not in ("GET", None):
            return resp
        headers_in = scope.get("headers") or []
        if any(k.lower() == b"range" for k, _ in headers_in):
            return resp

        rel = str(path).replace(os.sep, "/").lstrip("/")
        compressed = self._gzip_for(rel)
        if compressed is None:
            # 小文件、已压缩格式或读不到（目录/不存在）—— 不压，保持原响应。
            return resp

        accept = ""
        for key, value in headers_in:
            if key.lower() == b"accept-encoding":
                accept = value.decode("latin-1")
                break
        if not _accepts_gzip(accept):
            return resp

        # 直接用压缩体构造响应：`FileResponse` 是流式的，拿不到 `body`，
        # 在它上面改头改不了实际传输内容（实测：加了 content-encoding 但
        # content-length 仍是原长，客户端会解析失败）。
        #
        # ETag 必须按**变体**区分（gzip / 原始），否则共享缓存可能把 gzip 体
        # 发给只支持 identity 的客户端。
        etag = f'W/"{rel}-gzip"'
        if _etag_matches(headers_in, etag):
            return Response(
                status_code=304,
                headers={
                    "cache-control": "public, max-age=31536000, immutable",
                    "vary": "Accept-Encoding",
                    "etag": etag,
                },
            )
        return Response(
            content=compressed,
            status_code=200,
            headers={
                "cache-control": "public, max-age=31536000, immutable",
                "content-encoding": "gzip",
                "vary": "Accept-Encoding",
                "etag": etag,
            },
            media_type=resp.media_type,
        )


if os.path.isdir(_static_dir):
    app.mount(
        "/assets",
        _GzipStatic(directory=os.path.join(_static_dir, "assets")),
        name="assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str, request: Request):
        """SPA 兜底：前端路由（/accounts、/tasks…）刷新时要能拿到 index.html。

        **`/api/` 前缀必须排除**：不排除的话，任何拼错或已删除的 API 路径都会
        被这里兜住，返回 `200 + HTML` —— 调用方看到「成功」，然后在解析 JSON
        时炸掉，排查方向完全被带偏（实测：`/api/icloud/pool-summary` 这个不存在
        的路由返回 200 和整页 HTML）。API 命名空间下未命中的路径应当老老实实
        404，让错误立刻可见。
        """
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # 入口 HTML **不能**长缓存：它引用的资源名带哈希，缓存住就等于把用户
        # 钉在旧版本上（发版后一直白屏/报错）。`no-cache` = 每次校验 ETag。
        #
        # 这里显式处理条件请求：`FileResponse` 自己不做 `If-None-Match` 比对，
        # 直接返回它的话 `no-cache` 会退化成「每次都传整页」（复审实测：
        # 回传正确 ETag 仍得到 200）。
        index_path = os.path.join(_static_dir, "index.html")
        try:
            stat = os.stat(index_path)
        except OSError:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
        inm = ""
        for key, value in request.headers.items():
            if key.lower() == "if-none-match":
                inm = value
                break
        if inm and any(
            candidate.strip().removeprefix("W/").strip('"') == etag.strip('"')
            for candidate in inm.split(",")
        ):
            return Response(
                status_code=304,
                headers={"cache-control": "no-cache", "etag": etag},
            )
        return FileResponse(index_path, headers={"cache-control": "no-cache", "etag": etag})


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload_enabled = os.getenv("APP_RELOAD", "0").lower() in {"1", "true", "yes"}
    uvicorn.run("main:app", host=host, port=port, reload=reload_enabled)
