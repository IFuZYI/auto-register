"""集成测试：通过真实 HTTP 打后端 API（不是 mock 内部函数）。

与单元测试的区别：这里走 FastAPI 的 ASGI 栈，验证路由、依赖注入、
序列化、错误码这一整条链路。用 TestClient（in-process，不需要起服务）。

覆盖本轮改动直接影响的 API 面：
  - /api/config          读写配置（新增 grok_register_mode 白名单）
  - /api/proxies         代理列表（本轮修过代理链路）
  - /api/tasks/register  建注册任务（本轮改成浏览器路径）
  - /api/tasks           任务列表（日志/状态字段形状）
  - /api/accounts        账号列表（分页形状）
  - /api/icloud/*        iCloud 号池（本轮修过记账）
"""
from __future__ import annotations

import unittest
from pathlib import Path

# 用相对仓库根而不是硬编码 /home/Register：后者在任何别的 checkout 或 CI
# 上都不成立（评审发现）。parents[1] 即 tests/ 的上一级 = 项目根。
sys_root = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

if str(sys_root) not in sys.path:
    sys.path.insert(0, str(sys_root))


class ApiIntegrationTests(unittest.TestCase):
    """后端 API 的集成级验证。

    必须用 `with TestClient(app)`：FastAPI 的 lifespan（`main.lifespan`）
    只在上下文管理器里跑，而平台插件正是在那里 `load_all()` 注册的。
    直接 `TestClient(app)` 不进入上下文时，注册表是空的 —— 表现为
    「平台 'grok' 未注册，已注册: []」，任务端点全部误报。
    """

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        import main as main_mod

        cls.app = main_mod.app
        cls._ctx = TestClient(cls.app)
        cls.client = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    # ---------------- 配置 ----------------

    def test_config_read_returns_expected_keys(self):
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200, r.text[:200])
        data = r.json()
        for key in ("mail_provider", "default_executor"):
            self.assertIn(key, data, f"/api/config 应返回 {key}")

    def test_grok_register_mode_is_writable(self):
        """本轮新增的配置键必须能通过 API 写入（否则面板选了不生效）。"""
        r = self.client.get("/api/config")
        original = r.json().get("grok_register_mode", "")
        try:
            r = self.client.put("/api/config", json={"data": {"grok_register_mode": "browser"}})
            self.assertEqual(r.status_code, 200, r.text[:200])
            r = self.client.get("/api/config")
            self.assertEqual(r.json().get("grok_register_mode"), "browser",
                             "写进去必须能读出来")
        finally:
            self.client.put("/api/config", json={"data": {"grok_register_mode": original}})

    def test_unknown_config_key_is_reported_not_silently_dropped(self):
        """未知键仍被容忍（老前端会提交已删除渠道的值），但必须**报出来**。

        静默丢弃是真实踩过的坑：新增 `grok_register_mode` 时忘了加白名单，
        面板保存成功却完全不生效。所以契约是「写入被忽略的键要出现在
        `ignored` 里」，而不是静默 200 或直接 400。
        """
        r = self.client.put("/api/config", json={"data": {"definitely_not_a_key": "x"}})
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        self.assertIn("ignored", body, "被丢弃的键必须回报给调用方")
        self.assertIn("definitely_not_a_key", body["ignored"])
        self.assertNotIn("definitely_not_a_key", body.get("updated") or [],
                         "被忽略的键不应出现在 updated 里")

    def test_known_key_is_not_reported_as_ignored(self):
        """正常写入不能被误报成 ignored —— 否则提示永远亮着。"""
        r = self.client.put("/api/config", json={"data": {"grok_register_mode": "browser"}})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("grok_register_mode", body.get("updated") or [])
        self.assertNotIn("ignored", body)

    # ---------------- 代理 ----------------

    def test_proxies_list_shape(self):
        r = self.client.get("/api/proxies")
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        items = body if isinstance(body, list) else (body.get("items") or [])
        self.assertIsInstance(items, list)
        for p in items:
            self.assertIn("url", p, "代理项必须有 url 字段")

    # ---------------- 任务 ----------------

    def test_tasks_list_shape(self):
        r = self.client.get("/api/tasks")
        self.assertEqual(r.status_code, 200, r.text[:200])
        tasks = r.json()
        self.assertIsInstance(tasks, list)
        for t in tasks[:5]:
            for key in ("id", "status", "platform", "logs"):
                self.assertIn(key, t, f"任务对象缺 {key}")

    def test_register_task_validates_platform(self):
        """不存在的平台必须报错，而不是建出一个永远跑不了的任务。"""
        r = self.client.post("/api/tasks/register", json={
            "platform": "definitely_not_a_platform",
            "count": 1,
        })
        self.assertGreaterEqual(r.status_code, 400,
                                f"未知平台应报错，实际 {r.status_code}")

    def test_register_task_validates_count(self):
        """count=0 或负数必须被拒。"""
        for bad in (0, -1):
            r = self.client.post("/api/tasks/register", json={
                "platform": "grok", "count": bad,
            })
            self.assertGreaterEqual(r.status_code, 400,
                                    f"count={bad} 应报错，实际 {r.status_code}")

    def test_unknown_api_path_returns_404_not_html(self):
        """不存在的 /api/* 必须 404 —— 不能被 SPA 兜底吞成 200 + HTML。

        真实踩过：`/api/icloud/pool-summary`（不存在）返回 200 和整页 HTML，
        调用方以为成功、解析 JSON 时才炸，排查方向被彻底带偏。
        这条测试同时钉住「SPA 兜底仍要服务前端路由」这一半行为。
        """
        r = self.client.get("/api/definitely-not-a-route")
        self.assertEqual(r.status_code, 404,
                         f"未命中的 API 路径应 404，实际 {r.status_code}")
        self.assertNotIn("text/html", r.headers.get("content-type", ""),
                         "API 404 不能返回 HTML 页面")

    def test_spa_fallback_still_serves_frontend_routes(self):
        """前端路由（非 /api）必须仍能拿到 index.html —— 别把兜底整个关掉。

        注意：SPA 兜底只在 `static/` 存在时注册（main.py 的 `if os.path.isdir`），
        而 `static/` 是构建产物、被 gitignore。全新 clone 上没跑过 `npm run build`
        时这个路由根本不存在 —— 那种情况下跳过，而不是误报失败（评审发现）。
        """
        if not (Path(__file__).resolve().parents[1] / "static" / "index.html").exists():
            self.skipTest("static/ 未构建（SPA 兜底未注册），跳过")
        r = self.client.get("/accounts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers.get("content-type", ""),
                      "前端路由应由 SPA 兜底返回 HTML")

    def test_unknown_task_returns_404(self):
        r = self.client.get("/api/tasks/does-not-exist-12345")
        self.assertEqual(r.status_code, 404)

    # ---------------- 账号 ----------------

    def test_accounts_list_shape(self):
        r = self.client.get("/api/accounts?platform=grok")
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        self.assertIn("total", body)
        self.assertIn("items", body)
        self.assertIsInstance(body["items"], list)

    def test_accounts_pagination_is_bounded(self):
        """分页参数必须被接受且不返回全表。"""
        r = self.client.get("/api/accounts?platform=grok&page=1&page_size=1")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(len(r.json().get("items") or []), 1)

    # ---------------- iCloud 号池 ----------------

    def test_icloud_aliases_shape(self):
        r = self.client.get("/api/icloud/aliases")
        if r.status_code == 404:
            self.skipTest("本部署未启用 iCloud 路由")
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        items = body if isinstance(body, list) else (body.get("items") or [])
        self.assertIsInstance(items, list)

    def test_pool_summary_endpoints(self):
        """号池摘要：Outlook 有独立端点，iCloud 的嵌在 /api/icloud/accounts 里。

        写测试时踩过两个坑：① Outlook 的摘要在 /api/outlook 下，不在 /api/icloud；
        ② `/api/icloud/pool-summary` **不存在** —— 打它会被 SPA 的 catch-all 兜住，
        返回 200 + HTML，`r.json()` 才炸。所以这里断言 content-type 是 JSON，
        而不是只看状态码（只看 200 会把「路由不存在」误判成「接口正常」）。
        """
        r = self.client.get("/api/outlook/pool-summary")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn("application/json", r.headers.get("content-type", ""))
        self.assertIsInstance(r.json(), dict)

        # iCloud 的池状态随主号列表返回
        r = self.client.get("/api/icloud/accounts")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn("application/json", r.headers.get("content-type", ""))
        body = r.json()
        accounts = body if isinstance(body, list) else (body.get("items") or [])
        if accounts:
            self.assertIn("pool", accounts[0],
                          "iCloud 主号应带号池摘要（服务层 pool_summary 的输出）")

    # ---------------- 平台动作 ----------------


