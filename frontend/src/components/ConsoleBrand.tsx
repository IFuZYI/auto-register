/** 账号从多个邮箱汇入注册链，再交付到面板；这是产品本身的工作方式。 */
export function ConsoleBrand({ compact = false }: { compact?: boolean }) {
  return (
    <>
      <div className="sider-logo__mark" aria-hidden="true">
        <svg viewBox="0 0 32 32" fill="none">
          <path d="M7 9h7l4 7h7M7 23h7l4-7h7" stroke="currentColor" strokeWidth="2" />
          <rect x="4" y="6" width="6" height="6" rx="2" fill="currentColor" />
          <rect x="4" y="20" width="6" height="6" rx="2" fill="currentColor" />
          <rect x="22" y="13" width="6" height="6" rx="2" fill="currentColor" />
        </svg>
      </div>
      <div className={`sider-logo__text${compact ? ' sider-logo__text--hidden' : ''}`}>
        <span className="sider-logo__name">账号管理台</span>
        <span className="sider-logo__caption">注册、维护与面板对接</span>
      </div>
    </>
  )
}
