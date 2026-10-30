# 2FA 密钥只下发一次

TOTP 密钥在 enroll 响应里只出现一次。账号行此时还不存在，所以必须先写文件日志，否则「注册成功但绑 2FA 崩了」的号永久锁死。

## 重构约束

- journal 必须加密（阶段 1 T1.5）
- 不许改成「直接写 DB」—— 绑定期间 DB 写会 `database is locked`
- 恢复成功后必须删除 journal

## 证据（8 块，自动抽取自源码注释）

> 原始索引见 `_index.json`。改对应代码前逐块读完 —— 这些分支不是冗余，是故障换来的。

### `api/accounts.py:223-225`

~~~
    # 2FA 密钥**本体**不能进列表（安全审查 2026-10-27 加固项 1，实测确认）：
    # 列表页要的是「有没有绑 2FA」这个布尔，而密钥泄漏比密码更严重 ——
    # 拿到 TOTP 密钥等于永久通过第二因子，密码还能改，密钥不能。
~~~

### `main.py:232-239`

~~~
        # **没设密码 ≠ 随便看**（安全审查 2026-10-27 F1）。
        #
        # 原实现直接放行（fail-open），而默认部署绑 0.0.0.0 —— 于是「装好还没
        # 设密码」这个窗口里，任何能访问端口的人都读得到全量账号（password /
        # SSO / AT / RT / id_token / totp_secret），以及 `/api/backup/export`
        # 那个含**凭据加密密钥本身**的 ZIP。
        #
        # 现在只放行**本机回环**（本机调试不受影响），其余来源 503 并说明原因。
~~~

### `platforms/chatgpt/chatgpt2api_upload.py:1-36`

~~~
chatgpt2api 上传功能。

同名项目有两个不同作者的实现（yukkcat 版与 basketikun 版），上传器同一份代码
两边都能用；本项目的**读取对比**按 yukkcat 版对接（列表不带 token、凭证走
export），上传协议两边一致：

* `POST <host>/api/accounts`，body `{"accounts": [...]}`，
  认证头 `Authorization: Bearer <key>`。
* 每个对象只有 `access_token` 必需；**普通网页号不要带 `type: "codex"`**
  —— 带了会被对端当成 codex 源，走另一条解析路径。
* 重复 access_token 对端按 `skipped` 处理，所以重传是幂等的。
* 响应回 `{added, skipped, refreshed, errors}`。

## 凭据字段（对端 ≥ 2.2.0，2026-10-26）

对端可写字段已扩到 `access_token` / `refresh_token` / `id_token` / `email` /
`password` / `totp_secret` / `proxy` / `type` / `source_type`。对端文档明说
「务必带 `password` + `totp_secret`。RT 会失效，没有密码就只能人工救」——
本模块因此额外下发 `refresh_token` / `id_token` / `password` / `totp_secret`。

**只发非空字段，这条是硬要求，不是格式洁癖。** 对端 `addAccountPayloads` 对已
存在的账号做 `merged = copyMap(current); for k, v := range incoming { merged[k] = v }`
—— incoming 的每个键都会覆盖，**包括空串**（而且 `skipped` 的账号照样走这段
合并）。所以传 `password: ""` 会抹掉对端已有的密码、传 `totp_secret: ""` 会让
开了 2FA 的号永久锁死。**永远不要为了「字段形状稳定」把空值填进去。**

`source_type` 与是否带 RT 无关：对端 `prepareAccountPayload` 只在
`type == "codex"` 或 `export_type == "codex"` 时置 `SourceCodex`（本模块传
`source_type: "web"`，不受影响）。旧注释里「多传 RT 会让对端当成另一种源」是
2.2.0 之前的猜测，已证伪 —— 对端支持且推荐带上。

字段支持依赖对端 ≥ 2.2.0：老版本会忽略不认识的字段，不会因为多传而坏掉。

与 CPA / Sub2API 的区别：这里传的是**普通网页号**（`access_token` 必需），
不生成 codex 凭证（`type` 里绝不出现 `codex`）。

~~~

