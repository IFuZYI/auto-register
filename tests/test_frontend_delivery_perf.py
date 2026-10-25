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


#: `static/` 是构建产物、被 gitignore（`.gitignore` 的 `static/` 行）。
#: 全新 clone 上没跑过 `npm run build` 时这些用例没有前提 —— **跳过**，
#: 而不是报 FileNotFoundError 让 CI/新人以为代码坏了（与
#: `tests/test_api_integration.py` 的 SPA 兜底用例同一口径）。
if not os.path.isdir(ASSETS):
    pytest.skip("static/assets 未构建（先 npm run build），跳过交付性能用例",
                allow_module_level=True)


def _asset_name() -> str:
    names = sorted(n for n in os.listdir(ASSETS) if n.endswith(".js"))
    if not names:
        pytest.skip(f"static/assets 里没有 js 产物: {ASSETS}")
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


def test_conditional_request_returns_304(client):
    """`If-None-Match` 命中时必须 304 —— 否则每次访问都重传整个包。

    实测缺陷：自定义响应手写了 ETag 但没走条件请求逻辑，客户端回传
    `If-None-Match` 仍得到 200 + 完整内容，长缓存形同虚设。
    """
    name = _asset_name()
    first = client.get(f"/assets/{name}", headers={"Accept-Encoding": "gzip"})
    etag = first.headers.get("etag")
    assert etag, "没有 ETag，无法做条件请求"
    second = client.get(
        f"/assets/{name}",
        headers={"Accept-Encoding": "gzip", "If-None-Match": etag},
    )
    assert second.status_code == 304, (
        f"If-None-Match 命中却返回 {second.status_code} —— 每次都重传整个包"
    )


def test_precompression_cache_is_built_once_at_startup(tmp_path):
    """预压缩缓存是**启动时一次构建**的：构建产物在进程运行期间被替换时，
    缓存仍持有旧字节，新文件名则完全没有 gzip 变体。

    这是**已知取舍**（不在每个请求上压是为了省 CPU），不是缺陷 —— 但它是
    真实属性，必须写下来：实测踩到过「测试跑到一半重建前端」导致 6 条 gzip
    用例红。DN 的部署流程是「构建 → 重启服务」，正好避开这个窗口。

    这条测试钉住「缓存不会自己失效」，以免有人误以为改了文件就自动生效。
    """
    import main as main_mod

    (tmp_path / "a.js").write_bytes(b"var a=1;\n" * 400)
    store = main_mod._GzipStatic(directory=str(tmp_path))
    self_initial = list(store._gzip_cache)
    assert "a.js" in self_initial, "首次构建应缓存 a.js"

    # 进程运行期间替换同名文件：缓存不会更新
    (tmp_path / "a.js").write_bytes(b"var b=2;\n" * 400)
    assert list(store._gzip_cache) == self_initial, (
        "缓存应当保持不变（这是取舍，不是失效 bug）—— 若这里变了，"
        "说明有人加了自动失效，需要同步更新文档与部署约定"
    )

    # 新文件也不会被自动收录
    (tmp_path / "b.js").write_bytes(b"var c=3;\n" * 400)
    assert "b.js" not in store._gzip_cache, "新增文件不会被自动预压"


def test_range_request_is_not_served_gzip(client):
    """`Range` 请求不能带 gzip —— 字节区间是按**原始**文件算的。

    实测缺陷：`Range: bytes=0-99` 返回 206 但内容是未压缩字节，同时头里写着
    `content-encoding: gzip` —— 客户端会把原始字节当 gzip 解，直接报错。
    """
    name = _asset_name()
    resp = client.get(
        f"/assets/{name}",
        headers={"Accept-Encoding": "gzip", "Range": "bytes=0-99"},
    )
    assert resp.status_code == 206
    encoding = (resp.headers.get("content-encoding") or "").lower()
    assert encoding in ("", "identity"), (
        f"Range 请求被 gzip 了（content-encoding={encoding!r}）—— 字节区间会错位"
    )


@pytest.mark.parametrize("header,expect_gzip", [
    ("gzip", True),
    ("gzip, deflate, br", True),
    ("deflate, gzip;q=0.5", True),
    ("gzip;q=0", False),          # 明确拒绝
    ("*", True),                   # 什么都行 → 含 gzip
    ("*;q=0", False),
    ("identity", False),
    ("br", False),
    ("GZIP", True),                # 大小写
    ("", False),
])
def test_accept_encoding_parsing(client, header, expect_gzip):
    """`Accept-Encoding` 必须正确解析 —— `gzip;q=0` 是「不要 gzip」，`*` 是「都行」。

    朴素子串匹配（`"gzip" in header`）会把 `gzip;q=0` 判成要压缩，
    然后给一个明确拒绝 gzip 的客户端发压缩体。
    """
    from main import _accepts_gzip

    assert _accepts_gzip(header) is expect_gzip, (
        f"Accept-Encoding: {header!r} 判错了"
    )

    name = _asset_name()
    resp = client.get(f"/assets/{name}", headers={"Accept-Encoding": header})
    assert resp.status_code == 200
    got = (resp.headers.get("content-encoding") or "").lower() == "gzip"
    assert got is expect_gzip, (
        f"Accept-Encoding: {header!r} 实际下发 gzip={got}，期望 {expect_gzip}"
    )


def test_both_variants_advertise_vary(client):
    """`Vary: Accept-Encoding` 必须两个变体都带。

    实测缺陷：只标在 gzip 分支上，而 identity 分支（curl、健康检查、
    不支持 gzip 的客户端都会命中）没有变体标记 —— 共享缓存可能把原始体
    发给支持 gzip 的客户端，反之亦然。
    """
    name = _asset_name()
    for enc in ("gzip", "identity"):
        resp = client.get(f"/assets/{name}", headers={"Accept-Encoding": enc})
        vary = (resp.headers.get("vary") or "").lower()
        assert "accept-encoding" in vary, (
            f"{enc} 变体缺 Vary: Accept-Encoding（实际 {vary!r}）"
        )


def test_index_html_supports_conditional_get(client):
    """入口 HTML 要能 304 —— 否则 `no-cache` 退化成每次都传整页。"""
    first = client.get("/")
    etag = first.headers.get("etag")
    assert etag, "index.html 没有 ETag"
    for path in ("/", "/accounts"):
        resp = client.get(path, headers={"If-None-Match": etag})
        assert resp.status_code == 304, (
            f"{path} 回传正确 ETag 却得到 {resp.status_code}"
        )


def test_already_compressed_formats_are_skipped(tmp_path):
    """png/woff2 这类不该再压 —— 只烧 CPU 与内存。"""
    import gzip
    import main as main_mod

    src = tmp_path / "a.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 5000)
    js = tmp_path / "a.js"
    js.write_bytes(b"var x=1;\n" * 500)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.js").write_bytes(b"var y=2;\n" * 500)

    store = main_mod._GzipStatic(directory=str(tmp_path))
    assert "a.png" not in store._gzip_cache, "png 不该被 gzip"
    assert "a.js" in store._gzip_cache
    # 子目录也要压（只扫一层会漏掉）
    assert "sub/b.js" in store._gzip_cache, (
        f"子目录文件没被压缩，cache keys={list(store._gzip_cache)}"
    )
    # key 是相对路径 —— basename 会让 assets/x.js 与 assets/sub/x.js 串味
    assert gzip.decompress(store._gzip_cache["sub/b.js"]).startswith(b"var y=2;")
