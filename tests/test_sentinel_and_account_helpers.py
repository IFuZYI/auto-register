"""Sentinel 取 token 入口 + 账号状态 / 批量选号共享件的单元测试。

覆盖三个此前零覆盖或低覆盖的模块：

- ``platforms/chatgpt/protocol/sentinel.py`` —— ``get_sentinel_token`` 的四条出口：
  成功原样透传、falsy 结果抛 RuntimeError、ImportError 转 RuntimeError、
  TLS 瞬断异常原样上抛（其余异常包成 RuntimeError）；
- ``services/account_status.py`` —— ``access_token_expired`` 的边界：
  已过期 / 未过期 / 非 JWT / 缺 token / now_seconds 覆盖 / 坏 get_extra 回落 /
  token 列兜底（chatgpt 认、grok 不认）；
- ``services/chatgpt_account_selection.py`` —— ``normalize_account_ids`` 的去重与
  清洗，``select_chatgpt_accounts`` 的 ids / all_filtered / keep / 上限 / 报错各路径。

Sentinel 部分全部 mock ``sentinel_quickjs`` 的模块属性，不跑真实 Node 子进程；
选号部分用 conftest 隔离出来的真实 SQLite（默认库，chatgpt 未分库）。
"""

from __future__ import annotations

import base64
import json
import sys
import time
import unittest
from unittest import mock

from core.db import AccountModel, engine
from platforms.chatgpt.protocol.sentinel import DEFAULT_UA, get_sentinel_token
from services.account_status import access_token_expired
from services.chatgpt_account_selection import (
    MAX_BATCH_ACCOUNTS,
    normalize_account_ids,
    select_chatgpt_accounts,
)

QUICKJS_PATCH_TARGET = (
    "platforms.chatgpt.protocol.sentinel_quickjs.get_sentinel_token_via_quickjs"
)
TLS_CHECK_PATCH_TARGET = (
    "platforms.chatgpt.protocol.http_client._is_tls_handshake_error"
)


# --------------------------------------------------------------------- JWT 构造


def _jwt(claims: dict, header: dict | None = None) -> str:
    """构造一个 payload 段可解的假 JWT（不验签，只需 base64 合法）。"""

    def _seg(data: dict) -> str:
        raw = json.dumps(data).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_seg(header or {'alg': 'RS256'})}.{_seg(claims)}.sig"


EXPIRED_AT = _jwt({"iat": 1_000, "exp": 1_000})       # exp 固定在 1970，必过期
VALID_AT = _jwt({"iat": 1_000, "exp": 10**12})         # exp 远在未来


# --------------------------------------------------------------- sentinel 入口


