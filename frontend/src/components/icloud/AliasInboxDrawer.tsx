// 从 ICloud.tsx 抽出的收件箱抽屉（纯展示，接线不动）。
import { useCallback, useEffect, useMemo, useState } from 'react'
import { App, Badge, Button, Drawer, Empty, Grid, Input, List, Space, Tooltip, Typography } from 'antd'
import { PaperClipOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons'

import { listICloudAliasMessages, type ICloudAlias, type ICloudMessage } from '@/api/icloud'
import { formatDateTime, formatRelativeTime } from '@/lib/icloud'
import { MessageDetail } from '@/components/icloud/MessageDetail'

const { Text, Paragraph } = Typography

export function AliasInboxDrawer({ alias, onClose }: { alias: ICloudAlias | null; onClose: () => void }) {
  const { message } = App.useApp()
  const screens = Grid.useBreakpoint()
  const [messages, setMessages] = useState<ICloudMessage[]>([])
  const [loading, setLoading] = useState(false)
  const [selectedId, setSelectedId] = useState('')
  const [keyword, setKeyword] = useState('')

  const isNarrow = !screens.md

  const load = useCallback(async () => {
    if (!alias) return
    setLoading(true)
    try {
      setMessages(await listICloudAliasMessages(alias.id))
    } catch (error) {
      message.error((error as Error).message)
      setMessages([])
    } finally {
      setLoading(false)
    }
  }, [alias, message])

  useEffect(() => {
    setMessages([])
    setSelectedId('')
    setKeyword('')
    load()
  }, [load])

  const visible = useMemo(() => {
    const needle = keyword.trim().toLowerCase()
    if (!needle) return messages
    return messages.filter((item) =>
      [item.subject, item.from.name, item.from.email, item.snippet, item.text_body]
        .join(' ')
        .toLowerCase()
        .includes(needle),
    )
  }, [messages, keyword])

  // 宽屏默认摊开第一封，省掉一次点击；窄屏保持列表优先，点了才进详情。
  const selected =
    visible.find((item) => item.id === selectedId) ?? (isNarrow ? undefined : visible[0])

  const messageList = (
    <List
      loading={loading}
      dataSource={visible}
      locale={{
        emptyText: <Empty description={keyword ? '没有匹配的邮件' : '暂时没有收到邮件'} />,
      }}
      renderItem={(item) => {
        const active = selected?.id === item.id
        return (
          <List.Item
            onClick={() => setSelectedId(item.id)}
            style={{
              cursor: 'pointer',
              padding: '12px 16px',
              borderInlineStart: `3px solid ${active ? 'var(--accent)' : 'transparent'}`,
              background: active ? 'var(--accent-soft)' : undefined,
            }}
          >
            {/* 这里刻意不用 Space：它会给每个子元素套一层 div，flex:1 落不到文本上，
                长主题和长发件人地址就截不断——正是原来那版糊成一片的原因。 */}
            <div style={{ width: '100%', minWidth: 0, display: 'grid', gap: 4 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
                <Text strong={!item.is_read} ellipsis style={{ flex: 1, minWidth: 0 }}>
                  {item.subject || '(无主题)'}
                </Text>
                {item.has_attachments && <PaperClipOutlined style={{ opacity: 0.55 }} />}
                {!item.is_read && <Badge status="processing" />}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
                <Text type="secondary" ellipsis style={{ flex: 1, minWidth: 0, fontSize: 12 }}>
                  {item.from.name || item.from.email}
                </Text>
                <Tooltip title={formatDateTime(item.received_at)}>
                  <Text type="secondary" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
                    {formatRelativeTime(item.received_at)}
                  </Text>
                </Tooltip>
              </div>
              <Text type="secondary" ellipsis style={{ fontSize: 12, opacity: 0.75 }}>
                {item.snippet}
              </Text>
            </div>
          </List.Item>
        )
      }}
    />
  )

  const detailPane = selected ? (
    <MessageDetail item={selected} />
  ) : (
    <div style={{ display: 'grid', placeItems: 'center', height: '100%', minHeight: 240 }}>
      <Empty description="选择左侧邮件查看正文" />
    </div>
  )

  return (
    <Drawer
      open={Boolean(alias)}
      onClose={onClose}
      width={isNarrow ? '100%' : 'min(1080px, 92vw)'}
      styles={{ body: { padding: 0, display: 'flex', flexDirection: 'column' } }}
      title={
        <Tooltip title={alias?.address}>
          <Text ellipsis style={{ maxWidth: '100%' }}>
            收件箱 · {alias?.address ?? ''}
          </Text>
        </Tooltip>
      }
      extra={
        <Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {messages.length} 封
          </Text>
          <Button icon={<ReloadOutlined spin={loading} />} onClick={load} aria-label="刷新别名列表" title="刷新别名列表" />
        </Space>
      }
    >
      {isNarrow && selected ? (
        <div style={{ padding: 16, overflow: 'auto' }}>
          <Button type="link" style={{ paddingInline: 0 }} onClick={() => setSelectedId('')}>
            ← 返回列表
          </Button>
          <div style={{ marginTop: 8 }}>{detailPane}</div>
        </div>
      ) : (
        <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
          <div
            style={{
              width: isNarrow ? '100%' : 320,
              flex: isNarrow ? 1 : '0 0 320px',
              borderInlineEnd: isNarrow ? undefined : '1px solid var(--border)',
              display: 'flex',
              flexDirection: 'column',
              minHeight: 0,
            }}
          >
            <div style={{ padding: 12 }}>
              <Input
                allowClear
                prefix={<SearchOutlined />}
                placeholder="搜索主题、发件人、正文"
                value={keyword}
                onChange={(event) => setKeyword(event.target.value)}
              />
            </div>
            <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>{messageList}</div>
          </div>
          {!isNarrow && (
            <div style={{ flex: 1, overflow: 'auto', padding: 20, minWidth: 0 }}>
              {detailPane}
              <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 24, marginBottom: 0 }}>
                每次打开都会通过 IMAP 实时读取主号收件箱中投递到该地址的邮件，不使用本地缓存。
              </Paragraph>
            </div>
          )}
        </div>
      )}
    </Drawer>
  )
}
