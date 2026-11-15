# Task 4 — 代理域：池、探测、均衡、绑定
状态: 完成

审计对象：`api/proxies.py`、`core/proxy_pool.py`、`core/proxy_utils.py`、
`services/panel_proxy_binding.py`（`services/proxy/**` 不存在）。
Evidence 方式：临时库（`DATABASE_URL`/`DATA_DIR` 指向 tmpdir，与 `tests/conftest.py`
同法）+ 直接调用被测函数；探活用 monkeypatch 的 `requests.get` 假响应，
**全程没有连接任何真实代理**。既有测试作为第二类证据（给名字 + 断言）。
凭据一律打码为 `socks5h://***:***@host:port`。

## 结论

> **重要：审计期间仓库被并发修改。** 我读到的 `services/panel_proxy_binding.py`
> 在 2026-11-14 21:13 被另一个 agent 改过（`ensure_grok2api_egress` 签名修好 + 补了
> 一条测试）；`core/proxy_pool.py` / `api/proxies.py` 在我审计窗口内没有编辑
> （mtimes 分别为 07:23 / 09:00，仍是未提交的工作区版本）。下面每条发现都标注
> 了"读到的版本"与"复验时的版本"，T4-1 因此从"要修"变成"已被修，供追溯"。

代理池的**绑定侧**（`rebalance` / `rebind` / `resolve_for_account` / 打码）质量较好，
且被测试钉得比较密（dry-run 不写库、按 id 定位、`update_extra` 不复活账号、
`from/to` 打码）。**探测侧与惩罚侧有实质缺陷**，且两条都落在"没有任何测试覆盖"的区域：

- 期间的活跃缺陷：`ensure_grok2api_egress` 的公开包装函数把 `client` 传给了一个
  `(api_url, api_key, proxy_url, timeout=)` 的实现 —— **每一次**带代理的 grok2api
  接入都会抛 `TypeError`（T4-1，Important，已复现）。它被
  `grok2api_upload_proxy_enabled` 开关门控（默认关），所以默认部署不受影响；
  但一旦打开，grok2api 的出口绑定 **100% 失败**，异常在
  `platforms/grok/plugin.py:900` 被宽 `except` 吞成"接入失败"。
  **审计期间已被并发修复，我独立复验通过**（`ingest_sso` 现在返回
  `(True, '已接入（web + 代理）')`）。保留这条是因为它证明了本任务的核心命题：
  这类"有实现、零覆盖"的分支摔坏时，测试套件不会有任何反应。
- 其次：`check_all` 把"探活目标 `httpbin.org` 不可用"与"代理坏了"混为一谈，
  且**单次探测即停用**；连续 5 次全局故障（一个 HTTPS 端点抖动就够）就能把
  整个代理池停用、`get_next()` 返回 `None`（T4-2，Important）。Grok 会因此显式
  判失败并让"地址未消耗"；ChatGPT 没有这层守卫，会静默退化成直连。
- `report_fail` 的停用判据 `success_count == 0` 是"终身"条件：一个曾经成功过一次的
  代理此后**永远不会**被 `report_fail` 停用（10 次连续失败实测仍 active 且仍被选中）。
  这是**语义反转**：docstring 与 UI 说的"0 成功 + 5 次失败才拉黑"被实现成了
"终身 0 成功"，于是最该被拉黑的"曾经好、现在死"的代理恰恰永远拉不黑
（T4-3，Important：缺陷 + 该分支零覆盖）。

## 复现环境（下面所有片段共用）

片段都是「粘贴即跑」，跑在**临时库**里，不碰 `data/`、不碰任何真实代理：

```
cd /data/workspace/Register && DATA_DIR=$(mktemp -d) DATABASE_URL=sqlite:///$(mktemp -d)/t.db \
  .venv/bin/python -c '
import core.config_store; from core.db import init_db; init_db()
from sqlmodel import Session; from core.db import ProxyModel, engine
from core.proxy_pool import ProxyPool; proxy_pool = ProxyPool()
def add(url, tags="", active=True, ok=0, fail=0):
    with Session(engine) as s:
        r = ProxyModel(url=url, tags=tags, is_active=active, success_count=ok, fail_count=fail)
        s.add(r); s.commit()
# ↓↓↓ 下面各条的片段贴到这里 ↓↓↓
'```

