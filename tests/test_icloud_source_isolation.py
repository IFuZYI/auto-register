"""远程仓库的两个既有缺陷（独立 reviewer 探到，2026-10-09；已复现确认）。

背景：ADR-0001 承诺「同步只做远端→本地单向拉取；同地址去重时本地行优先」，
并写明「反向（本地同步遇到远程行）同理跳过，不把远程行改成本地」。
`icloud_accounts.email` 的单列唯一被移除，正是为了让同一个 Apple ID 能在
本地与远程各有一行。但两处按 email / address 的查找没有按 `source` 过滤：

缺陷 A（`import_session`，accounts.py）
    本地「应用内登录 / 手工导入 Cookie」时按邮箱查行，命中**远程行**就原地更新它 ——
    本地凭据写进远程行、`source` 仍是 remote，且不会建出本地账号。用户以为加了
    本地主号，实际改的是远程仓库账号（凭据与远端密码混在一行，语义全乱）。

缺陷 B（`_upsert_alias`，aliases.py）
    本地 `sync_aliases` 按**地址**查别名行，命中远程行就无条件
    `row.account_id = int(account_id)` 抢走它 —— 远程行的归属被改成另一个本地
    账号，而 `source` 仍是 remote。之后该行的删除/停用会按 remote 分支去调
    远端服务（拿错的 base_url / remote_account_id），远端账实不符。

TDD：本文件先于修复写完（RED），修复后必须全绿；随后做变异验证。

隔离方式：每个测试用例自建临时库并把 `platform_database_registry` 指过去，
**用完立即还原**（`try/finally`）—— 全局注册表在两个用例间泄漏会让一个用例的
失败连带另一个（实测：只变异 aliases.py，import_session 的用例也跟着红）。
"""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from sqlmodel import SQLModel, Session, create_engine, select

from core.db import ICloudAccountModel, ICloudAliasModel, platform_database_registry


@contextmanager
def _isolated_icloud_db():
    """临时 icloud 库 + 真 secret_box；退出时还原全局注册表。"""
    import core.secret_box as secret_box_module
    from core.secret_box import SecretBox
    from services import icloud_service

    os.environ.setdefault(
        "CREDENTIAL_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode()
    )
    tmp = Path(tempfile.mkdtemp())
    url = f"sqlite:///{tmp}/icloud.db"
    engine = create_engine(url)
    SQLModel.metadata.create_all(engine)

    saved_secret_box = secret_box_module.secret_box
    platform_database_registry.configure({"icloud": url})
    secret_box_module.secret_box = SecretBox()
    icloud_service.secret_box = secret_box_module.secret_box
    try:
        yield icloud_service, engine
    finally:
        platform_database_registry.configure({})
        secret_box_module.secret_box = saved_secret_box
        engine.dispose()


def _seed_accounts(engine, *, remote_email="remote-id@icloud.com", local_email="local-id@icloud.com"):
    with Session(engine) as s:
        remote = ICloudAccountModel(
            email=remote_email,
            display_name="远端那份",
            source="remote",
            base_url="http://hme:8081",
            remote_account_id="acc_1",
            remote_password_cipher="cipher",
        )
        local = ICloudAccountModel(email=local_email, display_name="本地那份", source="local")
        s.add(remote)
        s.add(local)
        s.commit()
        s.refresh(remote)
        s.refresh(local)
        return remote.id, local.id


