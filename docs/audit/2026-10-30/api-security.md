# api-security 审计

[MODE: SANDBOX]

- **仓库**：`/data/workspace/Register`，分支 `main`，HEAD `4680c8d`（feat(proxy): 代理标签【注册】【绑定】…）
- **审计范围**：`api/*.py`（accounts / tasks / proxies / config / actions / integrations / auth / mail_imports / outlook / icloud / backup / sms / shared_mail / platforms / contribution）、`main.py`、`core/auth*`（本仓库无 `core/auth*`，鉴权实现全部在 `api/auth.py` + `main.py` 中间件）、以及被上述接口直接调用的 `core/db/*`、`core/config_store.py`、`services/task_runners.py`、`services/data_bundle.py`
- **方法**：静态阅读 + 真实 HTTP 探测（GET/HEAD）+ 只读 SQLite 取证
- **只读约束**：真实库 `data/account_manager.db` 仅以 `file:...?mode=ro` 打开；所有写操作（含唯一一次 `POST /api/auth/setup`）只发生在**一次性 DB 副本**上

### 0.1 探测环境说明（重要）

审计开始时 `127.0.0.1:8000` **并未监听**（`curl` exit 7 / `ss -ltnp` 无 8000），工作区同时处于脏状态（`core/db/models_infra.py` 等在审计期间被并发修改，`python -c "import main"` 一度抛 `NameError: name 'Index' is not defined`）。为保证可复现且不影响任何人，采用如下沙箱：

1. `git archive HEAD | tar -x -C /root/.hermes/cache/scratch/audit-repo` —— 取**提交态**代码，不受并发编辑影响；
2. `cp -a data/. /root/.hermes/cache/scratch/audit-data` —— 真实数据的一次性副本；
3. `DATA_DIR=<副本> APP_ENABLE_SOLVER=0 APP_JWT_SECRET=audit-sandbox-secret uvicorn main:app --host 127.0.0.1 --port 18000`；
4. 全部 GET/HEAD 探测打在 `:18000`；**没有**对真实 `:8000` 或真实库发出任何写请求。

文中的响应体均来自该沙箱实例，数据与真实库同源（1057 个账号 / 5 个代理 / 837 个任务 / 1661 条日志）。长凭据在报告中只给**长度 + 结构**，不落明文。

---

## 1. 结论摘要

| ID | 严重度 | 主题 | 位置 | 一句话 |
|----|--------|------|------|--------|
| F1 | **严重** | 鉴权 | `main.py:228` + `api/auth.py:309-324` | `/api/auth/setup` 绕过鉴权中间件：未设密码时**任何来源**都能设密码并拿到 token → 远程接管 |
| F2 | **严重** | 敏感数据 | `api/backup.py:37-54` | `GET /api/backup/export` 一个请求返回全库 + `secrets/credential_key`（解密密钥本身），ZIP 不加密 |
| F3 | 高 | 敏感数据 | `api/accounts.py:260-300` | 账号**列表**明文下发 `token` 列（chatgpt=AT、grok=SSO），与文件内注释「列表不下发 AT」直接矛盾 |
| F4 | 高 | 敏感数据 | `services/task_runners.py:259,568` → `api/tasks.py:636,789` | 任务日志把 `socks5h://user:pass@host` 完整写库，并被两个列表接口回吐 |
| F5 | 高 | 敏感数据 | `api/proxies.py:37-39,128-173` | 代理列表/均衡接口返回带账号密码的代理串 |
| F6 | 高 | 鉴权 | `api/auth.py:402-403,406-416,58` | token 7 天有效且**不可吊销**：logout 是空实现，改密码不轮换 JWT secret |
| F7 | 高 | 鉴权 | `api/auth.py:256-266` | 登录退避按 `X-Forwarded-For` 首段计数 → 伪造该头即**完全绕过**爆破限速 |
| F8 | 高 | 敏感数据 | `core/db/models_account.py:81` | `token` 列（AT/SSO）**明文落盘**，1057/1057 行非密文，与「凭据加密落盘」不符 |
| F9 | 中 | 敏感数据 | `api/accounts.py:419-443` | `GET /api/accounts/export` 直出明文密码 CSV，无二次认证、无条数上限 |
| F10 | 中 | 鉴权 | `api/auth.py:309-337` | 持任意有效 token 即可 `setup` 改密 / `disable` 关鉴权，**不需要旧密码**（持久化接管） |
| F11 | 中 | 鉴权 | `main.py:205-222` | 无密码时的「仅回环」兜底在同机反代下失效（代理连接源地址恒为 127.0.0.1） |
| F12 | 中 | CORS | `main.py:269-274` | `allow_origins=["*"]` + 全方法 + 全头 |
| F13 | 中 | 敏感数据 | `api/accounts.py:724-735`、`api/backup.py:37` | 凭据类响应无 `Cache-Control: no-store` |
| F14 | 中 | 输入校验 | 见 §4.1 | 批量接口上限不一致：`import` / `outlook` / `icloud` / `mail-imports` / `proxies/bulk` **无上限**；`page_size` 无上限 |
| F15 | 中 | 错误语义 | 见 §5.1 | 多处 500/200 响应体透传 `str(exc)`（SQL、路径、代理串可能外泄） |
| F16 | 中 | 错误语义 | `api/mail_imports.py:19-38` | 参数越界返回 400 且带 pydantic 内部模型名与校验细节 |
| F17 | 中 | 破坏性 | 见 §6 | `sync` / `push?delete_old` / `icloud batch-delete(remote=true)` / `logs cleanup` 无二次确认或 dry-run |
| F18 | 中 | 敏感数据 | `api/icloud.py:338-346` + `api/shared_mail.py:149` | 1941 个免登录邮件页的 `share_token` 被列表接口批量下发 |
| F19 | 低 | 静态/头 | `main.py:573-610` | SPA 与 API 无 CSP / `X-Content-Type-Options` / `X-Frame-Options` |
| F20 | 低 | 信息泄露 | `api/auth.py:300-305,340-373` | 未认证可读 `has_password/has_totp`；login 用 403 `no_password_set` 确认「没设密码」 |
| F21 | 低 | 信息泄露 | `main.py:300-305` | `/api/runtime` 回绝对解释器路径、pid、启动时间 |
| F22 | 低 | 输入校验 | `api/tasks.py:636-657` | `page_size` 无上限，且每次 `len(s.exec(q).all())` 全表装载算 total |
| F23 | 低 | 鉴权 | `api/auth.py:214-240` | `_login_failures` 以可伪造的 client key 为键，无清理，可无界增长 |
| F24 | 低 | 错误语义 | `api/sms.py:83-100` | 上游错误原文透传（`f"查询余额失败: {exc}"`），可能回显 key |
| F25 | 低 | 契约 | `api/integrations.py:67-79` | GET 触发对第三方面板的外网请求（GET 有外部副作用），单次响应 388 KB |

**做得对、值得保留的控制点**（避免后续重构改坏）：

