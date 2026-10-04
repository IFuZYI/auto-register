# 扩展指南：新增平台与流程

本文说明如何在本项目里新增一个平台、一条注册流程或一种邮箱渠道，以及
数据库分库、邮箱唯一键这些公共能力怎么用。

> **接线清单在 [MAINTENANCE.md §2](MAINTENANCE.md)** —— 本文讲「怎么写代码」，
> 那篇讲「改完还要同步动哪些文件、漏了会怎样」。两篇配合看。

## 0. 分层总览

```
core/            基础设施（不依赖具体平台）
├── base_platform.py   平台插件基类（契约 + 账号池 API）
├── registry.py        插件注册表（SUPPORTED_PLATFORMS 白名单）
├── base_mailbox.py    邮箱渠道门面（重导出，实现在 core/mailboxes/）
├── mailboxes/         邮箱渠道包（一渠道一文件 + 注册表）
│   ├── base.py            MailboxAccount / BaseMailbox
│   ├── registry.py        渠道注册表（新增渠道 = 加文件 + 一行注册）
│   └── channels/          各渠道实现（outlook/ / ...）
├── mail_import_sources.py  邮箱导入视图映射（纯数据）
├── base_captcha.py    验证码方案基类
├── db/                数据层（分库 / 仓储 / 迁移）
└── executors/         执行器（protocol / playwright / factory）

modules/         可复用构件（不依赖 HTTP 与数据库）
├── automation/        注册流程编排（pipeline）
├── mail/              邮箱工厂（转发 core 注册表）+ 依赖 services 的渠道
├── execution/         执行器工厂（转出 core/executors/factory.py）
├── proxy/             代理池适配
├── verification/      验证挑战处理
├── config/            配置上下文
└── platforms/         平台无关的注册服务

platforms/       具体平台实现（只实现协议细节）
├── chatgpt/
├── grok/
└── icloud/

api/             HTTP 接口
services/        业务服务
```

**依赖方向**：`platforms → modules → core`。反向依赖（core 里 import platforms/modules/services）
是不允许的 —— 分库注册表因此由调用方传入表对象，而不是自己去 import 平台模型。

例外只有两个**加载器**：

| 文件 | 为什么放行 |
|------|-----------|
| `core/registry.py` | 插件加载器，扫描 `platforms/` 是它的职责本身 |
| `core/mailboxes/registry.py` | 渠道注册表，延迟加载 `modules/mail/`（`icloud_local` 要读 iCloud 主号凭据，依赖 `services/`，不能下沉进 core） |

`core/registry.py` 与 `core/mailboxes/registry.py` 是仅有的两个加载器例外，其余 core 文件一律禁止反向依赖。

## 1. 新增一个平台

### 1.1 最小实现

```python
# platforms/foo/__init__.py
from .plugin import FooPlatform

__all__ = ["FooPlatform"]
```

```python
# platforms/foo/plugin.py
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registry import register


@register
class FooPlatform(BasePlatform):
    name = "foo"                    # 唯一标识，也是分库键
    display_name = "Foo"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]

    def register(self, email: str = None, password: str = None) -> Account:
        # ① 判重（邮箱是唯一业务键）
        if email and self.is_email_registered(email):
            raise RuntimeError(f"该邮箱已注册: {email}")

        # ② 拿邮箱
        mailbox = self.mailbox or self._default_mailbox()
        account_email = email or mailbox.get_email().email

        # ③ 注册协议（平台自己的逻辑）
        ...

        return Account(
            platform=self.name,
            email=account_email,
            password=password or "...",
            token="...",
            status=AccountStatus.REGISTERED,
            extra={...},
        )

    def check_valid(self, account: Account) -> bool:
        ...

    def get_platform_actions(self) -> list:
        return [{"id": "probe", "label": "测活", "params": []}]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "probe":
            ...
            return {"ok": True, "data": {...}}
        raise NotImplementedError(action_id)
```

### 1.2 注册到白名单（必做）

```python
# core/registry.py
SUPPORTED_PLATFORMS = ("chatgpt", "icloud", "grok", "foo")
```

> 白名单是**安全闸**：不在名单里的插件即使有 `@register` 装饰器也不会加载。
> 忘了加这一步，插件会被静默忽略。

### 1.3 平台专属表（可选）

```python
# core/db/models_platform.py 里加模型
class FooThingModel(SQLModel, table=True):
    __tablename__ = "foo_things"
    ...

FOO_TABLES = [FooThingModel.__table__]
```

```python
# core/db/__init__.py 的 init_db() 里注册
for platform in platform_database_registry.configured_platforms():
    platform_database_registry.register_schema(platform, ACCOUNT_TABLES)
```

> 表对象必须取 `.__table__`（SQLAlchemy Table）；SQLModel 类本身不能直接传给
> `create_all(tables=...)`。

