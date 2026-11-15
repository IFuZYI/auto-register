# Task 3 — 凭据域 API：导入/导出/备份/回填
状态: 完成（第一版全量落盘；每条结论均有复现）

范围：`api/backup.py`、`api/contribution.py`、`services/account_export.py`、`services/data_bundle.py`。
验证方式：临时 `DATA_DIR`（`/tmp/t3*`）+ 真实 `SQLModel` 引擎 + `TestClient`，
直接调用被审函数并断言真实输出；未读写生产库、未读 `.env`/`data/secrets/`、未连真实上游
（contribution 用本机 `HTTPServer` 假上游）。凭据一律以 `sk-***` / `***@x.com` 占位。

## 结论

导出侧**基本可用**：15 种格式都能渲染，JSON 往返（export→import→再比 extra）字节级一致
（实测 `extra` 前后完全相同），Agent2API 契约脚本 `scripts/check_kiro_agent2api_export.py`
退出码 0。但**导出→导入这条闭环在文本格式上有静默写坏数据的分支**，且**备份导入的
来源校验基本不存在**：任何能构造 ZIP 的人都可整体替换账号库与凭据密钥。

按严重度：Critical ×2、Important ×4、Minor ×2、hypothesis ×3。

最要紧的两条：
1. **T3-1（Critical）** 文本导入的默认 `source_format="email_pw"` 把 2 列
   `email----2FA` 的**第二列当密码写进 password 列** —— 2 列对应 `email_pw` 与
   `email_2fa` 两个格式，解析器本身拒绝猜，API 层却给了个静默的默认值。
2. **T3-2（Critical）** `POST /api/backup/import` 只校验「自洽性」（manifest 与成员
   sha256 对得上），**不校验来源/签名**；密钥成员内容也不校验格式，导入一个
   `credential_key` 内容为 `garbage` 的包会得到 `ok=True, key_restored=True`，
   随后**任何一次凭据加密都抛 `CredentialKeyError`**（实测已复现）。

## 发现

### T3-1 · Critical · 2 列文本导入把 2FA 密钥写进 password 列（静默数据损坏）
`api/accounts.py:66`（`source_format: str = "email_pw"` 默认值）+
`services/account_export.py:403-408`（解析器对歧义列数抛错）。

问题：`email_pw` 与 `email_2fa` **都是 2 列**（实测
`formats_by_column_count(2) == ['email_pw', 'email_2fa']`）。解析器在
`source_format=""` 时正确地拒绝猜，但 API 层默认给了 `email_pw`，
于是 `email----TOTPSECRET` 被解析成 `{email, password=TOTPSECRET}`。

复现（实测输出）：
```
$ .venv/bin/python /tmp/t3_ambig.py
email_2fa export: t***@x.com----TOTPSECRET
import (no source_format): 200 {'created': 0, 'updated': 1, 'skipped': 0}
  password now: TOTPSECRET | totp: TOTPSECRET
```
即：**用户原密码被覆盖成 2FA 密钥**，接口回 `updated:1`，无任何警告；
同时 totp 也被写进去（因为 extra 合并），下次登录必然失败。
前端「导入」对话框的来源格式下拉默认就是第一个选项，用户不选即命中
（`static/assets/pages-Bj3kq065.js` 的 `source_format:lt` 取自该下拉）。

影响面：任何使用 `email_2fa` / 任何 2 列格式做「导出→编辑→导入」的账号；
密码列被写坏后不可恢复（原密码只在该行被覆盖前存在）。
建议修法：`ImportRequest.source_format` 默认改为 `""`（让解析器按列数唯一才自动识别、
歧义就 400），或在前端强制用户显式选择且把 `email_pw` 从默认项移开；
若要保留默认，至少对 2 列做「长度像 TOTP/base32 就提示」的守卫是不充分的 —— 建议直接去掉默认。

### T3-2 · Critical · 备份导入无来源校验，且凭据密钥内容不校验 → 可整体接管凭据
`services/data_bundle.py:578-770`（`apply_import_bundle`）、`:417-458`（`_verify_members`）、
`:623-637`（密钥成员只查大小）、`api/backup.py:57-112`。

两个可复现的独立问题：

