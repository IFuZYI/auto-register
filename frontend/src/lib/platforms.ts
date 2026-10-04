import { apiFetch } from '@/lib/utils'

export type PlatformName = 'chatgpt' | 'grok'

/**
 * 执行器标识。
 *
 * **不是固定枚举**：各平台支持的执行器可以不同（Grok 只有 `browser`，
 * ChatGPT 只有 `protocol`），权威来源是各插件的 `BasePlatform.supported_executors`
 * （随 `/api/platforms` 下发）。这里用 `string` 而不是字面量联合 ——
 * 写死一份枚举就等于在前端复制了插件的实现细节，插件加一个执行器时
 * 前端会把它当未知值丢掉。
 */
export type ExecutorType = string

export interface PlatformMeta {
  name: PlatformName
  label: string
  color: string
  /**
   * 作为 Tag 背景色（白字压在上面）时用的色。
   *
   * 品牌色本身大多是为「深色文字/图标」设计的，直接当背景 + 白字会不达标：
   * 实测 Grok #1d9bf0 只有 3.00:1、ChatGPT #10a37f 3.20:1，都低于 WCAG AA
   * 的 4.5:1。这里给的是同色系暗一档的值，保持品牌辨识度同时过线。
   * 只用于「色块 + 白字」的场景；`color` 仍用于图标/描边这类不需要白字的地方。
   */
  tagColor: string
  /** 该平台的“注册”是否消耗外部邮箱池。iCloud 自带隐私邮箱，无需临时邮箱。 */
  usesMailbox: boolean
  /** 注册流程是否会遇到人机验证。iCloud 走 Apple 私有协议，不需要打码服务。 */
  usesCaptcha: boolean
}

/**
 * 通用执行器标签表（兜底用）。
 *
 * 平台可以声明自己的 `executor_labels` 覆盖这里的条目（Grok 的 `browser`
 * 就不在这张表里 —— 它是平台专属的）。查不到时回落到原始值。
 */
export const EXECUTOR_LABELS: Record<string, string> = {
  protocol: '纯协议',
  headless: '无头浏览器',
  headed: '有头浏览器',
}

/** 纯展示用的平台元信息（颜色/名称）。执行器能力**不在这里** —— 它来自后端。 */
export const PLATFORMS: Record<PlatformName, PlatformMeta> = {
  chatgpt: {
    name: 'chatgpt',
    label: 'ChatGPT',
    color: '#10a37f',
    tagColor: '#0b7259',
    usesMailbox: true,
    usesCaptcha: true,
  },
  grok: {
    name: 'grok',
    label: 'Grok',
    color: '#1d9bf0',
    tagColor: '#0f6cae',
    usesMailbox: true,
    usesCaptcha: true,
  },
}

export const PLATFORM_OPTIONS = Object.values(PLATFORMS).map(({ name, label }) => ({
  value: name,
  label,
}))

export const PLATFORM_FILTER_OPTIONS = [{ value: '', label: '全部平台' }, ...PLATFORM_OPTIONS]

export function getPlatformMeta(platform?: string): PlatformMeta | undefined {
  return PLATFORMS[(platform || '') as PlatformName]
}

export function getPlatformLabel(platform?: string): string {
  return getPlatformMeta(platform)?.label ?? (platform || '-')
}

export function getPlatformColor(platform?: string): string {
  return getPlatformMeta(platform)?.color ?? '#8e8e93'
}

/**
 * Tag 背景用色（白字压在上面）。
 *
 * 与 `getPlatformColor` 分开：品牌色当背景 + 白字不达标（Grok 3.00:1、
 * ChatGPT 3.20:1），这里返回同色系暗一档的值。兜底灰也一并调深
 * （#8e8e93 → #6e6e73，5.07:1），因为未知平台同样会渲染成色块 + 白字。
 */
export function getPlatformTagColor(platform?: string): string {
  return getPlatformMeta(platform)?.tagColor ?? '#6e6e73'
}

// ─────────────────────────────────────────────────────────────────────────────
// 执行器能力（来自 /api/platforms，不在前端硬编码）
// ─────────────────────────────────────────────────────────────────────────────

export interface PlatformCapabilities {
  name: string
  display_name: string
  supported_executors: ExecutorType[]
  executor_labels: Record<string, string>
}

/**
 * 进程内缓存。执行器能力在一次会话里不会变，而多个页面/组件都要用 ——
 * 每个组件各发一次请求既浪费也会造成不一致（先加载的拿到旧值）。
 */
let capabilitiesPromise: Promise<Map<string, PlatformCapabilities>> | null = null

/** 拉取并缓存各平台的能力清单（失败时返回空表，调用方回落到默认）。 */
export function loadPlatformCapabilities(): Promise<Map<string, PlatformCapabilities>> {
  if (!capabilitiesPromise) {
    capabilitiesPromise = apiFetch('/platforms')
      .then((data) => {
        const list = Array.isArray(data) ? (data as PlatformCapabilities[]) : []
        return new Map(list.map((p) => [p.name, p]))
      })
      .catch(() => {
        // 失败不缓存 —— 下次调用重试，否则一次网络抖动会让整个会话拿不到能力
        capabilitiesPromise = null
        return new Map<string, PlatformCapabilities>()
      })
  }
  return capabilitiesPromise
}

/** 已加载的能力快照（同步读）。未加载时返回空表。 */
let capabilitiesCache = new Map<string, PlatformCapabilities>()

/** 同步读已缓存的能力（未加载时 undefined）。 */
export function peekPlatformCapabilities(platform?: string): PlatformCapabilities | undefined {
  return capabilitiesCache.get(platform || '')
}

/** 供调用方在 await 之后同步取用。 */
export function rememberPlatformCapabilities(map: Map<string, PlatformCapabilities>): void {
  capabilitiesCache = map
}

/**
 * 该平台支持的执行器。**未加载时返回空数组**（不是猜一个默认值）——
 * 猜错的后果是界面显示一个运行时不支持的选项。
 */
export function getSupportedExecutors(platform?: string): ExecutorType[] {
  return peekPlatformCapabilities(platform)?.supported_executors ?? []
}

/** 执行器选项（value + label）。标签优先用平台声明的，缺失时回落通用表。 */
export function getExecutorOptions(platform?: string) {
  const caps = peekPlatformCapabilities(platform)
  const supported = caps?.supported_executors ?? []
  const labels = caps?.executor_labels ?? {}
  return supported.map((value) => ({
    value,
    label: labels[value] || EXECUTOR_LABELS[value] || value,
  }))
}

/** 单个执行器的显示名。 */
export function executorLabel(platform?: string, executor?: string): string {
  const value = String(executor || '')
  const labels = peekPlatformCapabilities(platform)?.executor_labels ?? {}
  return labels[value] || EXECUTOR_LABELS[value] || value
}

/**
 * 把执行器归一成该平台支持的取值。不受支持（含空值）时回落到**平台声明的
 * 第一个**（即平台默认）—— 与后端 `BasePlatform.__init__` 的归一逻辑一致。
 *
 * 未加载能力时原样返回：此时无法判断，交给后端归一（它有权威的声明）。
 */
export function normalizeExecutorForPlatform(platform?: string, executor?: string): ExecutorType {
  const supported = getSupportedExecutors(platform)
  if (supported.length === 0) return String(executor || '')
  const value = String(executor || '')
  return supported.includes(value) ? value : supported[0]
}