（真实调用时把上面最后一行换成该条发现里的片段；`add` / `proxy_pool` 已就绪。）

## 发现

### T4-1 · Important（**审计期间已被并发修复**，本审计复现过缺陷、也复验了修复） · `ensure_grok2api_egress` 公开包装签名与实现不匹配

**阅读时刻（2026-11-14 20:57 UTC）的状态**：`services/panel_proxy_binding.py`

- 公开包装 `ensure_grok2api_egress(api_url, api_key, proxy_url, *, timeout=20)`
  （照抄 Sub2API 那一版），函数体把这三个值转发给
  `_ensure_grok2api_egress_unlocked(api_url, api_key, proxy_url, timeout=timeout)`；
- 实际实现 `_ensure_grok2api_egress_unlocked(client, proxy_url, *, scope="grok_web")`
  —— 收的是**客户端对象**，没有 `api_key`、没有 `timeout`。

**缺陷已复现**（当时，无网络、无真实代理）：

```
Python 3.12.12
>>> inspect.signature(ensure_grok2api_egress)
(api_url: 'str', api_key: 'str', proxy_url: 'str', *, timeout: 'int' = 20)
>>> Stub().ingest_sso("sso", "u@example.com", nsfw=False, proxy="socks5h://***:***@10.9.9.9:1080")
TypeError: ensure_grok2api_egress() missing 1 required positional argument: 'proxy_url'
```

爆炸半径（当时）：`ingest_sso` 的 3 个调用方全受影响 ——
`platforms/grok/plugin.py:888`（注册后自动接入，异常被 `:900` 的宽 `except` 吞掉，
只写 `grok2api_ingested=False`）、`platforms/grok/plugin.py:1051`（单账号"上传"，
返回 `grok2api 上传异常: TypeError: ...`）、`services/panel_push.py:376`
（面板推送，异常冒泡）。开关门控已核实：`grok2api_upload_proxy_enabled` 未设置时
`upload_proxy_for("grok2api", …)` 返回 `""`（默认不触发）；置 `"1"` 后必炸 ——
所以是"打开这个开关，功能就 100% 死"，不是 Critical。

验证缺口（缺陷得以存活的原因）：`tests/test_panel_proxy_binding_concurrency.py:3`
的 docstring 声称覆盖 `ensure_grok2api_egress`，但当时文件里只有 Sub2API 的一条用例，
grok2api 分支**没有任何测试调用过**。

**当前状态（2026-11-14 21:13 UTC，非本审计所做）**：`services/panel_proxy_binding.py`
已被并发修改（mtimes 21:13:05，`git diff` 可见），包装改成
`ensure_grok2api_egress(client, proxy_url, *, scope="grok_web")` 并转发
`_ensure_grok2api_egress_unlocked(client, proxy_url, scope=scope)`；
`tests/test_panel_proxy_binding_concurrency.py::Grok2ApiEgressWrapperTests::test_public_wrapper_accepts_the_client_form_used_by_callers`
同时新增（`impl.assert_called_once_with(client, PROXY_URL, scope="grok_web")`）。

**本审计对该修复的独立复验**（不是引用它自己的测试）：

```
>>> Stub().ingest_sso("sso", "u@example.com", nsfw=False, proxy="socks5h://***:***@10.9.9.9:1080")
(True, '已接入（web + 代理）')
调用序: GET /egress-nodes?scope=grok_web -> POST /egress-nodes -> POST /egress-nodes/node-9/accounts
```

即"查 → 建 → 绑账号"三段都走到了。`python -m pytest tests/test_panel_proxy_binding_concurrency.py -q` → `2 passed`。

**给上游的结论**：这条是真实缺陷（有时间戳证据），已被别人修掉，**当前代码不需要再修**；
但它揭示的模式仍在别处存在 —— 这正是下面 T4-2/T4-3 的处境（有实现、无测试）。
剩余建议只有一条：把该用例从"断言转发参数"加强成"断言对端只建一条 + 拿到 node_id"，
否则下次签名漂移时它只会告诉你"转发变了"，不会告诉你"绑定断了"。

