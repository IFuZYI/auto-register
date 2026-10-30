# api-security 审计

[MODE: SANDBOX]

- **仓库**：`/data/workspace/Register`，分支 `main`，HEAD **`c93b71f`**（`fix(auth): 堵住 setup 抢注接管 / 改密不要旧密码 / 令牌不可吊销 / XFF 绕退避 / CORS 全开`）
- **解释器**：`.venv/bin/python`（3.12）
- **审计时间**：2026-10-30 18:30–18:45 UTC
- **范围**：`api/*.py`（15 个路由模块，4537 行）、`main.py`、`core/net_utils.py`、`core/config_store.py`、`core/config_schema.py`、`core/db/models_account.py`、`services/task_runners.py`、`services/account_export.py`、`services/data_bundle.py`（仅导出侧）
- **方法**：静态通读 + 对运行中的 `127.0.0.1:8000` 做 **GET/HEAD 探测** + 真实库 `data/account_manager.db` 只读 SQL（`file:...?mode=ro`）
- **只读约束**：全程**没有**发出任何 POST/PUT/PATCH/DELETE，没有写真实库，没有改任何代码。本文件是本轮唯一写入的文件。

### 0.1 取证基线与两个必须说明的前提

1. **本轮 `:8000` 有实例在跑**（上一轮审计时没有），所以结论里第一次出现**运行时证据**而不只是静态推断。但它的 `DATA_DIR` 不是仓库的 `data/`：`GET /api/backup/info` 返回 `"data_dir": "/root/.hermes/cache/scratch/regperf/data"`（一个性能测试用的同源副本）。真实库 `data/account_manager.db` 已用只读连接逐表比对，行数完全一致（accounts 1057 / proxies 5 / task_runs 837 / task_logs 1661 / configs 143），所以运行时观察到的数据形状可以代表真实库。
2. **仓库在被并发修改**。审计期间鉴权加固从「工作区未提交」变成了提交 `c93b71f`（18:36），`api/proxies.py` 又出现了新的未提交改动（正在给代理 URL 打码，见 F5）。**本报告所有 `path:line` 以 `c93b71f` 为准**；`api/proxies.py` 因工作区正在改，行号取 `c93b71f` 版本并注明。

### 0.2 与上一版报告的关系

本目录下原有的 `api-security.md`（18:31 落盘）审的是 `4680c8d`，其 F1/F6/F7/F10/F12 五条已在 `c93b71f` 修掉。为避免团队正在引用的编号（提交信息里已经出现「安全审查 2026-10-30 F5」）失效，**本轮保留 F1–F25 原编号并给出复核状态**，新发现另编 N0–N6。

---

## 1. 结论摘要

### 1.1 本轮新增

| ID | 严重度 | 主题 | 位置 | 一句话 |
|----|--------|------|------|--------|
| **N0** | **严重** | 部署 | `main.py:258-264` vs 运行进程 | **在跑的进程是加固前代码**：CORS 仍回 `*`、预检仍宣告 `QUERY`；进程 18:25:13 启动，早于 `c93b71f`（18:36）—— 五条鉴权修复在当前实例上等于没生效 |
| **N1** | 中 | 鉴权 | `api/auth.py:446-453`、`main.py:213` | `/api/auth/logout` **无需认证**，任何能连到端口的人都能轮换全局 JWT 密钥 → 全体会话被踢、每次请求写一次 `configs` 表 |
| **N2** | 中高 | SSRF / 密钥外带 | `api/contribution.py:33-53,78-90` | 4 个 POST 的 `server_url` **由请求体控制**，服务端照此发请求，并把 `Authorization: Bearer <contribution_key>` 送到该地址、把响应体原样回传 |
| **N3** | 中 | SSRF / 凭据外带 | `api/icloud.py:104-119,257-285` | `base_url` 同样由请求体控制，`admin_password` 会被发往任意 host，响应回传 |
| **N4** | 中 | 敏感数据 | `core/config_store.py:20-27` + `core/config_schema.py:135-137` | `auth_jwt_secret` / `auth_password_hash` 不在 `SECRET_CONFIG_KEYS` → **明文存 `configs` 表**，并随备份包外带；拿到备份即可伪造管理员 JWT |
| **N5** | 中 | 鉴权 | `api/auth.py:370-380` | F10 只修了一半：`/api/auth/disable` 仍**只要 token、不要旧密码**就能清空密码与 TOTP（`setup` 已要求 `old_password`） |
| **N6** | 低 | 可观测性 | `main.py:45-84,290-309` | `_read_git_version()` 失败时返回 `"未知"`，而 `stale = code != disk` → `"未知" != "未知"` 为 `False`：**「改完没重启」这个守卫在取不到版本时谎报「已是最新」**（当前实例正是这个状态） |

### 1.2 前一轮 F1–F25 复核

