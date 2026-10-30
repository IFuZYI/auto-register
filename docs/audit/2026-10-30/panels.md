# panels 审计

[MODE: SANDBOX]

- 仓库：`/data/workspace/Register`，分支 `main`，HEAD `4680c8d`
- 解释器：`.venv/bin/python`（3.12）
- 生产库：`data/account_manager.db`（**只读**；`file:...?mode=ro` 打开，本报告未写入任何一行）
- 方式：静态通读 + 只读 SQL 抽样（1057 行账号）+ 1 个纯内存探针 + 8 个测试文件回归（192 passed，全绿）
- 本报告是唯一写入的文件
- **审计期间 HEAD 前进了两格**（并发修复）：`c2c502b`（`core/db/models_account.py` 补两条索引，该文件行号 +5）、`9630980`（把 F-14 的 `int(uuid)` 改成了 `str(uuid)`）。本报告的行号与证据以基线 `4680c8d` 为准，受影响的条目已在正文标注。

## 范围与定位

任务里写的 `core/panels/` **不存在**。四个适配器与面板编排实际分布如下（共 4626 行）：

| 角色 | 文件 | 行数 |
|---|---|---|
| 面板注册表（契约源头） | `services/panel_registry.py` | 161 |
| 对比 / 匹配 / 四个远端拉取器 | `services/panel_comparison.py` | 1302 |
| 对比缓存 + 本地行装配 | `services/panel_comparison_cache.py` | 333 |
| 推送（本地→远端） | `services/panel_push.py` | 630 |
| 拉回（远端→本地） | `services/panel_sync.py` | 322 |
| 登记表（enrollment） | `services/panel_enrollment.py` | 115 |
| 状态同步（非 CPA 三面板） | `services/panel_status_sync.py` | 250 |
| CPA 状态同步 / 探活 | `services/cliproxyapi_sync.py` | 706 |
| 落库（sync_statuses 写入器） | `services/chatgpt_sync.py` | 495 |
| 面板 HTTP 接口 | `api/integrations.py` | 237 |
| 适配器 CPA | `platforms/chatgpt/cpa_upload.py` | 289 |
| 适配器 Sub2API | `platforms/chatgpt/sub2api_upload.py` | 192 |
| 适配器 chatgpt2api | `platforms/chatgpt/chatgpt2api_upload.py` | 323 |
| 适配器 grok2api | `platforms/grok/grok2api.py`（+ `platforms/grok/upload.py`） | 706 + 197 |
| TLS 策略 | `core/panel_tls.py` | 75 |

---

## 0. 结论摘要

| # | 检查项 | 判定 | 最严重问题 |
|---|---|---|---|
| 1 | 四面板接口契约一致性 | **不一致（4 套各写各的）**：鉴权 3 种、错误判定 4 种、超时 15–60s、重试只有 1 个面板有、TLS 只有 3/4 面板走统一策略 | F-01 / F-02 / F-03 |
| 2 | 推/拉状态机 | **方向判定本身正确，但有一个活锁 + 一个删除顺序错误**：拉回被「登记」闸门锁死（生产库 CPA 0/667 登记）；`delete_old` 在幂等跳过时会删掉唯一记录 | F-06 / F-07 |
| 3 | 上传代理开关 | **语义与文档不符**：代理只写进上传**记录字段**，不用于请求出口；仅 2/4 面板支持；chatgpt2api 的键在 schema 里归错分组 | F-10 |
| 4 | 对比 local_id/remote_id 匹配 | **(平台,邮箱) 键可靠；重复远端记录被静默折叠**，只暴露一条 id（删不干净）；平台清单两处不一致（390 个 Grok 行进 CPA 对比且推不上去） | F-11 / F-13 |
| 5 | 失败隔离 | **两个批量端点没有逐账号隔离**，其中一个必现 500（F-14，基线 `4680c8d`；`9630980` 已修）；推送批次无总时限 | F-14 / F-15 / F-16 |
| 6 | 敏感信息暴露面 | **密钥本身不泄露**（配置接口只回 `_set` 布尔、对比响应不含 AT/RT）；但**面板返回的错误文本会落库并经列表接口下发** | F-18 |

一句话：**方向判定（谁较新动谁）这段是这次审计里质量最高的部分 —— iat 优先、单侧回落、banned 排除都做对了；出问题的是它的外围：登记闸门把「更新本地」锁死、批量端点没有逐账号隔离、以及「推成功后删旧记录」与 chatgpt2api 的幂等跳过语义互相打架。**

---

## 1. 检查项（1）：四个面板的接口契约是否一致

### 1.1 实测对照表

| 维度 | CPA (CLIProxyAPI) | Sub2API | grok2api | chatgpt2api |
|---|---|---|---|---|
| 鉴权 | `Bearer <cpa_api_key>` ¹ | `x-api-key` ² | 用户名+密码换 token ³ | `Bearer <chatgpt2api_api_key>` ⁴ |
| 未配置时 | 上传发 `Bearer `（空）→ 401；状态同步回落默认口令 `cliproxyapi`（`cliproxyapi_sync.py:78`） | 直接报「未配置」 | 直接报「未配置」 | 直接报「未配置」 |
| 分页 | **无**（一次 GET 全量；服务端 `page/page_size` 是 opt-in，`reference/panel/CLIProxyAPI/internal/api/handlers/management/auth_files.go:176-198`） | `page/page_size=100`，`len<100` 停（`panel_comparison.py:779-849`） | `page/pageSize=100`（`panel_comparison.py:884-931`） | `page/page_size=500`，整页重复也停（`panel_comparison.py:1175-1248`） |
| 错误判定 | 只看 HTTP（`raise_for_status` / 200-201） | HTTP 200 **且** `code==0`（`panel_comparison.py:789-794`） | HTTP 200 **且** SSE `created/updated/skipped`（`grok2api.py:265-283`） | HTTP <400 **且** 不是 `errors && !added && !skipped`（`chatgpt2api_upload.py:314-322`） |
| 超时 | 上传 30 / 列表 30 / 探活 30 | 上传 30 / 列表 20 | 登录 15 / 列表 20 / 导入 60 / 导出 30 | 上传 60 / 列表 20 / 导出 60 |
| 重试 | 仅探活（3 次，0.4s，只认连接/超时，`cliproxyapi_sync.py:170-190`） | **无** | **无** | 上传 4 次（连接类异常，sleep 2/4/6，`chatgpt2api_upload.py:262-278`）；导出遇 400 去缺失 id 重试 1 次（`panel_comparison.py:1100-1110`） |
| TLS | `verify_for("cpa"/"cliproxyapi")` | `verify_for("sub2api")` | **无 `verify=` 参数**（默认 True，但忽略 CA bundle） | `verify_for("chatgpt2api")` |


