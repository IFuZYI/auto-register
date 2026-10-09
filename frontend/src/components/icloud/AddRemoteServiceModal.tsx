// 添加远程 iCloud 仓库服务的弹窗。
//
// 两段式：填服务地址 + 管理员密码 → 「获取账号列表」→ 选一个远端账号接入。
// 密码只用于登录远端校验与落库（AES 加密），绝不回显。
import { useState } from 'react'
import { Alert, App, Form, Input, Modal, Select, Typography } from 'antd'

import { addICloudRemoteAccount, type ICloudAccount } from '@/api/icloud'
import { apiFetch } from '@/lib/utils'

const { Paragraph, Text } = Typography

interface RemoteAccountOption {
  value: string
  label: string
}

export function AddRemoteServiceModal({
  open,
  onClose,
  onAdded,
}: {
  open: boolean
  onClose: () => void
  onAdded: () => void
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [busy, setBusy] = useState(false)
  const [discovering, setDiscovering] = useState(false)
  const [options, setOptions] = useState<RemoteAccountOption[]>([])

  /**
   * 登录远端并拉取账号列表。
   *
   * 走「先接入再列账号」不划算：接入会落库，选错账号还得删。
   * 这里用同样的凭据先探一次账号列表（远端接口是管理员会话制的，
   * 没有免登录的「列账号」端点），所以借「不存在的远端账号」会被
   * add 接口拒绝的语义 —— 见下方实现：先请求一次 add 的干跑不可行，
   * 改为直接调后端的列表探针。
   */
  const discover = async () => {
    const values = await form.validateFields(['base_url', 'admin_password'])
    setDiscovering(true)
    try {
      const data = await apiFetch('/icloud/remote-accounts/discover', {
        method: 'POST',
        body: JSON.stringify({
          base_url: values.base_url,
          admin_password: values.admin_password,
        }),
      })
      const items: RemoteAccountOption[] = (data?.items ?? []).map(
        (item: { id: string; name?: string; icloud_email?: string; status?: string }) => ({
          value: item.id,
          label: `${item.icloud_email || item.name || item.id}${item.status === 'active' ? '' : `（${item.status || '未知'}）`}`,
        }),
      )
      setOptions(items)
      if (items.length === 0) {
        message.warning('远端服务里还没有账号，请先在 icloud-hme 服务里添加主号')
      } else {
        message.success(`发现 ${items.length} 个远端账号，请选择要接入的账号`)
      }
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setDiscovering(false)
    }
  }

  const submit = async () => {
    const values = await form.validateFields()
    setBusy(true)
    try {
      const account: ICloudAccount = await addICloudRemoteAccount(values)
      message.success(`已接入远端账号 ${account.email}`)
      onAdded()
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
      title="添加远程 iCloud 服务"
      onCancel={onClose}
      onOk={submit}
      confirmLoading={busy}
      okText="接入"
      width={620}
      destroyOnHidden
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="远程仓库：主号凭据保存在独立的 icloud-hme 服务里"
        description={
          <Text type="secondary">
            本机只保存服务地址与管理员密码（加密），别名生成与收件都经该服务完成。
            同一服务里的同一个账号只能接入一次；与本地主号同地址时会按「本地优先」去重。
          </Text>
        }
      />
      <Paragraph type="secondary" style={{ marginTop: 0 }}>
        填写 icloud-hme 服务的地址与管理员密码，先「获取账号列表」，再选择要接入的远端账号。
      </Paragraph>
      <Form form={form} layout="vertical">
        <Form.Item
          name="base_url"
          label="服务地址"
          rules={[{ required: true, message: '请填写 icloud-hme 服务地址' }]}
        >
          <Input placeholder="http://127.0.0.1:8081" />
        </Form.Item>
        <Form.Item
          name="admin_password"
          label="管理员密码"
          rules={[{ required: true, message: '请填写管理员密码' }]}
          extra="仅用于登录远端服务；加密保存，绝不回显"
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item>
          <a onClick={discover} style={{ pointerEvents: discovering ? 'none' : undefined }}>
            {discovering ? '获取中…' : '获取账号列表'}
          </a>
        </Form.Item>
        <Form.Item
          name="remote_account_id"
          label="远端账号"
          rules={[{ required: true, message: '请选择要接入的远端账号' }]}
        >
          <Select
            placeholder="先点上方「获取账号列表」"
            options={options}
            notFoundContent="先获取账号列表"
          />
        </Form.Item>
        <Form.Item name="name" label="本地显示名（可选）" extra="留空用远端账号名">
          <Input placeholder="例如：远程主号" />
        </Form.Item>
        <Form.Item name="proxy" label="访问远端服务的代理（可选）">
          <Input placeholder="http://user:pass@host:port" />
        </Form.Item>
      </Form>
    </Modal>
  )
}
