"""导出账号快照，用于重构前后的**字段级 diff**。

只读：不写库、不改状态、不碰网络。

为什么需要它：测试只覆盖你想到的路径，dump diff 覆盖「实际库里的每一行」。
阶段 1（数据层）结束时这个 diff 必须为空；阶段 2 起允许差异但必须逐条登记
（见 `docs/refactor/METRICS.md`）。

凭据字段**打码**：输出里只留 `sha256:<12 位>`，既能看出「值变了」，又不会把
AT/RT/password 落到文件或终端历史里。

用法：

    python scripts/dump_accounts.py --platform chatgpt > /tmp/before.json
    # ... 重构 ...
    python scripts/dump_accounts.py --platform chatgpt > /tmp/after.json
    diff <(jq -S 'map(del(.updated_at))' /tmp/before.json) \\
         <(jq -S 'map(del(.updated_at))' /tmp/after.json)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 需要打码的 extra 键（与 `core.credential_fields` 的注册表保持一致，
#: 外加 password / cookie_header / totp_secret 这类不在注册表里的敏感值）。
_SECRET_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "session_token",
        "id_token",
        "sso",
        "sso_token",
        "client_id",
        "cookie_header",
        "cookies",
        "totp_secret",
        "password",
        "accessToken",
        "refreshToken",
        "sessionToken",
        "idToken",
    }
)


def _mask(value: Any) -> str:
    """把敏感值换成一个稳定的短指纹：值变了指纹就变，但原文不外泄。"""
    text = str(value or "")
    if not text:
        return ""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}:len{len(text)}"


def _redact_extra(extra: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (extra or {}).items():
        out[key] = _mask(value) if key in _SECRET_KEYS else value
    return out


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": getattr(row, "id", None),
        "platform": getattr(row, "platform", ""),
        "email": getattr(row, "email", ""),
        "user_id": getattr(row, "user_id", ""),
        "region": getattr(row, "region", ""),
        "status": getattr(row, "status", ""),
        "cashier_url": getattr(row, "cashier_url", ""),
        "token": _mask(getattr(row, "token", "")),
        "password": _mask(getattr(row, "password", "")),
        "created_at": str(getattr(row, "created_at", "") or ""),
        "updated_at": str(getattr(row, "updated_at", "") or ""),
        "extra": _redact_extra(
            row.get_extra() if callable(getattr(row, "get_extra", None)) else {}
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出账号快照（凭据打码）")
    parser.add_argument("--platform", default="", help="只导出该平台；留空 = 全部")
    parser.add_argument(
        "--indent", type=int, default=2, help="JSON 缩进（0 = 紧凑输出）"
    )
    args = parser.parse_args(argv)

    from core.db import account_repository

    rows = (
        account_repository.list_accounts(args.platform)
        if args.platform
        else account_repository.list_all_accounts()
    )
    payload = sorted(
        (_row_to_dict(r) for r in rows),
        key=lambda item: (item["platform"], item["email"]),
    )
    json.dump(
        payload,
        sys.stdout,
        ensure_ascii=False,
        indent=(args.indent or None),
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
