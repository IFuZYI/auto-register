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


def _sample(
    auth_method: str,
    login_provider: str,
    client_id: str,
    client_secret: str,
    aws_extra: dict | None = None,
):
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
    extra.update(aws_extra or {})
    return AccountRecord(
        platform="kiro", email="a@x.ai", password="pw", token="rt-1", extra=extra
    )


def check() -> list[str]:
    from platforms.kiro.credentials import render_auth_tokens

    idc = _sample(
        "idc",
        "BuilderId",
        "kiro-cid",
        "kiro-csecret",
        {
            "kiro_aws_client_id": "aws-cid",
            "kiro_aws_client_secret": "aws-csecret",
            "kiro_aws_refresh_token": "aws-rt",
            "kiro_aws_access_token": "aws-at",
            "kiro_aws_region": "us-east-1",
        },
    )
    social = _sample("social", "github", "", "")
    doc = json.loads(render_auth_tokens([idc, social]))
    entries = doc["accounts"]
    problems: list[str] = []

    if len(entries) != 2:
        problems.append(f"expected 2 accounts, got {len(entries)}")
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
        method = entry.get("method")
        if method not in ("idc", "social"):
            # 少了 method，下面两条分支都不进 —— 整份文件会被判成「没问题」，
            # 而 Agent2API 按 method 选刷新路径，等于把错误推迟到运行时。
            problems.append(f"{where}: method 必须是 idc/social")
        if method == "idc":
            if not str(entry.get("expiresAt") or "").startswith("1970-"):
                problems.append(f"{where}: idc 的 expiresAt 应强制刷新（1970…）")
            if entry.get("loginProvider") != "builderid":
                problems.append(f"{where}: idc 的 loginProvider 必须是 builderid")
            # Agent2API 的 idc 刷新走 AWS SSO OIDC：必须是 AWS 的 client + RT，
            # 不是 Kiro IDE 的 OAuth 客户端（否则 invalid_client，额度/对话全挂）。
            if entry.get("clientId") != "aws-cid":
                problems.append(f"{where}: idc 的 clientId 必须是 AWS SSO 客户端")
            if entry.get("refreshToken") != "aws-rt":
                problems.append(f"{where}: idc 的 refreshToken 必须是 AWS SSO 的 RT")
            if int(entry.get("expires") or 0) > 10**12:
                problems.append(f"{where}: idc 必须强制刷新（AWS SSO AT 过期时间未知）")
            if str(entry.get("profileArn") or "").strip():
                problems.append(f"{where}: idc 的 profileArn 应为空，交给插件填 Builder ID profile")
            if not str(entry.get("clientSecret") or "").strip():
                problems.append(f"{where}: 缺少 clientSecret")
        elif method == "social":
            if entry.get("loginProvider") not in ("github", "google"):
                problems.append(f"{where}: social 的 loginProvider 应是 github/google")

    ids = [str(entry.get("id") or "") for entry in entries]
    if len(set(ids)) != len(ids):
        problems.append("同一邮箱的两个 provider 导出了相同 id，导入会互相覆盖")

    # 缺少 AWS 快照的 idc 必须报错，而不是导出一份「看起来成功」的文件。
    broken = _sample("idc", "BuilderId", "kid", "ksecret")
    try:
        render_auth_tokens([broken])
    except ValueError:
        pass
    else:
        problems.append("缺少 AWS 快照的 idc 没有报错，会导出 invalid_client 文件")
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