### T4-2 · Important · `check_all` 一次探测即停用 + 探活目标单点故障 → 整个池可被一次端点抖动清空

`core/proxy_pool.py:455-476`：每条代理 `requests.get("https://httpbin.org/ip", ..., timeout=8)`，
`status_code == 200` → `report_success`，**否则**（含非 200 与任意异常）→ `report_fail`。
`report_fail`（`:89-96`）在 `success_count == 0 and fail_count >= 5` 时置
`is_active=False`。于是：

- 从未成功过的代理，**5 次全局探活故障**就被全部停用（不是 5 次真实代理故障）；
- `get_next()`（`:49-79`）只取 active → 返回 `None`。

复现（假 `requests.get` 恒返回 503，两条 in-pool 代理）：

```
import requests
add("socks5h://***:***@10.9.0.1:1080"); add("socks5h://***:***@10.9.0.2:1080")
requests.get = lambda *a, **k: type("R", (), {"status_code": 503})()
for _ in range(5): proxy_pool.check_all()
print(proxy_pool.get_next())   # run5 之后 -> None
```

实测：run1..4 `{'ok': 0, 'fail': 2}` 且两行仍 `is_active=True`；**run5 两行同时
变成 `False`**，随后 `get_next()` 返回 `None`。

爆炸半径：`get_next()` 返回 `None` 时注册链路 `_get_proxy()`（`services/task_runners.py:478-495`）
返回 `None`，于是 `_proxy` 为空。两个平台结局不同：**Grok** 在
`services/task_runners.py:610-626` 显式判失败并说明"没有可用出口…本轮放弃"
（不会直连）；**其它平台（ChatGPT）没有这个守卫** —— 全仓只有这一处
`if not _proxy`，所以 `_proxy=None` 会让该轮**直连出网**，而 `ChatGPT` 注册
本来靠代理做的出口隔离就静默消失了。触发它只需要界面上点一次"检测全部代理"
+ 一个第三方端点抖动。靠 `POST /proxies/reactivate` 或再点一次是能
恢复的（`api/proxies.py:229-242` + `frontend/src/pages/Proxies.tsx:101`，前者已被
`tests/test_proxy_reactivate_endpoint.py::test_real_repository_is_used` 钉住：
`assertTrue(reloaded.is_active)` / `fail_count == 0`），所以是"可恢复的可用性中断"，
不是永久损坏 —— 但对"一次点击 + 一个第三方端点抖动"的放大倍数说，Important 合适。

次要同源问题（同一函数）：

- **没有并发**：`for p in proxies` 串行 + 每条 8s 超时。UI 侧自己写死了这个认知
  （`frontend/src/pages/Proxies.tsx:245-247`："每条约 8 秒…20 个代理要 160 秒"，
  轮询 deadline 也按 `n*10+30` 秒算）。代理数 100 → 最坏 13 分钟。
- **没有部分失败聚合**：`results` 只有 `{"ok": n, "fail": n}`，不返回"哪条失败了、
  为什么失败"；而 `api/proxies.py:245-248` 把它丢进 `BackgroundTasks` 且**丢弃返回值**，
  `POST /proxies/check` 只回 `{"message": "检测任务已启动"}`。一次全局故障后，
  用户从 API 拿不到任何"这 5 次失败是同一类错误（目标是 503，不是代理坏了）"的证据。
- 状态覆盖：`check_all` 无条件 `report_success` → `is_active=True`，会把管理员用
  `PATCH /proxies/{id}/toggle` **手工停用**的代理重新启用（实测：一条手工
  `is_active=False` 的代理，跑一次 `check_all` 后变回 `True`）。手工意图被静默覆盖。

修法：把"探活目标的可用性"与"代理的可用性"分开 —— 探测前先确认目标可达
（或使用多目标/本地回环式判据），只把"经由代理的请求失败而直连成功"记作代理失败；
`check_all` 改为并发（`ThreadPoolExecutor`，上限 N）+ 每代理结果明细
`{"url": redacted, "ok": bool, "status": …, "error": …}`；把明细写进任务日志或
`last_checked` 旁的字段，`POST /proxies/check` 至少回一个 task id 可查；
`report_success` 在 `check_all` 里不要无条件置 `is_active=True`（保留手工停用）。

