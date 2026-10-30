"""把分库的 `accounts` 合并进单一库（T1.3 的 CLI）。

用法::

    # 先看会发生什么（不写库）
    python scripts/merge_platform_dbs.py --target data/account_manager.db --dry-run

    # 真合并（自动发现 data/platforms/*.db）
    python scripts/merge_platform_dbs.py --target data/account_manager.db

    # 指定源库
    python scripts/merge_platform_dbs.py --target data/account_manager.db \\
        --source data/platforms/chatgpt.db data/platforms/grok.db

合并完成后把 `PLATFORM_DATABASE_URLS` / `DATABASE_URL_<PLATFORM>` 清掉，
服务才会全部走默认库。**不要先清配置再合并** —— 那会让服务读到一个空库。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.db.merge import merge_accounts  # noqa: E402


def _discover(data_dir: Path) -> list[Path]:
    """找出 `data/platforms/*.db` 里真的有 accounts 行的文件。"""
    found: list[Path] = []
    for path in sorted((data_dir / "platforms").glob("*.db")):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "select name from sqlite_master where type='table' and name='accounts'"
                ).fetchone()
                if not row:
                    continue
                count = conn.execute("select count(*) from accounts").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            continue
        if count:
            found.append(path)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="合并平台分库的 accounts 到单一库")
    parser.add_argument("--target", required=True, help="目标库（默认库）路径")
    parser.add_argument("--source", nargs="*", default=None, help="源库路径（默认自动发现）")
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data"), help="自动发现的根目录")
    parser.add_argument("--dry-run", action="store_true", help="只算不写")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)

    target = Path(args.target)
    sources = [Path(p) for p in args.source] if args.source else _discover(Path(args.data_dir))
    if not sources:
        print("没有发现需要合并的源库（data/platforms/*.db 里没有 accounts 行）")
        return 0

    report = merge_accounts(target, sources, dry_run=args.dry_run)
    payload = report.as_dict()

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    mode = "【DRY-RUN，未写库】" if report.dry_run else "【已合并】"
    print(f"{mode} 目标: {report.target}")
    print(f"  合并前 {report.target_before} 行 → 合并后 {report.target_after} 行")
    for item in report.sources:
        print(
            f"  {item.path}: {item.total} 行"
            f" → 新增 {item.inserted} / 更新 {item.updated}"
            f" / 较旧跳过 {item.skipped_older} / 源内重复 {item.duplicate_in_source}"
            f" / 换 id {item.id_reassigned}"
        )
    print(
        f"  合计: 源 {report.source_total} 行 ="
        f" 新增 {report.inserted} + 更新 {report.updated}"
        f" + 跳过 {report.skipped_older}"
    )
    if report.dry_run:
        print("\n确认无误后去掉 --dry-run 再跑一次。")
    else:
        print(
            "\n下一步：清掉 PLATFORM_DATABASE_URLS / DATABASE_URL_<PLATFORM>，"
            "重启服务，确认账号列表条数与上面「合并后」一致。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
