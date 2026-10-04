"""面板注册表的契约测试。

`services/panel_registry.py` 是前后端共用的契约：`GET /api/integrations/panels`
把它发给界面，前端按 `url_key` / `secret_key` 取值、按 `github` 渲染项目主页
链接、按 `key` 查图标。任何一项改名都会让界面静默退化（图标退回默认、
「去配置」跳不到正确字段），而不是报错 —— 所以这里把形状钉住。

刻意断言「形状与存在性」而不是「具体有哪些面板」：增删面板是正常演进，
但字段名与 key 的唯一性不是。
"""

from __future__ import annotations

import pytest

from services.panel_registry import PANELS, PANELS_BY_KEY, list_panels


def test_panels_is_not_empty():
    assert PANELS, "面板注册表不能为空"


def test_every_panel_has_the_contract_fields():
    """前端读取的字段必须齐备（缺一个就是界面静默退化）。"""
    required = {"key", "label", "desc", "url_key", "github"}
    for panel in PANELS:
        missing = required - set(panel)
        assert not missing, f"面板 {panel.get('key')!r} 缺少字段: {sorted(missing)}"


def test_panel_keys_are_unique():
    """key 是前端的查表键，重复会让一个面板被另一个覆盖。"""
    keys = [p["key"] for p in PANELS]
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"面板 key 重复: {sorted(dupes)}"


def test_url_keys_are_unique_except_declared_sharing():
    """url_key 是配置键，两个面板共用一个键会互相覆盖地址。

    **例外**：两个面板若是同一个服务（同一端点、同一套凭据），共享是刻意的。
    这种共享必须在注册表里显式声明（`same_service_as`），否则视为意外重复 ——
    意外重复的后果是用户在一个页面改了地址、另一个页面还显示旧值，
    或者更糟：两次保存互相覆盖。

    实测踩过：CLIProxyAPI 与 CPA 面板都打 `/v0/management/auth-files`，
    历史上各配一份（`cliproxyapi_base_url` / `cpa_api_url`），
    用户要在两个页面填同样的东西。现在收敛为一套键 + 显式声明共享。
    """
    for panel in PANELS:
        shared_with = panel.get("same_service_as")
        if shared_with:
            other = PANELS_BY_KEY.get(shared_with)
            assert other is not None, (
                f"面板 {panel['key']!r} 声明与 {shared_with!r} 同服务，但后者不存在"
            )
            assert panel["url_key"] == other["url_key"], (
                f"面板 {panel['key']!r} 声明与 {shared_with!r} 同服务，"
                f"url_key 却不一致（{panel['url_key']!r} vs {other['url_key']!r}）"
            )

    # 未声明共享的面板之间，url_key 必须唯一
    unshared = [p for p in PANELS if not p.get("same_service_as")]
    url_keys = [p["url_key"] for p in unshared]
    dupes = {k for k in url_keys if url_keys.count(k) > 1}
    assert not dupes, f"面板 url_key 意外重复: {sorted(dupes)}"


def test_github_links_point_at_github():
    """前端按这个字段渲染项目主页按钮；空串会导致按钮不渲染。"""
    for panel in PANELS:
        github = str(panel.get("github") or "")
        assert github, f"面板 {panel['key']!r} 的 github 为空 —— 界面不会渲染项目主页按钮"
        assert github.startswith("https://github.com/"), (
            f"面板 {panel['key']!r} 的 github 不是 GitHub 链接: {github!r}"
        )


def test_secret_key_when_present_is_a_string():
    """`secret_key` 可选，但给了就必须是非空字符串（前端用它决定是否渲染口令输入框）。"""
    for panel in PANELS:
        if "secret_key" not in panel:
            continue
        secret_key = panel["secret_key"]
        assert isinstance(secret_key, str) and secret_key.strip(), (
            f"面板 {panel['key']!r} 的 secret_key 必须是非空字符串，实际: {secret_key!r}"
        )


def test_panels_by_key_matches_panels():
    """`PANELS_BY_KEY` 是后端按 key 查面板的入口，必须与 PANELS 同源。

    （原先这个导出没有任何调用方，docstring 却声称「tests 直接断言它」——
    现在这条测试让那句话成立。）
    """
    assert set(PANELS_BY_KEY) == {p["key"] for p in PANELS}
    for key, panel in PANELS_BY_KEY.items():
        assert panel["key"] == key, f"PANELS_BY_KEY[{key!r}] 指向了别的面板"


def test_list_panels_returns_copies():
    """`list_panels()` 必须返回拷贝 —— 调用方改返回的 dict 不该脏注册表。"""
    first = list_panels()
    assert first, "list_panels() 返回空"
    first[0]["label"] = "被改脏了"
    assert PANELS[0]["label"] != "被改脏了", "list_panels() 返回的是注册表内部对象"


def test_endpoint_never_returns_secret_values():
    """`GET /api/integrations/panels` 只回 `secret_set` 布尔，绝不回口令本身。

    安全属性：面板页需要知道「口令有没有设置过」（决定要不要提示用户重填），
    但拿到明文会让页面一打开就把它渲染进 DOM。这里真的写一个口令进库，
    再确认响应体里搜不到它。
    """
    from fastapi.testclient import TestClient

    from core.config_store import config_store
    from main import app

    secret_panels = [p for p in PANELS if p.get("secret_key")]
    if not secret_panels:
        pytest.skip("当前注册表里没有带 secret_key 的面板")

    marker = "PANEL-SECRET-MARKER-DO-NOT-LEAK"
    for panel in secret_panels:
        config_store.set(panel["secret_key"], marker)

    client = TestClient(app)
    response = client.get("/api/integrations/panels")
    assert response.status_code == 200

    assert marker not in response.text, (
        "接口把口令明文回给了前端 —— 页面一打开就会渲染进 DOM"
    )

    # 但「设置过」这个事实要能看出来，否则界面没法提示用户
    for item in response.json()["items"]:
        if item.get("secret_key"):
            assert item["secret_set"] is True, (
                f"{item['key']} 存了口令却没报告 secret_set"
            )

    # 收尾：别把标记留在库里
    for panel in secret_panels:
        config_store.set(panel["secret_key"], "")


def test_endpoint_shape_matches_registry():
    """`GET /api/integrations/panels` 的返回项必须带上注册表字段 + 运行期字段。"""
    from fastapi.testclient import TestClient

    from main import app

    client = TestClient(app)
    response = client.get("/api/integrations/panels")
    assert response.status_code == 200

    body = response.json()
    items = body.get("items")
    assert isinstance(items, list) and items, "接口没返回面板清单"

    registry_keys = {p["key"] for p in PANELS}
    returned_keys = {item["key"] for item in items}
    assert returned_keys == registry_keys, "接口返回的面板与注册表不一致"

    for item in items:
        assert "url" in item, f"{item['key']} 缺少 url"
        assert "configured" in item, f"{item['key']} 缺少 configured"
        assert isinstance(item["secret_set"], bool), (
            f"{item['key']} 的 secret_set 必须是布尔（不能把口令本身回给前端）"
        )
