# OTP 防串号

一轮流程会发多封码值相同的邮件。必须同时用「发码时间窗 + 邮件 id 基线 + 排除已试码」三重过滤，缺一个就会拿旧码去验。

## 重构约束

- `otp_sent_at` 必须在发码**之前**取，先发再取会被 `issued_after` 过滤掉
- `prime()` 在第二段流程（绑 2FA / 补 RT）前必须重打
- 401 重试必须传 `exclude_codes={旧码}`，否则撞 `max_check_attempts` 锁死
- 已有账号只能 `resend_otp`，`send_otp` 会让旧邮件立即失效

## 证据（9 块，自动抽取自源码注释）

> 原始索引见 `_index.json`。改对应代码前逐块读完 —— 这些分支不是冗余，是故障换来的。

### `core/mailboxes/base.py:95-103`

~~~
通用验证码提取逻辑：若有捕获组则返回 group(1)，否则返回 group(0)。

        两层防误判（都是实测踩过的，见下方注释）：
        1. **先剥掉邮箱地址**：邮件头/转发标记里常带邮箱，本地部分可能正好含
           6 位数字（实测 `X-ICLOUD-HME: ... f=sample123456@icloud.com` →
           兜底正则抓到 `123456`，而真码是正文里的 `406553`）。
        2. **兜底正则要求字母数字边界**：`(?<![a-zA-Z0-9])` / `(?![a-zA-Z0-9])`，
           防止从 `u24681357` 这类紧贴字母数字的串里截出 6 位。
        
~~~

### `core/mailboxes/base.py:122-126`

~~~
                # 兜底：两条边界都要。
                #   `(?<!#)` 排除 hex 色值 —— 邮件 HTML 里 `color: #202123`
                #     会先于真码出现，实测抓到它而不是验证码。
                #   `(?<![a-zA-Z0-9])` / `(?![a-zA-Z0-9])` 排除紧贴字母数字的串
                #     （`u24681357`、`sample123456` 这类）。
~~~

### `platforms/chatgpt/protocol/mail_provider.py:97-106`

~~~
阻塞等待 OTP，拿到返回 6 位码，超时抛 ``TimeoutError``。

        ``issued_after`` 是防串号时间窗：只接受这个时间点之后到达的邮件，
        避免读到上一轮遗留的旧验证码。实现必须尊重这个参数。

        ``exclude_codes`` 是**已试过、判定为错**的码：补发重试时把上一轮的码
        排除掉。不排除的话，补发的新邮件还没到时会把同一封信再读一遍、拿同一个
        错码再验一次 —— 白耗一次机会，还可能撞上服务端的
        ``max_check_attempts`` 直接锁死（实测踩过）。实现必须尊重这个参数。
        
~~~

### `platforms/chatgpt/protocol/mixins/signup.py:115-130`

~~~
跟随 authorize 链，落 authorize 会话状态并取回 oai-did。

        这一步**建立的就是后面 authorize/continue 要用的那个 state**，头不像真
        浏览器就拿不到有效状态，下一步必 409 invalid_state。

        旧实现只发 Accept/Referer/UA，缺 client hints、**整组 Sec-Fetch-* 也没有**
        （真浏览器跳转必带 document/navigate/cross-site）。2026-08-10 实测 A/B
        对照各 6 轮（400 invalid_username 视为会话正常，只是 .test 域名被拒）：

            A 现状裸头        会话正常 2/6，**409 = 3**
            B 补齐 CH+SecFetch 会话正常 5/6，**409 = 0**

        和 warmup 那处是同一个病（详见 warmup docstring），当时只修了 warmup，
        漏了这里，所以主人实跑仍 409。头统一从 _navigation_headers 派生，
        保证 client hints 与 self._fingerprint / self._ua 同族。
        
~~~

### `platforms/chatgpt/protocol/mixins/signup.py:337-342`

~~~
                # 注：username_password_create 这个 flow 服务端**不下发 so 块**
                #    （实测 2026-08-06，见 sentinel_quickjs.py 里的说明），
                #    所以 so_token 为空是正常的，已在 sentinel_quickjs 按服务端要求判定，
                #    不再走到这个 except。这里只兜网络/子进程一类的真异常。
                #    走到这里说明用的是上一步的 token，flow 对不上是风控特征，
                #    但比当场崩掉（POST 根本发不出去）强，故降级继续。
~~~

### `platforms/chatgpt/protocol/mixins/signup.py:366-372`

~~~
        # ⚠️ 走到这里 = OpenAI 侧账号连同这个密码**已经建好了**，但注册流程后面还有
        #    发码 → 验证 OTP → create_account 三步，任何一步挂掉都到不了 save_registered。
        #    密码是这个方法现生成的、只活在内存里，进程一退就永久没了 ——
        #    号还在 OpenAI 那边好好的，却谁也登不进去（实测 2026-08-07 被 OTP 超时坑过一次）。
        #    所以在这里立刻回调落盘。
        #    位置刻意选在 POST 200 **之后**而不是生成密码时：POST 失败的密码
        #    OpenAI 侧根本没生效，写进库里反而误导人以为能用。
~~~

### `platforms/chatgpt/protocol/mixins/signup.py:477-479`

~~~
                # 封禁是终局结论：不能吞掉当普通失败 —— 吞掉后上层只会看到
                # 无关的后续错误（实测 409 invalid_state），账号被误标「失效」
                # 而不是「禁用」（用户报告 2026-10-07 的根因之一）。
~~~

### `platforms/chatgpt/protocol/mixins/signup.py:517-530`

~~~
找出登录这个号该用的密码。返回 (密码, 是否为真密码)。

        真密码三个来源，按优先级：
            ① self.result.password —— 本轮 register_password 刚设的
            ② LOGIN_PASSWORD 环境变量 —— 主人手动指定
            ③ account_callback —— 数据库里存的（**重跑老号全靠这条**）
        三条都空才退到 _default_password_from_email 猜一个，此时第二个返回值 False。

        ⚠️ 猜出来的密码**绝不能**写回 self.result.password。那个字段有两个下游：
             · to_dict() → registrar 落库，会把假密码存成这个号的密码；
             · registrar 异常兜底那行「该号已生成密码，请自行留存」。
           实测：一个 passwordless 老号（从没设过密码）被打成「邮箱去掉 @」，
           照着存等于存了个死密码。所以赋值一律由调用方按 is_real 决定。
        
~~~

### `platforms/chatgpt/protocol/mixins/signup.py:585-591`

~~~
            # **不要 raise**：401 不是致命错误，而是「这个号不能用密码登」
            # 的信号 —— 已有账号的正确路径是 OTP。
            #
            # 实测（2026-10-28 用户日志）：`page_type=login_password` 时密码
            # 是猜的（新号没设过），必然 401；原来这里 raise 直接让整条注册
            # 流水线失败，**第 50-53 个账号全挂、每个还重试 2 轮**，而 OTP
            # 分支（`WEBUI_ALLOW_LOGIN=1` 正是为它准备的）根本没机会跑。
~~~
