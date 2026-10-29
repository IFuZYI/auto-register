# Cloudflare 按浏览器家族封禁

`warmup` 拿不到 `oai-did` 就必须失败退出 —— 继续走必然 409 `invalid_state`。重试要**换浏览器家族**，不只是换 IP：CF 封的是 TLS/HTTP2 指纹本身。

## 重构约束

- 4 次重试不许降到 1 次（注释里有 5/5 全 409 vs 3/17 的对照）
- 判据必须是 cookie jar 里有 `oai-did`，不是 `status_code == 200`
- 「换家族」不许简化成「只换 IP」
- `sec-ch-ua` 全套必须与 UA 家族一致，否则 chrome 裸头只有 1/5 通过

## 证据（18 块，自动抽取自源码注释）

> 原始索引见 `_index.json`。改对应代码前逐块读完 —— 这些分支不是冗余，是故障换来的。

### `platforms/chatgpt/login_refresh.py:1-21`

~~~
走登录流程重新拿 access_token。

`token_refresh` 的两条路（session token / OAuth refresh token）都拿不到可用 AT
时才走这里：邮箱 + 密码重新跑一遍登录链，必要时过 2FA / 邮箱验证码。

    ① 账号密码 + 2FA（`totp_secret` 在库）—— 最常见也最快的一条。
    ② 服务端要邮箱验证码时，按用户要求**先判断邮箱是否入池**：没入池就直接
       报错说清（而不是去撞一个必然超时的收件箱），入池了才用邮箱池取码。
    ③ 两条都不通时，用登录链的响应判断账号是不是**已被封禁** —— OpenAI 对
       停用账号会明确回 "deleted or deactivated"，这是本模块最有价值的产出：
       它能把「凭证过期」和「号没了」分开，后者不该再进重试队列。

设计要点：登录链**必须复用** `AuthFlow` 而不是自己拼 HTTP —— 那条链里有 PoW
sentinel、device_id、oai-did cookie 等一串服务端校验，手搓必然 409 invalid_state。
本模块只做三件事：组装 `AuthFlow`、把「邮箱从哪来」接成协议层要的 `MailProvider`、
把结果翻译成 `LoginRefreshResult`。

与 `rt_backfill` 的关系：那个模块是「拿 RT」优先（它的登录链开了
`OAUTH_REFRESH_ONLY`，只要 RT 不要 AT），本模块是「拿 AT」优先，用同一套
`AuthFlow` 但不开那个开关 —— 目标不同，不能互相替代。

~~~

### `platforms/chatgpt/protocol/auth_flow.py:60-67`

~~~
        #: **实际签发 refresh_token 的那个 OAuth client_id**。
        #:
        #: 两条路线用的 client 不同（Codex / platform 密码登录），刷新时必须
        #: 拿「签发它的那个」去换 —— 用错会 `401 invalid_client`，而错误信息
        #: 看起来像「令牌失效」（实测 2026-10-28）。
        #:
        #: 来源：服务端 `client_auth_session` dump 的 `openai_client_id`
        #: （`mixins/trace.py:301` 同步到 `flow._oauth_client_id`）。
~~~

### `platforms/chatgpt/protocol/auth_flow.py:144-147`

~~~
        # 设备标识复用种子（用户问题 2026-10-07「指纹能复用吗，能不能减少
        # 后续登录封号的风险」）。登录链把库里的 device_id 经 `seed_device_id`
        # 预置到这里；warmup 成功后写进 cookie，让整条链（ext-oai-did 参数 /
        # auth_oauth_init 读 cookie）都用同一个设备标识。空 = 注册链，行为不变。
~~~

### `platforms/chatgpt/protocol/auth_flow.py:279-290`

~~~
按 `chatgpt_rt_route` 设置拿 RT；返回是否拿到了。

        **所有换 RT 的调用点都要走这里**，不要直接 `oauth_codex_rt_exchange`。
        此前 `_acquire_rt` 被赋值却从未调用（四个 Codex 调用点全都绕过它），
        于是设置选了「密码登录」，注册链仍走 Codex —— 实测日志里密码登录的
        路线一次都没被尝试过（用户 2026-10-27 报）。结构性测试
        `test_rt_route_reaches_protocol_flow.py` 盯住这一点。

        顺序：注入的路线（引擎按设置决定）→ 失败再回落 Codex。
        注入路线抛异常也吞掉回退：换不到 RT 是「少个字段」，炸掉整条注册链
        是「账号白注册」。
        
~~~

### `platforms/chatgpt/protocol/auth_flow.py:309-321`