class ImportSessionDoesNotTouchRemoteRowTests(unittest.TestCase):
    """缺陷 A：本地导入不得写进同邮箱的远程行。"""

    def test_import_session_creates_local_row_when_email_matches_remote(self):
        """同邮箱已有远程行时，本地导入必须新建本地行（不是更新远程行）。"""
        from platforms.icloud import SessionImportRequest
        from tests.test_icloud_service import _StubWebClient, _imported

        with _isolated_icloud_db() as (icloud_service, engine):
            _seed_accounts(engine, remote_email="same-id@icloud.com")

            holder = {}

            @contextmanager
            def _factory(**_kwargs):
                yield holder["client"]

            saved_web_client = icloud_service.web_client
            icloud_service.web_client = _factory
            try:
                holder["client"] = _StubWebClient(imported=_imported(email="same-id@icloud.com"))

                icloud_service.import_session(SessionImportRequest(cookie_header="a=1"))

                with Session(engine) as s:
                    rows = s.exec(
                        select(ICloudAccountModel).where(
                            ICloudAccountModel.email == "same-id@icloud.com"
                        )
                    ).all()

                sources = sorted(str(r.source) for r in rows)
                self.assertEqual(
                    sources,
                    ["local", "remote"],
                    f"同邮箱应各有本地与远程一行，实际 {sources} —— 本地导入写进了远程行",
                )
                remote_row = next(r for r in rows if r.source == "remote")
                self.assertEqual(remote_row.base_url, "http://hme:8081", "远程行被本地导入改动了")
                self.assertEqual(
                    remote_row.remote_account_id, "acc_1", "远程行被本地导入改动了"
                )
                self.assertEqual(
                    remote_row.remote_password_cipher, "cipher", "远程行的远端密码被覆盖"
                )
            finally:
                icloud_service.web_client = saved_web_client


class UpsertAliasDoesNotStealRemoteRowTests(unittest.TestCase):
    """缺陷 B：本地同步不得抢走同地址的远程行。"""

    def test_local_sync_skips_remote_row_with_same_address(self):
        with _isolated_icloud_db() as (icloud_service, engine):
            remote_id, local_id = _seed_accounts(engine)
            with Session(engine) as s:
                s.add(
                    ICloudAliasModel(
                        account_id=remote_id,
                        address="shared@icloud.com",
                        label="远端标签",
                        status="active",
                        provider_id="pid-remote",
                        source="remote",
                        pool_status="in_use",
                        used_platforms="chatgpt",
                    )
                )
                s.commit()

            icloud_service._upsert_alias(
                local_id,
                {
                    "address": "shared@icloud.com",
                    "label": "本地标签",
                    "provider_id": "pid-local",
                    "status": "active",
                },
                "local-id@icloud.com",
            )

            with Session(engine) as s:
                rows = s.exec(
                    select(ICloudAliasModel).where(ICloudAliasModel.address == "shared@icloud.com")
                ).all()

            self.assertEqual(len(rows), 1, f"同地址应只有一行（远程行优先），实际 {len(rows)} 行")
            row = rows[0]
            self.assertEqual(row.account_id, remote_id, "远程行的归属被本地同步抢走了")
            self.assertEqual(row.source, "remote", "远程行的来源被改动")
            self.assertEqual(row.provider_id, "pid-remote", "远程行的 provider_id 被本地同步覆盖")
            self.assertEqual(row.pool_status, "in_use", "远程行的池记账被本地同步改动")
            self.assertEqual(row.used_platforms, "chatgpt", "远程行的平台记账被本地同步改动")

    def test_remote_account_sync_still_updates_its_own_row(self):
        """远程账号自己同步仍要更新自己的行（守卫不能把正常路径也堵死）。"""
        with _isolated_icloud_db() as (icloud_service, engine):
            remote_id, _local_id = _seed_accounts(engine)
            with Session(engine) as s:
                s.add(
                    ICloudAliasModel(
                        account_id=remote_id,
                        address="own@icloud.com",
                        label="旧标签",
                        status="active",
                        provider_id="pid-own",
                        source="remote",
                    )
                )
                s.commit()

            # 同一个 account_id 再 upsert（远程同步的更新分支）→ 必须真的更新
            icloud_service._upsert_alias(
                remote_id,
                {"address": "own@icloud.com", "label": "新标签", "provider_id": "pid-own", "status": "active"},
                "remote-id@icloud.com",
            )

            with Session(engine) as s:
                row = s.exec(
                    select(ICloudAliasModel).where(ICloudAliasModel.address == "own@icloud.com")
                ).first()

            self.assertEqual(row.account_id, remote_id)
            self.assertEqual(row.label, "新标签", "远程账号自己的同步更新被守卫误挡")


if __name__ == "__main__":
    unittest.main()
