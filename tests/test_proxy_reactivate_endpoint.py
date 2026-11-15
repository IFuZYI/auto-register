"""`reactivate_all()` 必须有入口，否则被误停用的代理只能靠「检测全部」捞回来。

背景：`get_next()` 只取 `is_active` 的代理，而 `report_fail` 在「0 成功 + 5 次
失败」时会把代理置 `is_active=False`。那 5 次失败可能只是走了不支持它的调用
路径，代理本身是活的（实测：住宅代理 curl 直连正常，库里却躺着 is_active=0）。

`ProxyPool.reactivate_all()` 早就实现了，但全仓没有 API、也没有按钮 ——
用户只能逐条点开关，或者跑一遍串行探活（20 个代理约 160 秒）。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class ReactivateAllEndpointTests(unittest.TestCase):
    def _client(self) -> TestClient:
        from main import app

        return TestClient(app)

    def test_endpoint_reports_how_many_were_revived(self):
        """接口要回报复活了几条（前端据此说「已恢复 N 个」）。"""
        with patch("api.proxies.proxy_pool.reactivate_all", return_value=3) as called:
            response = self._client().post("/api/proxies/reactivate")
        self.assertEqual(response.status_code, 200, response.text[:200])
        called.assert_called_once()
        self.assertEqual(response.json().get("revived"), 3)

    def test_real_repository_is_used(self):
        """不打桩：真写一次，确认被停用的代理确实变回 active。"""
        from sqlmodel import Session, select

        from core.db import ProxyModel, engine

        with Session(engine) as session:
            row = ProxyModel(url="socks5://revive-me:1080", is_active=False, fail_count=7)
            session.add(row)
            session.commit()
            session.refresh(row)
            proxy_id = row.id

        response = self._client().post("/api/proxies/reactivate")
        self.assertEqual(response.status_code, 200, response.text[:200])

        with Session(engine) as session:
            reloaded = session.exec(
                select(ProxyModel).where(ProxyModel.id == proxy_id)
            ).first()
        self.assertIsNotNone(reloaded)
        self.assertTrue(reloaded.is_active, "被停用的代理没有复活")
        self.assertEqual(reloaded.fail_count, 0, "复活时失败计数要清零")


if __name__ == "__main__":
    unittest.main()