import type { ReactNode } from 'react'
import { Tooltip } from 'antd'
import { InfoCircleOutlined } from '@ant-design/icons'

/**
 * 全局配置页的三个原语：说明气泡、配置行、节头。
 *
 * 抽出来是因为这一页要反复用同一套排版 —— 配置项有 100 多个，每处各写一遍
 * 内联样式，行高与标签列宽迟早对不齐（改造前就是这样：各 Card 里的
 * `Form.Item` 各带一份 margin，一屏里能出现三种行距）。
 *
 * 设计取向见 index.css 里 `.set-row` 那段注释：这一页是「规格表」，
 * 一行一个配置项，解释文字默认不占版面。
 */

/** ⓘ 说明气泡。没有文案时不渲染 —— 摆一个点了没反应的图标比不摆更糟。 */
export function HelpTip({ text }: { text?: ReactNode }) {
  if (!text) return null
  return (
    <Tooltip
      // `focus` 必须显式加上：antd Tooltip 默认只响应 hover。只给 `tabIndex`
      // 是不够的 —— 图标能聚焦，但气泡不出来，键盘用户等于读不到说明。
      trigger={['hover', 'focus']}
      // 正文限宽 320px：长句在气泡里换行比拉成一整条更难读。
      title={<div style={{ maxWidth: 320, lineHeight: 1.65 }}>{text}</div>}
      styles={{ root: { maxWidth: 360 } }}
    >
      {/* tabIndex 让键盘用户也能触发（antd Tooltip 同时响应 focus）。
          没有它，说明文字对只用键盘的人等于不存在。 */}
      <InfoCircleOutlined className="set-help" tabIndex={0} role="img" aria-label="说明" />
    </Tooltip>
  )
}

/** 一行配置项：左边标签（可带 ⓘ），右边控件。 */
export function SettingRow({
  label,
  hint,
  children,
}: {
  label: ReactNode
  hint?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="set-row">
      <div className="set-row__label">
        <span>{label}</span>
        <HelpTip text={hint} />
      </div>
      <div className="set-row__control">{children}</div>
    </div>
  )
}

/** 节头：一个小标题 + 可选的 ⓘ，下面一条发丝线。 */
export function SectionHead({ title, hint }: { title: ReactNode; hint?: ReactNode }) {
  return (
    <div className="set-section__head">
      <span className="set-section__title">{title}</span>
      <HelpTip text={hint} />
    </div>
  )
}

/** 一节配置：节头 + 若干行。 */
export function SettingSection({
  title,
  hint,
  children,
}: {
  title: ReactNode
  hint?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="set-section">
      <SectionHead title={title} hint={hint} />
      {children}
    </section>
  )
}
