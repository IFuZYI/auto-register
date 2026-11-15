import { theme } from 'antd'

export type ThemeMode = 'dark' | 'light'

/**
 * 动效曲线。苹果的界面动效普遍是「起步快、收尾稳」，
 * 这条曲线接近系统转场手感，比默认的 ease 更顺。
 */
export const EASE_STANDARD = 'cubic-bezier(0.4, 0, 0.2, 1)'
export const EASE_OUT = 'cubic-bezier(0.16, 1, 0.3, 1)'

/** 中文优先使用清晰的人文黑体；数字与英文沿用同一字体，避免界面像两套产品拼接。 */
const FONT_STACK = [
  '-apple-system',
  'BlinkMacSystemFont',
  '"SF Pro Text"',
  '"Noto Sans CJK SC"',
  '"Source Han Sans SC"',
  '"PingFang SC"',
  '"Microsoft YaHei"',
  'sans-serif',
].join(', ')

/**
 * 界面用色统一在这里定义：antd token 与 CSS 变量共用同一份，
 * 页面里的内联样式一律写 var(--x)，切主题时不用改组件。
 */
interface Palette {
  bgLayout: string
  bgContainer: string
  bgElevated: string
  bgSubtle: string
  border: string
  borderStrong: string
  text: string
  textSecondary: string
  textMuted: string
  /** 占位符/禁用文字（带 alpha）。antd 默认 0.25 alpha 在深色底上只有 2.19:1。 */
  textQuaternary: string
  accent: string
  accentHover: string
  accentSoft: string
  /** 强调色当**文字**用（链接/选中/processing）：必须过 AA，见下方注释。 */
  accentText: string
  /** 紫色（2FA 已绑这类标记）。preset 的派生值不过 AA，见 index.css 的钉法。 */
  purple: string
  purpleSoft: string
  /** 青色（任务类型「自动刷新」标签）。同上，preset 派生值不过 AA。 */
  cyan: string
  cyanSoft: string
  /** 品红（任务类型「凭证同步」标签）。 */
  magenta: string
  magentaSoft: string
  success: string
  successSoft: string
  warning: string
  warningSoft: string
  danger: string
  dangerSoft: string
  logBg: string
  logBorder: string
  logText: string
  logMuted: string
  logSuccess: string
  logWarning: string
  logDanger: string
  selectionBg: string
  spotlightBg: string
  // ── 层次感相关（阴影 / 毛玻璃 / 渐变）────────────────────
  shadowSm: string
  shadowMd: string
  shadowLg: string
  glassBg: string
  glassBorder: string
  accentRing: string
  gradientFrom: string
  gradientTo: string
  sidebarBg: string
  sidebarText: string
  sidebarMuted: string
  sidebarBorder: string
  sidebarSelected: string
}

/** 暗色：低反差的苔绿灰阶。导航与工作区同族，但明度层级仍然明确。 */
const darkPalette: Palette = {
  bgLayout: '#111714',
  bgContainer: '#1b2420',
  bgElevated: '#25302b',
  bgSubtle: '#26312c',
  border: '#34423b',
  borderStrong: '#52645a',
  text: '#edf2ee',
  textSecondary: '#b2c0b7',
  /* 辅助文字在卡片与浮层上保持可读，不与主内容争夺注意力。 */
  textMuted: '#9eaea4',
  textQuaternary: 'rgba(237, 242, 238, 0.60)',
  accent: '#367d67',
  accentHover: '#367d67',
  accentSoft: '#29463a',
  /* 强调色作为**文字**时的取值（链接、菜单选中、Tab 选中、processing 标签）。
     与 `accent` 分开是因为用途不同：accent 多用于填充/描边（白字压在上面），
     而这一支是文字本身，必须压在各种浅色合成底上过 AA。

     两套主题分别选择浅青与深青，让文字在选中底与容器底上保持清晰。 */
  accentText: '#8ed0b5',
  purple: '#c9a6ff',
  purpleSoft: 'rgba(201,166,255,0.15)',
  /* 青色用于自动刷新任务，与主操作色保持区分。 */
  cyan: '#64d2ff',
  cyanSoft: 'rgba(100,210,255,0.15)',
  /* 品红用于凭据流转类任务，不用于装饰。 */
  magenta: '#ff9ecf',
  magentaSoft: 'rgba(255,158,207,0.15)',
  success: '#8ac99e',
  successSoft: '#294333',
  warning: '#ddb36d',
  warningSoft: 'rgba(255,159,10,0.15)',
  /* 危险色使用足够亮的暖红，保证删除与错误信息在深色底上可读。 */
  danger: '#ff7a70',
  dangerSoft: 'rgba(255,69,58,0.15)',
  logBg: '#101713',
  logBorder: '#34423b',
  logText: '#d6dfd8',
  logMuted: '#9eaea4',
  logSuccess: '#8ac99e',
  logWarning: '#ddb36d',
  logDanger: '#ff9b91',
  selectionBg: 'rgba(142,208,181,0.28)',
  spotlightBg: '#3a3a3c',
  shadowSm: '0 1px 2px rgba(0,0,0,0.16)',
  shadowMd: '0 12px 32px rgba(0,0,0,0.24)',
  shadowLg: '0 24px 64px rgba(0,0,0,0.36)',
  glassBg: 'rgba(27,36,32,0.94)',
  glassBorder: '#34423b',
  accentRing: 'rgba(142,208,181,0.42)',
  gradientFrom: '#367d67',
  gradientTo: '#367d67',
  sidebarBg: '#142e26',
  sidebarText: '#edf4ee',
  sidebarMuted: '#aec2b5',
  sidebarBorder: '#334c41',
  sidebarSelected: '#2a4c3e',
}

