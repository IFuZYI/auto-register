"""Turnstile Solver 进程管理 - 后端启动时自动拉起"""
import subprocess
import sys
import os
import time
import threading
import requests

from core.paths import LOG_DIR

_proc: subprocess.Popen = None
_log_file = None
_lock = threading.Lock()


def _solver_enabled() -> bool:
    return os.getenv("APP_ENABLE_SOLVER", "1").lower() not in {"0", "false", "no"}


def _solver_threads() -> int:
    """解题浏览器池大小。

    不传 `--thread` 时 solver 自己的默认是 **4**，而每个 camoufox 实测约
    200MB：2 核 / 2G 的机器上等于 800MB，整机开始 swap，界面连带变卡
    （实测可用内存只剩 70MB、swap 用掉 1.6G）。

    默认按机器规格收敛，需要更多并发用 `SOLVER_THREADS` 显式抬：

    - 内存 < 4GB → 1 个（这种机器上并发解多个 Turnstile 没有意义）
    - 否则 → min(2, 核数)
    """
    raw = os.getenv("SOLVER_THREADS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    total_gb = 0.0
    try:
        total_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024**3)
    except (ValueError, OSError, AttributeError):
        pass
    if total_gb and total_gb < 4:
        return 1
    return max(1, min(2, os.cpu_count() or 1))


def _solver_port() -> int:
    return int(os.getenv("SOLVER_PORT", "8889"))


def _solver_url() -> str:
    return (os.getenv("LOCAL_SOLVER_URL") or f"http://127.0.0.1:{_solver_port()}").rstrip("/")


def _solver_bind_host() -> str:
    """Solver 监听地址，**默认只绑本机**。

    solver 的 HTTP 接口（`/turnstile`、`/result`）**没有任何鉴权**（安全审查
    2026-10-27 F10）。默认绑 `0.0.0.0` 时，任何人可让服务器用自己的出口 IP
    打开任意 URL（SSRF / 匿名代理），也能把浏览器池打满逼出空闲回收，把正在
    跑的注册任务拖垮 —— 一条绕过面板登录的拒绝服务路径。

    compose 与 systemd 都把端口显式映射到 127.0.0.1；裸 `python main.py`
    时靠这个默认值兜底。需要跨容器访问时显式设 `SOLVER_BIND_HOST=0.0.0.0`。
    """
    return os.getenv("SOLVER_BIND_HOST", "127.0.0.1")


def _solver_browser_type() -> str:
    return os.getenv("SOLVER_BROWSER_TYPE", "camoufox")


def _solver_proxy_enabled() -> bool:
    """是否让 solver 的解题浏览器也走代理。

    默认**开启**：Turnstile 判的就是 IP 信誉，注册任务走住宅代理而解题走机房
    IP 是自相矛盾的（实测：直连出口过不了 x.ai 的挑战，住宅代理能过）。
    没有可用代理时 solver 内部会回落到直连，所以开着不会有副作用。

    显式设 `SOLVER_PROXY_ENABLED=0` 可关掉。
    """
    return os.getenv("SOLVER_PROXY_ENABLED", "1").lower() not in {"0", "false", "no"}


def is_running() -> bool:
    try:
        r = requests.get(f"{_solver_url()}/", timeout=2)
        return r.status_code < 500
    except Exception:
        return False


def start():
    global _proc, _log_file
    with _lock:
        if not _solver_enabled():
            print("[Solver] 已禁用，跳过自动启动")
            return
        if is_running():
            print("[Solver] 已在运行")
            return
        solver_script = os.path.join(
            os.path.dirname(__file__), "turnstile_solver", "start.py"
        )
        # 日志写数据目录（见 core/paths.py），不和代码混在一起。
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = str(LOG_DIR / "solver.log")
        _log_file = open(log_path, "a", encoding="utf-8")
        cmd = [
            sys.executable,
            "-u",
            solver_script,
            "--browser_type",
            _solver_browser_type(),
            # 池大小：不传就是 solver 自己的默认 4 —— 在 2 核 / 2G 的机器上
            # 等于同时开 4 个 camoufox（每个 ~250MB），整机开始 swap，界面
            # 连带变卡（实测 load 1.1、可用内存只剩 87MB）。
            # 默认收到 min(2, 核数)，需要更多并发用 `SOLVER_THREADS` 显式抬。
            "--thread",
            str(_solver_threads()),
            "--host",
            _solver_bind_host(),
            "--port",
            str(_solver_port()),
        ]
        if _solver_proxy_enabled():
            # 让解题浏览器也走代理池（见 _solver_proxy_enabled 的说明）。
            # 代理本身由 solver 自己从环境变量/主库读取，这里只开开关。
            cmd.append("--proxy")
        _proc = subprocess.Popen(
            cmd,
            stdout=_log_file,
            stderr=subprocess.STDOUT,
        )
        # 等待服务就绪（最多30s）
        for _ in range(30):
            time.sleep(1)
            if is_running():
                print(f"[Solver] 已启动 PID={_proc.pid}")
                return
            if _proc.poll() is not None:
                print(f"[Solver] 启动失败，退出码={_proc.returncode}，日志: {log_path}")
                _proc = None
                if _log_file:
                    _log_file.close()
                    _log_file = None
                return
        print(f"[Solver] 启动超时，日志: {log_path}")


def stop():
    """停掉 solver 进程。

    `wait(timeout=5)` 会抛 `TimeoutExpired`，而本函数被 `main.lifespan` 的收尾
    直接调用 —— 异常会从 FastAPI 的 lifespan `__aexit__` 冒出来，把**任何一个**
    正在用 `TestClient(app)` 的测试判为失败，无论它在测什么（实测：测试里
    启动的 solver 要 30s 才拉起 4 个浏览器，5s 内杀不掉，于是
    `test_panel_sync_endpoint` 报 `TimeoutExpired` —— 与面板逻辑毫无关系）。

    收尾路径不能抛：`terminate` 后等到超时即降级 `kill`，再超时就放手 ——
    进程会被 init 回收，绝不把异常传给调用方的 lifespan。
    """
    global _proc, _log_file
    with _lock:
        if _proc and _proc.poll() is None:
            _proc.terminate()
            try:
                _proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # 浏览器还在收尾（camoufox 启动/退出都不快）：升级到 kill。
                _proc.kill()
                try:
                    _proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # 连 kill 都没收掉：放手，不要拿这个异常去炸 lifespan。
                    pass
            print("[Solver] 已停止")
        _proc = None
        if _log_file:
            _log_file.close()
            _log_file = None


def start_async():
    """在后台线程启动，不阻塞主进程。

    已禁用时**不建线程**：建了也只会跑一个 print，而那个 print 是在后台线程里
    写 stdout —— 测试期间 pytest 的捕获流被换掉/收尾后，从别的线程写它会直接
    段错误（实测：`APP_ENABLE_SOLVER=0` 跑全量，第 24 个测试进程 SIGSEGV，
    栈顶是 solver_manager.start 里的 print）。禁用路径不该有任何副作用。
    """
    if not _solver_enabled():
        print("[Solver] 已禁用，跳过自动启动")
        return
    t = threading.Thread(target=start, daemon=True)
    t.start()
