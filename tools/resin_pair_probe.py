"""配对实验：Resin 出口 vs 已知可用出口 #6，交替各跑一次。

变量只有「出口」一个：域名/邮箱渠道、并发、重试次数全相同。
只要 #6 成功而 Resin 失败 → Resin 的 IP 在这台机器上是劣势；
两者同成败 → 是 x.ai 的全局窗口，跟出口无关。
"""
import json
import subprocess
import time
import urllib.request

API = "http://127.0.0.1:8000"
RESIN_ACCOUNTS = ["User_201", "User_202", "User_203", "User_204", "User_205", "User_206"]
EXIT6 = "socks5h://gobngawr:06pk6as8qlwu@9.142.37.16:5187"


def post(path, payload):
    req = urllib.request.Request(
        API + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(API + path, timeout=30) as r:
        return json.loads(r.read())


def exit_ip(proxy):
    try:
        out = subprocess.run(
            ["curl", "-sS", "--max-time", "12", "--proxy", proxy, "https://api.ipify.org"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip() or "?"
    except Exception:
        return "?"


def attempt(label, proxy):
    tid = post("/api/tasks/register", {
        "platform": "grok", "count": 1, "concurrency": 1,
        "register_retry_times": 0, "proxy": proxy,
    })["task_id"]
    deadline = time.time() + 300
    d = {}
    while time.time() < deadline:
        time.sleep(12)
        d = get(f"/api/tasks/{tid}?include_logs=1")
        if d.get("status") in ("done", "failed", "stopped"):
            break
    logs = [str(x) for x in (d.get("logs") or [])]
    ok = bool([l for l in logs if "[OK]" in l])
    note = ""
    for line in logs:
        if "发码结局" in line:
            note = "email_unavailable"
        if "资料页未通过" in line:
            note = "资料页 Turnstile"
        if "发码已提交" in line and not note:
            note = ""
    print(f"{label:14} ip={exit_ip(proxy):16} {'成功' if ok else '失败'} {note}", flush=True)
    return ok


rows = []
for acct in RESIN_ACCOUNTS:
    rp = f"socks5h://US.{acct}:123456@127.0.0.1:2260"
    rows.append(("Resin." + acct, rp, attempt("Resin." + acct, rp)))
    rows.append(("#6 对照", EXIT6, attempt("#6 对照", EXIT6)))

print("\n=== 汇总（出口 vs 结果）===", flush=True)
resin_ok = sum(1 for n, _, ok in rows if n.startswith("Resin") and ok)
resin_n = sum(1 for n, _, _ in rows if n.startswith("Resin"))
ctrl_ok = sum(1 for n, _, ok in rows if n.startswith("#6") and ok)
ctrl_n = sum(1 for n, _, _ in rows if n.startswith("#6"))
print(f"Resin 成功 {resin_ok}/{resin_n}", flush=True)
print(f"#6  成功 {ctrl_ok}/{ctrl_n}", flush=True)
