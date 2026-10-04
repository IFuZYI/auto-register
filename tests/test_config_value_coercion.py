"""
`PUT /api/config` 对非字符串值的容忍度。

`configs.value` 是字符串列。前端如果提交数组/对象（表单里的清单字段没转 JSON
就会这样），SQLite 会抛 `type 'list' is not supported` → 500，用户看到的是整个
保存失败、没有任何线索。这类 500 曾经真实发生过：邮箱页面加载时把整份配置灌进
antd form，保存时又把整份配置（含域名清单数组）提交回去。

两层防护，这里都覆盖：
1. 后端把非字符串值统一转成字符串（本文件测的就是这层）；
2. 前端只提交本页拥有的字段（`MailServicePage` 的保存走 `dropEmptySecrets`
   摘掉空口令键，由 `tests/test_secret_config_contract.py` 的源码契约覆盖）。

历史：这里曾用 `cfworker_domains` 做载体 —— 那个渠道（以及它的域名清单字段）
已随临时邮箱整体删除。改用 `contribution_key`：它是个自由文本字段，后端不做
任何格式校验，正好能把「非字符串值会不会炸」这件事单独测出来。

再后来 `contribution_key` 被列入口令键（`GET /api/config` 打码，读不回来），
所以载体换成 `cpa_api_url`：同样是自由文本、无格式校验，且**不是**口令，
读回来能看见值 —— 否则本文件测不到「存进去的是什么」。
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

# 载体字段：自由文本、无格式校验、非口令（读得回来），强制转换才测得准
CARRIER_KEY = "cpa_api_url"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import core.db as db

    engine = create_engine(f"sqlite:///{tmp_path / 'config.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)

    from main import app

    return TestClient(app)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # 数组 → JSON 字符串（曾经的 500 源头）
        (["a.com", "b.com"], '["a.com", "b.com"]'),
        ([], "[]"),
        # 布尔 → 小写字符串，与库里既有的 "true"/"0" 保持一致
        (True, "true"),
        (False, "false"),
        # None → 空串（清空字段的常见表达）
        (None, ""),
        # 对象 → JSON 字符串
        ({"a": 1}, '{"a": 1}'),
        # 数字 → 字符串
        (3, "3"),
        # 字符串原样保留
        ("ok", "ok"),
    ],
)
def test_non_string_values_are_coerced_not_500(client, payload, expected):
    resp = client.put("/api/config", json={"data": {CARRIER_KEY: payload}})

    assert resp.status_code == 200, resp.text
    stored = client.get("/api/config").json()[CARRIER_KEY]
    assert stored == expected
    assert isinstance(stored, str)


def test_unknown_keys_are_still_dropped(client):
    """类型强制不能绕过白名单：未知 key 依旧不落库。"""
    resp = client.put("/api/config", json={"data": {"not_a_real_key": ["x"]}})

    assert resp.status_code == 200
    assert resp.json()["updated"] == []
    assert "not_a_real_key" not in client.get("/api/config").json()


def test_every_ui_field_is_accepted_by_the_backend(client):
    """界面上能填的每个配置键，后端都必须接受。

    反过来的坑真实发生过：Mail.tm 的两个字段（`mailtm_domain` /
    `mailtm_password`）界面有输入框、后端也读它们，但漏在白名单外 ——
    用户填完保存，值被静默丢弃，界面上看不出任何异常。

    这里直接扫前端的 section 定义，逐个提交，确保都能落库。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "frontend" / "src"
    sections_src = (root / "lib" / "mailboxSections.ts").read_text(encoding="utf-8")
    ui_keys = set(re.findall(r"key: '([a-z0-9_]+)'", sections_src))
    assert ui_keys, "没解析出任何字段键，正则或文件结构变了？"

    # 全局配置页的字段也要一并覆盖：邮箱服务现在只剩两个渠道，但全局页
    # （默认邮箱服务 / 接码 / 验证码 / 注册）的键同样是界面能填的，漏一个就是静默丢值。
    #
    # 只取缩进 ≥8 空格的那些定义 —— tab 定义也写作 `key: 'mail'`（缩进 4），
    # 用宽松正则会把它当成配置键，然后永远报「后端不接受」。
    settings_src = (root / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    ui_keys |= set(re.findall(r"^ {8,}\{?\s*key: '([a-z0-9_]+)'", settings_src, re.M))

    rejected = []
    for key in sorted(ui_keys):
        resp = client.put("/api/config", json={"data": {key: "probe-value"}})
        if resp.status_code != 200 or key not in resp.json().get("updated", []):
            rejected.append(key)

    assert not rejected, f"这些键在界面上能填，但后端不接受（保存会被丢弃）：{rejected}"
