"""「先查再建」必须串行 —— 并发上传不能在对端堆出重复的代理/出口节点。

2026-11-04 独立审查（切片 F）：`ensure_sub2api_proxy` / `ensure_grok2api_egress`
都是「GET 列表 → 没找到就 POST 创建」的幂等写法，但**没有任何互斥**：
两个并发调用方只要都在对方创建前完成 GET，就会各建一条。
可达路径是注册成功后 `services/task_service.auto_upload_integrations` 为**每个
账号**起一个 daemon 线程跑上传，而注册任务本身是线程池 —— 同一批账号共用同一条
代理时，两个上传线程会并发进入这里。

这正是模块 docstring 明确要避免的：「否则每推一个账号就会在对端堆一条重复代理」
「账号绑在哪一条上完全不可预期」。

## 怎么构造确定性交错

在 GET 里放一个**带短超时的屏障**：两个线程都会走到 GET 并各等一小会儿，
然后各自按**当时的真实远端状态**决定要不要建。

- 没有互斥：两边都读到「没有」，于是各建一条 → 重复。
- 有互斥：后一个线程根本进不来，等它进来时 GET 已经能看到前一个建好的记录
  → 不重复。

正确性**不依赖屏障是否真的把两个线程同步上**（wait 带 timeout 且吞掉
BrokenBarrierError），它只是把竞态窗口撑开到必然可复现。
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

PROXY_URL = "socks5h://user:pw@1.2.3.4:1080"


class _Resp:
    """假响应：**必须带 `status_code = 200`**。

    实现里「先找」那一段的判据是
    `if int(getattr(resp, "status_code", 0) or 0) == 200:` —— 少了这个属性就
    永远进不了「找到就复用」分支，两边都会去创建。夹具形状不对会让测试
    **永远看到重复**（无论生产代码是否已修），第一版我就栽在这儿。
    响应体形状也要对：`_dig_items` 吃 `{items|data|list|results}`，且每行要能被
    实现用 `host/port/username` 比中。
    """

    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class EvidenceSub2ApiProxyCreationTests(unittest.TestCase):
    def test_two_concurrent_uploads_create_only_one_proxy_record(self):
        from services import panel_proxy_binding as binding

        created: list[dict] = []
        state = {"rows": []}
        barrier = threading.Barrier(2)

        def _get(_url, **_kw):
            # 短超时：只是把窗口撑开，同步不上也继续（不依赖调度）
            try:
                barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass
            return _Resp({"data": list(state["rows"])})

        def _post(_url, **kw):
            row = dict(kw.get("json") or {})
            row.setdefault("id", len(created) + 1)
            state["rows"].append(row)
            created.append(row)
            return _Resp({"data": {"id": row["id"]}})

        results: list[tuple] = []

        def _worker():
            results.append(binding.ensure_sub2api_proxy("http://panel.example", "k", PROXY_URL))

        with mock.patch("curl_cffi.requests.get", _get), \
                mock.patch("curl_cffi.requests.post", _post):
            threads = [threading.Thread(target=_worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        self.assertEqual(
            len(created), 1,
            f"两个并发上传只该建 1 条代理记录，实际建了 {len(created)} 条 —— "
            f"面板里会出现重复记录，账号绑在哪一条上不可预期",
        )
        ids = sorted(str(r[0]) for r in results)
        self.assertEqual(ids, ["1", "1"], f"两个调用方应当拿到**同一个** proxy_id，实际 {ids}")
