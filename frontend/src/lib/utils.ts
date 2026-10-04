export const API = '/api'
export const API_BASE = '/api'

export function getToken(): string {
  return localStorage.getItem('auth_token') || ''
}

export function setToken(token: string): void {
  localStorage.setItem('auth_token', token)
}

export function clearToken(): void {
  localStorage.removeItem('auth_token')
}

// 后端只在"面板登录态失效"时带上这个头。业务接口也可能回 401（比如上游服务不认
// 你提交的凭据），那种情况只该把错误显示出来，不能把人踢回登录页。
export const PANEL_AUTH_HEADER = 'X-Panel-Auth-Required'

export async function apiFetch(path: string, opts?: RequestInit) {
  const token = getToken()
  const baseHeaders: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) baseHeaders['Authorization'] = `Bearer ${token}`
  const res = await fetch(API + path, {
    ...opts,
    headers: { ...baseHeaders, ...(opts?.headers as Record<string, string> || {}) },
  })
  if (res.status === 401 && res.headers.get(PANEL_AUTH_HEADER)) {
    clearToken()
    if (window.location.pathname !== '/login') {
      window.location.href = '/login'
    }
    throw new Error('未认证，请重新登录')
  }
  if (!res.ok) {
    const text = await res.text()
    try {
      const json = JSON.parse(text)
      throw new Error(json.detail || text)
    } catch (e) {
      if (e instanceof SyntaxError) throw new Error(text)
      throw e
    }
  }
  return res.json()
}
