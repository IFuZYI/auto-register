import { createContext, useContext } from 'react'

/**
 * 移动端导航抽屉的开关。
 *
 * 侧栏在窄屏下不该占着 232px 不走（实测 390px 屏上只剩 94px 内容区），
 * 所以移动端把导航收进抽屉，由 PageHeader 左上角的汉堡按钮唤出。
 * 用 context 传是因为触发按钮在 PageHeader 里，而抽屉状态在 App 里 ——
 * 与其让每个页面各自持有状态，不如让「怎么打开导航」由外壳说了算。
 *
 * 放在 hooks/ 而不是 App.tsx：App.tsx 导出组件，再导出 hook 会让
 * react-refresh 无法只热替换组件（eslint react-refresh/only-export-components）；
 * 而且 PageHeader 从 App.tsx 取 hook、App.tsx 又经 pages 间接 import
 * PageHeader，形成循环依赖。
 */
export const MobileNavContext = createContext<{ open: () => void }>({ open: () => {} })

/** 供 PageHeader 等组件唤出移动端导航。 */
export function useMobileNav() {
  return useContext(MobileNavContext)
}
