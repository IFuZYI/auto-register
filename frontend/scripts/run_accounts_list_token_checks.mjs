// 检查账号列表「裁掉 token 正文」之后，前端是否正确回退到详情端点。
// 用法（frontend/ 下）：node scripts/run_accounts_list_token_checks.mjs
import { readFileSync } from 'node:fs'

const failures = []
let checked = 0
const ok = (cond, label) => { checked++; if (!cond) failures.push(label) }

const page = readFileSync(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

// ── Critical 1：AT 有效期列必须**优先**用后端给的 iat/exp ──
// 列表行已无 AT 正文，只靠 atListSummary 会解不出 exp → 整列「无法解析」。
// 正确做法：atSummaryFromClaims（读 at_iat/at_exp）优先，atListSummary 只作回退。
ok(/atSummaryFromClaims\(parsed\)/.test(page),
   'AT 有效期列没有用后端给的 at_iat/at_exp —— 列表无 AT 正文时该列无法渲染')
ok(/atSummaryFromClaims\(parsed\)\s*\?\?\s*atListSummary/.test(page),
   'AT 有效期列没有「claims 优先、atListSummary 回退」的取法')

// ── Critical 2：详情抽屉必须用详情端点的全量数据，不能直接用列表行 ──
ok(/openDetail/.test(page) || /fetchAccountDetail/.test(page),
   '打开详情时没有拉取全量账号（列表行已无 token 正文，抽屉里的 RT 会显示/复制成占位串）')

// 占位串必须只用于判有无：正文优先取真值，拿不到才回落占位串
ok(/HAS_TOKEN_PLACEHOLDER/.test(page), '缺少占位串常量（判有无用）')
ok(/const raw = extra\.refresh_token[\s\S]{0,200}?if \(raw\) return String\(raw\)/.test(page),
   'getRefreshToken 没有「正文优先」逻辑 —— 详情抽屉会显示占位串而不是真 token')
ok(/const fromExtra = String\(extra\.access_token[\s\S]{0,120}?if \(fromExtra\) return fromExtra/.test(page),
   'getAccessToken 没有「正文优先」逻辑')

// 抽屉里的 RT 必须来自 currentAccount（详情全量），不能来自列表行
ok(/getRefreshToken\(currentAccount\)/.test(page),
   'RT 块没有读 currentAccount（应来自详情端点）')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