鉴权实现位置（表格里的 ¹²³⁴）：

1. CPA：`platforms/chatgpt/cpa_upload.py:249`、`services/cliproxyapi_sync.py:83`（未配置时上传发空 Bearer，状态同步回落默认口令 `cliproxyapi`，`cliproxyapi_sync.py:78`）
2. Sub2API：`platforms/chatgpt/sub2api_upload.py:160`、`services/panel_comparison.py:784`（只认 `x-api-key`，无 Bearer 兜底）
3. grok2api：`platforms/grok/grok2api.py:187-220`（`/api/admin/v1/auth/login` 换 token，TTL 9 分钟，进程内缓存并复用实例）
4. chatgpt2api：`platforms/chatgpt/chatgpt2api_upload.py:247`、`services/panel_comparison.py:1166`

### F-01（P2）超时/重试矩阵不对称：4 个面板 4 套策略，且只有 chatgpt2api 会重试上传

- 位置：`platforms/chatgpt/cpa_upload.py:263`（30s，0 重试）、`platforms/chatgpt/sub2api_upload.py:164`（30s，0 重试）、`platforms/grok/grok2api.py:250`（导入 60s，0 重试）、`platforms/chatgpt/chatgpt2api_upload.py:262-278`（60s × 4 次）
- 证据：同一批账号被推往四个面板时，一次出口代理抖动对 chatgpt2api 是「等 12s 后成功」，对 CPA/Sub2API/grok2api 是「立刻记失败」，而失败的代价并不对称 —— 失败会被写进 `sync_statuses` 并计入 `failed`（`panel_push.py:239-244`），用户看到的是「推送失败」，再点一次又可能成功。
- 影响：**没有「重试预算」这个概念**：谁重试、重试几次，取决于当初写那个适配器的人。跨面板的可靠性无法用同一套指标衡量（同一网络条件下四个面板的失败率不可比）。
- 建议：在 `core/panel_tls.py` 旁新增 `core/panel_http.py`：统一的 `(timeout, retries, backoff, retry_on)` 配置表 + 一个 `request_with_retry()`，四个适配器全部改走它；重试口径统一为「连接类异常 + 5xx + 429」，4xx 不重试（现在是各写各的）。

### F-02（P3）成功判定口径不一：CPA / Sub2API 会把「HTTP 200 + 业务错误体」记成成功

- 位置：`platforms/chatgpt/cpa_upload.py:271-273`、`platforms/chatgpt/sub2api_upload.py:174-175`
- 证据：

```python
# cpa_upload.py:271
if response.status_code in (200, 201):
    return True, "上传成功"          # 不看响应体
```

```python
# sub2api_upload.py:174
if response.status_code in (200, 201):
    return True, "上传成功"          # 不看 code 字段（列表接口那边是看的，panel_comparison.py:789）
```

- 影响：这两个面板若以「200 + `{code:非0}`」表达业务失败（Sub2API 的列表接口就是这么干的，见 `panel_comparison.py:789-794` 的注释），推送会被记成成功并 `enroll()` 登记 —— 于是「已登记但远端没有」这个 ADR-0002 明确要消灭的自相矛盾状态又回来了，而且拉回方向会因为这个假登记而放行。
- 建议：与列表侧同口径 —— Sub2API 校验 `payload.code == 0`；CPA 校验 `payload.get("status") in ("ok","success",None)` 且无 `error` 键。补两条「200 + 业务错误体 → ok=False」的用例。

### F-03（P2）TLS 策略不一致：grok2api 的两个客户端绕过 `verify_for`，`panel_tls_ca_bundle` / `panel_tls_insecure` 对它完全无效

- 位置：`platforms/grok/grok2api.py:162-168`（`curl_requests.request(... proxies=proxies, impersonate="chrome", timeout=...)`，**无 `verify=`**）、`platforms/grok/upload.py:24-26`（`_http_post`）、`:94-100`（CPA multipart）
- 证据：全仓 `verify_for` 调用点 11 处，覆盖 cpa / sub2api / chatgpt2api / cliproxyapi / comparison / push，**唯独 grok 的两个客户端没有**：

```
$ grep -rn "verify_for" --include=*.py platforms/ services/ | wc -l   # 11
$ grep -n "verify" platforms/grok/grok2api.py                        # （无输出）
```

- 影响：
  1. 自签名 grok2api（内网自建，正是 `panel_tls_ca_bundle` 的目标场景）**永远连不上**，报 `SSLCertVerificationError`，而错误会被 `fetch_panel_raw` 收成 `remote_error`（`panel_comparison_cache.py:277-279`），用户看到的是「面板拉取失败」而不是「证书不被信任」；
  2. 反向也不成立：用户想临时排障设 `panel_tls_insecure=1`，对 grok2api **不起作用**（它本来就在校验）—— 同一份配置对四个面板产生三种行为。
- 建议：`Grok2ApiClient._request` 与 `platforms/grok/upload.py::_http_post/upload_to_cpa` 全部加 `verify=verify_for("grok2api")`；`tests/test_panel_tls_verification.py` 里补一条「枚举所有面板 HTTP 客户端，断言每个都传了 verify」的静态检查（现在是逐点断言，漏点不会被发现）。

### F-04（P3）grok2api 拉取器的 `api_key` 形参被丢弃，面板级凭据覆盖只对 3/4 面板有效

- 位置：`services/panel_comparison.py:849-861`（签名收 `api_key`）、`:884`（`client = Grok2ApiClient.from_config(api_url=api_url)` —— 没传 username/password）、`services/panel_comparison_cache.py:168-170`（`api_key` 位置传的是 `grok2api_password`）
- 证据：

```python
# panel_comparison.py:855-861
def fetch_grok2api_remote_accounts(*, api_url: str = "", api_key: str = "", emails=None):
    """...`api_key` 这个位置实际传的是账号密码..."""
    client = Grok2ApiClient.from_config(api_url=api_url)   # ← api_key 未使用
```

