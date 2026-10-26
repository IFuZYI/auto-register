"""备份导入必须有硬性字节上限，且不依赖 `Content-Length`。

安全审查（2026-10-27）F4：`api/backup.py` 的大小预检是

    declared = request.headers.get("content-length", "").strip()
    if declared.isdigit() and int(declared) > MAX_BUNDLE_BYTES: ...

`chunked` 编码的请求**没有 `Content-Length`**，`declared.isdigit()` 为假 →
预检整段跳过 → `await request.body()` 把任意大小的体攒进内存。本应用是
**单进程单 worker**，一次这样的请求就能 OOM 掉整个面板（拒绝服务）。

同类问题：`services/data_bundle.py` 里 `secrets/credential_key` 成员用
`zf.read()` 读取且**没有大小检查**（`MAX_DB_BYTES` 只作用于 manifest 里
列出的库），成员数量也没有上限 —— 一个高压缩比的小 ZIP 解压后可以撑爆内存。
"""

from __future__ import annotations

import unittest


class ChunkedBodyIsRejectedTests(unittest.TestCase):
    """没有 `Content-Length` 的请求要么被流式限流，要么直接拒。"""

    def test_import_without_content_length_is_rejected(self):
        from fastapi.testclient import TestClient

        import main

        client = TestClient(main.app)

        def _gen():
            # 模拟 chunked：不设 Content-Length，边生成边发
            yield b"PK\x03\x04" + b"\x00" * 1024

        r = client.post(
            "/api/backup/import",
            content=_gen(),
            headers={"Content-Type": "application/zip"},
        )
        # 明确拒绝（411/413）或正常处理小包都可以；**不允许**无上限地读
        self.assertIn(
            r.status_code, (400, 411, 413, 422),
            f"无 Content-Length 的导入没有被拒: {r.status_code}",
        )


class KeyMemberIsSizeCheckedTests(unittest.TestCase):
    def test_key_member_read_is_bounded(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "services" / "data_bundle.py").read_text(
            encoding="utf-8"
        )
        # 读取 key 成员处必须带大小检查（不能裸 zf.read）
        import re

        for m in re.finditer(r"zf\.read\(([^)]*)\)", src):
            snippet = src[max(0, m.start() - 400) : m.end() + 100]
            self.assertTrue(
                ("MAX_DB_BYTES" in snippet) or ("getinfo" in snippet) or ("file_size" in snippet),
                f"`zf.read({m.group(1)})` 附近没有大小检查（{src[:m.start()].count(chr(10)) + 1} 行）",
            )


class MemberCountIsBoundedTests(unittest.TestCase):
    def test_member_count_has_a_cap(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "services" / "data_bundle.py").read_text(
            encoding="utf-8"
        )
        self.assertTrue(
            "MAX_MEMBERS" in src or "MAX_BUNDLE_MEMBERS" in src,
            "备份包成员数量没有上限 —— 一个几万成员的小 ZIP 就能拖垮解压",
        )


if __name__ == "__main__":
    unittest.main()
