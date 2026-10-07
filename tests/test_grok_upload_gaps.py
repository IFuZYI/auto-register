"""Grok 上传模块的缺口覆盖：Sub2API 路径回退、分组解析、上传异常路径。

`platforms/grok/upload.py` 61% 覆盖。缺口集中在：
- `upload_to_sub2api` 的三路径回退（`/api/admin/accounts` → `/api/accounts` → `/admin/api/accounts`）
  —— 不同 Sub2API 版本路由不同，回退顺序错会让上传「看着成功其实没到」；
- `resolve_sub2api_group` 的名称→ID 解析（对端形状 `data`/`items`/`list` 变体）；
- `_http_post` 的异常与状态码分支。

已有测试覆盖「无 URL 报错」「CPA multipart 成功」「Sub2API 带分组成功」，
这里只补缺口。
"""

from __future__ import annotations

import unittest
from unittest import mock

import platforms.grok.upload as upload_mod


class Sub2ApiPathFallbackTests(unittest.TestCase):
    def _response(self, status_code=200, text="ok"):
        resp = mock.MagicMock()
        resp.status_code = status_code
        resp.text = text
        return resp

    def test_first_path_success_stops(self):
        with mock.patch("curl_cffi.requests.post", return_value=self._response()) as post:
            ok, msg = upload_mod.upload_to_sub2api(
                {"email": "a@b.com"}, api_url="http://sub.local", api_key="k"
            )
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 1)
        self.assertIn("/api/admin/accounts", post.call_args.args[0])

    def test_falls_back_to_second_then_third_path(self):
        """第一个路径 404 → 第二个 404 → 第三个 200。"""
        calls = []

        def fake_post(url, **kwargs):
            calls.append(url)
            if url.endswith("/admin/api/accounts"):
                return self._response(200)
            return self._response(404, "not found")

        with mock.patch("curl_cffi.requests.post", side_effect=fake_post):
            ok, _msg = upload_mod.upload_to_sub2api(
                {"email": "a@b.com"}, api_url="http://sub.local"
            )

        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)
        self.assertTrue(calls[0].endswith("/api/admin/accounts"))
        self.assertTrue(calls[1].endswith("/api/accounts"))
        self.assertTrue(calls[2].endswith("/admin/api/accounts"))

    def test_all_paths_fail_reports_last_error(self):
        with mock.patch(
            "curl_cffi.requests.post", return_value=self._response(500, "boom")
        ):
            ok, msg = upload_mod.upload_to_sub2api(
                {"email": "a@b.com"}, api_url="http://sub.local"
            )
        self.assertFalse(ok)
        self.assertIn("上传失败", msg)

    def test_request_exception_is_reported_not_raised(self):
        with mock.patch("curl_cffi.requests.post", side_effect=ConnectionError("refused")):
            ok, msg = upload_mod.upload_to_sub2api(
                {"email": "a@b.com"}, api_url="http://sub.local"
            )
        self.assertFalse(ok)
        self.assertIn("请求异常", msg)