**(a) 密钥内容不校验**（`:632-637` 只查 `file_size <= MAX_KEY_BYTES`）：
```
C: ok= True key_restored= True
key bytes on disk: 7
encrypt raises: CredentialKeyError 凭据加密密钥必须是 base64 或 hex 编码的 32 字节
```
导入一个 `secrets/credential_key` 内容为 `garbage` 的包：接口回 200 + `ok:true` +
`key_restored:true`，且把 7 字节垃圾**写进了活跃密钥文件**。此后进程内所有
`secret_box.encrypt/decrypt` 直接抛错（实测 `CredentialKeyError`），
所有加密凭据不可读、新账号注册写不进 extra。

**(b) 无来源/签名校验**：只靠「manifest 里的 sha256 == 成员 sha256」这一自洽性校验。
攻击者自建一个 `accounts` 表的最小 SQLite、自己算 sha256 写进 manifest，即可通过：
```
B: BundleError: 导入失败且回滚不完整 ...（原因: sqlite3.OperationalError) no such column: status
```
本例因我造的假库缺 `status` 列而在 `init_db()` 迁移阶段失败 —— 这恰好暴露第三个问题
（见 T3-4），但**校验与替换本身都放行了**：`_verify_members` 通过、旧库被
`shutil.move` 覆盖、`imported=['account_manager.db']` 已记录。

影响面：`/api/backup/import` 只在 `/api/` 认证中间件之后（`main.py:221-262`）：
设了密码则要求 `Bearer`（任何已登录会话即可，无二次确认）；**未设密码时对回环放行**
（`main.py:236-237`）—— 即本机任意进程/本机浏览器页面即可整体替换账号库与密钥。
导入成功即意味着全量账号 + 密钥被替换（配合 (a) 可直接废掉整套凭据）。
建议修法：(1) 密钥成员写入前用 `_decode_key()` 校验能否解出 32 字节，非法即
`BundleError` 且不落盘；(2) manifest 增加 HMAC（用当前密钥签），导入端校验
HMAC 后才接受；或至少要求 `Confirm` 头 + 显式写「将覆盖全部数据」的确认参数。

### T3-3 · Important · 备份导入不校验密钥与库的配套关系 → 「导入成功但凭据全解不开」
`services/data_bundle.py:699-708`（密钥写入）与 `:644-664`（库替换）互不关联。

去掉包里的 `secrets/credential_key` 成员（「只备份了库」这种最常见的半截备份），
在密钥**不同**的新装机器上导入：
```
A: ok= True key_restored= False
creds BROKEN: CredentialKeyError 凭据解密失败（密钥文件是否被替换过？）
```
接口回 200 + `ok:true`，但库里所有 `extra_json` 密文用本地另一把密钥解不开。
这正是 `data_bundle.py:10-12` docstring 自己说的那个症状（「数据都在但全用不了」），
只是它没被实现成「检测 + 拒绝/警告」。
建议修法：`ok` 里增加 `credentials_readable` 探测（导入后 `init_db` 完随机取一行
尝试 `get_extra()`），失败时在返回值里给 `warning` 字段，或直接拒绝并要求带密钥。

### T3-4 · Important · 导入失败后的「回滚」把库换成了未迁移的原始文件，且报「回滚不完整」
`services/data_bundle.py:716-730` + `_rollback_import:506-567`。

T3-2 的复现里已经看到：`BundleError: 导入失败且回滚不完整 —— 请手工用备份恢复`。
回滚逻辑本身把 `import_backups` 里的快照 `shutil.move` 回原位，但**移动**而非拷贝，
且随后 `init_db()` 若再抛错就把 `ok=False`。对用户而言「导入失败」却要求手工恢复，
且备份目录已被 move 走一半（原备份文件不再完整）。
建议修法：回滚用 `shutil.copy2` 而非 `move`（保留备份原样），并把
`init_db()` 的异常与「回滚是否成功」分开报告。

### T3-5 · Important · 导出→导入对「单列格式」与「CSV」不闭环（数据被静默丢弃/写坏）
`services/account_export.py:316-336`（单列格式 `at` / `rt` / `totp`，`skip_empty_rows=True`）、
`:163-188`（CSV 渲染）与 `api/accounts.py:521-560`（导入端只认 `----`/空白）。

实测 4 的矩阵：
```
  at                     200 created=0 updated=0 skipped=1
  rt                     200 created=0 updated=0 skipped=1
  totp                   200 created=0 updated=0 skipped=1
```
单列导出（一行一个 token）**没有 email 列**，导回来必然 `skipped=1` —— 导出格式
存在但不可逆，属正常的格式取舍，但接口对「整批都 skipped」仍回 200，用户看到
`skipped:N` 会以为「导进去了」。

