// 远端未连接时，对比显示不能把「未知」说成「未上传」。
import { build } from 'rolldown'
import { readFileSync } from 'node:fs'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const temp = await mkdtemp(join(tmpdir(), 'panel-unavailable-'))
const bundle = join(temp, 'panelComparison.mjs')
try {
  await build({
    input: new URL('../src/lib/panelComparison.ts', import.meta.url).pathname,
    output: { format: 'esm', file: bundle },
    platform: 'neutral',
  })
  const { rowsForComparisonDisplay } = await import(pathToFileURL(bundle).href)
  const failures = []
  let checked = 0
  const check = (name, condition) => {
    checked += 1
    if (!condition) failures.push(name)
  }
  const local = { state: 'local_only', label: '未上传', time_relation: '' }
  const newer = { state: 'credential_diff', label: '凭证不同', time_relation: 'local_newer' }

  const offline = rowsForComparisonDisplay([local, newer], '面板地址未配置')
  check('远端不可用时不再声称未上传', offline.every(row => row.label === '仅本地（未连接远端）'))
  check('不可用时不推断本地较新', offline.every(row => !row.time_relation))
  check('原始状态分类不被显示改写', offline[0].state === 'local_only' && offline[1].state === 'credential_diff')

  const online = rowsForComparisonDisplay([local, newer], '')
  check('正常对比保留原始未上传标签', online[0].label === '未上传')
  check('正常对比保留时间方向', online[1].time_relation === 'local_newer')

  // 首次加载不能先闪一屏「0」再跳成真实计数 —— 假零会被读成「远端是空的」。
  const panel = readFileSync(
    new URL('../src/components/settings/PanelComparisonPanel.tsx', import.meta.url), 'utf8')
  check('筛选条在数据到位前不渲染', /\{payload \? \(\s*<Space wrap size=\{6\}>/.test(panel))
  check('平台选择器同样等到数据到位', /shouldShowPlatformFilter\(platforms\) && payload \?/.test(panel))
  // 这个组件只在已选中面板时渲染 —— 「请先在左侧选择面板」永不可能成立。
  check('空态不再提示「先选面板」', !/在左侧选择一个面板/.test(panel))
  check('读取失败与「没有账号」区分开', /loadError/.test(panel))

  console.log(JSON.stringify({ checked, failures }))
  process.exitCode = failures.length ? 1 : 0
} finally {
  await rm(temp, { recursive: true, force: true })
}
