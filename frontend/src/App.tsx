import { BrowserRouter, Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useState, useEffect, useRef } from 'react'
import { App as AntdApp, ConfigProvider, Layout, Menu, Button, Spin, Tooltip, Drawer, Grid } from 'antd'
import {
  DashboardOutlined,
  UserOutlined,
  GlobalOutlined,
  HistoryOutlined,
  SettingOutlined,
  MailOutlined,
  SunOutlined,
  MoonOutlined,
  LogoutOutlined,
  PlayCircleOutlined,
  ThunderboltFilled,
  AppstoreOutlined,
} from '@ant-design/icons'
import zhCN from 'antd/es/locale/zh_CN'
import Dashboard from '@/pages/Dashboard'
import Accounts from '@/pages/Accounts'
import RegisterTaskPage from '@/pages/RegisterTaskPage'
import Proxies from '@/pages/Proxies'
import Settings from '@/pages/Settings'
import MailICloudLocalPage from '@/pages/mail/MailICloudLocalPage'
import MailOutlookPage from '@/pages/mail/MailOutlookPage'
import PanelManagement from '@/pages/PanelManagement'
import TaskHistory from '@/pages/TaskHistory'
import RunningTasks from '@/pages/RunningTasks'
import Login from '@/pages/Login'
import { applyThemeVars, darkTheme, lightTheme } from './theme'
import { apiFetch, clearToken, getToken } from '@/lib/utils'
import { MobileNavContext } from '@/hooks/useMobileNav'

const { Sider, Content } = Layout

/** 侧栏宽度（展开 / 折叠），抽屉与常驻侧栏共用展开值。 */
const SIDER_WIDTH = 232
const SIDER_COLLAPSED_WIDTH = 72

/** 移动端抽屉宽度：上限 280px，且至少给视口留 48px 露出背后的页面。 */
const DRAWER_MAX_WIDTH = 280
const DRAWER_VIEWPORT_GUTTER = 48

/**
 * 历史路径 → 现在该高亮的菜单项。
 * /icloud 并入了「邮箱服务 > iCloud 隐私邮箱（本地）」，保留旧路径只为兼容
 * 老书签，选中态归到新位置。
 */
const SELECTED_KEY_ALIASES: Record<string, string> = {
  '/icloud': '/mail/icloud',
  // 「平台配置」已并入「全局配置 → 面板配置」：旧路径重定向到这里，
  // 选中态归到「全局配置」。
  '/platform-config': '/settings',
}

/** 菜单里真实存在的一级 key（路径与 key 同名）。 */
const MENU_KEYS: ReadonlySet<string> = new Set([
  '/',
  '/accounts',
  '/mail',
  '/history',
  '/panel-management',
  '/proxies',
  '/settings',
  '/running-tasks',
])

/**
 * 路径 → 侧栏选中项。
 *
 * 用查表替代原先 16 个 if（其中 7 个是「路径与 key 同名」的样板）：
 * 现在加一级路由只要往 MENU_KEYS 里补一个名字，不用再抄一行判断。
 */
function getSelectedKey(pathname: string): string[] {
  const key = SELECTED_KEY_ALIASES[pathname] ?? pathname
  if (MENU_KEYS.has(key)) return [key]
  // 子路径：同时选中自己与父项。
  //
  // 只给子 key 的话，父项高亮依赖 rc-menu 内部「子项注册路径 → 重算」的时序，
  // 而子项是异步来的（面板列表 / 平台列表），首帧还不存在 → 硬刷新后父项一直
  // 是灰的（实测；`/mail` 的子项是写死的所以没这毛病）。把父 key 一起选上，
  // 父项自己挂载时就注册了路径，不再依赖子项何时到。
  if (key.startsWith('/accounts/') || key.startsWith('/mail/') || key.startsWith('/panel-management/')) {
    return [key.slice(0, key.indexOf('/', 1)), key]
  }
  return ['/']
}

/**
 * 未匹配路由的兜底页。
 *
 * 实测（dogfood 2026-10-04）：访问未知路径时内容区完全空白（30 字节），
 * 只有侧栏 —— 用户分不清「页面不存在」还是「界面崩了」。这里给出明确
 * 提示 + 回首页的入口。
 */