## 2. 新增一条流程（自动化编排）

`modules/automation/pipeline.py` 提供了与平台无关的编排骨架：它负责执行器创建、
邮箱分配、OTP 基线、代理池上报，平台回调只管自己的协议状态机。

```python
from modules.automation import RegistrationPipeline, RegistrationRequest

pipeline = RegistrationPipeline.from_application_defaults()

def my_flow(ctx):
    """ctx 提供：ctx.mailbox / ctx.mailbox_account / ctx.executor /
    ctx.wait_for_email_code() / ctx.solve_verification()"""
    code = ctx.wait_for_email_code(keyword="Foo", timeout=120, code_pattern=r"\d{6}")
    ...

result = pipeline.run(
    RegistrationRequest(
        mail_provider="icloud_local",   # 现存的渠道只有 icloud_local / outlook
        executor_type="protocol",
        settings={"foo_option": "1"},
    ),
    my_flow,
)
print(result.email, result.value)
```

**扩展点**：

| 需求 | 做法 |
|------|------|
| 加一个生命周期事件（埋点/日志） | `AutomationHooks(on_event=fn)`，事件名见 `_emit` 调用处 |
| 换邮箱渠道 | `RegistrationPipeline(mailbox_factory=...)` |
| 换代理来源 | `RegistrationPipeline(proxy_provider=...)` |
| 加验证处理 | `RegistrationPipeline(verification_handler=...)` |
| 平台特有的多步流程 | 在 `platforms/<name>/` 里写多个模块，由 `plugin.py` 组合 |

## 3. 新增一种邮箱渠道

**只需两步：加一个文件 + 在 `channels/__init__.py` 里 import 一次。**

```python
# core/mailboxes/channels/foo.py
"""Foo 临时邮箱。"""
from __future__ import annotations

from ..base import BaseMailbox, MailboxAccount
from ..registry import register_mailbox_provider


class FooMailbox(BaseMailbox):
    def __init__(self, api_key: str, proxy: str = None):
        self._api_key = api_key
        self._proxy = proxy

    def get_email(self) -> MailboxAccount:
        ...
        return MailboxAccount(email=addr, account_id=token)   # account_id 是收码凭证

    def get_current_ids(self, account: MailboxAccount) -> set:
        ...

    def wait_for_code(self, account, keyword="", timeout=120,
                      before_ids=None, code_pattern=None, **kw) -> str:
        def poll_once():
            ...
            return self._safe_extract(text, code_pattern)
        return self._run_polling_wait(
            timeout=timeout, poll_interval=3, poll_once=poll_once
        )


@register_mailbox_provider("foo", display_name="Foo（自动生成）")
def build(*, extra: dict, proxy: str = None) -> BaseMailbox:
    """构建 Foo 渠道。"""
    return FooMailbox(api_key=extra.get("foo_api_key", ""), proxy=proxy)
```

```python
# core/mailboxes/channels/__init__.py —— 加一行
from . import foo  # noqa: F401
```

不用改工厂：`create_mailbox` 由注册表驱动，`register_mailbox_provider`
装饰器就是唯一的接入点。

**要点**：

- `MailboxAccount.account_id` 是收码凭证（token / 账号 id），**不能丢** ——
  只有邮箱地址是收不到码的；
- 用 `self._run_polling_wait(...)` 而不是自己写循环，它会响应任务控制
  （暂停/停止/跳过）；
- `_safe_extract(text, pattern)` 复用通用验证码提取逻辑；
- `_build_search_text(message)` 把各渠道五花八门的 JSON 字段拼成可搜索文本
  （mail.tm 的正文就藏在 `html` 数组里、`text` 为空）；
- **渠道依赖 `services/` 时放 `modules/mail/`**（如 `icloud_local` 要读 iCloud
  主号凭据），在那里同样用 `register_mailbox_provider` 自注册；core 侧的
  注册表会在查不到渠道时延迟 import `modules.mail` 一次。见
  `core/mailboxes/registry.py` 的 `_OPTIONAL_PROVIDER_MODULES`；
- **未知 provider 直接报错**并列出可用渠道。旧版工厂末尾用 `else: # laoudo`
  兜底，名字拼错会静默返回 `LaoudoMailbox`，故障现场是「一直收不到验证码」，
  极难排查——这条路径已被移除。

### 3.1 两种渠道范式

| 范式 | 例子 | `get_email()` 做什么 | 收码凭证 |
|---|---|---|---|
| **自建地址** | （一次性临时邮箱渠道已整体删除，见下） | 向上游注册一个新邮箱 | 上游返回的 token |
| **复用已有地址** | `outlook`（别名 `microsoft` / `mail_import`）、`icloud_local` | 让主号创建/取用别名 | 主号 id + 别名地址 |

