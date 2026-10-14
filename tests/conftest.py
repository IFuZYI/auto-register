"""测试期的数据库隔离。

``core.db`` 在 import 时就按 ``DATABASE_URL`` 建好 engine，所以这里必须在任何
测试模块 import 之前改环境变量 —— conftest 是 pytest 唯一保证先于测试模块执行
的入口。跑测试不再往仓库根目录写 ``account_manager.db``，也不会读到上一次跑
留下的脏数据。

**`DATA_DIR` 也要一起改**：邮箱池（icloud / outlook）默认分库，库路径由
`DATA_DIR` 推导（`data/platforms/*.db`）。只改 `DATABASE_URL` 的话，测试会连到
真实的 `data/platforms/` 上 —— 测试跑一遍就把开发环境的邮箱池改了。

建表同样放在这里：生产环境由 ``main.py`` 启动时调 ``init_db()``，测试里没人调，
不建表的话所有落库路径都会撞上 ``no such table``。``configs`` 表定义在
``core.config_store`` 里，不 import 它就不会进 ``SQLModel.metadata``。

**solver 也在这里关掉**：``main.lifespan`` 会调 ``solver_manager.start_async()``，
而每个用 ``TestClient(app)`` 的测试都会跑一次 lifespan。默认开着的话，一次测试
运行会真的去拉起浏览器池 —— 实测 7 个文件、59 个测试因此从 6 秒变成 367 秒
（每次 lifespan 等 30 秒等浏览器就绪），而且那批浏览器会互相抢内存，把与该测试
毫无关系的用例拖失败。测试不该起真浏览器：跑的是接线契约，不是解题能力。
"""

import os
import tempfile

# solver 必须在任何测试模块 import 之前关掉（solver_manager 惰性读环境，但
# conftest 是唯一保证先执行的入口，放这里最稳）。
os.environ.setdefault("APP_ENABLE_SOLVER", "0")

_TMP_DB_DIR = tempfile.TemporaryDirectory()
_TMP_DATA_DIR = tempfile.TemporaryDirectory()
# 顺序无所谓（core.paths 惰性读环境），但两个都要在 import core.db 之前设好
os.environ["DATA_DIR"] = _TMP_DATA_DIR.name
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP_DB_DIR.name, 'test.db')}"

import core.config_store  # noqa: E402,F401  注册 configs 表
from core.db import init_db  # noqa: E402  必须在 DATABASE_URL 设好之后再 import

init_db()


def pytest_sessionfinish(session, exitstatus):
    from core.db import engine

    engine.dispose()
    for tmp in (_TMP_DB_DIR, _TMP_DATA_DIR):
        try:
            tmp.cleanup()
        except PermissionError:
            pass
