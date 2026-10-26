// 面板页：状态筛选**驱动**同步范围，不另设「显示未上传」开关。
// 用法（frontend/ 下）：node scripts/run_panel_filter_driven_checks.mjs
import { readFileSync } from 'node:fs'
const failures = []
let checked = 0
const ok = (c, l) => { checked++; if (!c) failures.push(l) }
const panel = readFileSync(new URL('../src/components/settings/PanelComparisonPanel.tsx', import.meta.url), 'utf8')
const lib = readFileSync(new URL('../src/lib/panelComparison.ts', import.meta.url), 'utf8')

// 1) 去掉独立开关（用户要求「不要单独按钮」）
ok(!/showUnenrolled/.test(panel), '「显示未上传」开关还在 —— 用户要求由状态筛选实现')
ok(!/显示未上传/.test(panel), '页面上仍有「显示未上传」字样')

// 2) 状态筛选直接决定可见行（不再额外过滤 local_only）
ok(/stateFilter/.test(panel), '缺少状态筛选')

// 3) 同步范围 = 当前筛选出的行，不是全量
ok(/filteredRows|visibleRows/.test(panel), '没有「当前筛选出的行」这个概念')
ok(!/selectPushIds\(platformRows\)/.test(panel),
   '推送仍读 platformRows（全量）—— 应当读当前筛选结果')
ok(!/selectPullIds\(platformRows\)/.test(panel),
   '拉回仍读 platformRows（全量）—— 应当读当前筛选结果')

// 4) 选中「未上传」时只给上传按钮
ok(/local_only/.test(panel), '没有针对 local_only 的分支')
ok(/上传/.test(panel), '缺少上传按钮文案')

// 5) 状态筛选的六档都在（用户列出的那六个）
for (const label of ['全部', '已同步', '凭证不同', '无法比对', '未上传', '仅远端']) {
  ok(new RegExp(label).test(panel) || /stateLabels/.test(panel), `筛选档位缺少「${label}」`)
}

// 6) 选「未上传」时**只**给上传按钮（用户明确要求）
ok(/actionsForState\('local_only'\)|local_only'\) return \['upload'\]/.test(lib) ||
   /local_only'\s*\)\s*return \['upload'\]/.test(lib),
   '未上传状态没有收窄成「只给上传」')

// 7) 纯函数放 lib，便于脚本直接测
ok(/export function actionsForState|export function uploadIds/.test(lib) ||
   /export function actionsForState|export function uploadIds/.test(panel),
   '缺少「某状态下该显示哪些动作」的纯函数')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