### T4-3 · Important · `report_fail` 的 `success_count == 0` 让"曾经成功过"的代理永不被停用（与 T4-2 相反的失败模式）

`core/proxy_pool.py:94`：`if p.fail_count > 0 and p.success_count == 0 and p.fail_count >= 5`。
`success_count` 只增不减（`report_success` / `reactivate_all` 才动，后者清零 `fail_count`
但不清 `success_count`），所以这是**终身**条件：只要这条代理在历史上成功过一次，
`report_fail` 永远不会把它摘掉，无论后面连续失败多少次。

复现：

```
u = "socks5h://***:***@10.8.0.1:1080"
add(u, ok=1)                                  # 历史上有 1 次成功
for _ in range(10): proxy_pool.report_fail(u)
print(proxy_pool.get_next())                  # 仍然返回这条
# 实测库内: (id=1, is_active=True, success_count=1, fail_count=10)
```

对比 `tests/test_proxy_pool.py::test_report_fail_disables_never_successful_proxy_after_threshold`
（`fail_count=4` → 一次报错后 `fail_count == 5` 且 `is_active is False`）和
`test_report_fail_matches_legacy_proxy_after_normalization`（首次失败
`fail_count == 1` 且 `is_active is True`）—— 两条测试只钉了"从未成功"这一侧，
**"曾成功但已彻底坏掉"这一侧没有测试，也没有外部手段可触发停用**（唯一的
"当场摘掉"通道是 `report_proxy_error`，它由 `looks_like_proxy_error` 文本匹配
决定，见 T4-4）。

爆炸半径：绑定/复用路径会一直把账号钉在这条死代理上 —— `resolve_for_account`
（`:403`）对"在可绑定池里且 `is_available()`"的绑定是**原样保留**，而
`is_available()` 读的就是 `is_active`；于是 `register_proxy` 指向的代理长期坏掉时，
账号每次复用都会先撞一次失败才可能被 `report_proxy_error` 摘掉。

修法：把判据改成"**连续**失败"而不是"终身零成功" —— 例如
`p.fail_count >= 5 and (p.success_count == 0 or consecutive_fail >= 5)`，需要加一列
`consecutive_fail`（`report_success` 清零、`report_fail` +1）；或者改用比率 +
样本量下限（`fail/(succ+fail) > 0.95 and total >= 20`）。补一条测试：
`success_count=3, fail_count=4` → 再 `report_fail` 一次后 `is_active is False`。

### T4-4 · Minor（误判已复现，下游影响为读码推断） · `looks_like_proxy_error` 把裸 `"407"` 当代理标记

`core/proxy_utils.py:223-246` 的 `_PROXY_ERROR_MARKERS` 含裸 `"407"`。任何包含
子串 `407` 的错误文本都会命中：毫秒数、UUID 片段、订单号。

复现（纯函数，无副作用）：

```
from core.proxy_utils import looks_like_proxy_error as f
f("task finished in 407ms")             # -> True
f("验证码等待超时 timeout after 1407ms")  # -> True
f("uuid 3f407a1c-1")                    # -> True
```

注意定级：**误判本身（纯函数）已复现**；"误判 → 摘好代理 → 换账号绑定"这条
下游链是读码推断（`services/task_runners.py:779-793`、`:1096-1101`），
未做端到端复现（要构造一个含 `407` 的失败任务），因此按 Minor 记。

下游（读码）：命中后调用方执行 `report_proxy_error(url)`（`core/proxy_pool.py:325-341`）
—— **一次命中即 `is_active=False`**，绕过 T4-3 的阈值；在
`services/task_runners.py:1098-1099` 还会顺带 `rebind()` 把账号换到别的出口
（对"同一账号出口 IP 稳定"的诉求是反向的）。反向代价：这条口径本来是为了
"窄一点"（docstring 说"宁可窄一点（不含裸 timeout）"），裸 `407` 恰恰相反。

修法：把 `"407"` 换成 `" 407 "` / `"http 407"` / `"status 407"` 之类带边界的模式
（或改用正则 `\b407\b` 且要求同时出现 `proxy|tunnel|connect`）。补测试：正例
（`"HTTP 407 Proxy Authentication Required"`）与反例（上列三条）各一组。