- `docs_url/redoc_url/openapi_url` 全关（`main.py:199-201`），实测 `/api/openapi.json` → 404；
- 未设密码时非回环返回 503 而非 fail-open（`main.py:231-251`），实测生效；
- 密码 PBKDF2-HMAC-SHA256 600k + 每用户随机盐 + `hmac.compare_digest`（`api/auth.py:100-158`）；
- TOTP 失败计数并消费临时令牌（`api/auth.py:376-399`，上限 5 次）；
- 账号/代理/任务/动作四个 batch-delete 都有 1000 条上限（`api/accounts.py:693`、`api/proxies.py:82`、`api/tasks.py:666`、`api/actions.py:138,167`）；
- 代理均衡默认 `dry_run=True`（`api/proxies.py:28-31`）；
- 备份导入有 zip-slip 净化、成员数/大小/sha256/魔数四重校验、Content-Length 强制、运行中任务 409（`services/data_bundle.py:409-458,578-606`、`api/backup.py:70-95`）；
- 静态目录 `StaticFiles` 与 SPA 兜底均无路径穿越（实测见 §7.2）。

---

## 2. (1) 鉴权：哪些端点无需登录即可读写

### 2.1 中间件的实际判定链

`main.py:225-266` 是唯一的鉴权入口，逻辑是三段：

```
path.startswith("/api/auth/") or not path.startswith("/api/")   → 直接放行   (main.py:228)
auth_password_hash 为空                                          → 仅回环放行，其余 503 (main.py:231-251)
否则要求 Authorization: Bearer <JWT>                             → 401 / 通过  (main.py:252-266)
```

由此得到**未登录可达面**的完整清单：

| 路径 | 方法 | 未登录可达 | 说明 |
|------|------|-----------|------|
| `/api/auth/status` | GET | ✅ | 回 `{"has_password":…,"has_totp":…}` |
| `/api/auth/setup` | POST | ✅ | **未设密码时无任何校验**（F1） |
| `/api/auth/disable` | POST | ✅ | 未设密码时无校验，且会清空 `auth_totp_secret`（`api/auth.py:327-337`） |
| `/api/auth/login` | POST | ✅ | 预期公开 |
| `/api/auth/verify-totp` | POST | ✅ | 预期公开 |
| `/api/auth/logout` | POST | ✅ | 空实现（F6） |
| `/api/auth/2fa/setup` `/2fa/enable` `/2fa/disable` `/change-password` | POST/GET | ❌ | 有 `Depends(require_auth)`（`api/auth.py:406-436`） |
| 其余所有 `/api/**` | 任意 | ❌（设了密码）/ ⚠️（未设密码+回环或反代） | 中间件统一拦截 |
| `/m/{share_token}` | GET | ✅ | 设计如此（`api/shared_mail.py:1-7,149`） |
| `/`、`/assets/**`、任意非 `/api` 路径 | GET | ✅ | SPA 静态资源 |

实测（沙箱，未设密码，客户端 127.0.0.1）：

```bash
$ curl -s http://127.0.0.1:18000/api/auth/status
{"has_password":false,"has_totp":false}
$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18000/api/accounts?page_size=1
200                      # 回环 → 全量可读
$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18000/api/openapi.json
404                      # docs/openapi 已关
$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18000/api/definitely-not-a-route
404 {"detail":"Not Found"}
```

**真实库的当前状态**：`configs` 表里**没有** `auth_password_hash`（只读查询 `select key from configs` 共 143 行，无该键）—— 也就是说现网部署正处于「未设密码」状态，下面 F1/F11 不是理论风险。

### F1（严重）`/api/auth/setup` 完全绕过鉴权中间件 → 远程接管

- **位置**：`main.py:228`（`path.startswith("/api/auth/")` 一律放行）+ `api/auth.py:309-324`
- **证据**：用一个**非回环**客户端（`TestClient` 的 `client.host == "testclient"`，且未设 `APP_TRUST_TEST_CLIENT`）对**一次性 DB 副本**发起请求：

```
remote(testclient) GET  /api/auth/status            -> 200 {"has_password":false,"has_totp":false}
remote(testclient) GET  /api/accounts?page_size=1   -> 503 {"detail":"面板尚未设置登录密码…","code":"password_not_set"}
remote(testclient) GET  /api/config                 -> 503 同上
remote(testclient) GET  /api/backup/info            -> 503 同上
remote(testclient) POST /api/auth/setup             -> 200 {"ok":true,"access_token":"<JWT>"}   ← 攻击者设定的密码生效
remote(testclient) GET  /api/accounts?page_size=1   -> 401 {"detail":"未认证，请先登录"}          ← 门已装上，钥匙在攻击者手里
```

`api/auth.py:315-322` 的逻辑是「库里没有 hash 就不校验调用方」，而中间件又对这个前缀完全放行 —— 两条各自看起来合理的规则叠在一起，等于把「首次设置密码」这个最高权限动作暴露给任意网络来源。

- **影响**：默认部署 `docker-compose.yml:44` 是 `0.0.0.0:8000` 对外。任何能连到端口的人在面板首次配置前发一个 POST，即成为管理员；随后可读全量账号、导出含 `credential_key` 的备份包（F2）。
- **建议**：
  1. `/api/auth/setup` 在 `auth_password_hash` 为空时**只接受回环**（复用 `_is_loopback_client`），非回环返回 403 并提示「请在本机初始化」；
  2. 更好的做法：首次密码只能由进程启动时生成的一次性 token（打印在 stdout / 写入 `data/secrets/`，0600）设置；
  3. 中间件不要用前缀整段放行，改成显式白名单集合 `{"/api/auth/status","/api/auth/login","/api/auth/verify-totp"}`，`setup`/`disable` 走单独判定。

### F6（高）token 不可吊销：logout 空实现 + 改密不轮换密钥

- **位置**：`api/auth.py:402-403`（`logout` 只 `return {"ok": True}`）、`api/auth.py:407-416`（`change_password` 只写新 hash）、`api/auth.py:58`（`expire_seconds=86400*7`）、`api/auth.py:71-89`（`verify_token` 只校验签名与 exp，无 jti/版本）
- **证据**：静态；`verify_token` 的 payload 只有 `sub/exp/iat`，没有任何可撤销标识；改密路径没有 `config_store.set("auth_jwt_secret", …)`。
- **影响**：一次 token 泄漏 = 7 天有效；受害者「改密码」不会踢掉攻击者；配合 F10，攻击者还能用旧 token 把密码再改回去。
- **建议**：payload 加 `ver`（或 `pwd_epoch`），改密/登出时自增；`verify_token` 比对 `ver`；或维护短期 access token + refresh token。

### F7（高）登录退避可被 `X-Forwarded-For` 绕过

- **位置**：`api/auth.py:256-266`

```python
forwarded = request.headers.get("x-forwarded-for", "")
if forwarded:
    return forwarded.split(",")[0].strip()      # ← 取的是**客户端可写**的第一段
```