class SentinelTokenTests(unittest.TestCase):
    """``get_sentinel_token``：成功透传与各类失败的出口。"""

    def _call(self, **overrides):
        session = object()
        kwargs = dict(device_id="did-1")
        kwargs.update(overrides)
        return session, get_sentinel_token(session, **kwargs)

    def test_success_returns_quickjs_result_unchanged(self):
        sentinel = ("main-token", "so-token")
        with mock.patch(QUICKJS_PATCH_TARGET, return_value=sentinel) as quickjs:
            session, result = self._call()

        self.assertIs(result, sentinel, "成功时必须原样返回 QuickJS 的结果")
        self.assertIs(quickjs.call_args.args[0], session, "session 要原样传给 QuickJS")
        self.assertEqual(quickjs.call_args.kwargs["device_id"], "did-1")

    def test_kwargs_pass_through_with_mapped_names(self):
        """调用方参数必须按 QuickJS 侧的形参名转发（navigator_platform → platform 等）。"""
        with mock.patch(QUICKJS_PATCH_TARGET, return_value=("t", "s")) as quickjs:
            self._call(
                flow="oauth_create_account",
                user_agent="UA-X",
                sec_ch_ua="v=1",
                sec_ch_ua_platform="Windows",
                sec_ch_ua_mobile="?0",
                sec_ch_ua_full_version_list='"Chromium";v="145"',
                sec_ch_ua_arch="x86",
                sec_ch_ua_bitness="64",
                sec_ch_ua_model="model",
                sec_ch_ua_platform_version="10.0",
                screen="1920x1080",
                lang="zh-CN",
                lang_full="zh-CN,zh",
                browser_type="chrome",
                navigator_platform="Win32",
                navigator_vendor="Google Inc.",
                hardware_concurrency=8,
                device_memory=16,
                max_touch_points=0,
                device_pixel_ratio=1.0,
                timezone="Asia/Shanghai",
            )

        kwargs = quickjs.call_args.kwargs
        expected = {
            "device_id": "did-1",
            "flow": "oauth_create_account",
            "user_agent": "UA-X",
            "screen": "1920x1080",
            "lang": "zh-CN",
            "lang_full": "zh-CN,zh",
            "browser_type": "chrome",
            "platform": "Win32",            # navigator_platform 映射
            "vendor": "Google Inc.",        # navigator_vendor 映射
            "hardware_concurrency": 8,
            "device_memory": 16,
            "max_touch_points": 0,
            "device_pixel_ratio": 1.0,
            "timezone": "Asia/Shanghai",
            "sec_ch_ua_full_version_list": '"Chromium";v="145"',
            "sec_ch_ua_arch": "x86",
            "sec_ch_ua_bitness": "64",
            "sec_ch_ua_model": "model",
            "sec_ch_ua_platform_version": "10.0",
        }
        for key, value in expected.items():
            self.assertEqual(kwargs[key], value, key)
        self.assertEqual(set(kwargs), set(expected) | {"log"}, "转发的参数集合变了")
        self.assertTrue(callable(kwargs["log"]), "log 必须是可调用对象")
        kwargs["log"]("测试日志")  # 不抛即可（内部走 logger.info）

    def test_default_flow_and_ua(self):
        with mock.patch(QUICKJS_PATCH_TARGET, return_value=("t", "s")) as quickjs:
            self._call()

        self.assertEqual(quickjs.call_args.kwargs["flow"], "authorize_continue")
        self.assertEqual(quickjs.call_args.kwargs["user_agent"], DEFAULT_UA)

    def test_none_result_raises_runtime_error(self):
        """QuickJS 返回 None（主 token 缺失）→ RuntimeError，中止注册。"""
        with mock.patch(QUICKJS_PATCH_TARGET, return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                self._call()

        self.assertIn("Sentinel QuickJS 失败", str(ctx.exception))

    def test_falsy_result_raises_runtime_error(self):
        with mock.patch(QUICKJS_PATCH_TARGET, return_value=()):
            with self.assertRaises(RuntimeError) as ctx:
                self._call()

        self.assertIn("Sentinel QuickJS 失败", str(ctx.exception))

    def test_import_error_becomes_module_missing_runtime_error(self):
        """QuickJS 模块缺失 → RuntimeError('Sentinel QuickJS 模块缺失: ...')。"""
        with mock.patch.dict(
            sys.modules, {"platforms.chatgpt.protocol.sentinel_quickjs": None}
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self._call()

        self.assertIn("Sentinel QuickJS 模块缺失", str(ctx.exception))

    def test_runtime_error_passes_through_unchanged(self):
        """QuickJS 自己抛的 RuntimeError 原样上抛（不包成「异常」）。"""
        err = RuntimeError("PoW 算不出来")
        with mock.patch(QUICKJS_PATCH_TARGET, side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                self._call()

        self.assertIs(ctx.exception, err)
        self.assertEqual(str(ctx.exception), "PoW 算不出来")

    def test_tls_handshake_error_reraises_original(self):
        """链路级 TLS 瞬断（curl: (35)）原样上抛 —— 不能被包成 QuickJS 异常。"""
        err = ConnectionError("curl: (35) TLS connect error: OPENSSL_internal")
        with mock.patch(QUICKJS_PATCH_TARGET, side_effect=err):
            with self.assertRaises(ConnectionError) as ctx:
                self._call()

        self.assertIs(ctx.exception, err, "必须保留原异常对象与类型（classify_error 靠它判 network）")

    def test_tls_classified_error_reraises_original(self):
        """分类器判为 TLS 的异常同样原样上抛（含非网络类型）。"""
        err = OSError("weird transient")
        with mock.patch(QUICKJS_PATCH_TARGET, side_effect=err):
            with mock.patch(TLS_CHECK_PATCH_TARGET, return_value=True) as checker:
                with self.assertRaises(OSError) as ctx:
                    self._call()

        self.assertIs(ctx.exception, err)
        checker.assert_called_once_with(err)

    def test_other_exception_wrapped_as_runtime_error(self):
        """非 TLS 的意外异常 → RuntimeError('Sentinel QuickJS 异常: ...')。"""
        with mock.patch(QUICKJS_PATCH_TARGET, side_effect=ValueError("boom")):
            with mock.patch(TLS_CHECK_PATCH_TARGET, return_value=False) as checker:
                with self.assertRaises(RuntimeError) as ctx:
                    self._call()

        self.assertIn("Sentinel QuickJS 异常", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))
        checker.assert_called_once()


# ------------------------------------------------------- access_token_expired


class _ExtraOnly:
    """Account 风格：只有 .extra 字典。"""

    def __init__(self, extra: dict, token: str = ""):
        self.extra = extra
        self.token = token


class _GetterAccount:
    """AccountModel 风格：有 get_extra()（可注入坏行为），也有 .token 列。"""

    def __init__(self, extra: dict | None = None, token: str = "", getter=None):
        self.extra = extra if extra is not None else {}
        self.token = token
        if getter is not None:
            self.get_extra = getter  # type: ignore[method-assign]

    def get_extra(self) -> dict:
        return self.extra


class AccessTokenExpiredTests(unittest.TestCase):
    """``access_token_expired``：只认能解出且已过的 exp，其余一律 False。"""

    def test_expired_token_is_true(self):
        account = _ExtraOnly({"access_token": EXPIRED_AT})
        self.assertTrue(access_token_expired(account, platform="chatgpt"))

    def test_valid_token_is_false(self):
        account = _ExtraOnly({"access_token": VALID_AT})
        self.assertFalse(access_token_expired(account, platform="chatgpt"))

    def test_camel_case_alias_is_read(self):
        account = _ExtraOnly({"accessToken": EXPIRED_AT})
        self.assertTrue(access_token_expired(account, platform="chatgpt"))

    def test_now_seconds_override_decides_the_boundary(self):
        """exp=1000：now 恰好等于 exp 算过期（exp <= now）。"""
        account = _ExtraOnly({"access_token": _jwt({"exp": 1000})})
        self.assertFalse(
            access_token_expired(account, platform="chatgpt", now_seconds=999)
        )
        self.assertTrue(
            access_token_expired(account, platform="chatgpt", now_seconds=1000)
        )
        self.assertTrue(
            access_token_expired(account, platform="chatgpt", now_seconds=1001)
        )

    def test_missing_token_is_false(self):
        self.assertFalse(access_token_expired(_ExtraOnly({}), platform="chatgpt"))
        self.assertFalse(
            access_token_expired(_GetterAccount(), platform="chatgpt"),
            "没有 AT 时不能误判成过期",
        )

    def test_non_jwt_token_is_false(self):
        for bad in ("not-a-jwt", "a.b", "abc.def.ghi", "x" * 40):
            with self.subTest(token=bad):
                account = _ExtraOnly({"access_token": bad})
                self.assertFalse(
                    access_token_expired(account, platform="chatgpt"), bad
                )

    def test_no_exp_claim_is_false(self):
        """能解出 payload 但没有 exp → 不算过期（由探测结论决定）。"""
        account = _ExtraOnly({"access_token": _jwt({"iat": 1000, "sub": "u-1"})})
        self.assertFalse(access_token_expired(account, platform="chatgpt"))

    def test_exp_zero_or_negative_is_false(self):
        for exp in (0, -5):
            with self.subTest(exp=exp):
                account = _ExtraOnly({"access_token": _jwt({"exp": exp})})
                self.assertFalse(access_token_expired(account, platform="chatgpt"))

    def test_non_numeric_exp_is_false(self):
        account = _ExtraOnly({"access_token": _jwt({"exp": "abc"})})
        self.assertFalse(access_token_expired(account, platform="chatgpt"))

    def test_numeric_string_exp_is_parsed(self):
        account = _ExtraOnly({"access_token": _jwt({"exp": "1000"})})
        self.assertTrue(
            access_token_expired(account, platform="chatgpt", now_seconds=2000)
        )

    def test_broken_get_extra_falls_back_to_extra_attr(self):
        """get_extra() 抛异常时回落到 .extra（坏数据不该打断状态判定）。"""
        def _boom():
            raise RuntimeError("坏数据")

        account = _GetterAccount(extra={"access_token": EXPIRED_AT}, getter=_boom)
        self.assertTrue(access_token_expired(account, platform="chatgpt"))

    def test_non_dict_get_extra_falls_back_to_extra_attr(self):
        account = _GetterAccount(extra={"access_token": EXPIRED_AT}, getter=lambda: "junk")
        self.assertTrue(access_token_expired(account, platform="chatgpt"))

    def test_chatgpt_token_column_is_used_as_fallback(self):
        """chatgpt 的 token 列镜像 AT —— extra 没有时认它。"""
        account = _GetterAccount(token=EXPIRED_AT)
        self.assertTrue(access_token_expired(account, platform="chatgpt"))
        self.assertFalse(
            access_token_expired(_GetterAccount(token=VALID_AT), platform="chatgpt")
        )

    def test_grok_token_column_is_not_used_for_access_token(self):
        """grok 的 token 列镜像 SSO，不是 AT —— 读 AT 时绝不认它。"""
        account = _GetterAccount(token=EXPIRED_AT)
        self.assertFalse(
            access_token_expired(account, platform="grok"),
            "把 grok 的 SSO 列当 AT 解会误判过期",
        )

    def test_grok_extra_access_token_still_read(self):
        """extra 里的 AT 与平台无关，grok 也照读。"""
        account = _GetterAccount(extra={"access_token": EXPIRED_AT})
        self.assertTrue(access_token_expired(account, platform="grok"))

    def test_unknown_platform_has_no_token_column_fallback(self):
        account = _GetterAccount(token=EXPIRED_AT)
        self.assertFalse(access_token_expired(account, platform="other"))

    def test_real_account_model_extra_and_column(self):
        """真实 AccountModel：extra_json 与 token 列两条读路径都通。"""
        via_extra = AccountModel(
            platform="chatgpt", email="extra@x.ai", password="pw",
            extra_json=json.dumps({"access_token": EXPIRED_AT}),
        )
        self.assertTrue(access_token_expired(via_extra, platform="chatgpt"))

        via_column = AccountModel(
            platform="chatgpt", email="col@x.ai", password="pw", token=EXPIRED_AT,
        )
        self.assertTrue(access_token_expired(via_column, platform="chatgpt"))

    def test_expiry_uses_real_clock_when_now_seconds_omitted(self):
        """不传 now_seconds 时用真实时钟：exp 在 1 小时前 → 过期。"""
        past = _jwt({"iat": int(time.time()) - 7200, "exp": int(time.time()) - 3600})
        future = _jwt({"iat": int(time.time()), "exp": int(time.time()) + 86400})
        self.assertTrue(
            access_token_expired(_ExtraOnly({"access_token": past}), platform="chatgpt")
        )
        self.assertFalse(
            access_token_expired(_ExtraOnly({"access_token": future}), platform="chatgpt")
        )


# --------------------------------------------------------- normalize_account_ids


class NormalizeAccountIdsTests(unittest.TestCase):
    """去重、去非法值、保持调用方给的顺序。"""

    def test_none_and_empty_return_empty_list(self):
        self.assertEqual(normalize_account_ids(None), [])
        self.assertEqual(normalize_account_ids([]), [])

    def test_dedupes_drops_invalid_and_keeps_order(self):
        result = normalize_account_ids([3, "2", 3, -1, 0, "abc", None, 1, " 2 "])
        self.assertEqual(result, [3, 2, 1], "去重保序，<=0 / 非数字丢弃")

    def test_accepts_strings_and_ints(self):
        self.assertEqual(normalize_account_ids(["5", 4, "6"]), [5, 4, 6])
        self.assertEqual(normalize_account_ids([" 7 "]), [7])


# ------------------------------------------------------ select_chatgpt_accounts


class _SelectionCase(unittest.TestCase):
    """选号测试基类：清空 chatgpt 账号，提供建行助手。"""

    def setUp(self):
        from sqlmodel import Session, delete

        with Session(engine) as session:
            session.exec(
                delete(AccountModel).where(AccountModel.platform == "chatgpt")
            )
            session.commit()

    def _add(self, email: str, status: str = "registered", *,
             platform: str = "chatgpt", extra: dict | None = None, token: str = "") -> int:
        from sqlmodel import Session

        with Session(engine) as session:
            row = AccountModel(
                platform=platform, email=email, password="pw", status=status, token=token
            )
            if extra is not None:
                row.set_extra(extra)
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id)

    def _select(self, **kwargs):
        from sqlmodel import Session

        with Session(engine) as session:
            return select_chatgpt_accounts(session, **kwargs)


class SelectByIdsTests(_SelectionCase):
    """勾行按 id 走：只回匹配的，缺失 id 原样上报。"""

    def test_returns_matching_accounts_in_requested_order(self):
        first = self._add("a@x.ai")
        second = self._add("b@x.ai")
        third = self._add("c@x.ai")

        accounts, missing = self._select(account_ids=[third, first])

        self.assertEqual([row.id for row in accounts], [third, first])
        self.assertEqual(missing, [])

    def test_reports_missing_ids_and_keeps_partial_results(self):
        present = self._add("a@x.ai")

        accounts, missing = self._select(account_ids=[present, 99999, 88888])

        self.assertEqual([row.id for row in accounts], [present])
        self.assertEqual(missing, [99999, 88888])

    def test_all_missing_returns_empty_accounts(self):
        accounts, missing = self._select(account_ids=[99999, 88888])
        self.assertEqual(accounts, [])
        self.assertEqual(missing, [99999, 88888])

    def test_ids_are_deduped_before_lookup(self):
        row_id = self._add("a@x.ai")

        accounts, missing = self._select(account_ids=[row_id, row_id, str(row_id)])

        self.assertEqual([row.id for row in accounts], [row_id])
        self.assertEqual(missing, [])

    def test_other_platforms_are_not_returned(self):
        """id 可能撞号：grok 的行绝不能被当成 chatgpt 账号交出来。"""
        grok_id = self._add("g@x.ai", platform="grok")

        accounts, missing = self._select(account_ids=[grok_id])

        self.assertEqual(accounts, [])
        self.assertEqual(missing, [grok_id])


class SelectAllFilteredTests(_SelectionCase):
    """没勾行：把当前筛选条件原样带过来。"""

    def test_returns_every_chatgpt_account_without_filters(self):
        self._add("a@x.ai")
        self._add("b@x.ai")

        accounts, missing = self._select(all_filtered=True)

        self.assertEqual({row.email for row in accounts}, {"a@x.ai", "b@x.ai"})
        self.assertEqual(missing, [])

    def test_status_filter(self):
        self._add("ok@x.ai", status="registered")
        self._add("bad@x.ai", status="invalid")

        accounts, _ = self._select(all_filtered=True, status="invalid")

        self.assertEqual([row.email for row in accounts], ["bad@x.ai"])

    def test_email_contains_filter(self):
        self._add("alpha@x.ai")
        self._add("beta@x.ai")

        accounts, _ = self._select(all_filtered=True, email="alpha")

        self.assertEqual([row.email for row in accounts], ["alpha@x.ai"])

    def test_status_and_email_filters_combine(self):
        self._add("alpha@x.ai", status="invalid")
        self._add("alpha2@x.ai", status="registered")
        self._add("beta@x.ai", status="invalid")

        accounts, _ = self._select(all_filtered=True, email="alpha", status="invalid")

        self.assertEqual([row.email for row in accounts], ["alpha@x.ai"])

    def test_plus_status_filter_reads_plus_check_extra(self):
        self._add("eligible@x.ai", extra={"plus_check": {"status": "trial_eligible"}})
        self._add("free@x.ai", extra={"plus_check": {"status": "free"}})
        self._add("plain@x.ai")

        accounts, _ = self._select(all_filtered=True, plus_status="trial_eligible")

        self.assertEqual([row.email for row in accounts], ["eligible@x.ai"])

    def test_plus_status_filter_delegates_to_shared_helper(self):
        """plus_status 必须走共享筛选件（而不是在这里重写一遍判定）。"""
        self._add("a@x.ai")

        with mock.patch(
            "services.chatgpt_account_selection.filter_accounts_by_plus_status",
            return_value=[],
        ) as helper:
            accounts, _ = self._select(all_filtered=True, plus_status="trial_eligible")

        self.assertEqual(accounts, [])
        self.assertEqual(helper.call_count, 1)
        passed_accounts, passed_status = helper.call_args.args
        self.assertEqual([row.email for row in passed_accounts], ["a@x.ai"])
        self.assertEqual(passed_status, "trial_eligible")

    def test_empty_ids_with_all_filtered_still_works(self):
        self._add("a@x.ai")

        accounts, _ = self._select(account_ids=[], all_filtered=True)

        self.assertEqual([row.email for row in accounts], ["a@x.ai"])


class SelectKeepAndLimitsTests(_SelectionCase):
    """每个任务自己的条件（keep）与批量上限。"""

    def test_keep_filter_applies_to_ids_path(self):
        keep_row = self._add("bad@x.ai", status="invalid")
        self._add("ok@x.ai", status="registered")

        accounts, _ = self._select(
            account_ids=[keep_row, 99999],
            keep=lambda row: row.status == "invalid",
        )

        self.assertEqual([row.id for row in accounts], [keep_row])

    def test_keep_filter_applies_after_plus_status(self):
        self._add("a@x.ai", status="invalid",
                  extra={"plus_check": {"status": "trial_eligible"}})
        self._add("b@x.ai", status="registered",
                  extra={"plus_check": {"status": "trial_eligible"}})

        accounts, _ = self._select(
            all_filtered=True,
            plus_status="trial_eligible",
            keep=lambda row: row.status == "invalid",
        )

        self.assertEqual([row.email for row in accounts], ["a@x.ai"])

    def test_over_max_accounts_raises(self):
        for i in range(3):
            self._add(f"u{i}@x.ai")

        with self.assertRaises(ValueError) as ctx:
            self._select(all_filtered=True, max_accounts=2)

        self.assertIn("2", str(ctx.exception))

    def test_at_max_accounts_is_allowed(self):
        for i in range(2):
            self._add(f"u{i}@x.ai")

        accounts, _ = self._select(all_filtered=True, max_accounts=2)

        self.assertEqual(len(accounts), 2)

    def test_keep_can_shrink_below_limit(self):
        """上限检查在 keep 之后 —— 筛掉的行不该再触发超限。"""
        for i in range(3):
            self._add(f"u{i}@x.ai", status="invalid")

        accounts, _ = self._select(
            all_filtered=True,
            max_accounts=2,
            keep=lambda row: False,
        )

        self.assertEqual(accounts, [])

    def test_default_max_is_the_module_constant(self):
        self.assertEqual(MAX_BATCH_ACCOUNTS, 1000)

    def test_ids_beyond_sqlite_variable_limit_do_not_crash(self):
        """33000 个 id（超过 SQLite 变量上限 32766）不能炸。

        实测（修复前）：查询在**上限检查之前**执行 → `sqlite3.OperationalError:
        too many SQL variables` 炸穿成 500（refresh-token 任务端点实测）。
        修复：IN 查询分块（500 一批）。上限只统计**命中**的账号数，所以
        33000 个 id 里只有 1 个真实账号时正常返回（不触发超限）。
        """
        present = self._add("only@x.ai")
        ids = [present] + list(range(present + 1, present + 33001))

        accounts, missing = self._select(account_ids=ids)

        self.assertEqual([row.id for row in accounts], [present])
        self.assertEqual(len(missing), 33000)

    def test_neither_ids_nor_all_filtered_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._select()

        self.assertIn("account_ids", str(ctx.exception))

    def test_empty_ids_without_all_filtered_raises(self):
        with self.assertRaises(ValueError):
            self._select(account_ids=[])


if __name__ == "__main__":
    unittest.main()