- 影响：`_panel_credentials()` 精心拼出的密码被静默丢掉，实际生效的是 `from_config` 自己再读一遍全局配置。今天不出错（两边同源），但这条契约是假的：任何想「临时指向另一套 grok2api」的调用方（`params` 通道，`plugin.py:975-981` 明确支持）在这条路径上会静默用回全局配置。
- 建议：`from_config(api_url=api_url, password=api_key)`，或把形参改名成 `password` 并显式传；顺手统一四个 fetcher 的签名（现在 CPA/Sub2API 收 `api_key`，grok2api 收的是密码，chatgpt2api 收 key —— 同一个名字三种语义）。

### F-05（P3）三处静默截断：CPA 无分页、grok2api 导出固定 `limit=1000`、凭证抓取上限 200

- 位置：`services/cliproxyapi_sync.py:194-197`（`list_auth_files` 只读一次 `files`）、`services/panel_comparison.py:958`（`export?provider=...&limit=1000` 不分页）、`:621` + `:697` + `:1254-1261`（`_MAX_CREDENTIAL_FETCH = 200`）
- 证据：`_MAX_CREDENTIAL_FETCH` 只在 chatgpt2api 分支打了 warning（`:1256-1261`），CPA 分支（`:697`）**超限时一句话都不说**，只是悄悄不再抓第 201 个账号的凭证。
- 影响：超过 200 个「两边都有」的账号时，第 201 个之后全部落 `unknown_credential`（`panel_comparison.py:120-125`），界面显示「无法比对」，而 `plan_push` 对 `unknown_credential` 是**直接不推**（`panel_push.py:146-151`）。也就是说：**大面板（生产库 667 个 chatgpt 账号）里有一部分账号永远推不上去，且界面不解释为什么**。
- 建议：CPA 分支补同一条 warning；把 `_MAX_CREDENTIAL_FETCH` 做成配置项并在响应里回 `credential_truncated: true`，让前端显示「凭证比对已截断（前 200 个）」；长期方案是分页拉凭证或按需（点开某行时）拉。

---

## 2. 检查项（2）：推送/拉取方向的状态机

### 2.1 实际状态机（含本次发现的两个断点）

```
                     ┌───────────────────────────── push（只走这两条）─────────────────────────────┐
local_only ──push──▶ 上传 ──ok──▶ enroll(cpa/sub2api/chatgpt2api) + updated_at=now ──▶ synced/credential_diff
     ▲                  │
     │                  └──fail──▶ sync_statuses.last_attempt_ok=False（不登记）──▶ 仍是 local_only
     │
credential_diff ──iat(local>remote)──▶ push
                └─iat(remote>local)──▶ pull ──▶ 需要 enrolled_panels 含该面板 ──✗ 未登记 → not_enrolled（F-06）
                                              └─ok─▶ apply_pull + updated_at=now ──▶ synced
```

方向判定本身是**对的**，且比多数实现严谨：

- 先比凭证本体（`compare_credentials`，全同即 synced），再比时间（`compare_credential_time`）；
- 时间**每侧独立**「iat 优先、无则回落该侧记录时间」，避免了「状态回写 touch 了 updated_at 就把方向判反」这个已发生过的事故（`panel_comparison.py:202-252` 的注释记录了 2026-10-05 的实测）；
- `banned` 两侧一起排除（`panel_comparison.py:449-474`）；
- push 只在 `local_only` / `local_newer` 时发生，`remote_newer` 明确拒绝（`panel_push.py:165-172`）—— **「本地旧凭证覆盖远端新凭证」在 push 主路径上不会发生**。

### F-06（P1）拉回被「登记」闸门死锁：生产库 CPA 0/667、grok2api 0/390 已登记，「更新本地」对它们整体不可用

- 位置：`services/panel_sync.py:122-126`（闸门）、`:268`（传 `enrolled_panels(extra)`）、`services/chatgpt_sync.py:100-125`（**只有上传成功才登记**）、`services/panel_enrollment.py:68-76`（`ok=False` 不写登记表）、`services/panel_push.py:506-512`（push 成功才登记）
- 证据（生产库只读统计）：

```
$ .venv/bin/python  # 遍历 1057 行 accounts，解析 extra_json
total {'grok': 390, 'chatgpt': 667}
enrolled:                  {'chatgpt2api': 314}          ← 只有 chatgpt2api 有登记
uploaded-but-not-enrolled: {'chatgpt2api': 353, 'grok2api': 51, 'cpa': 31, 'cliproxyapi': 42}
```

```
# panel_sync.py:122
if enrolled is not None and panel:
    if str(panel).strip().lower() not in {str(p).strip().lower() for p in enrolled}:
        outcome.reason = "not_enrolled"
        return outcome
```

- 影响：**这是本次审计里唯一一个「功能整体不可用」的缺陷。** 死锁闭环如下：

  1. 拉回要求「已登记到该面板」；
  2. 登记只在**上传成功**时写（`chatgpt_sync.py:116`，`panel_push.py:506`）；
  3. 而上传（push）恰好**跳过** `remote_newer` 的行（`panel_push.py:165-172`）—— 也就是唯一需要拉回的那批行；
  4. 于是老账号（登记表引入之前上传的、或远端人工建的）永远进不了第 2 步，第 1 步永远拒绝它。

  生产库现状：CPA 的 667 个 chatgpt 账号**一个都没登记** → `POST /api/integrations/panels/cpa/sync` 对每一行都返回 `not_enrolled`，`pulled=0`，界面上「更新本地凭证」看起来是「没有需要更新的账号」（前端 `PanelComparisonPanel.tsx:610-614` 的提示语正是这句），完全掩盖了真实原因。

- 建议：三选一（推荐 1+3）：
  1. 闸门语义反转：`enrolled` 只在**有登记表数据**时才作为判据，登记表为空视为「未启用登记功能」→ 放行（`panel_enrollment` 的 docstring 本来就写了它是「诊断信息」的补充，不是授权）；
  2. 提供显式「登记」入口（对比页每行的「登记到该面板」/ 批量登记），让用户能自救；
  3. 补一条一次性迁移：对 `sync_statuses[panel].uploaded == True` 的行回填 `panel_uploads[panel]`（生产库可覆盖 31+51+353 行）。

### F-07（P1）`delete_old` 与 chatgpt2api 的「幂等跳过」打架：跳过时删旧记录会删掉唯一那条

- 位置：`services/panel_push.py:230-237`（ok 就删）、`platforms/chatgpt/chatgpt2api_upload.py:314-322`（`added=0, skipped=1` 也算 ok）
- 证据：