/** 亮色：冷白纸面 + 深青操作色，以细边界而不是浮起阴影组织信息。 */
const lightPalette: Palette = {
  bgLayout: '#f3f5f2',
  bgContainer: '#ffffff',
  bgElevated: '#ffffff',
  bgSubtle: '#edf1ed',
  border: '#dce3dd',
  borderStrong: '#b7c5bb',
  text: '#21322c',
  textSecondary: '#50625a',
  /* 辅助文字承担副标题、表头与说明，采用可读的中深灰绿而非浅灰。 */
  textMuted: '#64736b',
  textQuaternary: 'rgba(33, 50, 44, 0.64)',
  accent: '#186454',
  accentHover: '#105143',
  accentSoft: '#e5f0e9',
  /* 强调色作为文字：见 darkPalette 同名注释。 */
  accentText: '#155b4c',
  purple: '#6b21a8',
  purpleSoft: 'rgba(107,33,168,0.1)',
  /* 青色用于自动刷新任务，与主操作色保持区分。 */
  cyan: '#006b8f',
  cyanSoft: 'rgba(0,107,143,0.1)',
  /* 品红用于凭据流转类任务，不用于装饰。 */
  magenta: '#a30b7a',
  magentaSoft: 'rgba(163,11,122,0.1)',
  success: '#276a45',
  successSoft: '#e7f2e9',
  /* 暖琥珀只用于需要处理或等待的状态。 */
  warning: '#835411',
  warningSoft: 'rgba(255,149,0,0.12)',
  danger: '#a13e44',
  dangerSoft: '#faebeb',
  logBg: '#15231d',
  logBorder: '#2e4237',
  logText: '#d9e4dc',
  logMuted: '#a8b8ad',
  logSuccess: '#8ac99e',
  logWarning: '#ddb36d',
  logDanger: '#ff9b91',
  selectionBg: 'rgba(24,100,84,0.18)',
  spotlightBg: '#1d1d1f',
  shadowSm: '0 1px 2px rgba(24,50,39,0.04)',
  shadowMd: '0 10px 30px rgba(24,50,39,0.10)',
  shadowLg: '0 24px 64px rgba(24,50,39,0.16)',
  glassBg: 'rgba(243,245,242,0.94)',
  glassBorder: '#dce3dd',
  accentRing: 'rgba(24,100,84,0.28)',
  gradientFrom: '#186454',
  gradientTo: '#186454',
  sidebarBg: '#173a30',
  sidebarText: '#eff5ef',
  sidebarMuted: '#b2c6b9',
  sidebarBorder: '#365449',
  sidebarSelected: '#2d5646',
}

