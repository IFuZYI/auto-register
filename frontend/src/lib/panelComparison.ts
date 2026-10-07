/**
 * 面板对比的纯函数：平台筛选、逐平台计数、状态汇总。
 *
 * 抽到 lib 而不是留在 `PanelComparisonPanel.tsx` 里的原因：前端没有测试运行器，
 * 组件内的逻辑只能靠「对着源码做正则」来测（改了实现就可能失效，是假安全网）。
 * 放在这里的函数可以被 `frontend/scripts/run_panel_filter_checks.mjs` 真实执行
 * 并断言 —— 与 `accountFormat.ts` / `icloud.ts` 同一套做法。
 *
 * 背景（用户要求「CPA 面板 增加 平台选择，比如选择 全部、gpt、grok」）：
 * CPA 同时托管 ChatGPT（`codex`）与 Grok（`xai`），对比表把两类混在一起。
 * 没有平台筛选时，「上传未上传 (32)」看不出这 32 个是哪个平台的。
 *
 * **筛选必须同时作用于表格、统计条与批量动作**：只筛表格的话，用户看着 32 个
 * Grok 行点上传，实际把 ChatGPT 的一起传了。
 */

/** 对比行里筛选与计数要用到的最小形状（不需要整个 ComparisonRow）。 */
export interface PlatformFilterableRow {
  platform?: string
}

/** 状态汇总的键：与后端 `services/panel_comparison.STATE_LABELS` 一致。 */
export type StateSummary = Record<string, number>

/**
 * 已知的对比状态（与后端 `services/panel_comparison.STATE_LABELS` 的键一致）。
 *
 * `summarizeRows` 会把它们全部补 0：筛选条按固定顺序渲染，某个状态在当前
 * 筛选下没有行时要显示 `标签 0`，而不是让那个筛选项整个消失 —— 消失会让用户
 * 以为功能坏了（「之前还能筛『已同步』，怎么没了」）。
 */
const KNOWN_STATES = [
  'local_only',
  'remote_only',
  'credential_diff',
  'unknown_credential',
  'unknown_time',
  'synced',
] as const

/**
 * 平台筛选：`''`（全部）原样返回，否则只留该平台的行。
 *
 * 返回**新数组**，不改入参 —— 调用方（React 的 useMemo）拿到的是稳定快照，
 * 就地修改会让「筛选后的集合」与「原始集合」变成同一个对象，批量动作读到的
 * 行数会跟着筛选变，出错时极难排查。
 */
export function filterRowsByPlatform<T extends PlatformFilterableRow>(
  rows: T[],
  platform: string,
): T[] {
  const all = Array.isArray(rows) ? rows : []
  const wanted = String(platform || '').trim().toLowerCase()
  if (!wanted) return all.slice()
  return all.filter((row) => String(row?.platform || '').trim().toLowerCase() === wanted)
}

/** 逐平台的账号数（含 `total`），给「全部 (213) / ChatGPT (181) / Grok (32)」这类标签用。 */
export function countByPlatform(rows: PlatformFilterableRow[]): Record<string, number> {
  const counts: Record<string, number> = { total: 0 }
  for (const row of Array.isArray(rows) ? rows : []) {
    counts.total += 1
    const key = String(row?.platform || '').trim().toLowerCase()
    if (!key) continue
    counts[key] = (counts[key] || 0) + 1
  }
  return counts
}

/**
 * 状态汇总：`{state: count, ..., total}`。
 *
 * 状态条的数字要跟着**平台筛选**走（用户筛了 Grok 之后「未上传 32」应当是
 * Grok 的 32，而不是两个平台的和）。后端 `summarize()` 算的是全量，筛完之后
 * 必须在前端重算 —— 否则界面上的计数与实际可见的行对不上。
 *
 * 缺失的状态补 0：筛选条按固定顺序渲染，某个状态在当前筛选下没有行时也要
 * 显示 `标签 0` 而不是让整条筛选项消失（消失会让用户以为功能坏了）。
 */
export function summarizeRows(rows: { state?: string }[]): StateSummary {
  const summary: StateSummary = { total: 0 }
  for (const state of KNOWN_STATES) summary[state] = 0
  for (const row of Array.isArray(rows) ? rows : []) {
    summary.total += 1
    const state = String(row?.state || '').trim()
    if (!state) continue
    summary[state] = (summary[state] || 0) + 1
  }
  return summary
}

/**
 * 是否渲染平台选择器。
 *
 * 只对**多平台面板**渲染：CPA 声明了 `platforms: ['chatgpt', 'grok']`，而
 * Sub2API / grok2api / chatgpt2api 只有一个平台 —— 给它们摆一个只有「全部」
 * 一个选项的选择器是纯噪声（用户点了也不会有任何变化）。
 *
 * 去重后判断：注册表里写重了（`['grok', 'grok']`）不该被当成多平台。
 */
export function shouldShowPlatformFilter(platforms?: string[] | null): boolean {
  if (!Array.isArray(platforms)) return false
  const unique = new Set(
    platforms.map((item) => String(item || '').trim().toLowerCase()).filter(Boolean),
  )
  return unique.size > 1
}

/** 方向筛选要读的最小形状。 */
export interface DirectionFilterableRow extends PlatformFilterableRow {
  state?: string
  /** local_newer / remote_newer / time_synced / '' */
  time_relation?: string
  local_id?: number | null
  /** 本地账号状态（registered / expired / invalid / banned）。 */
  local_status?: string
}

/**
 * 「更新远程凭证」的目标：未上传 + 本地较新的凭证不同行。
 *
 * 这是**推送方向**（本地 → 远端）：远端没有的（未上传）要补传；
 * 凭证不同且本地较新的要覆盖。远端较新的**不在此列** —— 推上去会用
 * 本地旧凭证覆盖远端新的（x.ai 的 RT 轮换，覆盖后远端拿到死值）。
 *
 * 同小时（`time_synced`）/ 无法判定（`''`）不动：分秒差异是噪声。
 *
 * 禁用（`banned`）的账号不参与同步（用户要求 2026-10-07）：凭证已死，
 * 推上去只会污染远端面板。后端 `plan_push` 同样跳过 —— 前端这里排除是
 * 为了让按钮上的计数与真实动作一致（「看到的」与「被改的」对不上是
 * 这个页面反复踩过的坑）。
 */
export function selectPushIds(rows: DirectionFilterableRow[]): number[] {
  return (Array.isArray(rows) ? rows : [])
    .filter(
      (row) =>
        row?.local_id &&
        String(row?.local_status || '').trim().toLowerCase() !== 'banned' &&
        (row?.state === 'local_only' ||
          (row?.state === 'credential_diff' && row?.time_relation === 'local_newer')),
    )
    .map((row) => row.local_id as number)
}

/**
 * 「更新本地凭证」的目标：远端较新的凭证不同行。
 *
 * 这是**拉回方向**（远端 → 本地），与 `selectPushIds` 互斥：同一行不可能
 * 同时在两个方向的目标里（`local_newer` 与 `remote_newer` 不可能同时成立）。
 * 禁用（`banned`）的账号不参与同步（用户要求 2026-10-07）。
 */
export function selectPullIds(rows: DirectionFilterableRow[]): number[] {
  return (Array.isArray(rows) ? rows : [])
    .filter(
      (row) =>
        row?.state === 'credential_diff' &&
        row?.time_relation === 'remote_newer' &&
        row?.local_id &&
        String(row?.local_status || '').trim().toLowerCase() !== 'banned',
    )
    .map((row) => row.local_id as number)
}
