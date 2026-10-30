"""把**存量**明文凭据就地加密（T1.4 的「全量」那一步）。

新代码是「写的时候加密、读的时候解密」，但升级前已经在库里的行不会自己变。
这个脚本补那一步，三块一起做：

1. `accounts.password` —— 明文 → `v1:` 信封
2. `accounts.extra_json` 里六把凭证（AT/RT/IDT/ST/SSO/totp_secret）
3. `configs` 表里 10 个口令键（`yescaptcha_key` / `sub2api_api_key` …）
4. `data/secrets/totp_journal/*.json` 里老格式的 `totp_secret`（明文）

三条硬要求：

- **幂等**：已经是 `v1:` 的原样跳过，跑几遍结果一样。
- **不动 `updated_at`**：走裸 SQL，不经 ORM —— 否则「按更新时间排序」的
  界面会以为所有账号刚刚都变过。
- **可预演**：`--dry-run` 只统计不写库。

用法：

    python scripts/encrypt_existing_credentials.py --dry-run
    python scripts/encrypt_existing_credentials.py
    python scripts/encrypt_existing_credentials.py --verify   # 只查还有多少明文
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _accounts(dry_run: bool) -> dict:
    from sqlalchemy import text

    from core.credential_crypto import encrypt_extra, encrypt_text, is_ciphertext
    from core.db import current_engine

    stats = {
        "rows": 0,
        "password": 0,
        "token": 0,
        "extra_fields": 0,
        "already": 0,
        "changed_rows": 0,
    }
    engine = current_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, password, token, extra_json FROM accounts")
        ).fetchall()
        for row in rows:
            stats["rows"] += 1
            account_id, password, token, extra_json = (
                row[0], row[1] or "", row[2] or "", row[3] or "{}"
            )
            new_password = encrypt_text(password)
            # `token` 列存的是 chatgpt 的 AT / grok 的 SSO —— Q3 点名的凭据之一
            new_token = encrypt_text(token)
            try:
                extra = json.loads(extra_json) if extra_json.strip() else {}
            except json.JSONDecodeError:
                extra = {}
            new_extra = encrypt_extra(extra)

            password_changed = new_password != password
            token_changed = new_token != token
            extra_changed = new_extra != extra
            if password_changed:
                stats["password"] += 1
            if token_changed:
                stats["token"] += 1
            if extra_changed:
                stats["extra_fields"] += sum(
                    1
                    for key, value in new_extra.items()
                    if extra.get(key) != value and is_ciphertext(value)
                )
            if not (password_changed or token_changed or extra_changed):
                stats["already"] += 1
                continue
            stats["changed_rows"] += 1
            if not dry_run:
                conn.execute(
                    text(
                        "UPDATE accounts SET password = :p, token = :t, extra_json = :e "
                        "WHERE id = :i"
                    ),
                    {
                        "p": new_password,
                        "t": new_token,
                        "e": json.dumps(new_extra, ensure_ascii=False),
                        "i": account_id,
                    },
                )
    return stats


def _configs(dry_run: bool) -> dict:
    from sqlalchemy import text

    from core.config_schema import SECRET_CONFIG_KEYS
    from core.credential_crypto import encrypt_text, is_ciphertext
    from core.db import current_engine

    stats = {"rows": 0, "encrypted": 0, "already": 0, "empty": 0}
    engine = current_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT key, value FROM configs")).fetchall()
        for key, value in rows:
            if key not in SECRET_CONFIG_KEYS:
                continue
            stats["rows"] += 1
            value = value or ""
            if not value.strip():
                stats["empty"] += 1
                continue
            if is_ciphertext(value):
                stats["already"] += 1
                continue
            stats["encrypted"] += 1
            if not dry_run:
                conn.execute(
                    text("UPDATE configs SET value = :v WHERE key = :k"),
                    {"v": encrypt_text(value), "k": key},
                )
    return stats


def _journal(dry_run: bool) -> dict:
    from core.credential_crypto import encrypt_text
    from core.paths import DATA_DIR

    stats = {"files": 0, "encrypted": 0, "already": 0, "unreadable": 0}
    directory = Path(DATA_DIR) / "secrets" / "totp_journal"
    if not directory.exists():
        return stats
    for path in sorted(directory.glob("*.json")):
        stats["files"] += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stats["unreadable"] += 1
            continue
        if payload.get("totp_secret_cipher"):
            stats["already"] += 1
            continue
        secret = str(payload.get("totp_secret") or "").strip()
        if not secret:
            stats["already"] += 1
            continue
        stats["encrypted"] += 1
        if dry_run:
            continue
        # 键名换成 `totp_secret_cipher`：老读侧会把这个字段当明文直接写库，
        # 复用键名等于让旧进程把密文当密钥写进去（2FA 直接废掉）。
        new_payload = dict(payload)
        new_payload.pop("totp_secret", None)
        new_payload["totp_secret_cipher"] = encrypt_text(secret)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(new_payload, ensure_ascii=False), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
    return stats


def _verify() -> int:
    from sqlalchemy import text

    from core.config_schema import SECRET_CONFIG_KEYS
    from core.credential_crypto import SENSITIVE_EXTRA_KEYS, is_ciphertext
    from core.db import current_engine

    plaintext_accounts = 0
    plaintext_tokens = 0
    plaintext_extra = 0
    with current_engine().connect() as conn:
        for _id, password, token, extra_json in conn.execute(
            text("SELECT id, password, token, extra_json FROM accounts")
        ).fetchall():
            if password and not is_ciphertext(password):
                plaintext_accounts += 1
            if token and not is_ciphertext(token):
                plaintext_tokens += 1
            try:
                extra = json.loads(extra_json or "{}")
            except json.JSONDecodeError:
                continue
            # 空值不算「未加密」：没有 RT 的账号本来就该是空串，
            # `encrypt_text("")` 也返回空串（加密空串没有意义，还会让
            # `has_password` 这类判空逻辑全部失真）。
            plaintext_extra += sum(
                1
                for key in SENSITIVE_EXTRA_KEYS & extra.keys()
                if str(extra[key] or "").strip() and not is_ciphertext(extra[key])
            )
        plaintext_configs = sum(
            1
            for key, value in conn.execute(text("SELECT key, value FROM configs")).fetchall()
            if key in SECRET_CONFIG_KEYS and (value or "").strip() and not is_ciphertext(value)
        )

    print(
        f"剩余明文：账号密码 {plaintext_accounts} 行 / AT·SSO {plaintext_tokens} 行 / "
        f"extra 凭证 {plaintext_extra} 个 / 配置口令 {plaintext_configs} 个"
    )
    return (
        0
        if (plaintext_accounts or plaintext_tokens or plaintext_extra or plaintext_configs) == 0
        else 1
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="存量凭据就地加密（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    parser.add_argument("--verify", action="store_true", help="只检查还有多少明文")
    args = parser.parse_args(argv)

    from core.credential_crypto import enabled

    if args.verify:
        return _verify()

    if not enabled():
        print("CREDENTIAL_ENCRYPTION 被显式关掉了，拒绝执行（会写进一堆不该写的密文）")
        return 2

    accounts = _accounts(args.dry_run)
    configs = _configs(args.dry_run)
    journal = _journal(args.dry_run)

    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}账号：{accounts['rows']} 行，密码待加密 {accounts['password']}，"
          f"AT/SSO 待加密 {accounts['token']}，"
          f"extra 凭证 {accounts['extra_fields']} 个，已是密文 {accounts['already']} 行")
    print(f"{prefix}配置：口令键 {configs['rows']} 个，待加密 {configs['encrypted']}，"
          f"已是密文 {configs['already']}，空值 {configs['empty']}")
    print(f"{prefix}2FA 日志：{journal['files']} 个文件，待加密 {journal['encrypted']}，"
          f"已是密文 {journal['already']}，读不了 {journal['unreadable']}")

    if args.dry_run:
        return 0
    return _verify()


if __name__ == "__main__":
    raise SystemExit(main())
