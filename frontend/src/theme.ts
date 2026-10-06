import { theme } from 'antd'

export type ThemeMode = 'dark' | 'light'

/**
 * 动效曲线。苹果的界面动效普遍是「起步快、收尾稳」，
 * 这条曲线接近系统转场手感，比默认的 ease 更顺。
 */
export const EASE_STANDARD = 'cubic-bezier(0.4, 0, 0.2, 1)'
export const EASE_OUT = 'cubic-bezier(0.16, 1, 0.3, 1)'

/** 系统字体栈：优先 SF Pro（苹果设备），中文回落到苹方。 */
const FONT_STACK = [
  '-apple-system',
  'BlinkMacSystemFont',
  '"SF Pro Display"',
  '"SF Pro Text"',
  '"Helvetica Neue"',
  '"PingFang SC"',
  '"Hiragino Sans GB"',
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
}

/** 暗色：以纯黑打底 + 分层灰面，接近 macOS / iOS 深色观感。 */
const darkPalette: Palette = {
  bgLayout: '#000000',
  bgContainer: '#1c1c1e',
  bgElevated: '#2c2c2e',
  bgSubtle: 'rgba(255,255,255,0.06)',
  border: 'rgba(255,255,255,0.1)',
  borderStrong: 'rgba(255,255,255,0.18)',
  text: '#f5f5f7',
  textSecondary: '#a1a1a6',
  /* #8e8e93 在卡片底 #1c1c1e 上是 5.22:1，但压在浮层底 #2a2a2c 上只有
     4.39:1（AA 需 4.5:1）—— 表头、空状态说明、副标题都吃这个色。
     #98989d 在两处分别是 4.99:1 / 5.99:1，都过线。
     （亮色主题此前出于同样原因把 #86868b 调成了 #6e6e73。） */
  textMuted: '#98989d',
  textQuaternary: 'rgba(245, 245, 247, 0.55)',
  accent: '#2997ff',
  accentHover: '#54aaff',
  accentSoft: 'rgba(41,151,255,0.16)',
  /* 强调色作为**文字**时的取值（链接、菜单选中、Tab 选中、processing 标签）。
     与 `accent` 分开是因为用途不同：accent 多用于填充/描边（白字压在上面），
     而这一支是文字本身，必须压在各种浅色合成底上过 AA。

     dark：#54aaff 最暗的底（Select 选中项 #2c3d4f）4.54:1。
     light：#005bb5 最亮处 5.34:1（菜单选中底），白底 6.64:1。 */
  accentText: '#54aaff',
  purple: '#c9a6ff',
  purpleSoft: 'rgba(201,166,255,0.15)',
  success: '#30d158',
  successSoft: 'rgba(48,209,88,0.15)',
  warning: '#ff9f0a',
  warningSoft: 'rgba(255,159,10,0.15)',
  /* 危险色。antd 会从 seed 派生 palette[6] 当实际文字色（#ff453a → #dc3e34），
     派生值在卡片底 #1c1c1e 上只有 3.87:1、在 error Tag 底 #2c1415 上 3.93:1，
     两处都低于 AA 的 4.5:1 —— 而危险色承载的正是「清空」「删除」「错误」这类
     必须看清的文字。改成 #ff7a70 后派生值为 #dc6b62：
     卡片底 6.7:1、Tag 底 4.98:1，两处都过。 */
  danger: '#ff7a70',
  dangerSoft: 'rgba(255,69,58,0.15)',
  logBg: '#0a0a0b',
  logBorder: 'rgba(255,255,255,0.08)',
  logText: '#d1d1d6',
  logMuted: '#7c7c81',
  logSuccess: '#30d158',
  logWarning: '#ff9f0a',
  logDanger: '#ff453a',
  selectionBg: 'rgba(41,151,255,0.35)',
  spotlightBg: '#3a3a3c',
  shadowSm: '0 1px 2px rgba(0,0,0,0.5)',
  shadowMd: '0 8px 24px rgba(0,0,0,0.55)',
  shadowLg: '0 20px 56px rgba(0,0,0,0.65)',
  glassBg: 'rgba(28,28,30,0.72)',
  glassBorder: 'rgba(255,255,255,0.08)',
  accentRing: 'rgba(41,151,255,0.4)',
  /* 主按钮渐变（白字压在上面）。原 #0a84ff 最亮处白字只有 3.65:1 ——
     渐变从左到右变暗，最差在起点。#0a6fd8 全段最差 4.91:1。 */
  gradientFrom: '#0a6fd8',
  gradientTo: '#5e5ce6',
}