```python
# chatgpt2api_upload.py:320
# 注意 `added=0, skipped=1` 仍算成功：那是幂等重传的正常结果。
if errors and not added and not skipped:
    return False, f"上传失败（{summary}）"
return True, f"上传成功（{summary}）"
```

```python
# panel_push.py:230 —— 只要 ok 就删掉「对比时匹配到的那条」
if ok and outcome.remote_id and delete_remote is not None:
    d_ok, d_msg = delete_remote(platform, outcome.remote_id)
```

- 影响：`skipped=1` 的语义是「对端认为这条已存在，**没有新建记录**」。此时再执行删除，删掉的正是对端认为「已存在」的那一条 → **该账号在远端变成 0 条**。下一轮对比变成 `local_only` → 再推 → 这次 `added=1`（因为旧记录已被删）→ 不再删（`remote_id` 为空）→ 收敛。所以不是永久丢失，而是「每轮维护窗口里有一段账号不在远端」的震荡，且 `deleted` 计数会把它记成一次「成功清理」。
- 触发条件：`chatgpt2api` 的导入按邮箱/AT 去重（本项目代码注释与对端实现都这么描述），且自动维护每轮都用 `delete_old=True`（`services/chatgpt_maintenance.py:797`）。生产库有 667 个 chatgpt 账号走这条路，触发面不小。
- 建议：`delete_remote` 的调用条件加上「本次上传**确实新建**了记录」—— 让上传器把 `added/skipped` 透出来（现在被压成一句中文 message），`PushOutcome` 增加 `created: bool`，只有 `created and remote_id` 才删。补一条用例：`upload → (True, "上传成功（新增 0，跳过 1）")` 时**不得**调用 delete。

### F-08（P1）`account_ids` 收窄只在前端实现，后端两条端点都没用 —— 「看到的」仍然不是「被改的」

- 位置：`api/integrations.py:82`（`sync_panel_endpoint(..., account_ids: str = "")`，**函数体从未引用**）、`api/integrations.py:146-150`（`push_panel_endpoint` 根本没有这个参数）；前端确实在传：`frontend/src/components/settings/PanelComparisonPanel.tsx:598`（pull）、`:635`（push）
- 证据：

```
$ grep -n "account_ids" api/integrations.py
35:    account_ids: list[str | int] = Field(default_factory=list)   # BackfillRequest
82:def sync_panel_endpoint(panel_key: str, platform: str = "", account_ids: str = ""):
172/186/188:                                                       # 全在 /backfill 里
```

```
# PanelComparisonPanel.tsx:633-635
// 传**当前筛选出的 id 集合**：不传的话后端会推该平台全部本地较新的行，
// 而弹窗按筛选后的数量报数 —— 「看到的 ≠ 被改的」（代码审查 I1）。
if (pushIds.length) params.set('account_ids', pushIds.join(','))
```

- 证据（历史）：`git show eb1bc23 -- api/integrations.py` 的提交说明写着「端点加 `account_ids`（逗号分隔），前端传 `pushIds`/`pullIds`」，但同一提交里 `/push` 的签名就只有 `(panel_key, platform, delete_old)` —— 这个修复**从来只做了前端一半**。
- 影响：用户在对比页筛了「凭证不同 · 本地较新 12 行」再点「更新远程」，后端实际推的是**该面板全部** `local_only + local_newer` 行（可能几百个）。方向没错（不会用旧覆盖新），但：① 会产生大量用户没预期的远端写入/新记录；② 弹窗数量与实际不符；③ 拉回同理，会改动筛选范围外的账号。这正是代码审查 I1 要修的那个 bug，现在回归了。
- 建议：`push_panel_endpoint` 增加 `account_ids: str = ""`，两个端点都在 `fetch_panel_raw` 之后按 `local_id` 过滤（与 `platform` 过滤同一个位置，`api/integrations.py:113-119` / `services/panel_push.py:571-576`）；补两条端点级用例（传 2 个 id → 只处理 2 个）。

### F-09（P3，信息）两处「永远不收敛」的行，且没有强制方向入口

- 位置：`services/panel_comparison.py:173-175`（同小时 → `time_synced`）、`:250-251`（取不到时间 → `""`）、`services/panel_push.py:153-163`
- 证据：`compare_by_hour` 在两侧时间落同一 UTC 小时时返回 `time_synced`，`plan_push` 与 `plan_sync` 对它都是「不动」。当凭证确实不同但两侧时间同小时（iat 解不出、回落记录时间的情况，例如 grok2api 只有 SSO 的账号），这一行会永久停在 `credential_diff`，两个按钮都不动它。
- 影响：这是**有意的保守设计**（docstring 写明了理由），不是 bug。但没有「我知道我在做什么，强制推/强制拉」的出口，用户只能去账号页用单账号动作绕过。
- 建议：对比页对 `time_synced` / `unknown_time` 的行提供「强制推送 / 强制拉回」二次确认按钮（走单账号动作路径，已有 `upload_cpa` 等实现），并在 tooltip 里写明风险（x.ai 的 RT 轮换）。

---

## 3. 检查项（3）：上传代理开关（`cpa_upload_proxy_enabled` 等）的行为

### 3.1 实测行为

| 面板 | 开关键 | 支持 | 代理去向 |
|---|---|---|---|
| CPA | `cpa_upload_proxy_enabled` | ✅ | 写进 auth 记录顶层 `proxy_url`（`cpa_upload.py:241-243`），**上传请求本身 `proxies=None`**（`:266`） |
| chatgpt2api | `chatgpt2api_upload_proxy_enabled` | ✅ | 写进导入对象 `proxy` 字段（`chatgpt2api_upload.py:179`），请求 `proxies=None`（`:267`） |
| Sub2API | — | ❌ | `proxies=None` 硬编码（`sub2api_upload.py:168`），无开关 |
| grok2api | — | ❌ | 导入格式不含代理字段，请求用 `Grok2ApiClient.proxy`（默认空，`grok2api.py:143`） |

取代理的唯一入口：`services/chatgpt_sync.py:477-495`，读 `extra["register_proxy"]`（账号绑定的注册代理），开关关闭/面板不支持/账号没绑 → 空串。

### F-10（P3）开关语义与用户预期不符 + 一个键归错分组