function buildTheme(palette: Palette, algorithm: typeof theme.darkAlgorithm) {
  return {
    token: {
      colorPrimary: palette.accent,
      colorInfo: palette.accent,
      colorSuccess: palette.success,
      colorWarning: palette.warning,
      colorError: palette.danger,
      colorBgBase: palette.bgContainer,
      colorTextBase: palette.text,
      colorBgContainer: palette.bgContainer,
      colorBgElevated: palette.bgElevated,
      colorBgLayout: palette.bgLayout,
      colorBorder: palette.borderStrong,
      colorBorderSecondary: palette.border,
      colorText: palette.text,
      colorTextSecondary: palette.textSecondary,
      colorTextTertiary: palette.textMuted,
      // 链接色（`Button type="link"`、「详情」这类）。antd 从 colorPrimary 派生，
      // dark 下得到 #2683dc —— 压在卡片底 #1c1c1e 上只有 4.34:1。用 accentText
      // 统一：dark #54aaff（6.94:1）、light #005bb5（6.64:1）。
      colorLink: palette.accentText,
      colorLinkHover: palette.accentText,
      colorLinkActive: palette.accentText,
      // 占位符与禁用文字不能淡到无法辨认；分别按主题保留足够对比。
      colorTextQuaternary: palette.textQuaternary,
      colorBgSpotlight: palette.spotlightBg,
      controlOutline: palette.accentSoft,
      // 控件适度圆润，数据面保持明确边界，不使用漂浮的大圆角卡片。
      borderRadius: 8,
      borderRadiusSM: 6,
      borderRadiusLG: 12,
      // 控件高度略抬高，点击目标更从容
      controlHeight: 36,
      controlHeightLG: 44,
      controlHeightSM: 28,
      fontFamily: FONT_STACK,
      fontSize: 14,
      lineHeight: 1.6,
      // 动效：统一曲线 + 略慢的时长，换来更顺的观感
      motionEaseInOut: EASE_STANDARD,
      motionEaseOut: EASE_OUT,
      motionDurationFast: '0.16s',
      motionDurationMid: '0.24s',
      motionDurationSlow: '0.36s',
      boxShadow: palette.shadowMd,
      boxShadowSecondary: palette.shadowSm,
      // 卡片自带一层极轻的投影，卡面从背景里"浮"起来
      boxShadowTertiary: palette.shadowSm,
      wireframe: false,
    },
    components: {
      Layout: {
        siderBg: 'transparent',
        triggerBg: palette.sidebarBg,
        triggerColor: palette.sidebarMuted,
        bodyBg: palette.bgLayout,
        headerBg: 'transparent',
      },
      Menu: {
        itemColor: palette.textSecondary,
        itemHoverBg: palette.bgSubtle,
        itemHoverColor: palette.text,
        itemSelectedBg: palette.accentSoft,
        itemSelectedColor: palette.accentText,
        itemHeight: 42,
        itemMarginInline: 8,
        itemMarginBlock: 2,
        itemBorderRadius: 6,
        subMenuItemBg: 'transparent',
        collapsedIconSize: 18,
        collapsedWidth: 72,
        darkItemBg: palette.sidebarBg,
        darkItemColor: palette.sidebarMuted,
        darkItemHoverBg: palette.sidebarBorder,
        darkItemHoverColor: palette.sidebarText,
        darkItemSelectedBg: palette.sidebarSelected,
        darkItemSelectedColor: '#ffffff',
        darkSubMenuItemBg: palette.sidebarBg,
        activeBarBorderWidth: 0,
        iconMarginInlineEnd: 12,
      },
      Card: {
        headerBg: 'transparent',
        colorBorderSecondary: palette.border,
        borderRadiusLG: 12,
        paddingLG: 24,
        headerFontSize: 14,
        headerHeight: 48,
      },
      Table: {
        headerBg: palette.bgSubtle,
        headerColor: palette.textSecondary,
        headerSplitColor: 'transparent',
        rowHoverBg: palette.bgSubtle,
        borderColor: palette.border,
        cellPaddingBlock: 12,
        cellPaddingInline: 16,
        headerBorderRadius: 12,
        footerBg: 'transparent',
      },
      Modal: {
        contentBg: palette.bgElevated,
        headerBg: palette.bgElevated,
        titleColor: palette.text,
        borderRadiusLG: 12,
        paddingContentHorizontalLG: 24,
      },
      Drawer: {
        colorBgElevated: palette.bgElevated,
        colorIcon: palette.textMuted,
        colorIconHover: palette.text,
      },
      Tag: {
        defaultBg: palette.bgSubtle,
        defaultColor: palette.textSecondary,
        borderRadiusSM: 6,
      },
      Button: {
        primaryColor: '#ffffff',
        primaryHoverColor: '#ffffff',
        primaryActiveColor: '#ffffff',
        primaryShadow: 'none',
        defaultShadow: 'none',
        dangerShadow: 'none',
        fontWeight: 500,
        paddingInline: 16,
        borderRadius: 8,
        borderRadiusLG: 8,
        borderRadiusSM: 6,
      },
      Segmented: {
        itemSelectedBg: palette.bgElevated,
        itemSelectedColor: palette.text,
        trackBg: palette.bgSubtle,
        itemColor: palette.textSecondary,
        itemHoverColor: palette.text,
        borderRadius: 10,
        borderRadiusSM: 8,
      },
      Input: {
        activeShadow: `0 0 0 3px ${palette.accentSoft}`,
        paddingBlock: 7,
        borderRadius: 10,
      },
      Select: {
        optionSelectedBg: palette.accentSoft,
        optionSelectedColor: palette.accentText,
        borderRadius: 10,
      },
      Tabs: {
        itemColor: palette.textSecondary,
        itemSelectedColor: palette.accentText,
        inkBarColor: palette.accent,
        titleFontSize: 14,
        horizontalItemPadding: '12px 0',
      },
      Dropdown: {
        borderRadiusLG: 14,
        paddingBlock: 6,
      },
      Tooltip: {
        colorBgSpotlight: palette.spotlightBg,
        borderRadius: 10,
      },
      Progress: {
        defaultColor: palette.accent,
        remainingColor: palette.bgSubtle,
      },
      Statistic: {
        contentFontSize: 30,
        titleFontSize: 13,
      },
      Empty: {
        colorTextDescription: palette.textMuted,
      },
      Popconfirm: {
        borderRadiusLG: 16,
      },
      Pagination: {
        // 页码是正文信息，跟随主题中用于文字的操作色。
        itemActiveColor: palette.accentText,
        itemActiveColorHover: palette.accentHover,
      },
      Switch: {
        trackHeight: 22,
        handleSize: 18,
        // 开关与主按钮使用同一操作色；文字对比由全局最终渲染规则保证。
        colorPrimary: palette.accent,
      },
    },
    algorithm,
  }
}

