// 真实执行 chatgptTokenPlan.ts 的纯函数。
// 用法（在 frontend/ 下）：node scripts/run_chatgpt_token_plan_checks.mjs
// 输出：一行 JSON {checked, failures: [...]}；退出码 0/1。
import { rolldown } from 'rolldown'

const ENTRY = new URL('../src/lib/chatgptTokenPlan.ts', import.meta.url).pathname
const bundle = await rolldown({ input: ENTRY })
const { output } = await bundle.generate({ format: 'esm' })
const mod = await import('data:text/javascript;base64,' + Buffer.from(output[0].code).toString('base64'))

const failures = []
let checked = 0
function eq(actual, expected, label) {
  checked++
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    failures.push(`${label}: got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`)
  }
}

const { TOKEN_PLANS, tokenPlanOf, tokenPlanExtra, DEFAULT_TOKEN_PLAN } = mod

// ── 必须是三种方案（用户要求：Token 方案合并成三选一） ──
eq(TOKEN_PLANS.length, 3, '方案数量')
eq(TOKEN_PLANS.map((p) => p.value),
   ['refresh_token_login', 'refresh_token_codex', 'access_token_only'], '方案取值与顺序')

// 默认仍是「有 RT」——别把既有默认行为改掉
eq(DEFAULT_TOKEN_PLAN, 'refresh_token_login', '默认方案')
eq(tokenPlanOf({ mode: 'refresh_token', route: 'login' }), 'refresh_token_login', '有RT+登录 → 方案1')
eq(tokenPlanOf({ mode: 'refresh_token', route: 'codex' }), 'refresh_token_codex', '有RT+Codex → 方案2')
eq(tokenPlanOf({ mode: 'access_token_only', route: 'codex' }), 'access_token_only', '无RT → 方案3')
eq(tokenPlanOf({ mode: 'access_token_only', route: 'login' }), 'access_token_only', '无RT 时路线无关')

// ── 三个方案各自要落成后端两个键 ──
eq(tokenPlanExtra('refresh_token_login'),
   { chatgpt_registration_mode: 'refresh_token', chatgpt_rt_route: 'login',
     chatgpt_has_refresh_token_solution: true }, '方案1 落键')
eq(tokenPlanExtra('refresh_token_codex'),
   { chatgpt_registration_mode: 'refresh_token', chatgpt_rt_route: 'codex',
     chatgpt_has_refresh_token_solution: true }, '方案2 落键')
eq(tokenPlanExtra('access_token_only'),
   { chatgpt_registration_mode: 'access_token_only', chatgpt_has_refresh_token_solution: false },
   '方案3 落键（无 RT 不带 route 键）')

// ── 文案要说清代价（用户要求「要注明」） ──
const codex = TOKEN_PLANS.find((p) => p.value === 'refresh_token_codex')
const login = TOKEN_PLANS.find((p) => p.value === 'refresh_token_login')
const none = TOKEN_PLANS.find((p) => p.value === 'access_token_only')
checked++
if (!/接码/.test(codex.hint)) failures.push('Codex 方案的说明没提接码代价')
checked++
if (!/不需要接码|无需接码/.test(login.hint)) failures.push('登录方案的说明没写「不需要接码」')
checked++
if (!/Access Token/.test(none.hint)) failures.push('无 RT 方案的说明没说清产出什么')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
