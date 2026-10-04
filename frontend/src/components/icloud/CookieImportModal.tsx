// 从 ICloud.tsx 抽出的 Cookie 导入弹窗（纯展示，接线不动）。
import { useState } from 'react'
import { App, Form, Input, Modal, Select, Typography } from 'antd'

import { importICloudCookie } from '@/api/icloud'
import {
  DEFAULT_ICLOUD_IMAP_HOST,
  DEFAULT_ICLOUD_IMAP_PORT,
  ICLOUD_REGION_OPTIONS,
} from '@/lib/icloud'

const { Paragraph, Text } = Typography

export function CookieImportModal({
  open,
  onClose,
  onImported,
}: {
  open: boolean
  onClose: () => void
  onImported: () => void
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    const values = await form.validateFields()
    setBusy(true)
    try {
      const account = await importICloudCookie(values)
      message.success(`已导入主号 ${account.email}`)
      onImported()
      onClose()
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="手工导入 iCloud Cookie"
      onCancel={onClose}
      onOk={submit}
      confirmLoading={busy}
      okText="校验并导入"
      width={620}
      destroyOnHidden
    >
      <Paragraph type="secondary">
        在浏览器登录 iCloud 后，从 <Text code>setup/ws/1/validate</Text> 请求复制完整 Cookie。
        应用内登录不可用时才需要这种方式。
      </Paragraph>
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          region: 'global',
          imap_host: DEFAULT_ICLOUD_IMAP_HOST,
          imap_port: DEFAULT_ICLOUD_IMAP_PORT,
        }}
      >
        <Form.Item
          name="cookie_header"
          label="Cookie"
          rules={[{ required: true, message: '请粘贴 Cookie' }]}
        >
          <Input.TextArea rows={5} placeholder="X-APPLE-WEBAUTH-USER=...; X-APPLE-WEBAUTH-TOKEN=..." />
        </Form.Item>
        <Form.Item name="email" label="Apple ID（可选）" extra="留空则从 iCloud 返回的账号信息中读取">
          <Input placeholder="owner@icloud.com" />
        </Form.Item>
        <Form.Item name="region" label="账号区域">
          <Select options={ICLOUD_REGION_OPTIONS} />
        </Form.Item>
        <Form.Item name="imap_password" label="IMAP 应用专用密码（可选）">
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
    </Modal>
  )
}