const darkTheme = buildTheme(darkPalette, theme.darkAlgorithm)
const lightTheme = buildTheme(lightPalette, theme.defaultAlgorithm)
/** 侧栏、抽屉和折叠弹出菜单的深色作用域，不泄漏到业务 Dropdown。 */
const navigationTheme = {
  components: {
    Menu: {
      itemColor: 'var(--sidebar-muted)',
      itemHoverBg: 'var(--sidebar-border)',
      itemHoverColor: 'var(--sidebar-text)',
      itemSelectedBg: 'var(--sidebar-selected)',
      itemSelectedColor: '#ffffff',
      subMenuItemBg: 'transparent',
      popupBg: 'var(--sidebar-bg)',
      darkItemBg: 'var(--sidebar-bg)',
      darkSubMenuItemBg: 'var(--sidebar-bg)',
      darkPopupBg: 'var(--sidebar-bg)',
      darkItemColor: 'var(--sidebar-muted)',
      darkItemHoverBg: 'var(--sidebar-border)',
      darkItemHoverColor: 'var(--sidebar-text)',
      darkItemSelectedBg: 'var(--sidebar-selected)',
      darkItemSelectedColor: '#ffffff',
    },
    Drawer: {
      colorBgElevated: 'var(--sidebar-bg)',
    },
    Button: {
      defaultColor: 'var(--sidebar-muted)',
      defaultHoverColor: 'var(--sidebar-text)',
      defaultHoverBg: 'var(--sidebar-border)',
      textTextColor: 'var(--sidebar-muted)',
      textHoverBg: 'var(--sidebar-border)',
    },
  },
}