现存渠道只有两条，都是**复用型 / 本地号池型**：`outlook`（微软号池，挂
`microsoft` / `mail_import` 两个别名）与 `icloud_local`（iCloud 主号开隐私邮箱）。
参考实现：`modules/mail/icloud_local.py`。`account_id` 存成
`"<主号 id>:<别名地址>"` —— 收件要按主号取邮件、按别名过滤，两者缺一不可。

> 一次性临时邮箱（Mail.tm / DuckMail / TempMail.lol / MoeMail / SkyMail /
> CloudMail / MaliAPI / GPTMail / OpenTrashMail / CF Worker / Laoudo / Aitre）与
> 远程服务型 `icloud_hme` 已按用户要求整体删除 —— 所以上面「自建地址」一栏没有
> 现存例子。新增这类渠道时照本节骨架写，并在
> `core/mail_import_sources.py` 的 `_LEGACY_PROVIDER_ALIASES` 里登记旧名收敛
> （老库里的值要能读，否则升级后报「未知邮箱提供商」）。

**本地号池型渠道**：`icloud_local` 与 `outlook` 的凭据在本机分库里
（`data/platforms/<key>.db`），渠道方法体内延迟 import `services.icloud_service`
取主号与收件。新增同类渠道时照这个模式：主号由「邮箱服务」下对应的控制台维护，
注册页只负责挑一个可用主号（配置键 `<provider>_account_id`）。

**依赖方向**：`modules/` 里访问 `services/` 时用**延迟导入**（在方法体内 import），
避免模块导入期就把整条业务栈拉起来。

## 4. 新增一个周期任务（定时调度）

`core/scheduler.py` 只负责按间隔触发，**不认识具体业务**（core 不能依赖
`services/`）。业务侧自己注册：

```python
# services/my_service.py 末尾
from core.scheduler import register_job

register_job(
    "my_job",
    interval_seconds=lambda: my_interval_seconds(),   # 返回 0 = 本次不跑
    runner=lambda: run_my_job(),
)
```

```python
# main.py 的 lifespan 里 import 一次即可（触发注册）
from services import my_service  # noqa: F401
```

参考实现：原 `services/cpa_manager.py` 末尾注册 `cpa_maintenance`（该任务已按用户
要求删除，但注册手法照旧可用）。
`scheduler.start()` 会把所有已注册任务的 `last_run_at` 设为当前时间，
避免应用一启动就瞬间触发。

### 4.1 生命周期约定（改调度器时必读）

`scheduler.stop()` 会**唤醒并 join** 循环线程，然后置 `_thread = None`：

- 循环线程用 `Event.wait(interval)` 而非 `time.sleep(interval)` —— stop 能立即
  生效，不用等满一个轮询周期（默认 60s）。
- 每次 `start()` 递增**代次令牌**（`_generation`），循环线程记住自己那一代；
  醒来后若代次已变就直接退出。
- **为什么必须这样**：早期实现里 `stop()` 只置 `_running = False`，既不 join
  也不唤醒正在 sleep 的线程。若在 sleep 窗口内再次 `start()`，`_running` 被翻
  回 `True`，旧线程醒来后 `while self._running` 又成立 —— 两个 `_loop` 并发，
  周期任务（如 CPA 维护）被重复执行。回归测试见
  `tests/test_task_runtime.py::SchedulerLifecycleTests`。

**四条容易踩的行为约定：**

| 行为 | 说明 |
|---|---|
| `interval_seconds()` 抛异常 | 该任务本轮跳过（`_Job.due()` 内部吞掉异常返回 0），不拖垮调度循环 |
| `interval_seconds()` 返回 < 1 的值 | 正常生效（内部用 `float`，不会被截断成 0）。返回 0 或负数 = 本次不跑 |
| `runner()` 抛异常 | **不推进** `last_run_at`：下一个 tick（默认 60s）就重试，而非等满一个 interval。这是刻意的——维护类任务遇瞬态错误应尽快恢复；代价是持续失败会按 tick 频率重试并打日志 |
| 运行期 `register_job()` | 首个 tick 只记录「见过」并初始化 `last_run_at`，**不会**立刻触发；间隔走完后正常执行 |

## 5. 账号池：邮箱唯一键与判重

### 5.1 插件里（推荐）

```python
# 判重
if self.is_email_registered(email):
    return  # 已注册，跳过

# 落库（自动规范化邮箱 + 进本平台库）
self.save_account(account)

# 平台库会话
with self.db_session() as session:
    ...
```

### 5.2 任务层（已自动生效）

`api/tasks.py` 在**指定邮箱**时先判重，命中即 `skipped`。批量自动建邮的场景
不判重（每次都是新邮箱）。

### 5.3 语义细节

