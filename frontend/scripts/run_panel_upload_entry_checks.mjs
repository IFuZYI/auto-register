// 账号页的「上传到面板」入口（Q3/Q8/Q15）。
// 用法（frontend/ 下）：node scripts/run_panel_upload_entry_checks.mjs
import { readFileSync } from 'node:fs'

const failures = []
let checked = 0
const ok = (cond, label) => { checked++; if (!cond) failures.push(label) }
const page = readFileSync(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

// 入口存在
ok(/上传到面板/.test(page), '账号页没有「上传到面板」入口')
// 目标面板可选（用户要求「上传到指定平台」）
ok(/PANEL_UPLOAD_TARGETS|panelUploadTarget/.test(page), '没有选择目标面板的控件')
// 走既有批量端点，不另造一套
ok(/\/actions\/\$\{[^}]*\}\/\$\{[^}]*\}\/batch|\/actions\/.+\/batch/.test(page),
   '没有调用既有的 /actions/{platform}/{action_id}/batch 端点')
// Q8=C：勾了就传勾选的，没勾传筛选结果
ok(/selectedRowKeys\.length > 0 \? 'selected' : 'all'/.test(page),
   '缺少「勾选优先、否则用筛选结果」的既有 scope 约定')
ok(/all_filtered/.test(page), '批量范围没走 all_filtered（筛选结果）这条路')
// 确认弹窗要显示确切数量（筛选条件写错会把几百个号推上去）
ok(/将上传/.test(page) || /共 \$\{|数量/.test(page), '确认弹窗没有显示将上传的数量')
// 不能把面板动作塞进行菜单（既有注释说明了它们属于面板页）——
// 但批量入口必须在页面上
ok(/scope !== 'panel'/.test(page), '行菜单仍应过滤面板动作（改动面要克制）')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