| ID | 主题 | 状态 | 依据 |
|----|------|------|------|
| F1 | `setup` 抢注接管 | ✅ **已修** | `api/auth.py:352-362` 未设密码时只允许回环（`APP_ALLOW_REMOTE_SETUP=1` 可显式放开）；`main.py:213` 的整段放行仍在，但被端点自校验兜住 |
| F2 | 备份含 `credential_key` | ❌ **未修** | 实测 ZIP 8 个成员，`secrets/credential_key` 44 B |
| F3 | 列表明文下发 `token` / `sso` | ❌ **未修** | 实测列表首行 `token` 长 1988；grok 行 `extra_json.sso` 152 字符 |
| F4 | 任务日志写代理账号密码 | ❌ **未修** | `?include_logs=true` 响应里 761 处 `socks5h://user:pass@` |
| F5 | 代理接口回明文 URL | 🟡 **修复中（未提交）** | `c93b71f:api/proxies.py:157` 仍是 `"url": url`；工作区已有 `_public_proxy()` 打码补丁 |
| F6 | token 不可吊销 | ✅ **已修** | `_rotate_jwt_secret()`（`api/auth.py:47-57`）+ logout/改密/关鉴权三处调用；副作用见 N1 |
| F7 | XFF 绕登录退避 | ✅ **已修** | `core/net_utils.py:55-70`，只有 `APP_TRUST_PROXY=1` 才看 XFF |
| F8 | `token` 列明文落盘 | ❌ **未修** | 只读 SQL：`token<>'' and token not like 'v1:%'` = **1057 / 1057** |
| F9 | 明文密码 CSV 导出 | ❌ **未修** | `api/accounts.py:428-451`，实测 667 行含 16 字符明文密码 |
| F10 | 持 token 即可改密/关鉴权 | 🟡 **半修** | `setup` 已要求旧密码；`disable` 未改（见 N5） |
| F11 | 回环兜底在同机反代下失效 | ❌ **未修** | `main.py:225` 仍只看 `request.client.host`，`docs/` 也没有「无密码模式不支持反代」的声明 |
| F12 | CORS 全开 | ✅ **源码已修 / ❌ 运行未生效** | `main.py:258-264` 已改 `allow_origin_regex`；实测进程仍回 `access-control-allow-origin: *`（N0） |
| F13 | 凭据响应无 `no-store` | ❌ **未修** | `/api/accounts` 响应头只有 `content-length/content-type/vary` |
| F14 | 分页/批量无上限 | ❌ **未修** | `api/accounts.py:315` 仍 `max(1, …)`；实测 `page_size=100000` → 200 / 3,802,434 B / 2.35 s |
| F15 | `str(exc)` 进响应体 | ❌ **未修** | `api/accounts.py:713`、`api/tasks.py:697`、`api/backup.py:46,110,122` 等 8 处 |
| F16 | pydantic 细节回显 | ❌ **未修** | 实测 `preview_limit=100000` → 400 + `MailImportSnapshotRequest` / `less_than_equal` / pydantic 文档链接 |
| F17 | 破坏性接口无确认/dry-run | ❌ **未修** | `api/icloud.py` 的 `remote` 默认仍是 `True` |
| F18 | `share_token` 随列表下发 | ❌ **未修** | 实测 712,390 B，每条 22 字符 token；`GET /m/<token>` 未登录 200 |
| F19 | 无 CSP / nosniff / X-Frame-Options | ❌ **未修** | 实测 `/` 与 `/api/**` 只有 `cache-control/content-type/vary` |
| F20 | `auth/status` 泄露是否设密码 | ❌ **未修（设计所需）** | 登录页要用；建议只保留 `has_password` |
| F21 | `/api/runtime`、`/api/backup/info` 泄露绝对路径/pid | ❌ **未修** | 实测 `python: "/data/workspace/Register/.venv/bin/python"`、`pid: 317023`、`data_dir: /root/.hermes/...` |
| F22 | `tasks/logs` 无上限 + 全表算 total | ❌ **未修** | `api/tasks.py:655` `total = len(s.exec(q).all())`；实测 `page_size=999999` → 356,026 B |
| F23 | `_login_failures` 无清理 | ❌ **未修** | `api/auth.py:227`，键已改成真实 IP（F7 修好），但字典仍只增不减 |
| F24 | SMS 上游错误原文透传 | ❌ **未修** | `api/sms.py:89,100` |
| F25 | GET 触发第三方面板请求 | ❌ **未修** | `api/integrations.py:66-79`（`refresh=true` 时） |

**净结果**：25 条里 **5 条已修（F1/F6/F7/F12 源码/F10 半条）**，**1 条修复中（F5）**，**19 条仍在**。真正的高危集中在「凭据怎么出去」（F2/F3/F4/F8/F9/F18）而不是「门锁得紧不紧」—— 门锁这一轮修得不错。

### 1.3 已核实「做对了」的控制点（防止后续重构改坏）

- `docs_url/redoc_url/openapi_url` 全关（`main.py:199-201`），实测 `/api/openapi.json` → 404；
- 未设密码时非回环返回 503 而非 fail-open（`main.py:227-236`），代码路径与上一轮沙箱实测一致；
- 密码 PBKDF2-HMAC-SHA256 / 600k / 随机盐 / `compare_digest`，旧无盐哈希可验证但会透明升级（`api/auth.py:115-171`）；
- TOTP 失败 5 次即作废临时令牌（`api/auth.py:429-439`），±1 窗口；
- `GET /api/config` 的 10 个口令键全部置空 + `<key>_set`（实测 `yescaptcha_key:""` + `yescaptcha_key_set:false`，无明文）；
- 路径穿越：9 个变体（`/api/auth/../accounts`、`%2e%2e`、`//`、`/./`、`/api/accounts%2f`）全部 404，`/API/accounts` 落到 SPA（200 HTML，非 API）—— 鉴权前缀判定**不可穿越**；
- SPA 兜底对 `/api/**` 显式 404（`main.py:573-574`），实测 `/api/definitely-not-a-route` → 404 JSON；
- 备份导入的 zip-slip / 成员数 / 大小 / sha256 / 魔数五重校验仍在（`services/data_bundle.py`）；
- 鉴权用 `Authorization` 头而非 Cookie，且 `allow_credentials=False` → **经典 CSRF 不成立**（这是下面「GET 导出」只定低危的原因）。

---

## 2. 鉴权面

### 2.1 中间件的实际判定链（`main.py:210-251`）

```
path.startswith("/api/auth/") or not path.startswith("/api/")  → 直接放行   (main.py:213)
auth_password_hash 为空                                        → 仅回环放行，其余 503 (main.py:216-236)
否则要求 Authorization: Bearer ***                            → 401 / 通过  (main.py:237-251)
```

未登录可达面（`c93b71f`）：

