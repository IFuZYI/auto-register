// 任务类型 → 展示元数据（标签 + 颜色）。任务运行页用它渲染彩色标签。
//
// 与后端 `RegisterTaskRecord.source` 同源。历史任务可能带已下线的 source
// （如 payment）—— 未知值原样显示并回落 default 色，不吞掉。

export interface TaskKindMeta {
  label: string
  /** antd Tag 的 preset 色名（对比度由 index.css 钉住） */
  color: string
}

const KINDS: Record<string, TaskKindMeta> = {
  manual: { label: '注册', color: 'blue' },
  api: { label: 'API 注册', color: 'geekblue' },
  schedule: { label: '定时注册', color: 'geekblue' },
  backfill_rt: { label: '补 RT', color: 'orange' },
  bind_2fa: { label: '绑 2FA', color: 'purple' },
  refresh_token: { label: '刷新 Token', color: 'green' },
  recover: { label: '恢复账号', color: 'orange' },
  auto_refresh: { label: '自动刷新', color: 'cyan' },
  chatgpt2api_sync: { label: '凭证同步', color: 'magenta' },
}

/**
 * 前缀型来源：后端按平台/动作拼出来的 source，逐个登记不现实。
 *
 * - `auto_probe_<platform>` / `auto_refresh_<platform>`：本地自动维护
 *   （`services/local_maintenance.py` 按 `PLATFORM_ACTIONS` 给每个平台注册，
 *   所以 AWS / Kiro 也是这两个前缀）；
 * - `auto_pull_<panel>`：自动维护里从面板拉回状态。
 *
 * （`action:<id>` 单独处理 —— 它要按动作 id 给更具体的文案。）
 *
 * 不登记的话任务页会直接显示 `auto_probe_aws` 这种原始 id —— 平台标签
 * 就在旁边，这里只需要说清「是什么任务」。
 */
const PREFIX_KINDS: { prefix: string; label: string; color: string }[] = [
  { prefix: 'auto_probe_', label: '自动检测', color: 'cyan' },
  { prefix: 'auto_refresh_', label: '自动刷新', color: 'cyan' },
  { prefix: 'auto_pull_', label: '自动同步', color: 'magenta' },
]

/** 批量动作里最常见的两个 id（AWS / Kiro 的授权账号页就这两个动作）。 */
const BATCH_ACTION_LABELS: Record<string, string> = {
  probe: '批量测活',
  refresh_token: '批量刷新 Token',
}

/** 任务类型 → 标签与颜色；未知类型原样显示（历史任务兼容）。 */
export function taskKindMeta(source?: string): TaskKindMeta {
  const key = String(source || '').trim()
  if (!key) return { label: '未知', color: 'default' }
  const exact = KINDS[key]
  if (exact) return exact
  if (key.startsWith('action:')) {
    const actionId = key.slice('action:'.length)
    return { label: BATCH_ACTION_LABELS[actionId] || '批量动作', color: 'blue' }
  }
  const prefixed = PREFIX_KINDS.find(({ prefix }) => key.startsWith(prefix))
  if (prefixed) return { label: prefixed.label, color: prefixed.color }
  return { label: key, color: 'default' }
}