const CSS_VAR_NAMES: Record<keyof Palette, string> = {
  bgLayout: '--bg-layout',
  bgContainer: '--bg-container',
  bgElevated: '--bg-elevated',
  bgSubtle: '--bg-subtle',
  border: '--border',
  borderStrong: '--border-strong',
  text: '--text',
  textSecondary: '--text-secondary',
  textMuted: '--text-muted',
  textQuaternary: '--text-quaternary',
  accent: '--accent',
  accentHover: '--accent-hover',
  accentSoft: '--accent-soft',
  accentText: '--accent-text',
  purple: '--purple',
  purpleSoft: '--purple-soft',
  cyan: '--cyan',
  cyanSoft: '--cyan-soft',
  magenta: '--magenta',
  magentaSoft: '--magenta-soft',
  success: '--success',
  successSoft: '--success-soft',
  warning: '--warning',
  warningSoft: '--warning-soft',
  danger: '--danger',
  dangerSoft: '--danger-soft',
  logBg: '--log-bg',
  logBorder: '--log-border',
  logText: '--log-text',
  logMuted: '--log-muted',
  logSuccess: '--log-success',
  logWarning: '--log-warning',
  logDanger: '--log-danger',
  selectionBg: '--selection-bg',
  spotlightBg: '--spotlight-bg',
  shadowSm: '--shadow-sm',
  shadowMd: '--shadow-md',
  shadowLg: '--shadow-lg',
  glassBg: '--glass-bg',
  glassBorder: '--glass-border',
  accentRing: '--accent-ring',
  gradientFrom: '--gradient-from',
  gradientTo: '--gradient-to',
  sidebarBg: '--sidebar-bg',
  sidebarText: '--sidebar-text',
  sidebarMuted: '--sidebar-muted',
  sidebarBorder: '--sidebar-border',
  sidebarSelected: '--sidebar-selected',
}

/**
 * 表单控件宽度梯度。
 *
 * 之前各页面各写各的宽度（硬编码 200 / 800，或者干脆不给、跟随容器拉伸），
 * 结果同一个站点里一位数输入框能占 924px。集中在这里是为了让"多宽"变成一个
 * 可以讨论的决定，而不是每个页面各自的偶然结果。
 *
 * 用 CSS 变量而不是内联 style，页面里写 `style={{ maxWidth: 'var(--w-field)' }}`。
 */
export const FIELD_WIDTH = {
  /** 数字、短标签、地区码（如 "30"、"US"） */
  field: 200,
  /** 名称、域名、单行短文本 */
  fieldMd: 360,
  /** URL、API Key、路径 */
  fieldLg: 480,
  /** 说明性长文本 */
  prose: 720,
  // 注：页面级上限（原 page: 1200）已挪到 index.css 的 --w-page ——
  // 它需要是响应式表达式 max(1200px, 62.5vw)（低缩放/超宽屏下随视口增长），
  // 而 applyThemeVars 注入的是固定像素值，会压掉表达式。
} as const

/**
 * 栅格间距（Row/Col 的 gutter）。
 *
 * 只用 JS 侧：antd 的 `gutter` 只接受数字，CSS 变量在这里帮不上忙，
 * 所以 index.css 里没有对应的 `--grid-gap`。各页面曾各写 16/18/24，
 * 视觉上同级的间距却不一样，集中到这里让「多宽」成为可讨论的决定。
 */
export const GRID_GAP = 18

const WIDTH_CSS_VAR_NAMES: Record<keyof typeof FIELD_WIDTH, string> = {
  field: '--w-field',
  fieldMd: '--w-field-md',
  fieldLg: '--w-field-lg',
  prose: '--w-prose',
  // --w-page 不在这里 —— 它是响应式表达式（index.css 定义），
  // 内联固定值会覆盖样式表里的 max()，低缩放下又会缩成窄条。
}


/** 把当前主题写成 CSS 变量，登录页等没有 ConfigProvider 的地方也能取到同一套色。 */
export function applyThemeVars(mode: ThemeMode) {
  const palette = mode === 'light' ? lightPalette : darkPalette
  const root = document.documentElement
  for (const [key, name] of Object.entries(CSS_VAR_NAMES)) {
    root.style.setProperty(name, palette[key as keyof Palette])
  }
  for (const [key, name] of Object.entries(WIDTH_CSS_VAR_NAMES)) {
    root.style.setProperty(name, `${FIELD_WIDTH[key as keyof typeof FIELD_WIDTH]}px`)
  }
  root.style.setProperty('--sider-trigger-border', palette.border)
  root.style.setProperty('--font-stack', FONT_STACK)
  root.style.setProperty('--ease-standard', EASE_STANDARD)
  root.style.setProperty('--ease-out', EASE_OUT)
  // 注意：--page-pad-* / --section-gap 刻意**不**在这里注入。
  // applyThemeVars 写的是 documentElement 的内联样式，优先级高于样式表规则，
  // 会永久压掉 index.css 里 <992px 的媒体查询 —— 窄屏就再也收不紧了。
  root.classList.toggle('light', mode === 'light')
  root.style.colorScheme = mode
}

export { darkTheme, lightTheme, navigationTheme, darkPalette, lightPalette, FONT_STACK }