更严重的是 CSV：把 CSV 行当文本导入并不报错，而是把**整行逗号串当成 email**：
```
CSV row: chatgpt,a***@x.com,pw,registered,,,,sk-***AT,,,2026-11-14 21:08:24,,,
CSV->text import: 200 {'created': 1, 'updated': 0, 'skipped': 0}
emails now: ['chatgpt,a***@x.com,pw,registered,...,sk-***at,,,...', 'a***@x.com']
```
即库里多出一条 email 字段装着整行 CSV（**含明文 AT**）的垃圾账号，
且 `normalize_email` 没有拒绝它。影响：污染账号表 / 明文 AT 落进 `email` 列（该列会
出现在列表接口里）。
建议修法：文本导入前检测 `,` 分隔 + 首行等于 CSV 表头 → 明确 400「CSV 请用 JSON/文本格式导入」；
`normalize_email` 拒绝含 `,` 的值。

### T3-6 · Important · CSV 导出缺 `user_id`/`region`/`status` 之外的导入端字段，且 `sso` 在 grok 行全空
`services/account_export.py:163-188`。

CSV 表头实测：
`platform,email,password,status,region,user_id,totp_secret,access_token,refresh_token,cashier_url,created_at,client_id,client_secret,sso`
—— 缺 `id_token`、`session_token`、`phone_number`、`bound_email`，而 JSON 导出**有**
这些（实测 JSON keys 含 `id_token`/`session_token`/`cookies`/`client_id`…）。
grok 行实测：
```
grok,s***@x.com,pw,registered,,,,,,,2026-11-14 21:07:34,,,sso-***JWT
```
`access_token` 为空是对的（grok 的 token 列镜像 SSO，`_access_token` 正确地拒绝导出 SSO），
但 CSV 里 SSO 落到**第 14 列** `sso`，而 grok 账号的「主凭据」列在最前 11 列里没有任何位置 ——
按列号切 CSV 的老脚本拿不到 grok 主凭据。
影响：用 CSV 做迁移会丢 id_token/session_token（chatgpt 的 ST 是可用凭据）。
建议修法：CSV 列与 `_render_json` 对齐（或至少追加 id_token/session_token），
并把 `sso` 的语义在表头注释/文档里写清。

### T3-7 · Minor · 导出在上游密钥损坏时返回 500 而非可读的 4xx
`services/account_export.py:44-51` 抛 `CredentialKeyError`，`api/accounts.py:552`
未捕获 → 500。实测：`7 broken-key export status: 500`。
这是**正确的失败**（`_extra_of` 的 docstring 明确要求炸出来，不能静默变空 —— 这点已验证是对的），
但用户拿到的是 500「Internal Server Error」，与「密钥文件丢了」这个可操作的原因对不上。
建议：`export_accounts_text` 捕获 `CredentialKeyError` → 409/400 + 中文说明。

### T3-8 · Minor · contribution 的 `redeem` 完全没有幂等键，重复点击即重复提现
`api/contribution.py:237-284`。

实测（本机假上游计数）：
```
redeem #1: 200 提现成功！额度：5 兑换码：redeem-abc
redeem #2: 200 提现成功！额度：5 兑换码：redeem-abc
upstream POSTs: [('POST','/public/redeem','{"amount_usd": 5.0}',...), ('POST',...)]
```
两次请求向上游发了**两次** `POST /public/redeem`。若上游不去重，双击就是双倍提现。
另一处：端点把上游返回的 `data` 原样返回（`"data": data`），若上游回显 key 会二次泄露。
建议：前端/服务端加一次性 nonce（`amount_usd + 时间窗` 去重），或至少前端禁用按钮
（当前 `okText` 无 loading 锁）。

### hypothesis（不可复现或未达成复现条件的观察，不给高severity）

- H1 · `api/contribution.py:47-53` `_resolve_server_url` 接受**任意** `server_url`，
  且把 `config_store` 里存的 `contribution_key` 作为 `Authorization: Bearer` +
  `X-Public-Key` 发过去。实测：`upstream saw: [('/public/key-info','Bearer sk-***STOREDKEY','sk-***STOREDKEY')]`
  —— 请求方指定的主机确实收到了**已存储的** key。这是 SSRF + 凭据外带，
  但 `/api/` 需要面板登录（或回环），所以定 hypothesis 而非 Critical：
  需先确认部署下是否有多用户/低权限角色（当前是单口令，故未升级）。
  建议：白名单校验 `server_url` 的 host，或禁止 `server_url` 覆盖已配置值。