/** 亮色：浅灰打底 + 纯白卡面，苹果官网的经典配色。 */
const lightPalette: Palette = {
  bgLayout: '#f5f5f7',
  bgContainer: '#ffffff',
  bgElevated: '#ffffff',
  bgSubtle: 'rgba(0,0,0,0.03)',
  border: 'rgba(0,0,0,0.08)',
  borderStrong: 'rgba(0,0,0,0.16)',
  text: '#1d1d1f',
  textSecondary: '#68686d',
  /* #86868b 在 #f5f5f7 上只有 3.33:1（WCAG AA 正文要求 4.5:1），
     而它承载的是副标题、卡片描述、表头这类高频阅读内容。
     上一版用 #6e6e73（4.66:1）—— 但 antd 的默认标签底、表行底都比布局底深一点，
     实测在 default 标签底 rgb(238,238,240) 上只有 4.38:1，仍不达标。
     #68686d 在三处分别是 4.78 / 4.91 / 5.09，全部过线。 */
  textMuted: '#68686d',
  textQuaternary: 'rgba(29, 29, 31, 0.62)',
  accent: '#0071e3',
  accentHover: '#0077ed',
  accentSoft: 'rgba(0,113,227,0.1)',
  /* 强调色作为文字：见 darkPalette 同名注释。light 用 #005bb5 ——
     菜单选中底 5.34:1、Select/Tab 选中底 5.35~5.80:1、白底 6.64:1。 */
  accentText: '#005bb5',
  purple: '#6b21a8',
  purpleSoft: 'rgba(107,33,168,0.1)',
  success: '#17742c',
  successSoft: 'rgba(52,199,89,0.12)',
  /* 亮色警告色。antd 的 Tag 会把该色与白底混合当作徽章底色，
     所以不能只按「白底」挑色 —— 实测合成底约 #e6ddcf，
     #a54b00 在其上只有 4.33:1；#9c4700 为 4.71:1、白底 6.34:1，两处都过 AA。 */
  warning: '#9c4700',
  warningSoft: 'rgba(255,149,0,0.12)',
  danger: '#d70015',
  dangerSoft: 'rgba(255,59,48,0.1)',
  logBg: '#fbfbfd',
  logBorder: 'rgba(0,0,0,0.07)',
  logText: '#3a3a3c',
  logMuted: '#86868b',
  logSuccess: '#248a3d',
  logWarning: '#9c4700',
  logDanger: '#d70015',
  selectionBg: 'rgba(0,113,227,0.2)',
  spotlightBg: '#1d1d1f',
  shadowSm: '0 1px 3px rgba(0,0,0,0.06)',
  shadowMd: '0 6px 20px rgba(0,0,0,0.08)',
  shadowLg: '0 20px 48px rgba(0,0,0,0.14)',
  glassBg: 'rgba(255,255,255,0.72)',
  glassBorder: 'rgba(0,0,0,0.06)',
  accentRing: 'rgba(0,113,227,0.3)',
  gradientFrom: '#0071e3',
  gradientTo: '#5e5ce6',
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
      // 占位符/禁用文字。antd 默认取 colorTextBase 的 0.25 alpha，压在深色底
      // 上只有 2.19:1（#1c1c1e 卡片）—— 占位符是「这个框该填什么」的唯一提示，
      // 看不清等于没有。实测 0.55 alpha 在三种深色底（#000 布局 / #1c1c1e 卡片 /
      // #2c2c2e 浮层）上分别是 5.83 / 5.57 / 5.00，全部过 AA。
      // 浅色主题同理（0.25 的深色文字在浅底上偏灰），统一按主题取 alpha。
      colorTextQuaternary: palette.textQuaternary,
      colorBgSpotlight: palette.spotlightBg,
      controlOutline: palette.accentSoft,
      // 圆角：卡面更大，控件收在中档，整体偏柔
      borderRadius: 10,
      borderRadiusSM: 8,
      borderRadiusLG: 18,
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
        triggerBg: palette.bgElevated,
        triggerColor: palette.textSecondary,
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
        itemBorderRadius: 10,
        subMenuItemBg: 'transparent',
        activeBarBorderWidth: 0,
        iconMarginInlineEnd: 12,
      },
      Card: {
        headerBg: 'transparent',
        colorBorderSecondary: palette.border,
        borderRadiusLG: 18,
        paddingLG: 24,
        headerFontSize: 15,
        headerHeight: 52,
      },
      Table: {
        headerBg: 'transparent',
        headerColor: palette.textMuted,
        headerSplitColor: 'transparent',
        rowHoverBg: palette.bgSubtle,
        borderColor: palette.border,
        cellPaddingBlock: 14,
        cellPaddingInline: 16,
        headerBorderRadius: 12,
        footerBg: 'transparent',
      },
      Modal: {
        contentBg: palette.bgElevated,
        headerBg: palette.bgElevated,
        titleColor: palette.text,
        borderRadiusLG: 20,
        paddingContentHorizontalLG: 28,
      },
      Drawer: {
        colorBgElevated: palette.bgElevated,
      },
      Tag: {
        defaultBg: palette.bgSubtle,
        defaultColor: palette.textSecondary,
        borderRadiusSM: 6,
      },
      Button: {
        primaryShadow: 'none',
        defaultShadow: 'none',
        dangerShadow: 'none',
        fontWeight: 500,
        paddingInline: 18,
        borderRadius: 10,
        borderRadiusLG: 12,
        borderRadiusSM: 8,
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
        // 当前页码的文字色。antd 从 colorPrimary 派生得到 #2683dc，压在
        // 布局底 #000 上只有 4.34:1（AA 需 4.5:1）—— 差一点点，但页码正是
        // 用户要读的。直接用品牌蓝本身：#2997ff 在 #000 上 6.96:1。
        itemActiveColor: palette.accent,
        itemActiveColorHover: palette.accentHover,
      },
      Switch: {
        trackHeight: 22,
        handleSize: 18,
        // 轨道上用白字（"有 RT"/"无 RT"）。antd 的 track 取 colorPrimary，
        // dark 算法把它调暗成 #2683dc → 白字只有 3.92:1。
        // #005bb5 实测 6.64:1，且与品牌蓝同色系。
        colorPrimary: '#005bb5',
      },
    },
    algorithm,
  }
}

const darkTheme = buildTheme(darkPalette, theme.darkAlgorithm)
const lightTheme = buildTheme(lightPalette, theme.defaultAlgorithm)

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

export { darkTheme, lightTheme, darkPalette, lightPalette, FONT_STACK }
