// 真实执行 lib/panelComparison.ts 的 actionsForState / selectUploadIds。
import { rolldown } from 'rolldown'
const ENTRY = new URL('../src/lib/panelComparison.ts', import.meta.url).pathname
const bundle = await rolldown({ input: ENTRY })
const { output } = await bundle.generate({ format: 'esm' })
const mod = await import('data:text/javascript;base64,' + Buffer.from(output[0].code).toString('base64'))

const failures = []
let checked = 0
const eq = (a, b, l) => { checked++; if (JSON.stringify(a) !== JSON.stringify(b)) failures.push(`${l}: got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`) }

const { actionsForState, selectUploadIds } = mod

// 未上传：只有上传
eq(actionsForState('local_only'), ['upload'], '未上传 → 只给上传')
// 已同步：凭证一致，没得推没得拉，只剩状态同步
eq(actionsForState('synced'), ['status'], '已同步 → 只给状态同步')
// 仅远端：本地没有行，按本地发起的动作都无处落脚
eq(actionsForState('remote_only'), [], '仅远端 → 不给动作')
// 凭证不同 / 无法比对：三个方向都要给
for (const s of ['credential_diff', 'unknown_credential']) {
  eq(actionsForState(s), ['upload', 'push', 'pull', 'status'], `${s} → 全给`)
}
// 全部：也全给
eq(actionsForState(''), ['upload', 'push', 'pull', 'status'], '全部 → 全给')

// selectUploadIds：只取未登记、非 banned、有 local_id 的行
const rows = [
  { state: 'local_only', local_id: 1, local_status: 'registered' },
  { state: 'local_only', local_id: 2, local_status: 'banned' },      // 禁用不传
  { state: 'synced', local_id: 3, local_status: 'registered' },      // 已登记不重传
  { state: 'credential_diff', local_id: 4, local_status: 'registered' },
  { state: 'local_only', local_id: null, local_status: 'registered' }, // 无 id
]
eq(selectUploadIds(rows), [1], '上传目标只含未登记且非禁用的行')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