~~~
预置要复用的设备标识（oai-did）——登录链专用。

        用户问题（2026-10-07）：「chatgpt 是会保留指纹的吧，这个指纹能复用吗，
        能不能减少后续登录封号的风险。」实测：服务端**保留**客户端预置的
        oai-did（预置 A → GET chatgpt.com 返回还是 A，不覆盖）。同一个账号
        每次登录都换一台「新设备」是要避免的风控特征，所以登录链把注册时
        落库的 device_id 带进来。

        只记值、**不种 cookie**：warmup 的判据是「cookie 到底种上没有」
        （CF 403 只给 __cf_bm），提前种会让判据永远为真、CF 403 被误报成功、
        重试轮换浏览器家族的逻辑失效。写进 cookie 在 warmup 成功之后
        （见 warmup 里的 `_device_id_seed` 处理）。
        
~~~

### `platforms/chatgpt/protocol/auth_flow.py:329-386`

~~~
GET chatgpt.com 种全套 cookie（含 oai-did），成功返回 True。

        为什么这步不能失败（2026-08-10 实测 26 轮，跨 40+ 出口 IP）：
        `POST /api/auth/signin/openai` 依据 chatgpt.com 的 cookie 决定返回什么——
        有 oai-did 就返 auth.openai.com/authorize URL，没有就返 NextAuth 页，
        后者到 authorize/continue 必然 409 invalid_state。
        实测：无 oai-did 的 5 轮 **5/5 全 409**；有 oai-did 的 17 轮只有 3 次 409。

        旧实现两个问题，实测各占一半失败：
        1. 单次无重试 + timeout=15。实测种 cookie 失败率 19%，形态有三种：
           TLS curl(35) 断连、15s 超时、CF 403。成功轮实际耗时 3.4~10.9s，
           15s 卡边缘，40s 才有富余。
        2. **返回值和实际结果对不上**：只 catch 异常，不看 status_code——
           403 照样 return True（实测 3 轮 True 但没 cookie），
           而超时前 cookie 其实已经种上了却 return False（实测 1 轮）。
           所以判据改成直接查 cookie jar，这是唯一可信的信号。

        3. **没发 client hints，自称 Chrome 却不带 sec-ch-ua —— CF 一眼假。**
           这是 403 的真因。真 Chrome 每个导航请求必带 sec-ch-ua/-mobile/-platform，
           而旧 warmup 是手搓 headers、一个都没带（_common_headers 带了，只有这里漏）。
           2026-08-10 实测，同一 impersonate 各打 5 次（每次新 IP）：

               impersonate   裸头(旧)   补 CH 全套
               chrome146      1/5        5/5
               chrome136      1/5        5/5
               chrome142      4/5        4/5   ← 唯一失败是 SSL 断连，不是 403

           补齐后 403 **全部消失**。此前"chrome 族被 CF 拦"的结论是误判：
           safari/firefox 当时 4/4 不是因为它们更干净，而是**它们本来就不该发
           client hints**，裸头对它们恰好是正确的头。所以修法是把头补齐，
           不是换成 safari —— 换指纹只是绕开症状，且会让 self._fingerprint 与
           self._ua 不一致（后续 _common_headers 会拿旧家族的 CH 配新 UA，更假）。

        4. **【2026-08-21 更新】CF 改成按家族封，chrome 全族 403。**
           上面第 3 条"补齐 client hints 后 chrome 就好了"已经过期。线上注册连挂，
           同一台机器（无代理）当场实测：

               impersonate                  结果
               chrome136 / 142 / 146        403，只给 __cf_bm
               mac_safari / ios_safari      200，oai-did 正常种下
               firefox133                   200，oai-did 正常种下

           头是对的、IP 是好的（5 小时前同一 IP 刚跑通过一轮补 RT），封的是
           chrome 的 TLS/HTTP2 指纹本身。所以重试**必须换家族**——原来那句
           "只换出口 IP，不换指纹"在这种封锁下等于拿同一张脸连撞 4 次。

        重试同时换出口 IP 和浏览器家族：代理池按会话分配出口，新 session ≈ 新 IP，
        绕开连不上的坏 IP；换家族绕开整族封锁。cookie 跟着 session 一起清掉是对的：
        失败轮本来就没种到有用的东西。家族顺序是随机的，不写死"safari 更安全"——
        今天挂的是 chrome，明天可能轮到别的。

        注：URL 保持首页 `/`。实测对比过 `/auth/login`（16 轮 vs 10 轮），
        失败率 18.75% vs 20%，无差异，不值得换。

        【最终验证 2026-08-10】本处 + auth_oauth_init + _follow_redirects 三处
        统一走 _navigation_headers 后，用真实 CF 域名跑完整 run_register
        **3/3 全成功**（各约 100s，password + access_token 齐全），409 = 0。
        
~~~

