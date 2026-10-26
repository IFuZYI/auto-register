"""上传器必须认 `extra` 里的凭据 —— `Account` 对象上根本没有这些属性。

实测的线上 bug（2026-10-26）：面板页点「上传到 CPA / Sub2API」传出去的是
**空凭据**：

    CPA:     {'access_token': '', 'refresh_token': '', 'id_token': ''}
    sub2api: {'access_token': '', 'refresh_token': '', 'id_token': ''}

根因：`core.base_platform.Account` 把凭据放在 **`extra` 字典**里，而
`generate_token_json` / `_build_sub2api_account_payload` 读的是**对象属性**
（`getattr(account, "access_token", "")`）。`api/integrations.py` 构造账号时
只给了 `token=AT`、`password=""`，于是三个属性全取不到，凭据静默变空。

对比：`chatgpt2api_upload.py` 早就走 `get_credential(extra, ...)`，所以它不受
影响 —— 这也解释了为什么「只有 chatgpt2api 传得上去」。

修法：上传器与 `chatgpt2api_upload` 同口径 —— **`extra` 优先，属性兜底**。
"""

from __future__ import annotations

import unittest

from core.base_platform import Account, AccountStatus


def _account_with_credentials_in_extra() -> Account:
    """按本仓真实落库形态造账号：凭据在 `extra` 里，`token` 列镜像 AT。"""
    return Account(
        platform="chatgpt",
        email="probe@example.com",
        password="Pw-From-Column",
        token="AT-from-column",
        status=AccountStatus.REGISTERED,
        extra={
            "access_token": "AT-in-extra",
            "refresh_token": "RT-in-extra",
            "id_token": "IDT-in-extra",
            "session_token": "ST-in-extra",
        },
    )


class CpaUploadReadsExtraTests(unittest.TestCase):
    """`generate_token_json` 是 CPA 上传的凭据来源。"""

    def test_credentials_come_from_extra(self):
        from platforms.chatgpt.cpa_upload import generate_token_json

        data = generate_token_json(_account_with_credentials_in_extra())

        self.assertEqual(data.get("access_token"), "AT-in-extra",
                         "CPA 上传的 AT 为空 —— 对端会收到一份没凭据的记录")
        self.assertEqual(data.get("refresh_token"), "RT-in-extra",
                         "CPA 上传的 RT 为空")
        self.assertEqual(data.get("id_token"), "IDT-in-extra",
                         "CPA 上传的 IDT 为空")

    def test_token_column_is_the_fallback_not_the_source(self):
        """`extra` 有值时不能被 `token` 列盖掉（列可能是旧的）。"""
        from platforms.chatgpt.cpa_upload import generate_token_json

        data = generate_token_json(_account_with_credentials_in_extra())
        self.assertNotEqual(data.get("access_token"), "AT-from-column",
                            "读了 token 列而不是 extra —— extra 是更新的来源")

    def test_empty_extra_falls_back_to_attributes(self):
        """没有 extra 时仍认属性（协议层其它调用方依赖这个）。"""
        from platforms.chatgpt.cpa_upload import generate_token_json

        acct = Account(platform="chatgpt", email="a@b.c", password="pw",
                       token="AT-col", status=AccountStatus.REGISTERED, extra={})
        acct.access_token = "AT-attr"
        acct.refresh_token = "RT-attr"
        self.assertEqual(generate_token_json(acct).get("access_token"), "AT-attr")


class Sub2ApiUploadReadsExtraTests(unittest.TestCase):
    """`_build_sub2api_account_payload` 是 sub2api 上传的凭据来源。"""

    def _payload(self):
        from platforms.chatgpt.sub2api_upload import _build_sub2api_account_payload

        return _build_sub2api_account_payload(_account_with_credentials_in_extra())

    def test_credentials_come_from_extra(self):
        creds = self._payload().get("credentials", {})
        self.assertEqual(creds.get("access_token"), "AT-in-extra",
                         "sub2api 上传的 AT 为空")
        self.assertEqual(creds.get("refresh_token"), "RT-in-extra",
                         "sub2api 上传的 RT 为空")
        self.assertEqual(creds.get("id_token"), "IDT-in-extra",
                         "sub2api 上传的 IDT 为空")


class PanelPushUsesExtraBackedUploadersTests(unittest.TestCase):
    """端到端：面板推送那两个分支产出的载荷必须带凭据。

    这条覆盖 `api/integrations.py` 的真实构造方式（`password=""`、只给
    `token=AT`、凭据在 extra）—— 那正是线上传空凭据的现场。
    """

    def _row_and_model(self):
        from core.db import AccountModel

        model = AccountModel(
            id=999_001, platform="chatgpt", email="probe@example.com",
            password="Pw-From-Column", token="AT-from-column",
            status="registered",
        )
        model.extra_json = (
            '{"access_token": "AT-in-extra", "refresh_token": "RT-in-extra",'
            ' "id_token": "IDT-in-extra"}'
        )
        return model

    def test_chatgpt_style_account_carries_credentials_to_cpa(self):
        """按 `api/integrations.py` 的构造方式（不设属性）走 CPA 上传。"""
        from platforms.chatgpt.cpa_upload import generate_token_json

        model = self._row_and_model()
        acct = Account(
            platform="chatgpt", email=model.email, password="",
            token=model.token, status=AccountStatus.REGISTERED,
            extra=model.get_extra(),
        )
        data = generate_token_json(acct)
        self.assertEqual(data.get("access_token"), "AT-in-extra",
                         "面板推送的 CPA 载荷仍是空 AT —— 这是线上那个 bug")
        self.assertEqual(data.get("refresh_token"), "RT-in-extra",
                         "面板推送的 CPA 载荷仍是空 RT")


if __name__ == "__main__":
    unittest.main()


class ExportKeyIsVariantSpecificTests(unittest.TestCase):
    """导出请求的键名**按变体不同**，不能一概而论。

    实测两个变体在这个端点上互不兼容（读源码确认，2026-10-26）：

    - yukkcat（`reference/panel/chatgpt2api/api/accounts.py:110-114`）：两个键
      都收，但 `access_tokens` 被当 **legacy token**，对不上就 400。management_id
      只能放 `account_ids`。
    - 部署版 IFuZYI 2.3.0（`internal/api/account_refresh.go:160`）：只读
      `access_tokens`，忽略 `account_ids`。

    **但部署版走不到导出** —— 它的列表直接带 `access_token`，凭证就地取到，
    `credential_targets` 不填。所以这一行应当保持 `account_ids`（服务 yukkcat），
    不要为了「兼容部署版」改成 `access_tokens`：那样只会把 yukkcat 打成 400，
    而部署版本就不到这里（曾经改过，已撤回）。
    """

    def test_export_request_keeps_the_yukkcat_account_ids_key(self):
        from unittest import mock

        import services.panel_comparison as pc

        captured = {}

        class _Resp:
            status_code = 200
            content = b'{"items": []}'
            text = '{"items": []}'

            def json(self):
                return {"items": []}

        def _fake_post(url, headers=None, json=None, timeout=None, verify=None):
            captured["json"] = json
            return _Resp()

        with mock.patch("requests.post", _fake_post):
            pc._chatgpt2api_export_credentials(
                base="https://panel.example", key="k", account_ids=["mgmt-1"],
            )

        self.assertEqual(captured.get("json", {}).get("account_ids"), ["mgmt-1"],
                         "yukkcat 只认 account_ids —— 改成 access_tokens 会 400")
        self.assertNotIn("access_tokens", captured.get("json", {}),
                         "别把 management_id 塞进 access_tokens：yukkcat 会当 legacy "
                         "token 查不到而 400")
