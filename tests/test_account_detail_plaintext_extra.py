"""详情端点必须返回**明文** extra 凭据，且时间格式与列表一致。

事故（2026-10-31）：账号详情弹窗里「Access Token / Refresh Token / TOTP 2FA」
三行显示的是 `v1:0hh8AWoFj9noSt7rHDXIwVwgSLg63nrtgGWu5gDUa6NHSqGWxFnJhY...`，
复制出来也是这串。原因不是前端 —— `extra_json` 这一列**落盘就是密文**
（AT/RT/IDT/ST/SSO/totp_secret 六把凭证），而 `GET /api/accounts/{id}`
把模型直接丢给 FastAPI 序列化，密文原样下发。

列表端点裁正文是对的（`_slim_account_row`）；详情端点要给人看、给人复制，
必须解密。这两个方向不矛盾，是同一件事的两面：
**列表不给你不需要的，详情给你要的。**

⚠️ 解密时不能用 `model_dump()` 重建 payload —— 那会把 `created_at` 变成
`+00:00`，而列表给的是 `Z`（`test_list_keeps_iso_datetime_format` 就是为
这个 bug 写的，这次在详情端点上又踩了一遍）。用 `model_copy(update=...)`
保留模型自己的序列化器。

这组测试走**真 HTTP**（TestClient）而不是直接调函数：序列化是 FastAPI 做的，
直接调函数只能看到 Python 对象，测不到时间格式。
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.sig"
RT = "rt.1.AAAr_DI0IH46JDu9NZyd4VvsUbY2mBLizGKgk"
TOTP = "JBSWY3DPEHPK3PXP"


@pytest.fixture(scope="module")
def client():
    sys.path.insert(0, ROOT)
    import main  # noqa: PLC0415

    return TestClient(main.app)


def _seed(email: str) -> str:
    from core.base_platform import Account, AccountStatus  # noqa: PLC0415
    from core.db import account_repository  # noqa: PLC0415

    acct = Account(
        platform="chatgpt",
        email=email,
        password="pw-123",
        token=AT,
        status=AccountStatus.REGISTERED,
        extra={"access_token": AT, "refresh_token": RT, "totp_secret": TOTP},
    )
    account_repository.upsert(acct)
    return email


def _cleanup(emails: list[str]) -> None:
    from core.db import account_repository  # noqa: PLC0415

    for email in emails:
        try:
            row = account_repository.get_by_email(email, platform="chatgpt")
            if row is not None:
                account_repository.delete(row.id, platform="chatgpt")
        except Exception:
            pass


def _row(client: TestClient, email: str) -> dict:
    items = client.get("/api/accounts", params={"page_size": 500, "email": email}).json()["items"]
    return next(r for r in items if r["email"] == email)


def _detail(client: TestClient, email: str) -> dict:
    resp = client.get(f"/api/accounts/{_row(client, email)['id']}", params={"platform": "chatgpt"})
    assert resp.status_code == 200
    return resp.json()


def test_detail_extra_is_decrypted(client):
    email = _seed("detail_plain_1@example.com")
    try:
        detail = _detail(client, email)
        extra = json.loads(detail["extra_json"])
        assert extra["access_token"] == AT, "详情里的 access_token 仍是密文"
        assert extra["refresh_token"] == RT
        assert extra["totp_secret"] == TOTP
        assert not str(detail["extra_json"]).startswith("v1:"), "extra_json 整串仍是密文"
    finally:
        _cleanup([email])


def test_detail_token_column_is_plaintext(client):
    email = _seed("detail_plain_2@example.com")
    try:
        detail = _detail(client, email)
        assert detail["token"] == AT, "token 列没有解密"
        assert "token_cipher" not in detail, "密文列不该出现在响应里"
    finally:
        _cleanup([email])


def test_detail_keeps_iso_datetime_format(client):
    """解密不能顺手改掉时间格式。

    `model_dump()` 会把 `created_at` 变成 `+00:00`，而列表给的是 `Z` ——
    同一时刻两种字符串是对外契约的静默变化。这条钉住两个端点必须一致。
    """
    email = _seed("detail_plain_3@example.com")
    try:
        row = _row(client, email)
        detail = _detail(client, email)
        assert str(detail["created_at"]) == str(row["created_at"]), (
            f"列表与详情的时间格式不一致: {row['created_at']!r} vs {detail['created_at']!r}"
        )
    finally:
        _cleanup([email])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