### `platforms/chatgpt/protocol/two_factor.py:83-94`

~~~
查已绑 → enroll → activate → 复核。

    整条链只用 ``flow.session`` 和 Bearer token，不碰 authorize 状态机，所以
    快慢两条路都能直接调。

    ``on_secret`` 在**拿到密钥的那一刻**立刻回调（早于 activate）。密钥只在
    enroll 响应里下发一次、服务端取不回，而「拿到」与「调用方落库」之间隔着
    activate + 复核两次网络往返 —— 中间崩了、被中断了、或调用方忘了落库，
    这个号的 2FA 就永久锁死（实测踩过：手工调 API 时没走落库，密钥直接丢了）。
    回调抛异常不中断绑定（密钥还在 ``result.secret`` 里，调用方仍可补救），
    但要留一条 error 日志。
    
~~~

### `platforms/chatgpt/protocol/two_factor.py:180-187`

~~~
慢路径：新起一条 flow 重走登录正式链，拿到 access_token 再 enroll。

    独立实例意味着独立指纹；但设备标识（oai-did）要复用库里那份
    （`device_id`）—— 同一个账号重登却换「新设备」是要避免的风控特征
    （用户问题 2026-10-07「指纹能复用吗」）。批量补绑时指纹各异、设备标识
    随账号，与真实用户换浏览器版本但设备不变的行为一致。
    整条链跑下来要一次 PoW，低信任新号还会被要求收一封邮件验证码。
    
~~~

### `platforms/chatgpt/protocol/two_factor.py:365-369`

~~~
    # 消费 callback 让 NextAuth 写 session cookie。**不能拿它的返回值当致命判据**：
    # 那是个「是否读到 cookie」的探测，而 curl_cffi 分域隔离时朴素读法读不到
    # （`_extract_session_cookie` 专门有三路兜底）。注册链与手机链都忽略返回值、
    # 直接走 `get_auth_session()` —— 这里原先把它当致命错误，于是「cookie 没读出来」
    # 被报成「拿不到会话」，整轮绑定白跑（实测日志：OTP 验证成功后卡在这一步）。
~~~

### `services/chatgpt_two_factor.py:93-103`

~~~
按账号字段绑 2FA，不落库（落库交给 ``build_extra_patch``）。

    只收纯数据不收 ORM 对象：这一趟要跑几十秒网络请求，调用方得以在这期间把
    数据库连接还回池子里。

    ``persist_secret`` 是**密钥一到手就落库**的回调（写前日志）：密钥只在 enroll
    响应里下发一次，从「拿到」到「调用方按 ``build_extra_patch`` 落库」之间隔着
    activate + 复核两次网络往返 —— 中间进程被 kill、任务被中断、或调用方漏了
    落库，这个号的 2FA 就永久锁死（实测踩过）。给了回调的话，密钥在 enroll 返回
    的那一刻就先写进库，后续步骤再出问题也不会丢。
    
~~~

### `services/chatgpt_two_factor.py:375-389`

~~~
造一个「密钥一到手就落库」的回调（写前日志）。

    给 ``bind_account_two_factor(persist_secret=...)`` 用。先写文件日志（原子
    替换 + fsync），再写库；自己开一个短连接写完就关 —— 绑 2FA 那趟要跑几十秒
    网络请求，不能占着连接；而这一步必须**立刻**做，不能等外层那趟跑完（实测
    踩过：密钥拿到后外层还没落库就出问题，密钥永久丢失、号的 2FA 锁死）。

    定位账号给 ``account_id`` 或 ``email`` 其一（任务链有 id，平台动作链只有
    email）。只在库里还没有密钥时写（不覆盖已有的）。

    **文件日志先于 DB 落地**：动作链绑定期间占着一条 session，等它 flush 过写
    事务后另开连接写库会 `database is locked`（实测复现），而这条路径的意义就是
    「进程被杀也要保住密钥」。文件写成功即视为安全 —— DB 写失败只记 warning，
    启动时 ``recover_pending_totp_secrets`` 会补写；文件也写不成功才往上抛。
    
~~~
