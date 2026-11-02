"""icloud-hme 远程服务的测试双端（真实 HTTP，非 mock）。

被 `test_icloud_remote_client.py`（客户端契约）与 `test_icloud_remote_service.py`
（服务层远程分支）共用。刻意用真实 HTTP 服务而不是 in-process mock：
- 客户端/服务层若没把请求发到双端（例如硬编码了生产地址），测试会直接连接失败；
- 双端**只认 `X-API-Key`**：缺头或值不对回 `401 INVALID_API_KEY`，不再有
  Cookie / CSRF / 管理员密码登录。

这是 `tests/` 下的辅助模块（无 `test_` 前缀，pytest 不会收集它）。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

API_KEY = "sk-test-key"


class RemoteHMEDouble:
    """icloud-hme 服务的最小双端（只实现本项目用到的契约）。"""

    def __init__(self, accounts: list[dict] | None = None, api_key: str = API_KEY) -> None:
        self.api_key = api_key
        self.seen_keys: list[str] = []
        self.requests: list[tuple[str, str, dict]] = []
        self.inbox_messages: dict[str, list[dict]] = {}
        self.aliases: dict[str, list[dict]] = {}
        self.create_error: dict | None = None
        self.last_create_label = ""
        self.created_aliases: list[tuple[str, str]] = []
        self.deleted_aliases: list[str] = []
        self.active_calls: list[tuple[str, bool]] = []
        self.accounts = accounts if accounts is not None else [
            {
                "id": "acc_1",
                "name": "主号",
                "icloud_email": "owner@icloud.com",
                "status": "active",
                "alias_total": 2,
                "alias_active": 2,
                "has_cookies": True,
                "has_app_password": True,
                "has_proxy": False,
                "last_validated": "",
                "status_message": "",
                "created_at": "2026-10-01T00:00:00Z",
            }
        ]
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # ------------------------------------------------------------- 生命周期

    def start(self) -> None:
        double = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: A003 - 静音
                return

            # -------------------------------------------------- 工具

            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    data = json.loads(raw or b"{}")
                except ValueError:
                    data = {}
                return data if isinstance(data, dict) else {}

            def _authed(self) -> bool:
                key = (self.headers.get("X-API-Key") or "").strip()
                double.seen_keys.append(key)
                if key != double.api_key:
                    self._json(401, {"success": False, "code": "INVALID_API_KEY", "message": "API 秘钥无效或已停用"})
                    return False
                return True

            # -------------------------------------------------- 路由

            def do_POST(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                body = self._read_body()
                double.requests.append(("POST", path, body))

                if not self._authed():
                    return

                if path == "/api/inbox/previews":
                    account_id = (query.get("account_id", [""])[0] or "").strip()
                    ids = [str(item) for item in (body.get("ids") or [])]
                    known = {
                        str(m.get("id")): str(m.get("preview") or "")
                        for m in double.inbox_messages.get(account_id, [])
                    }
                    self._json(
                        200,
                        {"success": True, "data": {mid: known.get(mid, f"摘要-{mid}") for mid in ids}},
                    )
                    return

                if path == "/api/create":
                    if double.create_error is not None:
                        self._json(429, {"success": False, **double.create_error})
                        return
                    account_id = str(body.get("account_id") or "")
                    label = str(body.get("label") or "")
                    double.last_create_label = label
                    double.created_aliases.append((account_id, label))
                    email = f"remote{len(double.created_aliases)}@icloud.com"
                    # 真实服务创建后，别名会出现在后续 /api/aliases 里（带 anonymousId）——
                    # 本项目依赖它解析新别名的匿名 id（删除/停用要用）。
                    double.aliases.setdefault(account_id, []).append(
                        {
                            "email": email,
                            "anonymousId": f"anon-{len(double.created_aliases)}",
                            "label": label,
                            "active": True,
                            "createdAt": "2026-10-09T12:00:00Z",
                        }
                    )
                    self._json(
                        200,
                        {
                            "success": True,
                            "data": {
                                "email": email,
                                "label": label,
                                "created_at": "2026-10-09T12:00:00Z",
                                "account_id": account_id,
                            },
                        },
                    )
                    return

                if path.endswith("/deactivate") or path.endswith("/reactivate"):
                    active = path.endswith("/reactivate")
                    anonymous = path.split("/")[-2]
                    double.active_calls.append((anonymous, active))
                    self._json(200, {"success": True, "data": {"anonymous_id": anonymous, "success": True}})
                    return

                if path.endswith("/check"):
                    self._json(200, {"success": True, "data": {"id": path.split("/")[-2], "status": "active"}})
                    return

                self._json(404, {"success": False, "code": "NOT_FOUND", "message": "not found"})

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                double.requests.append(("GET", path, {k: v[0] for k, v in query.items()}))

                if not self._authed():
                    return

                if path == "/api/accounts":
                    self._json(200, {"success": True, "data": double.accounts})
                    return

                if path == "/api/alias-labels":
                    self._json(200, {"success": True, "data": ["GitHub", "Notion"]})
                    return

                if path == "/api/aliases":
                    account_id = query.get("account_id", [""])[0]
                    aliases = double.aliases.get(account_id, [])
                    self._json(
                        200,
                        {
                            "success": True,
                            "data": {
                                "account_id": account_id,
                                "count": len(aliases),
                                "aliases": aliases,
                            },
                        },
                    )
                    return

                if path == "/api/inbox":
                    account_id = query.get("account_id", [""])[0]
                    messages = double.inbox_messages.get(account_id, [])
                    self._json(
                        200,
                        {
                            "success": True,
                            "data": {
                                "account_id": account_id,
                                "alias": query.get("alias", [""])[0],
                                "count": len(messages),
                                "method": "imap",
                                "messages": messages,
                            },
                        },
                    )
                    return

                if path.startswith("/api/inbox/"):
                    message_id = path.rsplit("/", 1)[-1]
                    self._json(
                        200,
                        {
                            "success": True,
                            "data": {
                                "id": message_id,
                                "subject": "验证码",
                                "from": "no-reply@example.com",
                                "to": "remote1@icloud.com",
                                "date": "2026-10-09T12:00:00Z",
                                "preview": "您的验证码是 123456",
                                "body": "您的验证码是 123456",
                                "content_type": "text/plain",
                                "folder": "INBOX",
                            },
                        },
                    )
                    return

                self._json(404, {"success": False, "code": "NOT_FOUND", "message": "not found"})

            def do_DELETE(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                double.requests.append(("DELETE", path, {}))
                if not self._authed():
                    return
                if path.startswith("/api/aliases/"):
                    anonymous = path.rsplit("/", 1)[-1]
                    double.deleted_aliases.append(anonymous)
                    self._json(200, {"success": True, "data": {"anonymous_id": anonymous}})
                    return
                self._json(404, {"success": False, "code": "NOT_FOUND", "message": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


__all__ = ["RemoteHMEDouble", "API_KEY"]
