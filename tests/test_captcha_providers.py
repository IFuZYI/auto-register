"""打码服务（CapMonster / YesCaptcha / AI 视觉）的契约与降级链。

三个 provider 的能力**不一样**，这里把差异钉成测试：

| provider | AWS WAF | Cloudflare Turnstile | 图片/3×3 分类 |
| --- | --- | --- | --- |
| CapMonster（`AmazonTask`） | ✅ 返回 `aws-waf-token` | ✅ | ✅（`ImageToTextTask`） |
| YesCaptcha | ❌ 只有 `AwsClassification`（返回要点的格子，**不返 token**） | ✅ | ✅ |
| AI 视觉（OpenAI 兼容） | ⚠️ 只能识别图片/格子 | ❌ | ✅ |

AWS Builder ID 的注册是纯 HTTP：没有浏览器可以「点格子」，所以只有**返回
token/cookie** 的 provider 能解锁它 —— 这就是降级链默认把 CapMonster 放第一、
YesCaptcha 第二、AI 最后的原因（AI 兜底用于图片类验证码，不冒充能解 WAF）。

运行：`.venv/bin/python -m pytest tests/test_captcha_providers.py -q`
"""
from __future__ import annotations

import json
import unittest


class _Response:
    def __init__(self, status_code: int = 200, body: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body
        self.text = text if body is None else json.dumps(body, ensure_ascii=False)

    def json(self):
        return json.loads(json.dumps(self._body)) if self._body is not None else {}


class _FakeSession:
    """按顺序喂响应；记录每次请求。"""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.headers = {}
        self.cookies = {}

    def post(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def get(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass


class CapMonsterTests(unittest.TestCase):
    def test_aws_waf_returns_the_waf_token(self):
        from core.captcha_providers import CapMonsterSolver

        session = _FakeSession(
            [
                _Response(body={"errorId": 0, "taskId": 42}),
                _Response(
                    body={
                        "errorId": 0,
                        "status": "ready",
                        "solution": {
                            "cookies": {"aws-waf-token": "waf-token-1"},
                            "userAgent": "UA-1",
                        },
                    }
                ),
            ]
        )
        solver = CapMonsterSolver(api_key="key-1", session=session, sleep=lambda _s: None)
        token = solver.solve_aws_waf(
            website_url="https://us-east-1.signin.aws/platform/d-x/login",
            challenge_script="https://abc.captcha-sdk.awswaf.com/abc/challenge.js",
            captcha_script="https://abc.captcha-sdk.awswaf.com/abc/captcha.js",
            website_key="key-from-goku",
            context="ctx-1",
            iv="iv-1",
            user_agent="UA-from-client",
        )
        self.assertEqual(token, "waf-token-1")

        create = session.requests[0]
        self.assertTrue(create["url"].endswith("/createTask"))
        task = create["json"]["task"]
        self.assertEqual(task["type"], "AmazonTask")
        self.assertEqual(task["websiteKey"], "key-from-goku")
        self.assertEqual(task["context"], "ctx-1")
        self.assertEqual(task["iv"], "iv-1")
        self.assertTrue(task["cookieSolution"], "必须要求返回 cookie，否则拿不到 aws-waf-token")
        self.assertEqual(task["userAgent"], "UA-from-client")
        self.assertEqual(create["json"]["clientKey"], "key-1")
        self.assertEqual(session.requests[1]["json"]["taskId"], 42)

    def test_solver_errors_surface(self):
        from core.captcha_providers import CaptchaProviderError, CapMonsterSolver

        session = _FakeSession([_Response(body={"errorId": 1, "errorCode": "ERROR_KEY_DENIED", "errorDescription": "bad key"})])
        solver = CapMonsterSolver(api_key="bad", session=session, sleep=lambda _s: None)
        with self.assertRaises(CaptchaProviderError):
            solver.solve_aws_waf(
                website_url="https://x/",
                challenge_script="https://x/challenge.js",
                captcha_script="",
                website_key="k",
                context="c",
                iv="i",
                user_agent="ua",
            )

    def test_unsolvable_status_raises(self):
        from core.captcha_providers import CaptchaProviderError, CapMonsterSolver

        session = _FakeSession(
            [
                _Response(body={"errorId": 0, "taskId": 1}),
                _Response(body={"errorId": 1, "status": "ready", "errorCode": "ERROR_CAPTCHA_UNSOLVABLE"}),
            ]
        )
        solver = CapMonsterSolver(api_key="k", session=session, sleep=lambda _s: None)
        with self.assertRaises(CaptchaProviderError):
            solver.solve_aws_waf(
                website_url="https://x/",
                challenge_script="https://x/challenge.js",
                captcha_script="",
                website_key="k",
                context="c",
                iv="i",
                user_agent="ua",
            )

    def test_turnstile_task(self):
        from core.captcha_providers import CapMonsterSolver

        session = _FakeSession(
            [
                _Response(body={"errorId": 0, "taskId": 7}),
                _Response(body={"errorId": 0, "status": "ready", "solution": {"token": "ts-1"}}),
            ]
        )
        solver = CapMonsterSolver(api_key="k", session=session, sleep=lambda _s: None)
        self.assertEqual(solver.solve_turnstile("https://x/", "site-key"), "ts-1")
        self.assertEqual(session.requests[0]["json"]["task"]["type"], "TurnstileTaskProxyless")


class YesCaptchaAwsTests(unittest.TestCase):
    def test_aws_classification_is_not_a_token_solver(self):
        """YesCaptcha 的 AWS 任务只返回格子，不能当 token 用 —— 必须明确报错。"""
        from core.base_captcha import YesCaptcha
        from core.captcha_providers import CaptchaProviderError

        solver = YesCaptcha("key")
        with self.assertRaises(CaptchaProviderError):
            solver.solve_aws_waf(
                website_url="https://x/",
                challenge_script="",
                captcha_script="",
                website_key="k",
                context="c",
                iv="i",
                user_agent="ua",
            )


class AiVisionTests(unittest.TestCase):
    def test_solve_image_uses_openai_compatible_endpoint(self):
        from core.captcha_providers import AiVisionSolver

        session = _FakeSession(
            [
                _Response(
                    body={
                        "choices": [
                            {"message": {"content": "AB12"}}
                        ]
                    }
                )
            ]
        )
        solver = AiVisionSolver(
            base_url="https://api.example.com",
            api_key="sk-test",
            model="vision-model",
            session=session,
        )
        answer = solver.solve_image("aGVsbG8=", prompt="读出图片里的字符")
        self.assertEqual(answer, "AB12")
        request = session.requests[0]
        self.assertTrue(request["url"].endswith("/v1/chat/completions"))
        self.assertEqual(request["headers"]["Authorization"], "Bearer sk-test")
        payload = request["json"]
        self.assertEqual(payload["model"], "vision-model")
        content = payload["messages"][-1]["content"]
        # 图片以 data URI 传（OpenAI 兼容视觉接口的通用形状）
        self.assertTrue(any("image_url" in part for part in content if isinstance(part, dict)))

    def test_classify_grid_returns_tile_indexes(self):
        from core.captcha_providers import AiVisionSolver

        session = _FakeSession(
            [_Response(body={"choices": [{"message": {"content": "1,4,5"}}]})]
        )
        solver = AiVisionSolver(
            base_url="https://api.example.com",
            api_key="sk-test",
            model="vision-model",
            session=session,
        )
        indexes = solver.classify_grid("aGVsbG8=", question="Select all hats")
        self.assertEqual(indexes, [1, 4, 5])

    def test_reasoning_models_get_enough_budget(self):
        """推理模型（deepseek-v4.1-flash 之类）会先花 token 思考，预算太小会返回空 content。"""
        from core.captcha_providers import AiVisionSolver

        session = _FakeSession([_Response(body={"choices": [{"message": {"content": "ok"}}]})])
        solver = AiVisionSolver(
            base_url="https://api.example.com", api_key="k", model="m", session=session
        )
        solver.solve_image("aGVsbG8=")
        self.assertGreaterEqual(session.requests[0]["json"]["max_tokens"], 256)

    def test_empty_content_raises_instead_of_returning_blank(self):
        from core.captcha_providers import AiVisionSolver, CaptchaProviderError

        session = _FakeSession(
            [
                _Response(
                    body={
                        "choices": [
                            {"message": {"content": "", "reasoning_content": "想了很久"}}
                        ]
                    }
                )
            ]
        )
        solver = AiVisionSolver(
            base_url="https://api.example.com", api_key="k", model="m", session=session
        )
        with self.assertRaises(CaptchaProviderError):
            solver.solve_image("aGVsbG8=")

    def test_aws_waf_is_not_supported(self):
        from core.captcha_providers import AiVisionSolver, CaptchaProviderError

        solver = AiVisionSolver(base_url="https://x", api_key="k", model="m", session=_FakeSession([]))
        with self.assertRaises(CaptchaProviderError):
            solver.solve_aws_waf(
                website_url="https://x/",
                challenge_script="",
                captcha_script="",
                website_key="k",
                context="c",
                iv="i",
                user_agent="ua",
            )


class ChainTests(unittest.TestCase):
    def test_chain_prefers_the_first_provider_that_works(self):
        from core.captcha_providers import CaptchaChain, CaptchaProviderError

        class _Boom:
            name = "capmonster"

            def solve_aws_waf(self, **kwargs):
                raise CaptchaProviderError("capmonster", "余额不足")

        class _Ok:
            name = "backup"

            def solve_aws_waf(self, **kwargs):
                return "token-from-backup"

        chain = CaptchaChain([_Boom(), _Ok()])
        self.assertEqual(chain.solve_aws_waf(website_url="x"), "token-from-backup")

    def test_chain_raises_when_everything_fails(self):
        from core.captcha_providers import CaptchaChain, CaptchaProviderError

        class _Boom:
            name = "p1"

            def solve_aws_waf(self, **kwargs):
                raise CaptchaProviderError("p1", "boom")

        with self.assertRaises(CaptchaProviderError):
            CaptchaChain([_Boom()]).solve_aws_waf(website_url="x")

    def test_chain_skips_providers_that_do_not_support_the_capability(self):
        from core.captcha_providers import CaptchaChain, CaptchaProviderError

        class _Unsupported:
            name = "yescaptcha"

            def solve_aws_waf(self, **kwargs):
                raise CaptchaProviderError("yescaptcha", "不支持 AWS WAF token")

        class _Ok:
            name = "capmonster"

            def solve_aws_waf(self, **kwargs):
                return "waf-1"

        self.assertEqual(
            CaptchaChain([_Unsupported(), _Ok()]).solve_aws_waf(website_url="x"), "waf-1"
        )


class ConfigWiringTests(unittest.TestCase):
    def test_keys_are_writable_config(self):
        from api.config import CONFIG_KEYS

        for key in ("capmonster_key", "ai_captcha_url", "ai_captcha_key", "ai_captcha_model"):
            self.assertIn(key, CONFIG_KEYS, f"{key} 不在白名单里 —— 界面保存会被静默忽略")

    def test_secret_keys_are_masked(self):
        from api.config import SECRET_CONFIG_KEYS

        self.assertIn("capmonster_key", SECRET_CONFIG_KEYS)
        self.assertIn("ai_captcha_key", SECRET_CONFIG_KEYS)

    def test_build_chain_reads_config(self):
        from core.captcha_providers import build_captcha_chain

        chain = build_captcha_chain(
            {
                "capmonster_key": "cm-key",
                "yescaptcha_key": "",
                "ai_captcha_url": "https://api.example.com",
                "ai_captcha_key": "sk-1",
                "ai_captcha_model": "vision",
            },
            session_factory=lambda: _FakeSession([]),
        )
        names = [provider.name for provider in chain.providers]
        self.assertEqual(names, ["capmonster", "ai"], "没配 key 的 provider 不该进链")

    def test_custom_provider_order_is_respected(self):
        from core.captcha_providers import build_captcha_chain

        chain = build_captcha_chain(
            {
                "capmonster_key": "cm-key",
                "yescaptcha_key": "yc-key",
                "captcha_provider_order": "yescaptcha,capmonster",
            },
            session_factory=lambda: _FakeSession([]),
        )
        self.assertEqual(
            [provider.name for provider in chain.providers],
            ["yescaptcha", "capmonster"],
        )

    def test_no_arg_build_reads_order_from_the_store(self):
        from unittest import mock

        from core.captcha_providers import build_captcha_chain

        values = {
            "capmonster_key": "cm-key",
            "yescaptcha_key": "yc-key",
            "captcha_provider_order": "yescaptcha,capmonster",
        }
        with mock.patch("core.captcha_providers.captcha_config_from_store", lambda: values):
            chain = build_captcha_chain(session_factory=lambda: _FakeSession([]))
        self.assertEqual(
            [provider.name for provider in chain.providers],
            ["yescaptcha", "capmonster"],
            "无参调用也要读到 captcha_provider_order",
        )


if __name__ == "__main__":
    unittest.main()