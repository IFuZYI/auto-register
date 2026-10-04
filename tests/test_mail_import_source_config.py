"""邮箱导入面板选中的那一栏要能存住。

以前只有 mail_provider（microsoft / applemail）落库，界面上的 Outlook / Hotmail /
MailAPI URL 三个视图靠反推，反推不出 MailAPI URL——选完退出再进来就变回 Outlook。

applemail（小苹果）视图已弃用删除：它不是 iCloud，是个第三方临时邮箱服务。
旧库里存过的值在读写两侧都收敛到 Outlook 视图（provider 收敛到 microsoft）。
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

from services.mail_imports import (
    align_source_with_provider,
    normalize_mail_import_source,
    resolve_mail_provider_from_source,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    import core.config_store as config_store_module
    import core.db as db
    from core.db import platform_database_registry

    engine = create_engine(f"sqlite:///{tmp_path / 'config.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    platform_database_registry.configure({"icloud": str(engine.url)})
    # config_store 通过 core.db.current_engine() 动态取库，patch db.engine 即可

    from main import app

    return TestClient(app)


@pytest.mark.parametrize(
    ("stored", "provider", "expected"),
    [
        ("mailapi", "microsoft", "mailapi"),
        ("hotmail", "microsoft", "hotmail"),
        ("MailAPI", "microsoft", "mailapi"),
        # 旧库里只有 microsoft/applemail 两个值，两个都收敛到 Outlook 视图
        ("microsoft", "microsoft", "outlook"),
        ("applemail", "microsoft", "outlook"),
        ("", "applemail", "outlook"),
        ("", "microsoft", "outlook"),
        ("", "luckmail", "outlook"),
        # 已弃用视图（以及任何认不出的值）一律兜底 Outlook
        ("nonsense", "applemail", "outlook"),
    ],
)
def test_normalize_mail_import_source(stored, provider, expected):
    assert normalize_mail_import_source(stored, provider) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("outlook", "microsoft"),
        ("hotmail", "microsoft"),
        ("mailapi", "microsoft"),
        # 弃用视图同样落在微软号池上（收敛发生在视图层，provider 恒为 microsoft）
        ("applemail", "microsoft"),
    ],
)
def test_three_microsoft_views_share_one_pool(source, expected):
    assert resolve_mail_provider_from_source(source) == expected


def test_provider_wins_when_the_two_disagree():
    assert align_source_with_provider("mailapi", "applemail") == "mailapi"
    assert align_source_with_provider("applemail", "microsoft") == "outlook"
    assert align_source_with_provider("mailapi", "microsoft") == "mailapi"
    # provider 不是邮箱导入时视图照存，等用户切回来还是他选的那一栏
    assert align_source_with_provider("mailapi", "luckmail") == "mailapi"


def test_mailapi_view_survives_a_reload(client):
    client.put(
        "/api/config",
        json={"data": {"mail_provider": "microsoft", "mail_import_source": "mailapi"}},
    )

    config = client.get("/api/config").json()

    assert config["mail_import_source"] == "mailapi"
    assert config["mail_provider"] == "microsoft"


def test_panel_can_change_only_the_view(client):
    client.put("/api/config", json={"data": {"mail_provider": "microsoft"}})

    client.put("/api/config", json={"data": {"mail_import_source": "mailapi"}})

    assert client.get("/api/config").json()["mail_import_source"] == "mailapi"


def test_write_side_converges_deprecated_provider_case_insensitively(client):
    """写入侧的收敛必须大小写无关。

    回归测试：早先写侧用的是 `== "outlook"` / `== "applemail"` 精确比较，
    实测 `PUT 'AppleMail'` / `'OUTLOOK'` / `' outlook '` 都会按原样落库 ——
    读侧 `get_all()` 会收敛所以没有功能故障，但库里留下了已删除的 provider
    名，排查时看不到任何线索。改成共享的 `normalize_mail_provider()` 后，
    已删渠道统一落成空串、存活渠道统一落成小写规范名。
    """
    import core.config_store as config_store_module

    # 已删除的渠道（含大小写变体）→ 空串，等用户重选
    for sent in ("AppleMail", "applemail", "LuckMail", "CFWorker"):
        response = client.put("/api/config", json={"data": {"mail_provider": sent}})
        assert response.status_code == 200, f"PUT {sent!r} 失败: {response.status_code}"
        stored = config_store_module.config_store.get("mail_provider")
        assert stored == "", f"PUT {sent!r} 落库成了 {stored!r}（应为空串）"

    # 存活的渠道 → 小写规范名，不能被误清空
    for sent, expected in (("OUTLOOK", "outlook"), (" outlook ", "outlook"),
                           ("MICROSOFT", "microsoft")):
        client.put("/api/config", json={"data": {"mail_provider": sent}})
        stored = config_store_module.config_store.get("mail_provider")
        assert stored == expected, f"PUT {sent!r} 落库成了 {stored!r}（应为 {expected}）"

    # 正常值不能被误改
    client.put("/api/config", json={"data": {"mail_provider": "icloud_local"}})
    assert config_store_module.config_store.get("mail_provider") == "icloud_local"


def test_config_without_a_stored_view_falls_back_instead_of_returning_blank(client):
    client.put("/api/config", json={"data": {"mail_provider": "microsoft"}})

    assert client.get("/api/config").json()["mail_import_source"] == "outlook"


def test_legacy_applemail_provider_converges_on_read_and_write(client):
    """老库里存过 mail_provider=applemail —— 这个渠道已经删了，读到就要收敛。

    收敛成**空串**而不是某个存活渠道：用户明确要求「全局默认留空、注册任务里
    必须显式选」。把死值悄悄改写成 Outlook 会让任务在用户不知情的情况下消耗
    微软号池 —— 那不是「修好了」，是换了个方式出错。

    不收敛的话注册任务会拿一个不存在的 provider 名去 create_mailbox，
    抛 `ValueError: 未知邮箱提供商`，而界面上看起来一切正常。
    """
    # 直接写库模拟旧数据（绕过 PUT 的收敛，否则测不到读侧）
    import core.config_store as config_store_module

    config_store_module.config_store.set("mail_provider", "applemail")

    config = client.get("/api/config").json()

    assert config["mail_provider"] == ""
    assert config["mail_import_source"] == "outlook"


def test_legacy_applemail_write_is_converged_before_storage(client):
    """老前端缓存/旧脚本仍可能 PUT applemail —— 落库前就得收敛成空串。"""
    client.put("/api/config", json={"data": {"mail_provider": "applemail"}})

    import core.config_store as config_store_module

    assert config_store_module.config_store.get("mail_provider") == ""


def test_every_deleted_channel_converges_to_blank():
    """**每一个**已删除渠道名都要收敛成空串，不能漏。

    回归测试：这张表最初只列了 `applemail` 一个。老库里存过 luckmail /
    cfworker / duckmail 的部署会把死值原样透传到 `create_mailbox()`，
    报错只剩一句 `未知邮箱提供商: 'luckmail'`，配置页上却毫无异样 ——
    排查时联想不到是渠道被删了。
    """
    from core.mail_import_sources import normalize_mail_provider

    deleted = [
        "aitre", "applemail", "cfworker", "cloudmail", "duckmail", "freemail",
        "gptmail", "icloud_hme", "laoudo", "luckmail", "maliapi", "mailtm",
        "moemail", "opentrashmail", "skymail", "tempmail_lol",
    ]
    for name in deleted:
        assert normalize_mail_provider(name) == "", f"{name} 没有收敛成空串"
        # 大小写变体同样要收敛（写侧会先 lower）
        assert normalize_mail_provider(name.upper()) == "", f"{name.upper()} 没有收敛"

    # 存活的渠道名必须原样保留，别把收敛写成「什么都清空」
    for name in ("microsoft", "outlook", "icloud_local"):
        assert normalize_mail_provider(name) == name, f"{name} 被误清空了"


def test_get_all_converges_legacy_provider_for_non_ui_callers(client):
    """收敛必须发生在 `get_all()`（数据层），不能只在 `GET /api/config`（视图层）。

    回归测试：只在视图层收敛时，界面显示空而 `get_all()` 仍返回 `applemail`。
    走 `get_all()` 的兄弟路径（`api/tasks.py` 构邮箱、`cpa_manager` 自动注册）
    会拿它去 `create_mailbox()`，抛 `ValueError: 未知邮箱提供商: 'applemail'`
    —— 失败点离配置页很远，且报错里看不出是配置遗留。
    """
    import core.config_store as config_store_module

    # 直接写库模拟旧数据（绕过 PUT 的收敛，否则测不到读侧）
    config_store_module.config_store.set("mail_provider", "applemail")

    raw = config_store_module.config_store.get_all()
    assert raw["mail_provider"] == "", "get_all() 必须收敛，否则非 UI 调用方会炸"

    # 收敛后的空串要能给出可读的报错，而不是别的东西
    import pytest

    from core.mailboxes import create_mailbox

    with pytest.raises(ValueError, match="未知邮箱提供商"):
        create_mailbox(provider=raw["mail_provider"], extra=raw, proxy=None)


def test_mail_import_provider_builds_the_microsoft_channel():
    """`mail_provider='mail_import'` 必须能建出渠道，不能抛「未知邮箱提供商」。

    回归测试：「邮箱导入」是前端下拉里的一个取值，`MailImportPanel` 与注册页
    都会把它写进配置（`MailImportPanel.tsx:325`、`RegisterTaskPage.tsx:132`）。
    注册页提交前会用 `resolveEffectiveMailProvider` 收敛成 `microsoft`，但别的
    入口把配置原值直接透传：Accounts 页快速注册（`Accounts.tsx:887` 用
    `cfg.mail_provider`）、`cpa_manager` 自动注册（`extra={}` → 落回全局配置）、
    `chatgpt_otp_mailbox` 的 otp 回填。这些路径会在建邮箱那一步抛
    `ValueError: 未知邮箱提供商: 'mail_import'`，任务启动即失败。
    """
    from core.mailboxes import create_mailbox

    mailbox = create_mailbox(provider="mail_import", extra={}, proxy=None)
    assert mailbox is not None

    # 未知名字仍然必须报错（别把修复写成「什么都认」）
    import pytest

    with pytest.raises(ValueError, match="未知邮箱提供商"):
        create_mailbox(provider="__definitely_not_a_provider__", extra={}, proxy=None)
