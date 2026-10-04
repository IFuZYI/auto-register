import { useEffect, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Space,
  Steps,
  Typography,
} from 'antd'
import {
  cancelICloudLogin,
  resendICloudLoginCode,
  sendICloudLoginSMS,
  startICloudLogin,
  verifyICloudLogin,
  type ICloudAccount,
  type ICloudLoginPayload,
  type ICloudLoginState,
} from '@/api/icloud'
import { DEFAULT_ICLOUD_IMAP_HOST, DEFAULT_ICLOUD_IMAP_PORT, ICLOUD_REGION_OPTIONS } from '@/lib/icloud'

const { Text, Paragraph } = Typography

const DELIVERY_HINTS: Record<string, string> = {
  trusted_devices: '验证码已推送到该 Apple ID 的受信任设备。',
  sms: '验证码已通过短信发送到受信任手机号。',
  sms_selection_required: '该账号没有受信任设备，请先选择接收验证码的手机号。',
}

interface Props {
  open: boolean
  onClose: () => void
  onCompleted: (account: ICloudAccount) => void
}

export function ICloudLoginModal({ open, onClose, onCompleted }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<ICloudLoginPayload>()
  const [login, setLogin] = useState<ICloudLoginState | null>(null)
  const [code, setCode] = useState('')
  const [phoneId, setPhoneId] = useState<number | undefined>()
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (open) {
      form.resetFields()
      setLogin(null)
      setCode('')
      setPhoneId(undefined)
    }
  }, [open, form])

  const finish = (state: ICloudLoginState) => {
    if (state.status !== 'completed' || !state.account) {
      setLogin(state)
      return
    }
    message.success(`iCloud 主号 ${state.account.email} 登录成功`)
    onCompleted(state.account)
    onClose()
  }

  const run = async (action: () => Promise<ICloudLoginState>, successText?: string) => {
    setBusy(true)
    try {
      const state = await action()
      if (successText && state.status !== 'completed') message.success(successText)
      finish(state)
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const submitCredentials = async () => {
    const values = await form.validateFields()
    await run(() => startICloudLogin(values))
  }

  const cancel = () => {
    if (login && login.status !== 'completed') {
      cancelICloudLogin(login.login_id).catch(() => {})
    }
    onClose()
  }

  const phones = login?.trusted_phone_numbers ?? []
  const needsPhoneChoice = login?.delivery === 'sms_selection_required'

  return (
    <Modal
      open={open}
      onCancel={cancel}
      title="Apple ID 应用内登录"
      width={560}
      destroyOnHidden
      footer={
        login ? (
          <Space>
            <Button onClick={cancel}>取消</Button>
            {!needsPhoneChoice && (
              <Button onClick={() => run(() => resendICloudLoginCode(login.login_id), '验证码已重新发送')}>
                重新发送
              </Button>
            )}
            <Button
              type="primary"
              loading={busy}
              disabled={needsPhoneChoice || code.trim().length !== 6}
              onClick={() => run(() => verifyICloudLogin(login.login_id, code.trim()))}
            >
              验证并保存
            </Button>
          </Space>
        ) : (
          <Space>
            <Button onClick={cancel}>取消</Button>
            <Button type="primary" loading={busy} onClick={submitCredentials}>
              登录
            </Button>
          </Space>
        )
      }
    >
      <Steps
        size="small"
        current={login ? 1 : 0}
        items={[{ title: '填写账号' }, { title: '双重认证' }]}
        style={{ marginBottom: 20 }}
      />

      {!login && (
        <Form
          form={form}
          layout="vertical"
          initialValues={{
            region: 'global',
            imap_host: DEFAULT_ICLOUD_IMAP_HOST,
            imap_port: DEFAULT_ICLOUD_IMAP_PORT,
          }}
        >
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message="Apple ID 密码只参与本次登录握手，不会保存；收件使用单独生成的“应用专用密码”。"
          />
          <Form.Item name="email" label="Apple ID" rules={[{ required: true, message: '请输入 Apple ID' }]}>
            <Input placeholder="owner@icloud.com" autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="password"
            label="Apple ID 密码"
            rules={[{ required: true, message: '请输入 Apple ID 密码' }]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="imap_password"
            label="IMAP 应用专用密码"
            extra="在 Apple 账号页面单独生成，用于连接 iCloud IMAP 收件"
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="region" label="账号区域">
            <Select options={ICLOUD_REGION_OPTIONS} />
          </Form.Item>
          <Form.Item name="display_name" label="备注名称（可选）">
            <Input placeholder="主号备注" />
          </Form.Item>
          <Space size={16}>
            <Form.Item name="imap_host" label="IMAP 服务器">
              <Input style={{ width: 220 }} />
            </Form.Item>
            <Form.Item name="imap_port" label="端口">
              <InputNumber min={1} max={65535} />
            </Form.Item>
          </Space>
        </Form>
      )}

      {login && (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Alert type="info" showIcon message={DELIVERY_HINTS[login.delivery] ?? '请输入受信任设备上的验证码。'} />

          {needsPhoneChoice ? (
            <>
              <Radio.Group value={phoneId} onChange={(event) => setPhoneId(event.target.value)}>
                <Space direction="vertical">
                  {phones.map((phone) => (
                    <Radio key={phone.id} value={phone.id}>
                      {phone.number}
                    </Radio>
                  ))}
                </Space>
              </Radio.Group>
              <Button
                type="primary"
                loading={busy}
                disabled={phoneId === undefined}
                onClick={() =>
                  run(() => sendICloudLoginSMS(login.login_id, phoneId as number), '短信验证码已发送')
                }
              >
                发送短信验证码
              </Button>
            </>
          ) : (
            <>
              <Input
                size="large"
                maxLength={6}
                value={code}
                placeholder="6 位验证码"
                onChange={(event) => setCode(event.target.value.replace(/\D/g, ''))}
                style={{ letterSpacing: 8, textAlign: 'center' }}
              />
              {phones.length > 0 && (
                <Paragraph type="secondary" style={{ marginBottom: 0 }}>
                  收不到推送？改用短信：
                  <Space wrap style={{ marginLeft: 8 }}>
                    {phones.map((phone) => (
                      <Button
                        key={phone.id}
                        size="small"
                        onClick={() =>
                          run(() => sendICloudLoginSMS(login.login_id, phone.id), '短信验证码已发送')
                        }
                      >
                        {phone.number}
                      </Button>
                    ))}
                  </Space>
                </Paragraph>
              )}
              <Text type="secondary">登录会话 10 分钟内有效，超时后需要重新登录。</Text>
            </>
          )}
        </Space>
      )}
    </Modal>
  )
}
