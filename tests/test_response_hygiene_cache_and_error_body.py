"""两个「响应卫生」契约：明文凭据不进缓存、上游原始体不进错误消息。

2026-11-04 独立审查（切片 E）实测：

1. `GET /api/accounts/{id}` 返回**解密后的明文**凭据（面板要显示，这是设计），
   但它是本仓库唯一一个返回明文却不带 `Cache-Control: no-store` 的端点 ——
   export 端点早就加了，理由写在注释里（「明文密码不进任何缓存」）。
   少这一个头，浏览器/中间代理就能把「密码 + AT/SSO + 全部 extra 凭据」落盘。

2. `POST /api/sms/balance` 把上游响应体原样塞进 detail：

       查询余额失败: {"error": "BAD_KEY", "secret_hint": "acct=12345"}

   上游是第三方短信服务，它的错误体里可能带 key/账号信息；而且把原始 JSON
   回给前端属于「错误消息说了不该说的」。判据与其它端点一致：
   消息里不许出现 `{`。
"""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient


class _ClientMixin:
    def _client(self):
        import main as main_mod

        return TestClient(main_mod.app)


class AccountDetailMustNotBeCacheableTests(_ClientMixin, unittest.TestCase):
    """详情端点的明文凭据不许被任何缓存留存。"""

    def test_detail_response_sets_no_store(self):
        from core.db import account_repository
        from core.db.models_account import AccountModel

        email = "detail-no-store@example.com"
        model = AccountModel(platform="chatgpt", email=email, password="pw-1")
        model.set_extra({"access_token": "eyJhbGciOiJSUzI1NiJ9.at.sig"})
        account_repository.upsert(model)
        try:
            row = account_repository.find_by_email("chatgpt", email)
            with self._client() as client:
                r = client.get(f"/api/accounts/{row.id}?platform=chatgpt")
            self.assertEqual(r.status_code, 200, r.text[:200])
            self.assertEqual(
                (r.headers.get("cache-control") or "").lower(), "no-store",
                "详情端点返回明文密码/AT，必须带 Cache-Control: no-store",
            )
        finally:
            row = account_repository.find_by_email("chatgpt", email)
            if row is not None:
                account_repository.delete(row.id, "chatgpt")


class SmsProbeErrorMustNotEchoUpstreamBodyTests(_ClientMixin, unittest.TestCase):
    """上游错误体不许原样回给前端。"""

    def test_upstream_body_is_not_echoed_into_the_detail(self):
        # 打**真路径**：`services/sms/providers.py` 拿不到响应体时会
        # `raise RuntimeError(f"查询余额失败: {text}")`，`api/sms.py` 再把它
        # 拼进 detail。所以这里让 provider.get_balance() 抛同样的错，
        # 而不是去 patch `_build_provider`（那个在 try 之外，打错了会走成 500，
        # 测的就不是这条分支了）。
        upstream = '{"error": "BAD_KEY", "secret_hint": "acct=12345"}'
        provider = mock.Mock()
        provider.get_balance.side_effect = RuntimeError(f"查询余额失败: {upstream}")

        with mock.patch("api.sms._build_provider", return_value=(provider, {"sms_provider": "x"})):
            with self._client() as client:
                r = client.post("/api/sms/balance", json={})

        self.assertEqual(r.status_code, 502, r.text[:200])
        detail = str(r.json().get("detail") or "")
        self.assertNotIn(
            "{", detail,
            f"错误消息里不许出现上游原始 JSON 花括号，实际：{detail}",
        )