- 位置：`services/chatgpt_sync.py:464-474`（`_UPLOAD_PROXY_SWITCHES`）、`core/config_schema.py:71`（`ConfigKey("chatgpt2api_upload_proxy_enabled", "cpa")`）、`frontend/src/components/settings/PanelConfigPanel.tsx:117`（前端把它放在 chatgpt2api 分组）
- 证据：
  1. **语义**：叫「上传代理」，但上传请求不走代理（`proxies=None`），代理只是随记录下发给面板、供**面板后续调用上游**时使用。这个语义在 `cpa_upload.py:225-227` 的 docstring 里写清楚了，但界面文案只有「上传代理（账号绑定的代理）」，用户会以为本机出口被换掉（很多人开这个开关是为了不让面板看到本机 IP —— 那恰好是反的）。
  2. **分组**：`config_schema.py:71` 把 `chatgpt2api_upload_proxy_enabled` 的 group 写成 `"cpa"`，于是 `GET /api/config/schema` 把它归到 CPA 组；而前端 `PanelConfigPanel.tsx` 自己硬编码了分组（放在 chatgpt2api 段）。两套分组表，schema 那份是错的。
  3. **覆盖**：4 个面板只有 2 个支持。`chatgpt_sync.py:469-470` 的注释解释了原因（Sub2API 要先建代理记录拿 `proxy_id`；grok2api 导入格式不含代理），理由成立，但没有在界面上说明 —— 用户在 Sub2API 面板找不到这个开关，只会以为是漏了。
- 影响：配置项的**名字、位置、实际效果三者不一致**；排障时「我开了上传代理怎么出口 IP 没变」会消耗大量时间。
- 建议：① 文案改成「随凭据下发代理（供面板侧出网）」；② `config_schema.py:71` 的 group 改 `"chatgpt2api"`；③ 在 Sub2API/grok2api 的面板卡片上补一行「该面板不支持下发代理（原因：…）」。

---

## 4. 检查项（4）：对比的 local_id / remote_id 匹配是否可靠

### 4.1 匹配键本身是可靠的

- 键 = `f"{platform}|{email.lower()}"`（`services/panel_comparison.py:429-438`），平台参与是因为 CPA 上同一邮箱可能同时有 `codex` 与 `xai` 两条凭据；
- `local_id` = `accounts.id`（UUID），只用于动作回传，不参与匹配（`panel_comparison.py:518`）；
- `remote_id` 逐面板取自对端主键：CPA `auth_index or name`（`:713`）、Sub2API `id|db_id|account_id`（`:834`）、grok2api `id`（`:918`）、chatgpt2api `id|management_id`（`:1197`）；
- 本地重复行由唯一索引 `uq_accounts_platform_email` 挡住（`core/db/models_account.py:36-43`（基线 4680c8d 为 33-42）），远端重复由 fetcher 各自处理（见 F-11）。

### F-11（P2）远端重复记录被静默折叠成一条：`remote_id` 只暴露一条，删除只能删一条，且四个面板四种去重口径

- 位置：`services/panel_comparison.py:494-498`（`remote_by_key[key] = item`，**后者覆盖前者**）、`services/panel_push.py:194-196`、`services/panel_sync.py:230-232`、`services/panel_status_sync.py:231-233`（同一段「按 key 建字典」被抄了四份）
- 证据（四种口径）：

| 面板 | 去重方式 | 位置 |
|---|---|---|
| CPA | **不去重**（同一邮箱多条 auth-file 全部保留 → 字典后者胜） | `panel_comparison.py:652-727` |
| Sub2API | **不去重**（`accounts.append` 无条件） | `:820-846` |
| grok2api | 按邮箱取**最新**（`_is_newer`） | `:926-928` |
| chatgpt2api | 按 `id` 去重，同邮箱多条**都保留** | `:1197-1201` |

- 影响：
  1. 新建式面板（Sub2API / chatgpt2api）历史上重复上传过的账号，远端有 2 条，对比表只显示 1 条（后出现的那条），`remote_id` 也只有它 —— `delete_old` 每轮只能删 1 条，**永远清不干净，且用户看不见剩下的那条**（它既不出现在「已同步」也不出现在「仅远端」）；
  2. 到底保留哪一条取决于对端列表顺序（CPA 是 `compareAuthFileListOrder`、Sub2API 是 DB 顺序、chatgpt2api 是分页顺序）—— 同一次推送前后可能换人，`delete_old` 于是删掉「另一条」，凭证比对结果跟着跳变；
  3. 四份 `remote_by_key` 各自演进（现在只有 `panel_push` 那份带注释），任一处改口径就会让「对比看到的」与「推送/拉回处理的」不是同一条记录 —— 而这正是 `fetch_panel_raw` 的 docstring 明确要避免的事（`panel_comparison_cache.py:246-250`）。
- 建议：① 把 `remote_by_key` 提成 `panel_comparison.build_remote_index()` 单一实现，四处调用；② 重复记录不要静默折叠 —— 在行上带 `remote_duplicates: [id...]`，`delete_old` 删全部，对比页显示「远端 N 条重复」；③ 至少统一成「同一 key 取时间最新的一条」并在文档里写死。

### F-12（P3）chatgpt2api 的 `id` 与 `management_id` 混用：两者不一致时凭证全空 → 全部「无法比对」

- 位置：`services/panel_comparison.py:1197`（列表侧 `remote_id = id or management_id`）、`:1130-1133`（导出侧 `account_id = management_id or id`）、`:1266`（`fetched.get(account.remote_id)`）
- 证据：列表优先 `id`，导出结果优先 `management_id`，回填时按 `account.remote_id` 查表。若对端两个字段都存在且不相等（yukkcat 变体里 `id` 是 DB 主键、`management_id` 是另一套标识），查表必然落空。
- 影响：落空是**静默**的（`if credentials:` 才赋值，`:1268`），结果是这些账号全部 `unknown_credential`；又因为 `plan_push` 对 `unknown_credential` 不推（`panel_push.py:146-151`），表现为「对比显示无法比对 + 推送说没有需要推的」，两个症状都指向不了真正原因。
- 建议：回填时同时尝试 `account.remote_id` 与 `account.extra.get("management_id")`；列表侧把两个 id 都存进 `extra`，并在两者不等时打一条 warning。

### F-13（P2）平台清单两处不一致：390 个 Grok 账号进 CPA 对比、没有平台筛选器、推送必失败

