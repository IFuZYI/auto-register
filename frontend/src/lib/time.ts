/**
 * 时间显示的统一口径：**浏览器本地时区 + 时区标注**。
 *
 * ## 为什么需要（用户报的问题）
 *
 * 「本地和远程面板服务器时区可能不同」——
 * - 远端面板实测用 `+08:00` 写时间（grok2api 的 `createdAt`、CPA 的
 *   `modtime`）；
 * - 本地库写的是 UTC（`updated_at` 是 naive UTC datetime）；
 * - 后端对比前已把两边归一成 UTC ISO 串（`services/panel_comparison._iso`），
 *   所以**比较**（谁更新）是对的。
 *
 * 但**显示**如果直接截断 ISO 串，用户看到的是 UTC 时钟：对 `+08:00` 的用户
 * 来说每个时间都差 8 小时，且界面上没有任何标注说明这是 UTC —— 「远端较新」
 * 的判断会被用户按本地钟面重算一遍，越看越糊涂。
 *
 * ## 做法
 *
 * 1. 解析后端给的 ISO 串（带时区信息，`+00:00` / `Z` 都认）；
 * 2. 转成**浏览器本地时区**显示（用户按自己的钟面读，不需要心算）；
 * 3. 界面附**时区标注**（如 `UTC+8`），明确「显示的是哪个时区的时间」；
 * 4. 原始串保留在 tooltip 里（排障时能看到远端到底怎么写的）。
 *
 * 解析失败时退回截断显示（保底可见，不隐藏数据）。
 */

/** 补零到两位。 */
function pad2(value: number): string {
  return String(value).padStart(2, '0')
}

/**
 * 解析时间串：认 ISO 8601（带 `Z` / 带偏移 / 不带时区按 UTC）与 epoch。
 *
 * 与后端 `parse_timestamp` 同一套口径 —— 两边对「什么算合法时间」的认定
 * 必须一致，否则会出现「后端说能比、前端显示不出来」的裂缝。
 */
export function parseTimeValue(value: unknown): Date | null {
  if (value === null || value === undefined || value === '') return null
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  if (typeof value === 'number') {
    // epoch 秒 / 毫秒都认（13 位以上当毫秒）
    const ms = value > 1e11 ? value : value * 1000
    const d = new Date(ms)
    return Number.isNaN(d.getTime()) ? null : d
  }
  const text = String(value).trim()
  if (!text) return null
  if (/^\d+$/.test(text)) {
    const n = Number(text)
    const ms = n > 1e11 ? n : n * 1000
    const d = new Date(ms)
    return Number.isNaN(d.getTime()) ? null : d
  }
  let normalized = text.replace(' ', 'T')
  // 不带时区的 ISO（后端本应用写的是 UTC）：补 Z 再解析 —— 不补的话
  // JS 按**本地时区**解释，等于把 UTC 当成当地时间，差一个时区偏移。
  if (!/([zZ]|[+-]\d{2}:?\d{2})$/.test(normalized)) {
    normalized += 'Z'
  }
  const d = new Date(normalized)
  return Number.isNaN(d.getTime()) ? null : d
}

/**
 * 把时间显示成**浏览器本地时区**的 `YYYY-MM-DD HH:mm`。
 *
 * 解析失败时退回原串截断（不隐藏数据）。
 */
export function formatLocalTime(value: unknown): string {
  const d = parseTimeValue(value)
  if (!d) {
    const text = String(value ?? '').trim()
    if (!text) return '—'
    return text.replace('T', ' ').slice(0, 16)
  }
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  )
}

/**
 * 浏览器当前时区标注（如 `UTC+8` / `UTC+5:30` / `UTC-4`）。
 *
 * 界面把它摆在时间列旁边，回答「显示的是哪个时区」—— 没有这个标注，
 * 用户会把 UTC 当本地时间读。
 */
export function localTimezoneLabel(): string {
  // getTimezoneOffset 返回的是「UTC - 本地」的分钟数（符号与直觉相反）
  const offset = -new Date().getTimezoneOffset()
  const sign = offset >= 0 ? '+' : '-'
  const abs = Math.abs(offset)
  const hours = Math.floor(abs / 60)
  const minutes = abs % 60
  return minutes ? `UTC${sign}${hours}:${pad2(minutes)}` : `UTC${sign}${hours}`
}
