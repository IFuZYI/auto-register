import { useEffect, useState, type ReactNode } from 'react'
import { Button, Grid } from 'antd'
import { MenuOutlined } from '@ant-design/icons'
import { useMobileNav } from '@/hooks/useMobileNav'

interface PageHeaderProps {
  title: string
  subtitle?: ReactNode
  /** 右侧操作区（按钮等） */
  actions?: ReactNode
}

/**
 * 页面标题区：滚动时毛玻璃吸附在顶部，分隔线在滚动后才出现。
 *
 * 抽出来是为了让各页面的标题排版一致 —— 之前每个页面各写一份
 * `<h1>` + 内联样式，字号与间距都对不齐。
 *
 * 窄屏（<992px）时左侧多一个汉堡按钮唤出导航抽屉：那时常驻侧栏已收起，
 * 没有它就没法切页。
 */
export function PageHeader({ title, subtitle, actions }: PageHeaderProps) {
  const [scrolled, setScrolled] = useState(false)
  const screens = Grid.useBreakpoint()
  // 与 App.tsx 的抽屉阈值保持一致（lg 以下走抽屉）。
  // 曾经这里写 md：768-991px 区间侧栏已被隐藏、汉堡却还不显示，
  // 导航在那一段宽度里完全不可达。
  const isMobile = screens.lg === false
  const mobileNav = useMobileNav()

  useEffect(() => {
    // 内容区是滚动容器（App.tsx 的 Content），不是 window
    const scroller = document.querySelector('.ant-layout-content')
    if (!scroller) return

    const onScroll = () => setScrolled(scroller.scrollTop > 8)
    onScroll()
    scroller.addEventListener('scroll', onScroll, { passive: true })
    return () => scroller.removeEventListener('scroll', onScroll)
  }, [])

  return (
    <header className={`page-header${scrolled ? ' page-header--scrolled' : ''}`}>
      <div style={{ minWidth: 0, display: 'flex', alignItems: 'flex-start', gap: isMobile ? 10 : 0 }}>
        {isMobile && (
          <Button
            type="text"
            icon={<MenuOutlined />}
            onClick={mobileNav.open}
            aria-label="打开导航菜单"
            style={{ marginTop: -2, flex: '0 0 auto' }}
          />
        )}
        <div style={{ minWidth: 0 }}>
          <h1 className="page-header__title">{title}</h1>
          {subtitle && <div className="page-header__subtitle">{subtitle}</div>}
        </div>
      </div>
      {actions && (
        <div className="page-header__actions">{actions}</div>
      )}
    </header>
  )
}

export default PageHeader
