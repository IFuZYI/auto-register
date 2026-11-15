"""上游熔断：同一个远程服务连续失败后，短暂拒绝新的收件请求。

为什么需要它
------------
2026-11-13 实测：`icloud-hme` 读超时，200 个账号的任务里每个 worker 都各自
发一次请求、各自等满 30s、各自判失败 —— 一次服务故障变成 142 条失败记录，
每条都要白等一个超时。

熔断把「这个服务现在不通」记一次，后续 worker 在**发请求前**就拿到快速的
可重试失败：不白等、也不给已经过载的服务继续加压。

与重试的关系
------------
熔断**不代替**重试，两者管不同的事：

- 重试（`services/task_runners.py`）管「这个账号要不要换个会话再来一轮」；
- 熔断管「现在这个服务值不值得发请求」。

所以熔断打开时抛的是**可重试**的 `UpstreamBusy`：这一轮快速失败、地址放回
池子、不烧重试轮。恢复后同一个地址还能正常用。

设计约束
--------
- **进程内状态**。本项目强制单后端进程（docs/MAINTENANCE.md §1.6），与
  iCloud 取号锁（`services/icloud/_locks.py`）、Kiro 来源认领同一口径，
  不引 DB 表、不加迁移。
- **按服务地址分键**。挂在 A 服务不该影响 B 服务。
- **冷却指数退避但封顶**。服务长挂时不是无限延长冷却，而是停在上限，
  这样恢复后不必再等半分钟才肯试一次。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

#: 连续失败多少次打开熔断。
#:
#: 选 5 而不是 1：单个 worker 撞上一次网络抖动不该让整个任务停摆；但
#: 5 个 worker 都撞上就足以说明是服务问题而不是偶发。
DEFAULT_THRESHOLD = 5

#: 首次冷却时长（秒）。与一次读超时（20s）同量级：服务真挂时，
#: 冷却期内不该有请求，而恢复后最多等这么久就开始试探。
DEFAULT_COOLDOWN_SECONDS = 60.0

#: 冷却上限（秒）。连续失败会翻倍退避，但停在这里。
DEFAULT_MAX_COOLDOWN_SECONDS = 300.0


class UpstreamBusy(RuntimeError):
    """熔断打开期间拒绝请求。

    继承 `RuntimeError` 而不是 `ICloudError`：它**不是**上游返回的错误，
    而是本地保护机制的判断。上层仍按「可重试失败」处理 ——
    地址没被消耗，服务恢复后还能用。
    """

    def __init__(self, key: str, remaining: float):
        self.key = key
        self.remaining = max(float(remaining), 0.0)
        super().__init__(
            f"上游服务 {key or '(默认)'} 暂不可用（熔断中，约 {self.remaining:.0f}s 后重试）"
        )


@dataclass
class _GateState:
    failures: int = 0
    opened_at: float = 0.0
    #: 本次冷却时长（指数退避后的值）
    cooldown: float = 0.0
    #: 最近一次失败的时间（用来判断「这些主号是不是同时坏的」）。
    failed_at: float = 0.0


@dataclass
class UpstreamGate:
    """按 key 分组的上游熔断器（线程安全）。

    两级粒度（2026-11-14 事故的直接产物）
    ------------------------------------
    `key` 是**服务**（`base_url`），主号级记账用 `key + account` 二级键。

    原先所有请求共用一个 `base_url` 键，于是「一个主号读不动」被记成「整个
    服务挂了」：4 个远程主号共用一台 icloud-hme，一个信箱的读超时把熔断顶开，
    另外 3 个健康主号在 9 分钟里被拒了 212 次。

    分两档的必要性来自两类故障本身就不同：
    - **主号级**（读超时、这个信箱的 IMAP 慢）：只影响这个主号，别的信箱好好的
      → 只挡这个主号；
    - **服务级**（连接被拒、连接被断，或**多个不同主号**同一窗口内都失败）：
      说明整台服务不值得试 → 所有主号一起挡。

    服务级判定见 `check()`：先看服务级记账，再看该主号的。
    """

    threshold: int = DEFAULT_THRESHOLD
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    max_cooldown_seconds: float = DEFAULT_MAX_COOLDOWN_SECONDS
    #: 多少个**不同主号**在同一窗口内失败，就升级成服务级熔断。
    account_threshold_for_service: int = 3
    #: 只统计这个窗口内的主号失败（秒）。超出窗口的旧失败不算「同时坏」。
    account_failure_window_seconds: float = 120.0
    clock: "callable" = field(default=time.monotonic, repr=False)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _states: dict[str, _GateState] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.threshold = max(int(self.threshold or 1), 1)
        self.cooldown_seconds = max(float(self.cooldown_seconds or 0), 0.0)
        self.max_cooldown_seconds = max(
            float(self.max_cooldown_seconds or 0), self.cooldown_seconds
        )

    # ---------------------------------------------------------------- 查询

    def _state(self, key: str) -> _GateState:
        return self._states.setdefault(str(key or ""), _GateState())

    def remaining_cooldown(self, key: str) -> float:
        """返回该 key 剩余的冷却秒数；未熔断返回 0。"""
        now = self.clock()
        with self._lock:
            state = self._state(key)
            if not state.opened_at:
                return 0.0
            elapsed = now - state.opened_at
            remaining = state.cooldown - elapsed
            return max(remaining, 0.0)

    def check(self, key: str, account: str = "") -> None:
        """熔断中则抛 `UpstreamBusy`；否则放行。

        先查服务级，再查该主号级 —— 服务整体不可用时不该靠主号键漏过去。
        `account` 为空时只看服务级（老调用点保持原行为）。
        """
        remaining = self.remaining_cooldown(key)
        if remaining <= 0 and account:
            remaining = self.remaining_cooldown(self._account_key(key, account))
        if remaining > 0:
            raise UpstreamBusy(key, remaining)

    def failure_count(self, key: str, account: str = "") -> int:
        with self._lock:
            return self._state(self._account_key(key, account) if account else key).failures

    @staticmethod
    def _account_key(key: str, account: str) -> str:
        """主号级记账的二级键。用 `#` 分隔：URL 里不会出现它，不会撞键。"""
        return f"{key or ''}#{str(account or '').strip()}"

    # ---------------------------------------------------------------- 记账

    def note_failure(self, key: str) -> None:
        """记一次**服务级**失败；达到阈值就打开熔断并按次退避。"""
        with self._lock:
            self._record_failure(self._state(key), self.clock())

    def _record_failure(self, state: "_GateState", now: float) -> None:
        """失败记账的唯一实现（服务级与主号级共用同一套阈值/退避口径）。"""
        state.failures += 1
        state.failed_at = now
        if state.opened_at and now - state.opened_at < state.cooldown:
            # 冷却期内又失败：延长这次冷却（指数退避到上限）
            state.cooldown = min(
                max(state.cooldown * 2, self.cooldown_seconds),
                self.max_cooldown_seconds,
            )
            state.opened_at = now
            return
        if state.failures >= self.threshold:
            state.cooldown = self.cooldown_seconds
            state.opened_at = now

    def note_account_failure(self, service: str, account: str) -> None:
        """记一次**主号级**失败；同一服务上多个主号都失败则升级为服务级。

        升级的意义：单个主号超时只挡自己；但如果 3 个不同主号在 2 分钟内都
        读不动，那大概是服务本身有问题（CPU 打满、IMAP 网关拥塞），继续让
        其余主号各自陪跑一个读超时没有任何收益。
        """
        now = self.clock()
        with self._lock:
            self._record_failure(self._state(self._account_key(service, account)), now)
            service_state = self._state(service)
            # 只数**最近**失败的那些主号：半小时前的一批旧失败不该把服务钉死。
            active = {
                key
                for key, state in self._states.items()
                if key.startswith(f"{service or ''}#")
                and state.failed_at
                and now - state.failed_at <= self.account_failure_window_seconds
            }
            if len(active) >= self.account_threshold_for_service:
                if not service_state.opened_at:
                    service_state.cooldown = self.cooldown_seconds
                    service_state.opened_at = now

    def note_account_success(self, service: str, account: str) -> None:
        """主号级成功：只清这个主号的记账。"""
        with self._lock:
            state = self._states.get(self._account_key(service, account))
            if state is not None:
                state.failures = 0
                state.opened_at = 0.0
                state.cooldown = 0.0
                state.failed_at = 0.0

    def note_success(self, key: str) -> None:
        """一次成功即清零：服务恢复了，没必要留着上次的失败记账。"""
        with self._lock:
            state = self._state(key)
            state.failures = 0
            state.opened_at = 0.0
            state.cooldown = 0.0

    def reset(self, key: str | None = None) -> None:
        """清掉一个 key（或全部）的记账。测试与「手动恢复」用。"""
        with self._lock:
            if key is None:
                self._states.clear()
            else:
                self._states.pop(str(key or ""), None)

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        """可放进任务日志的状态快照（只含基本类型）。"""
        now = self.clock()
        with self._lock:
            return {
                key: {
                    "failures": state.failures,
                    "cooldown": round(state.cooldown, 1),
                    "remaining": round(
                        max(state.cooldown - (now - state.opened_at), 0.0)
                        if state.opened_at
                        else 0.0,
                        1,
                    ),
                }
                for key, state in self._states.items()
            }


#: 进程内单例：所有 iCloud 收件通道共用一份熔断状态。
#:
#: 为什么必须是单例：熔断的意义在于**跨 worker 共享**「服务挂了」这个事实。
#: 每个 client 各建一个实例的话，5 个并发 worker 就各自撞一次，等于没做。
_GLOBAL_GATE = UpstreamGate()


def global_gate() -> UpstreamGate:
    return _GLOBAL_GATE


def is_upstream_busy(exc: BaseException) -> bool:
    """这个异常是否表示「熔断打开，服务现在不值得试」。

    用途：容错循环里要把它和「普通暂时失败」区分开 —— 后者值得再等一个
    轮询周期，前者不值得（再等也只会被同一条熔断再拒一次，白等到时限）。
    """
    return isinstance(exc, UpstreamBusy)


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_MAX_COOLDOWN_SECONDS",
    "DEFAULT_THRESHOLD",
    "UpstreamBusy",
    "UpstreamGate",
    "global_gate",
    "is_upstream_busy",
]