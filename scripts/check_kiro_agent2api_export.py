"""Contract check: the Kiro export must satisfy Agent2API's account import.

Agent2API's `account_transfer::import_one_item` requires a non-empty `id` per
record; its identity fallback reads `uid` before `email`, so exporting `uid`
makes every account share one identity; the Kiro plugin compares
`loginProvider == "builderid"` case-sensitively. A schema-shaped file that
misses these still fails every row on import.

Usage: python scripts/check_kiro_agent2api_export.py   (exit 1 on violation)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 直接 `python scripts/...` 时 sys.path[0] 是 scripts/，补上仓库根
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sample(auth_method: str, login_provider: str, client_id: str, client_secret: str):
    from core.models.account import AccountRecord
    from platforms.kiro.credentials import build_extra

    extra = build_extra(
        access_token="at-1",
        refresh_token="rt-1",
        client_id=client_id,
        client_secret=client_secret,
        profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/X",
        region="us-east-1",
        auth_method=auth_method,
        login_provider=login_provider,
        plan="Kiro FREE",
        account_id="a@x.ai",
        email="a@x.ai",
        expires_at=1794478346773,
    )
    return AccountRecord(
        platform="kiro", email="a@x.ai", password="pw", token="rt-1", extra=extra
    )


def check() -> list[str]:
    from platforms.kiro.credentials import render_auth_tokens

    idc = _sample("idc", "BuilderId", "kid", "ksecret")
    social = _sample("social", "github", "", "")
    doc = json.loads(render_auth_tokens([idc, social]))
    entries = doc["accounts"]
    problems: list[str] = []

    if len(entries) != 2:
        return [f"expected 2 accounts, got {len(entries)}"]
    for index, entry in enumerate(entries):
        where = f"accounts[{index}]"
        if not str(entry.get("id") or "").strip():
            problems.append(f"{where}: 缺少 id（Agent2API 逐条要求非空 id）")
        if not str(entry.get("email") or "").strip():
            problems.append(f"{where}: 缺少 email（身份回退会退到 uid）")
        if "uid" in entry:
            problems.append(f"{where}: 不应导出 uid（会把身份压成同一个值）")
        if str(entry.get("loginProvider") or "") != str(entry.get("loginProvider") or "").lower():
            problems.append(f"{where}: loginProvider 必须小写（插件按 builderid 比较）")
        if not str(entry.get("accessToken") or "").strip():
            problems.append(f"{where}: 缺少 accessToken")
        if not str(entry.get("refreshToken") or "").strip():
            problems.append(f"{where}: 缺少 refreshToken")

    ids = [str(entry.get("id") or "") for entry in entries]
    if len(set(ids)) != len(ids):
        problems.append("同一邮箱的两个 provider 导出了相同 id，导入会互相覆盖")
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
