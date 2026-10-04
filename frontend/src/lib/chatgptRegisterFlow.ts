export const CHATGPT_REGISTER_FLOW_EMAIL = 'email'
export const CHATGPT_REGISTER_FLOW_PHONE = 'phone'
export const CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL = 'phone_with_email'
export const CHATGPT_REGISTER_FLOW_STORAGE_KEY = 'chatgpt-register-flow'

export type ChatGPTRegisterFlow =
  | typeof CHATGPT_REGISTER_FLOW_EMAIL
  | typeof CHATGPT_REGISTER_FLOW_PHONE
  | typeof CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL

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
    hint: '用邮箱池里的地址注册，收邮件验证码完成验证。',
  },
  {
    value: CHATGPT_REGISTER_FLOW_PHONE,
    label: '手机注册',
    hint: '用接码平台的号码注册，收短信验证码完成验证，不占用邮箱。',
  },
  {
    value: CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL,
    label: '手机注册 + 绑定邮箱',
    hint: '先用号码注册，再把邮箱池里的地址绑到账号上，收一次邮件验证码。',
  },
]

export function normalizeChatGPTRegisterFlow(
  value: unknown,
): ChatGPTRegisterFlow {
  if (value === CHATGPT_REGISTER_FLOW_PHONE) return CHATGPT_REGISTER_FLOW_PHONE
  if (value === CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL) {
    return CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL
  }
  return DEFAULT_CHATGPT_REGISTER_FLOW
}

export function registerFlowUsesPhone(flow: ChatGPTRegisterFlow): boolean {
  return flow !== CHATGPT_REGISTER_FLOW_EMAIL
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
