"""看 Resin 的 `Default` 平台里有没有非机房（真 ISP）节点。

US 平台实测全是机房（Cloudflare WARP / Hurricane Electric / Datacamp /
Clouvider / GTHost / PEG TECH / HOSTKEY），而 x.ai 只放行 ISP 型出口
（我们自己的出口是 AS11404 Wave Broadband，能成功注册）。
"""
import json
import subprocess
import time
import urllib.request

RESIN = "socks5h://{plat}.{acct}:123456@127.0.0.1:2260"
HOSTING = (
    "cloudflare", "datacamp", "hurricane", "clouvider", "gthost", "globaltelehost",
    "peg tech", "hostkey", "egihosting", "digitalocean", "vultr", "linode", "akamai",
    "amazon", "aws", "google", "microsoft", "azure", "ovh", "hetzner", "m247",
    "leaseweb", "choopa", "psychz", "colocrossing", "zenlayer", "alibaba", "tencent",
    "oracle", "cogent", "quadranet", "hostwinds", "contabo", "racknerd", "ionos",
    "hostinger", "gcore", "melbicom", "serverius", "hosting", "datacenter",
    "data center", "colo", " vps", "cloud", "server", "warp", "vpn", "proxy",
    "frantech", "buyvm", "multacom", "sharktech", "fdcservers", "he.net",
)


def get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def proxy_ip(plat, acct):
    try:
        out = subprocess.run(
            ["curl", "-sS", "--max-time", "12", "--proxy", RESIN.format(plat=plat, acct=acct),
             "https://api.ipify.org"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def ip_info(ip):
    try:
        return get(f"http://ip-api.com/json/{ip}?fields=status,country,city,isp,org,as")
    except Exception:
        return {}


def looks_hosting(info):
    blob = f"{info.get('isp','')} {info.get('org','')} {info.get('as','')}".lower()
    return any(h in blob for h in HOSTING)


found_isp = []
for n in range(1, 17):
    acct = f"User_5{n:02d}"
    ip = proxy_ip("Default", acct)
    if not ip:
        print(f"Default.{acct} <no ip>", flush=True)
        continue
    info = ip_info(ip)
    host = looks_hosting(info)
    tag = "机房" if host else "★非机房"
    print(f"Default.{acct} {ip:16} {tag} {info.get('isp','?')[:34]} | {info.get('as','?')[:26]}", flush=True)
    if not host:
        found_isp.append({"plat": "Default", "acct": acct, "ip": ip,
                          "isp": info.get("isp", ""), "as": info.get("as", "")})
    time.sleep(1.2)

print(f"\n非机房节点 {len(found_isp)} 个", flush=True)
print(json.dumps(found_isp, ensure_ascii=False, indent=2), flush=True)
with open("/data/workspace/Register/tools/resin_default_isp.json", "w") as f:
    json.dump(found_isp, f, ensure_ascii=False, indent=2)
