"""清理 Grok 账号里已被废弃的 CPA / Sub2API 凭据字段。

用户要求（2026-10-27）：「Grok 数据结构优化调整，删除 AT/RT 这种（不再支持
CPA，只支持 grok2api）」。

## 删什么、留什么

删（只为 CPA / Sub2API 上传而存在）：
- `cpa_record`：CLIProxyAPI 扁平 xai auth 记录（AT/RT/expired/id_token 的容器）
- `access_token` / `refresh_token` / `id_token`：OAuth 交换产物

留：
- `sso`：**grok2api 的主凭证**，也是 SSO 续期路径的输入
- `totp_secret` / `sync_statuses` / 其它一切

## 为什么不会把号删废

Grok 有两条刷新路径（`platforms/grok/token_refresh.py`）：
- `refresh_via_grant`：RT → 新 token（需要 RT）
- `refresh_via_device_flow`：**SSO → 登录协议**（不需要 RT）

删掉 RT 后走第二条。所以清掉这些字段不会让账号失去续期能力。

## 幂等

重复运行不会报告变更 —— 迁移要能安全重跑（服务启动时每次都调）。
"""

from __future__ import annotations

from typing import Any

#: 只为 CPA / Sub2API 上传而存在的字段。
DEAD_EXTRA_KEYS: tuple[str, ...] = (
    "cpa_record",
    "access_token",
    "refresh_token",
    "id_token",
    # 同一批 OAuth 产物的附属字段（CPA 记录里用得到，grok2api 用不到）
    "expired",
    "last_refresh",
)


def clean_grok_extra(extra: Any) -> tuple[dict[str, Any], bool]:
    """返回 `(清理后的 extra, 是否有变更)`。

    非 dict 输入原样当空表处理（脏数据不该让启动流程炸）。
    """
    if not isinstance(extra, dict):
        return {}, False

    cleaned = dict(extra)
    changed = False
    for key in DEAD_EXTRA_KEYS:
        if key in cleaned:
            cleaned.pop(key, None)
            changed = True
    return cleaned, changed


__all__ = ["DEAD_EXTRA_KEYS", "clean_grok_extra"]


def migrate_stored_accounts(log=None) -> int:
    """把库里所有 Grok 账号的废弃字段清掉。返回处理条数。

    在 `main.py` 的 lifespan 里调用（与 2FA 密钥补写同一处）。幂等：第二次
    运行没有变更就不写库、不打印。

    只处理 `extra`，不动 `token` 列（那列镜像的是 SSO，要保留）。
    """
    from sqlmodel import select

    from core.db import AccountModel, platform_session

    changed_count = 0
    with platform_session("grok") as session:
        rows = session.exec(
            select(AccountModel).where(AccountModel.platform == "grok")
        ).all()
        for row in rows:
            extra = row.get_extra()
            cleaned, changed = clean_grok_extra(extra)
            if not changed:
                continue
            row.set_extra(cleaned)
            session.add(row)
            changed_count += 1
        if changed_count:
            session.commit()

    if changed_count and log:
        log(f"[OK] 已清理 {changed_count} 个 Grok 账号的废弃 CPA/Sub2API 字段")
    return changed_count
