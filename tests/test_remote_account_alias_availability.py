"""远程主号定位要挑**有可用别名**的那个，而不是第一个启用的。

用户 2026-10-27 报：注册直接失败「远程账号不支持生成隐私邮箱」。

实测：库里 789 个可用别名全在 account_id=4/5 名下，而
`resolve_remote_account("")` 取 `order_by(id).first()` = account_id=3
（`fuzy202610`），它名下**一个可用别名都没有**。于是取号流程回落到
「生成新别名」，远程又拒绝生成（本仓明确只允许本地账号生成）→ 注册必失败。

选主号时该看「有没有能领的别名」，而不是只看「启用」。

测试手法：不连远程服务、不依赖 conftest 的临时库 —— 用**内存 SQLite**
建两张表喂给挑选函数，这样测的是纯挑选逻辑。
"""

from __future__ import annotations

import unittest

from sqlmodel import Session, SQLModel, create_engine, select

from core.db.models_platform import ICloudAccountModel, ICloudAliasModel


def _session_with(accounts, aliases):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for a in accounts:
            # `(base_url, remote_account_id)` 有唯一约束 —— 空值会互相撞。
            s.add(ICloudAccountModel(remote_account_id=f"acc_{a['id']}", **a))
        for a in aliases:
            # `share_token` 的默认是个 lambda，同一进程内多次构造会撞唯一约束
            # （内存库里没有跨测试隔离），所以显式给值。
            s.add(ICloudAliasModel(share_token=f"tok-{a['id']}", **a))
        s.commit()
    return engine


