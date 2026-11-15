// 首页任务汇总必须真实执行与页面相同的纯函数。
import { build } from 'rolldown'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join, resolve } from 'node:path'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const temp = await mkdtemp(join(tmpdir(), 'dashboard-tasks-'))
const bundle = join(temp, 'dashboardTasks.mjs')
try {
  await build({
    input: resolve(frontend, 'src/lib/dashboardTasks.ts'),
    output: { format: 'esm', file: bundle },
    platform: 'neutral',
  })
  const { summarizeDashboardTasks } = await import(pathToFileURL(bundle).href)
  const failures = []
  let checked = 0
  const check = (name, actual, expected) => {
    checked += 1
    if (JSON.stringify(actual) !== JSON.stringify(expected)) {
      failures.push({ name, actual, expected })
    }
  }

  const task = status => ({ status })
  const tasks = [
    task('running'), task('pending'), task('failed'),
    task('running'), task('done'), task('pending'),
    task('stopped'), task('failed'), task('failed'),
  ]

  // 错误分支：把等待中并入运行中、把已停止计入失败、漏掉空列表都会失败。
  check('分别汇总进行中、等待中和失败任务', summarizeDashboardTasks(tasks), {
    running: 2,
    pending: 2,
    failed: 3,
  })
  check('空任务列表显示真实的零', summarizeDashboardTasks([]), {
    running: 0,
    pending: 0,
    failed: 0,
  })

  console.log(JSON.stringify({ checked, failures }))
  process.exitCode = failures.length ? 1 : 0
} finally {
  await rm(temp, { recursive: true, force: true })
}