### T4-5 · Minor · `get_next` 的"成功率"排序是常量偏置，不是负载均衡（且并列时偏向固定顺序）

`core/proxy_pool.py:72-79`：按 `success/(success+fail)` **降序排序后取模轮转**。
`sorted` 是稳定的，未使用过的代理得分是 `0/max(0,1) = 0.0`，永远排在已用过的
代理之后；实际效果是"每 N 次里给好代理 1 次、给坏代理 N-1 次"，且顺序固定。

复现：3 条代理其中 1 条 `50/0`（100%）、1 条 `0/4`（0%）、1 条 `0/0`，各取
300 次：份额 **100 / 100 / 100**（健康代理只占 33%）。这解释了一个真实症状：
死但仍在阈内的代理不会被"成功率"排除，只要它还 `is_active` 就均摊。

同时 docstring（`:50`）说"biased toward higher success rate"，与实现"严格轮转"
不符 —— 读者会以为有偏置。

修法：改成加权随机（权重 = `(succ+1)/(succ+fail+2)` 或拉普拉斯平滑后的成功率），
或对 `fail/(succ+fail)` 高于阈值（如 0.5 且有 ≥5 样本）的代理直接跳过。

### T4-6 · Minor · `POST /proxies/bulk` 的查重按**原始串**比对，同一代理可入池两次

`api/proxies.py:72-85` 用 `proxy_repository.find_by_url(url)`（`core/db/proxy_repository.py:56-73`，
`WHERE ProxyModel.url == wanted` **精确串**），而 `POST /proxies`（单条，`:62-69`）
用的是同一个精确比对 —— 但两条路径都**没有** `normalize_proxy_url`。池子其余部分
（`ProxyPool._find_by_url:24-47`、`bound_counts:120`、`rebalance:293`）全部按归一化
比对，于是同一物理代理可以用两种写法入库两次。

复现（TestClient + 临时库）：

```
POST /api/proxies/bulk {"proxies": ["socks5://***:***@10.4.0.1:1080"]}  -> {"added": 1}
POST /api/proxies/bulk {"proxies": ["socks5h://***:***@10.4.0.1:1080"]} -> {"added": 1}
# 库里两行：socks5://… 与 socks5h://…（同一 host:port，同一凭据）
```

同一条 `socks5h://` 再走单条 `POST /proxies` 会被 400 挡住（`proxy 已存在`），
说明拦截逻辑本身是"精确串"口径，只是 bulk 漏了归一化。旧式写法
（`10.4.0.2:1080:***:***`）与 `http://***:***@10.4.0.2:1080` 同样能各入一行。

爆炸半径：两行独立记 `success_count`/`fail_count`，`_find_by_url` 的"三级回退"
（精确→归一化→逐条归一化）只能命中**第一行**，第二行的统计永远是 0 —— 成功率
显示与 `pick_bindable` 的负载计数（按归一化 key 合并）对不上；且同一条出口在
选路里被当成两条，等于双倍权重。

修法：`add_proxy` / `bulk_add_proxies` 在入库前 `url = normalize_proxy_url(url)`，
查重也用归一化串；更稳的是在仓储层 `add()` 里做归一化，或在 `proxies.url` 上加
唯一索引（迁移）。

### T4-7 · Minor · `POST /proxies/balance` 的 `changed` 数被 `[:200]` 截断但计数不截断（真实执行时同样只回前 200 条明细）

`api/proxies.py:213-226`：`result["changed"][:200]` 生成 `items`，但 `changed` 字段
返回 `len(result["changed"])` 全长。实测 250 条变更 → `changed: 250, items: 200`。
前端只根据 `changed` 弹"将调整 N 个账号"（`frontend/src/pages/Proxies.tsx:358`：`将调整 ${res.changed} 个账号的绑定`；`:378` 同样用 `res.changed`），
明细表少 50 行却没有提示。

修法：要么在返回体里带 `truncated: true` + `shown: 200`，要么分页取明细。

### T4-8 · Minor · `rebalance` 的真实执行**不可逆**（无事务、无 undo、无对账）

