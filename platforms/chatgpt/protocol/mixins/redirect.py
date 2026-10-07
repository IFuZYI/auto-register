"""RedirectMixin：重定向链解析辅助（continue_url / page_type / workspace / 账号选择）。

从 3541 行的 auth_flow.py 拆出（纯搬家，方法体逐字节不变）。
"""
from __future__ import annotations

import base64
import json
import logging
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from platforms.chatgpt.protocol.response_summary import describe_error

logger = logging.getLogger(__name__)


class RedirectMixin:
    @staticmethod
    def _extract_query_first(url: str, keys: list[str]) -> str:
        if not url:
            return ""
        try:
            qs = parse_qs(urlparse(url).query)
        except Exception:
            return ""
        for k in keys:
            val = qs.get(k, [None])[0]
            if val:
                return val
        return ""


    @staticmethod
    def _extract_page_type(resp_json: dict | None) -> str:
        if not isinstance(resp_json, dict):
            return ""
        page = resp_json.get("page", {})
        if not isinstance(page, dict):
            return ""
        return (page.get("type", "") or "").strip()


    @staticmethod
    def _extract_continue_url_from_step(resp_json: dict | None) -> str:
        """
        从 auth step 响应提取 continue_url：
        - 顶层 continue_url
        - page.type=external_url 时 payload.url
        """
        if not isinstance(resp_json, dict):
            return ""
        continue_url = (resp_json.get("continue_url", "") or "").strip()
        if continue_url:
            return continue_url
        page = resp_json.get("page", {})
        if not isinstance(page, dict):
            return ""
        if (page.get("type", "") or "").strip() != "external_url":
            return ""
        payload = page.get("payload", {})
        if not isinstance(payload, dict):
            return ""
        return (payload.get("url", "") or "").strip()


    def _extract_workspace_id(self) -> str:
        """从 cookie 中提取 workspace_id"""
        try:
            auth_session = self.session.cookies.get("oai-client-auth-session", "")
            if auth_session:
                parts = auth_session.split(".")
                # 兼容不同 cookie 形态：workspace_id 可能在第 1 段/第 2 段，也可能在 workspaces[0].id
                for idx in range(min(2, len(parts))):
                    segment = (parts[idx] or "").strip()
                    if not segment:
                        continue
                    payload_b64 = segment + "=" * (-len(segment) % 4)
                    decoded = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
                    if not isinstance(decoded, dict):
                        continue
                    wid = (decoded.get("workspace_id", "") or "").strip()
                    if wid:
                        return wid
                    workspaces = decoded.get("workspaces", [])
                    if isinstance(workspaces, list):
                        for it in workspaces:
                            if isinstance(it, dict):
                                wid = (it.get("id", "") or "").strip()
                                if wid:
                                    return wid
        except Exception:
            pass
        return ""


    def _workspace_select(self, workspace_id: str) -> str:
        logger.info("执行 workspace 选择...")
        headers = self._common_headers("https://auth.openai.com/sign-in-with-chatgpt/codex/consent")
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/workspace/select",
            headers=headers,
            json={"workspace_id": workspace_id},
            timeout=30,
        )
        self._trace_http("workspace_select", resp)
        return resp.json().get("continue_url", "") if resp.status_code == 200 else ""


    def _choose_account_select(self, html_text: str, current_url: str) -> str:
        """处理 /choose-an-account 多账号选择页（react-router SSR）。

        HTML 里 streamController.enqueue 注入 `unified_sessions[].id` (us_*) 和
        `session_id` (authsess_*)。这里 regex 抽 us_*，按 react-router action 惯例
        POST 回 /choose-an-account，并 fallback 试几个候选 JSON endpoint。
        返回 next continue_url 或空串。
        """
        m = re.search(r"us_[A-Za-z0-9]{16,}", html_text or "")
        if not m:
            logger.warning("/choose-an-account HTML 里没找到 us_* session id, 跳过")
            return ""
        session_id = m.group(0)
        logger.debug(f"/choose-an-account 选 session_id={session_id}")
        headers = self._common_headers("https://auth.openai.com/choose-an-account")
        headers["Origin"] = "https://auth.openai.com"

        # 真实 endpoint 从 nextStepHandler-*.js 反编译解出：
        #   const {path, method} = r.data.intent === "select"
        #     ? {path: "/session/select", method: "POST"}
        #     : {path: "/session/remove", method: "DELETE"};
        #   fetch(`${authapi_base}/session/select`, {method, body: JSON.stringify({session_id})})
        # 即 POST https://auth.openai.com/api/accounts/session/select JSON {session_id}
        # （intent 决定 path 不进 body；body 只有 session_id 一个字段）
        # 之前直接 POST /choose-an-account 会先经过 react-router action loader 再被
        # nextStepHandler 转发，但 server-side 那一段似乎对 CT/form 字段强敏感，500。
        # 直接命中底层 /api/accounts/session/select 绕开 react-router 层。
        candidates = [
            ("POST", "https://auth.openai.com/api/accounts/session/select",
             {"session_id": session_id}, "json"),
            # 兜底：万一上面被风控，回退到 react-router 路径 + zod schema 字段
            ("POST", "https://auth.openai.com/choose-an-account",
             {"intent": "select", "session_id": session_id}, "form"),
        ]
        for method, url, body, kind in candidates:
            try:
                h = dict(headers)
                if kind == "json":
                    h["Content-Type"] = "application/json"
                    h["Accept"] = "application/json"
                    resp = self.session.post(url, headers=h, json=body, timeout=30)
                else:
                    h["Content-Type"] = "application/x-www-form-urlencoded"
                    h["Accept"] = "application/json, text/html;q=0.9"
                    body_str = "&".join(f"{k}={v}" for k, v in body.items())
                    resp = self.session.post(url, headers=h, data=body_str, timeout=30)
                self._trace_http(f"choose_account_try_{kind}_{url.rsplit('/', 1)[-1][:30]}", resp)
                status = getattr(resp, "status_code", 0)
                loc = (getattr(resp, "headers", {}) or {}).get("Location", "") or \
                      (getattr(resp, "headers", {}) or {}).get("location", "") or ""
                # print 到 stdout 让 webui SSE 能看到每个候选的具体结果
                print(
                    f"[choose-an-account] {method} {url} [{kind}] -> "
                    f"status={status} loc={loc[:120]} {describe_error(getattr(resp, 'text', ''))}",
                    flush=True,
                )
                if status in (200, 201, 302, 303):
                    next_url = ""
                    try:
                        j = resp.json() if resp is not None else {}
                        next_url = j.get("continue_url", "") if isinstance(j, dict) else ""
                    except Exception:
                        pass
                    if not next_url and loc:
                        next_url = loc
                    if next_url:
                        logger.debug(f"choose-an-account 选号成功 endpoint={url} next={next_url[:120]}")
                        return next_url
                    # 200 但没 continue_url：可能 set 了 cookie，直接让 caller 重 GET authorize
                    if status == 200:
                        logger.debug(f"choose-an-account POST {url} 200 OK 无 continue_url，假定 cookie 已 set")
                        return current_url  # 让外层重 GET 一次，cookie 已被 server set
            except Exception as e:
                print(f"[choose-an-account] {method} {url} [{kind}] -> EXC {e}", flush=True)
                continue
        logger.warning("/choose-an-account 全部候选 endpoint 都失败")
        return ""


    def _normalize_continue_url(self, continue_url: str) -> str:
        """
        标准化 continue_url：
        1) 相对路径 -> 绝对路径
        2) workspace 页面 -> 调用 workspace/select 取下一跳
        """
        if not continue_url:
            return ""
        out = continue_url.strip()
        if out.startswith("/"):
            out = urljoin("https://auth.openai.com", out)
        if "/workspace" in out:
            workspace_id = self._extract_workspace_id() or self._extract_query_first(out, ["workspace_id", "id"])
            if workspace_id:
                logger.info("检测到 workspace 页面，尝试 workspace/select: workspace_id=%s", workspace_id)
                next_url = self._workspace_select(workspace_id)
                if next_url:
                    out = next_url
        return out


    @staticmethod
    def _extract_workspace_id_from_html(html_text: str) -> str:
        """从 workspace 页面 HTML 文本中提取 workspace_id（兜底）。"""
        if not html_text:
            return ""
        try:
            # 先把转义引号还原，便于正则匹配
            text = html_text.replace('\\"', '"')
            patterns = [
                r'workspaces".{0,1600}?"id","([0-9a-fA-F-]{36})"',
                r'"workspace_id"\s*:\s*"([0-9a-fA-F-]{36})"',
                r'"workspaceId"\s*:\s*"([0-9a-fA-F-]{36})"',
            ]
            for p in patterns:
                m = re.search(p, text, flags=re.DOTALL | re.IGNORECASE)
                if m:
                    return (m.group(1) or "").strip()
        except Exception:
            return ""
        return ""

    def follow_redirect_chain(self, start_url: str) -> tuple[str, str]:
        """手动跟踪重定向，返回 (callback_url, final_url)"""
        logger.info("[9/10] 跟踪重定向链...")
        current_url = start_url
        callback_url = ""
        max_hops = 12
        referer = "https://auth.openai.com/"

        for i in range(max_hops):
            # 逐跳整页导航，头必须像浏览器：同 auth_oauth_init，旧版只发
            # Accept/Referer/UA，缺 client hints 和 Sec-Fetch-*（实测那正是
            # 409 invalid_state 的来源，见 auth_oauth_init docstring）。
            headers = self._navigation_headers()
            headers["Referer"] = referer
            headers.pop("sec-fetch-user", None)   # 302 跟随非用户点击
            # 跨站跳转（chatgpt.com <-> auth.openai.com）标 cross-site，同站标 same-origin
            try:
                headers["sec-fetch-site"] = (
                    "same-origin"
                    if urlparse(current_url).netloc == urlparse(referer).netloc
                    else "cross-site"
                )
            except Exception:
                headers["sec-fetch-site"] = "cross-site"
            resp = self.session.get(
                current_url, headers=headers, timeout=30, allow_redirects=False
            )
            self._trace_http(f"redirect_hop_{i+1}", resp)
            referer = current_url

            if "/api/auth/callback/openai" in current_url:
                callback_url = current_url

            # workspace 页面常见为 200，需要主动调 workspace/select 获取下一跳
            if "/workspace" in current_url and resp.status_code == 200:
                workspace_id = self._extract_workspace_id() or self._extract_workspace_id_from_html(resp.text or "")
                if workspace_id:
                    logger.info("workspace 页面提取到 workspace_id=%s，尝试继续授权", workspace_id)
                    next_url = self._workspace_select(workspace_id)
                    if next_url:
                        if next_url.startswith("/"):
                            next_url = urljoin("https://auth.openai.com", next_url)
                        current_url = next_url
                        continue

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if not location:
                    break
                if location.startswith("/"):
                    parsed = urlparse(current_url)
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"
                # 关键：不要主动 GET callback，避免 code 被服务端回调消费
                if "/api/auth/callback/openai" in location and "code=" in location:
                    callback_url = location
                    current_url = location
                    logger.info("捕获 callback URL（未消费）")
                    break
                current_url = location
                logger.debug(f"  重定向 {i + 1}: {current_url[:80]}...")
            else:
                break

        # 补一跳首页
        if (not callback_url) and (not current_url.rstrip("/").endswith("chatgpt.com")):
            self.session.get(
                "https://chatgpt.com/",
                headers={"Referer": current_url},
                timeout=30,
            )

        logger.info(f"重定向链完成, callback: {'有' if callback_url else '无'}")
        return callback_url, current_url


    def _reauthorize_for_session(self, original_auth_url: str) -> str | None:
        """已有账号 OTP 验证后，重新发起 authorize 获取 callback URL"""
        logger.info("[9.5/10] 重新 authorize 获取 session ...")
        try:
            # 去掉 prompt=login 参数，利用已有的 auth session cookie
            parsed = urlparse(original_auth_url)
            params = parse_qs(parsed.query, keep_blank_values=True)
            params.pop("prompt", None)
            # 重新构建 URL
            new_query = urlencode({k: v[0] for k, v in params.items()})
            authorize_url = urlunparse(parsed._replace(query=new_query))

            resp = self.session.get(
                authorize_url,
                allow_redirects=False,
                timeout=15,
            )
            self._trace_http("reauthorize_start", resp)
            logger.info(f"reauthorize status={resp.status_code}")

            # 跟随 redirect chain 找到 callback URL
            current_url = resp.headers.get("Location", "")
            logger.info(f"reauthorize Location: {current_url[:150]}")
            if resp.status_code in (301, 302, 303, 307, 308) and current_url:
                for hop in range(10):
                    logger.debug(f"reauthorize redirect hop {hop+1}: {current_url[:100]}")
                    if "code=" in current_url and "state=" in current_url:
                        logger.info("reauthorize: 找到 callback URL")
                        return current_url
                    try:
                        hop_resp = self.session.get(
                            current_url,
                            allow_redirects=False,
                            timeout=15,
                        )
                        self._trace_http(f"reauthorize_hop_{hop+1}", hop_resp)
                        next_loc = hop_resp.headers.get("Location", "")
                        if hop_resp.status_code not in (301, 302, 303, 307, 308) or not next_loc:
                            # 检查最终 URL
                            final_url = str(getattr(hop_resp, 'url', current_url))
                            if "code=" in final_url:
                                return final_url
                            break
                        current_url = next_loc
                        if not current_url.startswith("http"):
                            current_url = urljoin(authorize_url, current_url)
                    except Exception:
                        break
            logger.warning("reauthorize: 未能获取 callback URL")
            return None
        except Exception as e:
            logger.warning(f"reauthorize 失败: {e}")
            return None


    def _extract_session_cookie(self) -> str:
        """多路兜底提取 __Secure-next-auth.session-token cookie。

        curl_cffi 在某些情况下按 domain 隔离 cookie，session.cookies.get(name) 拿不到，
        所以这里把所有 cookie 都遍历一遍，按名字精确匹配。
        """
        target = "__Secure-next-auth.session-token"
        # 路径1：直接 get
        try:
            v = self.session.cookies.get(target, "")
            if v:
                return v
        except Exception:
            pass
        # 路径2：遍历 jar
        try:
            for c in self.session.cookies:
                name = getattr(c, "name", "") if hasattr(c, "name") else str(c)
                if name == target:
                    val = getattr(c, "value", "") or ""
                    if val:
                        return val
        except Exception:
            pass
        # 路径3：用 _get_cookie_value_by_name（不挑 domain）
        try:
            return self._get_cookie_value_by_name(target)
        except Exception:
            return ""


    def get_auth_session(self) -> tuple[str, str]:
        """获取 session_token 和 access_token。

        session_token 三路兜底（按优先级）：
          1. cookie `__Secure-next-auth.session-token`（NextAuth 数据库 session 策略）
          2. JSON 响应里的 `sessionToken` 字段（NextAuth JWT session 策略，某些路径）
          3. 兼容大小写 / 下划线变体
        access_token 取 JSON 响应里的 `accessToken`。
        """
        first_call = not getattr(self, "_auth_session_fetched", False)
        self._auth_session_fetched = True
        if first_call:
            logger.info("[10/10] 获取认证 Session...")
        headers = self._common_headers("https://chatgpt.com/")
        resp = self.session.get(
            "https://chatgpt.com/api/auth/session",
            headers=headers,
            timeout=30,
        )
        self._trace_http("chatgpt_auth_session", resp)
        resp.raise_for_status()

        try:
            sess_json = resp.json() if resp is not None else {}
        except Exception:
            sess_json = {}
        if not isinstance(sess_json, dict):
            sess_json = {}

        cookie_st = self._extract_session_cookie()
        json_st = (
            sess_json.get("sessionToken", "")
            or sess_json.get("session_token", "")
            or ""
        )
        session_token = cookie_st or json_st
        access_token = sess_json.get("accessToken", "") or sess_json.get("access_token", "") or ""

        if session_token:
            self.result.session_token = session_token
        if access_token:
            self.result.access_token = access_token
        self.result.cookie_header = self._build_chatgpt_cookie_header()

        _log = logger.info if first_call else logger.debug
        _log(f"session: st={'有' if session_token else '无'} at={'有' if access_token else '无'}")
        return session_token, access_token


    def _consume_callback_for_session(self, callback_url: str) -> bool:
        """主动 GET callback URL 让 chatgpt.com NextAuth 设 session cookie。

        协议层 follow_redirect_chain 故意不消费 callback（为后续 OAuth token exchange 留 code），
        但这导致 NextAuth 永远不会写 __Secure-next-auth.session-token cookie。
        在拿不到 session_token 时主动消费一次 callback：跟随到 chatgpt.com 主页，
        服务器会 Set-Cookie session-token。

        返回「是否拿到了 session cookie」。**这个返回值不可靠**：curl_cffi 在
        某些情况下按 domain 隔离 cookie，朴素的 `session.cookies.get(name)`
        读不到（`_extract_session_cookie` 专门为此有三路兜底）。所以调用方
        不该拿它当致命判据 —— 注册链与手机链都是忽略返回值、继续走
        `get_auth_session()`（它有兜底）。这里改成用同一套兜底读，让返回值
        至少与 `get_auth_session` 的口径一致。
        """
        if not callback_url or "code=" not in callback_url:
            return False
        try:
            current = callback_url
            referer = "https://auth.openai.com/"
            for hop in range(8):
                # 逐跳整页导航的头：与 `follow_redirect_chain` 同源。
                # 旧版只发 Accept/Referer/UA，缺 client hints 与 Sec-Fetch-*
                # —— 那正是 `follow_redirect_chain` 注释里记的 409 invalid_state
                # 来源（实测），同一个坑不要踩第二次。
                headers = self._navigation_headers()
                headers["Referer"] = referer
                headers.pop("sec-fetch-user", None)  # 302 跟随非用户点击
                try:
                    headers["sec-fetch-site"] = (
                        "same-origin"
                        if urlparse(current).netloc == urlparse(referer).netloc
                        else "cross-site"
                    )
                except Exception:
                    headers["sec-fetch-site"] = "cross-site"

                resp = self.session.get(
                    current, headers=headers, timeout=30, allow_redirects=False
                )
                self._trace_http(f"consume_callback_hop_{hop+1}", resp)
                referer = current
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                loc = (resp.headers.get("Location", "") or "").strip()
                if not loc:
                    break
                if loc.startswith("/"):
                    loc = urljoin(current, loc)
                current = loc
                # 已到 chatgpt.com 主页就够
                parsed = urlparse(current)
                if "chatgpt.com" in (parsed.netloc or "") and "/api/auth/callback" not in current:
                    # 再 GET 一下主页，让 cookie 全部落地
                    try:
                        self.session.get(current, timeout=20, allow_redirects=True)
                    except Exception:
                        pass
                    break
            # 与 `get_auth_session` 同一套兜底读法（朴素 get 在 curl_cffi 的
            # 分域隔离下可能读不到，那样会把「已成功」误报成「失败」）。
            return bool(self._extract_session_cookie())
        except Exception as e:
            logger.warning(f"消费 callback 失败: {e}")
            return False
