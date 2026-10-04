"""grok2api 管理面客户端：上传 SSO（Web）→ 开 NSFW。

只调用 grok2api **现成的**管理 API，不改它本身。

出处：`reference/grok/grok-hub-clean/hub/grok2api.mjs`（逐端点对照移植），
那是参考项目里已经跑通的实现。关键坑（原文记录）：

1. 上传文件名**必须**是 `grok-web-sso-tokens.txt` —— grok2api 靠文件名识别
   token 类型，换名字会报 `Cannot read properties of undefined (reading 'trim')`。
2. 重复上传返回 `created:0, updated:1` —— 也算成功（幂等）。
3. 账号级动作（条款/生日/NSFW）只对 **Web** 账号有效，必须先按邮箱找到
   `provider=grok_web` 的那条，拿它的数字 id。
4. NSFW 顺序不能换：`accept-terms` → `birth-date` → `nsfw`。

**不做派生**：Console / Build 两类凭据由用户自己在 grok2api 里手动转换
（用户要求：「grok2api 导入只需要 grokweb，build 和 console 不需要」）。
我们只负责把 Web 号（SSO）传上去。

与 ChatGPT 侧的差别：grok2api 的管理面要**用户名+密码换 token**（不是固定
API Key），token 有效期约 10 分钟，这里做进程内缓存并在 401 时重建。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

_TOKEN_TTL_SECONDS = 9 * 60  # 实测 10 分钟有效期，留 1 分钟缓冲


class Grok2ApiError(RuntimeError):
    """grok2api 调用失败。"""


#: `from_config` 的实例缓存：(base_url, username, proxy) → client。
#:
#: 为什么需要：登录 token 缓存在**实例**上（`_token` / `_token_exp`，TTL 9 分钟），
#: 而 `from_config` 每次都 new 一个 —— 缓存永远命不中，每次调用都要重新登录。
#: 实测：新建 client 登录 442ms / 477ms；复用同一实例第二次 0.01ms。
#: 面板对比每次刷新都走这条路径，登录开销占了大头。
#:
#: 密码不进 key：同一个 base_url + 用户名下换密码属于改配置，那种情况本来就该
#: 重新登录（换密码后旧 token 也已失效）。带锁是因为后台任务会并发跑。
_CLIENT_CACHE: dict[tuple[str, str, str], "Grok2ApiClient"] = {}
_CLIENT_CACHE_LOCK = threading.Lock()


class Grok2ApiClient:
    """带会话复用的 grok2api 管理面客户端。"""

    def __init__(
        self,
        base_url: str = "",
        username: str = "",
        password: str = "",
        *,
        proxy: str = "",
        timeout: int = 30,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self.proxy = str(proxy or "").strip()
        self.timeout = int(timeout or 30)
        self._token = ""
        self._token_exp = 0.0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 基础设施

    @classmethod
    def from_config(
        cls,
        *,
        proxy: str = "",
        api_url: str = "",
        username: str = "",
        password: str = "",
    ) -> "Grok2ApiClient":
        """从全局配置读连接参数（「全局配置 → 面板配置 → grok2api」）。

        显式传入的非空参数优先 —— 平台动作的 `params` 走这条通道，
        让调用方可以临时指向另一套 grok2api，不必改全局配置。

        同一个 (base_url, username, proxy) 复用**同一个实例**：登录 token 缓存在
        实例上，每次 new 就等于每次重新登录（实测 442ms vs 0.01ms）。
        换密码会走重新登录（密码不进缓存 key），所以缓存不会拿着旧凭据不放。
        """
        from core.config_store import config_store

        resolved_url = str(api_url or config_store.get("grok2api_base_url", "") or "")
        resolved_user = str(username or config_store.get("grok2api_username", "") or "admin")
        resolved_pass = str(password or config_store.get("grok2api_password", "") or "")
        resolved_proxy = str(proxy or "")

        key = (resolved_url.strip().rstrip("/"), resolved_user.strip(), resolved_proxy)
        with _CLIENT_CACHE_LOCK:
            cached = _CLIENT_CACHE.get(key)
            if cached is not None and cached.password == resolved_pass:
                return cached
            client = cls(
                resolved_url,
                resolved_user,
                resolved_pass,
                proxy=resolved_proxy,
            )
            _CLIENT_CACHE[key] = client
            return client

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.username and self.password)

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict | None = None,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        timeout: int | None = None,
    ) -> Any:
        """发一个 HTTP 请求，返回响应对象（调用方决定怎么读）。

        `files` 走 curl_cffi 的 `CurlMime` 构造 multipart —— **不能用
        `files=` 参数**：curl_cffi 0.16.x 里它仍在签名上，但一调用就抛
        `NotImplementedError: files is not supported, use multipart`
        （requirements 锁的是 `curl-cffi>=0.16.2,<0.17`）。这个坑只有在
        真发请求时才暴露 —— mock 掉 `_request` 的单元测试看不到。
        """
        try:
            from curl_cffi import requests as curl_requests
        except Exception as exc:  # pragma: no cover
            raise Grok2ApiError(f"curl_cffi 不可用: {exc}") from exc

        proxies = {"https": self.proxy, "http": self.proxy} if self.proxy else None
        url = f"{self.base_url}{path}"

        multipart = None
        try:
            if files:
                from curl_cffi import CurlMime

                multipart = CurlMime()
                for field, spec in files.items():
                    filename, content, content_type = spec
                    multipart.addpart(
                        field,
                        filename=filename,
                        content_type=content_type,
                        data=content if isinstance(content, bytes) else str(content).encode("utf-8"),
                    )
            return curl_requests.request(
                method.upper(),
                url,
                headers=headers or {},
                json=json_body,
                data=data,
                multipart=multipart,
                proxies=proxies,
                impersonate="chrome",
                timeout=timeout or self.timeout,
            )
        except Grok2ApiError:
            raise
        except Exception as exc:
            raise Grok2ApiError(f"请求 {path} 异常: {type(exc).__name__}: {str(exc)[:120]}") from exc
        finally:
            # CurlMime 持有本地资源，发完必须关（官方示例用 with 语句）。
            if multipart is not None:
                try:
                    multipart.close()
                except Exception:
                    pass

    def login(self, *, force: bool = False) -> str:
        """用户名+密码换 access token（进程内缓存）。"""
        if not self.configured:
            raise Grok2ApiError("grok2api 未配置（面板填地址 / 用户名 / 密码）")
        with self._lock:
            if not force and self._token and time.time() < self._token_exp:
                return self._token
            resp = self._request(
                "POST",
                "/api/admin/v1/auth/login",
                headers={"content-type": "application/json"},
                json_body={"username": self.username, "password": self.password},
                timeout=15,
            )
            if int(getattr(resp, "status_code", 0) or 0) != 200:
                raise Grok2ApiError(f"grok2api 登录失败 HTTP {resp.status_code}")
            try:
                doc = resp.json()
            except Exception as exc:
                raise Grok2ApiError(f"grok2api 登录响应不是 JSON: {str(exc)[:80]}") from exc
            token = (
                (((doc or {}).get("data") or {}).get("tokens") or {}).get("accessToken")
                or (doc or {}).get("accessToken")
                or (doc or {}).get("access_token")
                or ""
            )
            if not token:
                raise Grok2ApiError("grok2api 登录响应里没有 accessToken")
            self._token = str(token)
            self._token_exp = time.time() + _TOKEN_TTL_SECONDS
            return self._token

    def _auth_headers(self, *, content_type: str = "") -> dict:
        headers = {"authorization": f"Bearer {self.login()}"}
        if content_type:
            headers["content-type"] = content_type
        return headers

    # ---------------------------------------------------------------- 账号操作

    def import_sso(self, sso: str) -> tuple[bool, str]:
        """上传一个 SSO（Web 账号）。

        成功判定：SSE 文本里 `"created":1` 或 `"updated":1`（重复上传是幂等），
        或者 `"skipped":N>0` 且 `"failed":0`。
        """
        token = str(sso or "").strip()
        if not token:
            return False, "SSO 为空"

        # 文件名固定：grok2api 靠它识别 token 类型（改名字会 500）
        files = {
            "file": (
                "grok-web-sso-tokens.txt",
                (token + "\n").encode("utf-8"),
                "text/plain",
            )
        }
        resp = self._request(
            "POST",
            "/api/admin/v1/accounts/web/import",
            headers=self._auth_headers(),
            files=files,
            timeout=60,
        )
        status = int(getattr(resp, "status_code", 0) or 0)
        text = str(getattr(resp, "text", "") or "")

        # 解析 SSE 的 complete 事件来判断结果，而不是做子串匹配。
        # 曾经用 `'"created":1' in text` 这类字面量匹配：只要 grok2api 侧
        # 的 JSON 序列化加了空格（`"created": 1`）或顺序变化就会误判成失败
        # —— 端到端跑真实 HTTP 时就是这么翻车的（mock 掉 _request 的
        # 单元测试看不到）。按数值判断，与格式无关。
        done = self._last_sse_data(text)
        created = self._as_int(done.get("created"))
        updated = self._as_int(done.get("updated"))
        skipped = self._as_int(done.get("skipped"))
        failed = self._as_int(done.get("failed"))

        if done:
            # 幂等：重复上传是 created:0/updated:N；已被别处导入过是 skipped>0
            ok = (created + updated) > 0 or (skipped > 0 and failed == 0)
        else:
            # 没有 SSE 负载（例如网关返回了纯文本错误页）→ 只能按状态码判
            ok = False

        if status == 200 and ok:
            detail = f"created={created} updated={updated} skipped={skipped}"
            return True, detail
        brief = " ".join(text.split())[:180]
        return False, brief or f"HTTP {status}"

    @staticmethod
    def _as_int(value: Any) -> int:
        """宽容转 int：SSE 里可能是数字，也可能是字符串。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _find_account(self, email: str, *, provider: str = "") -> Optional[dict]:
        """按邮箱查账号；`provider` 非空时只认该类型（如 grok_web）。"""
        resp = self._request(
            "GET",
            f"/api/admin/v1/accounts?search={email}&pageSize=10",
            headers=self._auth_headers(),
            timeout=20,
        )
        if int(getattr(resp, "status_code", 0) or 0) != 200:
            raise Grok2ApiError(f"查询账号失败 HTTP {resp.status_code}")
        try:
            items = ((resp.json() or {}).get("data") or {}).get("items") or []
        except Exception as exc:
            raise Grok2ApiError(f"查询账号响应异常: {str(exc)[:80]}") from exc
        for item in items:
            if str((item or {}).get("email") or "") != email:
                continue
            if provider and str((item or {}).get("provider") or "") != provider:
                continue
            return item
        return None

    def find_web_account_by_email(self, email: str) -> Optional[dict]:
        """按邮箱找 **Web 号池** 的账号（账号级动作只对它有效）。

        必须筛 `provider=grok_web`：同邮箱下常同时存在 build / console 记录
        （grok2api 自己转换出来的），不筛就会拿到别的那条。
        """
        return self._find_account(email, provider="grok_web")

    @staticmethod
    def _last_sse_data(text: str) -> dict:
        """从 SSE 文本里取最后一个 `data: {...}`（complete 事件）。"""
        out: dict = {}
        for line in str(text or "").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            try:
                parsed = json.loads(payload)
            except Exception:
                continue
            if isinstance(parsed, dict):
                out = parsed
        return out

    def account_setup(self, web_id: Any, *, nsfw: bool = True) -> tuple[bool, list, list]:
        """Web 账号一键设置：接受条款 → 设成人生日 → 开 NSFW（顺序不能换）。

        返回 `(ok, done, failed)`。grok2api 对已做过的步骤是幂等的。
        """
        steps = ["accept-terms", "birth-date", "nsfw"] if nsfw else ["accept-terms"]
        done: list[str] = []
        failed: list[str] = []
        for step in steps:
            try:
                resp = self._request(
                    "POST",
                    f"/api/admin/v1/accounts/web/{web_id}/{step}",
                    headers=self._auth_headers(),
                    timeout=30,
                )
                status = int(getattr(resp, "status_code", 0) or 0)
                if status != 200:
                    failed.append(f"{step}: HTTP {status}")
                else:
                    done.append(step)
            except Exception as exc:
                failed.append(f"{step}: {type(exc).__name__}: {str(exc)[:60]}")
        return (not failed), done, failed

    def test_connection(self) -> tuple[bool, str]:
        """设置页「测试连接」：强制重新登录一次。"""
        try:
            self.login(force=True)
            return True, "连接正常"
        except Exception as exc:
            return False, str(exc)[:200]

    # ---------------------------------------------------------------- 组合流程

    def ingest_sso(
        self,
        sso: str,
        email: str,
        *,
        nsfw: bool = True,
        log=None,
    ) -> tuple[bool, str]:
        """一个账号的完整接入：上传 SSO（Web）→ 开 NSFW。

        只做 Web 导入。Console / Build 两类凭据**不在这里派生** ——
        用户要求：「grok2api 导入只需要 grokweb，build 和 console 不需要，
        这两个用户可以自己在 grok2api 中手动转换。」

        返回 `(ok, 摘要)`。NSFW 失败不影响 ok（账号已在池里，
        只把失败原因带回去让界面显示）。
        """
        def _say(msg: str) -> None:
            if log:
                try:
                    log(msg)
                except Exception:
                    pass

        ok, msg = self.import_sso(sso)
        if not ok:
            return False, f"上传失败: {msg}"
        done = ["web"]

        warnings: list[str] = []

        if nsfw:
            # 账号级动作（条款/生日/NSFW）只对 Web 账号有效，必须按
            # provider=grok_web 找 —— 同邮箱下常同时存在 build/console 记录
            # （grok2api 自己转换出来的），不筛就会拿到别的那条。
            web = self.find_web_account_by_email(email)
            if not web:
                warnings.append("NSFW 跳过（查不到 Web 账号）")
            else:
                ok_n, _done_n, failed_n = self.account_setup(web.get("id"))
                if ok_n:
                    done.append("NSFW")
                else:
                    warnings.append("NSFW 未完成: " + "；".join(failed_n)[:100])

        summary = "已接入（" + " + ".join(done) + "）"
        if warnings:
            summary += "；" + "；".join(warnings)
        _say(f"[Grok] grok2api {summary}")
        return True, summary


__all__ = ["Grok2ApiClient", "Grok2ApiError"]