- **证据**：静态 + 与 `main.py:205-222` 对照 —— 同一份代码在判定「是否回环」时明确写了「不看 X-Forwarded-For：那是请求方可伪造的头」，但退避计数却把它当成可信身份。
- **影响**：`_LOGIN_FAILURE_THRESHOLD=5` / `_LOGIN_BACKOFF_MAX=300s`（`api/auth.py:216-219`）形同虚设。攻击者每次请求换一个 `X-Forwarded-For`，即获得无限次密码尝试；若面板挂在 nginx 后且用 `$proxy_add_x_forwarded_for`（把客户端自带值放在**最前**），该头依然由攻击者控制。
- **建议**：计数键取 `request.client.host`；只有在**显式配置可信代理网段**（如 `TRUSTED_PROXIES=127.0.0.1/32`）且 `request.client.host` 属于该网段时，才解析 XFF 的**最后一段**。同时把计数下沉到共享存储（多 worker 时进程内 dict 不共享）。

### F10（中）持 token 即可改密 / 关鉴权，无需旧密码

- **位置**：`api/auth.py:309-324`（`setup_password`：只要 `verify_token` 通过就直接覆盖 hash）、`api/auth.py:327-337`（`disable_auth` 清空 hash 与 TOTP）
- **证据**：`setup_password` 分支里没有任何 `current_password` 校验；对照 `change_password`（`api/auth.py:407-416`）是要求旧密码的 —— 同一件事两个端点两套强度。
- **影响**：F6 的 token 泄漏被放大成**永久**接管（改密后原主人被锁在门外）；`disable` 还能把面板退回「无密码」状态，再叠加 F1。
- **建议**：`setup` 在已有 hash 时改为「必须提供 `current_password`」，或直接删除该分支、只保留 `change-password`；`disable` 同样要求旧密码 + TOTP。

### F11（中）「仅回环」兜底在同机反向代理下失效

- **位置**：`main.py:205-222`，注释自述「反向代理场景下本机代理连进来就是回环地址，判定仍然正确」
- **证据**：`_is_loopback_client` 只看 `request.client.host`。同机 nginx/caddy `proxy_pass http://127.0.0.1:8000` 时，**所有**外部访问的源地址都是 `127.0.0.1` → 未设密码时 503 分支永不触发，等价于 fail-open。
- **影响**：`api/auth.py:259` 的注释说明作者预期面板「通常挂在 nginx/caddy 后面」。在该拓扑 + 未设密码（现网状态）下，任何外部访问者都能读全量账号、导出备份（F2）。
- **建议**：不要用「源地址」表达「本机用户」。改为：未设密码时**只允许**携带启动期一次性 token 的请求，或干脆拒绝所有业务请求（前端只显示「请在本机初始化」页），并在文档中明确「无密码模式不支持反代」。

### `WEBUI_ALLOW_LOGIN` 的实际效果（**与面板鉴权无关**）

这个名字极易被误读成「允许登录 WebUI」，但它**不是**面板鉴权开关，与 `api/auth.py`、`main.py:225-266` 的中间件没有任何关系。它是 **ChatGPT 注册引擎的 per-flow 环境变量覆盖**，只控制「号池邮箱被 OpenAI 判定为已有账号」这一分支的走向。

- **读取点**：`platforms/chatgpt/protocol/auth_flow.py:602-620`
  - `WEBUI_ALLOW_LOGIN ∈ {1,true,yes}` → 走 OTP 登录拿凭证（access_token / session），继续当前流程；
  - 未设或其它值 → `mail_provider.mark_dead("OpenAI 识别为已有账号…")` 并把该邮箱标记死亡，抛错让外层 `register()` 自动 claim 下一个号。
- **写入点**（全部是代码里**硬编码**的流程级覆盖，不是用户配置）：`platforms/chatgpt/rt_backfill.py:70,79`（补 RT 的 `_LOGIN_OVERRIDES` / 密码登录路线）、`platforms/chatgpt/registration_engine.py:482`、`platforms/chatgpt/login_refresh.py:176`、`services/chatgpt_two_factor.py:160`。
- **作用域**：`auth_flow.py:111` 注释写明这是「本次流程专属的配置覆盖」；它不写回 `configs` 表，也不在 `GET /api/config` 的 143 个键里（实测该键不存在于配置响应）。
- **安全含义**：
  1. **不构成鉴权旁路** —— 面板是否需要密码、token 是否有效，完全由 `main.py:225-266` 决定，与该变量无关；
  2. 它是**数据破坏性**开关：`=1` 时「已被注册」的邮箱会被继续复用/消耗，未设时会被 `mark_dead` 淘汰。对同一号池反复以 `=1` 跑补 RT，会让已被 OpenAI 标记的邮箱继续留在池里；
  3. 因为它来自 `env_overrides`（流程级）而不是进程 env，`grep` 环境变量会漏掉 —— 排查「为什么某任务走了登录分支」要看 `platforms/chatgpt/rt_backfill.py:66-80` 的 `_LOGIN_OVERRIDES`，而不是 `.env`。
- **建议**：改名（如 `CHATGPT_REUSE_EXISTING_ACCOUNT`）以消除与面板鉴权的歧义；在 `docs/` 补一张「env 覆盖 → 行为」表，避免下一个人再把它当成登录开关去审计。

---

## 3. (2) 敏感数据暴露

### 3.1 逐项结论

| 数据 | 是否出现在响应 | 是否出现在日志/落盘 | 位置 |
|------|----------------|--------------------|------|
| 账号密码 | **是**：详情接口 + CSV 导出（明文） | 落盘为 `v1:` 密文（`password` 列），备份包内也是密文 | `api/accounts.py:724-735,420-443`；`core/db/models_account.py:71-77` |
| AT / RT / id_token / session_token / totp_secret | 列表：否（换成 `has_*` 布尔）✅；详情：`extra_json` 为 `v1:` 密文 ✅；**`token` 列：列表和详情都明文** ❌ | `extra_json` 落盘密文 ✅；**`token` 列明文落盘** ❌ | `api/accounts.py:158-167,260-300`；`core/db/models_account.py:81` |
| grok SSO | **列表明文**（`extra_json.sso` 未被瘦身）❌ | 落盘密文（`v1:`）✅ | `api/accounts.py:165`（`_LIST_HEAVY_EXTRA_KEYS` 漏 `sso`） |
| 面板 key（cpa/sub2api/chatgpt2api/cliproxyapi/sms/yescaptcha…） | 否（`GET /api/config` 置空 + `<key>_set`）✅ | 落盘密文 ✅ | `api/config.py:97-103`；`core/config_store.py:20-34` |
| 旧 provider key（`gptmail_api_key` / `maliapi_api_key`） | 否（不在 `CONFIG_KEYS`）✅ | **明文落盘** ❌ | `core/config_store.py:25`（`_seal` 只认当前 schema 的 secret 集合） |
| 代理串（含 user:pass） | **是**：`/api/proxies`、`/api/proxies/balance`、账号 `extra_json.register_proxy` ❌ | 明文（`proxies.url`）❌ | `api/proxies.py:37-39,128-173` |
| 代理账号密码 | **是**：任务日志 `使用代理: socks5h://user:pass@host` ❌ | 明文入库（`task_logs`/`task_runs`）❌ | `services/task_runners.py:259,568` |
| 凭据加密密钥 | **是**：备份 ZIP 内 `secrets/credential_key` ❌ | 文件 0600（需确认部署权限） | `api/backup.py:37-54` |
| 邮箱池 `share_token`（免登录邮件页凭证） | **是**：`/api/icloud/aliases` 每条都带 ❌ | 明文 | `api/icloud.py:338-346` |

