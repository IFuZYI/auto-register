/**
 * ChatGPT「Token 方案」：一个下拉三选一。
 *
 * 后端是**两个独立的键**（`chatgpt_registration_mode` = 要不要 RT、
 * `chatgpt_rt_route` = 怎么换 RT），但用户眼里这是同一件事的一个选择。
 * 这里做纯映射：一个方案值 ↔ 后端两个键。协议契约不动，只是呈现合并。
 */

import {
  CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
  CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
  type ChatGPTRegistrationMode,
} from '@/lib/chatgptRegistrationMode'

export const TOKEN_PLAN_REFRESH_LOGIN = 'refresh_token_login'
export const TOKEN_PLAN_REFRESH_CODEX = 'refresh_token_codex'
export const TOKEN_PLAN_ACCESS_ONLY = 'access_token_only'

export type ChatGPTTokenPlan =
  | typeof TOKEN_PLAN_REFRESH_LOGIN
  | typeof TOKEN_PLAN_REFRESH_CODEX
  | typeof TOKEN_PLAN_ACCESS_ONLY

export type ChatGPTRtRoute = 'codex' | 'login'

export interface TokenPlanOption {
  value: ChatGPTTokenPlan
  label: string
  hint: string
}

/** 三个方案。顺序即界面顺序：默认项在最前。 */
export const TOKEN_PLANS: readonly TokenPlanOption[] = [
  {
    value: TOKEN_PLAN_REFRESH_LOGIN,
    label: '有 RT · 密码登录',
    hint: '产出 Access Token + Refresh Token，用账号密码 + 2FA 换取，不需要接码。'
      + '要求账号有密码与 TOTP 密钥。',
  },
  {
    value: TOKEN_PLAN_REFRESH_CODEX,
    label: '有 RT · Codex 授权',
    hint: '产出 Access Token + Refresh Token，走 Codex OAuth 授权；'
      + '撞上 add-phone 时需要接码平台才能过。',
  },
  {
    value: TOKEN_PLAN_ACCESS_ONLY,
    label: '无 RT',
    hint: '只产出 Access Token / Session，依赖 RT 的能力可能不可用。',
  },
] as const

export const DEFAULT_TOKEN_PLAN: ChatGPTTokenPlan = TOKEN_PLAN_REFRESH_LOGIN

export function normalizeChatGPTTokenPlan(value: unknown): ChatGPTTokenPlan {
  const found = TOKEN_PLANS.find((p) => p.value === value)
  if (found) return found.value
  // 兼容旧值：老 localStorage 里存的是两态开关的 mode
  if (value === CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY) return TOKEN_PLAN_ACCESS_ONLY
  if (value === CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN) return TOKEN_PLAN_REFRESH_LOGIN
  return DEFAULT_TOKEN_PLAN
}

/** 由后端两个键反推方案（设置回填用）。 */
export function tokenPlanOf(input: {
  mode?: ChatGPTRegistrationMode | string
  route?: ChatGPTRtRoute | string
}): ChatGPTTokenPlan {
  const mode = input.mode
  if (mode === CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY) return TOKEN_PLAN_ACCESS_ONLY
  // 无 RT 时路线无意义，忽略它
  if (input.route === 'codex') return TOKEN_PLAN_REFRESH_CODEX
  return TOKEN_PLAN_REFRESH_LOGIN
}

/** 方案 → 要写进任务 extra 的键。 */
export function tokenPlanExtra(plan: ChatGPTTokenPlan): Record<string, unknown> {
  if (plan === TOKEN_PLAN_ACCESS_ONLY) {
    return {
      chatgpt_registration_mode: CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
      chatgpt_has_refresh_token_solution: false,
    }
  }
  return {
    chatgpt_registration_mode: CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
    chatgpt_rt_route: plan === TOKEN_PLAN_REFRESH_CODEX ? 'codex' : 'login',
    chatgpt_has_refresh_token_solution: true,
  }
}

export function tokenPlanLabel(plan: ChatGPTTokenPlan): string {
  return TOKEN_PLANS.find((p) => p.value === plan)?.label ?? plan
}

export function tokenPlanHint(plan: ChatGPTTokenPlan): string {
  return TOKEN_PLANS.find((p) => p.value === plan)?.hint ?? ''
}

export const CHATGPT_TOKEN_PLAN_STORAGE_KEY = 'chatgpt-token-plan'

export function loadChatGPTTokenPlan(): ChatGPTTokenPlan {
  if (typeof window === 'undefined') return DEFAULT_TOKEN_PLAN
  return normalizeChatGPTTokenPlan(
    window.localStorage.getItem(CHATGPT_TOKEN_PLAN_STORAGE_KEY),
  )
}

export function saveChatGPTTokenPlan(plan: ChatGPTTokenPlan): void {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(CHATGPT_TOKEN_PLAN_STORAGE_KEY, plan)
}