- 位置：`services/panel_registry.py:72`（`"platforms": ["chatgpt"]`，注释明确写「Grok 自 2026-10-27 起不再支持 CPA」）vs `services/panel_comparison_cache.py:140-145`（`"cpa": ("chatgpt", "grok")`）；推送侧拒绝：`services/panel_push.py:385-392`
- 证据：

```python
# panel_registry.py:72
"platforms": ["chatgpt"],          # ← 前端据此决定是否渲染平台选择器
# panel_comparison_cache.py:143
"cpa": ("chatgpt", "grok"),        # ← 对比据此决定把哪些本地平台的行拉进来
```

```
# panel_push.py:385-392
if plat == "grok":
    return False, ("Grok 已不再支持 CPA 上传（只支持 grok2api）—— 请在面板管理里用 grok2api")
```

- 影响（结合生产库）：
  - `shouldShowPlatformFilter(["chatgpt"])` → `unique.size > 1` 为假（`frontend/src/lib/panelComparison.ts:101-107`）→ **CPA 对比页没有平台选择器**；
  - 但 `_PANEL_PLATFORMS["cpa"]` 会把 390 个 Grok 账号全部拉进对比表（`panel_comparison_cache.py:107-135`）；
  - 其中 21（`sync_statuses.cpa.uploaded`）+ 32（`cliproxyapi`）个历史上传过、能匹配上远端 xai 记录，剩下 337 个显示「未上传」；
  - 用户点「上传未上传」→ `selectPushIds` 把这些 Grok 行全部选中（`panelComparison.ts:133-143`，不区分平台）→ 逐个返回 `ok=False`「Grok 已不再支持 CPA 上传」→ 全部计入 `failed`（`panel_push.py:239-244`）→ 界面报「N 个账号推送失败」。
- 也就是说：**每次点「更新远程」都会稳定产生一批必然失败的条目，而用户没有任何办法把它们筛掉。**
- 建议：二选一并保持一处为真 —— ① `_PANEL_PLATFORMS["cpa"] = ("chatgpt",)`（与 2026-10-27 的下线决定一致，最简单）；② 若确实要保留 Grok 的**只读**对比（看远端还有哪些 xai 残留），则 registry 的 `platforms` 补上 `"grok"` 让选择器出现，并给 `PushOutcome` 加 `unsupported` 原因（不计入 `failed`，单独一档「该面板不支持此平台」）。`tests/test_panel_management_actions.py:84-99` 只断言了 `panel["platform"] in _PANEL_PLATFORMS[key]`（子集方向），所以这条不一致测不出来 —— 建议改成双向相等。

---

## 5. 检查项（5）：失败隔离

### F-14（P0）CPA 批量「同步远端状态」必现 500：对 UUID 主键做 `int()`

- 位置：`api/actions.py:263`
- 证据：

```python
# api/actions.py:262-263
for acc_model in accounts:
    sync_result = sync_results.get(int(acc_model.id or 0), {})
```

`AccountModel.id` 是 UUID 字符串（`core/db/models_account.py:50-56`（基线 4680c8d 为 45-51，该文件因 `c2c502b` 下移 5 行），`default_factory=lambda: str(uuid.uuid4())`），而 `sync_chatgpt_cliproxyapi_status_batch` 用 `getattr(account, "id")` 原样做键（`services/cliproxyapi_sync.py:667`、`:577`，注解还写着 `dict[int, ...]` —— 同一个历史遗留假设）。

- 复现（纯内存，桩掉网络与落库，未开任何 DB 会话）：

```
$ .venv/bin/python /root/.hermes/cache/scratch/probe_int_id.py
RAISED: ValueError invalid literal for int() with base 10: '14de3423-34b4-4e60-9f07-718098cd1a30'
```

- 状态（并发修复，2026-10-30）：提交 `9630980` 已把这一行改成 `str(acc_model.id or "")` —— 本条对**基线 `4680c8d`** 成立。同一提交**没有**改另外两处：`services/cliproxyapi_sync.py:570/577/665/667` 的 `dict[int, ...]` 注解、以及 `_execute_batch_cliproxy_sync` 循环体仍然没有 try（F-15）—— 这两项现在依然成立。该提交同时补了 `tests/test_batch_cliproxy_sync_uuid.py`（99 行），本条的回归用例已就位。
- 影响：`POST /api/actions/chatgpt/sync_cliproxyapi_status/batch`（对比页「同步远端状态」的 CPA 分支，`PanelComparisonPanel.tsx:549`）对任何账号都 500，且异常在 `_execute_batch_cliproxy_sync` 内**没有 try**（对比 `api/actions.py:398-415` 的通用循环是有 try 的），冒泡到 FastAPI。
- 与 `docs/audit/2026-10-30/actions-tasks.md` 的 F-00 是同一处（该报告从动作分发角度记录）。本节补充两点它没写的：① `services/cliproxyapi_sync.py:570/577/665/667` 的 `dict[int, ...]` 注解是同一个错误的另一半，只改 `actions.py` 会把错误留在类型契约里；② 这条路径**没有任何测试**覆盖（`grep -rn "sync_cliproxyapi_status/batch" tests/` 无命中）。
- 建议：`sync_results.get(str(acc_model.id or ""), {})`；两处注解改 `dict[str, dict[str, Any]]`；`_execute_batch_cliproxy_sync` 循环体包 try（与通用循环同形）；补一条用 UUID 账号跑批量同步的用例。

### F-15（P2）两个面板专用批量分支没有逐账号隔离：一个坏行回滚整批

- 位置：`api/actions.py:196-232`（`_execute_batch_panel_status` 的循环，无 try）、`:234-284`（`_execute_batch_cliproxy_sync`，无 try）、提交点在 `:383` / `:365`（整批一次 `session.commit()`）
- 证据：通用批量循环每个账号都包了 try（`:398-415`），并逐条把异常转成 `ok=False` 的 item；而这两个面板专用分支没有 —— 循环里唯一的失败点 `account_repository.save(acc_model, session=session)`（`:214`）抛异常时：整个请求 500，且**已经处理完的账号的 patch 全部随会话回滚**（commit 在循环外）。
- 影响：一条脏数据（超长字段、并发写冲突、磁盘满）能让整批 1000 个账号的状态同步白做，并且用户拿到的是 500 而不是「哪几个失败」。面板状态同步是**可重入且幂等**的，但用户不知道重试是否安全。
- 建议：两个分支的循环体各包一层 try（与 `:398-415` 同形），失败条目 `ok=False, message=type(exc).__name__`；commit 改成逐账号或「先收集 patch，再统一写」，避免半批回滚。

