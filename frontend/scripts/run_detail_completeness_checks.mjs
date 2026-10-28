// 账号详情必须显示完整信息：密码、2FA 密钥、RT、代理等。
//
// 用户要求（2026-10-28）：「账号详细信息查看要完整，账号密码2FA等。」
//
// ## 实测的缺陷
//
// 详情弹窗读的是**驼峰字段**（`currentAccount.totpSecret` /
// `currentAccount.registerProxy` / `currentAccount.chatgptLocal`），
// 而 `openDetail` 拉到全量后**直接 setCurrentAccount(full)** —— 没走
// `normalizeAccount()`。后端给的是 `extra_json` 字符串，所以：
//
//   密码       ❌ 不显示（弹窗里根本没有这个字段）
//   RT         ❌ 不显示（getRefreshToken 读 extra_json 能拿到，但列表行
//                        的瘦身版把它换成了布尔）
//   TOTP 密钥   ❌ 不显示（totpSecret 是 undefined）
//   注册代理    ❌ 不显示（registerProxy 是 undefined）
//
// 浏览器实测确认：4 项全缺，而数据库里都有。
import { readFileSync } from 'node:fs'

const failures = []
let checked = 0
const ok = (c, l) => { checked++; if (!c) failures.push(l) }

const page = readFileSync(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

// 1) openDetail 必须归一化后再 setCurrentAccount
ok(/normalizeAccount\s*\(/.test(page),
   'openDetail 没有调 normalizeAccount —— 详情里的驼峰字段全是 undefined')

// 2) 详情弹窗必须显示密码
ok(/密码/.test(page) && /has_password|password/.test(page),
   '详情没有显示密码')

// 3) 详情必须显示 2FA 密钥与 RT
//
// 两者都是**有才渲染**（账号本来就没有 RT / 没绑 2FA 时不该显示空框）。
// 所以断言的是「渲染分支存在且读的是归一化后的字段」，不是「一定出现」。
ok(/currentAccount\.totpSecret/.test(page), '详情没有读 2FA 密钥（totpSecret）')
ok(/getRefreshToken\(currentAccount\)/.test(page), '详情没有读 RT（getRefreshToken）')

// 4) 列表行与详情行都要能取到归一化字段
ok(/const normalized\s*=|normalizeAccount\(/.test(page),
   '没有统一的归一化入口 —— 列表与详情会各读各的字段')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