### F2（严重）备份导出 = 全量凭据 + 解密密钥，单请求可取

- **位置**：`api/backup.py:37-54`；打包逻辑 `services/data_bundle.py:265-324`
- **证据**：

```bash
$ curl -s -o bundle.zip -w '%{http_code} %{size_download}\n' http://127.0.0.1:18000/api/backup/export
200 7298220
$ python - <<'PY'
import zipfile; z=zipfile.ZipFile("bundle.zip")
for n in z.namelist(): print(n, z.getinfo(n).file_size)
PY
account_manager.db            25825280
platforms/chatgpt.db              4096
platforms/grok.db                 4096
platforms/icloud.db             659456
platforms/outlook.db             16384
accounts.json                       71
secrets/credential_key              44      ← AES 密钥本身
manifest.json                     1531
```

- **影响**：ZIP 未加密。拿到它 = 拿到 `accounts` 全表（含明文 `token` 列，见 F8）+ `configs`（含加密后的面板 key）+ **解密这些密文的密钥**。任一读权限（含 F1 拿到的 token、任何本机进程、任何 SSRF 打到 localhost）都升级为「全部账号 + 全部配置」的离线完全控制。
- **建议**：导出包默认**不含** `secrets/credential_key`（导入端允许用户单独提供密钥）；或导出时用用户输入的 passphrase 做 AES-GCM 封装（`zipfile` 无原生加密，可用 `pyzipper` 或先加密每个成员）；导出动作要求二次认证（重新输入密码）并写审计日志。

### F3（高）账号列表明文下发 `token` 列（AT / SSO）

- **位置**：`api/accounts.py:260-300`（`_slim_account_row`）、`api/accounts.py:158-167`（注释承诺）
- **证据**：文件里的注释明确写「列表页只拿 AT/RT 判有没有……把它们换成布尔，详情端点仍返回全量」，但 `_slim_account_row` 只处理了 `password*` 与 `extra_json`，**从未 pop `token` 字段**：

```bash
$ curl -s 'http://127.0.0.1:18000/api/accounts?page_size=1&platform=chatgpt' | jq '.items[0] | keys'
["cashier_url","created_at","email","extra_json","has_password","id","legacy_id",
 "platform","region","status","token","updated_at","user_id"]        ← token 在
$ … | jq -r '.items[0].token | length'
1988                                                                  ← 完整 JWT
```

对该 JWT 解 payload（只读解码，未使用）：`iss=https://auth.openai.com`、`aud=["https://api.openai.com/v1"]`、`scp` 含 `model.request` / `model.read` / `offline_access` —— 这是可直接调 OpenAI API 的 **access_token**，不是展示用的 id。

grok 侧更严重，`sso` 根本没进瘦身名单：

```bash
$ curl -s 'http://127.0.0.1:18000/api/accounts?page_size=1&platform=grok' | jq -r '.items[0].extra_json' | jq 'keys'
["grok2api_ingested","grok2api_result","has_access_token","has_refresh_token",
 "mail_provider","register_mode","register_proxy","sso"]              ← sso 明文
# token 与 sso 均为 152 字符、3 段 JWT（段长 36/71/43）
```

- **量化**：`GET /api/accounts?page_size=100000` → `200`，**3,802,989 B**，1057 条，每条带 1988 字符 AT。一次 GET 拖走全池 AT。
- **影响**：与「列表不下发 AT」的设计目标完全相反；任何读接口的滥用（或 F1/F10 的接管）都直接等于交出 1057 个账号的 API 访问权。
- **建议**：`_slim_account_row` 里 `payload.pop("token", None)` 并补 `has_token` 布尔；`_LIST_HEAVY_EXTRA_KEYS` 增加 `"sso"`；补一条契约测试（`tests/test_accounts_list_payload.py` 已有类似断言，扩展它：断言列表里不存在任何 ≥100 字符的 JWT 形状字符串）。

### F4（高）任务日志把代理账号密码写进库并回吐

- **位置**：写入 `services/task_runners.py:259`、`:568`（`_api.log(task_id, f"使用代理: {_proxy}")`）；读出 `api/tasks.py:636-657`（`/api/tasks/logs`）、`api/tasks.py:789-819`（`/api/tasks?include_logs=true`）、`api/tasks.py:783-787`（`/api/tasks/{id}` 的 snapshot）
- **证据**：

```bash
$ curl -s 'http://127.0.0.1:18000/api/tasks?include_logs=true' > t.json   # 374,349 B / 837 个任务
# 对响应做凭据形状扫描（JWT / v1: 密文 / socks5://user:pass@ / password=…）：
secret-like hits: 75
  [0].logs[2]   'socks5h://<user>:<pass>@'      ← 代理账号+密码
  [4].logs[35]  'socks5h://<user>:<pass>@'
  ...
# 实际日志行（原文）：
[16:51:36] 使用代理: socks5h://<user>:<pass>@<proxy-host>:6740
```

- **影响**：日志同时存在于 `task_logs` / `task_runs`（备份包也带走）、`/api/tasks*` 响应、以及 uvicorn stdout（容器日志/`docker logs`）。代理服务商账号密码泄漏 = 出口 IP 池被他人白嫖或封禁。
- **建议**：写日志前统一过 `mask_proxy_url()`（`core/proxy_utils.py` 已有归一化函数，加一个只保留 `socks5h://***@host:port` 的掩码版）；历史数据做一次性清洗（`UPDATE task_logs SET message=…` 或正则脱敏脚本）；日志列表接口再叠一层输出侧脱敏，防止旧数据继续外泄。

### F5（高）代理接口返回带账号密码的代理串

- **位置**：`api/proxies.py:37-39`（`list_all` 原样返回）、`api/proxies.py:128-173`（`balance` 里 `"url": url`）、`api/proxies.py:176-192`（rebalance 的 `items` 也含 url）
- **证据**：

```bash
$ curl -s http://127.0.0.1:18000/api/proxies
[{"url":"socks5://<user>:<pass>@<proxy-host>:5328","id":2,"region":"","success_count":338,...}, …]
$ curl -s http://127.0.0.1:18000/api/proxies/balance
{"items":[{"id":2,"url":"socks5://<user>:<pass>@<proxy-host>:5328", … "bound_total":290}, …]}
```

- **影响**：与 F4 同一份凭据的第二条泄漏路径；`/api/proxies` 是页面轮询接口，出现频率远高于日志。
- **建议**：响应统一返回 `masked_url`（`socks5://***:***@host:port`）+ `has_auth` 布尔；需要编辑时走单独的、要求重新认证的 `GET /api/proxies/{id}/secret`。

### F8（高）`token` 列明文落盘，绕过「凭据加密」