| 路径 | 方法 | 未登录可达 | 说明 |
|------|------|-----------|------|
| `/api/auth/status` | GET | ✅ | `{"has_password":…,"has_totp":…}` |
| `/api/auth/setup` | POST | ✅ | 未设密码时**仅回环**；`APP_ALLOW_REMOTE_SETUP=1` 时放开 |
| `/api/auth/disable` | POST | ✅ | 未设密码时无校验；**已设密码时只需 token**（N5） |
| `/api/auth/login` | POST | ✅ | 预期公开，有失败退避（键=真实 IP） |
| `/api/auth/verify-totp` | POST | ✅ | 预期公开，5 次上限 |
| `/api/auth/logout` | POST | ✅ | **无任何校验**，且会轮换全局密钥（N1） |
| `/api/auth/2fa/*`、`/change-password` | GET/POST | ❌ | `Depends(require_auth)` |
| 其余 `/api/**` | 任意 | ❌（设了密码）/ ⚠️（未设密码 + 回环） | 中间件统一拦截 |
| `/m/{share_token}` | GET | ✅ | 设计如此（`api/shared_mail.py:149`） |
| `/`、`/assets/**` | GET | ✅ | SPA 静态资源 |

**当前真实状态**：真实库 `configs` 表 143 行里**没有** `auth_password_hash`、也没有 `auth_jwt_secret` —— 面板处于「未设密码」模式。此时任何**本机进程/本机用户**（含同机容器、同机其他服务、任何能打到 127.0.0.1:8000 的东西）无需任何凭据即可读全量账号与备份包。实测：

```
$ curl -s 'http://127.0.0.1:8000/api/accounts?page_size=1'   # 无 Authorization 头
200  {"total":1057,"items":[{"token":"eyJhbG…"(1988 字符), …}]}
$ curl -s -o bundle.zip -w '%{http_code} %{size_download}' http://127.0.0.1:8000/api/backup/export
200 7298029
```

这不是「配错了」，是 `main.py:225` 的显式设计（本机调试不受影响）。问题在于**默认部署是 `0.0.0.0:8000`**（`docker-compose.yml:44`，`${APP_PORT_BIND:-8000}:8000`），而「本机」的判据是 TCP 源地址 —— 见 F11：同机反代下所有外部访问的源地址都是 `127.0.0.1`。

### N1（中）`/api/auth/logout` 未认证 + 轮换全局密钥

- **位置**：`api/auth.py:446-453`（`def logout()` 无 `Depends(require_auth)`）+ `main.py:213`（`/api/auth/*` 整段放行）+ `api/auth.py:47-57`（`_rotate_jwt_secret` 写 `configs`）
- **证据**：静态。`logout()` 直接 `revoked = _rotate_jwt_secret()`，函数签名里没有任何凭据参数；中间件对该前缀直接 `call_next`。同理 `disable_auth`（`:370-380`）在未设密码时也是零校验。
- **影响**（按只读约束未发 POST 验证，结论来自代码路径）：
  1. **任意网络来源**（不限于本机）都能把 `auth_jwt_secret` 换掉 → 已签发的全部 token 立即失效 → 管理员被踢回登录页；反复调用即持续拒绝服务；
  2. 每次调用都写一次 `configs` 表（`ConfigStore.set` → `s.commit()`），是廉价的写放大点；
  3. 修复前 logout 是空实现（F6），修复时把「吊销」实现成了「全局换密钥」——**吊销的粒度错了**：一个用户的退出不该注销所有人，更不该由未认证者触发。
- **建议**：`logout` 加 `dependencies=[Depends(require_auth)]`，且只吊销**调用者自己**的会话（payload 里加 `jti` + 服务端黑名单/版本号，见 F6 的建议）；`disable` 加旧密码 + TOTP；把「全局换密钥」保留给 `change-password` 这类确实需要全量吊销的动作，并单独记审计日志。

### N5（中）`/api/auth/disable` 仍不要旧密码

- **位置**：`api/auth.py:370-380`
- **证据**：`if cfg.get("auth_password_hash",""): verify_token(...)` —— 只验 token，不验密码；而 `setup`（`:344-351`）与 `change_password`（`:456-462`）都要求旧密码。同一类动作三套强度。
- **影响**：面板 token 一旦泄漏（浏览器/日志/截图/XSS），攻击者可直接 `disable` 把面板退回无密码态，再叠加 F11（同机反代）即完全接管；即便不接管，也能清掉 `auth_totp_secret` 让二次因子消失。
- **建议**：`disable` 要求 `current_password` + TOTP；或干脆删除该端点，只保留「改密码」。

### F11（中）「仅回环」兜底在同机反代下失效

- **位置**：`main.py:225`（`if _is_loopback_client(request)`）
- **证据**：判据只有 `request.client.host`（`core/net_utils.py:30-46`），不看任何头。同机 nginx/caddy `proxy_pass http://127.0.0.1:8000` 时，**所有**外部请求的源地址都是 `127.0.0.1` → 503 分支永不触发。
- **影响**：在「未设密码（当前状态）+ 同机反代」这个组合下，`main.py:216-236` 那道兜底等于 fail-open，任何外部访问者都能读全量账号并下载含 `credential_key` 的备份包（F2）。
- **建议**：不要把「源地址」当作「本机用户」。未设密码时**只允许携带启动期一次性 token** 的请求，或直接拒绝所有业务请求（前端只显示「请在本机初始化」页）；`docs/DEPLOYMENT.md` 明确写「无密码模式不支持反代」。

---

## 3. 敏感数据暴露

### 3.1 一览