function NotFoundPage() {
  return (
    <div style={{ maxWidth: 480, margin: '80px auto 0', textAlign: 'center' }}>
      <h1 style={{ fontSize: 56, margin: 0, color: 'var(--text-muted)' }}>404</h1>
      <p style={{ marginTop: 12, fontSize: 15 }}>页面不存在</p>
      <p style={{ color: 'var(--text-muted)', fontSize: 13 }}>
        地址可能已变更或输入有误。从左侧菜单选一个页面，或回到仪表盘。
      </p>
      <Button type="primary" onClick={() => { window.location.href = '/' }} style={{ marginTop: 8 }}>
        回到仪表盘
      </Button>
    </div>
  )
}

function ProtectedLayout() {
  const navigate = useNavigate()
  const [ready, setReady] = useState(false)

  useEffect(() => {
    fetch('/api/auth/status')
      .then(r => r.json())
      .then(s => {
        const token = getToken()
        if (s.has_password && !token) {
          navigate('/login', { replace: true })
        } else {
          setReady(true)
        }
      })
      .catch(() => setReady(true))
  }, [])

  if (!ready) {
    return (
      <div style={{ minHeight: 'var(--app-vh)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Spin size="large" />
      </div>
    )
  }

  return <AppContent />
}

function AppContent() {
  const [themeMode, setThemeMode] = useState<'dark' | 'light'>(() =>
    (localStorage.getItem('theme') as 'dark' | 'light') || 'dark'
  )
  const [collapsed, setCollapsed] = useState(false)
  const [navOpen, setNavOpen] = useState(false)
  const [platforms, setPlatforms] = useState<{ key: string; label: string }[]>([])
  const [panels, setPanels] = useState<{ key: string; label: string }[]>([])
  const [hasPassword, setHasPassword] = useState(false)
  const location = useLocation()
  const navigate = useNavigate()
  const contentRef = useRef<HTMLDivElement>(null)
  const screens = Grid.useBreakpoint()
  // <992px（lg 以下）用抽屉导航。
  // 卡在 768 而不是 992 试过：iPad 竖屏下 232px 侧栏把内容区压到 536px，
  // 面板卡片网格只剩一列（472px），既稀疏又浪费。
  // 992 以上侧栏才真正有地方待。
  const isMobile = screens.lg === false

  useEffect(() => {
    applyThemeVars(themeMode)
    localStorage.setItem('theme', themeMode)
  }, [themeMode])

  useEffect(() => {
    fetch('/api/auth/status').then(r => r.json()).then(s => setHasPassword(s.has_password)).catch(() => {})
  }, [])

  useEffect(() => {
    apiFetch('/platforms')
      .then(d => setPlatforms((d || []).map((p: any) => ({ key: p.name, label: p.display_name }))))
      .catch(() => {})
  }, [])

  // 面板列表用于侧栏二级项。取不到就只留一级入口（页面本身仍是可用的）——
  // 菜单不该因为一个接口失败而消失。
  useEffect(() => {
    apiFetch('/integrations/panels')
      .then(d => setPanels(
        ((d?.items || []) as { key: string; label: string }[])
          .map(p => ({ key: p.key, label: p.label })),
      ))
      .catch(() => {})
  }, [])

  // 切路由时把内容区滚回顶部：苹果式页面切换不应该停在半路。
  // 顺手关掉移动端抽屉 —— 点完菜单项它就该收起来，而不是盖在新页面上。
  useEffect(() => {
    contentRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
    setNavOpen(false)
  }, [location.pathname])

  // 视口跨过 lg 断点时收起抽屉。
  //
  // 不这么做的话：在 <992px 打开抽屉 → 拉宽到 ≥992（抽屉被 isMobile 隐藏，
  // 但 navOpen 仍是 true）→ 再缩回 <992，抽屉会自行弹开，看起来像界面
  // 自己乱动（平板竖屏↔横屏、桌面窗口缩放都能复现）。
  //
  // 用「渲染期调整 state」而不是 useEffect：这是在响应 prop/派生值变化，
  // 不是同步外部系统 —— React 官方推荐这个写法，且不会触发
  // react-hooks/set-state-in-effect（同步 setState 的 effect 会多跑一轮渲染）。
  const [prevIsMobile, setPrevIsMobile] = useState(isMobile)
  if (prevIsMobile !== isMobile) {
    setPrevIsMobile(isMobile)
    if (!isMobile) setNavOpen(false)
  }

  /**
   * 横向滚动的表格区域要能用键盘操作（WCAG 2.1.1）。
   * antd 的 Table 生成的是 `overflow: auto` 的 div，本身不可聚焦 ——
   * 键盘用户没法把右侧被裁掉的列滚出来。这里统一给「确实溢出」的
   * 滚动区补上 tabindex，让它 Tab 得到、方向键滚得动。
   * 用 MutationObserver 是因为表格数据是异步来的，一次性扫描会漏。
   *
   * 另外要监听 resize：视口变化会改变溢出状态，而那**不产生 childList
   * 变更**（实测 1000px→2000px 后表格不再溢出，tabindex 却还留着 ——
   * 多出一个「按了没反应」的 Tab 停靠点）。用 ResizeObserver 盯内容容器
   * 比监听 window 更准：侧栏折叠、抽屉开合也会改变内容区宽度。
   */
  useEffect(() => {
    const patch = () => {
      document.querySelectorAll<HTMLElement>('.ant-table-content, .ant-table-body').forEach(el => {
        const overflows = el.scrollWidth > el.clientWidth + 2
        if (overflows && !el.hasAttribute('tabindex')) {
          el.setAttribute('tabindex', '0')
          el.setAttribute('role', 'region')
          el.setAttribute('aria-label', '可横向滚动的表格')
        } else if (!overflows && el.hasAttribute('tabindex')) {
          // 表格数据变了之后可能不再溢出（例如筛选后列变少）。
          // 留着 tabindex 会多出一个「按了没反应」的 Tab 停靠点 —— 双向同步。
          el.removeAttribute('tabindex')
          el.removeAttribute('role')
          el.removeAttribute('aria-label')
        }
      })
    }
    patch()

    // 合并到每帧一次：注册表/任务列表这类页面会成片插入 DOM，
    // 逐条回调跑 patch 等于对整页表格反复做同一遍查询。
    let frame = 0
    const schedule = () => {
      if (frame) return
      frame = window.requestAnimationFrame(() => {
        frame = 0
        patch()
      })
    }
    const mo = new MutationObserver(schedule)
    mo.observe(document.body, { childList: true, subtree: true })

    // 尺寸变化（视口缩放、侧栏折叠、抽屉开合）不产生 childList 变更，
    // 需要单独触发一次重算。
    const content = contentRef.current
    const ro = content ? new ResizeObserver(schedule) : null
    if (ro && content) ro.observe(content)
    window.addEventListener('resize', schedule)

    return () => {
      if (frame) window.cancelAnimationFrame(frame)
      mo.disconnect()
      ro?.disconnect()
      window.removeEventListener('resize', schedule)
    }
  }, [])

  const isLight = themeMode === 'light'
  const currentTheme = isLight ? lightTheme : darkTheme

  const menuItems = [
    {
      key: '/',
      icon: <DashboardOutlined />,
      label: '仪表盘',
    },
    {
      key: '/running-tasks',
      icon: <PlayCircleOutlined />,
      label: '任务运行',
    },
    {
      key: '/accounts',
      icon: <UserOutlined />,
      label: '平台管理',
      // iCloud 不在这里 —— 它的控制台（主号 + 隐私邮箱别名）是「邮箱服务」下的
      // 一个二级页（/mail/icloud）。iCloud 在 `uses_mailbox: False` 的平台里
      // 是个特例：它本身就是邮箱来源，管理界面跟其它平台的通用账号列表不是一回事。
      children: platforms
        .filter(p => p.key !== 'icloud')
        .map(p => ({
          key: `/accounts/${p.key}`,
          label: p.label,
        })),
    },
    {
      // 邮箱服务提到一级：它不再是「全局配置里的一个 tab」，而是一组页面。
      // 二级按「邮箱从哪来」分：本地自维护的号池。两个页面都是「先在面板里
      // 维护一份号，注册时按 provider 取号」的同一类东西。
      //
      // 历史上这里还有「临时邮箱」（一次性地址）与「远程 icloud-hme」两个
      // 二级页，按用户要求整体删除。
      key: '/mail',
      icon: <MailOutlined />,
      label: '邮箱服务',
      children: [
        { key: '/mail/icloud', label: 'iCloud 隐私邮箱（本地）' },
        { key: '/mail/outlook', label: 'Outlook（本地）' },
      ],
    },
    {
      key: '/panel-management',
      icon: <AppstoreOutlined />,
      label: '面板管理',
      // 二级按面板分（CPA 面板 / Sub2API / grok2api），与「平台管理」按平台分
      // 是同一种结构：一级是入口，二级是具体目标。面板列表由
      // `/api/integrations/panels` 下发，不硬编码 —— 注册表加面板这里自动多一项。
      children: panels.map(p => ({
        key: `/panel-management/${p.key}`,
        label: p.label,
      })),
    },
    {
      key: '/proxies',
      icon: <GlobalOutlined />,
      label: '代理管理',
    },
    {
      key: '/history',
      icon: <HistoryOutlined />,
      label: '任务历史',
    },
    {
      key: '/settings',
      icon: <SettingOutlined />,
      label: '全局配置',
    },
  ]

  /**
   * 导航内容（侧栏与移动端抽屉共用同一份，避免两处各写一遍菜单）。
   *
   * `inDrawer` 只影响一件事：抽屉里永远有足够宽度，所以即便侧栏处于
   * 折叠态也要显示完整文案。把「折叠 且 不在抽屉里」先归约成一个语义量，
   * 后面 6 处引用它而不是各自重写一遍复合条件。
   */
  const navContent = (inDrawer: boolean) => {
    const compact = collapsed && !inDrawer
    const align = compact ? 'center' : 'flex-start'
    const label = (text: string) => (compact ? '' : text)

    return (
      <>
        <div className="sider-logo" style={{ justifyContent: align }}>
          <div className="sider-logo__mark">
            <ThunderboltFilled />
          </div>
          <span className={`sider-logo__text${compact ? ' sider-logo__text--hidden' : ''}`}>
            账号管理台
          </span>
        </div>
        <nav aria-label="主导航" className="sider-menu">
          <Menu
            mode="inline"
            selectedKeys={getSelectedKey(location.pathname)}
            defaultOpenKeys={['/accounts', '/mail', '/panel-management']}
            items={menuItems}
            onClick={({ key }) => navigate(key)}
            // 子项缩进从默认 24 收到 16：232px 侧栏里
            // 「iCloud 隐私邮箱（本地）」只差 2px 被省略号截掉，
            // 收窄缩进比加宽侧栏更划算（后者会挤压内容区）
            inlineIndent={16}
            style={{
              borderRight: 0,
              background: 'transparent',
            }}
          />
        </nav>
        <div className="sider-footer">
          <Tooltip title={compact ? (isLight ? '切换到暗色' : '切换到亮色') : ''} placement="right">
            <Button
              block
              type="text"
              icon={isLight ? <MoonOutlined /> : <SunOutlined />}
              onClick={() => setThemeMode(isLight ? 'dark' : 'light')}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: align,
                gap: 10,
                color: 'var(--text-secondary)',
              }}
            >
              {label(isLight ? '切换到暗色' : '切换到亮色')}
            </Button>
          </Tooltip>
          {hasPassword && (
            <Tooltip title={compact ? '退出登录' : ''} placement="right">
              <Button
                block
                type="text"
                danger
                icon={<LogoutOutlined />}
                onClick={() => { clearToken(); navigate('/login') }}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: align,
                  gap: 10,
                }}
              >
                {label('退出登录')}
              </Button>
            </Tooltip>
          )}
        </div>
      </>
    )
  }

  return (
    <ConfigProvider theme={currentTheme} locale={zhCN}>
      <AntdApp>
        {/* 固定整屏高度（而非 min-height）：让内容区成为唯一的滚动容器，
            这样 PageHeader 的 sticky 吸附与滚动分隔线才能生效。
            用 --app-vh 而非 100vh —— 全站 zoom 0.9 下 100vh 会矮 10%
            （见 index.css 的 --app-vh 注释）。 */}
        <Layout style={{ height: 'var(--app-vh)', overflow: 'hidden', background: 'var(--bg-layout)' }}>
          {/* 跳到主内容：键盘用户不必逐个 Tab 过整条菜单 */}
          <a href="#main-content" className="skip-link">跳到主内容</a>

          {/* 常驻侧栏只在 ≥992px 出现；窄屏走下面的抽屉
              （阈值与 PageHeader 的汉堡、index.css 的 @media 一致） */}
          {!isMobile && (
            <Sider
              collapsible
              collapsed={collapsed}
              onCollapse={setCollapsed}
              className="sider-shell"
              width={SIDER_WIDTH}
              collapsedWidth={SIDER_COLLAPSED_WIDTH}
            >
              {navContent(false)}
            </Sider>
          )}

          <Content
            ref={contentRef}
            id="main-content"
            tabIndex={-1}
            style={{
              padding: 'var(--page-pad-y) var(--page-pad-x) 40px',
              overflow: 'auto',
              background: 'var(--bg-layout)',
            }}
          >
            <div key={location.pathname} className="page-enter">
              <MobileNavContext.Provider value={{ open: () => setNavOpen(true) }}>
                <Routes>
                  <Route path="/" element={<Dashboard />} />
                  <Route path="/accounts" element={<Accounts />} />
                  <Route path="/accounts/:platform" element={<Accounts />} />
                  <Route path="/register" element={<RegisterTaskPage />} />
                  {/* iCloud 控制台搬到「邮箱服务」下了。旧路径重定向，老书签不 404 */}
                  <Route path="/icloud" element={<Navigate to="/mail/icloud" replace />} />
                  <Route path="/running-tasks" element={<RunningTasks />} />
                  <Route path="/history" element={<TaskHistory />} />
                  <Route path="/panel-management" element={<PanelManagement />} />
                  <Route path="/panel-management/:panelKey" element={<PanelManagement />} />
                  {/* 「平台配置」已并入「全局配置 → 面板配置」。旧路径重定向，
                      老书签不 404；`?tab=panel` 让重定向直接落到那一栏。 */}
                  <Route
                    path="/platform-config"
                    element={<Navigate to="/settings?tab=panel" replace />}
                  />
                  <Route path="/proxies" element={<Proxies />} />
                  <Route path="/settings" element={<Settings />} />
                  {/* 邮箱服务（一级菜单）：二级各自是独立页面。
                      「默认邮箱服务」已并入「全局配置 > 邮箱」——它是全局默认值，
                      和各 provider 的连接参数不是一回事。旧路径重定向，老书签不 404。
                      「临时邮箱」与「远程 icloud-hme」两个页面已删除，旧路径同样
                      重定向到邮箱服务下的 iCloud 页，免得老书签落到空白路由。 */}
                  <Route path="/mail/default" element={<Navigate to="/settings" replace />} />
                  <Route path="/mail/icloud" element={<MailICloudLocalPage />} />
                  <Route path="/mail/outlook" element={<MailOutlookPage />} />
                  <Route path="/mail/temp" element={<Navigate to="/mail/icloud" replace />} />
                  <Route path="/mail/remote-hme" element={<Navigate to="/mail/icloud" replace />} />
                  {/* 兜底：未知路径不能是空白 —— 实测内容区 30 字节，
                      用户分不清「页面不存在」与「加载失败」。 */}
                  <Route path="*" element={<NotFoundPage />} />
                </Routes>
              </MobileNavContext.Provider>
            </div>
          </Content>
        </Layout>

        {/* 移动端导航抽屉：宽度按视口收，避免在窄机上占满整屏 */}
        <Drawer
          placement="left"
          open={isMobile && navOpen}
          onClose={() => setNavOpen(false)}
          width={Math.min(
            DRAWER_MAX_WIDTH,
            typeof window !== 'undefined' ? window.innerWidth - DRAWER_VIEWPORT_GUTTER : DRAWER_MAX_WIDTH,
          )}
          closable={false}
          styles={{ body: { padding: 0, display: 'flex', flexDirection: 'column' } }}
          className="mobile-nav-drawer"
        >
          {navContent(true)}
        </Drawer>
      </AntdApp>
    </ConfigProvider>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/*" element={<ProtectedLayout />} />
      </Routes>
    </BrowserRouter>
  )
}