`core/proxy_pool.py:268-323` 逐号 `update_extra` → 逐条独立 commit；中途异常
会留下"一半已挪、一半没挪"的状态，且**没有回滚入口**。路由层
`api/proxies.py` 全部 8 条路由里没有 `undo`/`revert`（实测：
`any("undo" in r.path ...) == False`）。`from` 字段记了旧值但只用于展示。

**dry-run 与实跑的一致性已实测通过**（这点比预想的好）：5 个不合规账号上
`rebalance(dry_run=True)` 与 `rebalance(dry_run=False)` 给出的
`(email, to)` 序列**完全相同**（`identical: True`），因为挑选是确定性的
`min(bindable, key=(count, url))`（`:298-301`）。注意这与 `pick_bindable`
（`resolve_for_account` 走的那条，`:191-193` 用 `random.choice` 打散并列）
**不是同一个选择器** —— 均衡预演可信，而"账号首次绑定"不可预演。

修法：给 `rebalance` 加一次事务（或先写一份 `[{id, from, to}]` 的 undo 记录，
再提供 `POST /proxies/balance/undo`）；至少让中途失败可重入地对账
（重跑 `rebalance(dry_run=True)` 即可看到剩余未迁移的账号）。

### T4-9 · 不上缺陷级 · `POST /proxies/check` 无锁，两次点击可并发两轮 `check_all`（未复现）

`api/proxies.py:245-248` 只有 `background_tasks.add_task(proxy_pool.check_all)`，
`check_all` 自身没有 `threading.Lock`（对比 `_ENSURE_LOCK` 在
`services/panel_proxy_binding.py:58` 的做法）。并发两轮会让每条代理被
`report_fail` 双记。**没有复现并发**（要真起两个后台任务并卡在 8s 超时里），
所以按规矩降为验证缺口，不计 Critical/Important。

## hypothesis

（未复现，不作为 Critical/Important）

0. `POST /proxies/check` 并发两轮 `check_all` 会让同一条代理的 `fail_count` 被双记
   （无锁，见 T4-9）。未复现并发时序，仅读码。

1. `rebalance` 的 `bound_counts()` 在**遍历账号的同时**被 `counts[target]["total"] += 1`
   就地改写（`core/proxy_pool.py:287,310`），这与"每次分配都重新读一次计数"的
   docstring（`:276`）不一致：它读的是**进入循环前的快照**，只是本地递增。
   在单进程内结果正确（因为本地递增模拟了写回），但**并发**下（例如均衡执行期间
   另一个注册任务正在给账号绑新代理）本地计数会与库里的真实分布漂移，最终
   分布可能超过 `abs(a-b) <= 1`。没有复现（需要并发写），列为假设。
2. `resolve_for_account` 在"可绑定池非空、`saved` 为空"时走 `pick_bindable`
   （`:405-407`），它内部 `random.choice` 打散并列；而注册链路
   `services/task_runners.py:723` 拿到的 `switched` 只用于日志。若 `pick_bindable`
   返回的正好是 `saved`（`exclude=saved` 已排除），逻辑上不会发生 —— 未发现问题，
   仅记录已核。
3. `_balance_settings` 把 `config_store.get(...)` 的 `ImportError` 留给调用方冒泡
   （`core/proxy_pool.py:135-153`，并被
   `tests/test_proxy_pool.py::test_balance_settings_raises_when_config_symbol_is_missing`
   钉住）。若 `core.config_store` 在运行时被替换成缺符号的模块，
   `pick_bindable` → `resolve_for_account` 会让**注册链路**抛 `ImportError`
   （不是降级）。这是刻意设计（注释说明了），但在注册热路径上，一个配置层
   形状错误会变成"注册全挂"而不是"均衡退化成随机"。未复现为故障，仅提示风险。

## 覆盖清单（选路 / 失败惩罚 / 均衡 / 探测 / 绑定切换）

