"""面板对比：时间解析、凭证比对、对比编排、缓存。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from services.panel_comparison import (
    RemoteAccount,
    STATE_CREDENTIAL_DIFF,
    STATE_LOCAL_ONLY,
    STATE_REMOTE_ONLY,
    STATE_SYNCED,
    STATE_UNKNOWN_CREDENTIAL,
    STATE_UNKNOWN_TIME,
    build_comparison,
    compare_by_hour,
    compare_credentials,
    diff_fields,
    hour_bucket,
    parse_timestamp,
    summarize,
)
from services.panel_comparison_cache import clear_cache, get_panel_comparison


def _dt(text: str) -> datetime:
    return parse_timestamp(text)


class ParseTimestampTests(unittest.TestCase):
    """两边的时间写法都要认：远端带偏移、本地是 UTC datetime。"""

    def test_iso_with_offset(self):
        self.assertEqual(
            parse_timestamp("2026-03-31T12:00:00+08:00"),
            datetime(2026, 3, 31, 4, 0, tzinfo=timezone.utc),
        )

    def test_iso_with_z(self):
        self.assertEqual(
            parse_timestamp("2026-03-31T04:00:00Z"),
            datetime(2026, 3, 31, 4, 0, tzinfo=timezone.utc),
        )

    def test_naive_iso_is_treated_as_utc(self):
        parsed = parse_timestamp("2026-03-31T04:00:00")
        self.assertEqual(parsed, datetime(2026, 3, 31, 4, 0, tzinfo=timezone.utc))

    def test_datetime_passthrough(self):
        value = datetime(2026, 3, 31, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_timestamp(value), value)

    def test_naive_datetime_gets_utc(self):
        parsed = parse_timestamp(datetime(2026, 3, 31, 4, 0))
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_epoch_seconds_and_millis_agree(self):
        seconds = parse_timestamp(1743393600)
        millis = parse_timestamp(1743393600000)
        self.assertEqual(seconds, millis)

    def test_empty_values_are_none(self):
        for value in (None, "", "   ", "not a date"):
            self.assertIsNone(parse_timestamp(value), repr(value))


class CompareByHourTests(unittest.TestCase):
    """时间比对 —— 现在只作**辅助信息**（凭证不同时说明是哪边动的）。"""

    def test_same_hour_is_time_synced(self):
        left = _dt("2026-03-31T04:10:00Z")
        right = _dt("2026-03-31T04:55:00Z")
        self.assertEqual(compare_by_hour(left, right), "time_synced")

    def test_different_hours_reports_who_is_newer(self):
        older = _dt("2026-03-31T04:00:00Z")
        newer = _dt("2026-03-31T05:00:00Z")
        self.assertEqual(compare_by_hour(older, newer), "remote_newer")
        self.assertEqual(compare_by_hour(newer, older), "local_newer")

    def test_a_one_second_gap_across_the_hour_boundary_counts(self):
        # 04:59:59 → 05:00:00：跨了小时档就该报出来
        left = _dt("2026-03-31T04:59:59Z")
        right = _dt("2026-03-31T05:00:00Z")
        self.assertEqual(compare_by_hour(left, right), "remote_newer")

    def test_timezones_are_normalized_before_comparing(self):
        # 同一个瞬间的两种写法 → 一致
        left = _dt("2026-03-31T12:00:00+08:00")
        right = _dt("2026-03-31T04:00:00Z")
        self.assertEqual(compare_by_hour(left, right), "time_synced")

    def test_missing_side_is_unknown(self):
        self.assertEqual(compare_by_hour(None, _dt("2026-03-31T04:00:00Z")), STATE_UNKNOWN_TIME)
        self.assertEqual(compare_by_hour(_dt("2026-03-31T04:00:00Z"), None), STATE_UNKNOWN_TIME)


class CompareCredentialsTests(unittest.TestCase):
    """用户口径：「本地和远端同步是判断 AT 这些是否相同，AT、RT 全相同就是同步」。"""

    def test_all_credentials_identical_is_synced(self):
        local = {"access_token": "at-1", "refresh_token": "rt-1", "session_token": "st-1"}
        remote = {"access_token": "at-1", "refresh_token": "rt-1", "session_token": "st-1"}
        state, diff = compare_credentials(local, remote)
        self.assertEqual(state, STATE_SYNCED)
        self.assertEqual(diff, [])

    def test_different_access_token_is_not_synced(self):
        local = {"access_token": "at-1", "refresh_token": "rt-1"}
        remote = {"access_token": "at-2", "refresh_token": "rt-1"}
        state, diff = compare_credentials(local, remote)
        self.assertEqual(state, STATE_CREDENTIAL_DIFF)
        self.assertEqual(diff, ["access_token"])

    def test_different_refresh_token_is_not_synced(self):
        """RT 不同也算不同步 —— 用户明确说「AT、RT 这种全相同就是同步」。"""
        local = {"access_token": "at-1", "refresh_token": "rt-1"}
        remote = {"access_token": "at-1", "refresh_token": "rt-2"}
        state, diff = compare_credentials(local, remote)
        self.assertEqual(state, STATE_CREDENTIAL_DIFF)
        self.assertEqual(diff, ["refresh_token"])

    def test_multiple_differences_are_all_reported(self):
        local = {"access_token": "at-1", "refresh_token": "rt-1"}
        remote = {"access_token": "at-2", "refresh_token": "rt-2"}
        state, diff = compare_credentials(local, remote)
        self.assertEqual(state, STATE_CREDENTIAL_DIFF)
        self.assertEqual(sorted(diff), ["access_token", "refresh_token"])

    def test_camel_case_aliases_are_recognized(self):
        """落库路径会写 camelCase，只认蛇形会把有值的账号当成"没有凭证"。"""
        local = {"accessToken": "at-1", "refreshToken": "rt-1"}
        remote = {"access_token": "at-1", "refresh_token": "rt-1"}
        state, _ = compare_credentials(local, remote)
        self.assertEqual(state, STATE_SYNCED)

    def test_sso_is_compared_for_grok(self):
        local = {"sso": "sso-1"}
        remote = {"sso_token": "sso-1"}
        state, _ = compare_credentials(local, remote)
        self.assertEqual(state, STATE_SYNCED)
        state2, diff2 = compare_credentials({"sso": "sso-1"}, {"sso_token": "sso-2"})
        self.assertEqual(state2, STATE_CREDENTIAL_DIFF)
        self.assertEqual(diff2, ["sso"])

    def test_one_sided_credentials_are_skipped_not_reported_as_diff(self):
        """远端不返回 RT 时，本地有值不算"不同" —— 硬报差异是误报。"""
        local = {"access_token": "at-1", "refresh_token": "rt-1"}
        remote = {"access_token": "at-1"}
        state, diff = compare_credentials(local, remote)
        self.assertEqual(state, STATE_SYNCED)
        self.assertEqual(diff, [])

    def test_no_comparable_credentials_is_unknown_not_synced(self):
        """比不了要说"比不了"，不能当"一致" —— 那是错误的安全感。"""
        state, diff = compare_credentials({}, {})
        self.assertEqual(state, STATE_UNKNOWN_CREDENTIAL)
        self.assertEqual(diff, [])

        # 只有一边有凭证也算比不了
        state2, _ = compare_credentials({"access_token": "at-1"}, {})
        self.assertEqual(state2, STATE_UNKNOWN_CREDENTIAL)

    def test_blank_values_do_not_count_as_present(self):
        state, _ = compare_credentials({"access_token": "  "}, {"access_token": ""})
        self.assertEqual(state, STATE_UNKNOWN_CREDENTIAL)

    def test_compared_count_matches_the_verdict(self):
        """「比了几个字段」与结论必须同源 —— 否则界面会撒谎。

        `_count_compared` 与 `compare_credentials` 曾各写一份「两边都有值」的
        判定；`CREDENTIAL_FIELDS` 增删时两处会走偏（界面说「比了 2 个」，
        实际只比了 1 个）。现在共用 `_comparable_fields`。
        """
        from services.panel_comparison import _comparable_fields, _count_compared

        local = {"access_token": "at-1", "refresh_token": "rt-1"}
        remote = {"access_token": "at-1", "id_token": "id-1"}
        # 只有 access_token 两边都有
        self.assertEqual(_count_compared(local, remote), 1)
        self.assertEqual(len(_comparable_fields(local, remote)), 1)

        # 两边各两个字段都齐 → 2
        both = {"access_token": "at-1", "refresh_token": "rt-1"}
        self.assertEqual(_count_compared(both, both), 2)

        # 一个都比不了 → 0，且结论是 unknown 而不是 synced
        self.assertEqual(_count_compared({}, {}), 0)
        state, _ = compare_credentials({}, {})
        self.assertEqual(state, STATE_UNKNOWN_CREDENTIAL)

    def test_difference_names_come_from_the_shared_helper(self):
        """差异字段名与 `_comparable_fields` 的规范名一致（前端展示稳定）。"""
        from services.panel_comparison import _comparable_fields

        local = {"access_token": "at-old"}
        remote = {"accessToken": "at-new"}   # camelCase 别名，规范名仍是 access_token
        pairs = _comparable_fields(local, remote)
        self.assertEqual([name for name, _, _ in pairs], ["access_token"])

        state, changed = compare_credentials(local, remote)
        self.assertEqual(state, STATE_CREDENTIAL_DIFF)
        self.assertEqual(changed, [name for name, _, _ in pairs])


class HourBucketTests(unittest.TestCase):
    def test_bucket_drops_minutes_and_seconds(self):
        self.assertEqual(hour_bucket(_dt("2026-03-31T04:37:12Z")), "2026-03-31T04:00Z")

    def test_bucket_is_empty_without_time(self):
        self.assertEqual(hour_bucket(None), "")


class BuildComparisonTests(unittest.TestCase):
    def _local(self, **overrides):
        row = {
            "id": 1,
            "email": "a@example.com",
            "status": "registered",
            "updated_at": _dt("2026-03-31T04:10:00Z"),
            "extra": {"access_token": "at-1", "refresh_token": "rt-1"},
        }
        row.update(overrides)
        return row

    def _remote(self, email="a@example.com", **overrides):
        payload = {
            "email": email,
            "updated_at": _dt("2026-03-31T04:50:00Z"),
            "credentials": {"access_token": "at-1", "refresh_token": "rt-1"},
        }
        payload.update(overrides)
        return RemoteAccount(**payload)

    def test_local_without_remote_is_not_uploaded(self):
        rows = build_comparison([self._local()], [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].state, STATE_LOCAL_ONLY)
        self.assertEqual(rows[0].label, "未上传")

    def test_remote_without_local_is_remote_only(self):
        remote = [RemoteAccount(email="ghost@example.com", remote_id="auth-g")]
        rows = build_comparison([], remote)
        self.assertEqual(rows[0].state, STATE_REMOTE_ONLY)
        self.assertEqual(rows[0].remote_id, "auth-g")

    def test_identical_credentials_are_synced(self):
        """用户口径：AT、RT 全相同就是同步。"""
        rows = build_comparison([self._local()], [self._remote()])
        self.assertEqual(rows[0].state, STATE_SYNCED)
        self.assertEqual(rows[0].label, "已同步")
        self.assertEqual(rows[0].credential_differences, [])

    def test_different_at_is_credential_diff(self):
        remote = [self._remote(credentials={"access_token": "at-2", "refresh_token": "rt-1"})]
        rows = build_comparison([self._local()], remote)
        self.assertEqual(rows[0].state, STATE_CREDENTIAL_DIFF)
        self.assertEqual(rows[0].label, "凭证不同")
        self.assertEqual(rows[0].credential_differences, ["access_token"])

    def test_different_rt_is_credential_diff(self):
        remote = [self._remote(credentials={"access_token": "at-1", "refresh_token": "rt-9"})]
        rows = build_comparison([self._local()], remote)
        self.assertEqual(rows[0].state, STATE_CREDENTIAL_DIFF)
        self.assertEqual(rows[0].credential_differences, ["refresh_token"])

    def test_time_no_longer_decides_sync(self):
        """时间差多少都不影响"是否同步" —— 只看凭证。

        回归：以前按小时比时间，同一小时报"一致"、差一小时报"本地较新"；
        现在即使时间差 5 小时，凭证相同就是 synced。
        """
        remote = [self._remote(updated_at=_dt("2026-03-31T09:50:00Z"))]
        rows = build_comparison([self._local()], remote)
        self.assertEqual(rows[0].state, STATE_SYNCED)
        # 时间关系仍作为辅助信息给出（远端 09:50 晚于本地 04:10）
        self.assertEqual(rows[0].time_relation, "remote_newer")

    def test_time_relation_is_reported_alongside_credential_diff(self):
        """凭证不同时，时间用来说明是哪边动的。"""
        remote = [
            self._remote(
                updated_at=_dt("2026-03-31T09:50:00Z"),
                credentials={"access_token": "at-2"},
            )
        ]
        rows = build_comparison([self._local()], remote)
        self.assertEqual(rows[0].state, STATE_CREDENTIAL_DIFF)
        self.assertEqual(rows[0].time_relation, "remote_newer")

    def test_remote_without_credentials_is_unknown_not_synced(self):
        """远端拿不到凭证时说"比不了"，不能当"已同步"。"""
        remote = [self._remote(credentials={})]
        rows = build_comparison([self._local()], remote)
        self.assertEqual(rows[0].state, STATE_UNKNOWN_CREDENTIAL)
        self.assertEqual(rows[0].credential_compared, 0)

    def test_credential_compared_counts_matched_fields(self):
        remote = [self._remote(credentials={"access_token": "at-1"})]
        rows = build_comparison([self._local()], remote)
        # 本地有 AT+RT，远端只有 AT → 只有 1 个字段能比
        self.assertEqual(rows[0].credential_compared, 1)

    def test_email_matching_is_case_insensitive(self):
        remote = [self._remote(email="A@Example.COM")]
        rows = build_comparison([self._local()], remote)
        self.assertEqual(rows[0].state, STATE_SYNCED)

    def test_unuploaded_rows_come_first(self):
        local = [
            self._local(id=1, email="synced@example.com"),
            self._local(id=2, email="pending@example.com"),
        ]
        remote = [self._remote(email="synced@example.com")]
        rows = build_comparison(local, remote)
        self.assertEqual(rows[0].email, "pending@example.com")
        self.assertEqual(rows[0].state, STATE_LOCAL_ONLY)

    def test_credential_diff_rows_rank_before_synced(self):
        """需要动作的排前面：凭证不同在已同步之前。"""
        local = [
            self._local(id=1, email="ok@example.com"),
            self._local(id=2, email="stale@example.com"),
        ]
        remote = [
            self._remote(email="ok@example.com"),
            self._remote(email="stale@example.com", credentials={"access_token": "at-OLD"}),
        ]
        rows = build_comparison(local, remote)
        self.assertEqual(rows[0].email, "stale@example.com")
        self.assertEqual(rows[0].state, STATE_CREDENTIAL_DIFF)

    def test_summary_counts_every_state(self):
        local = [
            self._local(id=1, email="a@example.com"),
            self._local(id=2, email="b@example.com"),
        ]
        remote = [
            self._remote(email="a@example.com"),
            self._remote(email="b@example.com", credentials={"access_token": "at-X"}),
            RemoteAccount(email="c@example.com"),
        ]
        summary = summarize(build_comparison(local, remote))
        self.assertEqual(summary[STATE_SYNCED], 1)
        self.assertEqual(summary[STATE_CREDENTIAL_DIFF], 1)
        self.assertEqual(summary[STATE_REMOTE_ONLY], 1)
        self.assertEqual(summary["total"], 3)

    def test_row_dict_is_json_safe(self):
        """前端直接吃这个 dict，时间必须是字符串。"""
        import json

        rows = build_comparison([self._local()], [])
        payload = json.dumps(rows[0].to_dict())
        self.assertIn("2026-03-31T04:00", payload)

    def test_remote_time_is_normalized_to_utc_in_the_payload(self):
        """远端时间要归一成 UTC 再给前端。

        回归：远端原始串是 `+08:00` 的，直接摆出来会跟本地的 UTC 时间看着差
        8 小时 —— 而"对比谁更新"正是那一列要回答的问题。实测踩过：同小时的
        两行显示成 22:20 与 06:00。
        """
        remote = [
            RemoteAccount(
                email="a@example.com",
                updated_at=_dt("2026-03-31T12:00:00+08:00"),
                updated_at_raw="2026-03-31T12:00:00+08:00",
            )
        ]
        row = build_comparison([self._local()], remote)[0].to_dict()
        # 与本地（04:10Z）同一小时档
        self.assertEqual(row["local_updated_hour"], "2026-03-31T04:00Z")
        self.assertEqual(row["remote_updated_hour"], "2026-03-31T04:00Z")
        # 归一后的时间戳落在 04:00Z 这一小时里，而不是 12:00Z
        self.assertIn("2026-03-31T04:00", row["remote_updated_at"])
        # 原始串仍然留着（tooltip 要显示远端本来的写法）
        self.assertEqual(row["remote_updated_at_raw"], "2026-03-31T12:00:00+08:00")

    def test_duplicate_local_emails_collapse(self):
        local = [self._local(id=1), self._local(id=2)]
        rows = build_comparison(local, [])
        self.assertEqual(len(rows), 1)


class DiffFieldsTests(unittest.TestCase):
    def test_plan_mismatch_is_reported(self):
        local = {"plan_type": "plus"}
        remote = RemoteAccount(email="a@b.c", extra={"plan_type": "free"})
        self.assertEqual(diff_fields(local, remote), ["plan_type"])

    def test_matching_values_are_not_reported(self):
        local = {"plan_type": "plus"}
        remote = RemoteAccount(email="a@b.c", extra={"plan_type": "plus"})
        self.assertEqual(diff_fields(local, remote), [])

    def test_missing_side_is_not_a_difference(self):
        """只有一边有值时不算"不一致" —— 那多半是字段没采到，不是内容不同。"""
        local = {"plan_type": "plus"}
        remote = RemoteAccount(email="a@b.c", extra={})
        self.assertEqual(diff_fields(local, remote), [])


class ComparisonCacheTests(unittest.TestCase):
    """缓存：默认命中、refresh 绕过、"同步时间"要能看。"""

    def setUp(self):
        clear_cache()

    def tearDown(self):
        clear_cache()

    def _fake_rows(self):
        return [{"id": 1, "email": "a@example.com", "status": "registered",
                 "updated_at": datetime.now(timezone.utc), "extra": {}}]

    def test_second_call_hits_the_cache(self):
        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=self._fake_rows(),
        ) as local_mock:
            with mock.patch(
                "services.panel_comparison_cache._panel_credentials",
                return_value={"api_url": "http://cpa.local", "api_key": "k"},
            ):
                with mock.patch(
                    "services.panel_comparison_cache.FETCHERS",
                    {"cpa": mock.Mock(return_value=[])},
                ) as fetchers:
                    first = get_panel_comparison("cpa")
                    second = get_panel_comparison("cpa")

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        # 本地账号只查了一次（第二次整段走了缓存）
        self.assertEqual(local_mock.call_count, 1)
        self.assertEqual(fetchers["cpa"].call_count, 1)

    def test_refresh_bypasses_the_cache(self):
        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=self._fake_rows(),
        ):
            with mock.patch(
                "services.panel_comparison_cache._panel_credentials",
                return_value={"api_url": "http://cpa.local", "api_key": "k"},
            ):
                with mock.patch(
                    "services.panel_comparison_cache.FETCHERS",
                    {"cpa": mock.Mock(return_value=[])},
                ) as fetchers:
                    get_panel_comparison("cpa")
                    refreshed = get_panel_comparison("cpa", refresh=True)

        self.assertFalse(refreshed["cached"])
        self.assertEqual(fetchers["cpa"].call_count, 2)

    def test_fetched_at_is_present_for_the_sync_time_display(self):
        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=self._fake_rows(),
        ):
            with mock.patch(
                "services.panel_comparison_cache._panel_credentials",
                return_value={"api_url": "", "api_key": ""},
            ):
                payload = get_panel_comparison("cpa")

        self.assertTrue(payload["fetched_at"])
        # 面板地址没配 → 远端拉不到，但本地清单照常返回
        self.assertEqual(payload["local_count"], 1)
        self.assertIn("未配置", payload["remote_error"])

    def test_payload_carries_the_state_labels(self):
        """状态标签由后端下发，前端不再抄一份（抄两份会漏改、UI 显示旧文案）。"""
        from services.panel_comparison import STATE_LABELS

        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=self._fake_rows(),
        ):
            with mock.patch(
                "services.panel_comparison_cache._panel_credentials",
                return_value={"api_url": "", "api_key": ""},
            ):
                payload = get_panel_comparison("cpa")

        self.assertEqual(payload["labels"], dict(STATE_LABELS))
        # 每个 summary 里出现过的状态都要有标签，否则前端只能显示英文 key
        for state in payload["summary"]:
            if state == "total":
                continue
            self.assertIn(state, payload["labels"], f"状态 {state} 没有中文标签")

    def test_remote_failure_keeps_local_rows_and_reports_the_reason(self):
        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=self._fake_rows(),
        ):
            with mock.patch(
                "services.panel_comparison_cache._panel_credentials",
                return_value={"api_url": "http://cpa.local", "api_key": "k"},
            ):
                with mock.patch(
                    "services.panel_comparison_cache.FETCHERS",
                    {"cpa": mock.Mock(side_effect=RuntimeError("CLIProxyAPI 无法连接"))},
                ):
                    payload = get_panel_comparison("cpa")

        self.assertEqual(payload["local_count"], 1)
        self.assertEqual(payload["remote_count"], 0)
        self.assertIn("无法连接", payload["remote_error"])
        self.assertEqual(payload["rows"][0]["state"], STATE_LOCAL_ONLY)

    def test_unknown_panel_raises(self):
        with self.assertRaises(ValueError):
            get_panel_comparison("nope")

    def test_legacy_panel_key_is_normalized(self):
        """`cliproxyapi` 是 `cpa` 的旧 key —— 文档承诺不 404，这里就得认。"""
        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=self._fake_rows(),
        ), mock.patch(
            "services.panel_comparison_cache._panel_credentials",
            return_value={"api_url": "", "api_key": ""},
        ):
            payload = get_panel_comparison("cliproxyapi")
        self.assertEqual(payload["panel"], "cpa")

    def test_legacy_key_shares_the_cache_with_the_canonical_one(self):
        """旧 key 归一后应与规范 key 共用同一份缓存，而不是各存一份。"""
        fetch = mock.Mock(return_value=[])
        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=self._fake_rows(),
        ), mock.patch(
            "services.panel_comparison_cache._panel_credentials",
            return_value={"api_url": "http://cpa.test", "api_key": "k"},
        ), mock.patch(
            "services.panel_comparison_cache.FETCHERS",
            {"cpa": fetch},
        ):
            first = get_panel_comparison("cpa")
            second = get_panel_comparison("cliproxyapi")

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"], "旧 key 没吃到规范 key 的缓存")
        self.assertEqual(fetch.call_count, 1)

    def test_cache_expires(self):
        """TTL 过了之后要重新拉，而不是继续吃旧数据。"""
        from services import panel_comparison_cache as cache_mod

        # 用「远端可用」的成功路径（未配置地址会走失败 TTL，那是另一条分支）
        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=self._fake_rows(),
        ), mock.patch(
            "services.panel_comparison_cache._panel_credentials",
            return_value={"api_url": "http://cpa.test", "api_key": "k"},
        ), mock.patch(
            "services.panel_comparison_cache.FETCHERS",
            {"cpa": mock.Mock(return_value=[])},
        ):
            with mock.patch.object(cache_mod, "CACHE_TTL_SECONDS", 0):
                get_panel_comparison("cpa")
                again = get_panel_comparison("cpa")
        self.assertFalse(again["cached"])

    def test_failed_fetch_uses_a_short_ttl(self):
        """远端没读到时的缓存要短命 —— 否则一次抖动被放大成整整一分钟的"读取失败"。

        回归：失败结果按 60 秒缓存，而「刷新」按钮走缓存路径清不掉它，
        用户只能点更重的「同步到最新」。
        """
        from services import panel_comparison_cache as cache_mod

        fetch = mock.Mock(side_effect=RuntimeError("CLIProxyAPI 无法连接"))
        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=self._fake_rows(),
        ), mock.patch(
            "services.panel_comparison_cache._panel_credentials",
            return_value={"api_url": "http://cpa.test", "api_key": "k"},
        ), mock.patch(
            "services.panel_comparison_cache.FETCHERS",
            {"cpa": fetch},
        ):
            first = get_panel_comparison("cpa")
            self.assertIn("无法连接", first["remote_error"])

            # 成功 TTL 还没到，但失败 TTL 已经过了 → 应当重试远端
            with mock.patch.object(cache_mod, "CACHE_TTL_SECONDS", 60), \
                 mock.patch.object(cache_mod, "FAILURE_CACHE_TTL_SECONDS", 0):
                second = get_panel_comparison("cpa")

        self.assertEqual(fetch.call_count, 2, "失败缓存没有按短 TTL 过期，没重试远端")
        self.assertFalse(second["cached"])

    def test_successful_fetch_still_uses_the_full_ttl(self):
        """成功结果照旧吃满 60 秒，别把短 TTL 误用到成功路径上。"""
        from services import panel_comparison_cache as cache_mod

        fetch = mock.Mock(return_value=[])
        with mock.patch(
            "services.panel_comparison_cache._local_accounts_for_panel",
            return_value=self._fake_rows(),
        ), mock.patch(
            "services.panel_comparison_cache._panel_credentials",
            return_value={"api_url": "http://cpa.test", "api_key": "k"},
        ), mock.patch(
            "services.panel_comparison_cache.FETCHERS",
            {"cpa": fetch},
        ):
            get_panel_comparison("cpa")
            with mock.patch.object(cache_mod, "FAILURE_CACHE_TTL_SECONDS", 0):
                second = get_panel_comparison("cpa")

        self.assertEqual(fetch.call_count, 1, "成功结果被短 TTL 提前作废了")
        self.assertTrue(second["cached"])


class LegacyTokenColumnTests(unittest.TestCase):
    """`token` 列上的凭证要参与对比。

    账号表的 `token` 列是历史遗留的凭证位（前端编辑弹窗的「Token / Access Token」
    字段、`POST /api/accounts` 都写它），较新的落库路径则写 `extra.access_token`。
    对比只看 extra 时，凭证在列上的账号会被当成「本地没有 AT」—— 实测能判出
    假 `synced`（本地有 RT、远端 AT 不同时）。
    """

    def _rows_from_db(self, platform, token, extra):
        """造一条真实 AccountModel，走仓储读回（不复刻 SQL，直接建模型）。"""
        from core.db.models_account import AccountModel

        model = AccountModel(
            platform=platform, email=f"legacy-{platform}@example.com",
            password="p", token=token,
        )
        model.set_extra(extra)
        return model

    def test_chatgpt_token_column_feeds_access_token(self):
        model = self._rows_from_db("chatgpt", "AT-FROM-COLUMN", {})
        with mock.patch(
            "core.db.account_repository.list_accounts", return_value=[model]
        ):
            from services.panel_comparison_cache import _local_accounts_for_panel

            rows = _local_accounts_for_panel("cpa")

        self.assertEqual(rows[0]["extra"].get("access_token"), "AT-FROM-COLUMN")

    def test_grok_token_column_feeds_sso(self):
        """Grok 侧的凭证键是 `sso`，不是 access_token。"""
        model = self._rows_from_db("grok", "SSO-FROM-COLUMN", {})
        with mock.patch(
            "core.db.account_repository.list_accounts", return_value=[model]
        ):
            from services.panel_comparison_cache import _local_accounts_for_panel

            rows = _local_accounts_for_panel("grok2api")

        self.assertEqual(rows[0]["extra"].get("sso"), "SSO-FROM-COLUMN")

    def test_extra_wins_over_the_column(self):
        """两处都有值时以 extra 为准 —— 列上的可能是刷新前的旧值。"""
        model = self._rows_from_db(
            "chatgpt", "AT-STALE-COLUMN", {"access_token": "AT-FRESH-EXTRA"}
        )
        with mock.patch(
            "core.db.account_repository.list_accounts", return_value=[model]
        ):
            from services.panel_comparison_cache import _local_accounts_for_panel

            rows = _local_accounts_for_panel("cpa")

        self.assertEqual(rows[0]["extra"].get("access_token"), "AT-FRESH-EXTRA")

    def test_the_blind_spot_would_have_been_reported_as_synced(self):
        """反向说明这个修复防的是什么：不补列时会被判成 synced。"""
        from services.panel_comparison import compare_credentials

        # 本地 extra 只有 RT（AT 在列上没被读出来），远端 AT 与本地列上的不同
        state, _ = compare_credentials(
            {"refresh_token": "RT-SAME"},
            {"access_token": "AT-DIFFERENT", "refresh_token": "RT-SAME"},
        )
        # 现状（只读 extra）确实是 synced —— 所以必须靠上面的合并来避免
        self.assertEqual(state, "synced")

        # 合并之后同样的数据判出差异
        state2, changed = compare_credentials(
            {"access_token": "AT-FROM-COLUMN", "refresh_token": "RT-SAME"},
            {"access_token": "AT-DIFFERENT", "refresh_token": "RT-SAME"},
        )
        self.assertEqual(state2, "credential_diff")
        self.assertIn("access_token", changed)


if __name__ == "__main__":
    unittest.main()