```python
from core.db import account_repository

# 默认：失效账号也算已注册（邮箱已被占用，重复注册没意义）
account_repository.is_registered("foo", "a@x.ai")                    # True

# 失效账号视为可重试
account_repository.is_registered("foo", "a@x.ai", include_invalid=False)  # False

# 批量（一次查完，避免 N 次查询）
account_repository.registered_emails("foo", emails)                  # set[str]
```

## 6. 分库

```bash
# 只给需要的平台分库，其余透明回落默认库
DATABASE_URL_FOO=sqlite:///data/foo.db
DATABASE_URL_GROK=sqlite:///data/grok.db
```

```python
from core.db import platform_session, platform_engine, platform_session_dep

with platform_session("foo") as session: ...
engine = platform_engine("foo")

@router.get("/x")
def endpoint(session: Session = Depends(platform_session_dep("foo"))): ...
```

细节见 [DATABASE_MODULARITY.md](DATABASE_MODULARITY.md)。

### 6.1 邮箱池默认分库（icloud / outlook）

两个邮箱池**默认就分库**，无需任何环境变量：

| 平台键 | 库文件 | 表 |
|--------|--------|-----|
| `icloud` | `data/platforms/icloud.db` | `icloud_accounts`、`icloud_aliases` |
| `outlook` | `data/platforms/outlook.db` | `outlook_accounts` |

它们不是注册平台而是**邮箱来源**，被多个注册流程共用；单独成库才能一个文件
备份、一个文件清空。定义在 `core.paths.DEFAULT_PLATFORM_DB_FILES`，由
`parse_platform_database_urls()` 注入，优先级：

```
内置默认分库  <  PLATFORM_DATABASE_URLS (JSON)  <  DATABASE_URL_<PLATFORM>
```

显式配置可以覆盖（比如把 icloud 池指到别的盘），不会被内置默认值锁死。

**加新邮箱池时**：在 `DEFAULT_PLATFORM_DB_FILES` 加一行，并在
`core/db/__init__.py` 的 `MAILBOX_POOL_TABLES` 里登记它的表 —— 否则
`init_db()` 会把它当注册平台，在池库里建出一张空的 `accounts`。

**坑：遍历平台库找账号要跳过池库。** 池库里没有 `accounts` 表，直接遍历
`configured_platforms()` 会撞上 `no such table: accounts`。用
`core.db.repository.account_platform_keys()`（已排除池库），别自己写循环。

```python
from core.db.repository import account_platform_keys

for key in account_platform_keys():          # 只含真正的注册平台
    with platform_database_registry.session_for(key) as sess: ...
```

## 7. 验证清单

新增平台/流程后，建议按顺序验证：

```bash
# ① 语法与导入
python -c "from core.registry import load_all, list_platforms; load_all(); print(list_platforms())"

# ② 契约完整（基类抽象方法都实现了）
python -c "
from core.registry import get, load_all; load_all()
p = get('foo')()
print([m for m in ('register','check_valid','get_platform_actions','execute_action','get_quota') if callable(getattr(p,m,None))])
"

# ③ 邮箱渠道已注册（新增渠道后必查）
python -c "from core.base_mailbox import available_providers; print(available_providers())"

# ④ 数据层
python -m pytest tests/test_db_modular.py -q

# ⑤ 全量回归
python -m pytest tests/ -q

# ⑥ 端到端（真实网络，按平台写脚本放 scripts/）
python -m scripts.<your_e2e_script>
```

## 8. 常见坑

| 坑 | 说明 |
|----|------|
| 忘了加 `SUPPORTED_PLATFORMS` | 插件被静默忽略，`load_all()` 里什么都看不到 |
| 新增邮箱渠道后忘了在 `channels/__init__.py` import | 渠道「文件存在但查不到」，`create_mailbox` 报未知 provider |
| `create_all(tables=...)` 传了模型类 | 必须传 `Model.__table__`，否则报 `AttributeError: name` |
| 分库后写裸 `select(AccountModel)` | 会漏掉分库平台的数据；改用 `account_repository` |
| 自写轮询循环等验证码 | 无法响应任务控制（停止/跳过）；用 `_run_polling_wait` |
| `MailboxAccount` 丢了 `account_id` | 收不到验证码（凭证丢失） |
| 在 `core/` 里 import `platforms/`、`modules/`、`services/` | 破坏依赖方向；公共能力应放 `core/`，依赖业务的放 `modules/`。唯二例外是 `core/registry.py` 与 `core/mailboxes/registry.py`（加载器） |
| 按 `core.base_mailbox.X` 打桩时改到了实现处 | `core.base_mailbox` 是门面，`create_mailbox` 经它转发；打桩点保持在那里，否则桩不生效 |
| 业务周期任务硬编码进 `core/scheduler.py` | core 会反向依赖 services；改用 `register_job(...)` 由业务侧自注册 |
| 平台库锁内重入 `engine_for` | `_lock` 非重入，会自死锁（分库注册表已规避，新增代码注意） |
