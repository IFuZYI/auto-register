# 重构提示词（可执行版）

给执行者（AI agent 或工程师）的作业单。设计理由见 [PLAN.md](./PLAN.md)，词汇见 [GLOSSARY.md](./GLOSSARY.md)。

---

## 0. 你的角色与硬约束

你是这个仓库的重构执行者。**你的任务是合并等价路径，不是减少行数。**

开工前必须接受以下约束，违反任何一条即为失败：

| # | 约束 | 检查方式 |
|---|---|---|
| C1 | 不重写。不新建"更好的"平行实现，只改造现有实现 | 新增文件必须能在 PLAN 的任务表里找到 |
| C2 | 不删「事故保险」分支。带 `实测`/`踩过` 注释的 if 一律保留，只允许改写法不允许删语义 | `git diff` 里不得出现删除含 `实测` 注释的 hunk |
| C3 | 每个任务先写测试再改代码（RED → GREEN → REFACTOR） | 每个任务至少 1 个新测试文件或新 test 函数 |
| C4 | 每完成一个任务立即提交，不攒批 | `git log` 每任务 ≥ 1 commit |
| C5 | 阶段门禁不通过不得进入下一阶段 | 门禁命令输出留档 |
| C6 | 不碰 `platforms/chatgpt/protocol/auth_flow.py` 的内部署辑（阶段 1–3） | 该文件 diff 仅允许签名/常量级改动 |
| C7 | 不确定就停下写问题，不要猜 | 产出 `docs/refactor/BLOCKERS.md` 条目 |

---

## 1. 环境事实（已核实，不要再查）

```
仓库根        /data/workspace/Register/.worktrees/refactor
Python        3.12+，pytest
生产代码      52,454 行 / 207 文件
测试          211 文件 / 2,969 test / 55,807 行
HTTP 端点     102 个
事故注释      293 条（grep "实测\|踩过"）
兼容 shim     47 处
真死代码      92 行（0.18%）—— 所以"删死代码"不是本次目标
```

基线指标（阶段 0 记录，阶段 3 对比）：

```
from api import 违规   12
字符串动作 id 散落     17
鸭子类型 shim          13
账号表示模型            2
except Exception      480（其中 pass 80）
```

---

## 2. 工作循环（每个任务都走一遍）

```
1. 读任务对应的 field-notes 篇章（见 FIELD-NOTES.md 索引）
2. 写失败测试（RED）
   - 优先表驱动：把「输入 → 期望」写成一张表，而不是一串 if 断言
3. 改实现直到测试通过（GREEN）
4. 跑该任务的全量相关测试（不是只跑新测试）
5. 跑门禁命令（见任务表）
6. 提交：refactor(phaseN): T<id> <一句话>
7. 若发现与 PLAN 冲突的事实 → 写 BLOCKERS.md，停下
```

**测试命名约定**：`test_<capability>_<behavior>_<condition>`，例如
`test_refresh_returns_partial_when_codex_hits_add_phone`。

---

## 3. 阶段 0 任务

### T0.1 注释抽取

```bash
mkdir -p docs/field-notes
python - <<'PY'
import re, pathlib, json
PAT = re.compile(r"实测|踩过|用户要求|用户报告|2026-\d\d-\d\d|线上")
out = {}
for p in pathlib.Path(".").rglob("*.py"):
    if "tests" in p.parts or ".git" in p.parts: continue
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    hits = [(i+1, l.strip()) for i, l in enumerate(lines) if PAT.search(l)]
    if hits: out[str(p)] = hits
pathlib.Path("docs/field-notes/_index.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(sum(len(v) for v in out.values()), "条 /", len(out), "文件")
PY
```

**验收**：`_index.json` 条目数 ≥ 280；按 [FIELD-NOTES.md](./FIELD-NOTES.md) 的 12 个主题各写一篇，每篇至少引用 3 处 `file:line`。

### T0.2 测试三分类

```bash
# A 契约（保留，重构必须全绿）
ls tests/test_*contract*.py tests/test_*semantics*.py tests/test_*flow*.py > /tmp/tests_A.txt
# B 实现锁定（阶段 3 改写）
grep -rln "from api import tasks\|patch(\"api\.tasks\|_task_store" tests/ > /tmp/tests_B.txt
# C 事故回归（一条都不许删）
grep -rln "实测\|踩过\|invalid_state\|add-phone\|429" tests/ > /tmp/tests_C.txt
wc -l /tmp/tests_A.txt /tmp/tests_B.txt /tmp/tests_C.txt
```

