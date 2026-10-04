"""定时任务调度 - 账号有效性检测、trial 到期提醒、可注册的周期任务。

设计
----
core 不能反向依赖 services/（见 docs/EXTENDING.md）。所以业务周期任务**不硬编码
在 core 里**，而是由 services/ 侧调用 `register_job(...)` 自注册进来；scheduler
只负责按间隔触发。

```python
# 在 services/ 下的任意模块末尾
from core.scheduler import register_job

register_job(
    "my_periodic_task",
    interval_seconds=lambda: get_my_task_interval_seconds(),
    runner=lambda: run_my_task(),
)
```

间隔函数返回 0 表示「当前不启用」，scheduler 会跳过该任务（不需要注销）。

当前状态：扩展点保留，但**尚无服务注册任务** —— 原 `cpa_maintenance`
（CPA 自动维护）已按需求删除。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .base_platform import Account, AccountStatus, RegisterConfig
from .db import account_repository
from .registry import get, load_all

# 周期任务：interval 返回秒数（0 = 本次不跑），runner 执行任务
JobInterval = Callable[[], int]
JobRunner = Callable[[], object]


@dataclass
class _Job:
    name: str
    interval: JobInterval
    runner: JobRunner
    last_run_at: float = field(default=0.0)

    def due(self, now: float) -> float:
        """到期则返回间隔秒数（>0），否则返回 0。

        用 `float` 而非 `int` 截断：`int(0.9)` 会变成 0，于是「亚秒间隔」被
        静默当成「本次不跑」（0 的语义是主动跳过），排查时完全看不出原因。
        interval 抛异常同样按 0 处理 —— 一个任务的配置错误不该拖垮整个调度循环。
        """
        try:
            interval = float(self.interval() or 0)
        except Exception:
            return 0.0
        if interval <= 0:
            return 0.0
        return interval if now - self.last_run_at >= interval else 0.0


_jobs: dict[str, _Job] = {}
_jobs_lock = threading.Lock()


def register_job(name: str, *, interval_seconds: JobInterval, runner: JobRunner) -> None:
    """注册一个周期任务（业务侧自注册，core 不认识具体业务）。

    同名重复注册直接覆盖，便于测试与热重载。
    """
    with _jobs_lock:
        _jobs[str(name)] = _Job(name=str(name), interval=interval_seconds, runner=runner)


def registered_jobs() -> list[str]:
    """已注册的周期任务名。"""
    with _jobs_lock:
        return sorted(_jobs)


class Scheduler:
    def __init__(self):
        self._running = False
        self._thread = None
        # 代次令牌：每次 start() 递增。循环线程记住自己那一代，
        # 醒来后若发现代次已变，直接退出——避免 stop() 后立刻 start() 时，
        # 还在 sleep 里的旧线程被新 _running=True「复活」，跑成两个循环。
        self._generation = 0
        self._stop_event = threading.Event()
        # 已「见过」的任务名：用于区分「启动前注册的」与「运行期新注册的」，
        # 后者第一次出现时也要把 last_run_at 顶到现在（不瞬间触发）。
        self._seen_jobs: set[str] = set()
        self._loop_interval_seconds = 60
        self._trial_check_interval_seconds = 3600
        self._last_trial_check_at = 0.0

    def start(self):
        if self._running:
            return
        self._running = True
        self._generation += 1
        my_generation = self._generation
        self._stop_event.clear()

        now = time.time()
        # 将上次执行时间设为当前时间，避免应用一启动就瞬间触发定时任务（如 CPA 自动注册）
        self._last_trial_check_at = now
        with _jobs_lock:
            self._seen_jobs = set(_jobs)
            for job in _jobs.values():
                job.last_run_at = now

        self._thread = threading.Thread(
            target=self._loop, args=(my_generation,), daemon=True
        )
        self._thread.start()
        print("[Scheduler] 已启动")

    def stop(self):
        """停止调度：置位停止信号并唤醒正在 sleep 的线程，然后 join。

        join 是必须的——只置 _running=False 的话，线程还要睡满一个
        _loop_interval_seconds（默认 60s）才退出；这期间若有人再 start()，
        旧线程醒来后会被新状态「复活」成第二个循环线程。
        """
        self._running = False
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self._loop_interval_seconds + 5)
        self._thread = None

    def _loop(self, generation: int):
        while self._running and generation == self._generation:
            now = time.time()
            if now - self._last_trial_check_at >= self._trial_check_interval_seconds:
                try:
                    self.check_trial_expiry()
                    self._last_trial_check_at = now
                except Exception as e:
                    print(f"[Scheduler] Trial 检查错误: {e}")

            self._run_due_jobs(now)

            # 用 Event.wait 代替 sleep：stop() 能立刻唤醒，不用等满整个周期
            if self._stop_event.wait(self._loop_interval_seconds):
                break

    def _run_due_jobs(self, now: float) -> None:
        with _jobs_lock:
            jobs = list(_jobs.values())
        for job in jobs:
            # 运行期新注册的任务：第一次见到时把 last_run_at 顶到现在，
            # 与 start() 里「不瞬间触发」的约定保持一致。
            # （register_job 在 start 之前调用时由 start() 统一初始化。）
            if job.name not in self._seen_jobs:
                self._seen_jobs.add(job.name)
                job.last_run_at = now
                continue
            interval = job.due(now)
            if not interval:
                continue
            try:
                job.runner()
                job.last_run_at = now
            except Exception as e:
                # 失败时**不**推进 last_run_at：下一次循环 tick（默认 60s）就重试，
                # 而不是等满一个 interval。这是刻意的重试策略——维护类任务
                # （如 CPA token 续期）遇到瞬态网络错误时应当尽快恢复，
                # 等一小时再试等于白等。代价是持续失败会按 tick 频率重试并打日志。
                print(f"[Scheduler] 周期任务 {job.name} 错误: {e}")

    def check_trial_expiry(self):
        """检查 trial 到期账号，更新状态（跨库）"""
        now = int(datetime.now(timezone.utc).timestamp())
        updated = 0
        # 分库后账号散落在各平台库：走仓储跨库列出，否则只看默认库会漏掉平台账号
        rows = account_repository.list_all_accounts(status="trial")
        for acc in rows:
            if acc.trial_end_time and acc.trial_end_time < now:
                acc.status = AccountStatus.EXPIRED.value
                acc.updated_at = datetime.now(timezone.utc)
                account_repository.upsert(acc)
                updated += 1
        if updated:
            print(f"[Scheduler] {updated} 个 trial 账号已到期")

    def check_accounts_valid(self, platform: str = None, limit: int = 50):
        """批量检测账号有效性（跨库）"""
        load_all()
        rows = account_repository.list_all_accounts(platform=platform or "")
        # 只测活活跃状态；「哪些状态算活跃」统一由 AccountStatus 定义，
        # 避免和仓储里的 is_registered 判断各写一份而漂移
        accounts = [r for r in rows if AccountStatus.is_active(r.status)][:limit]

        results = {"valid": 0, "invalid": 0, "error": 0}
        for acc in accounts:
            try:
                PlatformCls = get(acc.platform)
                plugin = PlatformCls(config=RegisterConfig())
                account_obj = Account(
                    platform=acc.platform,
                    email=acc.email,
                    password=acc.password,
                    user_id=acc.user_id,
                    region=acc.region,
                    token=acc.token,
                    extra=acc.get_extra(),
                )
                valid = plugin.check_valid(account_obj)
                if acc.id is not None:
                    fresh = account_repository.get(acc.id, platform=acc.platform)
                    if fresh:
                        if fresh.platform != "chatgpt":
                            fresh.status = (
                                fresh.status if valid else AccountStatus.INVALID.value
                            )
                        fresh.updated_at = datetime.now(timezone.utc)
                        account_repository.upsert(fresh)
                if valid:
                    results["valid"] += 1
                else:
                    results["invalid"] += 1
            except Exception:
                results["error"] += 1
        return results


scheduler = Scheduler()
