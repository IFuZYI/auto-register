// 面板动作映射：上传必须用 upload_actions，不许复用 sync_actions。
// 代码审查 2026-10-27 C1 抓到的 bug：CPA 的 sync 是 sync_cliproxyapi_status，
// 上传复用 sync 映射 → 点「上传未上传」实际发的是「同步远端状态」。
import { readFileSync } from 'node:fs'
const failures = []
let checked = 0
const ok = (c, l) => { checked++; if (!c) failures.push(l) }

const panel = readFileSync(new URL('../src/components/settings/PanelComparisonPanel.tsx', import.meta.url), 'utf8')
const page = readFileSync(new URL('../src/pages/PanelManagement.tsx', import.meta.url), 'utf8')

// 1) 上传调用必须传 upload 映射
ok(/platformActions\.upload/.test(panel), '上传按钮没传 platformActions.upload')
ok(/platformActions\.upload\b[\s\S]{0,40}upload-to-remote|upload-to-remote[\s\S]{0,80}platformActions\.upload/.test(panel),
   '上传调用的参数里没看到 upload 映射')

// 2) runBatch 不能再无条件读 sync
ok(/actionMap\?\.\[plat\]/.test(panel), 'runBatch 没有接显式映射参数（仍在复用 sync）')
ok(!/platformActions\.sync\?\.\[plat\] \|\| \(plat === platform \? fallbackActionId/.test(panel),
   'runBatch 仍在用 sync 映射解析所有动作')

// 3) 状态同步调用要显式传 sync
ok(/platformActions\.sync/.test(panel), '状态同步没传 sync 映射')

// 4) 页面要把 upload_actions 传下去
ok(/upload:\s*selectedPanel\.upload_actions/.test(page), 'PanelManagement 没传 upload_actions')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