**验收**：三份清单存在；B 类数量记入 `docs/refactor/BLOCKERS.md` 的"阶段 3 前置"。

### T0.3 Alembic 基线

```bash
pip install alembic
alembic init migrations
# env.py 指向 core.db 的 DATABASE_URL
alembic revision --autogenerate -m "baseline existing schema"
alembic stamp head          # 现有库不执行 DDL，只打标记
alembic upgrade head        # 空库应能建出全部表
```

同时把 `core/db/migrations.py` 的入口改为 no-op（保留函数签名，内部直接 return）。

**验收**：空库 / 现有库各跑一次 `upgrade head` 均成功；`migrations.py` 不再做 DDL。

### T0.4 快照

```bash
tar czf /tmp/data-snapshot-$(date +%F).tgz data/
python scripts/dump_accounts.py --platform chatgpt > /tmp/accounts-before.json  # 若无此脚本，用 api/accounts.py 的导出
```

### T0.5 去重 + 唯一索引

```sql
-- dry-run：先看冲突
SELECT platform, lower(email) e, COUNT(*) c FROM accounts GROUP BY 1,2 HAVING c>1;
-- 保留 id 最小的一行
DELETE FROM accounts WHERE id NOT IN (
  SELECT MIN(id) FROM accounts GROUP BY platform, lower(email));
CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_platform_email
  ON accounts(platform, lower(email));
```

**验收**：并发测试（20 线程 upsert 同邮箱）后 `COUNT(*) = 1`。

---

## 4. 阶段 1 任务

### T1.1 `AccountRecord` + `Credentials`

新建 `core/models/account.py`：

```python
@dataclass(frozen=True, slots=True)
class Credentials:
    access_token: str = ""
    refresh_token: str = ""
    session_token: str = ""
    id_token: str = ""
    sso: str = ""
    client_id: str = ""
    device_id: str = ""
    cookie_header: str = ""
    totp_secret: str = ""

@dataclass(slots=True)
class AccountRecord:
    platform: str
    email: str
    password: str = ""
    status: AccountStatus = AccountStatus.REGISTERED
    credentials: Credentials = field(default_factory=Credentials)
    register_proxy: str = ""
    observations: dict = field(default_factory=dict)   # chatgpt_local / token_refresh / plus_check
    intent: dict = field(default_factory=dict)         # registration_mode / bind_2fa
    extra: dict = field(default_factory=dict)          # 其余平台自定义
    id: int | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
```

**规则**：
- `observations` 只存原始结论，不存解释
- `intent` 只存用户选择
- `status` 是派生值，由 `recompute_status(record)` 一处计算
- 迁移期 `Account.to_record()` / `AccountModel.to_record()` 双向适配

**验收**：`test_account_record_roundtrip` —— `Account → record → Account` 与 `Model → record → Model` 字段级相等（排除时间精度差异）。

### T1.2 删 shim

逐个删除，每删一个跑一次相关测试：

```
platforms/chatgpt/plugin.py:104      _A（check_valid）—— 可直接删，probe 不读 .cookies
platforms/chatgpt/plugin.py:344      _A（execute_action）
services/chatgpt_sync.py:164         _SyncAccount
services/external_sync.py:106        _UploadAccount
core/db/proxy_repository.py:27       _JsonProxy
services/sms/providers.py:38         _FacadeProxy
core/proxy_utils.py:10               _LegacyProxyParts
platforms/icloud/mailbox.py:121      _Connection（保留：它是真实适配器）
platforms/grok/oauth_device.py:35    _NoRedirectSession（保留）
core/console.py:52                   _BrokenPipeTolerantStream（保留）
```

**验收**：`grep -rn "class _[A-Z]" core services platforms api | wc -l` = 3（只留三个真适配器）。

### T1.3 分库塌缩

