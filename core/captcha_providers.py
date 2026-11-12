"""打码服务：CapMonster / YesCaptcha / AI 视觉，以及它们的降级链。

## 为什么单独一个模块

`core/base_captcha.py` 是「浏览器流程用的验证码解决器」（Turnstile、手动、本地
Solver）。AWS Builder ID 的注册是**纯 HTTP**：一旦被 AWS WAF 拦下，需要的不是
「点格子」，而是一个 `aws-waf-token` cookie —— 这只有**返回 token/cookie 的
服务**能提供。本模块按能力把三类服务收敛成统一接口：

    CapMonsterSolver   AmazonTask → aws-waf-token；TurnstileTaskProxyless → token
    AiVisionSolver     OpenAI 兼容视觉接口 → 图片文字 / 3×3 格子索引
    CaptchaChain       按顺序试，跳过「不支持该能力」的 provider

**能力差异要显式暴露**（`solve_aws_waf` 不支持的 provider 直接抛
`CaptchaProviderError`，而不是返回空串假装成功）—— 静默失败会让注册流程在
「验证码这一步」白等一轮，日志里还看不出原因。

配置读取走 `core.config_store`（同属 core，不违反依赖方向）；HTTP 用
`requests`（公共 API 走系统 CA，不做 `verify=False`）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable, Optional

import requests

logger = logging.getLogger(__name__)

#: CapMonster 公共 API。
CAPMONSTER_API = "https://api.capmonster.cloud"
#: YesCaptcha 公共 API（与 `core/base_captcha.YesCaptcha` 同一个域名）。
YESCAPTCHA_API = "https://api.yescaptcha.com"

#: 轮询间隔与上限（秒）。
POLL_INTERVAL = 3
POLL_TIMEOUT = 180


class CaptchaProviderError(RuntimeError):
    """某个打码 provider 失败（带 provider 名，便于降级链记录原因）。"""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message


def _post_json(session: Any, url: str, payload: dict, *, timeout: int = 30) -> dict:
    response = session.post(url, json=payload, timeout=timeout)
    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - 非 JSON 响应是 provider 故障
        raise CaptchaProviderError("http", f"{url} 返回非 JSON: {type(exc).__name__}") from exc
    return data if isinstance(data, dict) else {}


def _check_error(data: dict, provider: str) -> None:
    """统一判错：`errorId != 0` 就是失败（两家都是这个约定）。"""
    error_id = data.get("errorId")
    if error_id in (None, 0, "0"):
        return
    raise CaptchaProviderError(
        provider,
        f"{data.get('errorCode') or data.get('errorId')} {data.get('errorDescription') or ''}".strip(),
    )


class CapMonsterSolver:
    """CapMonster Cloud（`AmazonTask` 能直接换 `aws-waf-token`）。"""

    name = "capmonster"

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str = CAPMONSTER_API,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: int = POLL_TIMEOUT,
    ) -> None:
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.session = session or requests.Session()
        self._sleep = sleep
        self.timeout = timeout

    # ---------------------------------------------------------------- 通用
    def _create(self, task: dict) -> int:
        data = _post_json(
            self.session,
            f"{self.api_base}/createTask",
            {"clientKey": self.api_key, "task": task},
        )
        _check_error(data, self.name)
        task_id = data.get("taskId")
        if task_id is None:
            raise CaptchaProviderError(self.name, f"createTask 未返回 taskId: {data}")
        return int(task_id)

    def _wait(self, task_id: int) -> dict:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            data = _post_json(
                self.session,
                f"{self.api_base}/getTaskResult",
                {"clientKey": self.api_key, "taskId": task_id},
            )
            _check_error(data, self.name)
            if data.get("status") == "ready":
                solution = data.get("solution")
                if not isinstance(solution, dict):
                    raise CaptchaProviderError(self.name, f"ready 但没有 solution: {data}")
                return solution
            self._sleep(POLL_INTERVAL)
        raise CaptchaProviderError(self.name, f"轮询超时（{self.timeout}s）")

    # ---------------------------------------------------------------- 能力
    def solve_aws_waf(
        self,
        *,
        website_url: str,
        challenge_script: str,
        captcha_script: str = "",
        website_key: str,
        context: str,
        iv: str,
        user_agent: str,
        proxy: str = "",
    ) -> str:
        """解 AWS WAF（Challenge / CAPTCHA），返回 `aws-waf-token` 的值。"""
        task: dict[str, Any] = {
            "type": "AmazonTask",
            "websiteURL": website_url,
            "websiteKey": website_key,
            "context": context,
            "iv": iv,
            "userAgent": user_agent,
            # 关键：要求返回 cookie 形态，否则拿到的是 voucher 而不是可用的 token
            "cookieSolution": True,
        }
        if challenge_script:
            task["challengeScript"] = challenge_script
        if captcha_script:
            task["captchaScript"] = captcha_script
        proxy_fields = _proxy_fields(proxy)
        if proxy_fields:
            task.update(proxy_fields)

        solution = self._wait(self._create(task))
        cookies = solution.get("cookies")
        token = ""
        if isinstance(cookies, dict):
            token = str(cookies.get("aws-waf-token") or cookies.get("aws_waf_token") or "")
        token = token or str(solution.get("token") or "")
        if not token:
            raise CaptchaProviderError(
                self.name, "AmazonTask 结果里没有 aws-waf-token（cookieSolution 是否生效？）"
            )
        return token

    def solve_turnstile(self, website_url: str, website_key: str) -> str:
        solution = self._wait(
            self._create(
                {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": website_url,
                    "websiteKey": website_key,
                }
            )
        )
        token = str(solution.get("token") or solution.get("gRecaptchaResponse") or "")
        if not token:
            raise CaptchaProviderError(self.name, "Turnstile 没有返回 token")
        return token

    def solve_image(self, image_b64: str) -> str:
        task = {"type": "ImageToTextTask", "body": image_b64}
        solution = self._wait(self._create(task))
        text = str(solution.get("text") or "")
        if not text:
            raise CaptchaProviderError(self.name, "图片识别没有返回文本")
        return text

    def get_balance(self) -> float:
        data = _post_json(
            self.session, f"{self.api_base}/getBalance", {"clientKey": self.api_key}
        )
        _check_error(data, self.name)
        try:
            return float(data.get("balance") or 0)
        except (TypeError, ValueError):
            return 0.0


class AiVisionSolver:
    """OpenAI 兼容的视觉模型（NewAPI 渠道）：图片文字 / 3×3 格子分类。

    **不支持** `solve_aws_waf`：它只能「看图说话」，没有 `aws-waf-token` 的
    签发能力。硬要它解 WAF 等于让注册流程在一个永远拿不到 token 的分支上空转。
    """

    name = "ai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "gpt-4o-mini",
        session: Any = None,
        timeout: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.session = session or requests.Session()
        self.timeout = timeout

    def _chat(self, content: list[dict[str, Any]], *, max_tokens: int = 512) -> str:
        # 预算给 512：推理型视觉模型（如 deepseek-v4.1-flash）会先花 token 输出
        # `reasoning_content`，64 会被思考吃光、`content` 返回空串（实测）。
        url = f"{self.base_url}/v1/chat/completions"
        response = self.session.post(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": 0,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=self.timeout,
        )
        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise CaptchaProviderError(self.name, f"非 JSON 响应: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise CaptchaProviderError(
                self.name, f"HTTP {response.status_code}: {str(data)[:200]}"
            )
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CaptchaProviderError(self.name, f"响应形状不对: {str(data)[:200]}") from exc
        cleaned = str(text or "").strip()
        if not cleaned:
            reasoning = ""
            try:
                reasoning = str(data["choices"][0]["message"].get("reasoning_content") or "")
            except (KeyError, IndexError, TypeError):
                reasoning = ""
            raise CaptchaProviderError(
                self.name,
                "模型只返回了 reasoning/空 content"
                + (f"（思考片段: {reasoning[:80]}…）" if reasoning else ""),
            )
        return cleaned

    @staticmethod
    def _image_part(image_b64: str) -> dict[str, Any]:
        payload = str(image_b64 or "").strip()
        if payload.startswith("data:"):
            url = payload
        else:
            url = f"data:image/png;base64,{payload}"
        return {"type": "image_url", "image_url": {"url": url}}

    def solve_image(self, image_b64: str, prompt: str = "读出图片里的字符，只回答字符本身") -> str:
        text = self._chat(
            [{"type": "text", "text": prompt}, self._image_part(image_b64)]
        )
        return text.strip().strip('"').strip("'")

    def classify_grid(self, image_b64: str, *, question: str = "") -> list[int]:
        """3×3 图片验证：返回要点击的格子下标（0-8）。"""
        prompt = (
            "这是一张 3x3 的验证码图片（格子按 0-8 从左到右、从上到下编号）。"
            f"题目：{question or '选择所有符合题目的图片'}。"
            "只回答需要点击的格子编号，用英文逗号分隔（例如 1,4,5）；没有就回答 none。"
        )
        text = self._chat([{"type": "text", "text": prompt}, self._image_part(image_b64)])
        return _parse_indexes(text)

    def solve_aws_waf(self, **_kwargs) -> str:
        raise CaptchaProviderError(
            self.name,
            "AI 视觉只能识别图片/格子，不能签发 aws-waf-token —— "
            "AWS WAF 需要 CapMonster 的 AmazonTask",
        )

    def solve_turnstile(self, *_args, **_kwargs) -> str:
        raise CaptchaProviderError(self.name, "AI 视觉不能解 Turnstile")


class YesCaptchaAwsSolver:
    """YesCaptcha：Turnstile 可用；AWS WAF 只有 `AwsClassification`（不返 token）。"""

    name = "yescaptcha"

    def __init__(self, *, api_key: str, api_base: str = YESCAPTCHA_API, session: Any = None) -> None:
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.session = session or requests.Session()

    def solve_turnstile(self, website_url: str, website_key: str) -> str:
        data = _post_json(
            self.session,
            f"{self.api_base}/createTask",
            {
                "clientKey": self.api_key,
                "task": {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": website_url,
                    "websiteKey": website_key,
                },
            },
        )
        _check_error(data, self.name)
        task_id = data.get("taskId")
        if task_id is None:
            raise CaptchaProviderError(self.name, f"createTask 未返回 taskId: {data}")
        deadline = time.monotonic() + POLL_TIMEOUT
        while time.monotonic() < deadline:
            result = _post_json(
                self.session,
                f"{self.api_base}/getTaskResult",
                {"clientKey": self.api_key, "taskId": task_id},
            )
            _check_error(result, self.name)
            if result.get("status") == "ready":
                token = str((result.get("solution") or {}).get("token") or "")
                if token:
                    return token
                raise CaptchaProviderError(self.name, "Turnstile 没有返回 token")
            time.sleep(POLL_INTERVAL)
        raise CaptchaProviderError(self.name, "轮询超时")

    def solve_aws_waf(self, **_kwargs) -> str:
        raise CaptchaProviderError(
            self.name,
            "YesCaptcha 的 AWS 任务（AwsClassification）只返回要点击的格子，"
            "不签发 aws-waf-token —— 纯协议链路用不了",
        )


class CaptchaChain:
    """按顺序尝试 provider；某个失败就换下一个，全失败抛最后一个原因。"""

    def __init__(self, providers: Iterable[Any]) -> None:
        self.providers = [item for item in providers if item is not None]

    def __bool__(self) -> bool:
        return bool(self.providers)

    def _run(self, capability: str, **kwargs) -> str:
        errors: list[str] = []
        for provider in self.providers:
            method = getattr(provider, capability, None)
            if not callable(method):
                continue
            try:
                return method(**kwargs)
            except CaptchaProviderError as exc:
                errors.append(f"{getattr(provider, 'name', '?')}: {exc.message}")
                logger.debug("打码 provider 失败: %s", exc)
            except Exception as exc:  # noqa: BLE001 - 单个 provider 崩溃不该拖垮整条链
                errors.append(f"{getattr(provider, 'name', '?')}: {type(exc).__name__}: {exc}")
                logger.debug("打码 provider 异常: %s", exc)
        detail = "；".join(errors) or "没有可用的打码 provider"
        raise CaptchaProviderError("chain", detail[:400])

    def solve_aws_waf(self, **kwargs) -> str:
        return self._run("solve_aws_waf", **kwargs)

    def solve_turnstile(self, **kwargs) -> str:
        return self._run("solve_turnstile", **kwargs)


def _proxy_fields(proxy: str) -> dict[str, Any]:
    """把 `scheme://user:pass@host:port` 拆成 CapMonster 的 proxy* 字段。"""
    from urllib.parse import urlparse

    text = str(proxy or "").strip()
    if not text:
        return {}
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    if not parsed.hostname or not parsed.port:
        return {}
    scheme = (parsed.scheme or "http").lower()
    if scheme.startswith("socks5"):
        proxy_type = "socks5"
    elif scheme.startswith("socks4"):
        proxy_type = "socks4"
    else:
        proxy_type = "http"
    fields: dict[str, Any] = {
        "proxyType": proxy_type,
        "proxyAddress": parsed.hostname,
        "proxyPort": int(parsed.port),
    }
    if parsed.username:
        fields["proxyLogin"] = parsed.username
    if parsed.password:
        fields["proxyPassword"] = parsed.password
    return fields


def _parse_indexes(text: str) -> list[int]:
    """从模型回答里抠出 0-8 的下标（容忍 `1,4,5` / `[1, 4]` / `1 4 5`）。"""
    import re

    found = [int(item) for item in re.findall(r"\d", str(text or ""))]
    return sorted({item for item in found if 0 <= item <= 8})


def build_captcha_chain(
    config: Optional[dict[str, Any]] = None,
    *,
    session_factory: Optional[Callable[[], Any]] = None,
) -> CaptchaChain:
    """按配置拼降级链（没配 key 的 provider 不进链）。

    顺序固定为 **CapMonster → YesCaptcha → AI**：前两个是「能解 token」的，
    AI 只兜底图片类（`solve_aws_waf` 会明确报不支持，链会继续往后/报错）。
    """
    values = config
    if values is None:
        # 统一走 `captcha_config_from_store()`：它包含 `captcha_provider_order`。
        # 手写键清单漏过一次这个键，导致「自定义顺序」永远不生效（审计 2026-11-13）。
        values = captcha_config_from_store()
    session_factory = session_factory or requests.Session
    provider_names = str(values.get("captcha_provider_order") or "").strip()
    order = [item.strip().lower() for item in provider_names.split(",") if item.strip()] or [
        "capmonster",
        "yescaptcha",
        "ai",
    ]

    built: dict[str, Any] = {}
    capmonster_key = str(values.get("capmonster_key") or "").strip()
    if capmonster_key:
        built["capmonster"] = CapMonsterSolver(
            api_key=capmonster_key, session=session_factory()
        )
    yescaptcha_key = str(values.get("yescaptcha_key") or "").strip()
    if yescaptcha_key:
        built["yescaptcha"] = YesCaptchaAwsSolver(
            api_key=yescaptcha_key, session=session_factory()
        )
    ai_url = str(values.get("ai_captcha_url") or "").strip()
    ai_key = str(values.get("ai_captcha_key") or "").strip()
    if ai_url and ai_key:
        built["ai"] = AiVisionSolver(
            base_url=ai_url,
            api_key=ai_key,
            model=str(values.get("ai_captcha_model") or "gpt-4o-mini").strip() or "gpt-4o-mini",
            session=session_factory(),
        )
    unknown = [name for name in order if name not in {"capmonster", "yescaptcha", "ai"}]
    if unknown:
        logger.warning("captcha_provider_order 含未知 provider（已忽略）: %s", unknown)
    return CaptchaChain([built[name] for name in order if name in built])


def captcha_config_from_store() -> dict[str, str]:
    """从配置库读打码相关的键（面板「全局配置 → 验证码」写入的就是这几个）。"""
    from core.config_store import config_store

    return {
        key: str(config_store.get(key, "") or "")
        for key in (
            "capmonster_key",
            "yescaptcha_key",
            "ai_captcha_url",
            "ai_captcha_key",
            "ai_captcha_model",
            "captcha_provider_order",
        )
    }


__all__ = [
    "AiVisionSolver",
    "CAPMONSTER_API",
    "CaptchaChain",
    "CaptchaProviderError",
    "CapMonsterSolver",
    "YesCaptchaAwsSolver",
    "build_captcha_chain",
    "captcha_config_from_store",
]