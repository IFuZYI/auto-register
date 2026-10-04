/**
 * 口令类配置键的客户端约定。
 *
 * 服务端 `GET /api/config` **不回明文**（只回 `<key>_set` 布尔），所以前端
 * 有两件事必须一致：
 *   1. 提交前把 `<key>_set` 这些只读标记摘掉 —— 它们不是配置项；
 *   2. 口令留空表示「不修改」，空串不能提交上去（服务端也会拦，这里是第一道）。
 *
 * 抽成共享函数而不是在三个页面各写一遍：漏一处就会有页面把空串写进库，
 * 用户的口令被静默清掉（`sms_api_key` 实测踩过）。
 */

/** 服务端下发的「是否已设置」标记后缀。 */
export const SECRET_SET_SUFFIX = '_set'

/**
 * 从 `GET /api/config` 的返回值里取出「已设置口令」的键集合。
 *
 * 服务端不回明文，只回 `<key>_set`；界面用它显示「已配置」提示。
 */
export function secretSetKeysFromConfig(config: Record<string, unknown>): Set<string> {
  const keys = new Set<string>()
  for (const [key, value] of Object.entries(config)) {
    if (!key.endsWith(SECRET_SET_SUFFIX)) continue
    if (value) keys.add(key.slice(0, -SECRET_SET_SUFFIX.length))
  }
  return keys
}

/** 从表单值里摘掉只读标记（`<key>_set`），返回同一对象的浅拷贝。 */
export function stripSecretSetFlags<T extends Record<string, unknown>>(payload: T): T {
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(payload)) {
    if (key.endsWith(SECRET_SET_SUFFIX)) continue
    out[key] = value
  }
  return out as T
}

/**
 * 口令留空 = 不修改：把值为空的口令键从提交体里删掉（**原地修改**）。
 *
 * `secretKeys` 由调用方给出（各页面声明的口令字段不同）。
 *
 * 原地修改而不是返回新对象：调用方习惯写成 `dropEmptySecrets(payload, keys)`
 * 然后直接用 `payload` —— 返回新对象的话这行等于没生效（实测漏过一次，
 * 靠服务端兜底才没丢口令）。原地改让「写了就生效」不依赖调用方记得接返回值。
 */
export function dropEmptySecrets<T extends Record<string, unknown>>(
  payload: T,
  secretKeys: Iterable<string>,
): T {
  for (const key of secretKeys) {
    if (key in payload && !String(payload[key] ?? '').trim()) delete payload[key]
  }
  return payload
}
