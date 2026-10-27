"""账号字段合并的业务规则要抽成纯函数 —— 两种存储后端共用。

## 为什么

`AccountRepository.upsert` 现在把**业务规则**与 **SQL 机制**混在一起：

- 业务规则：状态归一（删掉的 trial/subscribed 要收敛）、`cashier_url` 的
  两个来源谁优先、`extra_json` 原样保留（不解析再序列化）、`created_at`
  只在是 datetime 时保留；
- SQL 机制：查询、赋值、commit、refresh。

接 JSON 后端时如果照抄一遍，这些规则会立刻漂移 —— 而它们每一条都是踩过坑
才写出来的（见下）。抽成纯函数后，两种后端调同一个函数，规则只有一份。

## 三条规则的由来（都写在原代码注释里）

1. **状态归一**：旧客户端/旧导入文件会提交已删值（trial / subscribed），
   写侧必须收敛，否则历史行被写回废弃状态。
2. **cashier_url 优先级**：列上有值以列为准。旧实现无条件用
   `extra.get("cashier_url")` 重算，而 JSON 导入把该字段写在列上又排除在
   extra 之外 —— 刚导入的值被就地抹掉（带值导入丢失、不带值导入清空旧值）。
3. **沉默不等于清空**：两个来源都没提这个字段时保持原值。
"""

from __future__ import annotations

import json
import unittest


def _account(**overrides):
    from core.base_platform import Account, AccountStatus

    data = dict(
        platform="chatgpt",
        email="a@b.c",
        password="pw",
        token="AT",
        status=AccountStatus.REGISTERED,
        extra={"k": "v"},
    )
    data.update(overrides)
    return Account(**data)


class StatusIsNormalizedTests(unittest.TestCase):
    def test_removed_status_is_collapsed(self):
        from core.db.repository import merge_account_fields

        fields = merge_account_fields(incoming=_account(status="trial"), existing=None)
        self.assertNotEqual(fields["status"], "trial", "已删除的状态没被归一")
        self.assertEqual(fields["status"], "registered")

    def test_valid_status_passes_through(self):
        from core.db.repository import merge_account_fields

        fields = merge_account_fields(incoming=_account(status="banned"), existing=None)
        self.assertEqual(fields["status"], "banned")


class CashierUrlPrecedenceTests(unittest.TestCase):
    """`cashier_url` 有两个来源：账号表的**列**与 `extra`。

    插件产出的 `core.base_platform.Account` **没有**这个列字段（只放 extra），
    列是 API 层导入/创建时写的。所以测列优先级要用 `AccountModel`。
    """

    @staticmethod
    def _model(**overrides):
        from core.db.models_account import AccountModel

        row = AccountModel(
            platform="chatgpt", email="a@b.c", password="pw", status="registered"
        )
        for key, value in overrides.items():
            setattr(row, key, value)
        return row

    def test_column_wins_over_extra(self):
        """列上有值以列为准（导入把值写在列上）。"""
        from core.db.repository import merge_account_fields

        row = self._model(cashier_url="http://column")
        row.extra_json = json.dumps({"cashier_url": "http://extra"})
        fields = merge_account_fields(incoming=row, existing=None)
        self.assertEqual(fields["cashier_url"], "http://column")

    def test_extra_used_when_column_empty(self):
        from core.db.repository import merge_account_fields

        row = self._model(cashier_url="")
        row.extra_json = json.dumps({"cashier_url": "http://extra"})
        fields = merge_account_fields(incoming=row, existing=None)
        self.assertEqual(fields["cashier_url"], "http://extra")

    def test_silence_keeps_existing_value(self):
        """两个来源都没提 → 保持原值。沉默不等于清空。"""
        from core.db.repository import merge_account_fields

        row = self._model(cashier_url="")
        row.extra_json = json.dumps({"k": "v"})   # 没提 cashier_url
        fields = merge_account_fields(
            incoming=row, existing={"cashier_url": "http://old"}
        )
        self.assertEqual(fields["cashier_url"], "http://old",
                         "没提这个字段却把原值清空了")

    def test_explicit_empty_clears(self):
        """显式在 extra 里给空串 → 清空（那是有意的）。"""
        from core.db.repository import merge_account_fields

        row = self._model(cashier_url="")
        row.extra_json = json.dumps({"cashier_url": ""})
        fields = merge_account_fields(
            incoming=row, existing={"cashier_url": "http://old"}
        )
        self.assertEqual(fields["cashier_url"], "")


class ExtraJsonIsPreservedTests(unittest.TestCase):
    def test_account_model_input_keeps_raw_extra_json(self):
        """传 `AccountModel` 时保留其 `extra_json` 原文。

        解析再序列化会丢掉仓储不认识的键（以及键序、空白）—— 那是静默的
        数据损失。
        """
        from core.db.models_account import AccountModel
        from core.db.repository import merge_account_fields

        # 用**键序非字典序 + 非常规空白**的原文：解析再序列化会把
        # `sort_keys`/缩进/空格全改掉，从而与原文不等（字符串相等断言能抓到）。
        # 第一版用了字典序的紧凑 JSON，解析再序列化后字节相同 —— 变异没抓住。
        raw = '{\n  "zebra": 1,\n  "apple": [1, 2],\n  "mid": {"b": 2, "a": 1}\n}'
        row = AccountModel(
            platform="chatgpt", email="a@b.c", password="pw", status="registered"
        )
        row.extra_json = raw
        fields = merge_account_fields(incoming=row, existing=None)
        self.assertEqual(
            fields["extra_json"], raw,
            "extra_json 被改写 —— 解析再序列化会丢键序/空白，且可能丢未知键",
        )

    def test_plugin_account_serializes_its_extra_dict(self):
        from core.db.repository import merge_account_fields

        fields = merge_account_fields(incoming=_account(extra={"a": 1}), existing=None)
        self.assertEqual(json.loads(fields["extra_json"]), {"a": 1})


class CreatedAtIsPreservedOnlyWhenDatetimeTests(unittest.TestCase):
    def test_datetime_is_kept(self):
        from datetime import datetime, timezone

        from core.db.repository import merge_account_fields

        moment = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        fields = merge_account_fields(incoming=_account(created_at=moment), existing=None)
        self.assertEqual(fields.get("created_at"), moment)

    def test_int_is_ignored(self):
        """插件产出的 `created_at` 是 unix 秒 —— 塞进 datetime 列会炸。

        实测：`'int' object has no attribute 'utcoffset'`。
        """
        from core.db.repository import merge_account_fields

        fields = merge_account_fields(incoming=_account(created_at=1790000000), existing=None)
        self.assertNotIn("created_at", fields)


class ExistingFieldsArePreservedTests(unittest.TestCase):
    def test_empty_incoming_does_not_wipe_existing(self):
        """空串不覆盖已有值（密码等字段的既有语义）。"""
        from core.db.repository import merge_account_fields

        acct = _account(password="", token="")
        fields = merge_account_fields(
            incoming=acct, existing={"password": "old-pw", "token": "old-at"}
        )
        self.assertEqual(fields["password"], "old-pw")
        self.assertEqual(fields["token"], "old-at")


if __name__ == "__main__":
    unittest.main()
