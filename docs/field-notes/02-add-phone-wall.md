# add-phone 墙

Codex OAuth 拿 RT 时可能被要求先绑手机号。没配接码时这条路径必死，只能回落登录路线 —— 这是「RT 为空但账号可用」的唯一解释。

## 重构约束

- `_sms_callback is None` 时必须干净跳过，不许抛
- 不许把 add-phone 当成失败直接终止整条注册链
- 「刷新重试 ×3」与「去 prompt 重授权」两条无接码兜底要保留

## 证据（8 块，自动抽取自源码注释）

> 原始索引见 `_index.json`。改对应代码前逐块读完 —— 这些分支不是冗余，是故障换来的。

### `platforms/chatgpt/protocol/mixins/codex.py:383-394`

~~~

        纯协议方式获取 RT：
        - 使用独立 Codex OAuth 参数重新授权（可控 PKCE）
        - 捕获 callback code（不消费）
        - 直接调 /oauth/token 交换 access_token + refresh_token

        `speculative=True` 表示这是**抢跑**尝试（在 callback 链路跑完之前先试一把，
        目的是保留 auth.openai 登录态）。抢跑失败**不消耗**本轮的尝试名额 ——
        否则它把 `_codex_rt_attempted` 一置，后面真正有条件成功的那次就会被
        当成「重复尝试」跳过（实测：`refresh_token` 模式跑完库里 RT 是空的，
        而日志里只有一行 DEBUG 说明原因，任务却报成功）。
        
~~~

### `platforms/chatgpt/protocol/phone_flow.py:937-939`

~~~
            # 实测（尼日利亚 19 与泰国 52 都试过）：OpenAI 收下发码请求、返回 200，
            # 接码平台却全程 Waiting for SMS —— 短信根本没进这些虚拟号。
            # 换国家救不了，得换号源，所以别再让人对着国家号反复烧钱。
~~~

### `platforms/chatgpt/registration_engine.py:136-140`

~~~
        # 协议链的每一步都只写在协议层的 logger 上，不镜像出来的话任务日志里
        # 只有「开始/成功」两行 —— 实测踩过：`refresh_token` 模式跑完，日志
        # 全绿、账号也建好了，但库里 `refresh_token` 是空的，而**没有任何一行**
        # 说明 Codex 交换到底跑没跑、失败在哪。手机链路早就镜像了（见
        # `_run_phone`），邮箱链路漏了。
~~~

### `platforms/chatgpt/registration_engine.py:156-166`

~~~
`refresh_token` 模式却没拿到 RT 时，把原因摆到明面上。

        RT 由 Codex OAuth 交换提供，而那条链路会撞上 **add-phone 墙**：OpenAI
        要求先绑手机号才给 refresh_token（实测日志原文：「命中 add-phone，但未配置
        SMS 接码 / OPENAI_PHONE_NUMBER，无法继续推进」）。没配接码时这个模式
        只能拿到 access_token —— 账号可用，但**没有 RT**。

        这必须说出来：此前它按成功收场，库里 RT 是空的、界面一片绿，用户以为
        自己有一批带 RT 的号（实测踩过，三个号全空）。配置了接码时这条路能走通，
        所以不删模式，只把降级讲清楚。
        
~~~

### `platforms/chatgpt/registration_engine.py:288-299`

~~~
按 `chatgpt_rt_route` 选路换 refresh_token。

        用户要求的顺序（2026-10-26）：
        - `codex`（默认，现有行为）→ 失败时**回退**到登录链；
        - `login` → 只走登录链，不跑 Codex（那条路在这个场景白花时间）。

        为什么 Codex 失败要回退：实测 Codex 路线在新号上卡在
        choose-an-account / session.select，而登录路线能在同样的号上拿到 RT
        （新号 3 次 / 存量号 1 次验证）。回退让默认配置也能拿到 RT。

        `access_token_only` 模式两条都不跑 —— 那个模式本来就明确不要 RT。
        
~~~

### `platforms/chatgpt/registration_engine.py:350-352`

~~~
                # **必须传收件渠道**：登录链撞上邮箱验证码时要等码 ——
                # 不传的话它会直接放弃，RT 就彻底拿不到（实测
                # task_1793184821307：AT/session 都有，RT 空）。
~~~

### `platforms/chatgpt/registration_engine.py:526-528`

~~~
        # 把「Token 方案」原样带进协议层：抢跑判定（`_speculative_rt_attempt`）
        # 要按它决定跑不跑 Codex。不带的话协议层读不到设置、只能按默认走
        # （实测 2026-10-27：设置选密码登录，抢跑仍是 Codex）。
~~~

### `services/sms_service.py:1-33`

~~~
手机接码服务（sms-activate 协议系）。实现在 `services/sms/`。

供 ChatGPT 注册链路命中 ``add-phone`` 时自动租号、收码、验证。

目前接入 SmsBower 与 HeroSMS，两家共用 sms-activate 的 ``handler_api.php`` 协议，
差别只在 base_url 和固定价格参数的写法上，所以由同一个 provider 类覆盖。

三段式用法（``AuthFlow._handle_add_phone_via_sms`` 就是这么调的）：

    controller = build_phone_callback(config, log_fn=...)
    phone = controller.get_phone()          # 租号
    code = controller.get_code(timeout=80)  # 等短信
    controller.report_success()             # 业务侧验证通过

⚠️ OpenAI 自 2025 年起对大部分国家改用 WhatsApp 验证，纯 SMS 路径实测只有泰国
（country_id=52）稳定可用。其它国家可能抽到 WhatsApp 号导致收不到短信，所以
``OPENAI_SMS_COUNTRIES`` 白名单之外的国家在自动选号时会打告警但不阻止。

────────────────────────────────────────────────────────────────────────
本文件是**门面**（历史导入路径）。实现在 `services/sms/`。

以下名字**刻意留在门面**，因为测试既读又写它们：

* ``_SMS_CACHE`` / ``_SMS_CACHE_LOCK`` / ``_SMS_VERIFY_LOCK`` —— 测试直接赋值重置
  （``sms_service._SMS_CACHE = None``）。实现侧一律经门面读取，保证「测试重置的
  那一份」与「实现使用的那一份」是同一个对象。
* ``_cache_file`` —— 测试 ``mock.patch.object(sms_service, "_cache_file", …)``
  把它替换成临时路径。实现侧必须经门面调用 ``_facade._cache_file()``，patch 才生效。
* ``logger`` —— 测试 ``patch.object(sms_service.logger, "warning")``。

因此**顺序不能变**：这些名字必须在 import `services.sms` 之前定义好，
否则实现包反向读取门面时取不到它们。

~~~
