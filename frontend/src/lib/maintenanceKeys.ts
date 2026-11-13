/**
 * 「本地维护」面板的平台表与配置键 —— 纯数据，不依赖 React。
 *
 * 为什么单独一个文件：前端没有测试运行器，而 `MaintenancePanel.tsx` 带
 * antd / React 依赖，rolldown 打不进来。抽成纯模块后，`frontend/scripts/
 * run_maintenance_keys_checks.mjs` 能**真实执行** `localKeysFor()` 做契约检查
 * （键表发出去之前必须都登记过；没有面板的平台不许读写 `*_sync_strategy`）。
 *
 * `syncable` 是**必填布尔**：漏写由 `tsc -b` 直接抓，而不是悄悄退化成
 * 「有面板」——AWS / Kiro 没有可同步的面板（后端只有 probe / refresh），
 * 多渲染一个永远是 `local_only` 的下拉只会让人以为配了会生效。
 */
export interface LocalPlatform {
  prefix: string
  label: string
  subject: string
  /** 有没有可同步的面板。false = 不渲染「刷新策略」，也不读写 `*_sync_strategy`。 */
  syncable: boolean
}

/** 本地维护的平台。顺序 = 界面顺序（与 `lib/platforms.ts` 的 PLATFORMS 一致）。 */
export const LOCAL_PLATFORMS: LocalPlatform[] = [
  { prefix: 'chatgpt', label: 'ChatGPT', subject: '账号', syncable: true },
  { prefix: 'grok', label: 'Grok', subject: '账号', syncable: true },
  { prefix: 'aws', label: 'AWS', subject: '账号', syncable: false },
  { prefix: 'kiro', label: 'Kiro', subject: '账号', syncable: false },
]

/**
 * 本页要读写的全部本地维护配置键。
 *
 * **显式列出**而不是 `form.getFieldsValue(true)`：后者会把整份 config
 * （130 个键）提交上去，其中未渲染的字段值是 undefined，归一化会把它们
 * 变成「有意义的默认值」，等于保存一次就清空用户配好的东西（踩过）。
 *
 * 没有面板的平台（`syncable: false`）**不列** `*_sync_strategy`：这个键在
 * 后端根本不存在，提交上去只会被当成未知键丢弃（还会回报 `ignored`）。
 */
export function localKeysFor(platforms: LocalPlatform[] = LOCAL_PLATFORMS): string[] {
  return platforms.flatMap(({ prefix, syncable }) => [
    `${prefix}_auto_probe_enabled`,
    `${prefix}_probe_mode`,
    `${prefix}_probe_sample_percent`,
    `${prefix}_probe_strategy`,
    `${prefix}_probe_interval`,
    `${prefix}_auto_refresh_enabled`,
    `${prefix}_refresh_scope`,
    `${prefix}_refresh_random_count`,
    `${prefix}_refresh_interval`,
    `${prefix}_refresh_disable_banned`,
    ...(syncable ? [`${prefix}_sync_strategy`] : []),
  ])
}