- **位置**：`core/db/models_account.py:81`（`token: str = ""`，无 `sa_column`、无加解密）；对照同文件 `:71-77` 的 `password_cipher` 与 `extra_json` 内的 `v1:` 密文
- **证据**（只读查询沙箱副本）：

```sql
select count(*) from accounts where token<>'' and token not like 'v1:%';   -- 1057
select count(*) from accounts where password<>'' and password not like 'v1:%'; -- 0
-- 样例：chatgpt token_len=1497 head='eyJhbG'（JWT）；grok token_len=152 head='eyJ0eX'
```

- **影响**：`core/credential_fields.py:25-26` 明确写着「`token` 列 = 平台主凭证的镜像（chatgpt → AT，grok → SSO）」。也就是说：即使攻击者**拿不到** `credential_key`，只要能读到 DB 文件（备份包、卷快照、SQLite 文件被顺手拷走），就能直接拿走全部 AT/SSO —— 「凭据加密落盘」在最关键的一列上没有生效。
- **建议**：把 `token` 也纳入 `encrypt_text`/`decrypt_text`（属性化，像 `password_cipher` 那样保留列名以免迁移），或干脆取消该冗余列、统一从 `extra_json` 派生（`token_column_field()` 已定义映射规则，改为读时计算）；迁移脚本对存量 1057 行做一次加密回填。

### F9（中）明文密码 CSV 导出

- **位置**：`api/accounts.py:419-443`
- **证据**：

```bash
$ curl -s -o exp.csv -w '%{http_code} %{size_download}B %{content_type}\n' \
    'http://127.0.0.1:18000/api/accounts/export?platform=chatgpt'
200 56588B text/csv; charset=utf-8
$ head -2 exp.csv
platform,email,password,user_id,region,status,cashier_url,created_at
chatgpt,skyway-finale-6c@icloud.com,<明文密码>,…            # 667 行
```

- **问题**：GET、无二次认证、无条数上限、无审计日志、响应无 `no-store`（F13）；密码列逐行 `acc.password` 解密（`api/accounts.py:434`）。
- **建议**：导出改为 POST + 重新输入面板密码（或 TOTP）确认；默认**不含**密码列（提供显式 `include_password=true` 才带，并记审计）；响应加 `Cache-Control: no-store`；对大导出加分页/流式 + 限速。

### F18（中）免登录邮件页的 `share_token` 被批量下发

- **位置**：`api/icloud.py:338-346`（列表含 `share_token`）、`api/shared_mail.py:149`（`GET /m/{share_token}` 无鉴权）
- **证据**：`GET /api/icloud/aliases` → 712,390 B / 1941 条，每条形如
  `{"id":2312,"address":"…@icloud.com","share_token":"<22 字符>", …}`；
  `GET /m/<token>` → `200`（未登录，1883 B 邮件页）。
- **影响**：`share_token` 是「链接即权限」的 bearer 凭证，被当成普通字段塞进列表 = 任何一次列表读取（含浏览器缓存、前端日志、截图、XSS）都会扩散 1941 个可读收件箱的永久链接；这些收件箱正是 OTP 的落点。
- **建议**：列表只回 `has_share_link`；需要分享时单独 `POST /api/icloud/aliases/{id}/share` 返回一次性 token；`/m/` 侧加最小限速与 `Referrer-Policy: no-referrer`。

### 3.2 已确认**没有**泄漏的面（避免过度整改）

- `GET /api/config`：10 个 schema 内口令键全部置空 + `<key>_set` 布尔（实测 `yescaptcha_key:""`、`cpa_api_key_set:true`，无明文）；
- `GET /api/config/schema`：只有键名/分组/是否 secret；
- 账号详情：`extra_json` 内 AT/RT/ST/id_token/totp_secret 均为 `v1:` 密文（`password` 例外，是明文，见 F9 说明）；
- `POST /api/integrations/panels/{key}/comparison`：636 行对比结果只含 email/时间/差异项，**不含**任何凭据值（对响应做 JWT 形状扫描命中 0）；
- 错误响应中未观察到 Python traceback（`debug` 未开）。

---

## 4. (3) 输入校验

### 4.1 批量接口条数上限一览

| 端点 | 模型/参数 | 上限 | 评价 |
|------|-----------|------|------|
| `POST /api/accounts/batch-delete` | `ids: list[str\|int]` | 1000（`api/accounts.py:693`） | ✅ |
| `POST /api/proxies/batch-delete` | `ids: list[int]` | 1000（`api/proxies.py:82`） | ✅ |
| `POST /api/tasks/logs/batch-delete` | `ids` | 1000（`api/tasks.py:666`） | ✅ |
| `POST /api/actions/{platform}/{action}/batch` | `account_ids` / `all_filtered` | 1000（`api/actions.py:138,167`） | ✅ |
| `POST /api/accounts/import` | `lines: list[str]`（`api/accounts.py:40-46`） | **无** | ❌ F14 |
| `POST /api/proxies/bulk` | `proxies: list[str]`（`api/proxies.py:18-21`） | **无** | ❌ F14 |
| `POST /api/outlook/batch-import` | `data: str`（`api/outlook.py:9-12`） | **无**（仅字符串长度天然受限） | ❌ F14 |
| `POST /api/outlook/pool-status/import` | `ids: List[int]`（`api/outlook.py:22-24`） | **无** | ❌ F14 |
| `POST /api/icloud/aliases/batch-delete` | `ids`（`api/icloud.py:401-407`） | **无** | ❌ F14 |
| `POST /api/mail-imports/batch-delete` | `items`（`services/mail_imports/schemas.py:90-99`） | **无** | ❌ F14 |
| `GET /api/accounts?page_size=` | int（`api/accounts.py:312-315`） | **无上限**，只 `max(1, …)` | ❌ F14 |
| `GET /api/tasks/logs?page_size=` | int（`api/tasks.py:636-657`） | **无上限** | ❌ F22 |
| `GET /api/mail-imports/snapshot?preview_limit=` | int（`api/mail_imports.py:24`） | 1..500（但由 pydantic 事后校验，见 F16） | ⚠️ |
| `POST /api/backup/import` | 原始 body | 2 GB + 强制 `Content-Length`（`api/backup.py:70-95`） | ✅ |

**F14 证据**：

```bash
$ curl -s -o /dev/null -w '%{http_code} %{size_download}\n' \
    'http://127.0.0.1:18000/api/accounts?page_size=100000'
200 3802989                     # 1057 条全量 + 每条 1988 字符 token
$ curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:18000/api/accounts?page_size=0'
200                             # 被 `or 20` 兜成 20，不报错也不提示
```

**影响**：单进程 uvicorn（`docker-compose.yml` 无 `--workers`），一个 `page_size=10^7` 或 `lines=[…]` 十万条的请求就能把进程内存顶满；`/api/accounts` 还会顺带把 1057 个 AT 序列化一遍（F3 放大）。

**建议**：统一引入 `PageParams`（`page_size: int = Field(20, ge=1, le=200)`）与批量 `max_length`（1000，和删除端点对齐）；`import` 用 `Field(max_length=5000)` 并把单行长度限制在 4 KB；`preview_limit` 直接声明成 `Query(100, ge=1, le=500)` 让 FastAPI 出 422。

