"""前端交付性能：压缩与缓存头。

背景（实测）：`/assets/index-*.js` 是单个 1.57 MB 的包，服务端**没有任何压缩**
（响应头无 `content-encoding`，`content-length` 就是原始大小），也没有
`cache-control`。同一份内容 gzip 后 477 KB（省 69.6%）—— 这是首屏最贵的一跳，
在慢网/移动网络上尤其明显。

这组测试钉住三件事：
1. 客户端声明支持 gzip 时，静态资源必须压缩下发（且 `content-length` 变小）；
2. 客户端不支持时，不能压缩（否则客户端解析不了）；
3. 带内容哈希的 `/assets/*` 必须有长缓存（文件名变即 URL 变，可以永久缓存）。
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "static", "assets")


def _asset_name() -> str:
    names = sorted(n for n in os.listdir(ASSETS) if n.endswith(".js"))
    assert names, f"static/assets 里没有 js 产物: {ASSETS}"
    return names[0]


@pytest.fixture(scope="module")
def client():
    sys.path.insert(0, ROOT)
    import main  # noqa: PLC0415 - 需要先设好环境变量

    return TestClient(main.app)


def test_asset_is_gzipped_when_client_accepts_it(client):
    name = _asset_name()
    raw = os.path.getsize(os.path.join(ASSETS, name))
    resp = client.get(f"/assets/{name}", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip", (
        "静态资源没有压缩 —— 1.57MB 的包会原样下发（实测基线）"
    )
    # 注意：httpx/TestClient 会自动解压，`resp.content` 是**解压后**的内容。
    # 真正省下的字节看 `content-length`（线上实际传输量）。
    sent = int(resp.headers.get("content-length") or 0)
    assert sent, "缺少 content-length，无法判断实际传输量"
    assert sent < raw * 0.5, (
        f"压缩后仍有 {sent} 字节（原始 {raw}），压缩没生效"
    )
    # 解压回来必须和原文件一致（不能压坏）
    assert len(resp.content) == raw, "解压后长度与原文件不一致 —— 内容被破坏了"


def test_asset_is_not_compressed_when_client_cannot_decode(client):
    name = _asset_name()
    resp = client.get(f"/assets/{name}", headers={"Accept-Encoding": "identity"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") in (None, "identity"), (
        "客户端不支持 gzip 时不能压缩，否则拿到的是乱码"
    )


def test_hashed_assets_are_cached_long(client):
    """`/assets/index-<hash>.js` 内容变了 URL 就变，可以长期缓存。"""
    name = _asset_name()
    resp = client.get(f"/assets/{name}")
    cc = (resp.headers.get("cache-control") or "").lower()
    assert "max-age" in cc, f"带哈希的静态资源应有 cache-control，实际: {cc!r}"
    assert "no-store" not in cc, f"带哈希的静态资源不该禁缓存，实际: {cc!r}"


def test_index_html_is_not_cached(client):
    """入口 HTML 必须每次校验 —— 否则前端发版后用户永远拿到旧资源名。"""
    resp = client.get("/")
    cc = (resp.headers.get("cache-control") or "").lower()
    assert "no-cache" in cc or "no-store" in cc or "max-age=0" in cc, (
        f"index.html 不能长缓存，实际: {cc!r}"
    )
