export const CHATGPT_BIND_2FA_STORAGE_KEY = 'chatgpt-bind-2fa'

// 默认关：绑上之后这个号每次登录都要动态码，而密钥只在注册那一刻下发一次。
export const DEFAULT_CHATGPT_BIND_2FA = false

export function loadChatGPTBind2fa(): boolean {
  if (typeof window === 'undefined') return DEFAULT_CHATGPT_BIND_2FA
  return window.localStorage.getItem(CHATGPT_BIND_2FA_STORAGE_KEY) === '1'
}

export function saveChatGPTBind2fa(enabled: boolean): void {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(CHATGPT_BIND_2FA_STORAGE_KEY, enabled ? '1' : '0')
}
