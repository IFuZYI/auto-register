import { Space, Switch, Tag, Typography } from 'antd'

const { Text } = Typography

type ChatGPTBind2faSwitchProps = {
  enabled: boolean
  onChange: (enabled: boolean) => void
}

export function ChatGPTBind2faSwitch({
  enabled,
  onChange,
}: ChatGPTBind2faSwitchProps) {
  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Space align="center" wrap>
        <Switch
          checked={enabled}
          checkedChildren="绑定"
          unCheckedChildren="不绑"
          onChange={onChange}
        />
        <Tag color={enabled ? 'processing' : 'default'}>
          {enabled ? '注册后自动绑' : '默认关闭'}
        </Tag>
      </Space>
      <Text type="secondary">
        {enabled
          ? '注册成功后自动绑定 TOTP 双因素，密钥会写进账号详情并打进任务日志。'
          : '不绑 2FA，登录只需密码或邮箱验证码。'}
      </Text>
      {enabled && (
        <Text type="warning">
          密钥只在绑定时下发一次，服务端取不回；丢了该号将无法登录。
        </Text>
      )}
    </Space>
  )
}
