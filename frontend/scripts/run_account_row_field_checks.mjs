// 列表行必须读后端**实际返回**的字段。
//
// ## 修的是哪三个 bug
//
// 安全加固（commit 14c61d6）把列表响应里的 `password` / `totp_secret` 正文
// 换成了 `has_password` / `has_totp_secret` 布尔，但前端没跟上：
//
// 1. **密码列恒空**：列定义还是 `dataIndex: 'password'`，而响应里没有这个键。
// 2. **「2FA 已绑」标签永不显示**：读 `record.totpSecret`，而它从来没被赋值
//    过（全文件只有读、没有写）—— 正确来源是 `extra.has_totp_secret`。
// 3. **「复制 2FA 密钥」菜单项永不出现**：同一个 `acc.totpSecret`。
//
// 用户报的就是「2FA 显示不见了」+「账号信息有问题」。
import { readFileSync } from 'node:fs'

const failures = []
let checked = 0
const ok = (c, l) => { checked++; if (!c) failures.push(l) }

const page = readFileSync(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

// —— 密码列：不许再绑不存在的 `password` 键
// **只匹配真正的列定义行**（前面有 `dataIndex:` 的赋值形态），否则注释里
// 提到这个词也会误报 —— 第一版就这么红了一次。
ok(!/^\s*dataIndex: 'password',?$/m.test(page),
   '密码列仍用 dataIndex: "password" —— 列表响应已不含该键（恒空）')
ok(/has_password/.test(page),
   '没有读 has_password —— 密码列无法显示「已设置」')

// —— 2FA：必须读后端给的布尔
ok(/has_totp_secret/.test(page),
   '没有读 has_totp_secret —— 2FA 标签/菜单项永远不会出现')

// —— 列表行不许读 `totpSecret` 正文（它只存在于详情行）
// 列表行只有 `has_totp_secret`；读正文会让标签/菜单项永远不显示。
ok(!/record\.totpSecret/.test(page),
   '列表行仍在读 record.totpSecret —— 列表响应只有 has_totp_secret 布尔')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