1. 合并脚本：遍历 `data/platforms/*.db` + `data/account_manager.db`，按 `(platform,email)` upsert 进单库
2. `PlatformDatabaseRegistry.engine_for()` 恒返回 `current_engine()`；`url_for()` 返回 `DATABASE_URL` 并 `logger.warning` 一次
3. `mailbox_pool_session()` 改为 `Session(current_engine())`

**验收**：合并前后行数守恒；`grep -rn "platform_session(" | wc -l` 不变（接口保留）；跨库 join 测试通过。

### T1.4 全量加密

在仓储边界加解密，上层无感：

```python
# core/db/repository.py
def _seal(record: AccountRecord) -> dict:      # 写
    return {"password": seal(record.password),
            "credentials": seal(asdict(record.credentials))}

def _open(row: AccountModel) -> AccountRecord:  # 读
    ...
```

加密字段清单（**一个都不能漏**）：
`password`、`access_token`、`refresh_token`、`session_token`、`id_token`、`sso`、`client_id`、`cookie_header`、`totp_secret`、`configs.value`（当 key ∈ `SECRET_CONFIG_KEYS`）

**验收**：
```bash
# 库文件里不得出现 JWT 头或 sk- 前缀
strings data/account_manager.db | grep -c "eyJ"   # 期望 0
strings data/account_manager.db | grep -c "sk-"   # 期望 0
```

### T1.5 2FA journal 加密

`services/chatgpt_two_factor.py::_write_secret_journal` 改为 `secret_box.seal(json.dumps(payload))` 写文件；`recover_pending_totp_secrets` 读取时 `open_()` 解密。

**验收**：journal 文件 `strings` 不到明文 secret；恢复成功后文件被删除（已有 `path.unlink`）。

### T1.6 `config_schema.py`

```python
@dataclass(frozen=True)
class ConfigKey:
    key: str
    type: Literal["str", "int", "bool", "enum"]
    secret: bool = False
    default: str = ""
    owner: str = ""        # chatgpt / grok / panel:cpa / global

CONFIG_KEYS = [ ... 69 项 ... ]
# api/config.py 与前端 configFields.ts 从它派生
```

**验收**：`test_secret_config_contract` 绿；新增一个键只需改 `config_schema.py` 一处。

---

## 5. 阶段 2 任务

### T2.1 契约类型

```python
# core/platform_contract.py
class Capability(str, Enum):
    PROBE = "probe"; REFRESH = "refresh"; UPLOAD = "upload"
    SYNC_STATUS = "sync_status"; BACKFILL = "backfill"
    ENROLL_2FA = "enroll_2fa"; QUOTA = "quota"

@dataclass(frozen=True)
class CapabilitySpec:
    capability: Capability
    label: str
    params_schema: dict
    panel: str | None = None
    requires_proxy: bool = False

@dataclass(frozen=True)
class Outcome:
    ok: bool
    status_patch: StatusPatch | None = None
    credential_patch: Credentials | None = None
    observations: dict = field(default_factory=dict)
    error: str = ""
    error_kind: ErrorKind = ErrorKind.UNKNOWN
```

**验收**：`test_provider_contract` 对 chatgpt / grok 参数化跑同一套断言：
- `capabilities` 里每个 `capability` 都有实现
- 每个实现返回 `Outcome`（不是 dict）
- 未知 capability 抛 `NotImplementedError`

### T2.2–T2.3 适配器 + 落库收敛

`execute_action` 内部查表；`_apply_action_result` 从 9 分支缩到：

```python
if outcome.status_patch:
    repo.apply_status(account, outcome.status_patch)
if outcome.credential_patch:
    repo.apply_credentials(account, outcome.credential_patch)
```

**验收**：17 个旧 action id 全部仍可调用（表驱动测试，逐个断言 `ok` 与 `observations` 非空）。

### T2.4 状态表

```python
PROBE_STATE_TO_STATUS: dict[str, str | None] = {
    "access_token_valid": None,
    "access_token_invalidated": "invalid",
    "unauthorized": "invalid",
    "account_deactivated": "banned",
    "banned_like": "banned",
    "missing_access_token": "invalid",
    "probe_failed": None,
    ...
}
```

保留既有不变量：**banned 粘性**（弱信号不得降级）、**只有 expired/invalid 可恢复为 registered**、**configuration 类错误不动状态**。

