"""按 ASN 筛 Resin 出口：哪些是「住宅/ISP」，哪些是「数据中心」。

背景：x.ai 的注册（Cloudflare 后面）大概率拒数据中心 IP。
Resin 的 US 平台抽样里既有 Cloudflare/Datacamp/Hurricane 这类机房，
也有 Hargray(AS22646) 这种真 ISP —— 挑 ISP 再去试注册才有意义。
"""
import json
import subprocess
import sys
import time
import urllib.request

RESIN = "socks5h://US.{acct}:123456@127.0.0.1:2260"
#: 命中即判「数据中心/机房」（x.ai 最可能拒的就是这些）
DATACENTER_HINTS = (
    "cloudflare", "datacamp", "hurricane", "clouvider", "digitalocean", "vultr",
    "linode", "akamai", "amazon", "aws", "google", "microsoft", "azure", "ovh",
    "hetzner", "m247", "leaseweb", "choopa", "psychz", "colocrossing", "zenlayer",
    "alibaba", "tencent", "oracle", "cogent", "quadranet", "hostwinds", "contabo",
    "racknerd", "ionos", "hostinger", "godaddy", "namecheap", "datacamp limited",
    "gcore", "melbicom", "serverius", "server", "hosting", "datacenter", "data center",
    "colo", "cdn", "warp", "vpn", "proxy",
    # 视作机房（云厂商/骨干）
    "he.net", "he ", "frantech", "buyvm", "nexus", "multacom", "sharktech", "fdcservers",
)


def get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def proxy_ip(acct):
    try:
        out = subprocess.run(
            ["curl", "-sS", "--max-time", "12", "--proxy", RESIN.format(acct=acct),
             "https://api.ipify.org"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def ip_info(ip):
    try:
        d = get(f"http://ip-api.com/json/{ip}?fields=status,country,city,isp,org,as")
        if d.get("status") != "success":
            return {}
        return d
    except Exception:
        return {}


def looks_datacenter(info):
    blob = f"{info.get('isp','')} {info.get('org','')} {info.get('as','')}".lower()
    return any(h in blob for h in DATACENTER_HINTS)


rows = []
for n in range(1, 25):
    acct = f"User_4{n:02d}"
    ip = proxy_ip(acct)
    if not ip:
        continue
    info = ip_info(ip)
    dc = looks_datacenter(info)
    rows.append({"acct": acct, "ip": ip, "isp": info.get("isp", "?"),
                 "as": (info.get("as", "?") or "")[:40], "dc": dc})
    print(f"{acct} {ip:16} {'机房' if dc else 'ISP '} {info.get('isp','?')[:38]} | {info.get('as','?')[:28]}", flush=True)
    time.sleep(1.2)  # ip-api 免费档 45 次/分钟

isp = [r for r in rows if not r["dc"]]
print(f"\n合计 {len(rows)} 个：ISP/住宅 {len(isp)} 个，机房 {len(rows)-len(isp)} 个", flush=True)
print("ISP 候选:", json.dumps(isp, ensure_ascii=False), flush=True)
with open("/data/workspace/Register/tools/resin_isp_candidates.json", "w") as f:
    json.dump(isp, f, ensure_ascii=False, indent=2)
