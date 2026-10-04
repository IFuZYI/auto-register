// 从 ICloud.tsx 抽出的邮件正文与详情（纯展示，接线不动）。
import { useCallback, useRef, useState } from 'react'
import { Space, Tag, Typography } from 'antd'
import { PaperClipOutlined } from '@ant-design/icons'

import type { ICloudMessage } from '@/api/icloud'
import { formatDateTime } from '@/lib/icloud'

const { Text, Title } = Typography

/** 邮件正文是不可信的第三方 HTML，放进 iframe 隔离，且不给 allow-scripts。 */
export function MessageBody({ item }: { item: ICloudMessage }) {
  const frameRef = useRef<HTMLIFrameElement>(null)
  const [height, setHeight] = useState(240)
  const html = item.html_body?.trim() ?? ''

  const resize = useCallback(() => {
    const measure = () => {
      const body = frameRef.current?.contentDocument?.body
      if (body) setHeight(body.scrollHeight + 32)
    }
    measure()
    // 图片是 onLoad 之后才陆续解码的，高度还会再变一次。
    window.setTimeout(measure, 300)
  }, [])

  if (!html) {
    const text = item.text_body?.trim() || item.snippet?.trim()
    return text ? (
      <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.7 }}>{text}</div>
    ) : (
      <Text type="secondary">这封邮件没有正文</Text>
    )
  }

  // 邮件 HTML 基本都假定浅色背景，深色主题下直接渲染会出现黑字黑底。
  const srcDoc = `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="script-src 'none'">
<style>
  html,body{margin:0;padding:16px;background:#f7f8fa;color:#2f3540;
    font:14px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
    word-break:break-word;overflow-wrap:anywhere;}
  img,table{max-width:100%!important;height:auto;}
  a{color:#3f6ea8;}
</style></head><body>${html}</body></html>`

  return (
    <iframe
      ref={frameRef}
      title="邮件正文"
      sandbox="allow-same-origin"
      srcDoc={srcDoc}
      onLoad={resize}
      style={{ width: '100%', height, border: 0, borderRadius: 10, background: '#f7f8fa', display: 'block' }}
    />
  )
}

export function MessageDetail({ item }: { item: ICloudMessage }) {
  const sender = item.from.name || item.from.email
  return (
    <div>
      <Title level={5} style={{ marginTop: 0, marginBottom: 12, wordBreak: 'break-word' }}>
        {item.subject || '(无主题)'}
      </Title>
      <Space direction="vertical" size={4} style={{ width: '100%', marginBottom: 16 }}>
        <Space wrap size={8}>
          <Text strong>{sender}</Text>
          {item.from.name && <Text type="secondary">&lt;{item.from.email}&gt;</Text>}
          {item.has_attachments && <Tag icon={<PaperClipOutlined />}>含附件</Tag>}
        </Space>
        <Text type="secondary" style={{ fontSize: 12 }}>
          发往 {item.to.map((address) => address.email).join('、') || item.alias_address} ·{' '}
          {formatDateTime(item.received_at)}
        </Text>
      </Space>
      {/* key 让每封邮件重挂载，正文高度自然从初始值重新量起。 */}
      <MessageBody key={item.id} item={item} />
    </div>
  )
}
