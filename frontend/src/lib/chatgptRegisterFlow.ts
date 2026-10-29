export const CHATGPT_REGISTER_FLOW_EMAIL = 'email'
export const CHATGPT_REGISTER_FLOW_STORAGE_KEY = 'chatgpt-register-flow'

/**
 * 注册身份只剩邮箱 —— 手机号注册已删除（见 docs/adr/0007-email-only-registration.md）。
 *
 * 类型收成单字面量而不是 `string`：旧的 `'phone'` / `'phone_with_email'`
 * 会在编译期报错，而不是静默落到默认值。存量任务提交旧值时由
 * `normalizeChatGPTRegisterFlow` 归一成邮箱。
 */
export type ChatGPTRegisterFlow = typeof CHATGPT_REGISTER_FLOW_EMAIL

export const DEFAULT_CHATGPT_REGISTER_FLOW: ChatGPTRegisterFlow =
  CHATGPT_REGISTER_FLOW_EMAIL

export const CHATGPT_REGISTER_FLOW_OPTIONS: {
  value: ChatGPTRegisterFlow
  label: string
  hint: string
}[] = [
  {
    value: CHATGPT_REGISTER_FLOW_EMAIL,
    label: '邮箱注册',
    hint: '用邮箱池里的地址注册，收邮件验证码完成验证。手机号只在 Codex 授权要求绑定时才会用到。',
  },
]

export function normalizeChatGPTRegisterFlow(
  _value: unknown,
): ChatGPTRegisterFlow {
  return DEFAULT_CHATGPT_REGISTER_FLOW
}

export function loadChatGPTRegisterFlow(): ChatGPTRegisterFlow {
  if (typeof window === 'undefined') return DEFAULT_CHATGPT_REGISTER_FLOW
  return normalizeChatGPTRegisterFlow(
    window.localStorage.getItem(CHATGPT_REGISTER_FLOW_STORAGE_KEY),
  )
}

export function saveChatGPTRegisterFlow(flow: ChatGPTRegisterFlow): void {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(CHATGPT_REGISTER_FLOW_STORAGE_KEY, flow)
}
