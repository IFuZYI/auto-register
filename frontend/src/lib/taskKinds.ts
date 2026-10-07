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
  auto_refresh: { label: '自动刷新', color: 'cyan' },
  chatgpt2api_sync: { label: '凭证同步', color: 'magenta' },
}

/** 任务类型 → 标签与颜色；未知类型原样显示（历史任务兼容）。 */
export function taskKindMeta(source?: string): TaskKindMeta {
  const key = String(source || '').trim()
  if (!key) return { label: '未知', color: 'default' }
  return KINDS[key] || { label: key, color: 'default' }
}