- H2 · `api/contribution.py:63-64` `_resolve_key_optional` 允许**未认证**（无 key）调用
  `/quota-stats` 并让它打到任意 `server_url`：实测 `quota-stats no key: 200 | hits: 1`。
  与 H1 同一根因（无 URL 白名单），合并修。
- H3 · 默认上游是**明文 HTTP** 的公网地址（`DEFAULT_CONTRIBUTION_SERVER_URL =
  "http://new.xem8k5.top:7317/"`，`api/contribution.py:14`），key 明文过网。
  未连该地址验证（约束禁止打真实上游），故仅记为观察。

### 验证缺口（Important 级「无验证手段」不成立的地方已列出；以下是已确认的正面证据）

- 已有测试**确实**覆盖了多条关键断言，已核对：
  `tests/test_data_bundle.py::test_key_file_is_included_and_restored`（密钥入包并恢复）、
  `::test_rejects_tampered_content` / `::test_rejects_zip_slip` / `::test_rejects_non_sqlite_member`
  （包的完整性校验）、`tests/test_import_export_roundtrip.py::test_parser_refuses_to_guess_when_ambiguous`
  （解析器拒绝猜）、`::test_column_mapping_comes_from_the_export_registry`（列映射）。
- 但**没有**任何测试覆盖：密钥成员内容非法（T3-2a）、包无来源校验（T3-2b）、
  导入后凭据可解性（T3-3）、CSV 反向导入（T3-5）、contribution 幂等（T3-8）。
  这五处即「无验证手段」的缺口，均已用上面的临时脚本复现。

## 覆盖清单（导出格式 / 备份内容 / 导入校验 / 回填鉴权）

| 面 | 结论 | 证据 |
|---|---|---|
| 导出：8 种 `----` 文本格式 | 能渲染；列映射取自注册表，无漂移 | `/tmp/t3_roundtrip.py` 矩阵，均 200 |
| 导出：单列 `at`/`rt`/`totp` | 渲染正确但**不可逆**（无 email 列） | 矩阵 `skipped=1` |
| 导出：CSV | 表头 14 列，缺 id_token/session_token | 实测表头 |
| 导出：JSON | 35 键，含全部 `export_names()` + user_id/region/cashier_url/status/created_at | 实测 keys 列表 |
| JSON 往返 | `extra` 前后**完全一致**，id 不变 | 实测 `id_same=True`，`extra_lost=[]` |
| 导出：`kiro_auth_token`（Agent2API） | 契约检查通过 | `scripts/check_kiro_agent2api_export.py` exit 0 |
| 导出：`kiro_json`（账号管理器 v2） | 代码路径存在；无独立契约脚本 | 见 `services/account_export.py:198-206` |
| 导出：CPA / CSA / 2API / Sub2API | **不在本模块的导出格式里** —— CPA 走 `services/cliproxyapi_sync.py` 的 auth-file 拉取/比对，Sub2API 走 `services/panel_push.py` 推送 | 全仓 `grep CSA` 无业务命中；brief 里的「CSA/CPA/2API」在本模块无对应实现 |
| 备份：包含哪些字段 | `account_manager.db` + `platforms/*.db` + `data/*.json`（JSON 后端）+ `secrets/credential_key` + `manifest.json` | 实测 `bundle members` |
| 备份：是否含解密密钥 | **是**（`others.append((key_file, "secrets/credential_key"))`，且跟随 `CREDENTIAL_ENCRYPTION_KEY_FILE` 覆盖） | 实测成员列表 + `test_key_file_is_included_and_restored` |
| 导入：包结构/sha256/zip-slip/SQLite 魔数 | 有校验，且有测试 | `_verify_members`、`test_rejects_*` 系列 |
| 导入：来源/签名 | **无**（T3-2b） | 自造包通过校验 |
| 导入：密钥内容合法性 | **无**（T3-2a） | `garbage` 密钥被采纳 |
| 导入：库与密钥配套 | **无**（T3-3） | 无密钥包 → `ok:true` 但凭据解不开 |
| 导入：CSV/单列反向 | **无**（T3-5） | CSV 行变成垃圾 email |
| 回填/contribution：鉴权 | 走 `/api/` 中间件（登录或回环）；`key-info`/`redeem` 需 key，`quota-stats` 可不带 key | `main.py:221-262` + 实测 |
| 回填/contribution：幂等 | **无**（T3-8） | 两次 redeem 两次上游 POST |
| 回填/contribution：上游地址约束 | **无白名单**（H1/H2） | 假上游收到已存储 key |