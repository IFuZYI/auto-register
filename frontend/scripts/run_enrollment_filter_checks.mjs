// 面板页：默认只显示已登记的账号 + 「显示未上传」开关（Q11=B / Q16）。
import { readFileSync } from 'node:fs'
const failures = []
let checked = 0
const ok = (c, l) => { checked++; if (!c) failures.push(l) }
const panel = readFileSync(new URL('../src/components/settings/PanelComparisonPanel.tsx', import.meta.url), 'utf8')

// 开关存在且默认关
ok(/showUnenrolled/.test(panel), '面板页没有「显示未上传」开关')
ok(/useState\(false\)[\s\S]{0,120}showUnenrolled|showUnenrolled[\s\S]{0,60}useState\(false\)/.test(panel),
   '「显示未上传」开关的默认值不是关闭')
// 过滤逻辑：未登记（local_only）默认被隐藏
ok(/local_only/.test(panel), '过滤没有按 local_only（未上传）判断')
// 页头提示条：还有 N 个未上传（Q16=A）
ok(/未上传/.test(panel), '页头没有「还有 N 个未上传」的提示')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