### 4.2 id 类型与路径参数

- 账号 id 已 UUID 化，但 `BatchDeleteRequest.ids: list[str | int]`（`api/accounts.py:54-60`）接受任意字符串，无长度/格式校验；`delete_many` 内部 `str()` 归一，SQL 参数化 → **无注入**，但可用超长 id 制造无谓查询。建议 `ids: list[UUID]`。
- 代理 id 为 `int`（`api/proxies.py:33-34,68-74`），`"notanint"` → 路由不匹配 → 404（实测），语义可接受。
- 平台路径参数 `/{platform}/…`（`api/actions.py:291,312,436`）经 `platform_session_from_path` → `url_for()`；**未知平台回落默认库**（`core/db/registry.py:67-80`），不会拼出文件路径 → **无路径穿越**（详见 §7.2）。
- `account_id` 作为路径参数直接进 `session.get(AccountModel, id)`（`api/actions.py:445`），参数化查询，安全。

### 4.3 文件上传

- 只有 `POST /api/backup/import` 是真「上传」，且刻意不用 multipart（`api/backup.py:1-13`），直接收 `application/zip` 字节体。
- 校验链完整：`Content-Length` 必须存在（411）→ 声明值 ≤ 2 GB（413）→ 实读长度复核（413）→ ZIP 可打开 → 成员数 ≤ `MAX_MEMBERS` → 每个登记成员大小 ≤ `MAX_DB_BYTES` → sha256 比对 → SQLite 魔数校验（`services/data_bundle.py:417-458`）。
- **zip-slip 已防**：`_safe_member_name` 拒绝绝对路径与 `..`（`services/data_bundle.py:409-414`）。
- `X-Bundle-Filename` 头只截断 200 字符并回显到结果的 `source_filename`（`api/backup.py:97-100`、`services/data_bundle.py:757`），**不参与任何路径拼接** → 无穿越。
- 唯一缺口：2 GB 上限在**单进程内存**里 `await request.body()`（`api/backup.py:89`）→ 一次合法但巨大的导入即可 OOM。建议降到 256 MB 并改成流式落盘到临时文件后再校验。

---

## 5. (4) 错误语义：500 与 4xx 的边界

### 5.1 现状（实测分类）

| 请求 | 状态 | 响应 | 评价 |
|------|------|------|------|
| `/api/accounts?page=abc` | 422 | FastAPI 标准 `int_parsing` 明细 | ✅ 合理 |
| `/api/accounts/does-not-exist` | 404 | `{"detail":"账号不存在"}` | ✅ |
| `/api/tasks/nope` | 404 | `{"detail":"任务不存在"}` | ✅ |
| `/api/proxies/notanint` | 404 | `{"detail":"Not Found"}` | ✅（路由不匹配） |
| `/api/mail-imports/snapshot?type=nope` | 400 | `{"detail":"不支持的邮箱导入类型: nope"}` | ✅ |
| `/api/mail-imports/snapshot?preview_limit=100000` | 400 | `{"detail":"1 validation error for MailImportSnapshotRequest\npreview_limit\n Input should be less than or equal to 500 [type=less_than_equal, input_value=100000, …]"}` | ❌ F16 |
| `/api/mail-imports/snapshot?preview_limit=abc` | 422 | 标准明细 | ✅ |
| `/api/integrations/panels/nope/comparison` | 404 | `{"detail":"未知面板: nope"}` | ✅ |
| `/api/definitely-not-a-route` | 404 | `{"detail":"Not Found"}` | ✅（未被 SPA 兜成 200，`main.py:583-584` 的排除生效） |
| `/docs` | 200 | **SPA index.html**（1063 B），不是文档页 | ⚠️ 见 §7.3 |

### F16（中）参数越界把 pydantic 内部细节当 `detail` 回给客户端

- **位置**：`api/mail_imports.py:26-38` —— 处理器里手工构造 `MailImportSnapshotRequest`，`ValidationError` 继承 `ValueError`，被 `except ValueError` 捕获后 `HTTPException(400, str(exc))`。
- **证据**：上面表格第 6 行；文案里含**内部模型类名** `MailImportSnapshotRequest`、字段名、约束名 `less_than_equal`、以及回显的输入值。
- **影响**：低危信息泄露（暴露内部 schema），且语义错误 —— 客户端参数问题应当是 422 而不是 400，前端无法统一处理。
- **建议**：把 `preview_limit` 声明成 FastAPI 查询参数 `Query(100, ge=1, le=500)`；或捕获 `ValidationError` 后只回 `{"detail":"preview_limit 必须在 1..500"}`。

### F15（中）`str(exc)` 直接进响应体（500 与 200 两种）

- **位置**（全部为 `f"…: {str(e)}"` 形式）：
  - `api/accounts.py:713` `raise HTTPException(500, f"批量删除失败: {str(e)}")`
  - `api/tasks.py:697` `raise HTTPException(500, f"批量删除任务历史失败: {str(e)}")`
  - `api/backup.py:46,110,122` `f"导出失败/导入失败/读取数据概况失败: {exc}"`
  - `api/actions.py:465-466` `return {"ok": False, "error": str(e)}`（**HTTP 200**）
  - `api/integrations.py:232` `{"name":"error","ok":False,"msg":str(e)}`（**HTTP 200**）
  - `api/sms.py:89,100` `f"查询余额失败: {exc}"` / `f"查询国家排名失败: {exc}"`（502）
- **证据**：静态；典型 `str(exc)` 内容为 `sqlite3.OperationalError: no such column: …`、`sqlalchemy.exc.…`、`requests.exceptions.ConnectionError: … socks5h://user:pass@…`、`FileNotFoundError: /data/…` —— 三类内部信息（SQL 结构、代理凭据、绝对路径）都可能随之上屏。
- **另**：`api/actions.py:465` 与 `api/integrations.py:232` 用 **200** 表达失败，调用方只能靠 `ok` 字段区分 —— 与仓库其它端点（4xx/5xx）不一致，属于契约缺陷。
- **建议**：对外只回稳定的错误码 + 一句人话（`{"detail":"批量删除失败","code":"batch_delete_failed","request_id":…}`），原始异常只进 `logger.exception`；失败一律用 4xx/5xx（业务性失败用 409/422），不要 200。

### 5.2 未捕获异常

- `main.py` 未注册全局异常处理器，FastAPI 默认对未捕获异常返回 `500 {"detail":"Internal Server Error"}`（无 traceback）—— 这一点是好的。
- 但若响应已经开始流式输出（`api/accounts.py:439` 的 `StreamingResponse`、`api/tasks.py:738` 的 SSE），中途异常只会截断连接，客户端看到的是**不完整 CSV/事件流**而不是错误码。建议导出前先物化到临时文件再流式发送。

---

## 6. (5) 破坏性接口的幂等与确认

