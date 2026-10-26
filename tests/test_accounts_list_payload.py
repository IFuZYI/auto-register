"""账号列表的响应瘦身：重型 token 字段不进列表。

实测（全库 1006 个账号）：`extra_json` 合计 6.53 MB，其中
`session_token` 2.53 MB（39%）、`access_token` 1.48 MB（23%）、
`cpa_record` 0.66 MB（10%）、`cookies` 0.20 MB、`id_token` 0.19 MB。

列表页只需要「有没有 AT/RT」这两个布尔（`Accounts.tsx` 的
`getAccessToken` / `getRefreshToken` 只用来显示有无），正文一概不用。
详情端点仍返回全量。

这组测试钉住：
1. 列表里没有 `session_token` / `id_token` / `cookies` / `cpa_record`；
2. `access_token` / `refresh_token` 变成布尔标记 `has_access_token` /
   `has_refresh_token`；
3. 列表页真正要用的字段（`chatgpt_local` / `totp_secret` 等）仍在；
4. 详情端点仍是全量；
5. 负载量级降下来。
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HEAVY_KEYS = ("session_token", "id_token", "cookies", "cpa_record")


@pytest.fixture(scope="module")
def client():
    sys.path.insert(0, ROOT)
    import main  # noqa: PLC0415

    return TestClient(main.app)


def _make_account(idx: int, *, with_tokens: bool = True) -> str:
    """造一个带重型字段的账号，返回 email。"""
    from core.base_platform import Account, AccountStatus, RegisterConfig  # noqa: PLC0415
    from core.db import account_repository  # noqa: PLC0415
    from platforms.chatgpt.plugin import ChatGPTPlatform  # noqa: PLC0415

    email = f"payload_probe_{idx}@example.com"
    extra = {
        "chatgpt_local": {"auth": {"state": "access_token_valid"}},
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "register_proxy": "socks5h://u:p@127.0.0.1:1",
        # 重型字段：真实体量（session_token 平均 4 KB、AT 1.5 KB）
        "session_token": "s" * 4000,
        "access_token": "a" * 1500,
        "id_token": "i" * 300,
        "cookies": "c" * 350,
        "cpa_record": {"blob": "r" * 1800},
    }
    if with_tokens:
        extra["refresh_token"] = "rt." + "x" * 190
    acct = Account(
        platform="chatgpt", email=email, password="pw",
        token="", status=AccountStatus.REGISTERED, extra=extra,
    )
    account_repository.upsert(acct)
    return email


def _cleanup(client: TestClient, emails: list[str]) -> None:
    from core.db import account_repository  # noqa: PLC0415

    for email in emails:
        try:
            row = account_repository.get_by_email(email, platform="chatgpt")
            if row is not None:
                account_repository.delete(row.id, platform="chatgpt")
        except Exception:
            pass


def _row_for(client: TestClient, email: str) -> dict:
    resp = client.get("/api/accounts", params={"page_size": 500, "email": email})
    assert resp.status_code == 200
    items = resp.json()["items"]
    row = next((r for r in items if r["email"] == email), None)
    assert row is not None, f"造的账号没出现在列表里（{email}）"
    return row


def test_list_drops_heavy_token_fields(client):
    email = _make_account(1)
    try:
        row = _row_for(client, email)
        extra = json.loads(row["extra_json"])
        for key in HEAVY_KEYS:
            assert key not in extra, (
                f"列表里仍带着 {key} —— 它占了 extra_json 的大头，列表页不用"
            )
    finally:
        _cleanup(client, [email])


def test_list_replaces_tokens_with_booleans(client):
    email = _make_account(2)
    try:
        row = _row_for(client, email)
        extra = json.loads(row["extra_json"])
        assert "access_token" not in extra, "AT 正文不该进列表"
        assert "refresh_token" not in extra, "RT 正文不该进列表"
        assert extra.get("has_access_token") is True, "缺 has_access_token 标记"
        assert extra.get("has_refresh_token") is True, "缺 has_refresh_token 标记"
    finally:
        _cleanup(client, [email])


def test_list_reports_false_when_no_token(client):
    """没有 AT/RT 的账号要如实报 False —— 否则前端会把空号显示成「有 AT」。"""
    email = _make_account(3, with_tokens=False)
    try:
        row = _row_for(client, email)
        extra = json.loads(row["extra_json"])
        assert extra.get("has_refresh_token") is False
    finally:
        _cleanup(client, [email])


def test_list_keeps_fields_the_page_uses(client):
    """列表页渲染要用的字段不能被一起裁掉。"""
    email = _make_account(4)
    try:
        row = _row_for(client, email)
        extra = json.loads(row["extra_json"])
        assert extra.get("chatgpt_local"), "chatgpt_local 被裁了 —— 列表要读 state/plan"
        assert extra.get("totp_secret"), "totp_secret 被裁了 —— 列表要显示 2FA 状态"
        assert extra.get("register_proxy"), "register_proxy 被裁了"
    finally:
        _cleanup(client, [email])


def test_detail_endpoint_still_returns_everything(client):
    """详情端点必须仍给全量 —— 它才是看 token 正文的地方。"""
    email = _make_account(5)
    try:
        row = _row_for(client, email)
        resp = client.get(f"/api/accounts/{row['id']}", params={"platform": "chatgpt"})
        assert resp.status_code == 200
        extra = json.loads(resp.json()["extra_json"])
        assert extra.get("session_token"), "详情页的 session_token 丢了"
        assert extra.get("access_token"), "详情页的 access_token 丢了"
        assert extra.get("refresh_token"), "详情页的 refresh_token 丢了"
        assert extra.get("id_token"), "详情页的 id_token 丢了"
    finally:
        _cleanup(client, [email])


def test_list_payload_per_item_is_small(client):
    """单条平均值要降下来（改造前每条约 7 KB，大头是 token 正文）。"""
    emails = [_make_account(10 + i) for i in range(5)]
    try:
        resp = client.get("/api/accounts", params={"page_size": 500})
        assert resp.status_code == 200
        body = resp.json()
        mine = [r for r in body["items"] if r["email"] in set(emails)]
        assert len(mine) == 5, "造的账号没全部出现在列表里"
        per_item = sum(len(json.dumps(r, ensure_ascii=False)) for r in mine) / len(mine)
        assert per_item < 3000, (
            f"列表里每条仍有 {per_item:.0f} 字节 —— 重型字段没裁干净"
        )
    finally:
        _cleanup(client, emails)
