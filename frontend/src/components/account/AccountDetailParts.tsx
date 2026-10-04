// 从 Accounts.tsx 抽出的纯展示组件（无副作用）。
import type { ReactNode } from 'react'
import { Typography, Alert, Tag, Space, theme } from 'antd'

import {
  authStateMeta,
  codexStateMeta,
  formatStructuredText,
  formatSyncTime,
  planMeta,
} from '@/lib/accountFormat'

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
            maxHeight: code ? 160 : 'none',
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
