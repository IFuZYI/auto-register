// 真实执行组件源码的「说明文字是否走 tooltip」检查。
// 这里不渲染 React（无 jsdom），而是检查组件产出的静态结构：
// 说明文字必须以 tooltip 形式提供，不能作为常驻正文占版面。
// 用法（frontend/ 下）：node scripts/run_form_hint_checks.mjs
import { readFileSync } from 'node:fs'

const failures = []
let checked = 0
const ok = (cond, label) => { checked++; if (!cond) failures.push(label) }

const read = (p) => readFileSync(new URL(`../src/${p}`, import.meta.url), 'utf8')

// ── 1. 三个 ChatGPT 控件：说明必须通过 title（tooltip）而不是常驻 Text ──
for (const [file, name] of [
  ['components/ChatGPTTokenPlanSelect.tsx', 'Token 方案'],
  ['components/ChatGPTRegisterFlowSelect.tsx', '注册方式'],
  ['components/ChatGPTBind2faSwitch.tsx', '绑定 2FA'],
]) {
  const src = read(file)
  ok(/title=\{/.test(src), `${name}: 没有把说明挂到 title（tooltip）上`)
  // 常驻说明用 Typography.Text 渲染 —— 出现即视为占版面
  ok(!/<Text\s+type="secondary">\s*\{/.test(src),
     `${name}: 仍有常驻 <Text type="secondary"> 说明，应改为 tooltip`)
}

// ── 2. 任务页与账号页的表单项：说明走 Form.Item 的 tooltip 属性 ──
for (const [file, name] of [
  ['pages/RegisterTaskPage.tsx', '任务页'],
  ['pages/Accounts.tsx', '账号页'],
]) {
  const src = read(file)
  const hasTooltipFormItem = /<Form\.Item[\s\S]{0,200}?tooltip=/.test(src)
  ok(hasTooltipFormItem, `${name}: 没有用 Form.Item 的 tooltip 属性承载说明`)
}

// ── 2b. 邮箱导入面板：helper_text 走 tooltip，但「两步流程」警告必须留下 ──
const mail = read('components/settings/MailImportPanel.tsx')
ok(/<Tooltip title=\{selectedProvider\.helper_text\}>/.test(mail),
   '邮箱导入：helper_text 没有走 tooltip')
ok(!/<Typography\.Text type="secondary">\{selectedProvider\.helper_text\}/.test(mail),
   '邮箱导入：helper_text 仍以常驻正文渲染')
ok(/导入后还要勾选入池/.test(mail),
   '邮箱导入：删掉了「导入后还要勾选入池」警告 —— 那是操作步骤，不是注释，必须留在版面')

// ── 3. 全局配置页已是正确做法，别被改回去 ──
const panel = read('components/settings/RegisterSettingsPanel.tsx')
ok(/tooltip=\{mode\.desc\}/.test(panel),
   '全局配置页：mode.desc 必须继续走 tooltip（原注释说明了原因：长 extra 会拉歪两列基线）')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