| 数据 | 响应里 | 落盘 | 位置 |
|------|--------|------|------|
| 账号密码 | 详情 ✅（明文）、CSV 导出 ❌（明文） | `v1:` 密文 ✅ | `api/accounts.py:444`；`core/db/models_account.py:76` |
| AT / SSO（`token` 列） | **列表 + 详情都明文** ❌ | **明文** ❌ | `api/accounts.py:260-302`；`core/db/models_account.py:86` |
| grok `sso`（extra_json） | **列表明文** ❌ | `v1:` 密文 ✅ | `api/accounts.py:165`（瘦身名单漏 `sso`） |
| `extra_json` 内 AT/RT/ST/id_token/cookies/cpa_record | 列表已换成布尔/删除 ✅ | 密文 ✅ | `api/accounts.py:165-167,230-260` |
| 面板口令（10 个 schema 键） | 置空 + `_set` ✅ | 密文 ✅ | `api/config.py:97-104`；`core/config_store.py:20-34` |
| 代理 URL（含 user:pass） | **明文** ❌ | 明文 ❌ | `c93b71f:api/proxies.py:157` |
| 代理凭据（任务日志） | **明文** ❌ | 明文 ❌ | `services/task_runners.py:259,568` |
| 凭据加密密钥 | **备份 ZIP 内** ❌ | 文件 0600（未核） | `api/backup.py:37-54` |
| `share_token`（免登录邮件页） | **列表逐条下发** ❌ | 明文 | `api/icloud.py:339-346` |
| `auth_jwt_secret` / `auth_password_hash` | 不经 API 下发 ✅ | **明文** ❌（N4） | `core/config_store.py:20-27` |

### F2（严重）备份导出 = 全量凭据 + 解密密钥

- **位置**：`api/backup.py:37-54`；打包逻辑 `services/data_bundle.py`
- **证据**（实测，未解包到磁盘，只在内存里读成员表）：

```
$ curl -s -o bundle.zip -w '%{http_code} %{size_download}\n' http://127.0.0.1:8000/api/backup/export
200 7298029
account_manager.db          25825280
platforms/chatgpt.db             4096
platforms/grok.db                4096
platforms/icloud.db            659456
platforms/outlook.db            16384
secrets/credential_key             44      ← AES 密钥本身
manifest.json                    1512
```

  ZIP 未加密；包内 `account_manager.db` 的 `accounts.token` 仍是 **1057/1057 明文**（与 F8 同因）。
- **影响**：一个 GET = 全库 + 配置 + **解密这些密文的密钥**。任一读权限（本机任意进程、F11 下的外部访问者、任何 SSRF 打到 localhost）都升级为「全部账号 + 全部配置」的离线完全控制。这也是为什么 N2/N3 的 SSRF 不只是「探内网」——`http://127.0.0.1:8000/api/backup/export` 就是一个可被 SSRF 直接取走的 URL（响应体经 `{"raw": …}` 回传）。
- **建议**：导出默认**不含** `secrets/credential_key`（导入端允许单独提供）；或导出时用用户输入的 passphrase 做 AES-GCM 封装；导出动作要求二次认证（重输密码/TOTP）并写审计日志。

### F3（高）账号列表明文下发 `token` 列（AT / SSO）

- **位置**：`api/accounts.py:260-302`（`_slim_account_row`）、`:165-167`（瘦身名单）、`:230-260`（`_slim_extra_for_list`）
- **证据**：

```
$ curl -s 'http://127.0.0.1:8000/api/accounts?page_size=2'
items[0] keys = [cashier_url, created_at, email, extra_json, has_password, id,
                 legacy_id, platform, region, status, token, updated_at, user_id]   ← token 在
items[0].token  长度 1988，三段 JWT（eyJhbG…）
$ curl -s 'http://127.0.0.1:8000/api/accounts?platform=grok&page_size=1'
extra_json keys = [sso, register_mode, grok2api_ingested, …, register_proxy, has_access_token, …]
extra_json.sso  长度 152（3 段 JWT）                                              ← sso 在
```

- **根因**：`_slim_account_row` 只 `pop` 了 `password` / `password_cipher`（`:296-297`），**从未 pop `token`**；`_LIST_HEAVY_EXTRA_KEYS`（`:165`）里也没有 `"sso"`。文件内注释（`:158-167`）明确写「列表只拿 AT/RT 判有没有……换成布尔」，实现与承诺相反。
- **量化**：`page_size=100000` → `200` / **3,802,434 B** / 2.35 s，1057 条每条带 1988 字符 AT。一次 GET 拖走全池。
- **建议**：`payload.pop("token", None)` + 补 `has_token`；`_LIST_HEAVY_EXTRA_KEYS += ("sso",)`；补契约测试（断言列表里不存在 ≥100 字符的三段 JWT 形状字符串）。

### F8（高）`token` 列明文落盘

- **位置**：`core/db/models_account.py:86`（`token: str = ""`，无 `sa_column`、无加解密）；对照同文件 `:76` 的 `password_cipher`
- **证据**（只读 SQL，真实库）：

```sql
select count(*) from accounts where token<>'' and token not like 'v1:%';   -- 1057
select count(*) from accounts where token<>'';                             -- 1057
select count(*) from accounts where password<>'' and password not like 'v1:%'; -- 0
select platform, count(*) from accounts group by platform;                 -- chatgpt 667 / grok 390
```

- **影响**：`core/credential_fields.py` 定义 `token` = 平台主凭证镜像（chatgpt→AT，grok→SSO）。即使攻击者拿不到 `credential_key`，只要能读到 DB 文件（备份包、卷快照、误拷），就能直接拿走全部 AT/SSO。**「凭据加密落盘」在最关键的一列上没生效**。
- **建议**：把 `token` 纳入 `encrypt_text/decrypt_text`（属性化，列名不变），或取消该冗余列、统一从 `extra_json` 派生；对存量 1057 行做一次性加密回填。

### F4（高）任务日志写入代理账号密码并回吐

- **位置**：写 `services/task_runners.py:259`、`:568`；读 `api/tasks.py:637-657`、`:790-820`
- **证据**：