def test_upload_grok2api_action_is_exposed():
    """Grok 的 upload_grok2api 动作要出现在 /api/actions/grok。

    这是前端渲染「上传 grok2api」按钮与状态列的依据：前端按动作 id
    探测（`platformActions.some(a => a.id === 'upload_grok2api')`），
    动作没暴露就等于功能没上线。
    """
    from fastapi.testclient import TestClient

    import main as main_mod

    with TestClient(main_mod.app) as client:
        r = client.get("/api/actions/grok")
        assert r.status_code == 200, r.text[:200]
        actions = r.json().get("actions") or []
        ids = [a.get("id") for a in actions]
        assert "upload_grok2api" in ids, f"动作未暴露: {ids}"

        action = next(a for a in actions if a.get("id") == "upload_grok2api")
        assert action.get("label"), "动作缺少 label（前端菜单要显示）"
        # params 声明了才能让调用方知道可覆盖连接参数
        param_keys = {p.get("key") for p in (action.get("params") or [])}
        assert {"api_url", "username", "password"} <= param_keys, param_keys


def test_batch_upload_grok2api_rejects_unknown_account():
    """批量端点对不存在的账号要报「账号不存在」，而不是静默成功。

    走真实 ASGI 栈：验证路由参数解析、账号解析、结果计数这条链路。
    """
    from fastapi.testclient import TestClient

    import main as main_mod

    with TestClient(main_mod.app) as client:
        r = client.post(
            "/api/actions/grok/upload_grok2api/batch",
            json={"account_ids": [999999999]},
        )
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert body["total"] == 1, body
        assert body["failed"] == 1, body
        assert body["success"] == 0, body
        assert "不存在" in (body["items"][0].get("message") or ""), body