| 端点 | 破坏性 | 幂等 | 确认/dry-run | 结论 |
|------|--------|------|--------------|------|
| `POST /api/accounts/batch-delete`（`api/accounts.py:683-714`） | 删账号 | ✅ 二次调用回 `deleted:0` + `not_found:[…]` | ❌ 无 | 可接受（有上限、返回 not_found）；建议加 `confirm=true` 或预演 |
| `POST /api/proxies/batch-delete`（`api/proxies.py:77-94`） | 删代理 | ✅ 同上 | ❌ | 同上 |
| `POST /api/tasks/logs/batch-delete`（`api/tasks.py:660-697`） | 删历史 | ✅ | ❌ | 同上 |
| `POST /api/proxies/balance`（`api/proxies.py:176-192`） | **批量改账号绑定** | ⚠️ 非幂等（每次重算） | ✅ `dry_run=True` 默认 | 设计良好；但 `dry_run=false` 一次调用即可给上千账号换出口，**无二次确认、无回滚** |
| `POST /api/integrations/panels/{k}/sync`（`api/integrations.py:81-144`） | **覆盖本地 AT/RT/id_token** | ⚠️ | ❌ 无 dry-run | 缺确认；建议先出「将改动 N 个账号」预览 |
| `POST /api/integrations/panels/{k}/push?delete_old=true`（`api/integrations.py:146-163`） | **删远端旧记录** | ⚠️ | ❌ 破坏性开关在 query 里 | 建议改 body 字段并默认 false + 显式确认 |
| `POST /api/icloud/aliases/batch-delete`（`api/icloud.py:401-407`，`remote: bool = True` at `:135`） | **删 Apple 侧别名（不可逆）** | ⚠️ | ❌ 默认就删远端 | 最危险：默认值站在「不可逆」一侧，且 ids 无上限 |
| `DELETE /api/icloud/aliases/{id}?remote=true`（`api/icloud.py:427-433`） | 同上 | ✅ | ❌ | 建议 `remote` 默认 false |
| `POST /api/tasks/logs/cleanup`、`/runs/cleanup`（`api/tasks.py:700-737`） | 按天数删历史 | ✅ | ❌ | `older_than_days` 有 `ge=1,le=MAX`（`api/tasks.py:98`），可接受 |
| `POST /api/backup/import`（`api/backup.py:57-112`） | **整库替换 + 换密钥** | ❌ | ⚠️ 自动备份 + 运行中任务 409 | 机制最完整的一个：导入前 `_backup_current_data("import")`（`services/data_bundle.py:609`）、原子替换（`:611-618`）、失败回滚。建议再加「包内容摘要 + 显式 confirm」 |
| `POST /api/accounts/import`（`api/accounts.py:486-…`） | 覆盖同邮箱账号字段 | ✅（同邮箱更新） | ❌ | 无上限（F14）；`format=json` 会把 `lines` 直接 `"\n".join` 后 `json.loads`，十万行时内存翻倍 |

**通用建议**：给所有「一次影响 >N 个对象」的写接口统一加两层保护 ——(a) `dry_run` 或 `preview` 必填其一；(b) 服务端生成 `confirm_token`（把请求体哈希进去，5 分钟内有效），客户端必须回传。这样既防误点，也防「CSRF 式」的自动化滥用。

---

## 7. (6) CORS / 静态文件 / 路径穿越

### 7.1 CORS（F12）

- **位置**：`main.py:269-274` —— `allow_origins=["*"]`、`allow_methods=["*"]`、`allow_headers=["*"]`，未设 `allow_credentials`（默认 False）。
- **证据**：

```bash
$ curl -s -D - -o /dev/null -H 'Origin: https://evil.example' http://127.0.0.1:18000/api/auth/status | grep -i access-control
access-control-allow-origin: *

$ curl -s -D - -o /dev/null -X OPTIONS \
    -H 'Origin: https://evil.example' \
    -H 'Access-Control-Request-Method: POST' \
    -H 'Access-Control-Request-Headers: authorization,content-type' \
    http://127.0.0.1:18000/api/accounts | grep -i access-control
access-control-allow-origin: *
access-control-allow-methods: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT, QUERY
access-control-allow-headers: authorization,content-type
access-control-max-age: 600
```

- **影响评估**（不过度定级）：鉴权用 `Authorization` 头而非 Cookie，`allow_credentials` 为 False，因此**经典 CSRF 不成立**，恶意站点也读不到 `localStorage` 里的 token。真实风险是三条：
  1. 任意站点可**跨域读** `/api/auth/status`，并可直接调用 `/api/auth/login` 读响应 → 浏览器内即可做密码枚举（受 F7 影响时还可绕过退避）；
  2. 任何 token 泄漏（F3/F6/F10、XSS、日志、截图）都能被**任意源**直接使用，无 origin 约束这一层兜底；
  3. 预检把 `DELETE/PATCH/PUT` 全放开，等于对外宣告「这些方法可用」。
- **建议**：改成显式白名单（`APP_CORS_ORIGINS`，默认只允许同源 / `http://127.0.0.1:8000`）；`allow_methods` 收敛到实际用到的集合；生产环境同源部署时直接**不挂** CORS 中间件。

### 7.2 静态文件与路径穿越（结论：**未发现穿越**）

- `/assets` 用 `StaticFiles`（`main.py:566-571`），Starlette 内部做 `os.path.realpath` + 前缀校验；
- SPA 兜底 `@app.get("/{full_path:path}")`（`main.py:573-610`）**只用 `full_path` 判断是否 `/api` 前缀**，随后固定返回 `static/index.html`，不拼接用户路径；
- 实测（`--path-as-is`，保留原始编码）：

```
/assets/../../../../etc/passwd          404 {"detail":"Not Found"}
/assets/%2e%2e/%2e%2e/etc/passwd        404 {"detail":"Not Found"}
/api/../main.py                         404 {"detail":"Not Found"}
/..%2f..%2fetc%2fpasswd                 200 text/html 1063B   ← SPA index，不是 /etc/passwd
/%2e%2e/%2e%2e/etc/passwd               200 text/html 1063B   ← 同上
```

两个 200 是「未知前端路由回 index.html」的预期行为（`main.py:573-582` 的 docstring 明确说明），响应体是 SPA 外壳，**不含任何文件内容**。

- 鉴权中间件的前缀判定同样不可穿越（实测）：

```
/api/auth/../accounts?page_size=1        404
/api/auth/%2e%2e/accounts?page_size=1    404
/api//accounts?page_size=1               404
/api/./accounts?page_size=1              404
/API/accounts?page_size=1                200 text/html（SPA，非 API）
```

- 备份导入的 zip-slip 已防（§4.3）。
- **唯一建议**：`/..%2f..%2fetc%2fpasswd` 这类明显畸形的路径回 200 HTML 会让扫描器/日志噪声很大，可在兜底里对「含 `..` 或 `%2e` 的路径」直接 404。

### 7.3 其它静态/头部问题

