"""Plus 试用资格检测：判定规则与账号页筛选。"""

import json
import unittest
from unittest import mock

from platforms.chatgpt.status_probe import (
    PLUS_TRIAL_PROMO_ID,
    ProbeHTTPResult,
    classify_plus_trial,
    probe_plus_trial_status,
)
from services.chatgpt_account_state import (
    account_plus_status,
    filter_accounts_by_plus_status,
)


def _ok(entry: dict) -> ProbeHTTPResult:
    body = {"accounts": {"default": entry}}
    text = json.dumps(body)
    return ProbeHTTPResult(
        status_code=200, headers={}, body_text=text, body_json=body, error_code="", message="ok"
    )


def _rejected(status_code: int, body: str, error_code: str = "") -> ProbeHTTPResult:
    return ProbeHTTPResult(
        status_code=status_code,
        headers={},
        body_text=body,
        body_json={},
        error_code=error_code,
        message=body,
    )


class DummyAccount:
    def __init__(self, *, token="", extra=None, email="demo@example.com"):
        self.email = email
        self.token = token
        self.extra = dict(extra or {})
        self.user_id = ""


class PlusTrialClassifyTests(unittest.TestCase):
    def test_promo_campaign_means_the_free_month_is_still_available(self):
        verdict = classify_plus_trial(
            _ok(
                {
                    "account": {"plan_type": "free"},
                    "entitlement": {"has_active_subscription": False},
                    "eligible_promo_campaigns": {"plus": {"id": PLUS_TRIAL_PROMO_ID}},
                }
            )
        )

        self.assertEqual(verdict["status"], "trial_eligible")
        self.assertEqual(verdict["promo_id"], PLUS_TRIAL_PROMO_ID)
        self.assertEqual(verdict["plan"], "free")

    def test_free_account_without_the_campaign_is_just_free(self):
        verdict = classify_plus_trial(
            _ok(
                {
                    "account": {"plan_type": "free"},
                    "entitlement": {"has_active_subscription": False},
                    "eligible_promo_campaigns": {},
                }
            )
        )

        self.assertEqual(verdict["status"], "free")

    def test_other_campaigns_do_not_count_as_the_free_month(self):
        verdict = classify_plus_trial(
            _ok(
                {
                    "account": {"plan_type": "free"},
                    "entitlement": {},
                    "eligible_promo_campaigns": {"plus": {"id": "plus-20-percent-off"}},
                }
            )
        )

        self.assertEqual(verdict["status"], "free")

    def test_paid_plan_wins_over_a_still_listed_campaign(self):
        """已经在订阅里就别再说"可领"，促销字段有时还挂着。"""
        verdict = classify_plus_trial(
            _ok(
                {
                    "account": {"plan_type": "chatgptplusplan"},
                    "entitlement": {"has_active_subscription": True},
                    "eligible_promo_campaigns": {"plus": {"id": PLUS_TRIAL_PROMO_ID}},
                }
            )
        )

        self.assertEqual(verdict["status"], "plus_active")
        self.assertEqual(verdict["plan"], "plus")

    def test_active_subscription_counts_even_when_plan_type_is_missing(self):
        verdict = classify_plus_trial(
            _ok({"account": {}, "entitlement": {"has_active_subscription": True}})
        )

        self.assertEqual(verdict["status"], "plus_active")

    def test_deactivated_flag_is_a_ban(self):
        verdict = classify_plus_trial(
            _ok(
                {
                    "account": {"plan_type": "free", "is_deactivated": True},
                    "eligible_promo_campaigns": {"plus": {"id": PLUS_TRIAL_PROMO_ID}},
                }
            )
        )

        self.assertEqual(verdict["status"], "banned")

    def test_revoked_token_with_a_ban_reason_is_a_ban_not_an_expired_token(self):
        """封号时 AT 会一起被吊销，请求在 401 就被拒，走不到 is_deactivated。"""
        verdict = classify_plus_trial(
            _rejected(401, '{"detail":"Your account was deactivated for violating our policies."}')
        )

        self.assertEqual(verdict["status"], "banned")

    def test_plain_401_is_only_an_invalid_credential(self):
        verdict = classify_plus_trial(_rejected(401, '{"detail":"Could not parse your authentication token."}'))

        self.assertEqual(verdict["status"], "token_invalid")

    def test_forbidden_without_a_ban_reason_stays_an_error(self):
        verdict = classify_plus_trial(_rejected(403, '{"detail":"Cloudflare challenge"}'))

        self.assertEqual(verdict["status"], "error")
        self.assertEqual(verdict["http_status"], 403)

    def test_missing_account_data_is_an_error_not_a_free_account(self):
        body = {"accounts": {}}
        verdict = classify_plus_trial(
            ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text=json.dumps(body),
                body_json=body,
                error_code="",
                message="ok",
            )
        )

        self.assertEqual(verdict["status"], "error")


