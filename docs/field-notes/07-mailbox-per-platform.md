# 邮箱只对「用掉它的平台」一次性

`used` 不是全局锁。同一个地址注册过 ChatGPT 仍可注册 Grok —— 只用一个全局 `used` 会把地址对所有平台一起锁死。

## 重构约束

- `used_platforms` 的 `,grok,chatgpt,` 格式不许改
- 取号必须跨库比对 `accounts` 表（池内记账会因任务崩掉而缺项）
- `unpooled` 不参与取号 —— 必须用户显式「导入邮箱池」
- `consecutive_failures >= 3` 才跳过，否则失败的号一直霸占队首

## 证据（25 块，自动抽取自源码注释）

> 原始索引见 `_index.json`。改对应代码前逐块读完 —— 这些分支不是冗余，是故障换来的。

### `core/db/icloud_alias_repository.py:34-38`

~~~
#: 连续失败到这个次数后，取号跳过该别名。
#:
#: 为什么是 3：一次失败可能是网络抖动或 OpenAI 的瞬时风控；两次仍可能巧合。
#: 三次连续失败基本可以确定这个地址被标记了（实测：同一地址两次冒烟都撞
#: 429）。阈值太大则被拒的号还会被派很多次，太小则一次抖动就废掉一个号。
~~~

### `core/db/icloud_alias_repository.py:83-85`

~~~
        # **自带实现，不 import `services`**：架构约束连函数内导入也扫
        # （实测让 `test_dependency_direction.py` 变红）。别名表的 SQLite
        # 操作不复杂，直接写在这里。
~~~

### `core/db/icloud_alias_repository.py:90-92`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:108-109`

~~~
        # **必须走 icloud 平台库**：`icloud_aliases` 是分库表，用默认库的
        # `current_engine()` 会撞 `no such table: icloud_aliases`（实测）。
~~~

### `core/db/icloud_alias_repository.py:112-114`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:145-147`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:186-188`

~~~
        # **自带实现，不 import `services`** —— 架构约束连函数内导入也扫
        # （`test_dependency_direction.py` 实测抓到过）。别名表的 SQLite 操作
        # 不复杂，写在这里比跨层调用更清楚。
~~~

### `core/db/icloud_alias_repository.py:243-245`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:296-298`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:314-322`

~~~
记一次失败（或清零），返回累加后的失败次数。

        **为什么要记账**：取号按 id 升序，失败时把号放回 `available` 会让它
        回到队首 —— 下次任务还是它，每次都撞同一堵墙（实测 2026-10-28：两次
        冒烟都取到同一个地址、两次都 429，池子里另外 24 个可用号从未被尝试）。

        连续失败到 `MAX_CONSECUTIVE_FAILURES` 后 `find_candidates` 会跳过它。
        领取成功时用 `reset=True` 清零（这个号能用了）。
        
~~~

### `core/db/icloud_alias_repository.py:355-357`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:378-380`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:406-408`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:449-452`

~~~
                # 还要看这个地址有没有被该平台用掉（代码审查 2026-10-27 I3）：
                # 真正取号时会按 `used_platforms` 过滤，这里不同步的话会挑到
                # 「别名都已被本平台用完」的主号 → 领不到 → 回落生成 → 远程
                # 拒绝生成 → 仍是用户报的那个失败。
~~~

### `core/db/icloud_alias_repository.py:508-510`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:539-541`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:565-567`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:596-598`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:662-664`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:735-737`

~~~
            # **按 (失败次数, id) 排序**：失败过的号排到后面，让任务换一个试。
            # 只按 id 排的话，失败的号释放回 available 后又回到队首（实测：
            # 两次冒烟都取到同一个地址）。
~~~

### `core/db/icloud_alias_repository.py:749-751`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:779-788`

~~~
同步下来的别名写入（`_upsert_alias` 的仓储版）。

        三条业务规则原样保留（都是踩过坑的）：
        1. **新建的默认 `unpooled`** —— 必须用户勾选才进池（用户明确要求）；
        2. **已存在的行不动 `pool_status`** —— 用户可能已标成 used/in_use，
           一次同步不该把记账打回；
        3. **同地址的远程行、归属不同账号时跳过** —— 抢走它的 account_id 会让
           远程行归到另一个本地账号名下而 source 仍是 remote，之后删除/停用
           会按远程分支调远端服务，账实不符（ADR-0001）。
        
~~~

### `core/db/icloud_alias_repository.py:832-834`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/icloud_alias_repository.py:872-874`

~~~
        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
~~~

### `core/db/migrations.py:284-291`

~~~
iCloud 别名补 share_token 与 pool_status。

    `share_token` 每行不同随机值，不能用一条 UPDATE。
    `pool_status` 给老库补列：**默认值取 'unpooled'（未入池）而不是 'available'**。
    用户要求「账号邮箱要选择导入才能导入邮箱池，不是全部导入邮箱池」——
    迁移前库里那批别名从没经过一次显式入池动作，直接当成可领等于绕过这个要求，
    下一次注册就会拿它们去跑。补成 unpooled 后，用户在池页面勾选导入即可放行。
    
~~~