class BatchActionsDateFilterTests(unittest.TestCase):
    """批量动作端点的日期筛选（`_resolve_batch_accounts`，复审发现 2026-10-07）。

    同「显示与执行一致」缺陷：同步本地状态 / 检测 Plus 试用这类批量动作的
    显示计数包含日期筛选，`all_filtered` 请求也必须按同一组条件选号 ——
    否则设了日期范围时实际处理数大于显示数。
    """

    def setUp(self):
        from datetime import datetime, timedelta, timezone

        from sqlmodel import Session, delete

        from core.db import AccountModel, engine

        self.now = datetime.now(timezone.utc)
        self.old = self.now - timedelta(days=30)

        def _mk(email, created_at):
            m = AccountModel(platform="chatgpt", email=email, password="pw", status="registered")
            m.set_extra({"session_token": "st"})
            m.created_at = created_at
            return m

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.add_all([_mk("old@example.com", self.old), _mk("new@example.com", self.now)])
            session.commit()

    def test_batch_actions_honour_date_filter(self):
        from datetime import timedelta

        from sqlmodel import Session

        from api.actions import BatchActionRequest, _resolve_batch_accounts
        from core.db import engine

        start = self.now - timedelta(days=1)
        body = BatchActionRequest(all_filtered=True, created_at_start=start)
        with Session(engine) as session:
            accounts, missing = _resolve_batch_accounts("chatgpt", body, session)

        emails = sorted(a.email for a in accounts)
        self.assertEqual(
            emails, ["new@example.com"],
            "批量动作端点未应用日期筛选 —— 显示 1 个却会处理 2 个",
        )
        self.assertEqual(missing, [])


def test_runtime_endpoint_reports_the_loaded_code_version():
    """`/api/runtime` 要报出**进程启动时加载的那版代码**。

    这个服务手工拉起、改完代码不重启就一直跑旧代码 —— 实测踩过：OTP 提取的修复
    提交后没重启，之后三次任务全在旧代码上跑，症状与修复前一模一样，排查时先
    怀疑代码又绕了一大圈。这个端点就是用来一眼分辨「代码没改对」还是「进程没重启」。
    """
    from fastapi.testclient import TestClient

    import main as main_mod

    with TestClient(main_mod.app) as client:
        r = client.get("/api/runtime")
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert body.get("code_version"), body
        assert body.get("pid"), body
        # 磁盘版本与启动版本都要给：两者不一致才是「改完没重启」
        assert body.get("disk_version"), body
        assert "stale" in body, body
        assert body.get("booted_at"), body


def test_boot_version_is_captured_at_import_not_per_request():
    """`code_version` 必须是**进程启动那一刻**读的，不能每次请求现查。

    现查的版本报的是「此刻磁盘上的 HEAD」：进程 12:27 启动、12:38 才提交那版代码，
    端点会报出 12:38 的哈希 —— 看着像已经生效，实际跑的还是旧代码。这正是这个
    端点要防的误报，只是方向反了（实测踩到）。钉住「import 期求值」这件事。
    """
    import main as main_mod

    original = main_mod._BOOT_CODE_VERSION
    try:
        # 模拟「启动之后又提交了新代码」：启动版本与磁盘版本不一致
        main_mod._BOOT_CODE_VERSION = "deadbee"
        assert main_mod._code_version() == "deadbee", "启动版本被现查覆盖了"
        # stale 能识别出这种不一致
        assert main_mod._code_version() != main_mod._read_git_version()
    finally:
        main_mod._BOOT_CODE_VERSION = original


if __name__ == "__main__":
    unittest.main()