**验收**：`test_status_transition_table` —— 12 个探测态 × 当前状态 的笛卡尔积，期望值写死成表。

### T2.5 `ErrorKind` + `Policy`

```python
class ErrorKind(str, Enum):
    TRANSIENT = "transient"; PERMANENT = "permanent"
    CONFIG = "config"; UNKNOWN = ""

POLICY = {
    ErrorKind.TRANSIENT: Policy(retry=True,  touch_status=False),
    ErrorKind.PERMANENT: Policy(retry=False, status="invalid"),
    ErrorKind.CONFIG:    Policy(retry=False, touch_status=False),
}
```

**验收**：新增一个错误标记只改 `_KIND_RULES` 一行；chatgpt 与 grok 的 `refresh_kind` 值域一致。

### T2.6 `TokenPlan`

```python
class TokenPlan(str, Enum):
    NONE = "access_token_only"
    LOGIN = "refresh_token_login"
    CODEX = "refresh_token_codex"

    @property
    def wants_rt(self) -> bool: return self is not TokenPlan.NONE
    @property
    def route(self) -> str: return "codex" if self is TokenPlan.CODEX else "login"
```

**验收**：旧值 `refresh_token` / `rt` / `1` / `true` / `no_rt` / `0` 全部仍能解析（参数化测试覆盖 `LEGACY_TOKEN_PLAN_ALIASES` 全表）。

### T2.7 `AccountProxyBinder`

**验收**：三处调用点（注册复用、单账号动作、自动维护）行为一致；**注册路径不判"可绑定"，用了谁就绑谁**（见 PLAN 风险表）。

---

## 6. 阶段 3 任务

### T3.1–T3.2 TaskService

```python
@router.post("/register")
def create_register_task(req: RegisterTaskRequest):
    return task_service.submit(RegisterJob.from_request(req))
```

`TaskContext` 注入取代 `from api import tasks`：

```python
@dataclass
class TaskContext:
    task_id: str
    log: Callable[[str], None]
    checkpoint: Callable[..., None]
    stop_requested: Callable[[], bool]
    add_cashier_url: Callable[[str], None]
```

**验收**：`grep -rn "from api import" services/ | wc -l` = 0；`test_task_service_without_web` 在不 import fastapi 的进程里跑通。

### T3.3 PanelAdapter

```python
class PanelAdapter(ABC):
    key: str
    platforms: tuple[str, ...]
    def fetch(self, ctx) -> list[RemoteAccount]: ...
    def push(self, ctx, acc) -> PushResult: ...
    def pull(self, ctx, remote) -> Credentials | None: ...
    def diff(self, local, remote) -> DiffResult: ...   # 默认实现复用现有纯函数
```

**验收**：新增一个假面板只改 1 个文件；`test_panel_adapter_contract` 对 4 个适配器跑同一套断言。

---

## 7. 每个阶段结束必跑

```bash
# 门禁
pytest -q tests/ -x --timeout=300
python -m compileall -q core services platforms api

# 指标
echo "api 违规:   $(grep -rn 'from api import' --include=*.py services/ | wc -l)"
echo "动作 id:    $(grep -rn '\"probe_local_status\"\|\"refresh_token\"\|\"upload_cpa\"' --include=*.py api platforms services | wc -l)"
echo "shim:       $(grep -rn 'class _[A-Z]' --include=*.py api core services platforms | wc -l)"
echo "宽泛异常:   $(grep -rn 'except Exception' --include=*.py api core services platforms | wc -l)"

# 数据等价
python scripts/dump_accounts.py --platform chatgpt > /tmp/after.json
diff <(jq -S 'map(del(.updated_at))' /tmp/accounts-before.json) \
     <(jq -S 'map(del(.updated_at))' /tmp/after.json)
```

**diff 非空即停止**，先解释差异再继续。

---

## 8. 中断恢复协议

会话中断后重新进入，按此顺序恢复：

1. 读 `docs/refactor/BLOCKERS.md`（若有）
2. `git log --oneline -20` 找到最后一个 `refactor(phaseN): T<id>` 提交
3. 读该任务在 PLAN 里的验收条件
4. 跑该任务的验收命令 —— 通过则进下一个任务，失败则重做该任务
5. **不要**根据"代码看起来做了一半"推测进度；只认验收命令