### F-16（P2）推送批次没有总时限/并发上限：一个挂掉的面板能把一次点击拖成几小时

- 位置：`services/panel_push.py:190-247`（**串行** for 循环）、`platforms/chatgpt/chatgpt2api_upload.py:262-278`（每个号最多 60s × 4 次 + 12s 退避 ≈ 252s）
- 证据：`push_local_to_remote` 对 `local_rows` 逐行 `upload(row)`，无并发、无 deadline、无「连续 N 次失败就中止」。chatgpt2api 的 4 次重试全部在请求线程内 `time.sleep`（`:273`）。
- 影响（算术）：chatgpt2api 面板不可达时，一次「更新远程」若有 100 个 `local_only` 行 → 100 × 252s ≈ **7 小时**，全程占住一个 FastAPI 线程池线程（默认 40），并且期间前端超时、用户重复点击会再起一份。对比页/推送都走 `fetch_panel_raw`（无缓存、无锁，`panel_comparison_cache.py:246-279`），所以每次点击还会先付一次「列表 + 最多 200 次凭证 download」的成本。
- 建议：① 推送批次加总预算（例如 10 分钟）与「连续 5 次连接类失败 → 中止并返回部分结果」；② 重试的 sleep 用 `time.sleep` 改成可取消的短轮询，或把重试下沉到有并发控制的执行器；③ 面板级熔断：连续失败后 60s 内直接返回「面板不可用」，不要每个号都去撞一遍。

### F-17（P3）对比缓存的重建锁无超时：慢面板会阻塞同面板所有请求（含 `refresh=true`）

- 位置：`services/panel_comparison_cache.py:216-232`（`with _rebuild_lock(key):` 包住整次远端拉取）、`:52-76`（每面板一把 `threading.Lock`）
- 证据：锁的持有范围包含 `_rebuild_panel_comparison` → `fetch_panel_raw` → 远端 HTTP（CPA 最坏 1 次列表 + 200 次 download ≈ 20s+；Sub2API 20 页 × 20s = 400s）。`refresh=true` 也走同一把锁（`:228` 只跳过缓存检查，不跳过锁）。
- 影响：面板半死（TCP 连上但不回包）时，同面板的所有请求排队，每个占用一个线程池线程 → 与 F-16 叠加可耗尽 40 个线程。锁本身是对的（防惊群），缺的是超时与「用旧数据兜底」。
- 建议：`lock.acquire(timeout=...)`，超时后返回上一份缓存（带 `stale: true`）；或把远端拉取移出锁（单飞 + 结果发布）。

---

## 6. 检查项（6）：敏感信息（面板 key、账号密码）在日志/响应里的暴露面

### 6.1 已达标（这次要明确记下来，避免后续改动把它们改坏）

| 面 | 结论 | 证据 |
|---|---|---|
| `GET /api/integrations/panels` | 只回 `secret_key`（键名）与 `secret_set`（布尔），**不回值** | `api/integrations.py:57-63`；`tests/test_panel_registry.py::test_endpoint_never_returns_secret_values` |
| `GET /api/config` | 口令键统一置空 + `<key>_set` 布尔；面板四个口令（`cpa_api_key` / `sub2api_api_key` / `chatgpt2api_api_key` / `grok2api_password`）都在 `SECRET_CONFIG_KEYS` 里 | `api/config.py:96-104`、`core/config_schema.py:68/75/80/96` |
| 对比接口响应 | **不含**远端 AT/RT：`ComparisonRow.to_dict()` 只给 `remote_extra`（plan/disabled/availability）与字段名列表 | `services/panel_comparison.py:363-391` |
| 账号列表 | AT/RT/ST/IDT/cookies 正文被替换成 `has_*` 布尔 | `api/accounts.py:165-167`、`:201-232` |
| 日志 | 面板路径（`services/panel_*`、四个适配器、`api/integrations.py`）无「打印 payload/header/凭据」的语句；`grok2api` 登录失败只报状态码 | `platforms/grok/grok2api.py:198`；（协议层另有 `platforms/chatgpt/protocol/mixins/signup.py:73` 的 CSRF 前 20 字 debug 日志，不属面板路径，见 `api-security` 审计） |
| 鉴权 | 未设密码时**非回环来源 503**（fail-closed），不是放行 | `main.py:211-236`（基线 4680c8d 为 225-251） |

### F-18（P2）面板返回的错误文本被原样落库并经 `GET /api/accounts` 下发 —— 远端若回显凭据，就是明文入库

- 位置（产生端）：`platforms/chatgpt/sub2api_upload.py:177-189`、`platforms/chatgpt/cpa_upload.py:275-281`、`platforms/chatgpt/chatgpt2api_upload.py:293-303`
- 位置（落库端）：`services/panel_push.py:489`（`message` 原样取出）→ `:506-511`（写进 `enroll` 与 `sync_statuses`）、`services/chatgpt_sync.py:104-110`（`last_message`）
- 位置（下发端）：`api/accounts.py:201-232`（`sync_statuses` 明确**不**在瘦身白名单里，注释写着「列表页都要读」）
- 证据：

```python
# sub2api_upload.py:181-186 —— 对端 message 直接成为返回给调用方的字符串
error_msg = str(detail.get("message") or detail.get("msg") or detail.get("error") or error_msg)
...
return False, error_msg
```

```python
# chatgpt2api_upload.py:297-302
detail = str(body.get("message") or body.get("error") or body.get("detail") or detail)
return False, f"上传失败: {detail}"
```

```python
# api/accounts.py:205-206
只动上面列出的重型键，其余原样保留 —— `chatgpt_local`（state/plan）、
`totp_secret`（2FA 状态）、`register_proxy`、`sync_statuses` 等列表页都要读。
```

- 影响：这是一条**三段式管道**：对端错误体 → `sync_statuses.<panel>.last_message`（持久化，永久留在 `extra_json`）→ `GET /api/accounts` 明文下发 → 前端渲染。只要任一实现对「无效凭据」的错误体回显了提交内容（`invalid access_token: eyJ...` 这类写法在管理面板里很常见），凭据就永久落进本地库并出现在列表响应里 —— 而这条路径**没有任何截断或脱敏**（只有 `panel_push.py:237` 的 300 字截断，且只对删除失败那条）。
- 建议：① 落库前过一层 `redact_secrets(message)`：命中 `eyJ[A-Za-z0-9_-]{20,}`、`sk-`、`Bearer ` 后 20 位以上的串一律替换成 `<redacted>`；② `_slim_extra_for_list` 对 `sync_statuses.*.last_message` 做同样的脱敏（它是唯一会下发的自由文本）；③ 面板错误体只保留 `HTTP <code>` + 前 200 字，且先过 `redact`。