```
$ curl -s 'http://127.0.0.1:8000/api/tasks?include_logs=true' > t.json   # 13,724,404 B / 837 任务
logs scanned: 88,856   credential-shaped hits: 761
task 0: [16:51:36] 使用代理: socks5h://<user>:<pass>@9.142.194.72:6740
task 2: [15:46:05] 使用代理: socks5h://<user>:<pass>@9.142.43.158:5328
```

  账号行的 `extra_json.register_proxy` 也是同一条带凭据的串（`api/accounts.py` 未瘦身该键）。
- **影响**：代理服务商账号密码同时存在于 `task_logs` / `task_runs` / `/api/tasks*` 响应 / uvicorn stdout / 备份包。泄漏 = 出口 IP 池被白嫖或封禁。
- **建议**：写日志前统一过 `core/proxy_utils.redact_proxy_url`（本轮工作区已经在用它打码 `/api/proxies`，同一条纪律应下沉到写日志处）；历史数据一次性脱敏；列表接口再加一层输出侧脱敏。

### F5（高）代理接口回带账号密码的 URL

- **位置**：`c93b71f:api/proxies.py:157`（`"url": url`）、`:38`（`list_proxies` 原样返回仓储记录）
- **证据**：

```
$ curl -s http://127.0.0.1:8000/api/proxies
[{"url":"socks5://<user>:<pass>@9.142.43.158:5328","id":2,"region":"","success_count":338,…}]
$ curl -s http://127.0.0.1:8000/api/proxies/balance
{"items":[{"id":2,"url":"socks5://<user>:<pass>@9.142.43.158:5328", … "bound_total":290}]}
```

- **状态**：审计期间工作区出现了未提交补丁 `_public_proxy()`（`api/proxies.py` 的 `git diff`，23 行新增），已把两处 `url` 换成 `redact_proxy_url`。**补丁本身没问题，但要注意三点**：(a) 未提交；(b) 未重启前线上仍泄漏；(c) 打码后前端按 `url` 做 key 匹配的地方要同步改用 `id`（补丁注释里已提到，需实测）。
- **建议**：合并 + 重启 + 补一条契约测试（`GET /api/proxies` 响应中不得出现 `://user:pass@`）。

### F9（中）明文密码 CSV 导出

- **位置**：`api/accounts.py:428-451`
- **证据**：

```
$ curl -s -o exp.csv -w '%{http_code} %{size_download} %{content_type}\n' \
    'http://127.0.0.1:8000/api/accounts/export?platform=chatgpt'
200 56588 text/csv; charset=utf-8
header: platform,email,password,user_id,region,status,cashier_url,created_at
rows: 667，password 列 16 字符明文
```

- **问题**：GET、无二次认证、无条数上限、无审计、响应无 `no-store`（F13）；密码列逐行解密（`:444` `acc.password`）。
- **建议**：改 POST + 重输密码/TOTP；默认不含密码列（`include_password=true` 才带并记审计）；加 `Cache-Control: no-store`；大导出改流式分页。

### F18（中）免登录邮件页的 `share_token` 批量下发

- **位置**：`api/icloud.py:339-346`、`api/shared_mail.py:149-161`
- **证据**：

```
$ curl -s http://127.0.0.1:8000/api/icloud/aliases     # 712,390 B
items[0] = {id, address, share_token:"<22 字符>", pool_status, registered_platforms, …}
$ curl -s -o /dev/null -w '%{http_code} %{size_download}\n' http://127.0.0.1:8000/m/<share_token>
200 1883          # 未登录，页面标题里直接是别名邮箱
$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/m/bogus
404
```

- **影响**：`share_token` 是「链接即权限」的 bearer 凭证，被当普通字段塞进列表 → 任何一次列表读取（浏览器缓存、前端日志、截图、XSS）都会扩散成百上千个可读收件箱的永久链接；这些收件箱正是 OTP 的落点。token 本身 22 字符（≈132 bit）不可枚举，`/m/bogus` 也正确 404 —— 问题在**分发**不在强度。
- **建议**：列表只回 `has_share_link`；分享走 `POST /api/icloud/aliases/{id}/share` 返回一次性 token；`/m/` 侧加最小限速 + `Referrer-Policy: no-referrer`。

### F13 / F19（中/低）缓存与安全响应头

- **证据**（实测）：

```
$ curl -s -D - -o /dev/null http://127.0.0.1:8000/api/accounts?page_size=1
HTTP/1.1 200 OK
content-length: 3445
content-type: application/json
vary: Origin
      ← 无 cache-control / no-store，无 x-content-type-options，无 CSP，无 x-frame-options
```

- **影响**：凭据类响应可被浏览器「后退」、共享代理、磁盘缓存留存；面板 token 存在前端（`frontend/src/lib/utils.ts` 统一加 `Authorization`），缺 CSP 会放大任何 XSS。
- **建议**：对 `/api/accounts*`、`/api/backup/export`、`/api/proxies*`、`/api/icloud/*` 统一加 `Cache-Control: no-store, private`；全局加 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、`Content-Security-Policy: default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'`（`/m/**` 保留现有 iframe `srcdoc` + `script-src 'none'`，`api/shared_mail.py:72-95` 已做对）。

### N4（中）`auth_jwt_secret` 明文落盘，并随备份外带

- **位置**：`core/config_store.py:20-27`（`_seal` 只对 `SECRET_CONFIG_KEYS` 加密）+ `core/config_schema.py:135-137`（该集合由 `CONFIG_SCHEMA` 里 `secret=True` 的键派生，`auth_*` 不在其中）+ `api/auth.py:40-43,363-364`（首次签发 token 时 `cfg.set("auth_jwt_secret", …)`）
- **证据**：静态。`_seal("auth_jwt_secret", value)` 因键不在集合里直接原样返回；`ConfigStore.get_all()` 会把它解密后交给所有调用方。真实库当前 0 行（还没登录过），首次登录后即写入。备份包里的 `account_manager.db` 就是明文 `configs` 表。
- **影响**：拿到备份包（F2）或 DB 文件的人，**即使不拿 `credential_key`**，也能用这个 secret 离线签出任意 `{"sub":"admin"}` 的 HS256 token（`verify_token` 只校验签名与 exp，无 jti/版本），从而在服务上获得完整管理员权限。这把「离线读文件」升级成「在线接管」。
- **建议**：把 `auth_jwt_secret` 加入 `SECRET_CONFIG_KEYS`（`_seal/_unseal` 自动生效），或存到 `data/secrets/`（0600）而不是 `configs`；`auth_password_hash` 也一并加密（PBKDF2 虽不可逆，但离线爆破面没必要白送）。