### `platforms/chatgpt/protocol/auth_flow.py:419-425`

~~~
                # 设备标识复用：登录链预置了 device_id 时，把服务端种的
                # （或重建会话后重种的）值改写回预置值 —— 后续 ext-oai-did
                # 参数与 auth_oauth_init 读 cookie 都拿同一个标识，整条链
                # 在服务端眼里始终是「同一台设备」。实测服务端保留预置值，
                # 改写安全。注册链（无预置）原样保留服务端的值。
                # `getattr` 兜底：测试常以 `AuthFlow.__new__` 绕过 __init__，
                # 属性不存在时按「无预置」处理。
~~~

### `platforms/chatgpt/protocol/auth_flow.py:534-552`

~~~
密码被拒后**重开一条 authorize 链**，用于在活着的 state 上发 OTP。

        ## 为什么必须重开（实测 2026-10-28 真实注册）

            /password/verify → 401 invalid_username_or_password
            /email-otp/resend → 400 Invalid authorization step. invalid_auth_step
            /email-otp/send   → 409 Your sign-in session is no longer valid.

        401 之后服务端把这个 authorize 会话**作废了**。同一个 state 上再发码
        必然被拒 —— 不是参数问题，state 本身死了。所以降级到 OTP 的唯一可行
        路径是重走 csrf → auth_url → oauth_init → sentinel → signup，
        在新 state 上直接走「已有账号 → 发码」。

        `signup()` 本身就是新链的第一步：它会返回 `is_new=False` 并把
        `_existing_email_verification_mode` / `_existing_page_type` 重新填好，
        后续 OTP 分支据此工作。

        返回 True = 新链就绪（且确认是已有账号）。
        
~~~

### `platforms/chatgpt/protocol/auth_flow.py:574-575`

~~~
        # warmup 失败 = 没拿到 oai-did = 后面 authorize/continue 必 409（实测 5/5）。
        # 必须在 create_mailbox 之前拦掉：邮箱是花钱的，不能为一个注定 409 的轮次浪费。
~~~

### `platforms/chatgpt/protocol/auth_flow.py:626-630`

~~~
        # ⚠️ passwordless_signup **也是新账号**，只是服务端选择了"不设密码直接发码"的注册流程。
        #    signup() 用一个 bool 表达三种服务端状态，把它和"已有账号"压成了同一个 False，
        #    结果新号全部走进下面的 else 分支 —— register_password 从没被调用过，
        #    注册出来的号全是无密码号（只能靠临时邮箱收码登录，域名一失效就永久丢失）。
        #    实测 2026-08-06: 这类号照样能走 POST user/register 设密码并成功。
~~~

### `platforms/chatgpt/protocol/auth_flow.py:635-640`

~~~
                # ⚠️ POST user/register 成功后服务端**切换流程**到 email_otp_send 页
                #    （实测响应 continue_url=/api/accounts/email-otp/send, page.type=email_otp_send），
                #    signup 阶段自动发的那封 OTP 立即失效，拿它 verify 会 409 invalid_state。
                #    所以必须主动重新发码，且以本次发码时间为准，不能再用 -8 偏移。
                #    时间戳在发码**之前**取：邮件是服务端在这次请求里发出的，
                #    先发再取时间会让 otp_sent_at 晚于邮件时间戳，被 issued_after 过滤掉。
~~~

### `platforms/chatgpt/protocol/auth_flow.py:669-672`

~~~
                # 偶发 401 错码，补发一次 OTP 并重试。
                # 重试时必须把刚试过的码排除：补发的新邮件还没到时，收件端会把
                # 同一封信再读一遍、拿同一个错码再验一次 —— 白耗一次机会，还可能
                # 直接撞上服务端的 max_check_attempts 锁死（实测踩过）。
~~~

### `platforms/chatgpt/protocol/auth_flow.py:676-679`

~~~
                    # 与首轮同因：`POST user/register` 之后服务端流程在
                    # `email_otp_send` 页，此时 `passwordless/send-otp` 必然
                    # 409 invalid_state（实测日志里就有这条，白跑一趟还刷一条
                    # 吓人的 warning）。首轮走的就是 `send_otp()`，重试保持一致。
~~~

### `platforms/chatgpt/protocol/auth_flow.py:760-764`

~~~
                    # **密码被拒（401）后 state 已作废**，必须先重开一条 authorize
                    # 链再发码。实测 2026-10-28：401 之后在同一个 state 上
                    # `email-otp/resend` → 400 invalid_auth_step、
                    # `email-otp/send` → 409 invalid_state，两条都死。
                    # 用户日志里第 50-53 个账号每个都重试 2 轮全失败，就是这个原因。
~~~