### F-19（P3）残余暴露面：`remote_error` 回显 URL、无面板调用审计日志

- 位置：`services/panel_comparison_cache.py:277-279`（`remote_error = str(exc)` 直接进响应）、`api/integrations.py:76/116`（原样返回给前端）、`services/cliproxyapi_sync.py:154-157`（异常消息里带 `_base_url`）
- 证据：`requests` 的 `HTTPError`/`ConnectionError` 字符串形如 `401 Client Error: Unauthorized for url: http://host/v0/management/auth-files?name=...`。URL 本身不含 key（key 在 header），但若运维把凭据写进 URL（`http://user:pass@host`），它会随 `remote_error` 进响应、进日志、进前端 toast。
- 影响：低概率、高代价（一旦发生就是凭据进日志）。另外**没有审计**：谁在什么时候把哪批账号推给了哪个面板 URL，事后无法追溯（`logger.info` 只出现在 `cliproxyapi_sync.py:651/699` 的批量统计里，推送路径完全没有留痕）。
- 建议：① `remote_error` 过 `redact_url()`（去掉 `user:pass@`）；② 推送/拉回成功后写一条结构化日志（panel、url host、账号数、pushed/pulled/failed），不记凭据。

---

## 7. 建议修复顺序

| 顺序 | 编号 | 理由 |
|---|---|---|
| 1 | F-14 | 必现 500，改动一行；`9630980` 已修，确认回归用例后关闭 |
| 2 | F-06 | 生产库 CPA/grok2api 的「更新本地」整体不可用，用户感知最强 |
| 3 | F-07 | 会删掉远端唯一记录（数据面），且自动维护每轮都跑 |
| 4 | F-08 | 前端已按契约传参、后端不认，改动小、影响所有批量操作的范围 |
| 5 | F-13 | 每次点推送都产生一批必然失败条目 |
| 6 | F-15 / F-16 | 批量端点的健壮性，与 F-14 同一批改动最省 |
| 7 | F-03 / F-18 | 安全面：TLS 一致性、错误文本脱敏 |
| 8 | F-01 / F-11 / F-12 | 契约与匹配口径的统一，需要一次小重构（统一 HTTP 层 + 统一 remote index） |
| 9 | F-02 / F-04 / F-05 / F-09 / F-10 / F-17 / F-19 | 一致性/可观测性 |

## 8. 复现与验证

```bash
cd /data/workspace/Register

# F-14：批量 CPA 状态同步对 UUID 主键做 int()（纯内存，不触网、不开 DB 会话）
.venv/bin/python /root/.hermes/cache/scratch/probe_int_id.py
# → RAISED: ValueError invalid literal for int() with base 10: '<uuid>'

# F-08：account_ids 只在前端存在
grep -n "account_ids" api/integrations.py
grep -n "account_ids" frontend/src/components/settings/PanelComparisonPanel.tsx

# F-06：生产库登记覆盖（只读）
.venv/bin/python - <<'PY'
import sqlite3, json, collections
con = sqlite3.connect("file:data/account_manager.db?mode=ro", uri=True)
cur = con.cursor(); cur.execute("select platform, extra_json from accounts")
enr = collections.Counter(); up_no_enr = collections.Counter()
for plat, extra in cur.fetchall():
    d = json.loads(extra or "{}")
    pu = d.get("panel_uploads") or {}; ss = d.get("sync_statuses") or {}
    for k in pu: enr[k] += 1
    for k, v in ss.items():
        if isinstance(v, dict) and v.get("uploaded") and k not in pu: up_no_enr[k] += 1
print("enrolled:", dict(enr)); print("uploaded-but-not-enrolled:", dict(up_no_enr))
PY
# → enrolled: {'chatgpt2api': 314}
# → uploaded-but-not-enrolled: {'chatgpt2api': 353, 'grok2api': 51, 'cpa': 31, 'cliproxyapi': 42}

# F-13：平台清单两处不一致
grep -n '"platforms"' services/panel_registry.py
grep -n '"cpa": (' services/panel_comparison_cache.py

# F-03：grok 客户端没有 verify
grep -n "verify" platforms/grok/grok2api.py platforms/grok/upload.py   # 无输出

# 回归基线（本次实跑，全绿，说明上述问题都没有测试覆盖）
.venv/bin/python -m pytest tests/test_panel_push.py tests/test_panel_sync.py \
  tests/test_panel_status_sync.py tests/test_panel_registry.py \
  tests/test_panel_push_endpoint.py tests/test_panel_sync_endpoint.py \
  tests/test_panel_comparison.py tests/test_panel_management_actions.py -q
# → 192 passed
```

## 9. 未覆盖 / 建议补测

- **未覆盖**：`reference/panel/*`（上游四个面板的源码）只用于核对端点形状（CPA 分页是否 opt-in、chatgpt2api 的 `account_ids` vs `access_tokens`），未做安全审计；前端只核对了接线（是否真的传参、是否真的渲染选择器），未审 UI 层逻辑；`data/account_manager.db` 只读抽样，未验证历史数据的凭证有效性。
- **建议补的用例**（当前缺口正好对应上面的 P0/P1）：
  1. UUID 主键跑 `POST /actions/chatgpt/sync_cliproxyapi_status/batch`（F-14）—— **已有** `tests/test_batch_cliproxy_sync_uuid.py`（`9630980` 补），其余 6 条仍缺；
  2. `plan_sync(enrolled=set())` 的行为与「登记表为空」的语义（F-06）；
  3. `push → (True, "上传成功（新增 0，跳过 1）")` 时**不得**调用 `delete_remote`（F-07）；
  4. 端点级 `account_ids` 收窄：传 2 个 id，断言只处理 2 个（F-08）；
  5. 注册表 `platforms` 与 `_PANEL_PLATFORMS` **双向相等**（F-13，现有断言是子集方向，测不出来）；
  6. 「所有面板 HTTP 客户端都必须传 `verify=verify_for(...)`」的静态枚举检查（F-03）；
  7. `redact_secrets()` 的单元测试 + `sync_statuses.last_message` 下发前脱敏（F-18）。