| 检查点 | 结论 | 证据 |
|---|---|---|
| `get_next` 全部停用 | 返回 `None`（带 tag 也一样；tag 过滤先作用于 active 列表） | 临时库实测：两条 `is_active=False` → `get_next()` / `get_next(tag="register")` 均 `None` |
| `get_next` 标签为空回退 | 回退到全部 active，且有 `logger.info`；回退只在"有 active"时发生（全空仍 `None`） | `tests/test_proxy_tags_balance.py::test_falls_back_when_no_tagged_proxy`（`assertTrue(seen)`、`assertNotIn` 被删代理）+ 本审计读码复核 `core/proxy_pool.py:62-71` |
| 负载指标是否持续选中"死但 active" | **是**：成功率降序后轮转，`0/4` 与 `100%` 与 `0/0` 各占 1/3 份额 | 300 次采样：`{'10.7.0.1':100,'10.7.0.2':100,'10.7.0.3':100}`（T4-5） |
| `report_fail` 单次失败是否足够 | **不够**（首次失败仅 `+1`，仍 active）；阈值是 `success_count==0 且 fail>=5`，第 5 次才停用 | `tests/test_proxy_pool.py::test_report_fail_matches_legacy_proxy_after_normalization`（`fail_count==1, is_active is True`）+ `::test_report_fail_disables_never_successful_proxy_after_threshold`（`fail_count==5, is_active is False`）；临时库实测 4 次 active / 第 5 次 False |
| 曾成功过的代理能否被 `report_fail` 停用 | **不能**：终身条件；10 次连续失败仍 active 且仍被 `get_next` 选中 | 临时库实测 `(success=1, fail=10, is_active=True)`（T4-3） |
| 复活通道 | `POST /api/proxies/reactivate` → `reactivate_all()` → `is_active=True`、`fail_count=0`；UI 有按钮 | `tests/test_proxy_reactivate_endpoint.py::test_real_repository_is_used`（`assertTrue(reloaded.is_active)`、`fail_count==0`）+ `frontend/src/pages/Proxies.tsx:101`（`reactivateAll`）、`:540`（按钮） |
| 复活是否可达 | 可达（API + UI 都在）；但**手工停用与自动停用共用 `is_active`**，`reactivate_all` 会把手工停用的一起放开 | 读码 `core/proxy_pool.py:433-453`（无"自动/手工"来源标记）+ 实测 `check_all` 也会覆盖手工停用 |
| `rebalance` dry-run 不写库 | 成立 | `tests/test_proxy_tags_balance.py::test_dry_run_changes_nothing`（`before == after`） |
| `rebalance` 只挪不合规 + 均衡 | 成立（`abs(a-b) <= 1`） | `::test_rebalance_moves_only_non_compliant_and_balances`（`assertLessEqual(abs(a-b),1)`）+ `::test_compliant_bindings_are_not_touched` |
| `rebalance` 可逆性 | **不可逆**：逐条 commit，无 undo/事务（T4-8） | 读码 `core/proxy_pool.py:312-322`；接口 `api/proxies.py:196-226` 无 undo |
| dry-run 是否泄漏凭据 | **不泄漏**：`from`/`to` 都过 `redact_proxy_url` | `tests/test_proxy_balance_redaction.py::test_balance_response_does_not_leak_the_password`（`assertNotIn(SECRET)`、保留 `10.0.0.2`）；实测 250 条 `changed` 的响应里 `"secret-pw" in body == False` |
| `rebalance` 明细截断 | `changed` 计数不截断、`items` 只前 200（T4-7） | 实测 `changed: 250, items: 200` |
| `check_all` 并发 | **串行**，每条 8s 超时（T4-2） | 读码 `core/proxy_pool.py:461-467`；UI 注释与轮询 deadline `frontend/src/pages/Proxies.tsx:245-285` 确认这一认知 |
| `check_all` 超时 | 单条 8s；无整体超时/无并发上限 | 同上 |
| `check_all` 部分失败聚合 | **不足**：只回 `{"ok","fail"}`，API 丢弃；无逐条原因 | 读码 `core/proxy_pool.py:460,477`、`api/proxies.py:245-248`；实测 `{'ok':0,'fail':2}` 不含任何 url/reason |
| `check_all` 对状态的影响 | 非 200/异常 → `report_fail`（5 次全池停用）；200 → `report_success`（无条件 `is_active=True`，覆盖手工停用） | 临时库实测两次（T4-2） |
| 绑定切换是否可能读到过期快照 | **不会**：注册成功路径不用预取快照取 `register_proxy`，而是 `resolve_for_account(_proxy or "")` 直接按库现值决策；预取快照里的每一条还过 `is_available()` | `tests/test_register_binding_proxy_switch.py::test_register_only_proxy_is_switched_to_a_bind_tagged_proxy`（`bound in {BIND_A,BIND_B}`、日志含"绑定代理切换"）+ `::test_proxy_tagged_register_and_bind_is_kept`（保留）+ `::test_no_bind_pool_keeps_register_proxy_for_old_deployments`（保留）；`services/task_runners.py:452-495,717-733` |
| 绑定切换是否会把统计记到错误的代理行 | **不会**：`rebind` 按 `account_id`（或 email+platform）定位，写用 `update_extra`；`report_success/report_fail` 用 `_find_by_url` 三级回退 | `tests/test_proxy_cross_platform_and_writeback.py::test_rebind_by_id_only_touches_that_platform`、`::test_rebind_by_email_refuses_when_email_exists_on_two_platforms`、`::test_update_extra_does_not_insert_missing_rows`（无复活的断言）+ `tests/test_proxy_pool.py` 的两条归一化用例（`success_count == 1` / `fail_count == 1` 落在正确的行上） |
| 绑定切换是否跨平台写 | **不会**（按 id/platform；同邮箱跨平台时拒绝按 email 猜） | 同上 `test_rebind_by_email_refuses_when_email_exists_on_two_platforms`（返回 `("", "")`）；`core/proxy_pool.py:232-241` |
| 面板代理绑定（Sub2API）幂等/并发 | 成立（串行锁 + 幂等键 `(host,port,username)`） | `tests/test_panel_proxy_binding_concurrency.py::test_two_concurrent_uploads_create_only_one_proxy_record`（`len(created) == 1`、两方拿到同一 `proxy_id`） |
| 面板代理绑定（grok2api egress） | 缺陷已复现，**审计期间被并发修复**；修复后独立复验通过 | 缺陷态：`TypeError: ensure_grok2api_egress() missing 1 required positional argument: 'proxy_url'`；修复后：`ingest_sso(...)` → `(True, '已接入（web + 代理）')`，调用序 `GET /egress-nodes → POST /egress-nodes → POST /egress-nodes/node-9/accounts`；`pytest tests/test_panel_proxy_binding_concurrency.py` → `2 passed`（T4-1） |

