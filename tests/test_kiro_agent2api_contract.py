"""Kiro 导出必须满足 Agent2API 的账号导入契约。

Agent2API 的 `account_transfer::import_one_item` 逐条要求非空 `id`；身份回退
`uid` 先于 `email`；Kiro 插件按小写 `"builderid"` 比较 `loginProvider`。
只对齐"字段形状"而漏掉这三点的文件，导入时会 23 条全失败。
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from scripts.check_kiro_agent2api_export import check


class KiroAgent2ApiContractTests(unittest.TestCase):
    def test_export_satisfies_the_importer_contract(self):
        self.assertEqual(check(), [])

    def test_checker_flags_a_broken_document(self):
        """反向：契约检查器自己也要能红（不能永远返回 []）。"""
        bad = json.dumps(
            {
                "accounts": [
                    {
                        "id": "",
                        "email": "",
                        "method": "idc",
                        "loginProvider": "BuilderId",
                        "accessToken": "",
                        "refreshToken": "",
                        "clientId": "kiro-cid",
                        "clientSecret": "",
                        "expires": 1794478346773,
                        "expiresAt": "2026-11-12T19:32:26.773Z",
                        "profileArn": "arn:x",
                    }
                ]
            }
        )
        with mock.patch(
            "platforms.kiro.credentials.render_auth_tokens", return_value=bad
        ):
            problems = check()
        self.assertTrue(problems)
        text = "\n".join(problems)
        for expected in ("缺少 id", "缺少 email", "clientId 必须是 AWS", "loginProvider"):
            self.assertIn(expected, text)

    def test_checker_flags_a_missing_method(self):
        """method 丢了不能让 idc/social 两条分支都跳过、误判成通过。"""
        doc = json.dumps(
            {
                "accounts": [
                    {
                        "id": "kiro-a",
                        "email": "a@x.ai",
                        "loginProvider": "builderid",
                        "accessToken": "at",
                        "refreshToken": "rt",
                        "clientId": "cid",
                        "clientSecret": "cs",
                        "expires": 1,
                        "expiresAt": "1970-01-01T00:00:00.001Z",
                        "profileArn": "",
                    },
                    {
                        "id": "kiro-b",
                        "email": "b@x.ai",
                        "loginProvider": "github",
                        "accessToken": "at",
                        "refreshToken": "rt",
                        "clientId": "",
                        "clientSecret": "",
                        "expires": 1794478346773,
                        "expiresAt": "2026-11-12T19:32:26.773Z",
                        "profileArn": "arn",
                    },
                ]
            }
        )
        with mock.patch(
            "platforms.kiro.credentials.render_auth_tokens", return_value=doc
        ):
            problems = check()
        self.assertTrue(any("method 必须是 idc/social" in p for p in problems))