class ResolveSub2ApiGroupTests(unittest.TestCase):
    def _response(self, payload, status_code=200):
        resp = mock.MagicMock()
        resp.status_code = status_code
        resp.json.return_value = payload
        return resp

    def test_resolves_by_name_from_data_items(self):
        payload = {"data": {"items": [{"name": "grok", "id": "42"}]}}
        with mock.patch("curl_cffi.requests.get", return_value=self._response(payload)):
            group_id = upload_mod.resolve_sub2api_group("http://sub.local", "k", "grok")
        self.assertEqual(group_id, "42")

    def test_resolves_from_flat_list(self):
        payload = [{"group_name": "grok", "group_id": "7"}]
        with mock.patch("curl_cffi.requests.get", return_value=self._response(payload)):
            group_id = upload_mod.resolve_sub2api_group("http://sub.local", "", "grok")
        self.assertEqual(group_id, "7")

    def test_second_path_used_when_first_404(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if url.endswith("/api/groups"):
                return self._response({"data": [{"name": "grok", "id": "9"}]})
            return self._response({}, status_code=404)

        with mock.patch("curl_cffi.requests.get", side_effect=fake_get):
            group_id = upload_mod.resolve_sub2api_group("http://sub.local", "", "grok")

        self.assertEqual(group_id, "9")
        self.assertEqual(len(calls), 2)

    def test_missing_name_returns_none(self):
        payload = {"data": [{"name": "other", "id": "1"}]}
        with mock.patch("curl_cffi.requests.get", return_value=self._response(payload)):
            group_id = upload_mod.resolve_sub2api_group("http://sub.local", "", "grok")
        self.assertIsNone(group_id)

    def test_empty_inputs_return_none_without_request(self):
        with mock.patch("curl_cffi.requests.get") as get:
            self.assertIsNone(upload_mod.resolve_sub2api_group("", "k", "grok"))
            self.assertIsNone(upload_mod.resolve_sub2api_group("http://sub.local", "k", ""))
        get.assert_not_called()


class CpaUploadEdgeTests(unittest.TestCase):
    def test_proxy_url_is_embedded_in_record(self):
        """上传代理开关打开时，代理要写进记录顶层 proxy_url（CPA 用它固定出口）。"""
        import json

        captured = {}

        class FakeResp:
            status_code = 200
            text = "ok"

        class FakeMime:
            def __init__(self):
                self.parts = []

            def addpart(self, **kwargs):
                self.parts.append(kwargs)

            def close(self):
                pass

        def fake_post(url, **kwargs):
            captured["mime"] = kwargs.get("multipart")
            return FakeResp()

        with (
            mock.patch("curl_cffi.CurlMime", FakeMime),
            mock.patch("curl_cffi.requests.post", side_effect=fake_post),
        ):
            ok, _msg = upload_mod.upload_to_cpa(
                {"email": "a@b.com", "access_token": "at"},
                api_url="http://cpa.local",
                api_key="k",
                proxy="http://user:pw@1.2.3.4:8080",
            )

        self.assertTrue(ok)
        mime = captured["mime"]
        self.assertEqual(len(mime.parts), 1, "必须是单文件 multipart（字段名 file）")
        part = mime.parts[0]
        self.assertEqual(part["name"], "file")
        self.assertEqual(part["filename"], "xai-a@b.com.json")
        record = json.loads(part["data"].decode("utf-8"))
        self.assertEqual(record["proxy_url"], "http://user:pw@1.2.3.4:8080",
                         "代理必须写进记录顶层 proxy_url")

    def test_no_proxy_means_no_proxy_url_field(self):
        """不开上传代理时不得凭空塞 proxy_url。"""
        import json

        captured = {}

        class FakeResp:
            status_code = 200
            text = "ok"

        class FakeMime:
            def __init__(self):
                self.parts = []

            def addpart(self, **kwargs):
                self.parts.append(kwargs)

            def close(self):
                pass

        def fake_post(url, **kwargs):
            captured["mime"] = kwargs.get("multipart")
            return FakeResp()

        with (
            mock.patch("curl_cffi.CurlMime", FakeMime),
            mock.patch("curl_cffi.requests.post", side_effect=fake_post),
        ):
            upload_mod.upload_to_cpa(
                {"email": "a@b.com"}, api_url="http://cpa.local", api_key="k"
            )

        record = json.loads(captured["mime"].parts[0]["data"].decode("utf-8"))
        self.assertNotIn("proxy_url", record)

    def test_non_2xx_reports_status_and_body(self):
        class FakeResp:
            status_code = 401
            text = "unauthorized"

        with mock.patch("curl_cffi.requests.post", return_value=FakeResp()):
            ok, msg = upload_mod.upload_to_cpa(
                {"email": "a@b.com"}, api_url="http://cpa.local", api_key="k"
            )
        self.assertFalse(ok)
        self.assertIn("401", msg)

    def test_exception_reports_type_and_message(self):
        with mock.patch("curl_cffi.requests.post", side_effect=RuntimeError("net down")):
            ok, msg = upload_mod.upload_to_cpa(
                {"email": "a@b.com"}, api_url="http://cpa.local", api_key="k"
            )
        self.assertFalse(ok)
        self.assertIn("上传异常", msg)
        self.assertIn("net down", msg)


if __name__ == "__main__":
    unittest.main()