### 未覆盖 / 验证缺口（明确列出）

- `grok2api` egress 的"查→建→绑"整条链路：阅读时刻无任何测试（T4-1 之所以能存活）；
  审计期间新增了一条（`Grok2ApiEgressWrapperTests`），但它只断言"转发参数正确"，
  不断言"对端只建一条 + 拿到 node_id" —— 覆盖强度仍不足以拦住行为回归。
- `report_fail` 对"曾成功过的代理"的惩罚语义：无测试（T4-3）。
- `check_all` **函数体**：唯一涉及它的测试是
  `tests/test_api_proxies_and_auth.py::test_check_endpoint_starts_background_task`，
  且它把 `check_all` 整个 mock 掉（`assert check_mock.call_count == 1`）—— 只钉住
  "端点把它排进后台"，函数内部（并发/超时/聚合/停用/覆盖手工停用）零覆盖（T4-2）。
  本审计只能靠 monkeypatch `requests.get` 复现。
- `looks_like_proxy_error` 的 "407" 误报：`tests/test_proxy_utils.py` 有正例
  （`test_playwright_network_errors_are_proxy_errors`）和**一组**反例
  （`test_otp_wait_timeout_is_not_a_proxy_error`，只覆盖 3 条 OTP 超时串），
  没有覆盖"毫秒数/UUID 里含 407"这类（T4-4 建议补的那组）。
- `bulk` 端点与 `normalize_proxy_url` 的一致性：
  `tests/test_api_proxies_and_auth.py::test_bulk_add_skips_blanks_and_dedupes_existing`
  只钉"空行跳过 + 精确串重复"（`added == 1` / `added == 0`），没有"两种写法同一代理"（T4-6）。