---

## 4. 输入校验与资源上限

### F14（中）分页与批量无上限

| 端点 | 参数 | 上限 | 实测 |
|------|------|------|------|
| `GET /api/accounts` | `page_size` | **无**（`api/accounts.py:315` 只 `max(1,…)`） | `100000` → 200 / 3,802,434 B / 2.35 s；`100000000` → 200 / 同 3.8 MB / 0.54 s |
| `GET /api/tasks/logs` | `page_size` | **无**（`api/tasks.py:637`） | `999999` → 200 / 356,026 B |
| `POST /api/accounts/import` | `lines` | **无**（`api/accounts.py:40-46`） | — |
| `POST /api/proxies/bulk` | `proxies` | **无** | — |
| `POST /api/outlook/batch-import` | `data: str` | **无** | — |
| `POST /api/icloud/aliases/batch-delete` | `ids` | **无** | — |
| `POST /api/mail-imports/batch-delete` | `items` | **无** | — |
| 四个 batch-delete | `ids` | 1000 ✅ | `api/accounts.py:693`、`api/proxies.py`、`api/tasks.py:667`、`api/actions.py:138,167` |

- **影响**：单进程 uvicorn（compose 无 `--workers`），一个 `page_size=10^7` 就能把进程内存顶满；`/api/accounts` 还会顺带把 1057 个 AT 序列化一遍（F3 放大）。
- **建议**：统一 `PageParams`（`page_size: int = Field(20, ge=1, le=200)`）+ 批量 `max_length=1000`；`import` 的 `lines` 限 5000 条、单行 4 KB。

### F16（中）参数越界把 pydantic 内部细节当 `detail`

- **位置**：`api/mail_imports.py:19-38`
- **证据**：

```
$ curl -s 'http://127.0.0.1:8000/api/mail-imports/snapshot?type=microsoft&preview_limit=100000'
{"detail":"1 validation error for MailImportSnapshotRequest\npreview_limit\n
 Input should be less than or equal to 500 [type=less_than_equal, input_value=100000,
 input_type=int]\n For further information visit https://errors.pydantic.dev/2.13/v/less_than_equal"}
```

  对比 `GET /api/accounts?page=abc` → 422 标准 `int_parsing` 明细（✅ 正常）。
- **建议**：`preview_limit` 直接声明成 `Query(100, ge=1, le=500)`；或捕获 `ValidationError` 只回一句人话。

### 路径穿越（结论：**未发现**）

```
/api/auth/../accounts?page_size=1        404 {"detail":"Not Found"}
/api/auth/..%2faccounts?page_size=1      404
/api/auth/%2e%2e/accounts?page_size=1    404
/api//accounts?page_size=1               404
/api/./accounts?page_size=1              404
/api/accounts%2f?page_size=1             404
/API/accounts?page_size=1                200 text/html 1063B   ← SPA 外壳，不是 API
/%61pi/accounts?page_size=1              200 3445B            ← 解码后就是 /api/accounts，中间件同样看得到
```

结论：`request.url.path` 是**解码后**的路径，路由与中间件用的是同一个值，没有「中间件看到 A、路由匹配到 B」的缝隙。`/assets` 走 Starlette `StaticFiles`（内部 `realpath` + 前缀校验），SPA 兜底不拼接用户路径。

---

## 5. SSRF 与外发（本轮新增的重点）

### N2（中高）`/api/contribution/*`：`server_url` 由请求体控制 + 密钥外带

- **位置**：`api/contribution.py:33-53`（`ContributionProxyRequest.server_url` / `_resolve_server_url`）、`:67-92`（`_request_json`）、调用点 `:126`、`:205`、`:239`、`:289`
- **证据**：

```python
def _resolve_server_url(server_url: str | None) -> str:
    raw = str(server_url or config_store.get("contribution_server_url","") or "").strip()
    if not raw.startswith(("http://","https://")):
        raw = f"http://{raw}"
    return raw.rstrip("/") + "/"                      # 完全采信请求体

headers["X-Public-Key"] = key
headers["Authorization"] = f"Bearer {key}"            # 把已存的 contribution_key 发过去
response = requests.request(method=..., url=url, timeout=15)
data = response.json()  except → {"raw": response.text}   # 非 JSON 响应体原样回传
```

  4 个端点（`/quota-stats`、`/key-info`、`/redeem`、`/generate-key`）都接受 `server_url`；`/generate-key` 甚至不需要 key。
- **影响**：
  1. **SSRF**：服务端替调用方发 GET/POST 到任意 `http(s)` 地址 —— 内网面板、`169.254.169.254` 元数据、`127.0.0.1:8889`（solver，明确无鉴权）、以及 `127.0.0.1:8000/api/backup/export`（F2，7 MB 凭据包）。响应体（含非 JSON 的 `raw`）会回到调用方，是**带读回**的 SSRF，不只是盲打。
  2. **密钥外带**：`Authorization: Bearer <contribution_key>` 会被送到调用方指定的 host。`GET /api/config` 是**看不到**这个 key 的（被置空），但这条路径能把它原样送到攻击者服务器 —— 即「能调接口」升级为「能读已存口令」。
  3. 无重定向限制（`requests` 默认跟随），`http://attacker/302→http://169.254.169.254/` 同样成立。
