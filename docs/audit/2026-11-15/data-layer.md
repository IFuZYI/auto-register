# Lane: 数据层（JSON 后端并发 / 迁移 / 双后端等价）

基线: `fca2eec` + 工作树 dirty（62 改 / 30 未跟踪）
方法: 控制器直接复现（lane 1 子代理在 17 分钟后被中断，仅留下一条线索，
其余结论由控制器自行取证并标注来源）。

## 结论

**SQLite 后端可用（当前生产即此后端）。JSON 账号后端有一个 Critical：
多实例并发写会丢更新，且能写坏整个库文件。** 由于全新部署默认走 JSON
（`DEFAULT_BACKEND = json`），这是真实可达的故障面，不是理论缺陷。

## 发现

### DL-1 [Critical] JSON 账号后端：每次取 store 都新建实例 → 锁失效 → 丢写 + 写坏文件

- 位置: `core/db/repository.py:243`（`_json_store()` 每次 `return AccountStoreJson(DATA_DIR / "accounts.json")`）
  + `core/storage/account_store_json.py:84`（`self._store = JsonStore(path)`）
  + `core/storage/json_backend.py:59-72`（`_read` 全量读 / `_write` 用固定名 `.tmp` 原子替换）
- 复现: `PYTHONPATH=. .venv/bin/python /tmp/repro_json_lock.py`（40 个线程，每个线程
  **新建** `AccountStoreJson` 实例后 `update` 同一记录的**不同字段**）
- 证据（实际输出）:

```
初始: registered
期望 40 个 probe 全部被写入, 实际生效 11 个
丢失的 probe: [0, 1, 2, 3, 4, 6, 7, 8, 9, 11] …
→ 丢写: 29 个
```

  同时抛出的三类异常（并发副作用，不是偶发噪音）:

```
FileNotFoundError: [Errno 2] ... accounts.json.tmp -> accounts.json
JsonStoreError: ... 不是合法 JSON（Extra data: line 54 column 2 (char 1030)）...
```

- 影响: 三种后果，按严重度递增 ——
  1. **丢更新**：并发下 40 次写入只有 11 次生效（本文档实测）。
  2. **写坏文件**：所有实例共用同一个 `accounts.json.tmp`。A 写临时文件、
     B 覆盖同一临时文件、A `os.replace` 成功后 B `os.replace` 找不到文件 →
     异常；更坏的是 `_read` 可能读到别人半写的临时内容 → `Extra data`。
     `JsonStore._read` 对损坏文件**刻意抛错**（拒绝当空库，这点是对的），
     所以损坏后该后端会**整体不可用**，而不是静默丢数据。
  3. **可达性**：`DEFAULT_BACKEND = json`，全新部署默认开启；账号 JSON 后端
     另有 `STORAGE_BACKEND_ACCOUNTS` 显式开关（`accounts_backend_enabled`），
     当前生产是 sqlite 所以**尚未触发**。
- 建议（最小修法，二选一即可）:
  - **A（推荐）**：`_json_store()` 做**按路径的实例缓存**（模块级 dict + 锁），
    让同一路径在整个进程内共享同一个 `JsonStore`（也就共享同一把锁）。
  - **B**：把锁提到**类级**（`JsonStore._class_lock = threading.RLock()`，
    所有方法 with 它），并让临时文件名**每实例唯一**（`f"{path}.{uuid4().hex}.tmp"`），
    消除"共用临时文件"这一类竞态。
  - 两者都应补一条回归测试：多实例并发 `update` 不同字段，断言**零丢写**。

### DL-2 [Important] JSON 后端原子写的临时文件名固定，跨实例必然互撞

- 位置: `core/storage/json_backend.py:63`（`tmp = self.path.with_suffix(self.path.suffix + ".tmp")`）
- 复现: 同 DL-1 的脚本；即使只修锁不修临时文件名，只要存在两个实例（或两个进程）
  就会重现 `FileNotFoundError`。
- 证据: 见 DL-1 的异常输出。
- 影响: 即使把 DL-1 的锁修好（单进程内共享实例），**多进程**（用户误起两个
  `main.py`，或将来扩 worker）仍会互撞。属于独立的一层防护。
- 建议: 临时文件名加入进程/实例标识（`os.getpid()` 或 `uuid4`），随 DL-1 一起改。

## 本轮修复

已按 TDD 修复 DL-1 + DL-2，**同一个 fix 覆盖两者**：

- `core/storage/json_backend.py`：
  - 新增**按文件路径共享的锁**（`_LOCKS` + `_lock_for()`）。同一路径在进程内
    共享同一把 `RLock`，跨实例互斥；不同文件互不阻塞。
  - 所有公开方法改为 `with self._lock, self._shared:`（实例锁保留只为同实例重入）。
  - 临时文件名改为**每实例唯一**：`f".{os.getpid()}-{uuid4().hex}.tmp"`
    （`_tmp_path()`），消除"共用同一个 `.tmp`"这一类竞态。
- 回归测试（`tests/test_storage_backend.py::JsonStoreRoundTripTests`）：
  - `test_concurrent_updates_from_separate_instances_do_not_lose_writes`
    —— 40 线程 × 20 次、每线程新建实例，断言**零丢写、零异常**。
  - `test_tmp_file_is_unique_per_instance` —— 两实例的临时文件名必须不同。

**变异验证**（证明测试不是假绿）：把共享锁去掉并恢复固定 `.tmp` 名后，
上面两条立即变红；恢复实现后 36 条全绿。

## 已经正确的部分（避免误改）

- `JsonStore._read` 对非法 JSON **抛错而不是返回 `{}`**（`json_backend.py:52-57`），
  注释写明"否则下次写入会覆盖掉已有数据" —— 这是正确的防御，不要改成容错。
- `resolve_backend_name`：已有 SQLite 库时**不静默切到 JSON**，避免"数据看起来消失"。
- 账号 JSON 后端由 `STORAGE_BACKEND_ACCOUNTS` 单独开关，与总开关解耦 —— 理由
  写在 `_json_store()` docstring（"原生写路径尚未迁移"）。

## 未审计（本 lane 未完）

lane 1 子代理在 17 分钟内未落盘，以下面**未覆盖**，不得视为干净：

- 迁移链是否只在空库可用（先例 `docs/audit/2026-10-30/data-layer.md` 曾指出）。
- `init_db()` 是否覆盖全部表。
- SQLite 与 JSON 在**排序、分页、NULL、唯一约束**上的逐项等价性。
- 加密字段读写路径是否有一处漏加密/漏解密。
