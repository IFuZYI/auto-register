"""导出 → 导入要能往返。**当前是断的**。

## 实测的缺陷

导出用 `----` 分隔符（`services/account_export.py` 的 `SEPARATOR`，13 种格式
全用它），而文本导入按**空白**切分：

    导出：a@b.com----pw----2FAKEY
    导入：line.split() → 1 段 → len < 2 → **skipped**

也就是说「导出 → 编辑 → 导入」这条工作流对所有文本格式都不成立 —— 用户导出
备份后导入回来，得到的是「跳过 N 行」而账号一个都没进去。

## 修法

文本导入要认 `----`：

1. 行里有 `----` → 按它切分，列 → 字段的映射**取自导出注册表**（`columns`），
   保证两边用同一份定义；
2. 没给来源格式且列数对应**多个**格式时 **明确报错**，不猜 —— 猜错会把 AT
   写进 `refresh_token`（静默的错误数据）；
3. 行里没有 `----` → 走原有空白路径（手输/老脚本兼容）。
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient


def _client():
    import main

    return TestClient(main.app)


class DelimitedTextImportTests(unittest.TestCase):
    """`----` 分隔的文本行要能导进来。"""

    def setUp(self):
        self.client = _client()

    def _import(self, lines, **extra):
        body = {"platform": "chatgpt", "lines": lines, "format": "text", **extra}
        return self.client.post("/api/accounts/import", json=body)

    def _cleanup(self, *emails):
        from core.db import platform_session
        from core.db.models_account import AccountModel
        from sqlmodel import select

        with platform_session("chatgpt") as s:
            for email in emails:
                row = s.exec(
                    select(AccountModel).where(AccountModel.email == email)
                ).first()
                if row is not None:
                    s.delete(row)
            s.commit()

    def test_email_pw_line_is_imported(self):
        """`email----password`（2 列，唯一格式）应自动识别。"""
        email = "rt-emailpw@x.c"
        try:
            r = self._import([f"{email}----pw-1"])
            self.assertEqual(r.status_code, 200, r.text[:200])
            body = r.json()
            self.assertEqual(body["created"], 1, f"没导入成功: {body}")
            self.assertEqual(body["skipped"], 0, f"被跳过了: {body}")
        finally:
            self._cleanup(email)

    def test_column_mapping_comes_from_the_export_registry(self):
        """3 列带 `source_format` 时应按该格式的列映射落库。"""
        email = "rt-3col@x.c"
        try:
            r = self._import(
                [f"{email}----pw----TOTPKEY123"], source_format="email_pw_2fa"
            )
            self.assertEqual(r.status_code, 200, r.text[:200])
            self.assertEqual(r.json()["created"], 1, r.text[:200])

            from core.db import platform_session
            from core.db.models_account import AccountModel
            from sqlmodel import select

            with platform_session("chatgpt") as s:
                row = s.exec(
                    select(AccountModel).where(AccountModel.email == email)
                ).first()
                self.assertEqual(row.get_extra().get("totp_secret"), "TOTPKEY123",
                                 "3 列的第三列没落进 totp_secret")
                self.assertEqual(row.password, "pw")
        finally:
            self._cleanup(email)

    def test_column_count_mismatch_is_rejected(self):
        """行与「来源格式」列数不符时明确报错（不猜、不静默截断）。

        API 的默认来源格式是 `email_pw`（2 列）。来一行 3 列的说明用户导出的
        是别的格式，此时必须报错 —— 猜一个 3 列格式可能把 AT 写进
        `refresh_token`，那是静默的错误数据。
        """
        email = "rt-mismatch@x.c"
        try:
            r = self._import([f"{email}----pw----SOMETHING"])
            self.assertEqual(
                r.status_code, 400,
                f"列数不符被静默接受了: {r.status_code} {r.text[:200]}",
            )
            detail = str(r.json().get("detail") or "")
            self.assertIn("列", detail, f"报错没说清列数问题: {detail}")
        finally:
            self._cleanup(email)

    def test_parser_refuses_to_guess_when_ambiguous(self):
        """解析器本身**拒绝猜**（歧义列数直接抛）。

        这条与上一条是两层：API 层给一个**显式默认**（产品决策），解析器
        保持严格（工程安全属性）—— 谁调用它都不会得到「猜出来的」映射。
        """
        from services.account_export import parse_delimited_line

        with self.assertRaises(ValueError) as ctx:
            parse_delimited_line("a@b.c----pw----X")
        self.assertIn("多个格式", str(ctx.exception))

    def test_legacy_whitespace_still_works(self):
        """手输的空白分隔行不能被破坏（老脚本兼容）。"""
        email = "rt-legacy@x.c"
        try:
            r = self._import([f"{email} pw-2"])
            self.assertEqual(r.status_code, 200, r.text[:200])
            self.assertEqual(r.json()["created"], 1, r.text[:200])
        finally:
            self._cleanup(email)

    def test_short_line_is_still_skipped(self):
        """不成形的行（少于两列）仍计入 skipped。"""
        r = self._import(["not-a-valid-line"])
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(r.json()["skipped"], 1, r.text[:200])


class ExportImportSymmetryTests(unittest.TestCase):
    """结构性：列 → 字段的映射只有**一份**定义（导出注册表）。"""

    def test_importer_uses_the_export_registry(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "api" / "accounts.py").read_text(
            encoding="utf-8"
        )
        # 导入侧**必须**经由 `parse_delimited_line` 拿列映射 —— 它内部用
        # `EXPORT_FORMATS`。在导入侧手写一份「第几列是什么」就会逐渐漂移
        # （导出加一个格式、导入忘了跟）。
        self.assertIn(
            "parse_delimited_line", src,
            "导入没走共享解析器 —— 列映射会出现第二份定义并逐渐漂移",
        )


if __name__ == "__main__":
    unittest.main()
