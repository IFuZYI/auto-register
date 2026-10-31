// 从 Accounts.tsx 抽出的纯展示组件（无副作用）。
import { useState } from 'react'
import type { ReactNode } from 'react'
import { Typography, Alert, Button, Tag, Space, theme } from 'antd'
import { RightOutlined } from '@ant-design/icons'

import {
  authStateMeta,
  codexStateMeta,
  formatStructuredText,
  formatSyncTime,
  planMeta,
} from '@/lib/accountFormat'
import { HelpTip } from '@/components/settings/SettingsPrimitives'

const { Text } = Typography

export function SummaryField({
  label,
  value,
  code = false,
}: {
  label: string
  value?: string
  code?: boolean
}) {
  const { token } = theme.useToken()
  if (!value) return null

  const content = code ? formatStructuredText(value) : value
  const isBlock = code || content.length > 96 || content.includes('\n')

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '104px minmax(0, 1fr)',
        gap: 12,
        alignItems: 'start',
      }}
    >
      <Text type="secondary" style={{ fontSize: 12, lineHeight: '20px' }}>
        {label}
      </Text>
      {isBlock ? (
        <pre
          style={{
            margin: 0,
            padding: code ? '8px 10px' : 0,
            borderRadius: code ? token.borderRadius : 0,
            border: code ? `1px solid ${token.colorBorder}` : 'none',
            background: code ? token.colorBgElevated : 'transparent',
            color: code ? token.colorText : token.colorTextSecondary,
            fontFamily: code ? 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace' : 'inherit',
            fontSize: 12,
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            overflowWrap: 'anywhere',
            maxHeight: code ? 96 : 'none',
            overflow: code ? 'auto' : 'visible',
          }}
        >
          {content}
        </pre>
      ) : (
        <Text style={{ display: 'block', color: token.colorTextSecondary, lineHeight: '20px' }}>
          {content}
        </Text>
      )}
    </div>
  )
}

export function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  const { token } = theme.useToken()

  return (
    <div
      style={{
        marginTop: 16,
        padding: 14,
        borderRadius: token.borderRadiusLG,
        border: `1px solid ${token.colorBorder}`,
        background: token.colorFillAlter,
      }}
    >
      <div style={{ marginBottom: 10, fontWeight: 600, color: token.colorText }}>{title}</div>
      {children}
    </div>
  )
}

/* ── 账号详情（2026-10-31 重做） ─────────────────────────────────────
 *
 * 改造前这个弹窗里**三套表现形式并存**：
 *   1. `Form layout="vertical"` —— 状态 / 密码 / Access Token，占满整行
 *   2. 手写的圆角盒子 —— Refresh Token / TOTP / 注册代理（border + fillAlter）
 *   3. `DetailSection` —— 又一个带边框带底色的盒子
 *
 * 而这三套里装的是同一类东西：一个标签 + 一个值（值可能是要复制的密钥）。
 * 同一件事三种画法，用户每换一段就要重新认一遍「这段是什么」。
 *
 * 现在统一成「一行一个字段」：标签左固定 104px、值右对齐，行间一条发丝线。
 * 不套盒子 —— 盒子是**分组**的信号，一段字段不是一张卡（这条与账号表
 * 里删掉 `compactPanelStyle` 是同一条理由）。
 *
 * 分组用 `DetailGroup`：一个 12px 的小标题 + 上方发丝线，没有边框、没有底色。
 */

/** 一行字段。`hint` 走 ⓘ（hover + 键盘 focus 都能出）。 */
export function DetailRow({
  label,
  hint,
  children,
}: {
  label: ReactNode
  hint?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="detail-row">
      <div className="detail-row__label">
        <span>{label}</span>
        {hint ? <HelpTip text={hint} /> : null}
      </div>
      <div className="detail-row__value">{children}</div>
    </div>
  )
}