- **F19（低）**：`/`、`/api/**` 均无 `Content-Security-Policy`、`X-Content-Type-Options: nosniff`、`X-Frame-Options`/`frame-ancestors`（实测响应头只有 `date/server/cache-control/etag/content-type/vary`）。SPA 里存着 token（localStorage），缺 CSP 会放大任何 XSS 的影响面。建议加 `default-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'`，并对 `/m/**` 保留现有 iframe `srcdoc` + `script-src 'none'` 策略（`api/shared_mail.py:72-95` 已做，值得保留）。
- **`/docs` 返回 200**：不是文档泄漏（`docs_url=None`，`main.py:199`），只是 SPA 兜底把 `/docs` 当成了前端路由。若要更干净，把 `/docs`、`/redoc`、`/openapi.json` 也加进兜底的排除名单。
- **F13（中）**：凭据类响应缺 `Cache-Control: no-store`（实测 `/api/accounts/{id}`、`/api/accounts/export`、`/api/proxies`、`/api/backup/export` 都只有 `content-type`）。浏览器「后退」、共享代理、磁盘缓存都可能留下明文密码/AT/备份包。建议对上述路径统一加 `Cache-Control: no-store, private` + `Pragma: no-cache`。

---

## 8. 修复优先级

**P0（当天，挡住接管链）**

1. F1：`/api/auth/setup` 未设密码时只允许回环 / 一次性初始化 token；中间件改显式白名单。
2. F2：备份导出不再默认携带 `secrets/credential_key`；导出要求二次认证。
3. F8 + F3：`token` 列加密落盘；列表响应 `pop("token")` 且把 `sso` 加入瘦身名单。
4. F4 + F5：日志与代理接口统一掩码 `socks5://***@host:port`。

**P1（本周）**

5. F6 + F10：token 加版本号可吊销；`setup`/`disable` 要求旧密码。
6. F7：退避计数改用 `request.client.host` + 可信代理配置。
7. F9：密码导出改 POST + 二次认证 + 默认不含密码列。
8. F14：统一分页与批量上限（200 / 1000）。
9. F15 + F16：错误体只留稳定 code + 人话；4xx/5xx 语义统一。

**P2（两周内）**

10. F11：无密码模式的「仅回环」判定改为「仅初始化 token」，并在文档中声明不支持反代。
11. F12：CORS 白名单化。
12. F13 + F19：`no-store` 与安全响应头。
13. F17：破坏性接口加 `dry_run`/`confirm_token`，`remote` 默认改 false。
14. F18：`share_token` 不再随列表下发。
15. F20–F25：信息面收敛（`/api/runtime` 去掉绝对路径、`_login_failures` 清理、SMS 上游错误不直出）。

---

## 附录 A：探测矩阵（全部 GET/HEAD，除标注外）

| # | 请求 | 状态 | 关键观察 |
|---|------|------|----------|
| 1 | `GET /api/auth/status` | 200 | `{"has_password":false,"has_totp":false}` |
| 2 | `GET /api/accounts?page_size=2` | 200 / 7,528 B | 行内 **含 `token`**（F3） |
| 3 | `GET /api/accounts?page_size=100000` | 200 / 3,802,989 B | 1057 条全量 + AT |
| 4 | `GET /api/accounts/{uuid}` | 200 | `password` 明文 16 字符；`extra_json` 为 `v1:` 密文 |
| 5 | `GET /api/accounts/export?platform=chatgpt` | 200 / 56,588 B CSV | 667 行明文密码（F9） |
| 6 | `GET /api/config` | 200 / 2,338 B | 口令键全空 + `*_set`（✅） |
| 7 | `GET /api/proxies` | 200 / 926 B | 5 条 `socks5://user:pass@…`（F5） |
| 8 | `GET /api/proxies/balance` | 200 | 同上 + 绑定计数 |
| 9 | `GET /api/tasks?include_logs=true` | 200 / 374,349 B | 75 处凭据形状命中（F4） |
| 10 | `GET /api/tasks/logs?page_size=999999` | 200 / 356,026 B | 无上限（F22） |
| 11 | `GET /api/backup/export` | 200 / 7,298,220 B | 8 成员，含 `secrets/credential_key`（F2） |
| 12 | `HEAD /api/backup/export` | 405 | 未实现 HEAD（GET-only 路由） |
| 13 | `GET /api/backup/info` | 200 | 泄漏绝对路径 + 各库行数（低） |
| 14 | `GET /api/icloud/aliases` | 200 / 712,390 B | 1941 条，每条带 `share_token`（F18） |
| 15 | `GET /m/<share_token>` | 200 / 1,883 B | 未登录可读邮件（设计如此） |
| 16 | `GET /api/integrations/panels/{cpa,sub2api,chatgpt2api,grok2api}/comparison` | 200 | 388 KB / 154 KB / 204 KB / 294 KB；触发外网请求；**不含凭据值** |
| 17 | `GET /api/mail-imports/snapshot?preview_limit=100000` | 400 | 回显 pydantic 模型名（F16） |
| 18 | `GET /api/openapi.json` | 404 | docs 已关（✅） |
| 19 | `GET /docs` | 200 | SPA index，非文档页 |
| 20 | `GET /api/definitely-not-a-route` | 404 | 未被 SPA 兜底（✅） |
| 21 | `OPTIONS /api/accounts`（预检） | 200 | `allow-origin: *` + 全方法（F12） |
| 22 | 5 条路径穿越变体 | 404/200 | 无文件读取（✅，§7.2） |
| 23 | 4 条鉴权前缀穿越变体 | 404 | 无绕过（✅） |
| 24 | `POST /api/auth/setup`（**仅一次性副本**，`TestClient` 非回环） | 200 | 未认证设密成功并返回 token（F1） |

## 附录 B：未覆盖 / 需要下一轮补的

- **并发与竞态**：`_login_failures`、`_pending_2fa` 都是进程内 dict（`api/auth.py:197-266`），多 worker / 多副本部署下退避与 2FA 尝试计数**不共享**，可被「换 worker」绕过 —— 需要按部署形态确认是否真的单进程。
- **`api/contribution.py`**：4 个 POST（`/quota-stats`、`/key-info`、`/redeem`、`/generate-key`）不在本次指定范围内，未审；它们会把 `contribution_key` 发往 `contribution_server_url`（默认 `http://new.xem8k5.top:7317/`，明文 HTTP），建议单独一轮看「密钥外发 + 明文传输」。
- **Solver 端口 8889**：`docker-compose.yml:33-40` 明确写着「solver 接口**没有鉴权**，绝不能直接对外」，容器内绑 `0.0.0.0:8889`，仅靠 compose 的 `127.0.0.1:8889` 映射兜底。不走 compose（裸 `docker run -p 8889:8889`）时该无鉴权服务会直接暴露 —— 与本次 `api/*` 无关，但属于同一台机器上的旁路风险，建议下一轮并入。
- **`api/actions.py` 的平台动作面**：`_execute_platform_action` 会调用 `platforms/**` 里的真实业务（含密码登录、改密、上传），本次只审了错误语义与批量上限，动作本身的行为安全（是否会把密码写日志）需结合 `platforms/chatgpt` 再看。
- **真实 `:8000` 实例**：审计期间未运行，所有结论来自 HEAD 代码 + 数据副本；若线上跑的镜像与本 HEAD 不一致（工作区当时有未提交改动），需在部署机上复核 F1/F3/F8 三条。
