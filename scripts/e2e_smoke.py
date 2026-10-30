"""黑盒功能冒烟：真启动 app，走一遍主要接口，输出 JSON 报告。

用途有两个：

1. **重构前后的行为对比**。在 base commit 的 worktree 上跑一遍、在重构后的
   worktree 上跑一遍，比较报告里的 `ok` / 状态码 / 关键字段 —— 这是「功能没变」
   最直接的证据，比单测更接近用户实际点的那几下。
2. **上线前的自检**。`python scripts/e2e_smoke.py` 返回非 0 就是有接口坏了。

只走**两个版本都存在**的端点，且不假设账号 id 的类型（重构后是 UUID、之前是
整数）—— 报告里统一按 `id_type` 记录，比较时忽略具体值。

用法::

    python scripts/e2e_smoke.py            # 打印 JSON
    python scripts/e2e_smoke.py --quiet    # 只打印结论
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: 必须在 import main 之前生效（`core.paths` 的常量是模块级求值）。
_TMP = tempfile.mkdtemp(prefix="e2e-smoke-")
os.environ["DATA_DIR"] = _TMP
#: 默认 sqlite；`--backend json` 会把 `E2E_BACKEND` 提前塞进来，两条后端都验一遍。
#: 后端必须在**模块导入时**定下来（`core.paths` / storage 的常量都是模块级求值），
#: 所以这里直接读 argv，不能等 argparse。
_BACKEND = "sqlite"
if "--backend" in sys.argv:
    _idx = sys.argv.index("--backend")
    if _idx + 1 < len(sys.argv):
        _BACKEND = sys.argv[_idx + 1]
os.environ["STORAGE_BACKEND"] = _BACKEND
os.environ["APP_TRUST_TEST_CLIENT"] = "1"
os.environ.pop("PLATFORM_DATABASE_URLS", None)


def _record(steps: list[dict], name: str, resp, **extra) -> dict:
    entry = {"step": name, "status": resp.status_code, "ok": resp.status_code < 400}
    entry.update(extra)
    steps.append(entry)
    return entry


async def _run(steps: list[dict]) -> None:
    import main
    from fastapi.testclient import TestClient

    async with main.lifespan(main.app):
        # 500 要记成一步失败，不能让整份报告崩掉
        client = TestClient(main.app, raise_server_exceptions=False)

        # 1. 配置读
        _record(steps, "config.get", client.get("/api/config"))

        # 2. 配置写（一个无害的键）
        _record(
            steps,
            "config.put",
            client.put("/api/config", json={"data": {"register_count": "2"}}),
        )

        # 3. 建代理
        r = client.post("/api/proxies", json={"url": "socks5h://u:p@127.0.0.1:1080", "region": "US"})
        _record(steps, "proxy.create", r)
        proxy_id = (r.json() or {}).get("id")
        _record(steps, "proxy.list", client.get("/api/proxies"))

        # 4. 建账号
        r = client.post(
            "/api/accounts",
            json={
                "platform": "chatgpt",
                "email": "smoke@example.com",
                "password": "Pw-123",
                "token": "",
            },
        )
        created = r.json() if r.status_code < 400 else {}
        account_id = created.get("id")
        _record(
            steps,
            "account.create",
            r,
            id_type=type(account_id).__name__,
            has_password=bool(created.get("password") or created.get("has_password")),
        )

        # 5. 账号列表
        r = client.get("/api/accounts?page_size=50")
        page = r.json() if r.status_code < 400 else {}
        items = page.get("items") or []
        _record(
            steps,
            "account.list",
            r,
            total=page.get("total"),
            contains_smoke=any(i.get("email") == "smoke@example.com" for i in items),
            leaks_password=any("password" in i for i in items),
            has_password_flag=all("has_password" in i for i in items) if items else None,
        )

        # 6. 账号详情
        if account_id is not None:
            r = client.get(f"/api/accounts/{account_id}")
            detail = r.json() if r.status_code < 400 else {}
            _record(
                steps,
                "account.detail",
                r,
                email_ok=detail.get("email") == "smoke@example.com",
                password_ok=detail.get("password") == "Pw-123",
            )

            # 7. 账号改
            r = client.patch(
                f"/api/accounts/{account_id}",
                json={"status": "expired", "password": "Pw-456"},
            )
            _record(steps, "account.patch", r)

            # 8. 动作清单（两个版本都有；重构后多一个 capability 字段）
            r = client.get("/api/actions/chatgpt")
            actions = (r.json() or {}).get("actions") or []
            _record(
                steps,
                "actions.list",
                r,
                count=len(actions),
                has_capability=all("capability" in a for a in actions) if actions else None,
            )

            # 9. 导出（纯文本，不落盘）
            r = client.post(
                "/api/accounts/export-text",
                json={"account_ids": [account_id], "format": "email_pw"},
            )
            _record(
                steps,
                "account.export",
                r,
                has_smoke="smoke@example.com" in (r.text or ""),
            )

            # 10. 删账号
            _record(steps, "account.delete", client.delete(f"/api/accounts/{account_id}"))

        # 11. 任务列表（空也该 200）
        _record(steps, "tasks.list", client.get("/api/tasks"))

        # 12. 统计
        r = client.get("/api/accounts/stats")
        _record(steps, "account.stats", r, total=(r.json() or {}).get("total"))

        # 13. 面板清单（保留 4 面板是既定决策）
        _record(steps, "panels.list", client.get("/api/integrations/panels"))

        if proxy_id is not None:
            _record(steps, "proxy.delete", client.delete(f"/api/proxies/{proxy_id}"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--backend", choices=("sqlite", "json"), default=None,
        help="存储后端（默认 sqlite；json 走另一条写路径）",
    )
    args = parser.parse_args(argv)

    steps: list[dict] = []
    asyncio.run(_run(steps))

    failed = [s for s in steps if not s["ok"]]
    report = {
        "data_dir": _TMP,
        "backend": os.environ.get("STORAGE_BACKEND"),
        "steps": steps,
        "total": len(steps),
        "failed": len(failed),
        "ok": not failed,
    }
    if args.quiet:
        print(f"{len(steps) - len(failed)}/{len(steps)} 步通过")
        for item in failed:
            print(f"  FAIL {item['step']} -> {item['status']}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
