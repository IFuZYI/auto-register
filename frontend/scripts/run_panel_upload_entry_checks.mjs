// 面板登记只在一个页面发起，避免两套范围与状态口径再次分叉。
import { readFileSync } from 'node:fs'

const failures = []
let checked = 0
const ok = (cond, label) => { checked++; if (!cond) failures.push(label) }
const accounts = readFileSync(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const panel = readFileSync(new URL('../src/components/settings/PanelComparisonPanel.tsx', import.meta.url), 'utf8')

ok(/a\?\.scope !== 'panel'/.test(accounts), '账号行菜单必须过滤外部面板动作')
ok(!/panelUpload|PANEL_UPLOAD_TARGETS|handlePanelUpload/.test(accounts), '账号页不应再拥有一套上传状态与入口')
ok(!/title: '2API 接入'|label="2API"/.test(accounts), '账号台账不应展示外部面板接入结果')
ok(/上传未上传/.test(panel), '面板页保留按真实对比结果登记账号的入口')
ok(/selectUploadIds\(filteredRows\)/.test(panel), '面板登记范围来自当前筛选出的真实对比行')
ok(/\/actions\/\$\{plat\}\/\$\{actionId\}\/batch/.test(panel), '面板登记复用既有批量动作端点')
ok(/okText="上传"/.test(panel) && /上传成功后这些账号会被登记/.test(panel), '登记确认说明数量与后续同步范围')
ok(/rowsForComparisonDisplay\(filteredRows/.test(panel), '远端未连接时不能把本地行误称为未上传')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