class PlusTrialProbeTests(unittest.TestCase):
    def test_probe_without_access_token_never_hits_the_network(self):
        with mock.patch("platforms.chatgpt.status_probe._probe_accounts_check") as called:
            verdict = probe_plus_trial_status(DummyAccount())

        called.assert_not_called()
        self.assertEqual(verdict["status"], "no_access_token")
        self.assertEqual(verdict["label"], "无 AT")
        self.assertTrue(verdict["checked_at"])

    def test_probe_labels_and_timestamps_the_verdict(self):
        with mock.patch(
            "platforms.chatgpt.status_probe._probe_accounts_check",
            return_value=_ok(
                {
                    "account": {"plan_type": "free"},
                    "eligible_promo_campaigns": {"plus": {"id": PLUS_TRIAL_PROMO_ID}},
                }
            ),
        ):
            verdict = probe_plus_trial_status(DummyAccount(token="at"))

        self.assertEqual(verdict["status"], "trial_eligible")
        self.assertEqual(verdict["label"], "可领首月免费")
        self.assertTrue(verdict["checked_at"])

    def test_network_failure_is_an_error_rather_than_a_crash(self):
        with mock.patch(
            "platforms.chatgpt.status_probe._probe_accounts_check",
            side_effect=RuntimeError("proxy refused"),
        ):
            verdict = probe_plus_trial_status(DummyAccount(token="at"))

        self.assertEqual(verdict["status"], "error")
        self.assertIn("proxy refused", verdict["message"])

    def test_probe_sends_the_account_and_device_headers(self):
        account = DummyAccount(token="at", extra={"cookies": "foo=1; oai-did=device-abc"})

        with mock.patch(
            "platforms.chatgpt.status_probe._perform_get",
            return_value=_ok({"account": {"plan_type": "free"}}),
        ) as perform:
            probe_plus_trial_status(account)

        headers = perform.call_args.kwargs["headers"]
        self.assertEqual(headers["OAI-Device-Id"], "device-abc")
        self.assertEqual(headers["Origin"], "https://chatgpt.com")

    def test_device_id_falls_back_to_a_stable_value_per_email(self):
        account = DummyAccount(token="at")

        seen = []
        with mock.patch(
            "platforms.chatgpt.status_probe._perform_get",
            return_value=_ok({"account": {"plan_type": "free"}}),
        ) as perform:
            probe_plus_trial_status(account)
            seen.append(perform.call_args.kwargs["headers"]["OAI-Device-Id"])
            probe_plus_trial_status(account)
            seen.append(perform.call_args.kwargs["headers"]["OAI-Device-Id"])

        self.assertTrue(seen[0])
        self.assertEqual(seen[0], seen[1])


class _Row:
    def __init__(self, extra):
        self._extra = extra

    def get_extra(self):
        return self._extra


class PlusStatusFilterTests(unittest.TestCase):
    def test_account_without_a_check_counts_as_unchecked(self):
        self.assertEqual(account_plus_status(_Row({})), "unchecked")
        self.assertEqual(account_plus_status(_Row({"plus_check": "junk"})), "unchecked")

    def test_filter_picks_only_the_wanted_status(self):
        rows = [
            _Row({"plus_check": {"status": "trial_eligible"}}),
            _Row({"plus_check": {"status": "free"}}),
            _Row({}),
        ]

        self.assertEqual(len(filter_accounts_by_plus_status(rows, "trial_eligible")), 1)
        self.assertEqual(len(filter_accounts_by_plus_status(rows, "unchecked")), 1)
        self.assertEqual(len(filter_accounts_by_plus_status(rows, "")), 3)