- **建议**：`server_url` 只允许来自配置（不接受请求体覆盖），或强制白名单 + 解析后校验 IP 不在私网/回环/链路本地段；禁用重定向（`allow_redirects=False`）；key 只发往配置里的固定 host；把「发往第三方的请求」记审计日志。

### N3（中）`/api/icloud/remote-accounts*`：`base_url` 由请求体控制

- **位置**：`api/icloud.py:104-119`（`RemoteAccountRequest` / `DiscoverRemoteRequest` 的 `base_url`）、`:257-270`（discover）、`:273-285`（add）
- **证据**：`icloud_service.discover_remote_accounts(base_url=body.base_url, admin_password=body.admin_password, proxy=body.proxy)` —— 地址、密码、代理全部来自请求体；`add_remote_account` 同形。
- **影响**：与 N2 同类（SSRF + 把 `admin_password` 发往任意 host + 响应回传）。区别是这里外发的是**调用方自己填的**密码，危害略低；但 SSRF 面相同，且 `proxy` 参数可以让请求走任意代理（可用来打只有特定出口能到的内网）。
- **建议**：`base_url` 同样做私网/回环校验；或限定「必须是用户在设置页登记过的远端服务」。

> 注：`api/integrations.py:66-79` 的面板地址来自 `configs`（`GET /api/integrations/panels` 也是读配置），**不是**逐请求可控，所以只算 F25（GET 有外部副作用），不算 SSRF。

---

## 6. 错误语义

### F15（中）`str(exc)` 直接进响应体

- **位置**：`api/accounts.py:713`、`api/tasks.py:697`、`api/backup.py:46,110,122`、`api/actions.py:426,469`、`api/integrations.py:232`、`api/sms.py:89,100`、`api/mail_imports.py:36-71`、`api/outlook.py:57-59`
- **证据**：静态；典型 `str(exc)` 会带 `sqlite3.OperationalError: no such column: …`、`sqlalchemy.exc.…`、`requests.exceptions.ConnectionError: … socks5h://user:pass@…`、`FileNotFoundError: /data/…` —— SQL 结构、代理凭据、绝对路径三类内部信息都可能上屏。
- **附带契约问题**：`api/actions.py:469` 与 `api/integrations.py:232` 用 **HTTP 200** 表达失败（靠 `ok:false` 区分），与其它端点的 4xx/5xx 不一致。
- **建议**：对外只回稳定 `code` + 一句人话 + `request_id`，原始异常只进 `logger.exception`；业务性失败用 409/422，不要 200。

### 未捕获异常

`main.py` 未注册全局异常处理器，FastAPI 默认 500 只回 `{"detail":"Internal Server Error"}`（无 traceback）—— 这点是好的。但批量端点里「一个坏行拖垮整批」仍是 500 的主要来源（上一轮 `api/actions.py` 的 `int(uuid)` 就是实例，已由 `9630980` 修）。建议批量动作逐条 `try/except` 回 `{id, ok:false, reason}`。

---

## 7. 破坏性接口

| 端点 | 破坏性 | 幂等 | 确认/dry-run | 结论 |
|------|--------|------|--------------|------|
| `POST /api/accounts/batch-delete` | 删账号 | ✅ | ❌ | 有 1000 上限 + `not_found`，可接受 |
| `POST /api/proxies/batch-delete` | 删代理 | ✅ | ❌ | 同上 |
| `POST /api/tasks/logs/batch-delete` | 删历史 | ✅ | ❌ | 同上 |
| `POST /api/proxies/balance` | 批量改绑定 | ⚠️ | ✅ `dry_run=True` 默认 | 设计良好；`dry_run=false` 无二次确认、无回滚 |
| `POST /api/integrations/panels/{k}/sync` | **覆盖本地 AT/RT/id_token** | ⚠️ | ❌ | 建议先出「将改动 N 个账号」预览 |
| `POST /api/integrations/panels/{k}/push?delete_old=true` | **删远端旧记录** | ⚠️ | ❌ | 破坏性开关在 query 里，建议改 body + 默认 false |
| `POST /api/icloud/aliases/batch-delete`（`remote: bool = True`） | **删 Apple 侧别名（不可逆）** | ⚠️ | ❌ 默认删远端 | 最危险：默认值站在不可逆一侧，且 `ids` 无上限 |
| `POST /api/backup/import` | 整库替换 + 换密钥 | ❌ | ⚠️ 自动备份 + 运行中任务 409 | 机制最完整（原子替换 + 失败回滚） |
| `POST /api/accounts/import` | 覆盖同邮箱字段 | ✅ | ❌ | 无上限（F14） |

**通用建议**：一次影响 >N 个对象的写接口统一加两层 ——(a) `dry_run`/`preview` 必填其一；(b) 服务端签发 `confirm_token`（把请求体哈希进去，5 分钟有效），客户端必须回传。

---

## 8. 修复优先级

**P0（当天）**

1. **N0**：确认线上跑的是哪个 commit。当前 `:8000` 实例 18:25 启动、`c93b71f` 18:36 提交 —— 五条鉴权修复全部没生效。部署流程需要「重启后校验」这一步（`/api/runtime` 的 `stale` 本该干这个，但见 N6）。
2. **F2**：备份导出不再默认携带 `secrets/credential_key`；导出要求二次认证。
3. **F3 + F8**：列表 `pop("token")` + `sso` 进瘦身名单；`token` 列加密落盘（存量 1057 行回填）。
4. **F4 + F5**：日志与代理接口统一掩码；合并工作区里那份 `_public_proxy` 补丁并重启。

**P1（本周）**

