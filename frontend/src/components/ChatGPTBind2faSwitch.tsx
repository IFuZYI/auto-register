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
    ? '注册成功后自动绑定 TOTP 双因素验证。密钥会保存到本地账号，可在账号详情复制；任务日志也可能包含密钥，请妥善保管。'
      + ' ⚠ 丢失密钥可能导致无法登录。'
    : '不绑 2FA，登录只需密码或邮箱验证码。'

  return (
    <Space align="center" wrap size={8}>
      <Switch
        aria-label="绑定 2FA"
        checked={enabled}
        checkedChildren="绑定"
        unCheckedChildren="不绑"
        onChange={onChange}
      />
      <Tag color={enabled ? 'processing' : 'default'}>
        {enabled ? '注册后自动绑' : '默认关闭'}
      </Tag>
      <Tooltip title={hint} trigger={['hover', 'focus']}>
        <span
          tabIndex={0}
          role="img"
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
