// 静默失败与退出登录的接线契约。
//
// 三处修复的共同点：**界面不能对失败保持沉默**，也不能让「已退出」只发生在
// 浏览器本地 —— 后端的 JWT 仍有效 7 天。
import { readFileSync } from 'node:fs'

const failures = []
let checked = 0
const ok = (cond, label) => { checked++; if (!cond) failures.push(label) }

const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
const accounts = readFileSync(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

// ① 退出登录必须调用后端吊销端点，而不是只删本地令牌。
ok(/\/auth\/logout/.test(app), '退出登录没有调用 /auth/logout —— 令牌在服务端仍然有效')
ok(/clearToken\(\)[\s\S]{0,200}navigate\('\/login'\)/.test(app),
   '退出后没有回到登录页')

// ② 详情保存失败要告知用户，并且不能假装成功。
const detailBlock = accounts.slice(
  accounts.indexOf('const handleDetailSave'),
  accounts.indexOf('const handleBatchStatusSync'),
)
ok(/catch/.test(detailBlock), '详情保存没有 catch —— PATCH 失败时弹窗不关、也没有提示')
ok(/message\.error/.test(detailBlock), '详情保存失败没有 message.error')

// ③ 批量删除失败要告知用户，不能静默。
const deleteBlock = accounts.slice(
  accounts.indexOf('const handleBatchDelete'),
  accounts.indexOf('const handleAdd'),
)
ok(/catch/.test(deleteBlock), '批量删除没有 catch —— 失败后勾选还在，用户以为已删')
ok(/message\.error/.test(deleteBlock), '批量删除失败没有 message.error')

// ④ 侧栏二级菜单（平台 / 面板）加载失败不能静默清空。
ok(!/apiFetch\('\/platforms'\)[\s\S]{0,400}\.catch\(\(\) => \{\}\)/.test(app),
   '平台列表加载失败仍是静默 catch —— 侧栏会变成展不开的空分组')
ok(!/apiFetch\('\/integrations\/panels'\)[\s\S]{0,400}\.catch\(\(\) => \{\}\)/.test(app),
   '面板列表加载失败仍是静默 catch')
ok(/平台列表加载失败/.test(app), '平台列表失败没有提示')
ok(/面板列表加载失败/.test(app), '面板列表失败没有提示')

// ⑤ `/mail` 是菜单分组，本身没有页面 —— 要有兜底重定向，否则直达 404。
ok(/<Route path="\/mail" element=\{<Navigate/.test(app),
   '/mail 没有兜底路由，直达会落到 NotFound')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)