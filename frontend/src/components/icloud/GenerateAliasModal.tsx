// 从 ICloud.tsx 抽出的生成别名弹窗（纯展示，接线不动）。
import { useEffect, useState } from 'react'
import { App, Form, Input, InputNumber, Modal, Select } from 'antd'

import { generateICloudAliases, type ICloudAccount } from '@/api/icloud'
import { ICLOUD_HOURLY_ALIAS_LIMIT } from '@/lib/icloud'

export function GenerateAliasModal({
  open,
  accounts,
  defaultAccountId,
  onClose,
  onGenerated,
}: {
  open: boolean
  accounts: ICloudAccount[]
  defaultAccountId?: number
  onClose: () => void
  onGenerated: () => void
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [busy, setBusy] = useState(false)

  // 只列**本地**账号：远程账号的别名由远端仓库自己管，本注册机不许在远端新建
  // （用户要求 2026-10-09），后端会回 400 —— 与其让用户选完再吃一个错误，
  // 不如一开始就不给这个选项。
  const localAccounts = accounts.filter((item) => item.source !== 'remote')

  useEffect(() => {
    if (open) form.setFieldsValue({ account_id: defaultAccountId, count: 1 })
  }, [open, defaultAccountId, form])

  const submit = async () => {
    const values = await form.validateFields()
    setBusy(true)
    try {
      const created = await generateICloudAliases(values)
      message.success(`已生成 ${created.length} 个隐私邮箱`)
      onGenerated()
      onClose()
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const remaining = localAccounts.find((item) => item.id === form.getFieldValue('account_id'))
    ?.quota.remaining

  return (
    <Modal
      open={open}
      title="生成隐私邮箱"
      onCancel={onClose}
      onOk={submit}
      confirmLoading={busy}
      okText="生成"
      destroyOnHidden
    >
      <Form form={form} layout="vertical">
        <Form.Item name="account_id" label="主号" rules={[{ required: true, message: '请选择主号' }]}>
          <Select
            options={localAccounts.map((item) => ({ value: item.id, label: item.email }))}
            notFoundContent="没有本地主号 —— 远程账号不支持生成隐私邮箱"
          />
        </Form.Item>
        <Form.Item
          name="count"
          label="生成数量"
          extra={
            remaining === undefined
              ? `Apple 限制：每主号每小时最多 ${ICLOUD_HOURLY_ALIAS_LIMIT} 个`
              : `该主号本小时剩余额度 ${remaining} 个`
          }
        >
          <InputNumber min={1} max={ICLOUD_HOURLY_ALIAS_LIMIT} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item name="label" label="标签（可选）">
          <Input placeholder="注册" />
        </Form.Item>
        <Form.Item name="note" label="备注（可选）">
          <Input placeholder="批量生成" />
        </Form.Item>
      </Form>
    </Modal>
  )
}
