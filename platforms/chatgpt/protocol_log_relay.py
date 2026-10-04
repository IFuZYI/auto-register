"""把协议层的日志镜像进任务日志。

协议链一跑就是几十秒，中间的每一步都只写在 ``platforms.chatgpt.protocol``
这个 logger 上。不接出来的话，前端日志面板从头到尾只有"开始/结束"两行，
出问题时既看不出卡在哪一步，也看不到服务端到底回了什么。

接码那侧的 logger 也一起镜像：租号、退款这些关系到钱，出问题时最需要看见。
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

MIRRORED_LOGGERS = ("platforms.chatgpt.protocol", "services.sms_service")

_LOG_PREFIXES = {"services.sms_service": "[接码平台]"}
_DEFAULT_LOG_PREFIX = "[协议]"

_relay_lock = threading.Lock()
_relay_refcount: dict[str, int] = {}
_relay_saved_level: dict[str, int] = {}


class _ThreadScopedLogRelay(logging.Handler):
    """只转发本线程产生的日志。

    批量任务是多线程跑的，每个线程有自己的任务日志回调；不按线程过滤的话
    A 号的授权步骤会串进 B 号的日志里。
    """

    def __init__(self, thread_id: int, sink: Callable[[str], None], prefix: str):
        super().__init__(level=logging.INFO)
        self._thread_id = thread_id
        self._sink = sink
        self._prefix = prefix

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self._thread_id:
            return
        try:
            self._sink(f"{self._prefix} {record.getMessage()}")
        except Exception:
            pass


@contextmanager
def mirror_protocol_logs(sink: Optional[Callable[[str], None]]) -> Iterator[None]:
    """把协议层和接码平台的 INFO 日志镜像到 ``sink``。"""
    if sink is None:
        yield
        return

    thread_id = threading.get_ident()
    attached: list[tuple[logging.Logger, logging.Handler]] = []

    with _relay_lock:
        for name in MIRRORED_LOGGERS:
            target = logging.getLogger(name)
            if _relay_refcount.get(name, 0) == 0:
                _relay_saved_level[name] = target.level
                if not target.isEnabledFor(logging.INFO):
                    target.setLevel(logging.INFO)
            _relay_refcount[name] = _relay_refcount.get(name, 0) + 1
            relay = _ThreadScopedLogRelay(
                thread_id, sink, _LOG_PREFIXES.get(name, _DEFAULT_LOG_PREFIX)
            )
            target.addHandler(relay)
            attached.append((target, relay))

    try:
        yield
    finally:
        with _relay_lock:
            for target, relay in attached:
                target.removeHandler(relay)
                remaining = _relay_refcount.get(target.name, 1) - 1
                _relay_refcount[target.name] = max(remaining, 0)
                if remaining <= 0:
                    target.setLevel(_relay_saved_level.get(target.name, logging.NOTSET))
