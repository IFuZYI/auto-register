"""warmup 撞上整族封锁时要换浏览器家族，不能只换出口 IP。

2026-08-21 线上注册连挂：CF 把 chrome 的 TLS 指纹整族 403（只给 __cf_bm），
safari / firefox 同一秒同一个 IP 都是 200。旧实现 4 次重试刻意保持指纹不变，
等于拿同一张脸连撞 4 次。
"""

import random
import unittest
from unittest import mock

from platforms.chatgpt.protocol import auth_flow as auth_flow_module
from platforms.chatgpt.protocol.auth_flow import AuthFlow
from platforms.chatgpt.protocol.fingerprint import (
    cross_family_impersonates,
    family_impersonates,
    fingerprint_for_impersonate,
    generate_fingerprint,
)


class _Cookies:
    def __init__(self, jar: dict):
        self._jar = jar

    def get_dict(self):
        return dict(self._jar)


class _Session:
    """按 impersonate 决定放行还是 403，模拟 CF 的整族封锁。"""

    def __init__(self, impersonate: str, blocked_family: str):
        self.impersonate = impersonate
        blocked = impersonate.startswith(blocked_family)
        self.status = 403 if blocked else 200
        jar = {"__cf_bm": "x"} if blocked else {"__cf_bm": "x", "oai-did": "did-123"}
        self.cookies = _Cookies(jar)

    def get(self, url, headers=None, timeout=None):
        return mock.Mock(status_code=self.status)


def _flow(start_impersonate: str) -> AuthFlow:
    flow = AuthFlow.__new__(AuthFlow)
    flow.config = mock.Mock(proxy=None)
    flow._fingerprint = fingerprint_for_impersonate(start_impersonate, generate_fingerprint())
    flow._ua = flow._fingerprint["user_agent"]
    flow._impersonate_candidates = family_impersonates(start_impersonate)
    flow._impersonate_idx = 0
    flow._navigation_headers = lambda: {"sec-ch-ua": flow._fingerprint.get("sec_ch_ua", "")}
    flow.session = _Session(start_impersonate, "chrome")
    return flow


class WarmupFamilyRotationTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(auth_flow_module.time, "sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_warmup(self, flow, blocked_family="chrome"):
        created: list[str] = []

        def _create(proxy=None, impersonate=None, user_agent=None):
            created.append(impersonate)
            return _Session(impersonate, blocked_family)

        with mock.patch.object(auth_flow_module, "create_http_session", _create):
            ok = flow.warmup()
        return ok, created

    def test_blocked_family_is_abandoned_after_the_first_failure(self):
        flow = _flow("chrome136")
        ok, created = self._run_warmup(flow)

        self.assertTrue(ok)
        # 第一次用初始的 chrome 撞墙，重试必须离开 chrome 家族
        self.assertTrue(created)
        self.assertFalse(
            [imp for imp in created if imp.startswith("chrome")],
            f"重试仍在 chrome 家族里打转: {created}",
        )

    def test_working_family_is_kept_for_the_rest_of_the_flow(self):
        flow = _flow("chrome136")
        ok, _ = self._run_warmup(flow)

        self.assertTrue(ok)
        imp = flow._fingerprint["impersonate"]
        self.assertFalse(imp.startswith("chrome"))
        # UA 和 client hints 必须跟着换，否则头自相矛盾
        self.assertEqual(flow._ua, flow._fingerprint["user_agent"])
        self.assertEqual(flow._fingerprint["sec_ch_ua"], "")
        # 同族回退列表也要换过来，免得之后 TLS 重试跳回被封的家族
        self.assertNotIn("chrome136", flow._impersonate_candidates)

    def test_no_rotation_needed_when_the_first_try_works(self):
        flow = _flow("firefox133")
        ok, created = self._run_warmup(flow)

        self.assertTrue(ok)
        self.assertEqual(created, [])
        self.assertEqual(flow._fingerprint["impersonate"], "firefox133")

    def test_gives_up_after_four_tries_when_everything_is_blocked(self):
        flow = _flow("chrome136")
        ok, created = self._run_warmup(flow, blocked_family="")

        # blocked_family="" 让所有 impersonate 都命中 startswith("") → 全 403
        self.assertFalse(ok)
        self.assertEqual(len(created), 3)


class CrossFamilyCandidateTests(unittest.TestCase):
    def test_candidates_skip_the_current_family(self):
        for _ in range(20):
            picks = cross_family_impersonates("chrome136", random.Random(_))
            self.assertTrue(picks)
            self.assertFalse([p for p in picks if p.startswith("chrome")])

    def test_one_candidate_per_other_family(self):
        picks = cross_family_impersonates("chrome136", random.Random(7))
        self.assertEqual(len(picks), 3)  # mac_safari / ios_safari / firefox
        self.assertEqual(len(set(picks)), len(picks))

    def test_unknown_impersonate_still_yields_candidates(self):
        picks = cross_family_impersonates("something_new", random.Random(1))
        self.assertEqual(len(picks), 4)

    def test_family_fallbacks_start_with_the_current_one(self):
        picks = family_impersonates("chrome136")
        self.assertEqual(picks[0], "chrome136")
        self.assertTrue(all(p.startswith("chrome") for p in picks))


if __name__ == "__main__":
    unittest.main()
