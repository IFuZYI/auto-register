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


def _solver_port() -> int:
    return int(os.getenv("SOLVER_PORT", "8889"))


def _solver_url() -> str:
    return (os.getenv("LOCAL_SOLVER_URL") or f"http://127.0.0.1:{_solver_port()}").rstrip("/")


def _solver_bind_host() -> str:
    return os.getenv("SOLVER_BIND_HOST", "0.0.0.0")


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
    global _proc, _log_file
    with _lock:
        if _proc and _proc.poll() is None:
            _proc.terminate()
            _proc.wait(timeout=5)
            print("[Solver] 已停止")
        _proc = None
        if _log_file:
            _log_file.close()
            _log_file = None


def start_async():
    """在后台线程启动，不阻塞主进程"""
    t = threading.Thread(target=start, daemon=True)
    t.start()
