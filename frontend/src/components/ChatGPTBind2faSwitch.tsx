import { Space, Switch, Tag, Tooltip } from 'antd'

type ChatGPTBind2faSwitchProps = {
  enabled: boolean
  onChange: (enabled: boolean) => void
}

export function ChatGPTBind2faSwitch({
  enabled,
  onChange,
}: ChatGPTBind2faSwitchProps) {
  // 说明进 tooltip。开启时的「密钥丢了就登不进去」是**警告**，但同样不该常驻
  // 占版面 —— 放进 tooltip 并用 ⚠ 图标提示严重性。
  const hint = enabled
    ? '注册成功后自动绑定 TOTP 双因素，密钥会写进账号详情并打进任务日志。'
      + ' ⚠ 密钥只在绑定时下发一次，服务端取不回；丢了该号将无法登录。'
    : '不绑 2FA，登录只需密码或邮箱验证码。'

  return (
    <Space align="center" wrap size={8}>
      <Switch
        checked={enabled}
        checkedChildren="绑定"
        unCheckedChildren="不绑"
        onChange={onChange}
      />
      <Tag color={enabled ? 'processing' : 'default'}>
        {enabled ? '注册后自动绑' : '默认关闭'}
      </Tag>
      <Tooltip title={hint}>
        <span
          aria-label="说明"
          style={{
            color: enabled ? 'var(--ant-color-warning, #d48806)' : 'var(--text-muted)',
            cursor: 'help',
            fontSize: 13,
          }}
        >
          {enabled ? '⚠' : 'ⓘ'}
        </span>
      </Tooltip>
    </Space>
  )
}