/**
 * 一组字段。`title` 为空时不画标题，只保留发丝线。
 *
 * `collapsible` 用于**次要信息**（本地探测结果）：那是排查时才看的东西，
 * 默认展开会把弹窗撑到两屏高，而打开详情的人九成是来复制凭据或改状态的。
 * 折叠标题本身是 `<button>`，键盘能 Tab 到、`aria-expanded` 说明开合状态。
 */
export function DetailGroup({
  title,
  hint,
  children,
  collapsible = false,
  defaultOpen = true,
}: {
  title?: ReactNode
  hint?: ReactNode
  children: ReactNode
  collapsible?: boolean
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <section className="detail-group">
      {title ? (
        <h3 className="detail-group__title">
          {collapsible ? (
            <button
              type="button"
              className="detail-group__toggle"
              aria-expanded={open}
              onClick={() => setOpen((v) => !v)}
            >
              <RightOutlined className={open ? 'detail-group__chevron is-open' : 'detail-group__chevron'} />
              {title}
            </button>
          ) : (
            <span>{title}</span>
          )}
          {hint ? <HelpTip text={hint} /> : null}
        </h3>
      ) : null}
      {open ? children : null}
    </section>
  )
}

const MONO = 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace'

/**
 * 密钥值：**一行**、等宽、超长省略，悬停看全文，右侧一个复制按钮。
 *
 * 改造前同一类东西三种交互：密码是 `Text copyable`（会折行）、AT 是
 * 两行 TextArea（可编辑但看不见全貌）、RT/TOTP 是盒子里的 `Text copyable`
 * （`wordBreak: break-all` 把 2000 字符的 JWT 折成十几行）。
 * 一个 2000 字符的 JWT 折行后能把弹窗撑到三屏高，而真正要做的事只有
 * 一件 —— **复制走**。
 */
export function SecretLine({
  value,
  empty = '无',
  onEdit,
  editLabel = '替换',
}: {
  value?: string
  empty?: string
  onEdit?: () => void
  editLabel?: string
}) {
  if (!value) {
    return <Text type="secondary">{empty}</Text>
  }
  return (
    <div className="detail-secret">
      <Text
        className="detail-secret__text"
        style={{ fontFamily: MONO, fontSize: 12 }}
        ellipsis={{ tooltip: value }}
        copyable={{ text: value, tooltips: ['复制', '已复制'] }}
      >
        {value}
      </Text>
      {onEdit ? (
        <Button type="link" size="small" style={{ paddingInline: 4 }} onClick={onEdit}>
          {editLabel}
        </Button>
      ) : null}
    </div>
  )
}


export function LocalProbeSummary({ probe }: { probe: any }) {
  const checkedAt = probe?.checked_at || probe?.auth?.checked_at || probe?.subscription?.checked_at || probe?.codex?.checked_at
  const auth = probe?.auth || {}
  const subscription = probe?.subscription || {}
  const codex = probe?.codex || {}

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Tag color={authStateMeta(auth.state).color}>认证: {authStateMeta(auth.state).label}</Tag>
        <Tag color={planMeta(subscription.plan).color}>订阅: {planMeta(subscription.plan).label}</Tag>
        <Tag color={codexStateMeta(codex.state).color}>Codex: {codexStateMeta(codex.state).label}</Tag>
      </div>
      <SummaryField label="探测时间" value={checkedAt ? formatSyncTime(checkedAt) : ''} />
      <SummaryField label="认证信息" value={auth.message} code />
      <SummaryField label="工作区套餐" value={subscription.workspace_plan_type} />
      <SummaryField label="Codex 信息" value={codex.message} code />
    </div>
  )
}

export function TotpSecretAlert({ secret }: { secret: string }) {
  return (
    <Alert
      type="warning"
      showIcon
      message="TOTP 密钥只下发这一次，服务端取不回"
      description={
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          <Text
            style={{ fontFamily: 'monospace', fontSize: 13, wordBreak: 'break-all' }}
            copyable={{ text: secret, tooltips: ['复制密钥', '已复制'] }}
          >
            {secret}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            已随账号存库，之后可在账号详情或列表的「2FA 已绑」标签上再复制。
          </Text>
        </Space>
      }
    />
  )
}