5. **N1**：`logout` 加认证，吊销粒度改成「只吊销自己」。
6. **N5**：`disable` 要求旧密码 + TOTP。
7. **N2 + N3**：`server_url` / `base_url` 不再由请求体决定，或强制私网/回环黑名单 + 禁重定向。
8. **N4**：`auth_jwt_secret` / `auth_password_hash` 纳入 `SECRET_CONFIG_KEYS`（或单独加密）。
9. **F9**：密码导出改 POST + 二次认证 + 默认不含密码列。
10. **F14**：统一分页（≤200）与批量（≤1000）上限。
11. **F15 + F16**：错误体只留稳定 code + 人话。

**P2（两周内）**

12. **F11**：无密码模式的判据从「源地址」改成「初始化 token」，文档声明不支持反代。
13. **F13 + F19**：`no-store` 与安全响应头。
14. **F17**：破坏性接口加 `dry_run`/`confirm_token`，`remote` 默认改 false。
15. **F18**：`share_token` 不再随列表下发。
16. **F20–F25**：信息面收敛（`/api/runtime` 去掉绝对路径、`_login_failures` 清理、SMS 上游错误不直出、`tasks/logs` 的 total 改 `count()`）。

---

## 附录 A：探测矩阵（全部 GET/HEAD，无一次写请求）

| # | 请求 | 状态 | 关键观察 |
|---|------|------|----------|
| 1 | `GET /` | 200 / 1063 B | 只有 `cache-control/content-type/vary`（F19） |
| 2 | `GET /api/auth/status` | 200 | `{"has_password":false,"has_totp":false}` |
| 3 | `GET /api/accounts?page_size=1` | 200 / 3445 B | **无 Authorization 也 200**；行内含 1988 字符 `token`（F3） |
| 4 | `GET /api/accounts?page_size=2` | 200 / 7528 B | `items[0]` 13 个键，含 `token` |
| 5 | `GET /api/accounts?platform=grok&page_size=1` | 200 | `extra_json.sso` 152 字符（F3） |
| 6 | `GET /api/accounts?page_size=100000` | 200 / 3,802,434 B / 2.35 s | 无上限（F14） |
| 7 | `GET /api/accounts/export?platform=chatgpt` | 200 / 56,588 B CSV | 667 行明文密码（F9） |
| 8 | `GET /api/config` | 200 / 2,338 B | 口令键全空 + `_set`（✅） |
| 9 | `GET /api/proxies` | 200 / 926 B | 5 条 `socks5://user:pass@…`（F5） |
| 10 | `GET /api/tasks?include_logs=true` | 200 / 13,724,404 B | 837 任务 / 88,856 行日志 / 761 处凭据形状（F4） |
| 11 | `GET /api/tasks/logs?page_size=999999` | 200 / 356,026 B | 无上限（F22） |
| 12 | `GET /api/backup/export` | 200 / 7,298,029 B | 8 成员，含 `secrets/credential_key` 44 B（F2） |
| 13 | `GET /api/backup/info` | 200 / 1,083 B | 回绝对 `data_dir` 与各库路径/行数（F21） |
| 14 | `GET /api/runtime` | 200 / 176 B | `pid` / `python` 绝对路径 / `code_version:"未知"`（F21、N6） |
| 15 | `GET /api/icloud/aliases` | 200 / 712,390 B | 每条带 22 字符 `share_token`（F18） |
| 16 | `GET /m/<share_token>` | 200 / 1,883 B | 未登录可读邮件，标题即别名邮箱 |
| 17 | `GET /m/bogus` | 404 | ✅ |
| 18 | `GET /api/mail-imports/snapshot?type=microsoft&preview_limit=100000` | 400 | 回显 pydantic 模型名（F16） |
| 19 | `GET /api/mail-imports/snapshot?type=nope` | 400 | `不支持的邮箱导入类型` ✅ |
| 20 | `GET /api/accounts?page=abc` | 422 | 标准 `int_parsing` ✅ |
| 21 | `GET /api/openapi.json`、`/docs`、`/redoc` | 404 / 200 HTML / 200 HTML | docs 已关；`/docs` 落到 SPA（低） |
| 22 | `GET /api/definitely-not-a-route` | 404 | 未被 SPA 兜成 200 ✅ |
| 23 | `GET /api/accounts` + `Origin: https://evil.example` | 200 + `access-control-allow-origin: *` | **进程仍跑旧代码**（N0） |
| 24 | `OPTIONS /api/accounts`（预检，evil origin） | 200 | `allow-methods: … PUT, QUERY`、`max-age: 600`（N0） |
| 25 | 9 条路径/前缀穿越变体 | 404 ×7 / 200 HTML ×2 | 无绕过（§4） |
| 26 | `HEAD /api/accounts` | 405 | API 路由不支持 HEAD（低） |

## 附录 B：未覆盖 / 需要下一轮

- **写路径一律未实测**（只读约束）：N1/N5 的「未认证即可 logout/disable」、F17 的破坏性默认值、F1 的 403 分支，全部来自代码路径而非请求验证。下一轮若允许在**一次性副本**上发 POST，建议优先复验这四条。
- **多 worker / 多副本**：`_login_failures`、`_pending_2fa` 都是进程内 dict（`api/auth.py:210,227`）。当前 compose 是单进程，若改成 `--workers 4`，退避与 TOTP 尝试计数**不共享**，可被「换 worker」绕过。
- **`api/actions.py` 的平台动作面**：只审了错误语义与批量上限；`_execute_platform_action` 调用的 `platforms/**` 真实业务（登录、改密、上传）是否把密码写日志，需结合 `platforms/chatgpt` 另审。
- **Solver 端口 8889**：`docker-compose.yml` 明确「solver 接口没有鉴权」，仅靠 `127.0.0.1:8889` 映射兜底；裸 `docker run -p 8889:8889` 会直接暴露。与本报告无关但同机，建议并入下一轮。
- **前端**：`share_token`、`token` 在浏览器侧的生命周期（是否进 localStorage / 是否被日志组件打印）属于 frontend 审计范围，本报告只覆盖服务端出口。