class PlusTrialEndpointTests(unittest.TestCase):
    """账号页要用到的两条链路：按 Plus 状态筛列表、跑一次检测并落库。"""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlmodel import Session, delete, select

        from api.accounts import router as accounts_router
        from api.actions import router as actions_router
        from core.db import AccountModel, engine
        from core.registry import load_all

        load_all()

        self.AccountModel = AccountModel
        self.engine = engine
        self.select = select

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            eligible = AccountModel(
                platform="chatgpt", email="eligible@example.com", password="pw", status="registered"
            )
            eligible.set_extra({"plus_check": {"status": "trial_eligible", "label": "可领首月免费"}})
            plain = AccountModel(
                platform="chatgpt", email="plain@example.com", password="pw", status="registered"
            )
            plain.set_extra({"access_token": "at"})
            session.add_all([eligible, plain])
            session.commit()

        app = FastAPI()
        app.include_router(accounts_router)
        app.include_router(actions_router)
        self.client = TestClient(app)

    def _account_id(self, email: str) -> int:
        from sqlmodel import Session

        with Session(self.engine) as session:
            row = session.exec(
                self.select(self.AccountModel).where(self.AccountModel.email == email)
            ).first()
            return row.id

    def _extra(self, email: str) -> dict:
        from sqlmodel import Session

        with Session(self.engine) as session:
            row = session.exec(
                self.select(self.AccountModel).where(self.AccountModel.email == email)
            ).first()
            return row.get_extra()

    def test_list_filters_by_plus_status(self):
        eligible = self.client.get("/accounts", params={"plus_status": "trial_eligible"}).json()
        unchecked = self.client.get("/accounts", params={"plus_status": "unchecked"}).json()
        everything = self.client.get("/accounts").json()

        self.assertEqual([item["email"] for item in eligible["items"]], ["eligible@example.com"])
        self.assertEqual(eligible["total"], 1)
        self.assertEqual([item["email"] for item in unchecked["items"]], ["plain@example.com"])
        self.assertEqual(everything["total"], 2)

    def test_check_writes_the_verdict_onto_the_account(self):
        with mock.patch(
            "platforms.chatgpt.status_probe._probe_accounts_check",
            return_value=_ok(
                {
                    "account": {"plan_type": "free"},
                    "eligible_promo_campaigns": {"plus": {"id": PLUS_TRIAL_PROMO_ID}},
                }
            ),
        ):
            response = self.client.post(
                f"/actions/chatgpt/{self._account_id('plain@example.com')}/check_plus_trial",
                json={"params": {}},
            )

        body = response.json()
        self.assertTrue(body["ok"])
        self.assertIn("可领首月免费", body["data"]["message"])
        self.assertEqual(self._extra("plain@example.com")["plus_check"]["status"], "trial_eligible")

    def test_failed_check_is_not_recorded_as_a_verdict(self):
        """没查出结论就别写库，否则这号会从"未检测"里消失。"""
        with mock.patch(
            "platforms.chatgpt.status_probe._probe_accounts_check",
            side_effect=RuntimeError("proxy refused"),
        ):
            response = self.client.post(
                f"/actions/chatgpt/{self._account_id('plain@example.com')}/check_plus_trial",
                json={"params": {}},
            )

        self.assertFalse(response.json()["ok"])
        self.assertNotIn("plus_check", self._extra("plain@example.com"))

    def test_batch_check_honours_the_plus_status_filter(self):
        with mock.patch(
            "platforms.chatgpt.status_probe._probe_accounts_check",
            return_value=_ok({"account": {"plan_type": "free"}}),
        ):
            response = self.client.post(
                "/actions/chatgpt/check_plus_trial/batch",
                json={"all_filtered": True, "plus_status": "unchecked", "params": {}},
            )

        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual([item["email"] for item in body["items"]], ["plain@example.com"])
        self.assertEqual(self._extra("plain@example.com")["plus_check"]["status"], "free")


if __name__ == "__main__":
    unittest.main()