class PicksAccountWithAvailableAliasesTests(unittest.TestCase):
    """挑选规则：优先有可用别名的主号。"""

    def _pick(self, accounts, aliases, platform: str = "", registered=None):
        """走**生产入口** `resolve_remote_account("")`，不是内部函数。

        早先直接调 `_first_remote_account_with_aliases` —— 那样把实现换回
        「只取第一个启用的」测试仍然全绿（变异验证抓到），等于没测到接入点。
        这里把 `platform_session` 换成内存库，让生产代码跑真实路径。
        """
        from contextlib import ExitStack, contextmanager
        from unittest import mock

        import services.icloud.remote as remote_mod

        engine = _session_with(accounts, aliases)

        @contextmanager
        def _fake_session(_platform=None):
            with Session(engine) as s:
                yield s

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(remote_mod, "platform_session", _fake_session)
            )
            if registered is not None:
                stack.enter_context(
                    mock.patch.object(
                        remote_mod, "_registered_platforms_for", return_value=registered
                    )
                )
            return remote_mod.resolve_remote_account("", platform)

    def test_skips_account_without_available_aliases(self):
        accounts = [
            {"id": 3, "email": "a@x.com", "source": "remote", "enabled": True},
            {"id": 4, "email": "b@x.com", "source": "remote", "enabled": True},
        ]
        aliases = [
            # account 3 的别名全用掉了
            {"id": 1, "account_id": 3, "address": "u1@x.com",
             "status": "active", "pool_status": "used", "used_platforms": ",chatgpt,"},
            # account 4 有一个能用的
            {"id": 2, "account_id": 4, "address": "u2@x.com",
             "status": "active", "pool_status": "available", "used_platforms": ""},
        ]
        row = self._pick(accounts, aliases)
        self.assertEqual(row.id, 4, "选到了没有可用别名的主号 —— 注册会直接失败")

    def test_falls_back_to_first_when_none_has_aliases(self):
        """全都没别名时仍返回第一个启用的 —— 别让取号整个卡死。"""
        accounts = [
            {"id": 3, "email": "a@x.com", "source": "remote", "enabled": True},
            {"id": 4, "email": "b@x.com", "source": "remote", "enabled": True},
        ]
        row = self._pick(accounts, [])
        self.assertEqual(row.id, 3)

    def test_ignores_disabled_accounts(self):
        accounts = [
            {"id": 3, "email": "a@x.com", "source": "remote", "enabled": False},
            {"id": 4, "email": "b@x.com", "source": "remote", "enabled": True},
        ]
        aliases = [
            {"id": 1, "account_id": 3, "address": "u1@x.com",
             "status": "active", "pool_status": "available", "used_platforms": ""},
        ]
        row = self._pick(accounts, aliases)
        self.assertEqual(row.id, 4, "选到了停用的主号")

    def test_skips_account_whose_aliases_are_all_used_by_the_platform(self):
        """别名都被**本平台**用掉的主号要跳过（代码审查 2026-10-27 I3/I8）。

        原版这条测试的 docstring 说「`used_platforms` 含本平台的不算可用」，
        断言却是「兜底返回第一个，但没有可用的判断」—— 把缺口写成了预期。
        """
        accounts = [
            {"id": 3, "email": "a@x.com", "source": "remote", "enabled": True},
            {"id": 4, "email": "b@x.com", "source": "remote", "enabled": True},
        ]
        aliases = [
            # account 3 的别名池可用，但都已被 chatgpt 用过
            {"id": 1, "account_id": 3, "address": "u1@x.com", "status": "active",
             "pool_status": "available", "used_platforms": ",chatgpt,"},
            # account 4 有一个干净的
            {"id": 2, "account_id": 4, "address": "u2@x.com", "status": "active",
             "pool_status": "available", "used_platforms": ""},
        ]
        row = self._pick(accounts, aliases, platform="chatgpt")
        self.assertEqual(row.id, 4,
                         "选中了「别名已被本平台用尽」的主号 —— 取号会领不到")

    def test_skips_account_whose_aliases_are_all_registered_on_platform(self):
        """`accounts` 表登记过本平台的主号也要跳过（比 used_platforms 更权威）。

        生产实测：account 3 的 426 个 available 别名在 `accounts` 表里全都
        注册过 ChatGPT，但 `used_platforms` 是空的 —— 只看记账会挑中它，
        取号时全被 `claim_alias` 跳过，最后报「没有邮箱」。
        """
        accounts = [
            {"id": 3, "email": "a@x.com", "source": "remote", "enabled": True},
            {"id": 4, "email": "b@x.com", "source": "remote", "enabled": True},
        ]
        aliases = [
            {"id": 1, "account_id": 3, "address": "u1@x.com", "status": "active",
             "pool_status": "available", "used_platforms": ""},
            {"id": 2, "account_id": 3, "address": "u2@x.com", "status": "active",
             "pool_status": "available", "used_platforms": ""},
            {"id": 3, "account_id": 4, "address": "u3@x.com", "status": "active",
             "pool_status": "available", "used_platforms": ""},
        ]
        row = self._pick(
            accounts,
            aliases,
            platform="chatgpt",
            registered={"u1@x.com": {"chatgpt"}, "u2@x.com": {"chatgpt"}},
        )
        self.assertEqual(row.id, 4, "选中了「池里有号、实际全注册过」的主号")

    def test_prefers_account_with_most_claimable_aliases(self):
        """有多个主号可选时选可领数量最多的，别选只有 1 个干净号的。"""
        accounts = [
            {"id": 3, "email": "a@x.com", "source": "remote", "enabled": True},
            {"id": 4, "email": "b@x.com", "source": "remote", "enabled": True},
        ]
        aliases = [
            {"id": 1, "account_id": 3, "address": "a1@x.com", "status": "active",
             "pool_status": "available", "used_platforms": ""},
            {"id": 2, "account_id": 4, "address": "b1@x.com", "status": "active",
             "pool_status": "available", "used_platforms": ""},
            {"id": 3, "account_id": 4, "address": "b2@x.com", "status": "active",
             "pool_status": "available", "used_platforms": ""},
            {"id": 4, "account_id": 4, "address": "b3@x.com", "status": "active",
             "pool_status": "available", "used_platforms": ""},
        ]
        row = self._pick(accounts, aliases, platform="chatgpt")
        self.assertEqual(row.id, 4, "没选可领数量最多的主号")

    def test_without_platform_filter_ignores_used_platforms(self):
        """不传 platform 时保持旧行为（只看 pool_status）。"""
        accounts = [{"id": 3, "email": "a@x.com", "source": "remote", "enabled": True}]
        aliases = [
            {"id": 1, "account_id": 3, "address": "u1@x.com", "status": "active",
             "pool_status": "available", "used_platforms": ",chatgpt,"},
        ]
        self.assertEqual(self._pick(accounts, aliases).id, 3)


if __name__ == "__main__":
    unittest.main()