### `platforms/chatgpt/protocol/auth_flow.py:1104-1108`

~~~
                # 封禁是终局结论，不能被当作「探测失败」回退：TOTP 提交等步骤
                # 撞上「deleted or deactivated」时若吞掉异常，回退链会以无关的
                # 409 invalid_state 收场 —— 封禁措辞在最终错误里消失，账号被
                # 误标「失效」而不是「禁用」（用户报告 2026-10-07：一批账号
                # 服务端已明确回停用措辞，状态却没变）。
~~~

### `platforms/chatgpt/protocol/fingerprint.py:754-762`

~~~
给出**其它家族**的 impersonate，每族一个，顺序随机。

    风控是按家族整体封的，不是按版本：2026-08-21 实测 chatgpt.com warmup，
    chrome136/142/146 全 403（只给 __cf_bm），mac_safari / ios_safari /
    firefox 全 200 并正常种下 oai-did。同族换版本对这种封锁没有意义。

    顺序不写死：今天挂的是 chrome，明天可能换成别的家族。随机轮换让 4 次重试
    把其它家族都覆盖到，而不是赌某一族一定能用。
    
~~~

### `platforms/chatgpt/protocol/http_client.py:40-76`

~~~
给 session 的 get/post 套一层 TLS 瞬断重试，其余属性原样透传。

    ── 为什么要有这东西 ──
    代理链路会偶发 `curl: (35) TLS connect error ... OPENSSL_internal`，
    连 HTTP 请求都没发出去就炸。2026-08-10 实测（148 轮扫描）：

        发生率            5.4%（8/148）
        与指纹的关系      无 —— chrome146/142/136、safari18_0/15_3、firefox133 都中过
        与域名的关系      无 —— chatgpt.com 3/25、auth.openai.com 1/25，
                          且见过同一轮两个域一起炸（那一路出口链路整个坏了）

    换句话说这是**链路级瞬断**，不是风控、不是指纹问题，摘掉任何一个指纹都没用。

    ── 为什么必须原 session 重试，不能重建 ──
    warmup 那处的重试是重建 session（换出口 IP），因为那时还没 cookie。
    但链路中后段（auth_oauth_init / sentinel / authorize_continue …）session 里
    已经装着 warmup 种的 oai-did 和 csrf，**一重建就全丢，直接变 409 invalid_state**
    —— 那正是上一轮刚修好的病。所以这里只重试，绝不碰 session。

    实测原 session 重试的效果（8 次 TLS35 事件全部捕获后立即重试）：

        恢复 8/8，全部**第 1 次重试就成功**，恢复后 oai-did 仍在 8/8

    ── 为什么包在 session 层，而不是逐个调用点加 try ──
    这个错能打在链上**任意一步**。主人 2026-08-10 那批 10 个号的两次失败就分别
    炸在 `[3/10] auth_oauth_init` 和 `[4/10] sentinel`（后者还被 sentinel_quickjs
    的 catch-all 吞成 "QuickJS 失败/主 token 缺失"，真因全被掩盖）。auth_flow 里
    有 35 处 session.get/post，且 sentinel.py 是直接拿 session 对象自己发请求的，
    逐点打补丁既治不完也漏得到 —— 包在出口这一层才是一次覆盖全部。

    ── 透传安全性（已实测）──
    全项目在 session 上访问的非 get/post 属性只有 cookies(20处) / trust_env(3) /
    proxies(3) / mount(2) / headers(1)，实测包装后全部行为一致：
    cookies.get_dict() / cookies.get() / cookies.jar / 迭代 / __setattr__ 透传均 OK。
    （注：迭代 session.cookies 产出的是 str 而非 Cookie 对象、拿不到 .name，
    这是 curl_cffi **原生行为**，包装前后一致，与本类无关。）
    
~~~

### `platforms/grok/oauth_browser.py:1-18`

~~~
浏览器内完成 OAuth Device Flow 授权（拿 access/refresh token）。

为什么需要
----------
`device/verify` 与 `device/approve` 都被 CF 保护：

- 纯 HTTP（curl_cffi）：device/verify → 403、device/approve → 403（实测）
- 浏览器 ctx.request：device/code 200 ✅，但 verify/approve 仍需真实页面上下文

只有**真实浏览器导航到授权页并点击授权按钮**才能完成：
  导航 https://accounts.x.ai/oauth2/device?user_code=XXX
  → 点 "Continue" / "Allow"
  → 落地 /oauth2/device/done
  → 纯 HTTP 轮询 /oauth2/token 拿 token

出处：grok-reg/device_mint.py:5-8
  「授权: 有头 Chrome 注入 SSO cookie → 打开授权页自动点"继续/允许" → 轮询 token」

~~~
